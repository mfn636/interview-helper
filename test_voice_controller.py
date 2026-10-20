"""离线验证语音分段、自动提交、失败恢复及真实后台会话的顺序。"""

from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from floating_session import ChatResult, ChatSession
from voice_controller import VoiceController


class FakeSession:
    def __init__(self):
        self.ready = True
        self.busy = False
        self.prompts = []
        self.next_id = 0
        self.clear_count = 0

    def submit(self, prompt):
        self.next_id += 1
        self.prompts.append(prompt)
        self.busy = True
        return self.next_id

    def clear(self):
        self.busy = False
        self.clear_count += 1


class VoiceControllerTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.voice = VoiceController(self.session)

    def complete(self, *, error=None):
        self.session.busy = False
        result = ChatResult(self.session.next_id, self.session.prompts[-1],
                            reply="回答" if error is None else None, error=error)
        self.voice.accept(result)
        return result

    def test_partial_replaces_preview_without_submitting(self):
        self.voice.partial("one", "什么是")
        self.voice.partial("one", "什么是进程？")
        self.assertEqual(self.voice.preview, "什么是进程？")
        self.assertEqual(self.voice.pending_count, 0)
        self.assertIsNone(self.voice.dispatch())
        self.assertEqual(self.session.prompts, [])

    def test_final_is_deduplicated_by_id_without_deduplicating_repeated_questions(self):
        self.voice.final("one", "问题")
        self.voice.final("one", "问题")
        self.voice.partial("one", "迟到的部分结果")
        self.assertEqual(self.voice.preview, "")
        self.assertEqual(self.voice.pending_count, 1)
        self.voice.final("two", "问题")
        self.assertEqual(self.voice.pending_count, 2)
        self.assertEqual(self.voice.dispatch(), "问题")
        self.complete()
        self.assertEqual(self.voice.dispatch(), "问题")

    def test_rollover_accumulates_until_endpoint_and_does_not_answer_early(self):
        self.voice.final("connection-one", "请解释事务的", endpoint=False)
        self.assertEqual(self.voice.preview, "请解释事务的")
        self.assertIsNone(self.voice.dispatch())
        self.voice.partial("connection-two", "隔离")
        self.assertEqual(self.voice.preview, "请解释事务的隔离")
        self.voice.final("connection-two", "隔离级别。")
        self.assertEqual(self.voice.preview, "")
        self.assertEqual(self.voice.dispatch(), "请解释事务的隔离级别。")

    def test_empty_endpoint_finishes_accumulated_text_but_empty_question_is_ignored(self):
        self.voice.final("blank", " \n ")
        self.assertEqual(self.voice.pending_count, 0)
        self.voice.final("rollover", "完整问题", endpoint=False)
        self.voice.final("endpoint", "")
        self.assertEqual(self.voice.dispatch(), "完整问题")

    def test_questions_queue_while_busy_and_wait_for_accepted_answer(self):
        self.voice.final("one", "第一题")
        self.assertEqual(self.voice.dispatch(), "第一题")
        self.voice.final("two", "第二题")
        self.voice.final("three", "第三题")
        self.assertIsNone(self.voice.dispatch())
        self.assertEqual(self.voice.pending_count, 2)
        self.session.busy = False
        self.assertIsNone(self.voice.dispatch(), "未 accept 上轮结果前不能提交下一题")
        self.complete()
        self.assertEqual(self.voice.dispatch(), "第二题")
        self.complete()
        self.assertEqual(self.voice.dispatch(), "第三题")

    def test_unready_session_preserves_queue(self):
        self.session.ready = False
        self.voice.final("one", "等待初始化的问题")
        self.assertIsNone(self.voice.dispatch())
        self.assertEqual(self.voice.pending_count, 1)
        self.session.ready = True
        self.assertEqual(self.voice.dispatch(), "等待初始化的问题")

    def test_submit_exception_keeps_queue(self):
        self.voice.final("one", "保留问题")
        original = self.session.submit
        self.session.submit = Mock(side_effect=RuntimeError("线程启动失败"))
        with self.assertRaisesRegex(RuntimeError, "线程启动失败"):
            self.voice.dispatch()
        self.assertEqual(self.voice.pending_count, 1)
        self.session.submit = original
        self.assertEqual(self.voice.dispatch(), "保留问题")

    def test_failure_blocks_queue_and_retry_goes_first(self):
        self.voice.final("one", "失败题")
        self.voice.final("two", "后续题")
        self.voice.dispatch()
        failed = self.complete(error="接口异常")
        self.assertEqual(self.voice.failed_prompt, "失败题")
        self.assertEqual(self.voice.pending_count, 1)
        self.assertIsNone(self.voice.dispatch())
        self.voice.retry()
        self.voice.accept(failed)
        self.assertIsNone(self.voice.failed_prompt)
        self.assertEqual(self.voice.pending_count, 2)
        self.assertEqual(self.voice.dispatch(), "失败题")
        self.complete()
        self.assertEqual(self.voice.dispatch(), "后续题")

    def test_skip_failed_allows_next_question(self):
        self.voice.final("one", "失败题")
        self.voice.final("two", "后续题")
        self.voice.dispatch()
        self.complete(error="失败")
        self.voice.skip_failed()
        self.assertEqual(self.voice.dispatch(), "后续题")

    def test_pause_preserves_complete_questions_and_discards_unfinished_speech(self):
        self.voice.final("one", "完整问题")
        self.voice.final("two", "未结束的", endpoint=False)
        self.voice.partial("three", "语音")
        self.voice.pause()
        self.assertEqual(self.voice.preview, "")
        self.assertEqual(self.voice.pending_count, 1)
        self.assertIsNone(self.voice.dispatch())
        self.voice.resume()
        self.assertEqual(self.voice.dispatch(), "完整问题")
        self.complete()
        self.voice.final("four", "新问题")
        self.assertEqual(self.voice.dispatch(), "新问题")

    def test_pause_allows_active_answer_to_finish(self):
        self.voice.final("one", "当前题")
        self.voice.final("two", "待答题")
        self.voice.dispatch()
        self.voice.pause()
        self.complete()
        self.assertIsNone(self.voice.dispatch())
        self.voice.resume()
        self.assertEqual(self.voice.dispatch(), "待答题")

    def test_clear_resets_buffer_queue_failure_dedup_and_ignores_old_completion(self):
        self.voice.final("one", "旧问题")
        self.voice.dispatch()
        old = ChatResult(self.session.next_id, "旧问题", error="旧错误")
        self.voice.final("two", "待答题")
        self.voice.final("three", "半句话", endpoint=False)
        self.voice.clear()
        self.voice.accept(old)
        self.assertEqual(self.session.clear_count, 1)
        self.assertEqual(self.voice.pending_count, 0)
        self.assertEqual(self.voice.preview, "")
        self.assertIsNone(self.voice.failed_prompt)
        self.voice.final("one", "新问题")
        self.assertEqual(self.voice.dispatch(), "新问题")
        self.voice.accept(old)
        self.assertIsNone(self.voice.failed_prompt)

    def test_initialization_results_do_not_change_answer_tracking(self):
        self.voice.final("one", "问题")
        self.voice.dispatch()
        self.voice.accept(ChatResult(self.session.next_id, "", error="旧初始化", kind="initialization"))
        self.assertIsNone(self.voice.failed_prompt)
        self.complete()
        self.voice.final("two", "下一题")
        self.assertEqual(self.voice.dispatch(), "下一题")

    def test_real_chat_session_dispatches_next_question_after_history_is_updated(self):
        started = Event()
        release = Event()
        workers = []

        def make_thread(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            workers.append(thread)
            return thread

        def answer(prompt, *, history):
            if prompt == "第一题":
                started.set()
                if not release.wait(3):
                    raise TimeoutError("测试未释放第一题")
            return f"回答{prompt}"

        client = Mock(chat=Mock(side_effect=answer))
        client.detect_projects.return_value = ("项目一",)
        client.initialize.return_value = "我准备好了"
        with patch("floating_session.Thread", side_effect=make_thread):
            session = ChatSession(lambda: client)
            voice = VoiceController(session)
            try:
                session.initialize()
                workers[-1].join(3)
                self.assertFalse(workers[-1].is_alive())
                for result in session.poll():
                    voice.accept(result)
                self.assertFalse(session.ready)
                session.confirm_projects(("项目一",))
                workers[-1].join(3)
                self.assertFalse(workers[-1].is_alive())
                for result in session.poll():
                    voice.accept(result)
                voice.final("one", "第一题")
                self.assertEqual(voice.dispatch(), "第一题")
                self.assertTrue(started.wait(3))
                voice.final("two", "第二题")
                self.assertIsNone(voice.dispatch())
                release.set()
                workers[-1].join(3)
                self.assertFalse(workers[-1].is_alive())
                for result in session.poll():
                    voice.accept(result)
                self.assertEqual(voice.dispatch(), "第二题")
                workers[-1].join(3)
                self.assertFalse(workers[-1].is_alive())
                history = client.chat.call_args_list[1].kwargs["history"]
                self.assertEqual(history, [
                    {"role": "user", "content": "第一题"},
                    {"role": "assistant", "content": "回答第一题"},
                ])
                for result in session.poll():
                    voice.accept(result)
                self.assertEqual(len(session.history), 4)
            finally:
                release.set()
                session.close()
                for worker in workers:
                    worker.join(3)


if __name__ == "__main__":
    unittest.main()
