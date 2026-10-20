"""离线验证初始化、后台问答和旧结果隔离，不调用真实 API。"""

from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from floating_session import ChatSession


READY = "我准备好了，面试人：张三，面试岗位：Python 工程师"
PROJECTS = ("AI 面试助手", "订单系统", "数据平台")


class ControlledCall:
    def __init__(self, reply="口语回答", error=None):
        self.reply = reply
        self.error = error
        self.started = Event()
        self.release = Event()
        self.arguments = None

    def __call__(self, *args, **kwargs):
        self.arguments = (args, kwargs)
        self.started.set()
        if not self.release.wait(3):
            raise TimeoutError("测试未释放模拟请求")
        if self.error is not None:
            raise self.error
        return self.reply


class ChatSessionTests(unittest.TestCase):
    def setUp(self):
        self.workers = []
        self.calls = []
        thread_patch = patch("floating_session.Thread", side_effect=self.make_thread)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)
        self.addCleanup(self.finish_workers)

    def make_thread(self, *args, **kwargs):
        worker = Thread(*args, **kwargs)
        self.workers.append(worker)
        return worker

    def call(self, **kwargs):
        result = ControlledCall(**kwargs)
        self.calls.append(result)
        return result

    def finish_workers(self):
        for call in self.calls:
            call.release.set()
        for worker in self.workers:
            worker.join(timeout=3)

    def finish(self, call=None, worker_index=-1):
        if call is not None:
            self.assertTrue(call.started.wait(3), "后台请求没有启动")
            call.release.set()
        self.workers[worker_index].join(timeout=3)
        self.assertFalse(self.workers[worker_index].is_alive())

    def detected_session(self, client, projects=PROJECTS):
        client.detect_projects.return_value = projects
        factory = Mock(return_value=client)
        session = ChatSession(factory)
        session.initialize()
        self.finish()
        result = session.poll()[0]
        self.assertEqual(result.kind, "project_selection")
        self.assertEqual(result.projects, projects)
        self.assertTrue(session.awaiting_project_order)
        self.assertFalse(session.ready)
        client.initialize.assert_not_called()
        return session, factory

    def ready_session(self, client):
        client.initialize.return_value = READY
        session, factory = self.detected_session(client)
        session.confirm_projects(PROJECTS)
        self.finish()
        session.poll()
        self.assertTrue(session.ready)
        return session, factory

    def test_initialization_is_explicit_asynchronous_and_excluded_from_history(self):
        detection = self.call(reply=PROJECTS)
        initialization = self.call(reply=READY)
        client = Mock(detect_projects=detection, initialize=initialization)
        factory = Mock(return_value=client)
        session = ChatSession(factory)
        factory.assert_not_called()
        self.assertFalse(session.ready)
        self.assertIsNone(session.ready_message)
        with self.assertRaisesRegex(ValueError, "先完成"):
            session.submit("问题")
        request_id = session.initialize()
        self.assertTrue(detection.started.wait(3))
        self.assertTrue(session.busy)
        self.assertTrue(self.workers[0].daemon)
        with self.assertRaisesRegex(ValueError, "正在初始化"):
            session.initialize()
        self.finish(detection)
        self.assertFalse(session.ready)
        result = session.poll()[0]
        self.assertEqual((result.request_id, result.kind, result.projects),
                         (request_id, "project_selection", PROJECTS))
        self.assertFalse(session.ready)
        self.assertTrue(session.awaiting_project_order)
        self.assertFalse(initialization.started.is_set())
        self.assertEqual(session.history, [])
        request_id = session.confirm_projects(tuple(reversed(PROJECTS)))
        self.assertTrue(initialization.started.wait(3))
        self.assertTrue(session.project_order_confirmed)
        self.assertFalse(session.awaiting_project_order)
        self.assertFalse(session.ready)
        self.finish(initialization)
        result = session.poll()[0]
        self.assertEqual((result.request_id, result.kind, result.prompt, result.reply),
                         (request_id, "initialization", "", READY))
        self.assertTrue(session.ready)
        self.assertEqual(session.ready_message, READY)
        self.assertFalse(session.busy)
        self.assertEqual(session.history, [])
        with self.assertRaisesRegex(ValueError, "已经完成"):
            session.initialize()
        client.chat.assert_not_called()

    def test_success_reuses_client_and_supplies_copied_history(self):
        first = self.call(reply="第一条回答")
        second = self.call(reply="第二条回答")
        client = Mock(chat=Mock(side_effect=lambda prompt, **kw: {"第一个问题": first, "第二个问题": second}[prompt](prompt, **kw)))
        session, factory = self.ready_session(client)
        request_id = session.submit("  第一个问题  ")
        self.finish(first)
        self.assertEqual(session.history, [])
        result = session.poll()[0]
        self.assertEqual((result.request_id, result.prompt, result.reply, result.kind),
                         (request_id, "第一个问题", "第一条回答", "answer"))
        expected = [{"role": "user", "content": "第一个问题"},
                    {"role": "assistant", "content": "第一条回答"}]
        self.assertEqual(session.history, expected)
        self.assertEqual(session.poll(), [])
        session.submit("第二个问题")
        self.assertTrue(second.started.wait(3))
        snapshot = second.arguments[1]["history"]
        self.assertEqual(snapshot, expected)
        snapshot[0]["content"] = "客户端修改副本"
        self.assertEqual(session.history, expected)
        self.finish(second)
        session.poll()
        self.assertEqual(len(session.history), 4)
        factory.assert_called_once_with()
        client.initialize.assert_called_once_with()

    def test_empty_and_concurrent_submissions_are_rejected(self):
        answer = self.call()
        session, _ = self.ready_session(Mock(chat=answer))
        with self.assertRaisesRegex(ValueError, "请输入问题"):
            session.submit(" \n ")
        self.assertFalse(session.busy)
        session.submit("问题")
        self.assertTrue(answer.started.wait(3))
        with self.assertRaisesRegex(ValueError, "正在生成"):
            session.submit("重复提交")
        self.finish(answer)
        session.poll()

    def test_failed_answer_preserves_readiness_and_can_retry(self):
        client = Mock()
        client.chat.side_effect = [RuntimeError("模拟网络异常"), "重试成功"]
        session, factory = self.ready_session(client)
        session.submit("失败的问题")
        self.finish()
        self.assertEqual(session.poll()[0].error, "模拟网络异常")
        self.assertEqual(session.history, [])
        self.assertFalse(session.busy)
        self.assertTrue(session.ready)
        session.submit("重试的问题")
        self.finish()
        session.poll()
        self.assertEqual([item["content"] for item in session.history], ["重试的问题", "重试成功"])
        factory.assert_called_once_with()

    def test_detection_failures_allow_retry(self):
        bad_client = Mock()
        bad_client.detect_projects.side_effect = ValueError("项目识别格式错误")
        good_client = Mock()
        good_client.detect_projects.return_value = PROJECTS
        good_client.initialize.return_value = READY
        factory = Mock(side_effect=[OSError("资料读取失败"), bad_client, good_client])
        session = ChatSession(factory)
        for expected in ("资料读取失败", "项目识别格式错误"):
            session.initialize()
            self.finish()
            result = session.poll()[0]
            self.assertEqual((result.kind, result.error), ("initialization", expected))
            self.assertFalse(session.ready)
            self.assertFalse(session.busy)
            self.assertFalse(session.awaiting_project_order)
        session.initialize()
        self.finish()
        self.assertEqual(session.poll()[0].kind, "project_selection")
        self.assertFalse(session.ready)
        session.confirm_projects(PROJECTS)
        self.finish()
        self.assertEqual(session.poll()[0].reply, READY)
        self.assertTrue(session.ready)
        self.assertEqual(session.history, [])

    def test_clear_discards_old_answer_and_retains_initialized_client(self):
        old = self.call(reply="旧回答")
        new = self.call(reply="新回答")
        client = Mock(chat=Mock(side_effect=lambda prompt, **kw: {"旧请求": old, "新请求": new}[prompt](prompt, **kw)))
        session, factory = self.ready_session(client)
        old_id = session.submit("旧请求")
        self.assertTrue(old.started.wait(3))
        session.clear()
        self.assertFalse(session.busy)
        self.assertTrue(session.ready)
        self.assertEqual(session.ready_message, READY)
        new_id = session.submit("新请求")
        self.assertGreater(new_id, old_id)
        self.assertTrue(new.started.wait(3))
        self.assertEqual(new.arguments[1]["history"], [])
        self.finish(old, -2)
        self.assertEqual(session.poll(), [])
        self.assertTrue(session.busy)
        self.finish(new)
        self.assertEqual(session.poll()[0].reply, "新回答")
        self.assertEqual([item["content"] for item in session.history], ["新请求", "新回答"])
        factory.assert_called_once_with()

    def test_clear_during_initialization_keeps_initialization_active(self):
        initialization = self.call(reply=READY)
        client = Mock()
        session, _ = self.detected_session(client)
        client.initialize = initialization
        session.confirm_projects(PROJECTS)
        self.assertTrue(initialization.started.wait(3))
        session.clear()
        self.assertTrue(session.busy)
        self.finish(initialization)
        self.assertEqual(session.poll()[0].reply, READY)
        self.assertTrue(session.ready)
        self.assertEqual(session.project_order, PROJECTS)

    def test_close_ignores_late_initialization_and_rejects_all_work(self):
        initialization = self.call(reply=READY)
        client = Mock()
        session, _ = self.detected_session(client)
        client.initialize = initialization
        session.confirm_projects(PROJECTS)
        self.assertTrue(initialization.started.wait(3))
        session.close()
        self.finish(initialization)
        self.assertEqual(session.poll(), [])
        self.assertFalse(session.ready)
        self.assertFalse(session.busy)
        self.assertIsNone(session.ready_message)
        self.assertEqual(session.history, [])
        self.assertFalse(session.project_order_confirmed)
        self.assertFalse(session.awaiting_project_order)
        self.assertEqual(session.projects, ())
        for operation in (session.initialize, lambda: session.submit("关闭后的问题"),
                          lambda: session.confirm_projects(PROJECTS)):
            with self.assertRaisesRegex(ValueError, "已经关闭"):
                operation()

    def test_zero_single_and_multiple_projects_all_require_explicit_confirmation(self):
        for projects in ((), ("AI 面试助手",), PROJECTS):
            with self.subTest(projects=projects):
                client = Mock()
                client.initialize.return_value = READY
                session, _ = self.detected_session(client, projects)
                self.assertEqual(session.projects, projects)
                self.assertEqual(session.project_order, ())
                self.assertFalse(session.project_order_confirmed)
                with self.assertRaisesRegex(ValueError, "先完成"):
                    session.submit("问题")
                expected_order = tuple(reversed(projects))
                session.confirm_projects(list(expected_order))
                self.finish()
                session.poll()
                self.assertTrue(session.ready)
                self.assertEqual(session.project_order, expected_order)
                client.set_project_order.assert_called_once_with(expected_order)
                self.assertEqual(session.history, [])

    def test_invalid_project_orders_do_not_confirm_or_initialize(self):
        client = Mock()
        session, _ = self.detected_session(client)
        invalid_orders = [PROJECTS[:-1], PROJECTS + ("额外项目",),
                          (PROJECTS[0], PROJECTS[0], PROJECTS[2]),
                          (PROJECTS[0], PROJECTS[1], "陌生项目"),
                          (PROJECTS[0], PROJECTS[1], None), "AI 面试助手"]
        for order in invalid_orders:
            with self.subTest(order=order), self.assertRaisesRegex(ValueError, "每个项目"):
                session.confirm_projects(order)
        self.assertTrue(session.awaiting_project_order)
        self.assertFalse(session.project_order_confirmed)
        self.assertFalse(session.busy)
        client.set_project_order.assert_not_called()
        client.initialize.assert_not_called()

    def test_confirm_before_detection_or_while_working_is_rejected(self):
        detection = self.call(reply=PROJECTS)
        client = Mock(detect_projects=detection)
        session = ChatSession(lambda: client)
        with self.assertRaisesRegex(ValueError, "识别完成"):
            session.confirm_projects(PROJECTS)
        session.initialize()
        self.assertTrue(detection.started.wait(3))
        with self.assertRaisesRegex(ValueError, "识别完成"):
            session.confirm_projects(PROJECTS)
        self.finish(detection)
        session.poll()
        initialization = self.call(reply=READY)
        client.initialize = initialization
        session.confirm_projects(PROJECTS)
        self.assertTrue(initialization.started.wait(3))
        with self.assertRaisesRegex(ValueError, "识别完成"):
            session.confirm_projects(PROJECTS)
        self.finish(initialization)
        session.poll()
        with self.assertRaisesRegex(ValueError, "识别完成"):
            session.confirm_projects(PROJECTS)

    def test_readiness_failure_retains_preference_and_retries_same_client(self):
        client = Mock()
        client.initialize.side_effect = [RuntimeError("准备回复失败"), READY]
        session, factory = self.detected_session(client)
        order = tuple(reversed(PROJECTS))
        session.confirm_projects(order)
        self.finish()
        result = session.poll()[0]
        self.assertEqual((result.kind, result.error), ("initialization", "准备回复失败"))
        self.assertFalse(session.ready)
        self.assertFalse(session.awaiting_project_order)
        self.assertTrue(session.project_order_confirmed)
        self.assertEqual(session.project_order, order)
        session.clear()
        self.assertEqual(session.project_order, order)
        session.initialize()
        self.finish()
        self.assertEqual(session.poll()[0].reply, READY)
        self.assertTrue(session.ready)
        factory.assert_called_once_with()
        client.detect_projects.assert_called_once_with()
        client.set_project_order.assert_called_once_with(order)
        self.assertEqual(client.initialize.call_count, 2)
        self.assertEqual(session.history, [])

    def test_reload_after_readiness_failure_rereads_materials_and_requires_new_confirmation(self):
        first_client = Mock()
        first_client.detect_projects.return_value = PROJECTS
        first_client.initialize.side_effect = ValueError("缺少岗位")
        second_client = Mock()
        second_client.detect_projects.return_value = ("修改资料后的项目",)
        second_client.initialize.return_value = READY
        factory = Mock(side_effect=[first_client, second_client])
        session = ChatSession(factory)
        session.initialize()
        self.finish()
        session.poll()
        session.confirm_projects(tuple(reversed(PROJECTS)))
        self.finish()
        self.assertEqual(session.poll()[0].error, "缺少岗位")
        self.assertTrue(session.project_order_confirmed)

        request_id = session.reload_materials()
        self.assertFalse(session.ready)
        self.assertFalse(session.project_order_confirmed)
        self.assertFalse(session.awaiting_project_order)
        self.assertEqual(session.projects, ())
        self.assertEqual(session.project_order, ())
        self.assertIsNone(session.ready_message)
        self.finish()
        result = session.poll()[0]
        self.assertEqual((result.request_id, result.kind, result.projects),
                         (request_id, "project_selection", ("修改资料后的项目",)))
        self.assertFalse(session.ready)
        self.assertTrue(session.awaiting_project_order)
        second_client.initialize.assert_not_called()
        self.assertEqual(factory.call_count, 2)
        first_client.initialize.assert_called_once_with()

        session.confirm_projects(("修改资料后的项目",))
        self.finish()
        self.assertEqual(session.poll()[0].reply, READY)
        self.assertTrue(session.ready)
        self.assertEqual(session.project_order, ("修改资料后的项目",))
        self.assertEqual(session.history, [])

    def test_reload_rejects_busy_ready_and_closed_sessions(self):
        detection = self.call(reply=PROJECTS)
        client = Mock(detect_projects=detection)
        client.initialize.return_value = READY
        session = ChatSession(lambda: client)
        session.initialize()
        self.assertTrue(detection.started.wait(3))
        with self.assertRaisesRegex(ValueError, "正在初始化"):
            session.reload_materials()
        self.assertTrue(session.busy)
        self.finish(detection)
        session.poll()
        session.confirm_projects(PROJECTS)
        with self.assertRaisesRegex(ValueError, "正在初始化"):
            session.reload_materials()
        self.finish()
        session.poll()
        with self.assertRaisesRegex(ValueError, "已经完成"):
            session.reload_materials()
        self.assertTrue(session.ready)
        self.assertEqual(session.project_order, PROJECTS)
        session.close()
        with self.assertRaisesRegex(ValueError, "已经关闭"):
            session.reload_materials()

    def test_reinitialize_while_selecting_rereads_materials_and_detects_again(self):
        first_client = Mock()
        first_client.detect_projects.return_value = PROJECTS
        second_client = Mock()
        second_client.detect_projects.return_value = ("新项目",)
        factory = Mock(side_effect=[first_client, second_client])
        session = ChatSession(factory)
        session.initialize()
        self.finish()
        session.poll()
        self.assertEqual(session.projects, PROJECTS)
        session.clear()
        self.assertTrue(session.awaiting_project_order)
        session.initialize()
        self.assertFalse(session.awaiting_project_order)
        self.assertEqual(session.projects, ())
        self.finish()
        self.assertEqual(session.poll()[0].projects, ("新项目",))
        self.assertEqual(session.projects, ("新项目",))
        self.assertFalse(session.project_order_confirmed)
        first_client.initialize.assert_not_called()
        second_client.initialize.assert_not_called()

    def test_clear_during_detection_keeps_selection_pending(self):
        detection = self.call(reply=PROJECTS)
        session = ChatSession(lambda: Mock(detect_projects=detection))
        session.initialize()
        self.assertTrue(detection.started.wait(3))
        session.clear()
        self.assertTrue(session.busy)
        self.finish(detection)
        self.assertEqual(session.poll()[0].kind, "project_selection")
        self.assertTrue(session.awaiting_project_order)
        self.assertFalse(session.ready)

    def test_close_discards_late_project_detection(self):
        detection = self.call(reply=PROJECTS)
        client = Mock(detect_projects=detection)
        session = ChatSession(lambda: client)
        session.initialize()
        self.assertTrue(detection.started.wait(3))
        session.close()
        self.finish(detection)
        self.assertEqual(session.poll(), [])
        self.assertEqual(session.projects, ())
        self.assertFalse(session.awaiting_project_order)
        self.assertFalse(session.ready)
        client.initialize.assert_not_called()

    def test_switching_role_changes_next_request_and_starts_fresh_history(self):
        client = Mock()
        client.role = "candidate"
        requests = []

        def change_role(role):
            client.role = role

        def answer(prompt, history):
            requests.append((client.role, prompt, history))
            return f"{client.role}的回复"

        client.set_role.side_effect = change_role
        client.chat.side_effect = answer
        session, factory = self.ready_session(client)
        self.assertEqual(session.role, "candidate")
        client.set_role.assert_not_called()
        session.submit("说说你的项目")
        self.finish()
        session.poll()
        self.assertEqual(requests[0], ("candidate", "说说你的项目", []))
        self.assertEqual(len(session.history), 2)

        session.set_role("interviewer")
        self.assertEqual(session.role, "interviewer")
        self.assertEqual(session.history, [])
        self.assertTrue(session.ready)
        self.assertEqual(session.ready_message, READY)
        self.assertEqual(session.projects, PROJECTS)
        self.assertEqual(session.project_order, PROJECTS)
        self.assertTrue(session.project_order_confirmed)
        session.submit("我负责了项目中的缓存模块")
        self.finish()
        session.poll()
        self.assertEqual(requests[1], ("interviewer", "我负责了项目中的缓存模块", []))

        same_role_history = [dict(message) for message in session.history]
        session.set_role("interviewer")
        self.assertEqual(session.history, same_role_history)
        client.set_role.assert_called_once_with("interviewer")
        session.submit("我们使用了 Redis")
        self.finish()
        session.poll()
        self.assertEqual(requests[2], ("interviewer", "我们使用了 Redis", same_role_history))

        session.set_role("candidate")
        self.assertEqual(session.role, "candidate")
        self.assertEqual(session.history, [])
        self.assertTrue(session.ready)
        self.assertEqual(client.set_role.call_count, 2)
        factory.assert_called_once_with()
        client.initialize.assert_called_once_with()
        client.detect_projects.assert_called_once_with()

    def test_selecting_role_before_initialization_applies_before_detection(self):
        client = Mock()
        client.role = "candidate"
        factory = Mock(return_value=client)
        session = ChatSession(factory)

        def change_role(role):
            client.role = role

        def detect():
            self.assertEqual(client.role, "interviewer")
            return PROJECTS

        client.set_role.side_effect = change_role
        client.detect_projects.side_effect = detect
        client.initialize.return_value = READY
        session.set_role("interviewer")
        self.assertEqual(session.role, "interviewer")
        factory.assert_not_called()
        client.set_role.assert_not_called()
        session.initialize()
        self.finish()
        result = session.poll()[0]
        self.assertEqual((result.kind, result.projects), ("project_selection", PROJECTS))
        self.assertTrue(session.awaiting_project_order)
        client.set_role.assert_called_once_with("interviewer")
        session.confirm_projects(PROJECTS)
        self.finish()
        session.poll()
        self.assertTrue(session.ready)
        self.assertEqual(session.role, "interviewer")

    def test_role_switching_rejects_busy_initialization_and_answer_requests(self):
        detection = self.call(reply=PROJECTS)
        client = Mock(detect_projects=detection)
        client.initialize.return_value = READY
        session = ChatSession(lambda: client)
        session.initialize()
        self.assertTrue(detection.started.wait(3))
        for role in ("interviewer", "candidate"):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "稍候再切换"):
                session.set_role(role)
        self.assertEqual(session.role, "candidate")
        self.finish(detection)
        session.poll()
        session.confirm_projects(PROJECTS)
        self.finish()
        session.poll()

        client.chat.return_value = "已有回答"
        session.submit("已有问题")
        self.finish()
        session.poll()
        previous_history = [dict(message) for message in session.history]
        answer = self.call()
        client.chat = answer
        session.submit("正在回答的问题")
        self.assertTrue(answer.started.wait(3))
        with self.assertRaisesRegex(ValueError, "稍候再切换"):
            session.set_role("interviewer")
        self.assertEqual(session.role, "candidate")
        self.assertEqual(session.history, previous_history)
        self.assertTrue(session.busy)
        client.set_role.assert_not_called()
        self.finish(answer)
        session.poll()
        self.assertEqual(len(session.history), 4)

    def test_failed_role_change_retains_old_history_and_readiness(self):
        client = Mock()
        client.chat.return_value = "已有回答"
        session, _ = self.ready_session(client)
        session.submit("已有问题")
        self.finish()
        session.poll()
        previous_history = [dict(message) for message in session.history]
        client.set_role.side_effect = OSError("面试官提示词读取失败")
        with self.assertRaisesRegex(OSError, "提示词读取失败"):
            session.set_role("interviewer")
        self.assertEqual(session.role, "candidate")
        self.assertEqual(session.history, previous_history)
        self.assertTrue(session.ready)
        self.assertEqual(session.ready_message, READY)
        self.assertEqual(session.project_order, PROJECTS)
        session.submit("继续原角色的问题")
        self.finish()
        session.poll()
        self.assertEqual(client.chat.call_args.kwargs["history"], previous_history)

    def test_role_is_retained_when_reloading_materials_after_readiness_failure(self):
        first_client = Mock()
        first_client.detect_projects.return_value = PROJECTS
        first_client.initialize.side_effect = ValueError("缺少岗位")
        second_client = Mock()
        second_client.detect_projects.return_value = ("新项目",)
        second_client.initialize.return_value = READY
        factory = Mock(side_effect=[first_client, second_client])
        session = ChatSession(factory)
        session.set_role("interviewer")
        session.initialize()
        self.finish()
        session.poll()
        session.confirm_projects(PROJECTS)
        self.finish()
        self.assertEqual(session.poll()[0].error, "缺少岗位")
        session.reload_materials()
        self.finish()
        self.assertEqual(session.poll()[0].projects, ("新项目",))
        self.assertEqual(session.role, "interviewer")
        self.assertFalse(session.project_order_confirmed)
        first_client.set_role.assert_called_once_with("interviewer")
        second_client.set_role.assert_called_once_with("interviewer")
        session.confirm_projects(("新项目",))
        self.finish()
        session.poll()
        self.assertTrue(session.ready)
        self.assertEqual(session.role, "interviewer")

    def test_invalid_and_closed_role_changes_are_rejected_without_losing_history(self):
        client = Mock()
        session, _ = self.ready_session(client)
        session.history.append({"role": "user", "content": "已有问题"})
        previous_history = [dict(message) for message in session.history]
        for role in ("", "assistant", "面试官", None, [], 1):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "请选择"):
                session.set_role(role)
        self.assertEqual(session.role, "candidate")
        self.assertEqual(session.history, previous_history)
        client.set_role.assert_not_called()
        session.close()
        with self.assertRaisesRegex(ValueError, "已经关闭"):
            session.set_role("interviewer")


if __name__ == "__main__":
    unittest.main()
