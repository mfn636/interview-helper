"""串行测试当前客户端；保留原始回答、完整响应延迟和 API 用量。"""

import argparse
import hashlib
import json
import math
import platform
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests

from deepseek_client import DeepSeekClient
from loop import parse_project_order


ROOT = Path(__file__).resolve().parent


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def material_fingerprints(client: DeepSeekClient) -> dict[str, str]:
    """记录实际使用的资料文件，同时兼容显式传入的旧合并文件。"""
    legacy_path = getattr(client, "context_path", None)
    if legacy_path is not None:
        return {"context": digest(Path(legacy_path))}
    return {
        name: digest(Path(path))
        for name in ("resume", "jd")
        if (path := getattr(client, f"{name}_path", None)) is not None
    }


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def summarize(rows: list[dict]) -> dict:
    successful = [row for row in rows if row["success"]]
    durations = sorted(row["elapsed_seconds"] for row in successful)
    result = {"attempted": len(rows), "successful": len(successful),
              "failed": len(rows) - len(successful)}
    if durations:
        result.update({
            "mean_seconds": statistics.mean(durations),
            "median_seconds": statistics.median(durations),
            "p95_seconds": durations[math.ceil(0.95 * len(durations)) - 1],
            "p95_method": "nearest-rank: sorted[ceil(0.95 * n) - 1]",
            "min_seconds": durations[0], "max_seconds": durations[-1],
            "sum_answer_seconds": sum(durations),
            "mean_answer_characters": statistics.mean(row["answer_characters"] for row in successful),
            "single_line_answers": sum(row["line_breaks"] == 0 for row in successful),
        })
        raw_rows = [row for row in successful if "raw_line_breaks" in row]
        if raw_rows:
            result["raw_format_sample_count"] = len(raw_rows)
            result["raw_single_line_answers"] = sum(row["raw_line_breaks"] == 0 for row in raw_rows)
        usage_rows = [row for row in successful
                      if isinstance(row.get("usage", {}).get("completion_tokens"), int)]
        if usage_rows:
            result["usage_sample_count"] = len(usage_rows)
            result["usage_totals"] = {
                key: sum(row["usage"].get(key, 0) for row in usage_rows)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
                if any(key in row["usage"] for row in usage_rows)
            }
            result["effective_output_tokens_per_second"] = (
                result["usage_totals"]["completion_tokens"]
                / sum(row["elapsed_seconds"] for row in usage_rows)
            )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=ROOT / "benchmarks" / "questions.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate", action="store_true", help="只校验题库，不调用 API")
    parser.add_argument(
        "--project-order", metavar="3,1,2",
        help="显式确认项目熟悉度：按识别列表编号从最熟悉到最不熟悉列出全部项目；无项目填 none",
    )
    args = parser.parse_args()
    suite = json.loads(args.questions.read_text(encoding="utf-8"))
    questions = suite["questions"]
    if len(questions) != 20 or len({q["id"] for q in questions}) != 20:
        raise ValueError("题库必须包含 20 道 ID 不重复的题目。")
    for question in questions:
        for key in ("id", "domain", "question", "expected_points", "critical_errors"):
            if not question.get(key):
                raise ValueError(f"题目字段缺失：{key}")
    if args.validate:
        print("Validated 20 questions; no API calls made.", flush=True)
        return 0
    if args.project_order is None:
        parser.error("实际测试前必须明确项目熟悉度，例如 --project-order '3,1,2'；无项目使用 --project-order none。")
    output = args.output or ROOT / "benchmarks" / "results" / (
        datetime.now().strftime("flash_%Y%m%d_%H%M%S") + ".json"
    )
    if output.exists():
        raise FileExistsError(f"结果文件已存在，请使用新路径：{output}")
    client = DeepSeekClient()
    run = {
        "started_at": now(), "status": "running", "python": platform.python_version(),
        "requests_version": requests.__version__,
        "method": "sequential; fresh question history; same cached prompt and resume/JD as app",
        "response_mode": "non-streaming; elapsed is complete response, not time to first token",
        "sampling": "one response per question; API defaults; no retries; initialization excluded from answer latency",
        "source_sha256": {
            "client": digest(ROOT / "deepseek_client.py"),
            "prompt": digest(ROOT / "prompts" / "interview_assistant.md"),
            **material_fingerprints(client),
            "questions": digest(args.questions),
        },
        "rubric": suite["rubric"], "project_detection": None,
        "project_confirmation": None, "initialization": None, "results": [],
    }
    original_post = requests.post
    observation = {}

    def observed_post(*post_args, **post_kwargs):
        payload = post_kwargs.get("json", {})
        observation.update({
            "requested_model": payload.get("model"), "thinking": payload.get("thinking"),
            "stream": payload.get("stream"),
            "message_count": len(payload.get("messages", [])),
        })
        response = original_post(*post_args, **post_kwargs)
        observation["http_status"] = response.status_code
        try:
            body = response.json()
            if isinstance(body, dict):
                observation["response_model"] = body.get("model")
                observation["usage"] = body.get("usage") or {}
                choices = body.get("choices") or []
                if choices:
                    observation["finish_reason"] = choices[0].get("finish_reason")
                    raw_answer = (choices[0].get("message") or {}).get("content")
                    if isinstance(raw_answer, str):
                        observation["raw_answer"] = raw_answer
                        observation["raw_answer_characters"] = len(raw_answer)
                        observation["raw_line_breaks"] = len(re.findall(
                            r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]", raw_answer
                        ))
        except (ValueError, TypeError, AttributeError):
            pass
        return response

    def measure(action, result_key="answer"):
        observation.clear()
        row = {"started_at": now()}
        started = time.perf_counter()
        try:
            answer = action()
            row.update({"success": True, result_key: answer})
            if isinstance(answer, str):
                row.update({"answer_characters": len(answer),
                            "line_breaks": len(re.findall(r"\r\n|[\r\n]", answer))})
        except Exception as error:
            row.update({"success": False, "error_type": type(error).__name__,
                        "error": str(error).replace(client.api_key, "[REDACTED]")})
        row["elapsed_seconds"] = time.perf_counter() - started
        row.update(observation)
        return row

    save(output, run)
    print(f"Results: {output}", flush=True)
    with patch("deepseek_client.requests.post", new=observed_post):
        print("Detecting resume projects...", flush=True)
        run["project_detection"] = measure(lambda: list(client.detect_projects()), "projects")
        save(output, run)
        if not run["project_detection"]["success"]:
            run.update({"status": "project_detection_failed", "finished_at": now()})
            save(output, run)
            print(json.dumps(run["project_detection"], ensure_ascii=False), flush=True)
            return 2
        projects = tuple(run["project_detection"]["projects"])
        print("Detected projects: " + json.dumps(
            {str(index): name for index, name in enumerate(projects, 1)}, ensure_ascii=False
        ), flush=True)

        def confirm_projects():
            ordered = parse_project_order(args.project_order, projects)
            client.set_project_order(ordered)
            return list(ordered)

        run["project_confirmation"] = measure(confirm_projects, "ordered_projects")
        save(output, run)
        if not run["project_confirmation"]["success"]:
            run.update({"status": "project_confirmation_failed", "finished_at": now()})
            save(output, run)
            print(json.dumps(run["project_confirmation"], ensure_ascii=False), flush=True)
            print("请按上面的项目编号重新指定 --project-order，例如 '3,1,2'；无项目填 none。", flush=True)
            return 2
        print("Initializing current client...", flush=True)
        run["initialization"] = measure(client.initialize)
        save(output, run)
        if not run["initialization"]["success"]:
            run.update({"status": "initialization_failed", "finished_at": now()})
            save(output, run)
            print(json.dumps(run["initialization"], ensure_ascii=False), flush=True)
            return 2
        print(f"Initialization OK ({run['initialization']['elapsed_seconds']:.2f}s)", flush=True)
        for index, question in enumerate(questions, 1):
            print(f"[{index:02d}/20] {question['id']} {question['domain']}...", flush=True)
            result = dict(question)
            result.update(measure(lambda: client.chat(question["question"])))
            run["results"].append(result)
            run["summary"] = summarize(run["results"])
            save(output, run)
            state = "OK" if result["success"] else result["error_type"]
            print(f"  {state} {result['elapsed_seconds']:.2f}s; "
                  f"{result.get('answer_characters', 0)} characters", flush=True)
    run.update({"status": "completed", "finished_at": now()})
    save(output, run)
    print(json.dumps(run["summary"], ensure_ascii=False, indent=2), flush=True)
    return 0 if run["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
