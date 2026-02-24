"""Task skill routing for dynamic, domain-specific execution guidance."""

from __future__ import annotations

import json
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
        name="orientation-lock-normalization",
        description=(
            "Preflight orientation normalization: ensure portrait lock is enabled "
            "before any task actions."
        ),
        required_keywords=(),
        optional_keywords=(),
        prompt_files={
            "cn": "orientation-lock-normalization/cn.md",
            "en": "orientation-lock-normalization/en.md",
        },
    ),
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
    TaskSkill(
        name="x-post-collection",
        description=(
            "X/Twitter post workflow with mixed-media handling, "
            "structured capture, and export protocol."
        ),
        required_keywords=("x",),
        optional_keywords=(
            "twitter",
            "tweet",
            "post",
            "musk",
            "推文",
            "帖子",
            "评论",
            "热度",
            "媒体",
            "导出",
            "保存",
            "采集",
        ),
        prompt_files={"cn": "x-post-collection/cn.md", "en": "x-post-collection/en.md"},
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


def _build_x_learning_prompt(lang: str = "cn", limit: int = 5) -> str:
    """
    Build dynamic learned-experience prompt block for X extraction tasks.

    This converts persisted rules in artifacts/x_extract/x_learning_rules.json
    into concise instructions that can be consumed by the planner.
    """
    repo_root = Path(__file__).resolve().parents[2]
    rules_path = repo_root / "artifacts" / "x_extract" / "x_learning_rules.json"
    if not rules_path.exists():
        return ""

    try:
        data = json.loads(rules_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        return ""

    top = sorted(
        [item for item in rules if isinstance(item, dict)],
        key=lambda row: (int(row.get("seen", 0) or 0), float(row.get("success_rate", 0.0))),
        reverse=True,
    )[: max(1, limit)]

    if lang == "en":
        lines = ["[Learned Experience: x-post-collection]"]
        for item in top:
            scenario = str(item.get("scenario", "") or "")
            seen = int(item.get("seen", 0) or 0)
            success_rate = float(item.get("success_rate", 0.0))
            missing = ", ".join(item.get("missing_fields", []) or []) or "none"
            advice = str(item.get("advice", "") or "")
            lines.append(
                f"- scenario={scenario}; seen={seen}; success_rate={success_rate:.2f}; "
                f"missing={missing}; advice={advice}"
            )
        lines.append("Use these learned patterns first, then fallback to generic flow.")
        return "\n".join(lines).strip()

    lines = ["[Learned Experience: x-post-collection]"]
    for item in top:
        scenario = str(item.get("scenario", "") or "")
        seen = int(item.get("seen", 0) or 0)
        success_rate = float(item.get("success_rate", 0.0))
        missing = "、".join(item.get("missing_fields", []) or []) or "无"
        advice = str(item.get("advice", "") or "")
        lines.append(
            f"- 场景={scenario}；样本={seen}；成功率={success_rate:.2f}；"
            f"常缺字段={missing}；建议={advice}"
        )
    lines.append("优先使用以上已学习策略，再执行通用流程。")
    return "\n".join(lines).strip()


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

    if "x-post-collection" in names:
        learned = _build_x_learning_prompt(language)
        if learned:
            blocks.append(learned)

    return "\n\n".join(blocks).strip(), names
