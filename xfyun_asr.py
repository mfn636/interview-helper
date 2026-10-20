"""讯飞流式听写：16 kHz、16 位、小端、单声道 PCM 音频转写。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import websocket
from dotenv import dotenv_values


class XfyunError(RuntimeError):
    """可直接向用户展示的识别错误，不包含鉴权参数或原始网络异常。"""


class XfyunCancelled(XfyunError):
    """本次听写已取消；已显示的临时结果不应提交给 AI。"""


@dataclass(frozen=True)
class XfyunConfig:
    app_id: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    eos_ms: int = 2000
    connect_timeout: float = 8.0
    poll_timeout: float = 0.2
    response_timeout: float = 15.0
    final_timeout: float = 10.0
    session_timeout: float = 65.0

    def __post_init__(self) -> None:
        names = ("XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET")
        missing = [name for name, value in zip(names, (self.app_id, self.api_key, self.api_secret))
                   if not isinstance(value, str) or not value.strip()]
        if missing:
            raise ValueError("请在 .env 中配置讯飞语音听写凭据：" + "、".join(missing))
        if isinstance(self.eos_ms, bool) or not isinstance(self.eos_ms, int) or not 0 < self.eos_ms <= 10000:
            raise ValueError("讯飞 eos_ms 必须为 1～10000 毫秒。")
        if any(value <= 0 for value in (self.connect_timeout, self.poll_timeout,
                                       self.response_timeout, self.final_timeout, self.session_timeout)):
            raise ValueError("讯飞连接与识别超时时间必须大于 0。")

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> XfyunConfig:
        """系统环境变量优先于项目 .env；只读取凭据，不修改进程环境。"""
        path = Path(env_path) if env_path is not None else Path(__file__).resolve().parent / ".env"
        settings = dotenv_values(path)

        def setting(name: str) -> str:
            value = os.environ[name] if name in os.environ else settings.get(name)
            return (value or "").strip()

        return cls(app_id=setting("XFYUN_APP_ID"), api_key=setting("XFYUN_API_KEY"),
                   api_secret=setting("XFYUN_API_SECRET"))


class _Transcript:
    """按 sn 保存片段，rpl 的 rg 是闭区间，不是当前字符串的下标。"""

    def __init__(self) -> None:
        self.parts: dict[int, str] = {}

    def apply(self, result: dict[str, Any]) -> str:
        sn = result.get("sn")
        if isinstance(sn, bool) or not isinstance(sn, int) or sn < 0:
            raise XfyunError("讯飞返回了无效的听写片段序号。")
        mode = result.get("pgs", "apd")
        if mode not in ("apd", "rpl"):
            raise XfyunError("讯飞返回了无法识别的动态修正类型。")
        words = result.get("ws", [])
        if not isinstance(words, list):
            raise XfyunError("讯飞返回的听写文本格式不正确。")
        text_parts: list[str] = []
        for word in words:
            if not isinstance(word, dict) or not isinstance(word.get("cw"), list):
                raise XfyunError("讯飞返回的听写文本格式不正确。")
            candidates = word["cw"]
            if not candidates:
                continue
            if not isinstance(candidates[0], dict) or not isinstance(candidates[0].get("w"), str):
                raise XfyunError("讯飞返回的听写文本格式不正确。")
            text_parts.append(candidates[0]["w"])
        if mode == "rpl":
            span = result.get("rg")
            if (not isinstance(span, list) or len(span) != 2
                    or any(isinstance(item, bool) or not isinstance(item, int) for item in span)
                    or not 0 <= span[0] <= span[1]):
                raise XfyunError("讯飞返回了无效的动态修正范围。")
            for old_sn in tuple(self.parts):
                if span[0] <= old_sn <= span[1]:
                    del self.parts[old_sn]
        self.parts[sn] = "".join(text_parts)
        return "".join(self.parts[index] for index in sorted(self.parts))


class XfyunRecognizer:
    FRAME_BYTES = 1280
    FRAME_SECONDS = 0.04
    ENDPOINT = "wss://iat-api.xfyun.cn/v2/iat"

    def __init__(
        self,
        config: XfyunConfig,
        *,
        create_connection: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[threading.Event, float], bool] | None = None,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._create_connection = create_connection or websocket.create_connection
        self._clock = monotonic
        self._wait = wait or (lambda event, seconds: event.wait(seconds))
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))

    def _signed_url(self) -> str:
        current = self._utcnow()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        date = format_datetime(current.astimezone(timezone.utc), usegmt=True)
        host = "iat-api.xfyun.cn"
        signing_text = f"host: {host}\ndate: {date}\nGET /v2/iat HTTP/1.1"
        digest = hmac.new(self.config.api_secret.encode("utf-8"),
                          signing_text.encode("utf-8"), hashlib.sha256).digest()
        signature = base64.b64encode(digest).decode("ascii")
        authorization = (f'api_key="{self.config.api_key}",algorithm="hmac-sha256",'
                         f'headers="host date request-line",signature="{signature}"')
        return self.ENDPOINT + "?" + urlencode({
            "authorization": base64.b64encode(authorization.encode("utf-8")).decode("ascii"),
            "date": date,
            "host": host,
        })

    @staticmethod
    def _close_socket(connection: Any, *, abort: bool = False) -> None:
        if abort:
            try:
                connection.abort()
            except Exception:
                pass
        try:
            connection.close(timeout=0.1)
        except Exception:
            pass

    def _connect(self, stop_event: threading.Event) -> Any:
        """连接在线程中进行，使取消不必等待整个握手超时。"""
        completed = threading.Event()
        abandoned = threading.Event()
        lock = threading.Lock()
        result: dict[str, Any] = {}

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
                # 网络库异常可能包含完整签名 URL，不能保存或向上传递原异常。
                result["failed"] = True
            finally:
                completed.set()

        threading.Thread(target=connect, daemon=True, name="xfyun-connect").start()
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
                raise XfyunError("连接讯飞语音服务超时，请检查网络。")
        if stop_event.is_set():
            if "connection" in result:
                self._close_socket(result["connection"], abort=True)
            raise XfyunCancelled("本次语音识别已取消。")
        if result.get("failed") or "connection" not in result:
            raise XfyunError("连接讯飞语音服务失败，请检查网络、凭据和服务授权。")
        return result["connection"]

    def transcribe(
        self,
        frames: Iterable[bytes],
        on_partial: Callable[[str], None],
        stop_event: threading.Event,
    ) -> str:
        """一轮听写。frames 每帧须为 40 ms PCM；停止时调用方也须停止产出帧。

        on_partial 返回当前整句（可被动态修正），不是增量字符。只有收到服务端
        data.status=2 才返回最终文本；断连、超时和取消均抛异常，禁止误发临时结果。
        """
        if stop_event.is_set():
            raise XfyunCancelled("本次语音识别已取消。")
        connection = self._connect(stop_event)
        halted = threading.Event()
        upload_done = threading.Event()
        upload_errors: list[XfyunError] = []
        ended_at: list[float] = []
        sender: threading.Thread | None = None
        started_at = self._clock()
        final_received = False

        def should_stop() -> bool:
            return halted.is_set() or stop_event.is_set()

        def send_audio() -> None:
            last_send: float | None = None

            def putback_unsent(frame: bytes) -> None:
                restore = getattr(frames, "putback_unsent", None)
                if callable(restore):
                    restore(frame)

            def send(frame: bytes, status: int) -> bool:
                nonlocal last_send
                if last_send is not None:
                    delay = last_send + self.FRAME_SECONDS - self._clock()
                    if delay > 0:
                        self._wait(stop_event, delay)
                if should_stop():
                    return False
                payload: dict[str, Any] = {"data": {
                    "status": status,
                    "format": "audio/L16;rate=16000",
                    "encoding": "raw",
                    "audio": base64.b64encode(frame).decode("ascii"),
                }}
                if status == 0:
                    payload["common"] = {"app_id": self.config.app_id}
                    payload["business"] = {"language": "zh_cn", "domain": "iat",
                                           "accent": "mandarin", "dwa": "wpgs",
                                           "eos": self.config.eos_ms, "ptt": 1}
                connection.send(json.dumps(payload, ensure_ascii=False))
                # 以实际发送完成时间为基准，积压音频也不能突发追赶发送。
                last_send = self._clock()
                return True

            try:
                first = True
                byte_count = 0
                for frame in frames:
                    if should_stop():
                        putback_unsent(frame)
                        return
                    if not isinstance(frame, bytes) or len(frame) != self.FRAME_BYTES:
                        raise XfyunError("音频帧必须为 1280 字节的 16 kHz 单声道 PCM16 数据。")
                    byte_count += len(frame)
                    if byte_count > 60 * 16000 * 2:
                        raise XfyunError("单轮讯飞听写音频不能超过 60 秒，请分段识别。")
                    if not send(frame, 0 if first else 1):
                        putback_unsent(frame)
                        return
                    first = False
                if should_stop():
                    return
                if first and not send(b"", 0):
                    return
                if send(b"", 2):
                    ended_at.append(self._clock())
                    upload_done.set()
            except XfyunError as exc:
                upload_errors.append(exc)
            except Exception:
                upload_errors.append(XfyunError("读取或发送音频失败，请检查声音采集和网络连接。"))

        transcript = _Transcript()
        last_text = ""
        last_received_at = started_at
        try:
            try:
                connection.settimeout(self.config.poll_timeout)
            except Exception:
                raise XfyunError("无法设置讯飞语音连接超时。") from None
            sender = threading.Thread(target=send_audio, daemon=True, name="xfyun-send")
            sender.start()
            while True:
                if stop_event.is_set():
                    raise XfyunCancelled("本次语音识别已取消。")
                if upload_errors:
                    raise upload_errors[0]
                now = self._clock()
                if now - started_at >= self.config.session_timeout:
                    raise XfyunError("讯飞单轮语音识别超时，请缩短单次问题。")
                if upload_done.is_set() and now - ended_at[0] >= self.config.final_timeout:
                    raise XfyunError("等待讯飞最终识别结果超时，本次临时文本未提交。")
                if now - last_received_at >= self.config.response_timeout:
                    raise XfyunError("讯飞语音服务长时间未返回结果，请检查网络。")
                try:
                    raw = connection.recv()
                except (websocket.WebSocketTimeoutException, TimeoutError):
                    continue
                except Exception:
                    if stop_event.is_set():
                        raise XfyunCancelled("本次语音识别已取消。") from None
                    raise XfyunError("讯飞语音连接中断，未收到完整的最终识别结果。") from None
                if stop_event.is_set():
                    raise XfyunCancelled("本次语音识别已取消。")
                if not raw:
                    raise XfyunError("讯飞语音连接已关闭，未收到完整的最终识别结果。")
                try:
                    packet = json.loads(raw)
                except (ValueError, TypeError):
                    raise XfyunError("讯飞返回了无法解析的识别数据。") from None
                if not isinstance(packet, dict):
                    raise XfyunError("讯飞返回的识别数据格式不正确。")
                code = packet.get("code")
                if isinstance(code, bool) or not isinstance(code, int):
                    raise XfyunError("讯飞返回的识别状态格式不正确。")
                if code != 0:
                    # 只显示数字错误码，避免服务端原始 message 回显密钥或请求 URL。
                    raise XfyunError(f"讯飞语音识别失败（错误码 {code}），请检查服务授权、额度和配置。")
                data = packet.get("data")
                status = data.get("status") if isinstance(data, dict) else None
                if (not isinstance(data, dict) or isinstance(status, bool)
                        or not isinstance(status, int) or status not in (0, 1, 2)):
                    raise XfyunError("讯飞返回的识别结果状态不正确。")
                last_received_at = self._clock()
                if "result" in data:
                    if not isinstance(data["result"], dict):
                        raise XfyunError("讯飞返回的识别结果格式不正确。")
                    text = transcript.apply(data["result"])
                    if text != last_text:
                        try:
                            on_partial(text)
                        except Exception:
                            raise XfyunError("更新实时转写文字失败。") from None
                        last_text = text
                if stop_event.is_set():
                    raise XfyunCancelled("本次语音识别已取消。")
                if data["status"] == 2:
                    final_received = True
                    return last_text.strip()
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
                if final_received and sender.is_alive():
                    raise XfyunError("上一段音频尚未停止读取，本次识别结果未提交。")
            if final_received and upload_errors:
                raise upload_errors[0]
            if final_received and cancel_failed:
                raise XfyunError("停止本段音频读取失败，本次识别结果未提交。")
