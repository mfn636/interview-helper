"""读取彼此分开的简历和岗位资料，保留旧单文件显式调用兼容。"""

import re
from pathlib import Path


CONTEXT_PATH = Path(__file__).resolve().parent / "context" / "interview.md"
RESUME_PATH = CONTEXT_PATH.with_name("resume.md")
JD_PATH = CONTEXT_PATH.with_name("job_description.md")


def _read_material(path: str | Path, label: str) -> str:
    path = Path(path)
    if not path.exists():
        raise ValueError(f"缺少{label}：{path}，请先填写该文件。")
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ValueError(f"{label}不能为空：{path}")
    return text


def load_resume(path: str | Path = RESUME_PATH) -> str:
    return _read_material(path, "个人简历")


def load_job_description(path: str | Path = JD_PATH) -> str:
    return _read_material(path, "岗位 JD")


def combine_interview_materials(resume: str, job_description: str) -> str:
    return ("<resume>\n" + resume + "\n</resume>\n\n"
            "<job_description>\n" + job_description + "\n</job_description>")


def split_legacy_interview_materials(text: str) -> tuple[str, str]:
    """只将明确标识的岗位二级章节归入 JD；其余资料保留为个人背景。"""
    headings = list(re.finditer(r"(?m)^##[ \t]+([^\r\n]+)[ \t]*$", text))
    for index, heading in enumerate(headings):
        label = re.sub(r"[ \t]+", "", heading.group(1)).casefold()
        if label in {"岗位", "岗位jd", "岗位描述", "jobdescription", "jd"}:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            resume = (text[:heading.start()].rstrip() + "\n\n" + text[end:].lstrip()).strip()
            return resume, text[heading.start():end].strip()
    raise ValueError("旧面试资料缺少独立的‘## 岗位 JD’章节；请改用分开的简历和岗位文件。")


def load_interview_context(
    path: str | Path | None = None, *,
    resume_path: str | Path = RESUME_PATH,
    jd_path: str | Path = JD_PATH,
) -> str:
    if path is not None:
        return _read_material(path, "面试资料")
    return combine_interview_materials(load_resume(resume_path), load_job_description(jd_path))
