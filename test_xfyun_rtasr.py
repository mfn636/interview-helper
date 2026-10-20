"""RTASR 标准版离线协议测试；所有凭据均为虚构值或官方文档公开样例。"""

import json
import os
import queue
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import websocket

from xfyun_asr import XfyunCancelled, XfyunError
from xfyun_rtasr import XfyunRtasrConfig, XfyunRtasrRecognizer


TEXT = websocket.ABNF.OPCODE_TEXT
CLOSE = websocket.ABNF.OPCODE_CLOSE
STARTED = (TEXT, json.dumps({"action": "started", "code": "0"}))
CLEAN_CLOSE = (CLOSE, (1000).to_bytes(2, "big"))
END = b'{"end": true}'


def sentence(seg_id, text, kind="0", *, begin=None):
    begin = seg_id * 1000 if begin is None else begin
    nested = {"seg_id": seg_id, "cn": {"st": {"type": kind, "bg": str(begin),
              "ed": "0" if kind == "1" else str(begin + 800),
              "rt": [{"ws": [{"cw": [{"w": text}]}]}]}}}
    return TEXT, json.dumps({"action": "result", "code": "0", "data": json.dumps(nested)})


class Socket:
    def __init__(self, on_send=None):
        self.incoming = queue.Queue()
        self.incoming.put(STARTED)
        self.sent = []
        self.sent_at = []
        self.closed = threading.Event()
        self.shutdown_called = threading.Event()
        self.on_send = on_send
        self.timeout = 0.01
        self.started = False
        self.aborted = False
        self.clock = lambda: 0.0

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv_data(self, control_frame=False):
        if not control_frame:
            raise AssertionError("需要读取 CLOSE 帧确认完整完成")
        try:
            value = self.incoming.get(timeout=self.timeout)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException() from None
        if isinstance(value, Exception):
            raise value
        if value == STARTED:
            self.started = True
        return value

    def send_binary(self, data):
        if not self.started:
            raise AssertionError("started 确认前不能上传")
        self.sent.append(data)
        self.sent_at.append(self.clock())
        if self.on_send:
            self.on_send(self, data)

    def abort(self):
        self.aborted = True

    def close(self, **kwargs):
        self.closed.set()

    def shutdown(self):
        self.shutdown_called.set()


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def wait(self, event, seconds):
        if not event.is_set():
            self.value += seconds
        return event.is_set()


class RtasrTests(unittest.TestCase):
    def setUp(self):
        self.config = XfyunRtasrConfig("fake-app", "fake-key", poll_timeout=0.01)
        self.frame = b"\x01\x00" * 640
        self.stop = threading.Event()

    def recognizer(self, socket, **kwargs):
        return XfyunRtasrRecognizer(self.config, create_connection=lambda *args, **kw: socket, **kwargs)

    @staticmethod
    def complete(socket, data):
        if data == END:
            socket.incoming.put(sentence(0, "介绍一下 Python。"))
            socket.incoming.put(CLEAN_CLOSE)

    def test_two_credentials_only_environment_precedence_no_real_env_read(self):
        with patch("xfyun_rtasr.dotenv_values", return_value={"XFYUN_APP_ID": "file-app", "XFYUN_API_KEY": "file-key"}):
            with patch.dict(os.environ, {"XFYUN_API_KEY": " env-key "}, clear=True):
                config = XfyunRtasrConfig.from_env("mock.env")
        self.assertEqual((config.app_id, config.api_key), ("file-app", "env-key"))
        self.assertNotIn("env-key", repr(config))
        self.assertNotIn("file-app", repr(config))
        self.assertFalse(hasattr(config, "api_secret"))

    def test_missing_credentials_asks_for_only_app_id_and_key(self):
        with patch("xfyun_rtasr.dotenv_values", return_value={}):
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "XFYUN_APP_ID.*XFYUN_API_KEY") as caught:
                    XfyunRtasrConfig.from_env("mock.env")
        self.assertNotIn("API_SECRET", str(caught.exception))

    def test_signature_matches_synthetic_fixture(self):
        # 固定的虚构输入与预先计算的签名，不包含任何服务凭据。
        config = XfyunRtasrConfig("test-app", "test-fixture-not-a-real-api-key")
        recognizer = XfyunRtasrRecognizer(config, utcnow=lambda: datetime.fromtimestamp(1512041814, timezone.utc))
        parsed = urlsplit(recognizer._signed_url())
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("wss", "rtasr.xfyun.cn", "/v1/ws"))
        self.assertEqual(parse_qs(parsed.query), {"appid": ["test-app"], "ts": ["1512041814"],
                         "signa": ["QZ5bhhvIj+8mZHwQCkXTnD5+5dI="]})

    def test_non_default_language_is_explicit_in_signed_url(self):
        recognizer = XfyunRtasrRecognizer(replace(self.config, lang="en"))
        self.assertEqual(parse_qs(urlsplit(recognizer._signed_url()).query)["lang"], ["en"])

    def test_binary_frames_end_marker_and_realtime_pacing(self):
        clock = Clock()
        socket = Socket(self.complete)
        socket.clock = clock.now
        partials = []
        final = self.recognizer(socket, monotonic=clock.now, wait=clock.wait).transcribe(
            [self.frame] * 3, partials.append, self.stop)
        self.assertEqual(socket.sent, [self.frame, self.frame, self.frame, END])
        self.assertEqual(socket.sent_at, [0, 0.04, 0.08, 0.12])
        self.assertEqual(final, "介绍一下 Python。")
        self.assertEqual(partials, [final])
        self.assertTrue(socket.closed.is_set())
        self.assertTrue(socket.shutdown_called.is_set())

    def test_result_order_duplicates_and_old_partials(self):
        def finish(socket, data):
            if data == END:
                for value in (sentence(0, "临时", "1", begin=0),
                              sentence(1, "第一句。", begin=0),
                              sentence(2, "第二句临时", "1", begin=1000),
                              sentence(3, "第二句。", begin=1000),
                              sentence(2, "旧临时", "1", begin=1000),
                              sentence(3, "第二句。", begin=1000),
                              sentence(4, "第三句。", begin=2000), CLEAN_CLOSE):
                    socket.incoming.put(value)

        partials = []
        final = self.recognizer(Socket(finish)).transcribe([self.frame], partials.append, self.stop)
        self.assertEqual(final, "第一句。第二句。第三句。")
        self.assertEqual(partials, ["临时", "第一句。", "第一句。第二句临时", "第一句。第二句。", final])

    def test_real_incrementing_result_ids_replace_partial_full_sentence(self):
        def finish(socket, data):
            if data == END:
                for seg_id, text in enumerate(("介绍", "介绍一下", "介绍一下 Python", "介绍一下 Python 的")):
                    socket.incoming.put(sentence(seg_id, text, "1", begin=820))
                socket.incoming.put(sentence(4, "介绍一下 Python 的 GIL。", begin=820))
                socket.incoming.put(CLEAN_CLOSE)

        partials = []
        final = self.recognizer(Socket(finish)).transcribe([self.frame], partials.append, self.stop)
        self.assertEqual(final, "介绍一下 Python 的 GIL。")
        self.assertEqual(partials, ["介绍", "介绍一下", "介绍一下 Python", "介绍一下 Python 的", final])

    def test_two_sentences_keep_first_final_and_replace_second_partial(self):
        def finish(socket, data):
            if data == END:
                for value in (sentence(0, "第一", "1", begin=0),
                              sentence(1, "第一句。", begin=0),
                              sentence(2, "第二", "1", begin=1200),
                              sentence(3, "第二句。", begin=1200),
                              sentence(3, "第二句。", begin=1200), CLEAN_CLOSE):
                    socket.incoming.put(value)

        partials = []
        final = self.recognizer(Socket(finish)).transcribe([self.frame], partials.append, self.stop)
        self.assertEqual(final, "第一句。第二句。")
        self.assertEqual(partials, ["第一", "第一句。", "第一句。第二", final])

    def test_old_result_cannot_replace_newer_text_of_same_sentence(self):
        def finish(socket, data):
            if data == END:
                for value in (sentence(2, "最新修订", "1", begin=0),
                              sentence(1, "旧文本", "1", begin=0),
                              sentence(3, "最终文字。", begin=0),
                              sentence(0, "更旧的最终文字。", begin=0), CLEAN_CLOSE):
                    socket.incoming.put(value)

        partials = []
        final = self.recognizer(Socket(finish)).transcribe([self.frame], partials.append, self.stop)
        self.assertEqual(final, "最终文字。")
        self.assertEqual(partials, ["最新修订", final])

    def test_drifting_begin_and_final_punctuation_match_live_response_pattern(self):
        def finish(socket, data):
            if data == END:
                for seg_id, begin, text in ((0, 0, "介绍"), (1, 200, "介绍一下"),
                                           (2, 240, "介绍一下 Python"), (3, 280, "介绍一下 Python 的线程锁")):
                    socket.incoming.put(sentence(seg_id, text, "1", begin=begin))
                socket.incoming.put(sentence(4, "介绍一下 Python 的线程锁", begin=0))
                socket.incoming.put(sentence(5, "。", begin=0))
                socket.incoming.put(sentence(5, "。", begin=0))
                socket.incoming.put(CLEAN_CLOSE)

        partials = []
        final = self.recognizer(Socket(finish)).transcribe([self.frame], partials.append, self.stop)
        self.assertEqual(final, "介绍一下 Python 的线程锁。")
        self.assertEqual(partials, ["介绍", "介绍一下", "介绍一下 Python", "介绍一下 Python 的线程锁", final])

    def test_sentence_timing_metadata_not_needed_for_result_assembly(self):
        def finish(socket, data):
            if data == END:
                nested = {"seg_id": 0, "cn": {"st": {"type": "0", "rt": [{"ws": [{"cw": [{"w": "完成"}]}]}]}}}
                socket.incoming.put((TEXT, json.dumps({"action": "result", "code": "0", "data": json.dumps(nested)})))
                socket.incoming.put(CLEAN_CLOSE)

        self.assertEqual(self.recognizer(Socket(finish)).transcribe([self.frame], lambda text: None, self.stop), "完成")

    def test_temporary_sentence_cannot_be_promoted_by_clean_close(self):
        def finish(socket, data):
            if data == END:
                socket.incoming.put(sentence(0, "还在修正", "1"))
                socket.incoming.put(CLEAN_CLOSE)

        with self.assertRaisesRegex(XfyunError, "未稳定"):
            self.recognizer(Socket(finish)).transcribe([self.frame], lambda text: None, self.stop)

    def test_empty_temporary_result_after_stable_text_still_blocks_final(self):
        def finish(socket, data):
            if data == END:
                socket.incoming.put(sentence(0, "已稳定。"))
                socket.incoming.put(sentence(1, "", "1"))
                socket.incoming.put(CLEAN_CLOSE)

        with self.assertRaisesRegex(XfyunError, "未稳定"):
            self.recognizer(Socket(finish)).transcribe([self.frame], lambda text: None, self.stop)

    def test_abrupt_disconnect_after_stable_result_is_failure(self):
        def finish(socket, data):
            if data == END:
                socket.incoming.put(sentence(0, "稳定句子"))
                socket.incoming.put(websocket.WebSocketConnectionClosedException("signa=SECRET fake-key"))

        with self.assertRaisesRegex(XfyunError, "异常中断") as caught:
            self.recognizer(Socket(finish)).transcribe([self.frame], lambda text: None, self.stop)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("fake-key", str(caught.exception))

    def test_abnormal_close_code_is_failure(self):
        def finish(socket, data):
            if data == END:
                socket.incoming.put((CLOSE, (1011).to_bytes(2, "big")))

        with self.assertRaisesRegex(XfyunError, "异常状态关闭"):
            self.recognizer(Socket(finish)).transcribe([self.frame], lambda text: None, self.stop)

    def test_no_status_close_frame_can_finish_stable_transcript(self):
        def finish(socket, data):
            if data == END:
                socket.incoming.put(sentence(0, "完成"))
                socket.incoming.put((CLOSE, b""))

        self.assertEqual(self.recognizer(Socket(finish)).transcribe([self.frame], lambda text: None, self.stop), "完成")

    def test_started_timeout_does_not_upload_audio(self):
        socket = Socket()
        socket.incoming.get_nowait()
        recognizer = XfyunRtasrRecognizer(replace(self.config, started_timeout=0.03),
                                         create_connection=lambda *args, **kw: socket)
        with self.assertRaisesRegex(XfyunError, "started.*超时"):
            recognizer.transcribe([self.frame], lambda text: None, self.stop)
        self.assertEqual(socket.sent, [])

    def test_service_error_does_not_echo_desc_or_signa(self):
        socket = Socket()
        socket.incoming.get_nowait()
        socket.incoming.put((TEXT, json.dumps({"action": "error", "code": "10110", "desc": "fake-key signa=SECRET"})))
        with self.assertRaisesRegex(XfyunError, "10110") as caught:
            self.recognizer(socket).transcribe([self.frame], lambda text: None, self.stop)
        self.assertNotIn("signa", str(caught.exception))
        self.assertNotIn("fake-key", str(caught.exception))

    def test_final_wait_is_bounded_even_after_stable_sentence(self):
        def finish(socket, data):
            if data == END:
                socket.incoming.put(sentence(0, "没有正常关闭"))

        socket = Socket(finish)
        recognizer = XfyunRtasrRecognizer(replace(self.config, final_timeout=0.03),
                                         create_connection=lambda *args, **kw: socket)
        with self.assertRaisesRegex(XfyunError, "正常关闭超时"):
            recognizer.transcribe([self.frame], lambda text: None, self.stop)

    def test_silence_without_results_for_18_seconds_during_upload_is_valid(self):
        clock = Clock()
        socket = Socket(self.complete)
        self.assertEqual(self.recognizer(socket, monotonic=clock.now, wait=clock.wait).transcribe(
            [self.frame] * 451, lambda text: None, self.stop), "介绍一下 Python。")
        self.assertGreaterEqual(clock.value, 18)

    def test_early_close_during_pacing_restores_only_unsent_frame(self):
        second = b"\x02\x00" * 640
        release = threading.Event()
        restored = []
        frame = self.frame

        class Frames:
            def __iter__(self):
                yield frame
                yield second

            def cancel(self):
                release.set()

            def putback_unsent(self, value):
                restored.append(value)

        socket = Socket()

        def wait(event, seconds):
            socket.incoming.put(CLEAN_CLOSE)
            release.wait(1)
            return event.is_set()

        with self.assertRaisesRegex(XfyunError, "上传完成前关闭"):
            self.recognizer(socket, wait=wait).transcribe(Frames(), lambda text: None, self.stop)
        self.assertEqual(restored, [second])
        self.assertEqual(socket.sent, [frame])

    def test_cancel_on_partial_never_returns_final(self):
        def finish(socket, data):
            if data == END:
                socket.incoming.put(sentence(0, "停止"))
                socket.incoming.put(CLEAN_CLOSE)

        socket = Socket(finish)
        with self.assertRaises(XfyunCancelled):
            self.recognizer(socket).transcribe([self.frame], lambda text: self.stop.set(), self.stop)
        self.assertTrue(socket.aborted)

    def test_cancelled_handshake_holds_shared_lease_until_late_socket_closed(self):
        connecting = threading.Event()
        release = threading.Event()
        second_opened = threading.Event()
        late = Socket()
        second = Socket(self.complete)
        errors = []
        second_results = []

        def first_factory(*args, **kwargs):
            connecting.set()
            release.wait(2)
            return late

        first = XfyunRtasrRecognizer(self.config, create_connection=first_factory)

        def second_factory(*args, **kwargs):
            self.assertTrue(late.closed.is_set())
            self.assertTrue(late.shutdown_called.is_set())
            second_opened.set()
            return second

        following = XfyunRtasrRecognizer(self.config, create_connection=second_factory)

        def first_work():
            try:
                first.transcribe([self.frame], lambda text: None, self.stop)
            except XfyunError as exc:
                errors.append(exc)

        def second_work():
            second_results.append(following.transcribe([self.frame], lambda text: None, threading.Event()))

        worker = threading.Thread(target=first_work)
        worker.start()
        waiter = threading.Thread(target=second_work)
        try:
            self.assertTrue(connecting.wait(0.5))
            self.stop.set()
            worker.join(0.5)
            self.assertIsInstance(errors[0], XfyunCancelled)
            waiter.start()
            self.assertFalse(second_opened.wait(0.1), "取消的握手未结束时不能建立第二路连接")
        finally:
            release.set()
            worker.join(1)
            if waiter.ident is not None:
                waiter.join(1)
        self.assertTrue(late.closed.is_set())
        self.assertTrue(second_opened.is_set())
        self.assertEqual(second_results, ["介绍一下 Python。"])

    def test_waiting_for_shared_lease_is_cancellable(self):
        XfyunRtasrRecognizer._provider_lease.acquire()
        self.stop.set()
        try:
            with patch("xfyun_rtasr.websocket.create_connection") as factory:
                with self.assertRaises(XfyunCancelled):
                    XfyunRtasrRecognizer(self.config).transcribe([self.frame], lambda text: None, self.stop)
                factory.assert_not_called()
        finally:
            XfyunRtasrRecognizer._provider_lease.release()

    def test_bad_pcm_frame_fails_and_closes_connection(self):
        socket = Socket()
        with self.assertRaisesRegex(XfyunError, "1280 字节"):
            self.recognizer(socket).transcribe([b"bad"], lambda text: None, self.stop)
        self.assertTrue(socket.closed.is_set())


if __name__ == "__main__":
    unittest.main()
