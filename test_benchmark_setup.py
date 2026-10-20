"""基准测试必须先明确项目排序；全部测试都隔离网络。"""

import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import benchmark


class BenchmarkSetupTests(unittest.TestCase):
    def test_validation_does_not_construct_client(self):
        with patch("sys.argv", ["benchmark.py", "--validate"]), patch("benchmark.DeepSeekClient") as factory, redirect_stdout(io.StringIO()):
            self.assertEqual(benchmark.main(), 0)
        factory.assert_not_called()

    def test_missing_project_order_fails_before_any_api(self):
        with patch("sys.argv", ["benchmark.py"]), patch("benchmark.DeepSeekClient") as factory, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                benchmark.main()
        self.assertEqual(error.exception.code, 2)
        factory.assert_not_called()

    def test_fingerprints_follow_split_or_legacy_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            resume, jd, legacy = [Path(directory) / name for name in ("resume.md", "jd.md", "interview.md")]
            for path in (resume, jd, legacy):
                path.write_text(path.stem, encoding="utf-8")
            client = SimpleNamespace(context_path=None, resume_path=resume, jd_path=jd)
            self.assertEqual(benchmark.material_fingerprints(client), {
                "resume": benchmark.digest(resume), "jd": benchmark.digest(jd),
            })
            client.context_path = legacy
            self.assertEqual(benchmark.material_fingerprints(client), {"context": benchmark.digest(legacy)})

    def run_benchmark(self, directory, projects, order):
        output = Path(directory) / "result.json"
        client = Mock()
        client.context_path = None
        client.resume_path = None
        client.jd_path = None
        client.api_key = "fake-test-key"
        client.detect_projects.return_value = projects
        client.initialize.return_value = "我准备好了，面试人：测试，面试岗位：开发"
        client.chat.return_value = "测试回答"
        with patch("sys.argv", ["benchmark.py", "--output", str(output), "--project-order", order]), patch(
            "benchmark.DeepSeekClient", return_value=client
        ), patch("requests.post") as network, redirect_stdout(io.StringIO()):
            code = benchmark.main()
        network.assert_not_called()
        return code, client, json.loads(output.read_text(encoding="utf-8"))

    def test_detection_order_and_ready_measured_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            code, client, result = self.run_benchmark(directory, ("甲", "乙", "丙"), "3,1,2")
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in client.mock_calls[:3]], ["detect_projects", "set_project_order", "initialize"])
        client.set_project_order.assert_called_once_with(("丙", "甲", "乙"))
        self.assertEqual(result["project_detection"]["projects"], ["甲", "乙", "丙"])
        self.assertEqual(result["project_confirmation"]["ordered_projects"], ["丙", "甲", "乙"])
        for phase in ("project_detection", "project_confirmation", "initialization"):
            self.assertTrue(result[phase]["success"])
            self.assertGreaterEqual(result[phase]["elapsed_seconds"], 0)
        self.assertEqual(len(result["results"]), 20)

    def test_invalid_order_never_initializes_or_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            code, client, result = self.run_benchmark(directory, ("甲", "乙"), "1,1")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "project_confirmation_failed")
        client.set_project_order.assert_not_called()
        client.initialize.assert_not_called()
        client.chat.assert_not_called()

    def test_empty_detection_still_requires_none(self):
        with tempfile.TemporaryDirectory() as directory:
            code, client, result = self.run_benchmark(directory, (), "none")
        self.assertEqual(code, 0)
        client.set_project_order.assert_called_once_with(())
        self.assertTrue(result["project_confirmation"]["success"])


if __name__ == "__main__":
    unittest.main()
