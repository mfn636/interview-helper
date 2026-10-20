"""验证资料直接拼接和多轮问答，不调用真实 API。"""

import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import loop
from deepseek_client import DeepSeekClient
from interview_context import load_interview_context, split_legacy_interview_materials


class InterviewContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.context_path = self.directory / "interview.md"
        self.materials = "# 本次面试\n\n## 个人简历\n候选人做过 FastAPI 项目。\n\n## 岗位 JD\n岗位要求 Python 和 RAG。\n\n## 补充个人背景\n候选人负责接口设计。"
        self.context_path.write_text(self.materials, encoding="utf-8")
        environment = patch.dict(os.environ, {"API_KEY": "mock-test-key"})
        environment.start()
        self.addCleanup(environment.stop)
        post_patch = patch("deepseek_client.requests.post")
        self.post = post_patch.start()
        self.addCleanup(post_patch.stop)
        self.post.return_value.json.return_value = {"choices": [{"message": {"content": "口语回答"}}]}
        self.client = DeepSeekClient(context_path=self.context_path)
        self.post.return_value.json.return_value = {"choices": [{"message": {"content": '{"projects":[]}'}}]}
        self.client.detect_projects()
        self.client.set_project_order([])
        self.post.reset_mock()
        self.post.return_value.json.return_value = {"choices": [{"message": {"content": "口语回答"}}]}

    def test_materials_and_history_are_in_request(self):
        history = [{"role": "user", "content": "上一个问题"}, {"role": "assistant", "content": "上一条回答"}]
        original = [dict(item) for item in history]
        self.assertEqual(self.client.chat("介绍项目", history=history), "口语回答")
        messages = self.post.call_args.kwargs["json"]["messages"]
        self.assertEqual([item["role"] for item in messages], ["system", "user", "assistant", "user"])
        for expected in ("FastAPI", "Python 和 RAG", "负责接口设计", "不能把岗位要求当成候选人已经拥有的经验"):
            self.assertIn(expected, messages[0]["content"])
        self.assertEqual(messages[0]["content"].count(self.materials), 1)
        self.assertEqual(history, original)
        self.assertEqual(messages[-1]["content"], "介绍项目")

    def test_materials_are_loaded_once_at_initialization(self):
        self.client.chat("第一个问题")
        self.context_path.write_text(self.materials.replace("负责接口设计", "负责性能优化"), encoding="utf-8")
        self.client.chat("第二个问题")
        content = self.post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("负责接口设计", content)
        self.assertNotIn("负责性能优化", content)
        DeepSeekClient(context_path=self.context_path).chat("新会话")
        self.assertIn("负责性能优化", self.post.call_args.kwargs["json"]["messages"][0]["content"])

    def test_markdown_and_utf8_bom_are_preserved(self):
        self.context_path.write_text(self.materials, encoding="utf-8-sig")
        self.assertEqual(load_interview_context(self.context_path), self.materials)

    def test_required_materials_fail_before_request(self):
        self.context_path.write_text(" ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            DeepSeekClient(context_path=self.context_path)
        self.context_path.unlink()
        with self.assertRaisesRegex(ValueError, "缺少面试资料"):
            DeepSeekClient(context_path=self.context_path)
        self.post.assert_not_called()

    def test_explicitly_disable_context(self):
        DeepSeekClient(context_path=None).chat("问题", system_prompt="")
        self.assertEqual(self.post.call_args.kwargs["json"]["messages"], [{"role": "user", "content": "问题"}])

    def test_chat_merges_line_breaks_and_preserves_inline_text(self):
        cases = [
            ("  SQL  JOIN 的开销是 O(n)。  ", "SQL  JOIN 的开销是 O(n)。"),
            (" \t核心结论。\r\n\r\n  原理 A  B。\n\t边界 O(n²d)。\r 结束。 \n ",
             "核心结论。 原理 A  B。 边界 O(n²d)。 结束。"),
            ("第一句。\u0085\u2028  第二句。\u2029第三句。", "第一句。 第二句。 第三句。"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=repr(raw)):
                self.post.return_value.json.return_value = {"choices": [{"message": {"content": raw}}]}
                self.assertEqual(self.client.chat("问题"), expected)

    def test_chat_preserves_complete_long_answer(self):
        answer = "关键条件和原理。" * 60
        self.post.return_value.json.return_value = {"choices": [{"message": {"content": answer}}]}
        self.assertGreater(len(answer), 350)
        self.assertEqual(self.client.chat("详细解释"), answer)

    def test_chat_rejects_invalid_text(self):
        for content in (None, "", " \r\n\t\u2028", {"text": "错误的格式"}):
            with self.subTest(content=content):
                self.post.return_value.json.return_value = {"choices": [{"message": {"content": content}}]}
                with self.assertRaisesRegex(ValueError, "有效的文本回复"):
                    self.client.chat("问题")

    def test_loop_uses_normalized_answer_in_output_and_next_turn_history(self):
        self.post.return_value.json.side_effect = [
            {"choices": [{"message": {"content": "结论。\r\n\n  原理。"}}]},
            {"choices": [{"message": {"content": "后续回答。"}}]},
        ]
        output = StringIO()
        with patch("loop.DeepSeekClient", return_value=self.client), patch.object(self.client, "initialize", return_value="我准备好了，面试人：测试人，面试岗位：Python工程师"), patch("builtins.input", side_effect=["问题", "追问", "/exit"]), redirect_stdout(output):
            self.assertEqual(loop.main(), 0)
        self.assertIn("面试助手：结论。 原理。", output.getvalue())
        messages = self.post.call_args_list[1].kwargs["json"]["messages"]
        self.assertEqual(messages[2], {"role": "assistant", "content": "结论。 原理。"})

    def test_loop_clear_keeps_materials_but_clears_history(self):
        with patch("loop.DeepSeekClient", return_value=self.client), patch.object(self.client, "initialize", return_value="我准备好了，面试人：测试人，面试岗位：Python工程师"), patch("builtins.input", side_effect=["一", "二", "/clear", "三", "/exit"]), redirect_stdout(StringIO()):
            self.assertEqual(loop.main(), 0)
        messages = [call.kwargs["json"]["messages"] for call in self.post.call_args_list]
        self.assertEqual([len(items) for items in messages], [2, 4, 2])
        self.assertTrue(all("FastAPI" in items[0]["content"] for items in messages))

    def test_initialization_sends_both_documents_and_caches_real_reply(self):
        ready = "我准备好了，面试人：测试人，面试岗位：Python工程师"
        self.post.return_value.json.return_value = {"choices": [{"message": {"content": ready}}]}
        self.assertEqual(self.client.initialize(), ready)
        self.assertEqual(self.client.initialize(), ready)
        self.assertEqual(self.post.call_count, 1)
        messages = self.post.call_args.kwargs["json"]["messages"]
        self.assertIn("候选人", messages[0]["content"])
        self.assertIn(self.materials, messages[0]["content"])
        self.assertIn("岗位 JD", messages[1]["content"])

    def test_initialization_rejects_unconfirmed_or_placeholder_reply(self):
        for text in (
            "你好", "资料不完整：缺少姓名或岗位", "我准备好了，面试人：xxx，面试岗位：xxx",
            "我准备好了，面试人：   ，面试岗位：Python工程师",
            "我准备好了，面试人：测试人，面试岗位：   ",
        ):
            self.post.return_value.json.return_value = {"choices": [{"message": {"content": text}}]}
            with self.assertRaises(ValueError):
                self.client.initialize()
            self.assertIsNone(self.client.ready_message)

    def test_separate_documents_are_loaded_and_snapshot_once(self):
        resume = self.directory / "resume.md"
        jd = self.directory / "job_description.md"
        resume.write_text("# 简历\n候选人：测试人\n项目：检索服务。\n个人背景：负责接口。", encoding="utf-8-sig")
        jd.write_text("# 岗位 JD\nPython 工程师，需要 RAG。", encoding="utf-8")
        client = DeepSeekClient(resume_path=resume, jd_path=jd)
        self.assertEqual(client.resume_path, resume)
        self.assertEqual(client.jd_path, jd)
        self.assertIsNone(client.context_path)
        resume.write_text("新的简历", encoding="utf-8")
        jd.write_text("新的岗位", encoding="utf-8")
        client.chat("问题")
        system = self.post.call_args.kwargs["json"]["messages"][0]["content"]
        for expected in ("候选人：测试人", "需要 RAG", "个人背景：负责接口", "<resume>", "<job_description>"):
            self.assertIn(expected, system)
        self.assertNotIn("新的简历", system)
        self.assertNotIn("新的岗位", system)

    def test_missing_or_empty_separate_document_fails_before_request(self):
        resume = self.directory / "resume.md"
        jd = self.directory / "job_description.md"
        with self.assertRaisesRegex(ValueError, "缺少个人简历"):
            DeepSeekClient(resume_path=resume, jd_path=jd)
        resume.write_text("简历正文", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "缺少岗位 JD"):
            DeepSeekClient(resume_path=resume, jd_path=jd)
        jd.write_text(" \n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "岗位 JD不能为空"):
            DeepSeekClient(resume_path=resume, jd_path=jd)
        self.post.assert_not_called()

    def test_legacy_split_keeps_background_with_resume(self):
        resume, jd = split_legacy_interview_materials(self.materials)
        self.assertIn("FastAPI", resume)
        self.assertIn("负责接口设计", resume)
        self.assertNotIn("Python 和 RAG", resume)
        self.assertEqual(jd, "## 岗位 JD\n岗位要求 Python 和 RAG。")


if __name__ == "__main__":
    unittest.main()
