"""讯飞「实时语音转写标准版」：APPID + APIKey、二进制音频协议。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import websocket
from dotenv import dotenv_values

from xfyun_asr import XfyunCancelled, XfyunError, XfyunRecognizer


@dataclass(frozen=True)
class XfyunRtasrConfig:
    app_id: str = field(repr=False)
    api_key: str = field(repr=False)
    lang: str = "cn"
    connect_timeout: float = 8.0
    started_timeout: float = 8.0
    poll_timeout: float = 0.2
    final_timeout: float = 15.0
    send_idle_timeout: float = 12.0

    def __post_init__(self) -> None:
        missing = [name for name, value in (("XFYUN_APP_ID", self.app_id), ("XFYUN_API_KEY", self.api_key))
                   if not isinstance(value, str) or not value.strip()]
        if missing:
            raise ValueError("请在 .env 中配置实时语音转写标准版凭据：" + "、".join(missing))
        if not isinstance(self.lang, str) or not self.lang.strip():
            raise ValueError("实时语音转写的 lang 不能为空。")
        for value in (self.connect_timeout, self.started_timeout, self.poll_timeout,
                      self.final_timeout, self.send_idle_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("实时语音转写超时时间必须为正的有限数字。")
        if self.send_idle_timeout >= 15:
            raise ValueError("本地音频发送空闲超时必须小于服务端的 15 秒限制。")

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> XfyunRtasrConfig:
        path = Path(env_path) if env_path is not None else Path(__file__).resolve().parent / ".env"
        settings = dotenv_values(path)

        def setting(name: str) -> str:
            value = os.environ[name] if name in os.environ else settings.get(name)
            return (value or "").strip()

        return cls(setting("XFYUN_APP_ID"), setting("XFYUN_API_KEY"))


class _RtasrTranscript:
    def __init__(self) -> None:
        # seg_id 是结果序号，bg 在临时结果中可能漂移；最终标点还可能
        # 单独返回。按协议状态保存已稳定文本和最新的一份临时文本。
        self.stable_parts: list[str] = []
        self.pending: str | None = None
        self.latest_result_id = -1

    @property
    def has_pending(self) -> bool:
        return self.pending is not None

    def text(self) -> str:
        return "".join(self.stable_parts) + (self.pending or "")

    def apply(self, raw: Any) -> str:
        if not isinstance(raw, str):
            raise XfyunError("实时转写结果 data 应为 JSON 字符串。")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            raise XfyunError("无法解析实时语音转写的句子数据。") from None
        if not isinstance(payload, dict):
            raise XfyunError("实时语音转写句子格式不正确。")
        seg_id = payload.get("seg_id")
        if isinstance(seg_id, bool) or not isinstance(seg_id, int) or seg_id < 0:
            raise XfyunError("实时语音转写返回了无效的 seg_id。")
        if seg_id <= self.latest_result_id:
            # WebSocket 有序传输；重放的旧结果不能覆盖新文本或重复追加。
            return self.text()
        cn = payload.get("cn")
        st = cn.get("st") if isinstance(cn, dict) else None
        if not isinstance(st, dict) or st.get("type") not in ("0", "1"):
            raise XfyunError("实时语音转写返回了无效的句子状态。")
        stable = st["type"] == "0"
        blocks = st.get("rt")
        if not isinstance(blocks, list):
            raise XfyunError("实时语音转写返回的分词结构不正确。")
        words: list[str] = []
        for block in blocks:
            tokens = block.get("ws") if isinstance(block, dict) else None
            if not isinstance(tokens, list):
                raise XfyunError("实时语音转写返回的分词结构不正确。")
            for token in tokens:
                candidates = token.get("cw") if isinstance(token, dict) else None
                if not isinstance(candidates, list):
                    raise XfyunError("实时语音转写返回的候选词结构不正确。")
                if not candidates:
                    continue
                first = candidates[0]
                if not isinstance(first, dict) or not isinstance(first.get("w"), str):
                    raise XfyunError("实时语音转写返回的候选词结构不正确。")
                words.append(first["w"])
        text = "".join(words)
        self.latest_result_id = seg_id
        if stable:
            self.stable_parts.append(text)
            self.pending = None
        else:
            self.pending = text
        return self.text()


class XfyunRtasrRecognizer(XfyunRecognizer):
    """仅复用可取消的连接/关闭辅助；鉴权、收发、完成条件均使用 RTASR 协议。"""

    ENDPOINT = "wss://rtasr.xfyun.cn/v1/ws"
    _provider_lease = threading.Lock()

    def __init__(
        self,
        config: XfyunRtasrConfig,
        *,
        create_connection: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[threading.Event, float], bool] | None = None,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(config, create_connection=create_connection, monotonic=monotonic,
                         wait=wait, utcnow=utcnow)
        self._connect_done: threading.Event | None = None
        self._sender_done: threading.Event | None = None

    @staticmethod
    def _close_socket(connection: Any, *, abort: bool = False) -> None:
        try:
            XfyunRecognizer._close_socket(connection, abort=abort)
        finally:
            # recv_data 收到 CLOSE 后会把 connected 置 False，此时 close() 可能
            # 直接返回；显式 shutdown，避免底层 socket 泄漏占住唯一并发额度。
            try:
                shutdown = getattr(connection, "shutdown", None)
                if callable(shutdown):
                    shutdown()
            except Exception:
                try:
                    raw_socket = getattr(connection, "sock", None)
                    if raw_socket is not None:
                        raw_socket.close()
                except Exception:
                    pass

    def _signed_url(self) -> str:
        current = self._utcnow()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        ts = str(int(current.timestamp()))
        digest = hashlib.md5((self.config.app_id + ts).encode("utf-8")).hexdigest()
        signature = hmac.new(self.config.api_key.encode("utf-8"), digest.encode("ascii"), hashlib.sha1).digest()
        parameters = {
            "appid": self.config.app_id, "ts": ts,
            "signa": base64.b64encode(signature).decode("ascii"),
        }
        # 官方默认中文，使用经真实转写验证的最小请求；其他语种显式指定。
        if self.config.lang != "cn":
            parameters["lang"] = self.config.lang
        return self.ENDPOINT + "?" + urlencode(parameters)

    def _connect(self, stop_event: threading.Event) -> Any:
        completed = threading.Event()
        abandoned = threading.Event()
        lock = threading.Lock()
        result: dict[str, Any] = {}
        self._connect_done = completed

        def connect() -> None:
            try:
                connection = self._create_connection(
                    self._signed_url(), timeout=self.config.connect_timeout, enable_multithread=True)
                with lock:
                    if abandoned.is_set():
                        self._close_socket(connection, abort=True)
                    else:
                        result["connection"] = connection
            except Exception:
                result["failed"] = True
            finally:
                # 晚到 socket 也关闭完毕之后，才能释放唯一的 provider 连接租约。
                completed.set()

        try:
            threading.Thread(target=connect, daemon=True, name="xfyun-rtasr-connect").start()
        except Exception:
            completed.set()
            raise XfyunError("无法启动实时语音服务连接。") from None
        deadline = self._clock() + self.config.connect_timeout
        while not completed.wait(0.05):
            if stop_event.is_set() or self._clock() >= deadline:
                with lock:
                    abandoned.set()
                    connection = result.get("connection")
                if connection is not None:
                    self._close_socket(connection, abort=True)
                if stop_event.is_set():
                    raise XfyunCancelled("本次语音识别已取消。")
                raise XfyunError("连接实时语音服务超时，请检查网络。")
        if stop_event.is_set():
            if "connection" in result:
                self._close_socket(result["connection"], abort=True)
            raise XfyunCancelled("本次语音识别已取消。")
        if result.get("failed") or "connection" not in result:
            raise XfyunError("连接实时语音服务失败，请检查网络与标准版授权。")
        return result["connection"]

    @staticmethod
    def _packet(raw: Any) -> dict[str, Any]:
        try:
            packet = json.loads(raw)
        except (TypeError, ValueError):
            raise XfyunError("无法解析实时语音转写返回的数据。") from None
        if not isinstance(packet, dict):
            raise XfyunError("实时语音转写返回的数据格式不正确。")
        if packet.get("action") == "error" or packet.get("code") != "0":
            code = packet.get("code")
            safe_code = code if isinstance(code, str) and code.isascii() and code.isdecimal() else "未知"
            raise XfyunError(f"实时语音转写失败（错误码 {safe_code}），请检查标准版授权、额度和配置。")
        return packet

    @staticmethod
    def _clean_close(data: Any) -> bool:
        if not isinstance(data, bytes):
            return False
        # RFC 6455 允许不带状态码的 CLOSE；1000 表示正常完成。
        return not data or (len(data) >= 2 and int.from_bytes(data[:2], "big") == 1000)

    def _receive(self, connection: Any, stop_event: threading.Event) -> tuple[int, Any] | None:
        try:
            return connection.recv_data(control_frame=True)
        except (websocket.WebSocketTimeoutException, TimeoutError):
            return None
        except Exception:
            if stop_event.is_set():
                raise XfyunCancelled("本次语音识别已取消。") from None
            raise XfyunError("实时语音转写连接异常中断，未确认全部结果接收完成。") from None

    def transcribe(self, frames: Iterable[bytes], on_partial: Callable[[str], None],
                   stop_event: threading.Event) -> str:
        while not self._provider_lease.acquire(timeout=0.05):
            if stop_event.is_set():
                raise XfyunCancelled("等待上一轮实时语音连接结束时已取消。")
        self._connect_done = None
        self._sender_done = None
        try:
            return self._transcribe(frames, on_partial, stop_event)
        finally:
            pending = [event for event in (self._connect_done, self._sender_done)
                       if event is not None and not event.is_set()]
            if pending:
                def release_later() -> None:
                    for event in pending:
                        event.wait()
                    self._provider_lease.release()
                threading.Thread(target=release_later, daemon=True, name="xfyun-rtasr-release").start()
            else:
                self._provider_lease.release()

    def _transcribe(self, frames: Iterable[bytes], on_partial: Callable[[str], None],
                    stop_event: threading.Event) -> str:
        if stop_event.is_set():
            raise XfyunCancelled("本次语音识别已取消。")
        connection = self._connect(stop_event)
        halted = threading.Event()
        upload_done = threading.Event()
        upload_errors: list[XfyunError] = []
        last_sent_at = [self._clock()]
        ended_at: list[float] = []
        sender: threading.Thread | None = None
        completed = False
        sender_done = threading.Event()
        sender_done.set()
        self._sender_done = sender_done

        def should_stop() -> bool:
            return halted.is_set() or stop_event.is_set()

        def send_audio() -> None:
            last_send: float | None = None

            def restore(frame: bytes) -> None:
                putback = getattr(frames, "putback_unsent", None)
                if callable(putback):
                    putback(frame)

            def send(data: bytes) -> bool:
                nonlocal last_send
                if last_send is not None:
                    delay = last_send + self.FRAME_SECONDS - self._clock()
                    if delay > 0:
                        self._wait(stop_event, delay)
                if should_stop():
                    return False
                connection.send_binary(data)
                last_send = self._clock()
                last_sent_at[0] = last_send
                return True

            try:
                for frame in frames:
                    if should_stop():
                        restore(frame)
                        return
                    if not isinstance(frame, bytes) or len(frame) != self.FRAME_BYTES:
                        raise XfyunError("音频帧必须为 1280 字节的 16 kHz 单声道 PCM16 数据。")
                    if not send(frame):
                        restore(frame)
                        return
                if not should_stop() and send(b'{"end": true}'):
                    ended_at.append(self._clock())
                    upload_done.set()
            except XfyunError as exc:
                upload_errors.append(exc)
            except Exception:
                upload_errors.append(XfyunError("读取或发送实时语音音频失败，请检查采集和网络。"))
            finally:
                sender_done.set()

        try:
            try:
                connection.settimeout(self.config.poll_timeout)
            except Exception:
                raise XfyunError("无法设置实时语音转写连接超时。") from None
            deadline = self._clock() + self.config.started_timeout
            while True:
                if stop_event.is_set():
                    raise XfyunCancelled("本次语音识别已取消。")
                if self._clock() >= deadline:
                    raise XfyunError("等待实时语音转写 started 确认超时。")
                incoming = self._receive(connection, stop_event)
                if incoming is None:
                    continue
                opcode, raw = incoming
                if opcode in (websocket.ABNF.OPCODE_PING, websocket.ABNF.OPCODE_PONG):
                    continue
                if opcode != websocket.ABNF.OPCODE_TEXT:
                    raise XfyunError("实时语音转写在 started 确认前结束或返回了错误帧。")
                packet = self._packet(raw)
                if packet.get("action") != "started":
                    raise XfyunError("实时语音转写未返回 started 确认，音频未上传。")
                break
            if stop_event.is_set():
                raise XfyunCancelled("本次语音识别已取消。")
            last_sent_at[0] = self._clock()
            sender = threading.Thread(target=send_audio, daemon=True, name="xfyun-rtasr-send")
            sender_done.clear()
            try:
                sender.start()
            except Exception:
                sender_done.set()
                sender = None
                raise XfyunError("无法启动实时音频发送。") from None
            transcript = _RtasrTranscript()
            last_text = ""
            while True:
                if stop_event.is_set():
                    raise XfyunCancelled("本次语音识别已取消。")
                if upload_errors:
                    raise upload_errors[0]
                now = self._clock()
                if upload_done.is_set():
                    if now - ended_at[0] >= self.config.final_timeout:
                        raise XfyunError("等待实时语音转写最终结果与正常关闭超时，临时文本未提交。")
                elif now - last_sent_at[0] >= self.config.send_idle_timeout:
                    raise XfyunError("实时语音音频发送间隔过长，请检查声音采集。")
                # 标准版静音时可能不返回任何结果；不能用收包空闲时间中止有效上传。
                incoming = self._receive(connection, stop_event)
                if incoming is None:
                    continue
                if stop_event.is_set():
                    raise XfyunCancelled("本次语音识别已取消。")
                opcode, raw = incoming
                if opcode in (websocket.ABNF.OPCODE_PING, websocket.ABNF.OPCODE_PONG):
                    continue
                if opcode == websocket.ABNF.OPCODE_CLOSE:
                    # recv_data 自动回复 CLOSE；同步确认 sender 已发完 end。
                    if not self._clean_close(raw):
                        raise XfyunError("实时语音转写以异常状态关闭，结果未提交。")
                    if not upload_done.wait(timeout=self.config.poll_timeout):
                        raise XfyunError("实时语音转写在音频上传完成前关闭，结果未提交。")
                    if transcript.has_pending:
                        raise XfyunError("实时语音转写仍有未稳定的临时句子，结果未提交。")
                    completed = True
                    return transcript.text().strip()
                if opcode != websocket.ABNF.OPCODE_TEXT:
                    raise XfyunError("实时语音转写返回了非文本结果帧。")
                packet = self._packet(raw)
                if packet.get("action") != "result":
                    raise XfyunError("实时语音转写返回了未知 action。")
                text = transcript.apply(packet.get("data"))
                if text != last_text:
                    try:
                        on_partial(text)
                    except Exception:
                        raise XfyunError("更新实时转写文字失败。") from None
                    last_text = text
        finally:
            halted.set()
            cancel_failed = False
            try:
                cancel = getattr(frames, "cancel", None)
                if callable(cancel):
                    cancel()
            except Exception:
                cancel_failed = True
            self._close_socket(connection, abort=stop_event.is_set())
            if sender is not None:
                sender.join(timeout=0.5)
                if completed and sender.is_alive():
                    raise XfyunError("上一段实时音频尚未停止读取，结果未提交。")
            if completed and upload_errors:
                raise upload_errors[0]
            if completed and cancel_failed:
                raise XfyunError("停止本段实时音频读取失败，结果未提交。")
