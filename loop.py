"""交互式多轮面试对话。历史仅保存在内存中，退出后不保留。"""

import sys
import re

import requests

from deepseek_client import DeepSeekClient


def parse_project_order(raw: str, projects: tuple[str, ...]) -> tuple[str, ...]:
    """把用户明确提供的完整编号排序转换为项目名称。"""
    if not projects:
        if raw.strip().lower() in {"none", "无项目"}:
            return ()
        raise ValueError("没有识别到项目；请输入 none 明确确认本次无项目。")
    tokens = re.split(r"[\s,，]+", raw.strip())
    if not tokens or any(not token.isascii() or not token.isdecimal() for token in tokens):
        raise ValueError("请输入完整的项目编号，用空格或逗号分隔。")
    numbers = [int(token) for token in tokens]
    if len(numbers) != len(projects) or set(numbers) != set(range(1, len(projects) + 1)):
        raise ValueError("请按熟悉程度列出全部编号，每个编号只能出现一次。")
    return tuple(projects[number - 1] for number in numbers)


def confirm_project_order(client: DeepSeekClient) -> bool:
    """准备好之前由用户确认项目熟悉度；退出不写入偏好。"""
    print("正在识别简历中的项目……")
    projects = client.detect_projects()
    if projects:
        print("已识别到以下项目：")
        for number, project in enumerate(projects, 1):
            print(f"  {number}. {project}")
        if len(projects) == 1:
            prompt = "输入 1 确认使用这个项目，或 /exit 退出检查简历："
        else:
            prompt = "请按最熟悉到最不熟悉输入全部编号（例如 3 1 2），或 /exit 退出："
    else:
        print("没有识别到项目，请检查简历。若确实没有项目，可以明确确认后继续。")
        prompt = "输入 none 确认本次无项目，或 /exit 退出后修改简历："
    while True:
        raw = input(prompt).strip()
        if raw.lower() in {"/exit", "exit", "quit"}:
            return False
        try:
            ordered = parse_project_order(raw, projects)
            client.set_project_order(ordered)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            continue
        if ordered:
            print("本次项目熟悉度：" + " → ".join(ordered))
        else:
            print("已确认本次无项目。")
        return True


def main() -> int:
    try:
        client = DeepSeekClient()
        if not client.project_order_confirmed and not confirm_project_order(client):
            print("对话已结束。")
            return 0
        ready_message = client.initialize()
    except (EOFError, KeyboardInterrupt):
        print("\n对话已结束。")
        return 0
    except (OSError, ValueError, requests.RequestException) as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1

    history: list[dict[str, str]] = []
    print(ready_message)
    print("输入 /clear 开始新对话，输入 /exit 退出。")

    try:
        while True:
            prompt = input("\n你：").strip()
            if not prompt:
                continue
            if prompt.lower() in {"/exit", "exit", "quit"}:
                break
            if prompt.lower() == "/clear":
                history.clear()
                print("对话历史已清空，继续使用面试资料和已确认的项目熟悉度顺序。")
                continue

            try:
                reply = client.chat(prompt, history=history)
            except (OSError, ValueError, requests.RequestException) as exc:
                print(f"调用失败：{exc}\n本轮未记入历史，可以重新输入。", file=sys.stderr)
                continue

            history.extend([
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": reply},
            ])
            print(f"\n面试助手：{reply}")
    except (EOFError, KeyboardInterrupt):
        pass

    print("\n对话已结束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
