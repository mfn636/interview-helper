"""后台系统声音听写：本地停顿切句，讯飞稳定结果才交给聊天层。"""

from array import array
from collections import deque
from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
from threading import Event, Lock, Thread
import time
from typing import Literal

from dotenv import dotenv_values

from system_audio import FRAME_BYTES, FRAME_MS, SystemAudioError, SystemAudioSource
from xfyun_asr import XfyunCancelled, XfyunConfig, XfyunError
from xfyun_rtasr import XfyunRtasrConfig, XfyunRtasrRecognizer


@dataclass(frozen=True)
class SpeechSettings:
    silence_ms: int = 1500
    rms_threshold: int = 180
    max_segment_ms: int = 50000

    def __post_init__(self):
        for name, value, low, high in (
            ("SPEECH_SILENCE_MS", self.silence_ms, 400, 5000),
            ("SPEECH_RMS_THRESHOLD", self.rms_threshold, 1, 32767),
            ("SPEECH_MAX_SEGMENT_MS", self.max_segment_ms, 1000, 50000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} 必须为 {low}～{high} 的整数。")

    @classmethod
    def from_env(cls, env_path=None):
        path = Path(env_path) if env_path is not None else Path(__file__).resolve().parent / ".env"
        values = dotenv_values(path)

        def number(name, default):
            value = os.environ.get(name, values.get(name))
            try:
                return int(value) if value is not None and str(value).strip() else default
            except (ValueError, TypeError):
                raise ValueError(f"{name} 必须是整数。") from None

        return cls(number("SPEECH_SILENCE_MS", 1500), number("SPEECH_RMS_THRESHOLD", 180),
                   number("SPEECH_MAX_SEGMENT_MS", 50000))


@dataclass(frozen=True)
class SpeechEvent:
    generation: int
    kind: Literal["started", "partial", "final", "error", "stopped"]
    utterance_id: str = ""
    text: str = ""
    endpoint: bool = True


def _rms(frame):
    if not isinstance(frame, bytes) or len(frame) != FRAME_BYTES:
        raise SystemAudioError("采集音频必须为 1280 字节的单声道 PCM16 数据。")
    samples = array("h")
    samples.frombytes(frame)
    import sys
    if sys.byteorder != "little":
        samples.byteswap()
    return math.sqrt(sum(value * value for value in samples) / len(samples))


class _Gate:
    def __init__(self, settings, clock):
        self.settings = settings
        self.clock = clock
        self.quiet_ms = 0
        self.last_frame_at = clock()

    def observe(self, frame):
        voiced = _rms(frame) >= self.settings.rms_threshold
        self.quiet_ms = 0 if voiced else self.quiet_ms + FRAME_MS
        self.last_frame_at = self.clock()
        return voiced

    def paused(self):
        # 有些 WASAPI 设备在完全静音时不产生 callback；空读同样要能结束一句。
        missing_ms = max(0, (self.clock() - self.last_frame_at) * 1000)
        return self.quiet_ms + missing_ms >= self.settings.silence_ms


class _FrameReader:
    """提前收到服务端 final 时，把尚未使用的采集帧交还给下一段。"""

    def __init__(self, source):
        self.source = source
        self.pending = deque()

    def read(self, timeout=0.1):
        return self.pending.popleft() if self.pending else self.source.read(timeout=timeout)

    def putback(self, frame):
        if frame is not None:
            self.pending.appendleft(frame)


class _Segment:
    def __init__(self, source, seed, gate, stop_event):
        self.source, self.seed, self.gate, self.stop_event = source, deque(seed), gate, stop_event
        self.closed = Event()
        self.reason = None
        self.error = None

    def cancel(self):
        self.closed.set()

    def putback_unsent(self, frame):
        if not self.stop_event.is_set():
            self.seed.appendleft(frame)

    def recover(self):
        # transcribe 已等待 sender 退出，这时才把未发送的句首/末帧还给下一段。
        while self.seed and not self.stop_event.is_set():
            self.source.putback(self.seed.pop())

    def __iter__(self):
        duration_ms = 0
        try:
            while self.seed:
                if self.stop_event.is_set() or self.closed.is_set():
                    return
                frame = self.seed.popleft()
                duration_ms += FRAME_MS
                yield frame
            while not self.stop_event.is_set() and not self.closed.is_set():
                frame = self.source.read(timeout=0.1)
                if self.stop_event.is_set() or self.closed.is_set():
                    if not self.stop_event.is_set():
                        self.source.putback(frame)
                    return
                if frame is None:
                    if self.gate.paused():
                        self.reason = "pause"
                        return
                    continue
                self.gate.observe(frame)
                duration_ms += FRAME_MS
                if self.gate.quiet_ms >= self.gate.settings.silence_ms:
                    self.reason = "pause"
                elif duration_ms >= self.gate.settings.max_segment_ms:
                    self.reason = "rollover"
                yield frame
                if self.reason is not None:
                    return
        except Exception as exc:
            self.error = exc
            raise


class SpeechSession:
    """公开方法在 UI 线程调用；录音、联网、转写都在后台运行。"""

    def __init__(self, *, source_factory=SystemAudioSource, recognizer_factory=XfyunRtasrRecognizer,
                 config_factory=XfyunRtasrConfig.from_env, settings_factory=SpeechSettings.from_env,
                 clock=time.monotonic):
        self._source_factory = source_factory
        self._recognizer_factory = recognizer_factory
        self._config_factory = config_factory
        self._settings_factory = settings_factory
        self._clock = clock
        self._lock = Lock()
        self._events = deque()
        self._generation = 0
        self._stop_event = Event()
        self._source = None
        self._running = False
        self._closed = False

    @property
    def running(self):
        return self._running

    def start(self, device_index=None):
        if self._closed:
            raise ValueError("语音会话已关闭。")
        if self._running:
            return
        # 缺少凭据时，在真正打开采集设备之前明确报错。
        settings = self._settings_factory()
        config = self._config_factory()
        # 兼容注入的旧 IAT 配置；RTASR 标准版由本地停顿切句，无 eos 参数。
        if isinstance(config, XfyunConfig):
            config = replace(config, eos_ms=settings.silence_ms + 2000)
        self._generation += 1
        generation = self._generation
        stop_event = Event()
        self._stop_event = stop_event
        self._running = True
        worker = Thread(target=self._run, args=(generation, stop_event, device_index, settings, config),
                        name="speech-session", daemon=True)
        try:
            worker.start()
        except Exception:
            self._running = False
            raise

    def _emit(self, generation, kind, utterance_id="", text="", endpoint=True):
        event = SpeechEvent(generation, kind, utterance_id, text, endpoint)
        with self._lock:
            if self._closed or generation != self._generation:
                return
            # UI 暂忙时只保留同一句最新的临时全文；final 和错误不能丢失。
            if kind == "partial" and self._events and self._events[-1].kind == "partial":
                if self._events[-1].utterance_id == utterance_id:
                    self._events[-1] = event
                    return
            self._events.append(event)

    def _run(self, generation, stop_event, device_index, settings, config):
        source = None
        segment = None
        try:
            source = self._source_factory(device_index)
            with self._lock:
                if generation != self._generation or stop_event.is_set():
                    return
                self._source = source
            source.start()
            if stop_event.is_set():
                return
            self._emit(generation, "started", text=source.device_name)
            recognizer = self._recognizer_factory(config)
            reader = _FrameReader(source)
            gate = _Gate(settings, self._clock)
            preroll = deque(maxlen=6)  # 240 ms，保留句首弱音。
            question_open = False
            sequence = 0
            while not stop_event.is_set():
                frame = reader.read(timeout=0.1)
                if stop_event.is_set():
                    break
                voiced = gate.observe(frame) if frame is not None else False
                if frame is not None:
                    preroll.append(frame)
                if question_open and gate.paused():
                    sequence += 1
                    self._emit(generation, "final", f"{generation}:{sequence}", endpoint=True)
                    question_open = False
                    preroll.clear()
                if not voiced:
                    continue
                sequence += 1
                utterance_id = f"{generation}:{sequence}"
                question_open = True
                segment = _Segment(reader, list(preroll), gate, stop_event)
                preroll.clear()
                try:
                    text = recognizer.transcribe(
                        segment,
                        lambda text, uid=utterance_id: self._emit(generation, "partial", uid, text),
                        stop_event,
                    )
                finally:
                    segment.closed.set()
                    segment.recover()
                if stop_event.is_set():
                    break
                endpoint = segment.reason == "pause"
                self._emit(generation, "final", utterance_id, text, endpoint)
                question_open = not endpoint
                segment = None
        except XfyunCancelled:
            if not stop_event.is_set():
                self._emit(generation, "error", text="语音识别已取消，请重新开始监听。")
        except Exception as exc:
            error = segment.error if segment is not None and segment.error is not None else exc
            if not stop_event.is_set():
                message = str(error) if isinstance(error, (SystemAudioError, XfyunError, ValueError)) else (
                    "语音处理失败，请检查系统输出设备、网络和讯飞配置后重新开始监听。")
                self._emit(generation, "error", text=message)
        finally:
            if segment is not None:
                segment.closed.set()
            if source is not None:
                source.close()
            with self._lock:
                if generation == self._generation:
                    self._source = None
            self._emit(generation, "stopped")

    def poll(self):
        with self._lock:
            events = [event for event in self._events if event.generation == self._generation]
            self._events.clear()
        if any(event.kind == "stopped" for event in events):
            self._running = False
        return events

    def stop(self):
        self._generation += 1
        self._stop_event.set()
        self._running = False
        with self._lock:
            source, self._source = self._source, None
            self._events.clear()
        if source is not None:
            source.close()

    def close(self):
        self.stop()
        self._closed = True
