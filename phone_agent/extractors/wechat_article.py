"""WeChat article extraction helper driven by vision model + page dedup."""

from __future__ import annotations

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
class PageCapture:
    """One captured WeChat article page."""

    note: str
    title: str = ""
    source: str = ""
    publish_time: str = ""
    body_text: str = ""
    key_points: list[str] = field(default_factory=list)
    end_markers: list[str] = field(default_factory=list)
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
    # Remove markdown code fences if present.
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


class WeChatArticleExtractor:
    """Collect and export WeChat article content from incremental screenshots."""

    def __init__(self) -> None:
        self.base_url = os.getenv("PHONE_AGENT_BASE_URL", "http://localhost:8000/v1")
        self.model_name = os.getenv("PHONE_AGENT_MODEL", "autoglm-phone-9b")
        self.api_key = os.getenv("PHONE_AGENT_API_KEY", "EMPTY")
        self.provider = os.getenv("PHONE_AGENT_PROVIDER", "openai").strip().lower()
        self.anthropic_version = os.getenv("PHONE_AGENT_ANTHROPIC_VERSION", "2023-06-01")

        self._captures: list[PageCapture] = []
        self._seen_hashes: set[str] = set()

    @staticmethod
    def _normalize_for_hash(text: str) -> str:
        compact = re.sub(r"\s+", "", text or "")
        return compact[:12000]

    def _hash_page(self, title: str, body_text: str, key_points: list[str]) -> str:
        payload = "\n".join([title or "", body_text or "", "\n".join(key_points or [])])
        normalized = self._normalize_for_hash(payload)
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

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
            max_tokens=900,
        )
        return (response.choices[0].message.content or "").strip()

    def _analyze_with_anthropic(self, image_base64: str, prompt: str) -> str:
        endpoint = _build_anthropic_messages_url(self.base_url)
        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": 900,
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

    def _analyze_page(self, image_base64: str) -> dict[str, Any]:
        prompt = (
            "You are extracting content from a WeChat public-account article screenshot. "
            "Return strict JSON only with keys: title, source, publish_time, body_text, "
            "key_points, end_markers, is_article, is_ad_or_promo. "
            "Rules: keep body_text concise but useful; key_points must be array of short strings; "
            "if uncertain return empty strings/arrays."
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
            print(f"[wechat-ocr] analyze failed: {exc}")
            return {}

        # Fallback: retain plain text when model does not return strict JSON.
        fallback_text = raw.strip()
        if fallback_text:
            recovered = _extract_json_object(fallback_text)
            if recovered:
                return recovered
            print(f"[wechat-ocr] non-json fallback text captured, len={len(fallback_text)}")
            return {
                "title": "",
                "source": "",
                "publish_time": "",
                "body_text": fallback_text[:3000],
                "key_points": [],
                "end_markers": [],
                "is_article": False,
                "is_ad_or_promo": False,
            }
        print("[wechat-ocr] empty response from OCR model")
        return {}

    def capture(self, note: str, device_id: str | None = None) -> PageCapture:
        """Capture current page and append to extraction memory."""
        screenshot = get_device_factory().get_screenshot(device_id)
        parsed = self._analyze_page(screenshot.base64_data)

        title = str(parsed.get("title", "") or "").strip()
        source = str(parsed.get("source", "") or "").strip()
        publish_time = str(parsed.get("publish_time", "") or "").strip()
        body_text = str(parsed.get("body_text", "") or "").strip()
        key_points = [
            str(item).strip()
            for item in (parsed.get("key_points") or [])
            if str(item).strip()
        ]
        end_markers = [
            str(item).strip()
            for item in (parsed.get("end_markers") or [])
            if str(item).strip()
        ]

        page_hash = self._hash_page(title, body_text, key_points)
        is_duplicate = page_hash in self._seen_hashes or not any([title, body_text, key_points])
        if not is_duplicate:
            self._seen_hashes.add(page_hash)

        capture = PageCapture(
            note=note,
            title=title,
            source=source,
            publish_time=publish_time,
            body_text=body_text,
            key_points=key_points,
            end_markers=end_markers,
            is_duplicate=is_duplicate,
            page_hash=page_hash,
        )
        self._captures.append(capture)
        return capture

    def _merge_article(self) -> dict[str, Any]:
        valid = [item for item in self._captures if not item.is_duplicate]
        if not valid:
            valid = self._captures[:]

        title = next((item.title for item in valid if item.title), "")
        source = next((item.source for item in valid if item.source), "")
        publish_time = next((item.publish_time for item in valid if item.publish_time), "")

        merged_lines: list[str] = []
        seen_lines: set[str] = set()
        merged_points: list[str] = []
        seen_points: set[str] = set()

        for item in valid:
            for line in (item.body_text or "").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                key = self._normalize_for_hash(stripped)
                if key in seen_lines:
                    continue
                seen_lines.add(key)
                merged_lines.append(stripped)

            for point in item.key_points:
                key = self._normalize_for_hash(point)
                if key in seen_points:
                    continue
                seen_points.add(key)
                merged_points.append(point)

        return {
            "title": title,
            "source": source,
            "publish_time": publish_time,
            "merged_body_text": "\n".join(merged_lines).strip(),
            "key_points": merged_points,
            "captures": [item.__dict__ for item in self._captures],
            "capture_count": len(self._captures),
            "unique_capture_count": len([item for item in self._captures if not item.is_duplicate]),
        }

    @staticmethod
    def _write_markdown(payload: dict[str, Any], md_path: Path) -> None:
        lines = [
            f"# {payload.get('title') or 'WeChat Article Extract'}",
            "",
            f"- Source: {payload.get('source') or ''}",
            f"- Publish Time: {payload.get('publish_time') or ''}",
            f"- Captures: {payload.get('capture_count', 0)}",
            f"- Unique Captures: {payload.get('unique_capture_count', 0)}",
            "",
            "## Key Points",
            "",
        ]
        points = payload.get("key_points") or []
        if points:
            for point in points:
                lines.append(f"- {point}")
        else:
            lines.append("- (none)")

        lines.extend(["", "## Body Text", "", payload.get("merged_body_text") or ""])
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

    def export(self, instruction: str, device_id: str | None = None) -> str:
        """Export extraction memory to local files and optionally to phone download."""
        payload = self._merge_article()
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("artifacts") / "wechat_extract"
        out_dir.mkdir(parents=True, exist_ok=True)

        json_path = out_dir / f"wechat_extract_{now}.json"
        md_path = out_dir / f"wechat_extract_{now}.md"

        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._write_markdown(payload, md_path)

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
            f"WeChat extract exported: local_json={json_path}, local_md={md_path}, "
            f"captures={payload.get('capture_count', 0)}, "
            f"unique={payload.get('unique_capture_count', 0)}"
        )
        if pushed_paths:
            message += f", phone={','.join(pushed_paths)}"
        if instruction:
            message += f" | instruction={instruction}"
        return message
