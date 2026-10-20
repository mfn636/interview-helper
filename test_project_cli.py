"""项目确认的 CLI 回归测试，不调用网络。"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import loop


class ProjectOrderCliTests(unittest.TestCase):
    def test_number_order_is_complete_and_explicit(self):
        projects = ("项目甲", "项目乙", "项目丙")
        for value in ("3 1 2", "3,1,2", "3，1，2"):
            self.assertEqual(loop.parse_project_order(value, projects), ("项目丙", "项目甲", "项目乙"))
        for value in ("", "1 2", "1 1 3", "0 1 2", "1 2 4", "默认", "none"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                loop.parse_project_order(value, projects)

    def test_no_project_requires_explicit_none(self):
        self.assertEqual(loop.parse_project_order("none", ()), ())
        for value in ("", "1", "默认"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                loop.parse_project_order(value, ())

    def test_invalid_choice_reprompts_before_initialize(self):
        client = Mock()
        client.project_order_confirmed = False
        client.detect_projects.return_value = ("甲", "乙", "丙")
        client.initialize.return_value = "我准备好了，面试人：测试，面试岗位：开发"
        client.chat.return_value = "回答"
        with patch("loop.DeepSeekClient", return_value=client), patch(
            "builtins.input", side_effect=["1 1 2", "3 1 2", "/clear", "问题", "/exit"]
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(loop.main(), 0)
        self.assertEqual([call[0] for call in client.mock_calls], [
            "detect_projects", "set_project_order", "initialize", "chat",
        ])
        client.set_project_order.assert_called_once_with(("丙", "甲", "乙"))

    def test_single_project_still_requires_confirmation(self):
        client = Mock()
        client.detect_projects.return_value = ("唯一项目",)
        with patch("builtins.input", side_effect=["", "1"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertTrue(loop.confirm_project_order(client))
        client.set_project_order.assert_called_once_with(("唯一项目",))

    def test_no_project_can_exit_or_explicitly_continue(self):
        for raw, expected in (("/exit", False), ("none", True)):
            with self.subTest(raw=raw):
                client = Mock()
                client.detect_projects.return_value = ()
                with patch("builtins.input", return_value=raw), redirect_stdout(io.StringIO()):
                    self.assertEqual(loop.confirm_project_order(client), expected)
                if expected:
                    client.set_project_order.assert_called_once_with(())
                else:
                    client.set_project_order.assert_not_called()

    def test_interrupt_during_setup_does_not_initialize(self):
        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error=error):
                client = Mock()
                client.project_order_confirmed = False
                client.detect_projects.return_value = ("项目",)
                with patch("loop.DeepSeekClient", return_value=client), patch("builtins.input", side_effect=error), redirect_stdout(io.StringIO()):
                    self.assertEqual(loop.main(), 0)
                client.initialize.assert_not_called()
                client.set_project_order.assert_not_called()

    def test_confirmed_order_is_reused_without_asking_again(self):
        client = Mock()
        client.project_order_confirmed = True
        client.initialize.return_value = "我准备好了，面试人：测试，面试岗位：开发"
        with patch("loop.DeepSeekClient", return_value=client), patch("builtins.input", side_effect=["/exit"]), redirect_stdout(io.StringIO()):
            self.assertEqual(loop.main(), 0)
        client.detect_projects.assert_not_called()
        client.set_project_order.assert_not_called()
        client.initialize.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
