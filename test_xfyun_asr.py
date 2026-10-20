"""无需音频设备或真实 API 的讯飞鉴权、流协议与失败边界测试。"""

import base64
import hashlib
import hmac
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import websocket

from xfyun_asr import XfyunCancelled, XfyunConfig, XfyunError, XfyunRecognizer


def result_packet(sn, text, status=1, pgs="apd", rg=None):
    result = {"sn": sn, "pgs": pgs, "ws": [{"cw": [{"w": text}]}]}
    if rg is not None:
        result["rg"] = rg
    return json.dumps({"code": 0, "data": {"status": status, "result": result}})


class FakeSocket:
    def __init__(self, on_send=None, clock=time.monotonic):
        self.incoming = queue.Queue()
        self.sent = []
        self.sent_at = []
        self.on_send = on_send
        self.clock = clock
        self.timeout = 0.01
        self.closed = threading.Event()
        self.aborted = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, text):
        payload = json.loads(text)
        self.sent.append(payload)
        self.sent_at.append(self.clock())
        if self.on_send is not None:
            self.on_send(self, payload)

    def recv(self):
        try:
            item = self.incoming.get(timeout=self.timeout)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException() from None
        if isinstance(item, Exception):
            raise item
        return item

    def abort(self):
        self.aborted = True
        self.incoming.put("")

    def close(self, **kwargs):
        self.closed.set()


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.delays = []

    def now(self):
        return self.value

    def wait(self, event, delay):
        if not event.is_set():
            self.delays.append(delay)
            self.value += delay
        return event.is_set()


class XfyunTests(unittest.TestCase):
    def setUp(self):
        self.config = XfyunConfig("test-app", "private-api-key", "private-secret", poll_timeout=0.01)
        self.stop = threading.Event()
        self.frame = b"\x01\x00" * 640

    def recognizer(self, connection, **kwargs):
        return XfyunRecognizer(self.config, create_connection=lambda *args, **kw: connection, **kwargs)

    def test_env_file_and_environment_precedence_do_not_expose_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("XFYUN_APP_ID=file-app\nXFYUN_API_KEY=file-key\nXFYUN_API_SECRET=file-secret\n",
                            encoding="utf-8")
            with patch.dict(os.environ, {"XFYUN_API_KEY": "  environment-key  "}, clear=True):
                config = XfyunConfig.from_env(path)
            self.assertEqual((config.app_id, config.api_key, config.api_secret),
                             ("file-app", "environment-key", "file-secret"))
            self.assertNotIn("environment-key", repr(config))
            self.assertNotIn("file-secret", repr(config))

    def test_missing_credentials_identifies_fields_without_values(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "XFYUN_APP_ID.*XFYUN_API_KEY.*XFYUN_API_SECRET"):
                    XfyunConfig.from_env(Path(directory) / "missing.env")

    def test_signed_url_uses_utc_rfc1123_and_hmac_sha256(self):
        recognizer = XfyunRecognizer(self.config, utcnow=lambda: datetime(2026, 10, 18, 1, 2, 3,
                                                                         tzinfo=timezone.utc))
        url = urlsplit(recognizer._signed_url())
        params = parse_qs(url.query)
        self.assertEqual((url.scheme, url.netloc, url.path), ("wss", "iat-api.xfyun.cn", "/v2/iat"))
        self.assertEqual(params["date"], ["Sun, 18 Oct 2026 01:02:03 GMT"])
        self.assertEqual(params["host"], ["iat-api.xfyun.cn"])
        signed = ("host: iat-api.xfyun.cn\ndate: Sun, 18 Oct 2026 01:02:03 GMT\n"
                  "GET /v2/iat HTTP/1.1")
        expected = base64.b64encode(hmac.new(b"private-secret", signed.encode(), hashlib.sha256).digest()).decode()
        authorization = base64.b64decode(params["authorization"][0]).decode()
        self.assertEqual(authorization, 'api_key="private-api-key",algorithm="hmac-sha256",'
                         'headers="host date request-line",signature="' + expected + '"')

    def test_frame_status_business_and_realtime_pacing_with_prebuffer(self):
        clock = FakeClock()

        def on_send(connection, payload):
            if payload["data"]["status"] == 2:
                connection.incoming.put(result_packet(0, "请介绍 Python。", 2))

        connection = FakeSocket(on_send, clock.now)
        partials = []
        actual = self.recognizer(connection, monotonic=clock.now, wait=clock.wait).transcribe(
            [self.frame] * 3, partials.append, self.stop)
        self.assertEqual(actual, "请介绍 Python。")
        self.assertEqual(partials, [actual])
        self.assertEqual([item["data"]["status"] for item in connection.sent], [0, 1, 1, 2])
        first = connection.sent[0]
        self.assertEqual(first["common"], {"app_id": "test-app"})
        self.assertEqual(first["business"]["dwa"], "wpgs")
        self.assertEqual(first["business"]["eos"], 2000)
        self.assertNotIn("vad_eos", first["business"])
        for item in connection.sent[1:]:
            self.assertNotIn("common", item)
            self.assertNotIn("business", item)
        for item in connection.sent[:-1]:
            self.assertEqual(base64.b64decode(item["data"]["audio"]), self.frame)
            self.assertEqual(item["data"]["format"], "audio/L16;rate=16000")
            self.assertEqual(item["data"]["encoding"], "raw")
        self.assertEqual(base64.b64decode(connection.sent[-1]["data"]["audio"]), b"")
        self.assertEqual(connection.sent_at, [0, 0.04, 0.08, 0.12])
        self.assertTrue(connection.closed.is_set())

    def test_slow_source_does_not_catch_up_in_bursts(self):
        clock = FakeClock()

        def frames():
            yield self.frame
            clock.value += 0.4
            yield self.frame
            yield self.frame

        def on_send(connection, payload):
            if payload["data"]["status"] == 2:
                connection.incoming.put(result_packet(0, "完成", 2))

        connection = FakeSocket(on_send, clock.now)
        self.recognizer(connection, monotonic=clock.now, wait=clock.wait).transcribe(frames(), lambda text: None, self.stop)
        self.assertAlmostEqual(connection.sent_at[1], 0.4)
        self.assertGreaterEqual(connection.sent_at[2] - connection.sent_at[1], 0.039999)
        self.assertGreaterEqual(connection.sent_at[3] - connection.sent_at[2], 0.039999)

    def test_dynamic_replacements_are_inclusive_sequence_ranges(self):
        updates = [result_packet(0, "今天"), result_packet(1, "天气"), result_packet(2, "不错"),
                   result_packet(3, "很好", pgs="rpl", rg=[1, 2]), result_packet(4, "。")]

        def on_send(connection, payload):
            status = payload["data"]["status"]
            if status == 0:
                for update in updates:
                    connection.incoming.put(update)
            elif status == 2:
                connection.incoming.put(result_packet(5, "今天天气很好。", 2, "rpl", [0, 4]))

        partials = []
        connection = FakeSocket(on_send)
        final = self.recognizer(connection).transcribe([self.frame], partials.append, self.stop)
        self.assertEqual(partials, ["今天", "今天天气", "今天天气不错", "今天很好", "今天很好。", "今天天气很好。"])
        self.assertEqual(final, "今天天气很好。")

    def test_final_status_without_new_result_preserves_accumulated_text(self):
        def on_send(connection, payload):
            if payload["data"]["status"] == 0:
                connection.incoming.put(result_packet(0, "请解释闭包。"))
            else:
                connection.incoming.put(json.dumps({"code": 0, "data": {"status": 2}}))

        self.assertEqual(self.recognizer(FakeSocket(on_send)).transcribe(
            [self.frame], lambda text: None, self.stop), "请解释闭包。")

    def test_service_error_never_echoes_raw_message_or_credentials(self):
        connection = FakeSocket()
        connection.incoming.put(json.dumps({"code": 11200, "message": "private-api-key authorization=SECRET"}))
        with self.assertRaisesRegex(XfyunError, "11200") as caught:
            self.recognizer(connection).transcribe([self.frame], lambda text: None, self.stop)
        self.assertNotIn("private-api-key", str(caught.exception))
        self.assertNotIn("authorization", str(caught.exception))
        self.assertTrue(connection.closed.is_set())

    def test_connection_failure_hides_signed_url(self):
        def fail(url, **kwargs):
            raise OSError(url + " private-secret")

        with self.assertRaises(XfyunError) as caught:
            XfyunRecognizer(self.config, create_connection=fail).transcribe([], lambda text: None, self.stop)
        self.assertNotIn("authorization", str(caught.exception))
        self.assertNotIn("private-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_disconnect_after_partial_is_failure_not_success(self):
        connection = FakeSocket()
        connection.incoming.put(result_packet(0, "尚未完成"))
        connection.incoming.put("")
        partials = []
        with self.assertRaisesRegex(XfyunError, "最终识别结果"):
            self.recognizer(connection).transcribe([self.frame], partials.append, self.stop)
        self.assertEqual(partials, ["尚未完成"])

    def test_final_timeout_does_not_return_partial_as_final(self):
        def on_send(connection, payload):
            if payload["data"]["status"] == 0:
                connection.incoming.put(result_packet(0, "临时结果"))

        connection = FakeSocket(on_send)
        recognizer = XfyunRecognizer(replace(self.config, final_timeout=0.04),
                                      create_connection=lambda *args, **kwargs: connection)
        with self.assertRaisesRegex(XfyunError, "最终识别结果超时"):
            recognizer.transcribe([self.frame], lambda text: None, self.stop)

    def test_no_server_response_has_finite_timeout(self):
        connection = FakeSocket()
        recognizer = XfyunRecognizer(replace(self.config, response_timeout=0.03),
                                      create_connection=lambda *args, **kwargs: connection)
        with self.assertRaisesRegex(XfyunError, "长时间未返回"):
            recognizer.transcribe([self.frame], lambda text: None, self.stop)

    def test_cancel_before_connect_does_not_open_connection(self):
        self.stop.set()
        with patch("xfyun_asr.websocket.create_connection") as connect:
            with self.assertRaises(XfyunCancelled):
                XfyunRecognizer(self.config).transcribe([], lambda text: None, self.stop)
            connect.assert_not_called()

    def test_cancel_on_final_callback_does_not_submit_result(self):
        connection = FakeSocket()
        connection.incoming.put(result_packet(0, "停止后的结果", 2))
        with self.assertRaises(XfyunCancelled):
            self.recognizer(connection).transcribe([self.frame], lambda text: self.stop.set(), self.stop)
        self.assertTrue(connection.closed.is_set())
        self.assertTrue(connection.aborted)

    def test_cancelling_pending_connection_closes_late_socket(self):
        connecting = threading.Event()
        release = threading.Event()
        connection = FakeSocket()
        errors = []

        def factory(*args, **kwargs):
            connecting.set()
            release.wait(1)
            return connection

        recognizer = XfyunRecognizer(self.config, create_connection=factory)

        def work():
            try:
                recognizer.transcribe([self.frame], lambda text: None, self.stop)
            except XfyunError as exc:
                errors.append(exc)

        worker = threading.Thread(target=work)
        worker.start()
        try:
            self.assertTrue(connecting.wait(0.5))
            self.stop.set()
            worker.join(timeout=0.5)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(errors[0], XfyunCancelled)
        finally:
            release.set()
            worker.join(timeout=1)
        self.assertTrue(connection.closed.wait(0.5))

    def test_invalid_pcm_frame_fails_and_closes_connection(self):
        connection = FakeSocket()
        with self.assertRaisesRegex(XfyunError, "1280 字节"):
            self.recognizer(connection).transcribe([b"invalid"], lambda text: None, self.stop)
        self.assertTrue(connection.closed.is_set())

    def test_malformed_dynamic_range_fails(self):
        connection = FakeSocket()
        connection.incoming.put(result_packet(1, "错误", pgs="rpl", rg=[3, 1]))
        with self.assertRaisesRegex(XfyunError, "动态修正范围"):
            self.recognizer(connection).transcribe([self.frame], lambda text: None, self.stop)

    def test_early_server_final_cancels_frame_reader_before_return(self):
        waiting = threading.Event()
        cancelled = threading.Event()
        exited = threading.Event()
        frame = self.frame

        class Frames:
            def __iter__(self):
                yield frame
                waiting.set()
                cancelled.wait(1)
                exited.set()

            def cancel(self):
                cancelled.set()

        def on_send(connection, payload):
            if payload["data"]["status"] == 0:
                connection.incoming.put(result_packet(0, "服务提前结束", 2))

        connection = FakeSocket(on_send)
        original_recv = connection.recv

        def recv_after_reader_blocks():
            packet = original_recv()
            waiting.wait(0.5)
            return packet

        connection.recv = recv_after_reader_blocks
        final = self.recognizer(connection).transcribe(Frames(), lambda text: None, self.stop)
        self.assertEqual(final, "服务提前结束")
        self.assertTrue(waiting.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertTrue(exited.is_set(), "转写成功返回前必须结束此段 sender")
        self.assertFalse(self.stop.is_set(), "结束一段不能停止整场会话")
        self.assertEqual([item["data"]["status"] for item in connection.sent], [0])

    def test_early_final_with_unstoppable_sender_is_failure(self):
        waiting = threading.Event()
        release = threading.Event()
        exited = threading.Event()

        def frames():
            yield self.frame
            waiting.set()
            release.wait(2)
            exited.set()

        def on_send(connection, payload):
            if payload["data"]["status"] == 0:
                connection.incoming.put(result_packet(0, "不能直接提交", 2))

        connection = FakeSocket(on_send)
        original_recv = connection.recv

        def recv_after_reader_blocks():
            packet = original_recv()
            waiting.wait(0.5)
            return packet

        connection.recv = recv_after_reader_blocks
        try:
            with self.assertRaisesRegex(XfyunError, "音频尚未停止读取"):
                self.recognizer(connection).transcribe(frames(), lambda text: None, self.stop)
        finally:
            release.set()
        self.assertTrue(exited.wait(0.5))

    def test_early_final_during_pacing_restores_exactly_the_unsent_frame(self):
        throttling = threading.Event()
        release = threading.Event()
        first_frame = self.frame
        second_frame = b"\x02\x00" * 640
        restored = []

        class Frames:
            def __iter__(self):
                yield first_frame
                yield second_frame

            def cancel(self):
                release.set()

            def putback_unsent(self, frame):
                restored.append(frame)

        connection = FakeSocket()

        def pause(event, seconds):
            throttling.set()
            connection.incoming.put(result_packet(0, "提前结束", 2))
            release.wait(1)
            return event.is_set()

        final = self.recognizer(connection, wait=pause).transcribe(Frames(), lambda text: None, self.stop)
        self.assertTrue(throttling.is_set())
        self.assertEqual(final, "提前结束")
        self.assertEqual(restored, [second_frame])
        self.assertEqual([base64.b64decode(item["data"]["audio"]) for item in connection.sent],
                         [first_frame])

    def test_successful_upload_does_not_restore_frames(self):
        restored = []
        frame = self.frame

        class Frames:
            def __iter__(self):
                yield frame

            def putback_unsent(self, unsent):
                restored.append(unsent)

        def on_send(connection, payload):
            if payload["data"]["status"] == 2:
                connection.incoming.put(result_packet(0, "正常结束", 2))

        final = self.recognizer(FakeSocket(on_send)).transcribe(Frames(), lambda text: None, self.stop)
        self.assertEqual(final, "正常结束")
        self.assertEqual(restored, [])

    def test_result_status_requires_integer_excluding_boolean(self):
        for invalid_status in (True, False, 0.0, 1.0, 2.0, "2"):
            with self.subTest(status=invalid_status):
                connection = FakeSocket()
                connection.incoming.put(result_packet(0, "无效状态", invalid_status))
                with self.assertRaisesRegex(XfyunError, "结果状态不正确"):
                    self.recognizer(connection).transcribe([self.frame], lambda text: None, self.stop)


if __name__ == "__main__":
    unittest.main()
