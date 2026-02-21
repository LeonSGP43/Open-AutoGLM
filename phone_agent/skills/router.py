"""Task skill routing for dynamic, domain-specific execution guidance."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class TaskSkill:
    """Definition for a task skill."""

    name: str
    description: str
    required_keywords: tuple[str, ...]
    optional_keywords: tuple[str, ...]
    prompt_files: dict[str, str]

    def matches(self, task: str) -> bool:
        """Return True if this skill should be activated for the task."""
        normalized = task.lower()
        if not all(keyword.lower() in normalized for keyword in self.required_keywords):
            return False
        if not self.optional_keywords:
            return True
        return any(keyword.lower() in normalized for keyword in self.optional_keywords)


_SKILL_LIBRARY_DIR = Path(__file__).resolve().parent / "library"

TASK_SKILLS: tuple[TaskSkill, ...] = (
    TaskSkill(
        name="wechat-home-normalization",
        description="Normalize WeChat tasks to the default home tab before task actions.",
        required_keywords=(),
        optional_keywords=("微信", "wechat"),
        prompt_files={"cn": "wechat-home-normalization/cn.md", "en": "wechat-home-normalization/en.md"},
    ),
    TaskSkill(
        name="wechat-public-article",
        description=(
            "WeChat public-account article workflow with ad filtering, "
            "long-article scrolling, and extraction/save counting."
        ),
        required_keywords=("公众号",),
        optional_keywords=("文章", "图文", "提取", "保存", "总结", "导出"),
        prompt_files={"cn": "wechat-public-article/cn.md", "en": "wechat-public-article/en.md"},
    ),
)


@lru_cache(maxsize=32)
def _read_skill_prompt(relative_path: str) -> str:
    """Read a skill prompt file from the local skill library."""
    prompt_path = _SKILL_LIBRARY_DIR / relative_path
    if not prompt_path.exists():
        return ""
    return prompt_path.read_text(encoding="utf-8").strip()


def resolve_task_skills(task: str) -> list[TaskSkill]:
    """Resolve all matching task skills for the given task."""
    if not task:
        return []
    return [skill for skill in TASK_SKILLS if skill.matches(task)]


def build_task_skill_prompt(task: str, lang: str = "cn") -> tuple[str, list[str]]:
    """
    Build the runtime prompt suffix for all matched task skills.

    Returns:
        A tuple of (combined_prompt, activated_skill_names).
    """
    matched = resolve_task_skills(task)
    if not matched:
        return "", []

    language = "en" if lang == "en" else "cn"
    blocks: list[str] = []
    names: list[str] = []

    for skill in matched:
        rel_path = skill.prompt_files.get(language) or skill.prompt_files.get("cn", "")
        if not rel_path:
            continue
        prompt = _read_skill_prompt(rel_path)
        if not prompt:
            continue
        names.append(skill.name)
        blocks.append(prompt)

    return "\n\n".join(blocks).strip(), names
