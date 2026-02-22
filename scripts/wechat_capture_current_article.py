#!/usr/bin/env python3
"""Capture current WeChat article page-by-page, scroll to near bottom, and export."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import time
from pathlib import Path

from phone_agent.device_factory import get_device_factory
from phone_agent.extractors.wechat_article import WeChatArticleExtractor, _extract_json_object

BOTTOM_MARKERS = (
    "写留言",
    "留言",
    "精选留言",
    "阅读",
    "点赞",
    "分享",
)


def _normalize_capture_fields(capture) -> None:
    """Recover structured fields when model response is wrapped as fenced JSON text."""
    if capture.title and capture.source:
        return
    if not capture.body_text:
        return
    recovered = _extract_json_object(capture.body_text)
    if not recovered:
        return
    capture.title = str(recovered.get("title", "") or "").strip()
    capture.source = str(recovered.get("source", "") or "").strip()
    capture.publish_time = str(recovered.get("publish_time", "") or "").strip()
    capture.body_text = str(recovered.get("body_text", "") or capture.body_text).strip()
    capture.key_points = [
        str(item).strip()
        for item in (recovered.get("key_points") or [])
        if str(item).strip()
    ]
    capture.end_markers = [
        str(item).strip()
        for item in (recovered.get("end_markers") or [])
        if str(item).strip()
    ]


def _copy_exported_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture current WeChat article by scrolling and export details."
    )
    parser.add_argument("--device-id", type=str, default=os.getenv("PHONE_AGENT_DEVICE_ID"))
    parser.add_argument("--max-scrolls", type=int, default=20)
    parser.add_argument("--min-scrolls", type=int, default=6)
    parser.add_argument("--swipe-pause", type=float, default=1.0)
    parser.add_argument(
        "--slug",
        type=str,
        default="",
        help="Optional output slug, files will be copied to artifacts/wechat_extract/<slug>.{json,md}",
    )
    args = parser.parse_args()

    extractor = WeChatArticleExtractor()
    factory = get_device_factory()
    screenshot = factory.get_screenshot(args.device_id)
    width = screenshot.width
    height = screenshot.height
    center_x = width // 2
    swipe_start_y = int(height * 0.82)
    swipe_end_y = int(height * 0.30)

    duplicate_streak = 0
    bottom_detected = False
    capture_count = 0

    for idx in range(max(1, args.max_scrolls)):
        note = "wechat_article_meta" if idx == 0 else "wechat_article_page"
        capture = extractor.capture(note, args.device_id)
        _normalize_capture_fields(capture)
        capture_count += 1

        joined_markers = " ".join(capture.end_markers)
        if any(marker in joined_markers for marker in BOTTOM_MARKERS):
            bottom_detected = True

        if capture.is_duplicate:
            duplicate_streak += 1
        else:
            duplicate_streak = 0

        print(
            f"[capture] idx={idx + 1} dup={capture.is_duplicate} "
            f"title='{capture.title[:50]}' source='{capture.source[:30]}' "
            f"time='{capture.publish_time[:30]}' markers={capture.end_markers}"
        )

        if idx + 1 >= args.min_scrolls and (bottom_detected or duplicate_streak >= 2):
            break

        factory.swipe(
            center_x,
            swipe_start_y,
            center_x,
            swipe_end_y,
            device_id=args.device_id,
        )
        time.sleep(max(0.1, args.swipe_pause))

    message = extractor.export("wechat_export_to_download", args.device_id)
    print(f"[export] {message}")

    md_match = re.search(r"local_md=([^,]+)", message)
    json_match = re.search(r"local_json=([^,]+)", message)

    if md_match:
        md_path = Path(md_match.group(1).strip())
        print(f"[file] md={md_path}")
    else:
        md_path = None
    if json_match:
        json_path = Path(json_match.group(1).strip())
        print(f"[file] json={json_path}")
    else:
        json_path = None

    if args.slug:
        safe_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", args.slug.strip())
        if md_path and md_path.exists():
            dst_md = Path("artifacts/wechat_extract") / f"{safe_slug}.md"
            _copy_exported_file(md_path, dst_md)
            print(f"[file] copied_md={dst_md}")
        if json_path and json_path.exists():
            dst_json = Path("artifacts/wechat_extract") / f"{safe_slug}.json"
            _copy_exported_file(json_path, dst_json)
            print(f"[file] copied_json={dst_json}")

    print(
        f"[done] captures={capture_count} bottom_detected={bottom_detected} "
        f"duplicate_streak={duplicate_streak}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
