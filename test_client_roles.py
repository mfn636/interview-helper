"""双角色提示词切换的离线回归，不读取本地密钥或调用真实 API。"""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_client import DeepSeekClient, ROLE_LABELS


class ClientRoleTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"API_KEY": "offline-test-key"})
        environment.start()
        self.addCleanup(environment.stop)
        settings = patch("deepseek_client.dotenv_values", return_value={"API_KEY": "offline-test-key"})
        settings.start()
        self.addCleanup(settings.stop)
        post = patch("deepseek_client.requests.post")
        self.post = post.start()
        self.addCleanup(post.stop)
        self.post.return_value.json.return_value = {"choices": [{"message": {"content": "测试回复"}}]}
        self.client = DeepSeekClient(context_path=None)

    def test_default_and_explicit_roles_load_distinct_prompts(self):
        self.assertEqual(ROLE_LABELS, {"candidate": "面试者", "interviewer": "面试官"})
        self.assertEqual(self.client.role, "candidate")
        candidate_prompt = self.client.system_prompt
        interviewer = DeepSeekClient(context_path=None, role="interviewer")
        self.assertEqual(interviewer.role, "interviewer")
        self.assertNotEqual(interviewer.system_prompt, candidate_prompt)
        self.assertIn("你的身份是正在回答问题的候选人", candidate_prompt)
        self.assertIn("用户消息通常是候选人的讲话或回答", interviewer.system_prompt)
        self.post.assert_not_called()

    def test_switch_changes_next_request_preserving_materials_preferences_and_readiness(self):
        self.client.resume = "姓名：测试人；项目：订单平台"
        self.client.job_description = "Python工程师"
        self.client.interview_materials = "<interview_materials>简历与岗位资料</interview_materials>"
        self.client._projects = ("订单平台", "知识检索平台")
        self.client.set_project_order(("知识检索平台", "订单平台"))
        self.client.ready_message = "我准备好了，面试人：测试人，面试岗位：Python工程师"
        snapshot = (self.client.resume, self.client.job_description, self.client.interview_materials,
                    self.client.projects, self.client.project_order, self.client.ready_message)
        candidate_prompt = self.client.system_prompt
        self.client.set_role("interviewer")
        self.post.assert_not_called()
        self.assertEqual((self.client.resume, self.client.job_description, self.client.interview_materials,
                          self.client.projects, self.client.project_order, self.client.ready_message), snapshot)
        self.assertTrue(self.client.project_order_confirmed)
        self.assertEqual(self.client.initialize(), snapshot[-1])
        self.post.assert_not_called()
        history = [{"role": "user", "content": "我们用了缓存"}, {"role": "assistant", "content": "如何失效？"}]
        self.client.chat("通过事件通知失效", history=history)
        messages = self.post.call_args.kwargs["json"]["messages"]
        system = messages[0]["content"]
        self.assertIn(self.client.system_prompt, system)
        self.assertNotIn(candidate_prompt, system)
        self.assertIn(snapshot[2], system)
        self.assertIn('["知识检索平台", "订单平台"]', system)
        self.assertIn("候选人或已有问题明确点名项目", system)
        self.assertIn("不替候选人作答", system)
        self.assertNotIn("优先围绕该项目回答", system)
        self.assertEqual(messages[1:-1], history)
        self.assertEqual(messages[-1], {"role": "user", "content": "通过事件通知失效"})
        self.client.set_role("candidate")
        self.assertEqual(self.client.system_prompt, candidate_prompt)
        self.assertIn("优先围绕该项目回答", self.client._project_preferences())

    def test_invalid_role_does_not_replace_previous_role_or_prompt(self):
        previous_prompt = self.client.system_prompt
        for value in ("", "unknown", "面试官", None, ["interviewer"]):
            with self.subTest(role=value):
                with self.assertRaises(ValueError):
                    self.client.set_role(value)
                self.assertEqual(self.client.role, "candidate")
                self.assertEqual(self.client.system_prompt, previous_prompt)
        self.post.assert_not_called()

    def test_failed_prompt_read_or_empty_prompt_keeps_previous_state(self):
        self.client.ready_message = "准备缓存"
        previous_prompt = self.client.system_prompt
        for failure in (FileNotFoundError("missing"), PermissionError("unreadable")):
            with self.subTest(failure=type(failure).__name__), patch.object(Path, "read_text", side_effect=failure):
                with self.assertRaises(type(failure)):
                    self.client.set_role("interviewer")
            self.assertEqual(self.client.role, "candidate")
            self.assertEqual(self.client.system_prompt, previous_prompt)
            self.assertEqual(self.client.ready_message, "准备缓存")
        with patch.object(Path, "read_text", return_value=" \n "):
            with self.assertRaisesRegex(ValueError, "不能为空"):
                self.client.set_role("interviewer")
        self.assertEqual(self.client.role, "candidate")
        self.assertEqual(self.client.system_prompt, previous_prompt)
        self.assertEqual(self.client.ready_message, "准备缓存")
        self.post.assert_not_called()

    def test_interviewer_initialize_keeps_fixed_ready_format(self):
        self.client.set_role("interviewer")
        self.client._projects = ()
        self.client.set_project_order(())
        expected = "我准备好了，面试人：测试人，面试岗位：Python工程师"
        self.post.return_value.json.return_value = {"choices": [{"message": {"content": expected}}]}
        self.assertEqual(self.client.initialize(), expected)
        messages = self.post.call_args.kwargs["json"]["messages"]
        self.assertIn("初始化确认只表示资料准备就绪，不追加面试问题", messages[0]["content"])
        self.assertIn("严格使用以下格式：我准备好了", messages[-1]["content"])

    def test_interviewer_chat_preserves_single_paragraph_normalization(self):
        self.client.set_role("interviewer")
        self.post.return_value.json.return_value = {"choices": [{"message": {"content": "  为什么使用缓存？\n\n 更新时如何失效？  "}}]}
        self.assertEqual(self.client.chat("我们使用缓存。"), "为什么使用缓存？ 更新时如何失效？")


if __name__ == "__main__":
    unittest.main()
