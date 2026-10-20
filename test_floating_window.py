"""不创建 Tk 窗口，验证主线程如何衔接语音、排队和聊天结果。"""

from collections import deque
from queue import SimpleQueue
import unittest
from unittest.mock import Mock, patch

from floating_session import ChatResult, ChatSession, _Completion
from floating_window import FloatingWindow
from speech_session import SpeechEvent
from voice_controller import VoiceController


READY = "我准备好了，面试人：张三，面试岗位：Python 工程师"
PROJECTS = ("检索项目", "订单项目", "数据项目")


class FakeValue:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeWidget:
    def __init__(self):
        self.options = {}
        self.text = ""

    def configure(self, **options):
        self.options.update(options)

    def insert(self, position, text, *args):
        self.text += text

    def delete(self, *args):
        self.text = ""

    def see(self, *args):
        pass


class RecordingSession(ChatSession):
    """使用真实 poll/clear；只替换后台派发，不创建线程或调用 API。"""

    def __init__(self, ready=True):
        super().__init__(client_factory=Mock())
        self.submitted = []
        self.submitted_histories = []
        if ready:
            self._client = Mock()
            self._initialized = True
            self._project_order_confirmed = True
            self._projects = PROJECTS
            self._project_order = PROJECTS
            self._ready_message = READY

    def _start(self, kind, target, *args):
        self._next_request_id += 1
        self._active_request_id = self._next_request_id
        self._active_kind = kind
        if kind == "answer":
            self.submitted.append(args[1])
            self.submitted_histories.append(args[2])
        return self._next_request_id

    def complete(self, reply="回答", error=None, projects=None):
        kind = "project_selection" if projects is not None else self._active_kind
        prompt = self.submitted[-1] if kind == "answer" else ""
        result = ChatResult(self._active_request_id, prompt, reply=reply, error=error, kind=kind,
                            projects=projects or ())
        client = (self._client or Mock()) if kind != "answer" else None
        self._results.put(_Completion(result, client))
        return result


class FakeSpeech:
    def __init__(self, running=True):
        self.running = running
        self.events = deque()
        self.stop_calls = 0
        self.start_calls = []

    def poll(self):
        events = list(self.events)
        self.events.clear()
        if any(event.kind == "stopped" for event in events):
            self.running = False
        return events

    def stop(self):
        self.stop_calls += 1
        self.running = False
        self.events.clear()

    def start(self, device_index=None):
        self.start_calls.append(device_index)
        self.running = True

    def close(self):
        self.stop()


def make_window(ready=True, listening=True):
    window = FloatingWindow.__new__(FloatingWindow)
    window.root = Mock()
    window.root.after.return_value = "poll-timer"
    window.session = RecordingSession(ready=ready)
    window.speech = FakeSpeech(running=listening)
    window.voice = VoiceController(window.session)
    if not listening:
        window.voice.pause()
    window.device_results = SimpleQueue()
    window.device_request = 0
    window.devices = {}
    window.closed = False
    window.poll_timer = None
    window.copy_timer = None
    window.last_answer = ""
    window.waiting_ticks = 0
    window.ready_message = READY if ready else ""
    window.project_dialog = None
    for name in ("status", "turn_count", "speech_state", "queue_status"):
        setattr(window, name, FakeValue())
    window.audio_device = FakeValue("Windows 默认输出")
    for name in ("transcript", "preview", "clear_button", "listen_button", "retry_button",
                 "skip_button", "copy_button", "refresh_button", "device_selector",
                 "role_button", "conversation_title"):
        setattr(window, name, FakeWidget())
    return window


class FloatingWindowFlowTests(unittest.TestCase):
    def event(self, window, kind, uid="one", text="", endpoint=True):
        window.speech.events.append(SpeechEvent(1, kind, uid, text, endpoint))

    def test_role_button_shows_current_role_and_disables_during_requests(self):
        window = make_window(listening=False)
        window._update_controls()
        self.assertEqual(window.role_button.options["text"], "面试者 → 切换为面试官")
        self.assertEqual(window.role_button.options["state"], "normal")
        window.session.submit("正在回答的问题")
        window._update_controls()
        self.assertEqual(window.role_button.options["state"], "disabled")
        window.switch_role()
        self.assertEqual(window.session.role, "candidate")
        window.session._client.set_role.assert_not_called()
        self.assertIn("等待当前回复完成", window.status.get())

    def test_switch_role_clears_old_dialogue_queue_and_transcription_but_retains_materials(self):
        window = make_window()
        self.event(window, "final", text="旧问题")
        window._poll()
        window.session.complete(error="请求失败")
        window._poll()
        window.voice.final("queued", "旧的排队问题")
        window.voice.partial("partial", "未说完")
        self.event(window, "final", uid="late", text="未处理的旧转写")
        window.session.history[:] = [{"role": "user", "content": "旧角色的历史"}]
        window.last_answer = "旧回答"
        window.copy_timer = "copy-timer"
        client = window.session._client

        window.switch_role()

        client.set_role.assert_called_once_with("interviewer")
        self.assertIs(window.session._client, client)
        self.assertEqual(window.session.role, "interviewer")
        self.assertTrue(window.session.ready)
        self.assertEqual(window.session.project_order, PROJECTS)
        self.assertEqual(window.session.ready_message, READY)
        self.assertEqual(window.session.history, [])
        self.assertFalse(window.speech.running)
        self.assertEqual(list(window.speech.events), [])
        self.assertEqual(window.voice.pending_count, 0)
        self.assertIsNone(window.voice.failed_prompt)
        self.assertEqual(window.voice.preview, "")
        self.assertEqual(window.preview.text, "")
        self.assertEqual(window.last_answer, "")
        self.assertEqual(window.turn_count.get(), "0 轮对话")
        self.assertEqual(window.copy_button.options["text"], "复制最新提问")
        self.assertEqual(window.copy_button.options["state"], "disabled")
        window.root.after_cancel.assert_called_with("copy-timer")
        self.assertIsNone(window.copy_timer)
        self.assertIn(READY, window.transcript.text)
        self.assertNotIn("旧问题", window.transcript.text)
        self.assertEqual(window.conversation_title.options["text"], "提问与对话")
        self.assertEqual(window.role_button.options["text"], "面试官 → 切换为面试者")

    def test_interviewer_turn_uses_new_history_and_displays_question_labels(self):
        window = make_window(listening=False)
        window.switch_role()
        window.toggle_listening()
        self.event(window, "final", text="我准备好开始面试了")
        window._poll()
        self.assertEqual(window.session.submitted_histories, [[]])
        self.assertIn("候选人的发言", window.transcript.text)
        window.session.complete(reply="请介绍一下你的检索项目。")
        window._poll()
        self.assertIn("面试官提问", window.transcript.text)
        self.assertEqual(window.last_answer, "请介绍一下你的检索项目。")
        self.assertEqual(window.copy_button.options["text"], "复制最新提问")
        self.assertEqual(window.session.history[-1]["content"], "请介绍一下你的检索项目。")
        window.copy_answer()
        window.root.clipboard_append.assert_called_with("请介绍一下你的检索项目。")
        window._reset_copy_label()
        self.assertEqual(window.copy_button.options["text"], "复制最新提问")

        window.switch_role()
        self.assertEqual(window.session.role, "candidate")
        self.assertEqual(window.session.history, [])
        self.assertFalse(window.speech.running)
        self.assertEqual(window.conversation_title.options["text"], "回答与对话")
        self.assertEqual(window.copy_button.options["text"], "复制最新回答")

    def test_failed_role_switch_preserves_current_conversation_and_listening(self):
        window = make_window()
        window.session.history[:] = [{"role": "user", "content": "已保存的对话"}]
        window.session._client.set_role.side_effect = ValueError("面试官提示词无法读取")
        window.last_answer = "已有回答"
        window.switch_role()
        self.assertEqual(window.session.role, "candidate")
        self.assertEqual(window.session.history, [{"role": "user", "content": "已保存的对话"}])
        self.assertTrue(window.speech.running)
        self.assertEqual(window.speech.stop_calls, 0)
        self.assertEqual(window.last_answer, "已有回答")
        self.assertIn("切换角色失败", window.status.get())

    def test_role_can_change_while_waiting_for_project_confirmation(self):
        window = make_window(ready=False, listening=False)
        window.initialize()
        window.session.complete(projects=PROJECTS)
        with patch("floating_window.ProjectOrderDialog"):
            window._poll()
        dialog = window.project_dialog
        window.switch_role()
        self.assertEqual(window.session.role, "interviewer")
        self.assertFalse(window.session.ready)
        self.assertTrue(window.session.awaiting_project_order)
        self.assertEqual(window.session.projects, PROJECTS)
        self.assertIs(window.project_dialog, dialog)
        self.assertEqual(window.speech.start_calls, [])
        self.assertEqual(window.listen_button.options["state"], "disabled")

    def test_initialization_enables_listening_but_does_not_open_audio(self):
        window = make_window(ready=False, listening=False)
        window.initialize()
        self.assertTrue(window.session.busy)
        self.assertEqual(window.listen_button.options["state"], "disabled")
        window.session.complete(projects=PROJECTS)
        with patch("floating_window.ProjectOrderDialog") as dialog:
            window._poll()
        self.assertTrue(window.session.awaiting_project_order)
        self.assertFalse(window.session.ready)
        self.assertNotIn(READY, window.transcript.text)
        self.assertEqual(window.listen_button.options["state"], "disabled")
        self.assertEqual(dialog.call_args.args[1], PROJECTS)
        window.toggle_listening()
        self.assertEqual(window.speech.start_calls, [])
        window._confirm_projects(PROJECTS[::-1])
        self.assertFalse(window.session.ready)
        window.session.complete(reply=READY)
        with patch("floating_window.sys.stdout", None):
            window._poll()
        self.assertTrue(window.session.ready)
        self.assertIn(READY, window.transcript.text)
        self.assertEqual(window.listen_button.options["state"], "normal")
        self.assertEqual(window.speech.start_calls, [])
        self.assertEqual(window.session.submitted, [])
        self.assertIn("数据项目 → 订单项目 → 检索项目", window.transcript.text)

    def test_deferred_selection_can_reopen_without_reloading_or_starting_audio(self):
        window = make_window(ready=False, listening=False)
        window.initialize()
        window.session.complete(projects=PROJECTS)
        with patch("floating_window.ProjectOrderDialog") as dialog:
            window._poll()
            window._project_dialog_closed()
            request_id = window.session._next_request_id
            window.retry()
            self.assertEqual(dialog.call_count, 2)
        self.assertEqual(window.session._next_request_id, request_id)
        self.assertTrue(window.session.awaiting_project_order)
        self.assertFalse(window.session.ready)
        self.assertEqual(window.speech.start_calls, [])

    def test_clear_retains_confirmed_project_order(self):
        window = make_window(listening=False)
        window.session._project_order = PROJECTS[::-1]
        window.clear()
        self.assertEqual(window.session.project_order, PROJECTS[::-1])
        self.assertTrue(window.session.ready)
        self.assertIn("数据项目 → 订单项目 → 检索项目", window.transcript.text)

    def test_reload_after_ready_failure_discards_old_materials_and_order(self):
        window = make_window(ready=False, listening=False)
        window.initialize()
        window.session.complete(projects=PROJECTS)
        with patch("floating_window.ProjectOrderDialog"):
            window._poll()
        window._confirm_projects(PROJECTS[::-1])
        window._project_dialog_closed()
        window.session.complete(error="资料缺少岗位名称")
        with patch("floating_window.sys.stderr", None):
            window._poll()
        self.assertTrue(window.session.project_order_confirmed)
        self.assertEqual(window.clear_button.options["text"], "重新加载资料")
        window.clear()
        self.assertTrue(window.session.busy)
        self.assertFalse(window.session.project_order_confirmed)
        self.assertEqual(window.session.projects, ())
        self.assertEqual(window.speech.start_calls, [])

    def test_failed_initialization_can_be_retried_without_opening_audio(self):
        window = make_window(ready=False, listening=False)
        window.initialize()
        window.session.complete(reply=None, error="密钥未配置")
        with patch("floating_window.sys.stderr", None):
            window._poll()
        self.assertFalse(window.session.ready)
        self.assertIn("密钥未配置", window.transcript.text)
        self.assertEqual(window.retry_button.options["state"], "normal")
        self.assertEqual(window.listen_button.options["state"], "disabled")
        window.retry()
        self.assertTrue(window.session.busy)
        self.assertEqual(window.speech.start_calls, [])

    def test_corrected_partials_and_rollover_only_dispatch_at_pause(self):
        window = make_window()
        self.event(window, "partial", text="解释进程")
        self.event(window, "partial", text="请解释进程和线程")
        window._poll()
        self.assertEqual(window.preview.text, "请解释进程和线程")
        self.assertEqual(window.session.submitted, [])
        self.event(window, "final", text="请解释进程和线程", endpoint=False)
        self.event(window, "partial", uid="two", text="有什么区别？")
        window._poll()
        self.assertEqual(window.preview.text, "请解释进程和线程有什么区别？")
        self.assertEqual(window.session.submitted, [])
        self.event(window, "final", uid="two", text="有什么区别？", endpoint=False)
        self.event(window, "final", uid="boundary", text="", endpoint=True)
        window._poll()
        question = "请解释进程和线程有什么区别？"
        self.assertEqual(window.session.submitted, [question])
        self.assertEqual(window.preview.text, question)
        self.assertEqual(window.transcript.text.count(question), 1)

    def test_next_question_uses_history_committed_by_the_same_poll(self):
        window = make_window()
        self.event(window, "final", text="第一题")
        window._poll()
        self.event(window, "final", uid="two", text="第二题")
        window._poll()
        self.assertEqual(window.voice.pending_count, 1)
        window.session.complete(reply="第一题的答案")
        window._poll()
        self.assertEqual(window.session.submitted, ["第一题", "第二题"])
        self.assertEqual(window.session.submitted_histories[1], [
            {"role": "user", "content": "第一题"},
            {"role": "assistant", "content": "第一题的答案"},
        ])
        self.assertEqual(window.turn_count.get(), "1 轮对话")
        self.assertEqual(window.last_answer, "第一题的答案")

    def test_failed_answer_blocks_queue_until_explicit_retry(self):
        window = make_window()
        self.event(window, "final", text="第一题")
        window._poll()
        self.event(window, "final", uid="two", text="第二题")
        window.session.complete(reply=None, error="请求超时")
        window._poll()
        self.assertEqual(window.voice.failed_prompt, "第一题")
        self.assertEqual(window.voice.pending_count, 1)
        self.assertEqual(window.session.submitted, ["第一题"])
        self.assertEqual(window.session.history, [])
        self.assertEqual(window.retry_button.options["state"], "normal")
        window.retry()
        window._poll()
        self.assertEqual(window.session.submitted, ["第一题", "第一题"])
        window.session.complete(reply="重试成功")
        window._poll()
        self.assertEqual(window.session.submitted, ["第一题", "第一题", "第二题"])

    def test_pause_preserves_complete_queue_and_discards_unfinished_speech(self):
        window = make_window()
        self.event(window, "final", text="第一题")
        window._poll()
        self.event(window, "final", uid="two", text="第二题")
        self.event(window, "partial", uid="unfinished", text="尚未讲完")
        window._poll()
        window.toggle_listening()
        self.assertEqual(window.preview.text, "")
        self.assertEqual(window.voice.preview, "")
        self.assertEqual(window.voice.pending_count, 1)
        window.session.complete(reply="第一题答案")
        window._poll()
        self.assertEqual(window.session.submitted, ["第一题"])
        window.toggle_listening()
        window._poll()
        self.assertEqual(window.session.submitted, ["第一题", "第二题"])

    def test_asr_error_discards_partial_and_prevents_automatic_queue_dispatch(self):
        window = make_window()
        window.voice.final("queued", "已有完整问题")
        self.event(window, "partial", text="未完成的语音")
        self.event(window, "error", text="设备断开")
        self.event(window, "stopped")
        window._poll()
        self.assertEqual(window.preview.text, "")
        self.assertEqual(window.voice.preview, "")
        self.assertEqual(window.voice.pending_count, 1)
        self.assertEqual(window.session.submitted, [])
        self.assertIn("设备断开", window.status.get())
        self.assertEqual(window.speech_state.get(), "已停止")

    def test_submission_failure_retains_queue_and_pauses_capture(self):
        window = make_window()
        self.event(window, "final", text="请解释闭包")
        with patch.object(window.session, "submit", side_effect=ValueError("提交失败")):
            window._poll()
        self.assertFalse(window.speech.running)
        self.assertEqual(window.voice.pending_count, 1)
        self.assertIn("提交失败", window.status.get())
        window.toggle_listening()
        window._poll()
        self.assertEqual(window.session.submitted, ["请解释闭包"])

    def test_new_conversation_clears_queue_and_ignores_late_answer(self):
        window = make_window()
        self.event(window, "final", text="旧问题")
        window._poll()
        window.voice.final("queued", "排队的旧问题")
        window.voice.partial("unfinished", "旧的临时转写")
        window.session.complete(reply="稍后才被poll的旧回答")
        window.clear()
        window._poll()
        self.assertFalse(window.speech.running)
        self.assertTrue(window.session.ready)
        self.assertEqual(window.session.history, [])
        self.assertEqual(window.voice.pending_count, 0)
        self.assertIsNone(window.voice.failed_prompt)
        self.assertEqual(window.preview.text, "")
        self.assertEqual(window.last_answer, "")
        self.assertEqual(window.turn_count.get(), "0 轮对话")
        self.assertNotIn("旧回答", window.transcript.text)
        self.assertIn(READY, window.transcript.text)


if __name__ == "__main__":
    unittest.main()
