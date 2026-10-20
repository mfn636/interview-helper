"""Windows WASAPI 扬声器回环采集，不会回退到麦克风。

PyAudioWPatch 延迟导入；PCM 重采样使用 Python 3.11 / 3.12 的 audioop。
输出固定为 16 kHz、单声道、有符号 PCM16LE，每帧 40 ms。
"""

from array import array
import importlib
from queue import Empty, Full, Queue
import sys
from threading import Event, Lock, Thread


SAMPLE_RATE = 16000
FRAME_MS = 40
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2
MAX_BUFFER_SECONDS = 15


class SystemAudioError(RuntimeError):
    """可直接向用户显示的系统声音采集错误。"""


def _load_backend():
    if sys.platform != "win32":
        raise SystemAudioError("系统声音采集需要 Windows 和 WASAPI 回环设备。")
    try:
        return importlib.import_module("pyaudiowpatch")
    except ImportError as exc:
        raise SystemAudioError(
            "缺少系统声音采集依赖，请运行 python -m pip install -r requirements.txt。"
        ) from exc


def _wasapi(audio, backend):
    try:
        return audio.get_host_api_info_by_type(backend.paWASAPI)
    except (OSError, ValueError) as exc:
        raise SystemAudioError("当前系统没有可用的 WASAPI 音频服务。") from exc


def _loopback_devices(audio):
    return [
        dict(device)
        for device in audio.get_loopback_device_info_generator()
        if device.get("isLoopbackDevice") and int(device.get("maxInputChannels", 0)) > 0
    ]


def _select_device(audio, backend, device_index=None):
    host = _wasapi(audio, backend)
    if device_index is not None:
        try:
            device = dict(audio.get_device_info_by_index(device_index))
        except (OSError, ValueError) as exc:
            raise SystemAudioError(f"找不到系统输出回环设备 {device_index}，请重新列出设备。") from exc
        if not device.get("isLoopbackDevice") or int(device.get("maxInputChannels", 0)) <= 0:
            raise SystemAudioError("所选设备不是系统声音回环设备；不能使用麦克风代替。")
        return device
    output_index = int(host.get("defaultOutputDevice", -1))
    if output_index < 0:
        raise SystemAudioError("Windows 没有默认声音输出，请先连接扬声器或耳机。")
    try:
        output = dict(audio.get_device_info_by_index(output_index))
    except (OSError, ValueError) as exc:
        raise SystemAudioError("无法访问默认声音输出，请检查耳机或扬声器连接。") from exc
    if output.get("isLoopbackDevice") and int(output.get("maxInputChannels", 0)) > 0:
        return output
    output_name = str(output.get("name", "")).strip()
    candidates = _loopback_devices(audio)
    exact = [d for d in candidates if str(d["name"]).replace("[Loopback]", "").strip() == output_name]
    matches = exact or [d for d in candidates if output_name and output_name in str(d["name"])]
    if len(matches) != 1:
        raise SystemAudioError(
            "找不到默认输出对应的唯一 WASAPI 回环设备，请用 --list-audio-devices 选择设备。"
        )
    return matches[0]


def list_output_devices():
    """列出可录制的系统输出回环设备，id/index 均为 PortAudio 设备编号。"""
    backend = _load_backend()
    audio = None
    try:
        audio = backend.PyAudio()
        _wasapi(audio, backend)
        devices = _loopback_devices(audio)
        try:
            default_index = int(_select_device(audio, backend)["index"])
        except SystemAudioError:
            default_index = None
        return [
            {
                "id": int(device["index"]),
                "index": int(device["index"]),
                "name": str(device["name"]),
                "sample_rate": int(device["defaultSampleRate"]),
                "channels": int(device["maxInputChannels"]),
                "is_default": int(device["index"]) == default_index,
            }
            for device in devices
        ]
    except SystemAudioError:
        raise
    except Exception as exc:
        raise SystemAudioError("无法枚举系统输出，请检查 Windows 音频服务和设备连接。") from exc
    finally:
        if audio is not None:
            audio.terminate()


class _PCM16Converter:
    """连续转换，保留不足一个输入采样帧的数据和重采样相位。"""

    def __init__(self, sample_rate, channels):
        if sample_rate <= 0 or channels <= 0:
            raise ValueError("音频采样率和声道数必须大于零。")
        try:
            self._audioop = importlib.import_module("audioop")
        except ImportError as exc:
            raise SystemAudioError("当前音频模块需要 Python 3.11 或 3.12，请使用兼容版本。") from exc
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self._state = None
        self._pending = bytearray()

    def feed(self, data):
        self._pending.extend(data)
        sample_frame_bytes = self.channels * 2
        usable = len(self._pending) // sample_frame_bytes * sample_frame_bytes
        if not usable:
            return b""
        data = bytes(self._pending[:usable])
        del self._pending[:usable]
        if self.channels == 2:
            data = self._audioop.tomono(data, 2, 0.5, 0.5)
        elif self.channels > 2:
            samples = array("h")
            samples.frombytes(data)
            if sys.byteorder != "little":
                samples.byteswap()
            mixed = array("h", (
                sum(samples[start:start + self.channels]) // self.channels
                for start in range(0, len(samples), self.channels)
            ))
            if sys.byteorder != "little":
                mixed.byteswap()
            data = mixed.tobytes()
        if self.sample_rate != SAMPLE_RATE:
            data, self._state = self._audioop.ratecv(
                data, 2, 1, self.sample_rate, SAMPLE_RATE, self._state
            )
        return data


class SystemAudioSource:
    """后台捕获默认输出，read 消费固定 PCM 帧；最多缓存约 15 秒。"""

    def __init__(self, device_index=None):
        if device_index is not None and (isinstance(device_index, bool) or not isinstance(device_index, int) or device_index < 0):
            raise ValueError("系统声音设备编号必须是非负整数。")
        self.device_index = device_index
        self.device_name = ""
        self._frames = Queue(maxsize=MAX_BUFFER_SECONDS * 1000 // FRAME_MS)
        self._closed = Event()
        self._resource_lock = Lock()
        self._state_lock = Lock()
        self._audio = None
        self._stream = None
        self._backend = None
        self._converter = None
        self._pending = bytearray()
        self._error = None
        self._started = False
        self._cleanup_thread = None

    def start(self):
        """在调用者的后台线程中打开音频设备；采集由 PortAudio callback 完成。"""
        with self._resource_lock:
            if self._closed.is_set():
                raise SystemAudioError("系统声音采集已关闭，请重新创建采集会话。")
            if self._started:
                return
            try:
                self._backend = _load_backend()
                self._audio = self._backend.PyAudio()
                device = _select_device(self._audio, self._backend, self.device_index)
                self.device_name = str(device["name"])
                rate = int(device["defaultSampleRate"])
                channels = int(device["maxInputChannels"])
                self._converter = _PCM16Converter(rate, channels)
                self._stream = self._audio.open(
                    format=self._backend.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=int(device["index"]),
                    frames_per_buffer=max(1, rate * FRAME_MS // 1000),
                    stream_callback=self._callback,
                    start=False,
                )
                if self._closed.is_set():
                    raise SystemAudioError("系统声音采集已关闭。")
                self._stream.start_stream()
                self._started = True
            except Exception as exc:
                self._release_resources()
                if isinstance(exc, SystemAudioError):
                    raise
                raise SystemAudioError(
                    "无法启动系统声音采集，请检查输出设备是否被拔出或被其他程序独占。"
                ) from exc

    def _fail(self, message):
        with self._state_lock:
            if self._error is None:
                self._error = SystemAudioError(message)

    def _callback(self, in_data, frame_count, time_info, status):
        if self._closed.is_set():
            return None, self._backend.paAbort
        if status:
            self._fail(f"系统声音采集发生丢帧或中断（PortAudio 状态 {status}），请重新开始监听。")
            return None, self._backend.paAbort
        try:
            if in_data:
                self._pending.extend(self._converter.feed(in_data))
            while len(self._pending) >= FRAME_BYTES:
                self._frames.put_nowait(bytes(self._pending[:FRAME_BYTES]))
                del self._pending[:FRAME_BYTES]
        except Full:
            self._fail("系统声音缓存已超过 15 秒，处理速度不足；已停止采集，请重新开始监听。")
            return None, self._backend.paAbort
        except Exception:
            self._fail("系统声音格式转换失败，已停止采集，请重新开始监听。")
            return None, self._backend.paAbort
        return None, self._backend.paContinue

    def read(self, timeout=0.2):
        if self._closed.is_set():
            return None
        if self._error is not None:
            raise self._error
        if not self._started:
            raise SystemAudioError("请先启动系统声音采集。")
        try:
            frame = self._frames.get(timeout=max(0.0, timeout))
        except Empty:
            frame = None
        if self._closed.is_set():
            return None
        if self._error is not None:
            raise self._error
        stream = self._stream
        try:
            active = stream is not None and stream.is_active()
        except Exception as exc:
            if self._closed.is_set():
                return None
            raise SystemAudioError("系统输出设备已断开，请重新选择输出设备并开始监听。") from exc
        if not active:
            raise SystemAudioError("系统声音采集已中断，请检查输出设备并重新开始监听。")
        return frame

    def _release_resources(self):
        stream, self._stream = self._stream, None
        audio, self._audio = self._audio, None
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if audio is not None:
            try:
                audio.terminate()
            except Exception:
                pass

    def close(self):
        """立即停止接收；驱动资源在 daemon 线程释放，UI 最多等待 50 ms。"""
        with self._state_lock:
            if self._closed.is_set():
                return
            self._closed.set()

        def cleanup():
            with self._resource_lock:
                self._release_resources()

        self._cleanup_thread = Thread(target=cleanup, name="system-audio-close", daemon=True)
        self._cleanup_thread.start()
        self._cleanup_thread.join(timeout=0.05)
