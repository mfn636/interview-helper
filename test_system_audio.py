"""纯离线音频格式、设备选择和生命周期验证，不打开真实录音设备。"""

from array import array
from queue import Queue
from threading import Event
import unittest
from unittest.mock import Mock, patch

from system_audio import (
    FRAME_BYTES, SystemAudioError, SystemAudioSource, _PCM16Converter,
    _select_device, list_output_devices,
)


OUTPUT = {"index": 1, "name": "Headphones", "maxInputChannels": 0, "defaultSampleRate": 48000}
LOOPBACK = {"index": 4, "name": "Headphones [Loopback]", "maxInputChannels": 2, "defaultSampleRate": 48000, "isLoopbackDevice": True}
MICROPHONE = {"index": 2, "name": "Microphone", "maxInputChannels": 1, "defaultSampleRate": 48000}


def backend_fixture():
    backend = Mock(paWASAPI=13, paInt16=8, paContinue=0, paAbort=2)
    audio = backend.PyAudio.return_value
    audio.get_host_api_info_by_type.return_value = {"defaultOutputDevice": 1}
    audio.get_device_info_by_index.side_effect = lambda index: {1: OUTPUT, 2: MICROPHONE, 4: LOOPBACK}[index]
    audio.get_loopback_device_info_generator.side_effect = lambda: iter([LOOPBACK])
    audio.open.return_value.is_active.return_value = True
    return backend, audio


class PCMConverterTests(unittest.TestCase):
    def test_stereo_downmix_and_six_channel_downmix(self):
        stereo = array("h", [1000, 3000, -1000, -3000]).tobytes()
        self.assertEqual(array("h", [2000, -2000]).tobytes(), _PCM16Converter(16000, 2).feed(stereo))
        surround = array("h", [600, 1200, 1800, 2400, 3000, 3600]).tobytes()
        self.assertEqual(array("h", [2100]).tobytes(), _PCM16Converter(16000, 6).feed(surround))

    def test_resampling_is_independent_of_chunk_boundaries(self):
        for rate in (44100, 48000):
            samples = array("h", ((i % 2000) - 1000 for i in range(rate * 2)))
            original = samples.tobytes()
            expected = _PCM16Converter(rate, 2).feed(original)
            converter = _PCM16Converter(rate, 2)
            actual = b"".join(converter.feed(original[start:start + 997]) for start in range(0, len(original), 997))
            self.assertEqual(actual, expected)
            self.assertEqual(len(actual), 16000 * 2)


class SystemAudioTests(unittest.TestCase):
    def start_source(self):
        backend, audio = backend_fixture()
        patcher = patch("system_audio._load_backend", return_value=backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        source = SystemAudioSource()
        self.addCleanup(source.close)
        source.start()
        return source, backend, audio

    def test_selects_default_output_loopback_and_rejects_microphone(self):
        backend, audio = backend_fixture()
        self.assertEqual(_select_device(audio, backend)["index"], 4)
        with self.assertRaisesRegex(SystemAudioError, "麦克风"):
            _select_device(audio, backend, 2)
        audio.get_loopback_device_info_generator.side_effect = lambda: iter([])
        with self.assertRaisesRegex(SystemAudioError, "回环设备"):
            _select_device(audio, backend)

    def test_list_devices_terminates_portaudio_without_opening_stream(self):
        backend, audio = backend_fixture()
        with patch("system_audio._load_backend", return_value=backend):
            devices = list_output_devices()
        self.assertEqual(devices[0]["id"], 4)
        self.assertTrue(devices[0]["is_default"])
        audio.open.assert_not_called()
        audio.terminate.assert_called_once()

    def test_callback_produces_fixed_frames_and_read_can_timeout(self):
        source, backend, audio = self.start_source()
        callback = audio.open.call_args.kwargs["stream_callback"]
        self.assertEqual(audio.open.call_args.kwargs["input_device_index"], 4)
        self.assertEqual(source.device_name, LOOPBACK["name"])
        data = array("h", [1000, 3000] * 3840).tobytes()
        self.assertEqual(callback(data, 3840, {}, 0)[1], backend.paContinue)
        self.assertEqual(source.read(0), array("h", [2000] * 640).tobytes())
        self.assertEqual(len(source.read(0)), FRAME_BYTES)
        self.assertIsNone(source.read(0))

    def test_overflow_is_reported_and_capture_aborted(self):
        source, backend, audio = self.start_source()
        source._frames = Queue(maxsize=1)
        callback = audio.open.call_args.kwargs["stream_callback"]
        self.assertEqual(callback(array("h", [1, 1] * 3840).tobytes(), 3840, {}, 0)[1], backend.paAbort)
        with self.assertRaisesRegex(SystemAudioError, "缓存"):
            source.read(0)

    def test_driver_overflow_and_lost_device_are_reported(self):
        source, backend, audio = self.start_source()
        callback = audio.open.call_args.kwargs["stream_callback"]
        self.assertEqual(callback(b"", 0, {}, 2)[1], backend.paAbort)
        with self.assertRaisesRegex(SystemAudioError, "丢帧"):
            source.read(0)
        source._error = None
        audio.open.return_value.is_active.return_value = False
        with self.assertRaisesRegex(SystemAudioError, "中断"):
            source.read(0)

    def test_open_failure_releases_portaudio(self):
        backend, audio = backend_fixture()
        audio.open.side_effect = OSError("device unavailable")
        with patch("system_audio._load_backend", return_value=backend):
            with self.assertRaisesRegex(SystemAudioError, "无法启动"):
                SystemAudioSource().start()
        audio.terminate.assert_called_once()

    def test_close_is_idempotent_and_does_not_wait_for_stuck_driver(self):
        source, backend, audio = self.start_source()
        release = Event()
        entered = Event()
        stream = audio.open.return_value

        def blocked_stop():
            entered.set()
            release.wait(2)

        stream.stop_stream.side_effect = blocked_stop
        try:
            source.close()
            self.assertTrue(entered.wait(1))
            self.assertTrue(source._cleanup_thread.is_alive())
            self.assertIsNone(source.read(0))
            source.close()
        finally:
            release.set()
            source._cleanup_thread.join(2)
        stream.close.assert_called_once()
        audio.terminate.assert_called_once()
        with self.assertRaisesRegex(SystemAudioError, "已关闭"):
            source.start()


if __name__ == "__main__":
    unittest.main()
