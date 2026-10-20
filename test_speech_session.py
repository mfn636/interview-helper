"""假音源与假识别器验证完整语音事件流，不打开设备或连接网络。"""

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Lock, Thread
import time
import unittest
from unittest.mock import Mock, patch

from speech_session import SpeechSession, SpeechSettings, _FrameReader, _Gate, _Segment
from system_audio import FRAME_BYTES, SystemAudioError
from voice_controller import VoiceController
from xfyun_asr import XfyunCancelled
from xfyun_rtasr import XfyunRtasrConfig


SILENT = bytes(FRAME_BYTES)
VOICED = (1200).to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)


class ManualClock:
    def __init__(self):
        self._value = 0.0
        self._lock = Lock()

    def __call__(self):
        with self._lock:
            return self._value

    def advance(self, seconds):
        with self._lock:
            self._value += seconds


@dataclass(frozen=True)
class SilenceWithoutCallback:
    seconds: float


class FakeSource:
    device_name = "离线测试扬声器"

    def __init__(self, clock):
        self.clock = clock
        self.frames = Queue()
        self.started = Event()
        self.closed = Event()
        self.close_calls = 0

    def start(self):
        self.started.set()

    def feed(self, *frames):
        for frame in frames:
            self.frames.put(frame)

    def checkpoint(self):
        barrier = Event()
        self.frames.put(barrier)
        return barrier

    def read(self, timeout=0.1):
        if self.closed.is_set():
            return None
        try:
            frame = self.frames.get(timeout=min(timeout, 0.02))
        except Empty:
            return None
        if isinstance(frame, Event):
            frame.set()
            return None
        if isinstance(frame, Exception):
            raise frame
        if isinstance(frame, SilenceWithoutCallback):
            self.clock.advance(frame.seconds)
            return None
        self.clock.advance(0.04)
        return frame

    def close(self):
        self.close_calls += 1
        self.closed.set()


class FakeRecognizer:
    def __init__(self, answers=None):
        self.answers = list(answers or ["最终识别的问题"])
        self.calls = []
        self.partial_sent = Event()

    def transcribe(self, frames, on_partial, stop_event):
        consumed = []
        self.calls.append(consumed)
        for frame in frames:
            consumed.append(frame)
            on_partial("尚未稳定的文字")
            self.partial_sent.set()
        if stop_event.is_set():
            raise XfyunCancelled("测试取消")
        index = len(self.calls) - 1
        return self.answers[min(index, len(self.answers) - 1)]


class LateRecognizer:
    """故意在取消后继续回调，验证 SpeechSession 自己的隔离边界。"""

    def __init__(self):
        self.started = Event()
        self.release = Event()

    def transcribe(self, frames, on_partial, stop_event):
        next(iter(frames))
        on_partial("旧会话临时文字")
        self.started.set()
        if not self.release.wait(3):
            raise TimeoutError("测试未释放旧识别请求")
        on_partial("迟到的旧临时文字")
        return "迟到的旧最终文字"


class SpeechSessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.workers = []
        self.sessions = []
        self.releases = []
        worker_patch = patch("speech_session.Thread", side_effect=self.make_thread)
        worker_patch.start()
        self.addCleanup(worker_patch.stop)
        self.addCleanup(self.cleanup_workers)

    def make_thread(self, *args, **kwargs):
        worker = Thread(*args, **kwargs)
        self.workers.append(worker)
        return worker

    def cleanup_workers(self):
        for session in self.sessions:
            session.close()
        for release in self.releases:
            release.set()
        for worker in self.workers:
            worker.join(3)
            self.assertFalse(worker.is_alive(), "语音后台线程没有退出")

    def make_session(self, *, sources=None, recognizers=None, config_factory=None):
        sources = sources or [FakeSource(self.clock)]
        recognizers = recognizers or [FakeRecognizer()]
        source_factory = Mock(side_effect=sources)
        recognizer_factory = Mock(side_effect=recognizers)
        config_factory = config_factory or Mock(return_value=XfyunRtasrConfig("test-app", "test-key"))
        session = SpeechSession(
            source_factory=source_factory,
            recognizer_factory=recognizer_factory,
            config_factory=config_factory,
            settings_factory=lambda: SpeechSettings(silence_ms=400, rms_threshold=180, max_segment_ms=1000),
            clock=self.clock,
        )
        self.sessions.append(session)
        return session, sources, recognizers, source_factory, recognizer_factory

    def wait_events(self, session, predicate, timeout=3):
        events = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events.extend(session.poll())
            if predicate(events):
                return events
            time.sleep(0.005)
        self.fail(f"未收到预期事件，实际为 {events!r}")

    def start(self, session, source):
        session.start()
        self.assertTrue(source.started.wait(3))
        events = self.wait_events(session, lambda items: any(e.kind == "started" for e in items))
        self.assertEqual(next(e.text for e in events if e.kind == "started"), source.device_name)

    def test_silence_does_not_open_an_asr_request(self):
        session, sources, recognizers, _, _ = self.make_session()
        self.start(session, sources[0])
        sources[0].feed(*([SILENT] * 30))
        self.assertTrue(sources[0].checkpoint().wait(3))
        self.assertEqual(recognizers[0].calls, [])
        self.assertEqual(session.poll(), [])
        self.assertTrue(session.running)

    def test_partial_is_preview_only_and_pause_emits_the_final_text(self):
        session, sources, recognizers, _, _ = self.make_session()
        self.start(session, sources[0])
        sources[0].feed(VOICED)
        self.assertTrue(recognizers[0].partial_sent.wait(3))
        events = self.wait_events(session, lambda items: any(e.kind == "partial" for e in items))
        self.assertFalse(any(e.kind == "final" for e in events))
        sources[0].feed(*([SILENT] * 10))
        events = self.wait_events(session, lambda items: any(e.kind == "final" for e in items))
        final = next(e for e in events if e.kind == "final")
        self.assertEqual(final.text, "最终识别的问题")
        self.assertTrue(final.endpoint)
        self.assertNotEqual(final.text, "尚未稳定的文字")
        self.assertEqual(len(recognizers[0].calls), 1)

    def test_pause_without_audio_callbacks_also_finalizes_the_question(self):
        session, sources, recognizers, _, _ = self.make_session()
        self.start(session, sources[0])
        sources[0].feed(VOICED)
        self.assertTrue(recognizers[0].partial_sent.wait(3))
        sources[0].feed(SilenceWithoutCallback(0.5))
        events = self.wait_events(session, lambda items: any(e.kind == "final" for e in items))
        final = next(e for e in events if e.kind == "final")
        self.assertTrue(final.endpoint)
        self.assertEqual(final.text, "最终识别的问题")

    def test_long_speech_rollover_does_not_dispatch_until_the_pause(self):
        recognizer = FakeRecognizer(["前半段", "后半段"])
        session, sources, _, _, _ = self.make_session(recognizers=[recognizer])
        self.start(session, sources[0])
        chat = Mock(ready=True, busy=False)
        chat.submit.return_value = 1
        voice = VoiceController(chat)

        sources[0].feed(*([VOICED] * 25))
        events = self.wait_events(session, lambda items: any(e.kind == "final" for e in items))
        first = next(e for e in events if e.kind == "final")
        self.assertFalse(first.endpoint)
        voice.final(first.utterance_id, first.text, first.endpoint)
        self.assertEqual(voice.preview, "前半段")
        self.assertEqual(voice.pending_count, 0)
        self.assertIsNone(voice.dispatch())
        chat.submit.assert_not_called()

        sources[0].feed(*([VOICED] * 5), *([SILENT] * 10))
        events = self.wait_events(session, lambda items: any(e.kind == "final" and e.endpoint for e in items))
        for event in events:
            if event.kind == "final":
                voice.final(event.utterance_id, event.text, event.endpoint)
        self.assertEqual(len(recognizer.calls), 2)
        self.assertEqual(voice.pending_count, 1)
        self.assertEqual(voice.dispatch(), "前半段后半段")
        chat.submit.assert_called_once_with("前半段后半段")

    def test_early_final_then_empty_pause_endpoint_queues_exactly_once(self):
        class EarlyFinalRecognizer:
            def __init__(self):
                self.calls = 0

            def transcribe(self, frames, on_partial, stop_event):
                self.calls += 1
                next(iter(frames))
                on_partial("临时文字")
                return "提前返回的稳定问题"

        recognizer = EarlyFinalRecognizer()
        session, sources, _, _, _ = self.make_session(recognizers=[recognizer])
        self.start(session, sources[0])
        chat = Mock(ready=True, busy=False)
        chat.submit.return_value = 1
        voice = VoiceController(chat)

        sources[0].feed(VOICED)
        first_events = self.wait_events(session, lambda items: any(e.kind == "final" for e in items))
        first = next(e for e in first_events if e.kind == "final")
        self.assertFalse(first.endpoint)
        self.assertEqual(first.text, "提前返回的稳定问题")
        voice.final(first.utterance_id, first.text, first.endpoint)
        self.assertEqual(voice.pending_count, 0)
        self.assertIsNone(voice.dispatch())

        sources[0].feed(*([SILENT] * 10))
        end_events = self.wait_events(session, lambda items: any(e.kind == "final" and e.endpoint for e in items))
        endpoint = next(e for e in end_events if e.kind == "final" and e.endpoint)
        self.assertEqual(endpoint.text, "")
        self.assertNotEqual(endpoint.utterance_id, first.utterance_id)
        voice.final(endpoint.utterance_id, endpoint.text, endpoint.endpoint)
        self.assertEqual(voice.pending_count, 1)
        self.assertEqual(voice.dispatch(), "提前返回的稳定问题")
        self.assertIsNone(voice.dispatch())
        chat.submit.assert_called_once_with("提前返回的稳定问题")

        sources[0].feed(*([SILENT] * 10))
        self.assertTrue(sources[0].checkpoint().wait(3))
        self.assertFalse(any(e.kind == "final" for e in session.poll()))
        self.assertEqual(recognizer.calls, 1)

    def test_cancel_recovers_remaining_seed_in_order_without_replaying_sent_frame(self):
        first, second, third, fourth = [bytes([value, 0]) * (FRAME_BYTES // 2) for value in range(1, 5)]
        source = Mock(read=Mock(side_effect=[fourth, None]))
        reader = _FrameReader(source)
        gate = _Gate(SpeechSettings(silence_ms=400), self.clock)
        segment = _Segment(reader, [first, second, third], gate, Event())
        frames = iter(segment)
        self.assertEqual(next(frames), first)  # 第一帧已经发送。
        segment.cancel()
        self.assertEqual(list(frames), [])
        segment.recover()
        segment.recover()  # 重复清理也不能重复交还帧。
        self.assertEqual([reader.read() for _ in range(4)], [second, third, fourth, None])

    def test_cancel_during_send_throttle_recovers_yielded_but_unsent_frame(self):
        sent, unsent, following = [bytes([value, 0]) * (FRAME_BYTES // 2) for value in range(1, 4)]
        source = Mock(read=Mock(side_effect=[unsent, following, None]))
        reader = _FrameReader(source)
        gate = _Gate(SpeechSettings(silence_ms=400), self.clock)
        segment = _Segment(reader, [sent], gate, Event())
        frames = iter(segment)
        self.assertEqual(next(frames), sent)
        self.assertEqual(next(frames), unsent)  # sender 正在等待节流时长，尚未 send。
        segment.cancel()
        segment.putback_unsent(unsent)
        self.assertEqual(list(frames), [])
        segment.recover()
        self.assertEqual([reader.read() for _ in range(3)], [unsent, following, None])

    def test_cancel_during_source_read_recovers_inflight_frame_before_later_audio(self):
        sent, inflight, following = [bytes([value, 0]) * (FRAME_BYTES // 2) for value in range(1, 4)]
        reading = Event()
        release = Event()
        self.releases.append(release)
        remaining = iter([following, None])
        first_read = True

        def read(timeout=0.1):
            nonlocal first_read
            if first_read:
                first_read = False
                reading.set()
                if not release.wait(3):
                    raise TimeoutError("测试未释放音源 read")
                return inflight
            return next(remaining)

        reader = _FrameReader(Mock(read=read))
        gate = _Gate(SpeechSettings(silence_ms=400), self.clock)
        segment = _Segment(reader, [sent], gate, Event())
        frames = iter(segment)
        self.assertEqual(next(frames), sent)
        accidentally_yielded = []
        worker = Thread(target=lambda: accidentally_yielded.extend(frames), daemon=True)
        self.workers.append(worker)
        worker.start()
        self.assertTrue(reading.wait(3))
        segment.cancel()
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(accidentally_yielded, [])
        segment.recover()
        self.assertEqual([reader.read() for _ in range(3)], [inflight, following, None])

    def test_stop_cancels_partial_without_emitting_a_final(self):
        session, sources, recognizers, _, _ = self.make_session()
        self.start(session, sources[0])
        sources[0].feed(VOICED)
        self.assertTrue(recognizers[0].partial_sent.wait(3))
        self.wait_events(session, lambda items: any(e.kind == "partial" for e in items))
        session.stop()
        self.workers[-1].join(3)
        self.assertFalse(self.workers[-1].is_alive())
        self.assertTrue(sources[0].closed.is_set())
        self.assertFalse(session.running)
        self.assertEqual(session.poll(), [])

    def test_restart_filters_old_callbacks_and_old_stopped_event(self):
        old = LateRecognizer()
        self.releases.append(old.release)
        sources = [FakeSource(self.clock), FakeSource(self.clock)]
        current = FakeRecognizer(["新会话问题"])
        session, _, _, source_factory, _ = self.make_session(sources=sources, recognizers=[old, current])
        self.start(session, sources[0])
        sources[0].feed(VOICED)
        self.assertTrue(old.started.wait(3))
        session.stop()
        self.start(session, sources[1])
        old.release.set()
        self.workers[0].join(3)
        self.assertFalse(self.workers[0].is_alive())
        self.assertTrue(session.running)
        self.assertEqual(session.poll(), [])

        sources[1].feed(VOICED, *([SILENT] * 10))
        events = self.wait_events(session, lambda items: any(e.kind == "final" for e in items))
        self.assertFalse(any("旧" in e.text for e in events))
        self.assertEqual(next(e.text for e in events if e.kind == "final"), "新会话问题")
        self.assertEqual(source_factory.call_count, 2)
        self.assertTrue(session.running)

    def test_source_failure_during_speech_emits_error_and_never_promotes_partial(self):
        session, sources, recognizers, _, _ = self.make_session()
        self.start(session, sources[0])
        sources[0].feed(VOICED)
        self.assertTrue(recognizers[0].partial_sent.wait(3))
        sources[0].feed(SystemAudioError("模拟设备中断"))
        events = self.wait_events(session, lambda items: any(e.kind == "stopped" for e in items))
        self.assertEqual([e.text for e in events if e.kind == "error"], ["模拟设备中断"])
        self.assertFalse(any(e.kind == "final" for e in events))
        self.assertFalse(session.running)
        self.assertTrue(sources[0].closed.is_set())

    def test_missing_credentials_do_not_open_a_device_or_create_a_worker(self):
        config_factory = Mock(side_effect=ValueError("请配置 XFYUN_API_KEY"))
        session, _, _, source_factory, recognizer_factory = self.make_session(config_factory=config_factory)
        with self.assertRaisesRegex(ValueError, "XFYUN_API_KEY"):
            session.start()
        source_factory.assert_not_called()
        recognizer_factory.assert_not_called()
        self.assertEqual(self.workers, [])
        self.assertFalse(session.running)
        self.assertEqual(session.poll(), [])

    def test_close_rejects_restart_and_closes_the_source(self):
        session, sources, _, _, _ = self.make_session()
        self.start(session, sources[0])
        session.close()
        self.assertTrue(sources[0].closed.is_set())
        with self.assertRaisesRegex(ValueError, "已关闭"):
            session.start()
        self.assertEqual(session.poll(), [])


if __name__ == "__main__":
    unittest.main()
