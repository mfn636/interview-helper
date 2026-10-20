"""项目抽取和熟悉度约束的离线回归；不调用真实 API。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from deepseek_client import DeepSeekClient


class ProjectPreferenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.resume = self.directory / "resume.md"
        self.jd = self.directory / "jd.md"
        self.resume.write_text(
            "# 个人简历\n姓名：测试人\n## 项目经历\n### 知识检索平台\n负责向量检索接口。\n"
            "### 订单管理系统\n负责订单API。\n### 数据分析工具\n负责清洗流程。\n", encoding="utf-8")
        self.jd.write_text("# 岗位\nPython工程师\n需建设智能推荐项目。", encoding="utf-8")
        environment = patch.dict(os.environ, {"API_KEY": "mock-test-key"})
        environment.start()
        self.addCleanup(environment.stop)
        post_patch = patch("deepseek_client.requests.post")
        self.post = post_patch.start()
        self.addCleanup(post_patch.stop)
        self.client = DeepSeekClient(resume_path=self.resume, jd_path=self.jd)
        self.names = ("知识检索平台", "订单管理系统", "数据分析工具")
        self.projects = [
            {"name": self.names[0], "evidence": "知识检索平台\n负责向量检索接口。"},
            {"name": self.names[1], "evidence": "订单管理系统\n负责订单API。"},
            {"name": self.names[2], "evidence": "数据分析工具\n负责清洗流程。"},
        ]
        self.reply(json.dumps({"projects": self.projects}, ensure_ascii=False))

    def reply(self, text):
        self.post.return_value.json.return_value = {"choices": [{"message": {"content": text}}]}

    def confirm(self):
        self.client.detect_projects()
        self.client.set_project_order(self.names[::-1])

    def test_detection_uses_only_resume_and_caches_success(self):
        self.assertIsNone(self.client.projects)
        self.assertFalse(self.client.project_order_confirmed)
        self.assertEqual(self.client.detect_projects(), self.names)
        self.assertEqual(self.client.detect_projects(), self.names)
        self.assertEqual(self.client.projects, self.names)
        self.post.assert_called_once()
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        messages = payload["messages"]
        combined = "\n".join(message["content"] for message in messages)
        self.assertIn("订单管理系统", combined)
        self.assertNotIn("智能推荐项目", combined)
        self.assertNotIn(self.client.system_prompt, combined)

    def test_initialize_requires_confirmation_before_ready_request(self):
        with self.assertRaisesRegex(ValueError, "先按熟悉程度"):
            self.client.initialize()
        self.assertIsNone(self.client.ready_message)
        self.assertEqual(self.client.projects, self.names)
        self.assertFalse(self.client.project_order_confirmed)
        self.post.assert_called_once()
        self.client.set_project_order(self.names[::-1])
        ready = "我准备好了，面试人：测试人，面试岗位：Python工程师"
        self.reply(ready)
        self.assertEqual(self.client.initialize(), ready)
        self.assertEqual(self.client.initialize(), ready)
        self.assertEqual(self.post.call_count, 2)
        self.assertIn("数据分析工具", self.post.call_args.kwargs["json"]["messages"][0]["content"])

    def test_zero_projects_still_needs_explicit_confirmation(self):
        self.reply('{"projects":[]}')
        with self.assertRaisesRegex(ValueError, "显式确认"):
            self.client.initialize()
        self.assertEqual(self.client.projects, ())
        self.assertFalse(self.client.project_order_confirmed)
        self.client.set_project_order([])
        self.assertTrue(self.client.project_order_confirmed)
        self.assertEqual(self.client.project_order, ())

    def test_invalid_detection_never_becomes_empty_success(self):
        invalid_results = [
            "不是JSON", "```json\n{\"projects\":[]}\n```", "null", "[]", "{}",
            '{"projects":null}', '{"projects":[],"error":"无法判断"}',
            json.dumps({"projects": [{"name": "智能推荐项目", "evidence": "需建设智能推荐项目。"}]}),
            json.dumps({"projects": [{"name": "知识检索平台", "evidence": "知识检索平台节省成本90%"}]}),
            json.dumps({"projects": [{"name": "知识检索平台", "evidence": "负责订单API。"}]}),
            json.dumps({"projects": [{"name": "", "evidence": ""}]}),
            json.dumps({"projects": [{"name": "知识检索平台"}]}),
            json.dumps({"projects": [self.projects[0], self.projects[0]]}),
        ]
        for content in invalid_results:
            with self.subTest(content=content):
                self.reply(content)
                with self.assertRaises(ValueError):
                    self.client.detect_projects()
                self.assertIsNone(self.client.projects)
                self.assertFalse(self.client.project_order_confirmed)
        self.reply(json.dumps({"projects": self.projects}))
        self.assertEqual(self.client.detect_projects(), self.names)

    def test_network_failure_can_be_retried_without_confirming_zero_projects(self):
        self.post.side_effect = requests.ConnectionError("offline")
        with self.assertRaises(requests.ConnectionError):
            self.client.detect_projects()
        self.assertIsNone(self.client.projects)
        self.post.side_effect = None
        self.assertEqual(self.client.detect_projects(), self.names)

    def test_missing_resume_is_not_zero_project_success(self):
        client = DeepSeekClient(context_path=None)
        with self.assertRaisesRegex(ValueError, "个人简历"):
            client.detect_projects()
        self.assertIsNone(client.projects)
        self.post.assert_not_called()

    def test_order_rejects_missing_duplicate_foreign_or_unidentified_projects(self):
        with self.assertRaisesRegex(ValueError, "先完成项目识别"):
            self.client.set_project_order(self.names)
        self.client.detect_projects()
        for bad in ([], self.names[:2], [self.names[0]] * 3, [*self.names[:2], "未知项目"],
                    self.names[0], None, [self.names[0], self.names[1], []]):
            with self.subTest(order=bad):
                with self.assertRaises(ValueError):
                    self.client.set_project_order(bad)
                self.assertFalse(self.client.project_order_confirmed)
        self.client.set_project_order(self.names[::-1])
        self.assertEqual(self.client.project_order, self.names[::-1])

    def test_every_turn_has_order_and_question_specific_constraints(self):
        self.confirm()
        self.reply("口语答案")
        history = [{"role": "user", "content": "介绍订单管理系统"}, {"role": "assistant", "content": "订单项目的背景"}]
        snapshot = [dict(item) for item in history]
        for question in ("介绍一个项目", "订单管理系统用了什么数据库", "它为什么这样设计", "什么是事务隔离"):
            self.client.chat(question, history=history)
            messages = self.post.call_args.kwargs["json"]["messages"]
            system = messages[0]["content"]
            self.assertIn(json.dumps(list(self.names[::-1]), ensure_ascii=False), system)
            for constraint in ("与题目相关", "延续前文追问", "不得将A项目的经历挪给B项目", "不得编造", "不代表客观能力", "纯技术八股"):
                self.assertIn(constraint, system)
            self.assertEqual(messages[-1]["content"], question)
            self.assertEqual(history, snapshot)

    def test_changing_confirmed_order_invalidates_ready_cache(self):
        self.confirm()
        self.reply("我准备好了，面试人：测试人，面试岗位：Python工程师")
        self.client.initialize()
        self.client.set_project_order(self.names[::-1])
        self.assertIsNotNone(self.client.ready_message)
        self.client.set_project_order(self.names)
        self.assertIsNone(self.client.ready_message)
        self.assertTrue(self.client.project_order_confirmed)

    def test_legacy_detection_excludes_jd_and_retains_personal_background(self):
        legacy = self.directory / "interview.md"
        legacy.write_text("## 个人简历\n知识检索平台\n负责向量检索接口。\n\n## 岗位 JD\n智能推荐项目\n\n## 补充个人背景\n负责接口设计。", encoding="utf-8")
        client = DeepSeekClient(context_path=legacy)
        self.reply(json.dumps({"projects": [self.projects[0]]}))
        self.assertEqual(client.detect_projects(), (self.names[0],))
        content = self.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("负责接口设计", content)
        self.assertNotIn("智能推荐项目", content)


if __name__ == "__main__":
    unittest.main()
