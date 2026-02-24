"""X/Twitter post extraction helper for mixed media posts and comments."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from openai import OpenAI

from phone_agent.device_factory import DeviceType, get_device_factory

DEFAULT_HTTP_USER_AGENT = "Open-AutoGLM/0.1"


@dataclass
class XPostCapture:
    """One captured X post page."""

    note: str
    post_index: int | None = None
    stage: str = "unknown"
    author: str = ""
    handle: str = ""
    post_text: str = ""
    post_time: str = ""
    media_types: list[str] = field(default_factory=list)
    is_repost: bool = False
    is_quote: bool = False
    is_reply: bool = False
    heat: dict[str, str | None] = field(
        default_factory=lambda: {
            "replies": None,
            "reposts": None,
            "likes": None,
            "bookmarks": None,
            "views": None,
        }
    )
    top_comments: list[dict[str, Any]] = field(default_factory=list)
    is_duplicate: bool = False
    page_hash: str = ""


def _build_anthropic_messages_url(base_url: str) -> str:
    """Build Anthropic /v1/messages endpoint from a base URL."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return normalized + "/messages"
    return normalized + "/v1/messages"


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract first JSON object from arbitrary model text."""
    if not text:
        return {}
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    candidate = cleaned[start : end + 1]
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    except Exception:
        return {}
    return {}


class XPostExtractor:
    """Collect and export structured post data from X screenshots."""

    def __init__(self) -> None:
        self.base_url = os.getenv("PHONE_AGENT_BASE_URL", "http://localhost:8000/v1")
        self.model_name = os.getenv("PHONE_AGENT_MODEL", "autoglm-phone-9b")
        self.api_key = os.getenv("PHONE_AGENT_API_KEY", "EMPTY")
        self.provider = os.getenv("PHONE_AGENT_PROVIDER", "openai").strip().lower()
        self.anthropic_version = os.getenv("PHONE_AGENT_ANTHROPIC_VERSION", "2023-06-01")
        self._captures: list[XPostCapture] = []
        self._seen_hashes: set[str] = set()

    @staticmethod
    def _normalize_for_hash(text: str) -> str:
        compact = re.sub(r"\s+", "", text or "")
        return compact[:12000]

    def _hash_capture(
        self,
        post_index: int | None,
        author: str,
        handle: str,
        post_text: str,
        post_time: str,
        media_types: list[str],
        heat: dict[str, str | None],
        top_comments: list[dict[str, Any]],
    ) -> str:
        payload = json.dumps(
            {
                "post_index": post_index,
                "author": author,
                "handle": handle,
                "post_text": post_text,
                "post_time": post_time,
                "media_types": media_types,
                "heat": heat,
                "top_comments": top_comments,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        normalized = self._normalize_for_hash(payload)
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_post_index(note: str, parsed_index: Any) -> int | None:
        if parsed_index is not None:
            try:
                idx = int(parsed_index)
                if idx > 0:
                    return idx
            except (TypeError, ValueError):
                pass
        note_text = str(note or "")
        patterns = [
            r"(?:post[_\s-]*index|idx|index|post)\s*[:=]?\s*(\d+)",
            r"第\s*(\d+)\s*条",
        ]
        for pattern in patterns:
            match = re.search(pattern, note_text, flags=re.IGNORECASE)
            if not match:
                continue
            try:
                idx = int(match.group(1))
                if idx > 0:
                    return idx
            except (TypeError, ValueError):
                continue
        return None

    def _analyze_with_openai(self, image_base64: str, prompt: str) -> str:
        client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=90.0)
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_base64}"},
                        },
                    ],
                }
            ],
            temperature=0.0,
            max_tokens=1200,
        )
        return (response.choices[0].message.content or "").strip()

    def _analyze_with_anthropic(self, image_base64: str, prompt: str) -> str:
        endpoint = _build_anthropic_messages_url(self.base_url)
        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": 1200,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_base64,
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.0,
        }
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": self.anthropic_version,
                "user-agent": DEFAULT_HTTP_USER_AGENT,
            },
        )
        with urlopen(request, timeout=90.0) as response:
            data = json.loads(response.read().decode("utf-8"))
        blocks = data.get("content", [])
        texts: list[str] = []
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") in {"text", "thinking"}:
                    text = block.get("text")
                    if isinstance(text, str):
                        texts.append(text)
        return "".join(texts).strip()

    def _analyze_page(self, image_base64: str, note: str) -> dict[str, Any]:
        prompt = (
            "You extract structured data from an X/Twitter screenshot. "
            "Return strict JSON only with keys: post_index, stage, author, handle, post_text, "
            "post_time, media_types, is_repost, is_quote, is_reply, heat, top_comments. "
            "heat must include keys replies,reposts,likes,bookmarks,views. "
            "top_comments must be an array (max 3) where each item has: rank, comment_author, "
            "comment_handle, comment_text, comment_heat. "
            "comment_heat should be null/string or an object with keys "
            "replies,reposts,likes,bookmarks,views. "
            "If a field is not visible, use null or empty string/array. "
            f"Operator note: {note}"
        )

        raw = ""
        try:
            if self.provider == "anthropic":
                raw = self._analyze_with_anthropic(image_base64, prompt)
            else:
                raw = self._analyze_with_openai(image_base64, prompt)
            parsed = _extract_json_object(raw)
            if parsed:
                return parsed
        except (HTTPError, URLError, RuntimeError, ValueError, Exception) as exc:
            print(f"[x-ocr] analyze failed: {exc}")
            return {}

        fallback_text = raw.strip()
        if fallback_text:
            recovered = _extract_json_object(fallback_text)
            if recovered:
                return recovered
            print(f"[x-ocr] non-json fallback text captured, len={len(fallback_text)}")
            return {
                "post_index": self._parse_post_index(note, None),
                "stage": "raw_fallback",
                "author": "",
                "handle": "",
                "post_text": fallback_text[:3000],
                "post_time": "",
                "media_types": [],
                "is_repost": False,
                "is_quote": False,
                "is_reply": False,
                "heat": {
                    "replies": None,
                    "reposts": None,
                    "likes": None,
                    "bookmarks": None,
                    "views": None,
                },
                "top_comments": [],
            }
        else:
            print("[x-ocr] empty response from OCR model")
        return {}

    @staticmethod
    def _normalize_media_types(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        allowed = {"text", "image", "video", "gif", "link", "poll", "quote", "repost", "audio"}
        out: list[str] = []
        seen: set[str] = set()
        for item in values:
            val = str(item or "").strip().lower()
            if not val:
                continue
            if val not in allowed:
                val = "text" if "text" in val else "unknown"
            if val in seen:
                continue
            seen.add(val)
            out.append(val)
        return out

    @staticmethod
    def _normalize_heat(values: Any) -> dict[str, str | None]:
        defaults: dict[str, str | None] = {
            "replies": None,
            "reposts": None,
            "likes": None,
            "bookmarks": None,
            "views": None,
        }
        if not isinstance(values, dict):
            return defaults
        out = defaults.copy()
        for key in defaults:
            raw = values.get(key)
            text = str(raw).strip() if raw is not None else ""
            out[key] = text or None
        return out

    @staticmethod
    def _normalize_comments(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            return []
        normalized: list[dict[str, Any]] = []
        for i, item in enumerate(values[:3], start=1):
            if not isinstance(item, dict):
                continue
            rank_raw = item.get("rank")
            try:
                rank = int(rank_raw)
            except (TypeError, ValueError):
                rank = i
            normalized.append(
                {
                    "rank": max(1, min(3, rank)),
                    "comment_author": str(item.get("comment_author", "") or "").strip() or None,
                    "comment_handle": str(item.get("comment_handle", "") or "").strip() or None,
                    "comment_text": str(item.get("comment_text", "") or "").strip() or None,
                    "comment_heat": XPostExtractor._normalize_comment_heat(item.get("comment_heat")),
                }
            )
        return normalized

    @staticmethod
    def _normalize_comment_heat(value: Any) -> dict[str, str | None] | str | None:
        """Normalize comment heat into structured dict when possible."""
        keys = ("replies", "reposts", "likes", "bookmarks", "views")

        def _normalize_dict(raw: Any) -> dict[str, str | None] | None:
            if not isinstance(raw, dict):
                return None
            out: dict[str, str | None] = {}
            for key in keys:
                item = raw.get(key)
                text = str(item).strip() if item is not None else ""
                out[key] = text or None
            return out if any(out.values()) else None

        direct = _normalize_dict(value)
        if direct is not None:
            return direct

        text = str(value or "").strip()
        if not text:
            return None

        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(text)
            except Exception:
                continue
            parsed_dict = _normalize_dict(parsed)
            if parsed_dict is not None:
                return parsed_dict

        return text

    def capture(self, note: str, device_id: str | None = None) -> XPostCapture:
        """Capture current page and append to extraction memory."""
        screenshot = get_device_factory().get_screenshot(device_id)
        parsed = self._analyze_page(screenshot.base64_data, note)

        post_index = self._parse_post_index(note, parsed.get("post_index"))
        stage = str(parsed.get("stage", "") or "").strip().lower() or "unknown"
        author = str(parsed.get("author", "") or "").strip()
        handle = str(parsed.get("handle", "") or "").strip()
        post_text = str(parsed.get("post_text", "") or "").strip()
        post_time = str(parsed.get("post_time", "") or "").strip()
        media_types = self._normalize_media_types(parsed.get("media_types"))
        if not media_types and post_text:
            media_types = ["text"]
        is_repost = bool(parsed.get("is_repost", False))
        is_quote = bool(parsed.get("is_quote", False))
        is_reply = bool(parsed.get("is_reply", False))
        heat = self._normalize_heat(parsed.get("heat"))
        top_comments = self._normalize_comments(parsed.get("top_comments"))

        page_hash = self._hash_capture(
            post_index=post_index,
            author=author,
            handle=handle,
            post_text=post_text,
            post_time=post_time,
            media_types=media_types,
            heat=heat,
            top_comments=top_comments,
        )
        is_duplicate = page_hash in self._seen_hashes or not any(
            [
                author,
                handle,
                post_text,
                post_time,
                any(heat.values()),
                top_comments,
            ]
        )
        if not is_duplicate:
            self._seen_hashes.add(page_hash)

        capture = XPostCapture(
            note=note,
            post_index=post_index,
            stage=stage,
            author=author,
            handle=handle,
            post_text=post_text,
            post_time=post_time,
            media_types=media_types,
            is_repost=is_repost,
            is_quote=is_quote,
            is_reply=is_reply,
            heat=heat,
            top_comments=top_comments,
            is_duplicate=is_duplicate,
            page_hash=page_hash,
        )
        self._captures.append(capture)
        return capture

    @staticmethod
    def _pick_longer(a: str, b: str) -> str:
        if not a:
            return b or ""
        if not b:
            return a
        return b if len(b) > len(a) else a

    def _merge_posts(self) -> dict[str, Any]:
        valid = [item for item in self._captures if not item.is_duplicate]
        if not valid:
            valid = self._captures[:]

        groups: dict[int, list[XPostCapture]] = {}
        fallback_index = 1
        for item in valid:
            idx = item.post_index
            if idx is None:
                while fallback_index in groups:
                    fallback_index += 1
                idx = fallback_index
                fallback_index += 1
            groups.setdefault(idx, []).append(item)

        merged_posts: list[dict[str, Any]] = []
        for idx in sorted(groups.keys()):
            captures = groups[idx]
            author = ""
            handle = ""
            post_text = ""
            post_time = ""
            media_types: list[str] = []
            media_seen: set[str] = set()
            is_repost = False
            is_quote = False
            is_reply = False
            heat: dict[str, str | None] = {
                "replies": None,
                "reposts": None,
                "likes": None,
                "bookmarks": None,
                "views": None,
            }
            comments_by_rank: dict[int, dict[str, Any]] = {}

            for cap in captures:
                author = self._pick_longer(author, cap.author)
                handle = self._pick_longer(handle, cap.handle)
                post_text = self._pick_longer(post_text, cap.post_text)
                post_time = self._pick_longer(post_time, cap.post_time)
                is_repost = is_repost or cap.is_repost
                is_quote = is_quote or cap.is_quote
                is_reply = is_reply or cap.is_reply

                for media in cap.media_types:
                    if media in media_seen:
                        continue
                    media_seen.add(media)
                    media_types.append(media)

                for key in heat:
                    value = cap.heat.get(key)
                    if value:
                        heat[key] = value

                for item in cap.top_comments:
                    rank_raw = item.get("rank")
                    try:
                        rank = int(rank_raw)
                    except (TypeError, ValueError):
                        continue
                    if rank < 1 or rank > 3:
                        continue
                    current = comments_by_rank.get(rank)
                    if current is None:
                        comments_by_rank[rank] = item
                        continue
                    current_text = str(current.get("comment_text") or "")
                    new_text = str(item.get("comment_text") or "")
                    if len(new_text) > len(current_text):
                        comments_by_rank[rank] = item

            top_comments: list[dict[str, Any] | None] = []
            for rank in (1, 2, 3):
                item = comments_by_rank.get(rank)
                if not item:
                    top_comments.append(None)
                    continue
                top_comments.append(
                    {
                        "comment_author": item.get("comment_author"),
                        "comment_handle": item.get("comment_handle"),
                        "comment_text": item.get("comment_text"),
                        "comment_heat": item.get("comment_heat"),
                    }
                )

            merged_posts.append(
                {
                    "post_index": idx,
                    "author": author or None,
                    "handle": handle or None,
                    "post_text": post_text or None,
                    "post_time": post_time or None,
                    "media_types": media_types,
                    "is_repost": is_repost,
                    "is_quote": is_quote,
                    "is_reply": is_reply,
                    "heat": heat,
                    "top_comments": top_comments,
                }
            )

        return {
            "posts": merged_posts,
            "captures": [item.__dict__ for item in self._captures],
            "capture_count": len(self._captures),
            "unique_capture_count": len(valid),
        }

    @staticmethod
    def _format_comment_heat(value: Any) -> str:
        if isinstance(value, dict):
            parts: list[str] = []
            for key in ("replies", "reposts", "likes", "bookmarks", "views"):
                item = value.get(key)
                if item:
                    parts.append(f"{key}={item}")
            return ", ".join(parts) if parts else "null"
        text = str(value or "").strip()
        return text or "null"

    @staticmethod
    def _write_markdown(payload: dict[str, Any], md_path: Path) -> None:
        lines = [
            "# X Post Extract",
            "",
            f"- Capture Count: {payload.get('capture_count', 0)}",
            f"- Unique Captures: {payload.get('unique_capture_count', 0)}",
            "",
        ]
        posts = payload.get("posts") or []
        if not posts:
            lines.append("(no posts)")
        else:
            for post in posts:
                lines.extend(
                    [
                        f"## Post {post.get('post_index')}",
                        "",
                        f"- Author: {post.get('author') or ''}",
                        f"- Handle: {post.get('handle') or ''}",
                        f"- Time: {post.get('post_time') or ''}",
                        f"- Media: {', '.join(post.get('media_types') or [])}",
                        f"- Repost: {post.get('is_repost')}",
                        f"- Quote: {post.get('is_quote')}",
                        f"- Reply: {post.get('is_reply')}",
                        "",
                        "### Text",
                        "",
                        post.get("post_text") or "",
                        "",
                        "### Heat",
                        "",
                        f"- Replies: {(post.get('heat') or {}).get('replies')}",
                        f"- Reposts: {(post.get('heat') or {}).get('reposts')}",
                        f"- Likes: {(post.get('heat') or {}).get('likes')}",
                        f"- Bookmarks: {(post.get('heat') or {}).get('bookmarks')}",
                        f"- Views: {(post.get('heat') or {}).get('views')}",
                        "",
                        "### Top Comments",
                        "",
                    ]
                )
                for i, comment in enumerate(post.get("top_comments") or [], start=1):
                    if not comment:
                        lines.append(f"- {i}. (null)")
                        continue
                    text = comment.get("comment_text") or ""
                    heat = XPostExtractor._format_comment_heat(comment.get("comment_heat"))
                    lines.append(f"- {i}. {text}")
                    lines.append(f"  heat: {heat}")
                lines.append("")

        md_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    @staticmethod
    def _push_file_to_phone(device_id: str | None, file_path: Path) -> str:
        adb_prefix = ["adb", "-s", device_id] if device_id else ["adb"]
        remote_dir = "/sdcard/Download/"
        result = subprocess.run(
            adb_prefix + ["push", str(file_path), remote_dir],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return f"{remote_dir}{file_path.name}"
        return ""

    @staticmethod
    def _build_scenario_key(post: dict[str, Any]) -> str:
        media_values = [str(item).strip().lower() for item in (post.get("media_types") or []) if item]
        media_part = "+".join(sorted(set(media_values))) if media_values else "unknown"
        type_flags: list[str] = []
        if post.get("is_repost"):
            type_flags.append("repost")
        if post.get("is_quote"):
            type_flags.append("quote")
        if post.get("is_reply"):
            type_flags.append("reply")
        if not type_flags:
            type_flags.append("original")
        return f"media:{media_part}|type:{'+'.join(type_flags)}"

    @staticmethod
    def _scenario_success(post: dict[str, Any]) -> bool:
        post_text = str(post.get("post_text") or "").strip()
        post_time = str(post.get("post_time") or "").strip()
        media = post.get("media_types") or []
        heat = post.get("heat") or {}
        visible_heat = sum(1 for key in ("replies", "reposts", "likes", "bookmarks", "views") if heat.get(key))
        has_core_content = bool(post_text) or bool(media)
        has_time_or_views = bool(post_time) or bool(heat.get("views"))
        return has_core_content and has_time_or_views and visible_heat >= 2

    @staticmethod
    def _scenario_advice(post: dict[str, Any]) -> str:
        media = [str(item).strip().lower() for item in (post.get("media_types") or []) if item]
        is_repost = bool(post.get("is_repost"))
        is_quote = bool(post.get("is_quote"))
        if "video" in media:
            return (
                "视频帖不要在播放器层连续滑动；按“作者栏->正文/时间”顺序各点1次进入线程详情，"
                "仍失败则评论置null继续。"
            )
        if "image" in media and (is_repost or is_quote):
            return "图像转帖/引用帖先点文字区域进入详情，分别记录原作者与转帖者，再抓热度。"
        if "image" in media:
            return "图像帖先采时间与热度，不可见字段填null后继续，避免在图片层反复滑动。"
        if is_repost or is_quote:
            return "转帖/引用帖要保留类型标记，并优先采集转帖文案+原帖热度。"
        return "文本帖优先在详情页一次性采正文、时间、热度，评论最多补1次滑动。"

    @staticmethod
    def _scenario_missing_fields(post: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        if not post.get("post_time"):
            missing.append("post_time")
        heat = post.get("heat") or {}
        for key in ("replies", "reposts", "likes", "bookmarks", "views"):
            if not heat.get(key):
                missing.append(f"heat.{key}")
        comments = post.get("top_comments") or []
        if not any(item for item in comments):
            missing.append("top_comments")
        return missing

    def _update_learning_rules(self, payload: dict[str, Any], out_dir: Path) -> Path:
        """Update reusable X extraction learning rules from latest captures."""
        rules_path = out_dir / "x_learning_rules.json"
        existing: dict[str, Any] = {"updated_at": "", "total_events": 0, "rules": []}
        if rules_path.exists():
            try:
                parsed = json.loads(rules_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    existing = parsed
            except Exception:
                pass

        indexed: dict[str, dict[str, Any]] = {}
        for item in existing.get("rules", []) if isinstance(existing.get("rules"), list) else []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("scenario") or "").strip()
            if not key:
                continue
            indexed[key] = {
                "scenario": key,
                "seen": int(item.get("seen", 0) or 0),
                "success": int(item.get("success", 0) or 0),
                "missing_fields": item.get("missing_fields", []),
                "advice": str(item.get("advice", "") or ""),
            }

        posts = payload.get("posts") if isinstance(payload, dict) else []
        if not isinstance(posts, list):
            posts = []

        for post in posts:
            if not isinstance(post, dict):
                continue
            key = self._build_scenario_key(post)
            slot = indexed.setdefault(
                key,
                {
                    "scenario": key,
                    "seen": 0,
                    "success": 0,
                    "missing_fields": [],
                    "advice": self._scenario_advice(post),
                },
            )
            slot["seen"] = int(slot.get("seen", 0) or 0) + 1
            if self._scenario_success(post):
                slot["success"] = int(slot.get("success", 0) or 0) + 1
            current_missing = set(str(item) for item in (slot.get("missing_fields") or []) if item)
            current_missing.update(self._scenario_missing_fields(post))
            slot["missing_fields"] = sorted(current_missing)
            if not slot.get("advice"):
                slot["advice"] = self._scenario_advice(post)

        merged_rules = list(indexed.values())
        for item in merged_rules:
            seen = int(item.get("seen", 0) or 0)
            success = int(item.get("success", 0) or 0)
            item["success_rate"] = round((success / seen), 3) if seen > 0 else 0.0
        merged_rules.sort(key=lambda row: (int(row.get("seen", 0)), float(row.get("success_rate", 0.0))), reverse=True)

        output = {
            "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total_events": int(existing.get("total_events", 0) or 0) + len(posts),
            "rules": merged_rules[:50],
        }
        rules_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return rules_path

    def export(self, instruction: str, device_id: str | None = None) -> str:
        """Export extraction memory to local files and optionally to phone download."""
        payload = self._merge_posts()
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("artifacts") / "x_extract"
        out_dir.mkdir(parents=True, exist_ok=True)

        json_path = out_dir / f"x_extract_{now}.json"
        md_path = out_dir / f"x_extract_{now}.md"

        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._write_markdown(payload, md_path)
        learning_path = self._update_learning_rules(payload, out_dir)

        pushed_paths: list[str] = []
        should_push_to_phone = os.getenv("PHONE_AGENT_EXPORT_TO_PHONE", "1") == "1"
        if should_push_to_phone:
            device_factory = get_device_factory()
            if device_factory.device_type == DeviceType.ADB:
                for file_path in (json_path, md_path):
                    remote = self._push_file_to_phone(device_id, file_path)
                    if remote:
                        pushed_paths.append(remote)

        message = (
            f"X extract exported: local_json={json_path}, local_md={md_path}, "
            f"learning={learning_path}, "
            f"captures={payload.get('capture_count', 0)}, "
            f"unique={payload.get('unique_capture_count', 0)}"
        )
        if pushed_paths:
            message += f", phone={','.join(pushed_paths)}"
        if instruction:
            message += f" | instruction={instruction}"
        return message
