#!/usr/bin/env python3
"""Fetch WeChat article text directly from article URLs (no OCR/model calls)."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


def _fetch_html(url: str, timeout: float = 20.0) -> str:
    req = Request(
        url,
        headers={
            "user-agent": USER_AGENT,
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "referer": "https://mp.weixin.qq.com/",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _first_match(pattern: str, text: str, flags: int = 0) -> str:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else ""


def _strip_html(raw_html: str) -> str:
    txt = re.sub(r"(?is)<script[^>]*>.*?</script>", "", raw_html)
    txt = re.sub(r"(?is)<style[^>]*>.*?</style>", "", txt)
    txt = re.sub(r"(?i)<br\\s*/?>", "\n", txt)
    txt = re.sub(r"(?is)</p>", "\n\n", txt)
    txt = re.sub(r"(?is)<[^>]+>", "", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"\r\n?", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _decode_js_quoted(value: str) -> str:
    # Handle escaped unicode sequences from JS strings.
    try:
        return bytes(value, "utf-8").decode("unicode_escape")
    except Exception:
        return value


def _extract_attr(tag: str, attr_name: str) -> str:
    pattern = rf'{attr_name}\s*=\s*"(.*?)"'
    m = re.search(pattern, tag, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return html.unescape(m.group(1).strip())


def _normalize_img_url(raw_url: str, base_url: str) -> str:
    raw = (raw_url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("/"):
        return urljoin(base_url, raw)
    if raw.startswith("javascript:") or raw.startswith("data:"):
        return ""
    return urljoin(base_url, raw)


def _extract_images_and_marked_html(content_html: str, base_url: str) -> tuple[str, list[dict]]:
    images: list[dict] = []
    seen: set[str] = set()
    out_parts: list[str] = []
    last = 0
    img_idx = 0

    for match in re.finditer(r"(?is)<img\b[^>]*>", content_html or ""):
        out_parts.append(content_html[last : match.start()])
        tag = match.group(0)

        candidates = [
            _extract_attr(tag, "data-src"),
            _extract_attr(tag, "data-original"),
            _extract_attr(tag, "src"),
            _extract_attr(tag, "original-src"),
            _extract_attr(tag, "data-backsrc"),
        ]
        resolved = ""
        for candidate in candidates:
            resolved = _normalize_img_url(candidate, base_url)
            if resolved:
                break

        if resolved and resolved not in seen:
            seen.add(resolved)
            img_idx += 1
            image_id = f"IMG_{img_idx:03d}"
            alt = _extract_attr(tag, "alt") or _extract_attr(tag, "data-alt")
            images.append(
                {
                    "id": image_id,
                    "url": resolved,
                    "alt": alt,
                    "local_rel_path": "",
                    "downloaded": False,
                    "error": "",
                }
            )
            out_parts.append(f"\n\n[[{image_id}]]\n\n")
        last = match.end()

    out_parts.append((content_html or "")[last:])
    return "".join(out_parts), images


def _guess_image_ext(img_url: str, content_type: str) -> str:
    ctype = (content_type or "").lower().strip()
    if "png" in ctype:
        return ".png"
    if "gif" in ctype:
        return ".gif"
    if "webp" in ctype:
        return ".webp"
    if "bmp" in ctype:
        return ".bmp"
    if "svg" in ctype:
        return ".svg"
    if "jpeg" in ctype or "jpg" in ctype:
        return ".jpg"

    path = urlparse(img_url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def _download_images(images: list[dict], asset_dir: Path, referer_url: str, timeout: float = 20.0) -> None:
    if not images:
        return
    asset_dir.mkdir(parents=True, exist_ok=True)
    for idx, image in enumerate(images, start=1):
        img_url = image.get("url", "")
        if not img_url:
            image["error"] = "empty_url"
            continue
        try:
            req = Request(
                img_url,
                headers={
                    "user-agent": USER_AGENT,
                    "referer": referer_url,
                    "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            with urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                content_type = resp.headers.get("Content-Type", "")
            ext = _guess_image_ext(img_url, content_type)
            filename = f"image_{idx:03d}{ext}"
            local_path = asset_dir / filename
            local_path.write_bytes(payload)
            image["downloaded"] = True
            image["local_rel_path"] = f"{asset_dir.name}/{filename}"
        except Exception as exc:
            image["error"] = str(exc)


def _render_body_with_images(body_text: str, images: list[dict]) -> str:
    rendered = body_text or ""
    for image in images:
        marker = f"[[{image.get('id', '')}]]"
        alt = image.get("alt", "") or image.get("id", "image")
        link = image.get("local_rel_path") if image.get("downloaded") else image.get("url", "")
        replacement = f"![{alt}]({link})" if link else ""
        rendered = rendered.replace(marker, replacement)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return rendered


def _parse_article(url: str, page_html: str) -> dict:
    title = _first_match(r'<meta\\s+property="og:title"\\s+content="([^"]+)"', page_html)
    if not title:
        title = _first_match(r"<title>(.*?)</title>", page_html, flags=re.DOTALL)
    title = html.unescape(title).strip()
    title = re.sub(r"\s+", " ", title)

    nickname_raw = _first_match(r'var\\s+nickname\\s*=\\s*htmlDecode\\("([^"]*)"\\)', page_html)
    if not nickname_raw:
        nickname_raw = _first_match(r'var\\s+user_name\\s*=\\s*"([^"]*)"', page_html)
    author = html.unescape(_decode_js_quoted(nickname_raw)).strip()

    ct_raw = _first_match(r'var\\s+ct\\s*=\\s*"?(\\d+)"?\\s*;', page_html)
    publish_time = ""
    if ct_raw.isdigit():
        ts = int(ct_raw)
        publish_time = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    content_html = _first_match(
        r'(?is)<div[^>]*id="js_content"[^>]*>(.*?)</div>\\s*<script',
        page_html,
    )
    if not content_html:
        content_html = _first_match(r'(?is)<div[^>]*id="js_content"[^>]*>(.*?)</div>', page_html)
    marked_html, images = _extract_images_and_marked_html(content_html, url)
    body_text = _strip_html(marked_html)

    return {
        "url": url,
        "title": title,
        "author": author,
        "publish_time": publish_time,
        "body_text": body_text,
        "images": images,
    }


def _slug_from_url(url: str, idx: int) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    mid = (query.get("mid") or [""])[0]
    sn = (query.get("sn") or [""])[0][:10]
    if mid or sn:
        base = "_".join([part for part in [mid, sn] if part])
    else:
        base = f"article_{idx}"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)


def _write_article_files(article: dict, out_dir: Path, slug: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{slug}.json"
    md_path = out_dir / f"{slug}.md"

    json_path.write_text(json.dumps(article, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    body_markdown = article.get("body_markdown", article.get("body_text", ""))
    images = article.get("images", [])
    downloaded_count = len([img for img in images if img.get("downloaded")])

    md_lines = [
        f"# {article.get('title') or '(no title)'}",
        "",
        f"- Author: {article.get('author', '')}",
        f"- Publish Time: {article.get('publish_time', '')}",
        f"- URL: {article.get('url', '')}",
        f"- Images: {downloaded_count}/{len(images)} downloaded",
        "",
        "## Body",
        "",
        body_markdown,
        "",
    ]
    if images:
        md_lines.extend(["## Image List", ""])
        for image in images:
            if image.get("downloaded"):
                md_lines.append(
                    f"- {image.get('id')}: {image.get('local_rel_path')} <- {image.get('url')}"
                )
            else:
                md_lines.append(
                    f"- {image.get('id')}: download_failed ({image.get('error','')}) <- {image.get('url')}"
                )
        md_lines.append("")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    return json_path, md_path


def _load_urls(args) -> list[str]:
    urls: list[str] = []
    urls.extend(args.url or [])
    if args.url_file:
        for line in Path(args.url_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    # Keep order, dedupe.
    seen: set[str] = set()
    ordered: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        ordered.append(u)
    return ordered


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch WeChat articles by URL.")
    parser.add_argument("--url", action="append", help="WeChat article URL, can pass multiple")
    parser.add_argument("--url-file", help="Text file with one URL per line")
    parser.add_argument(
        "--out-dir",
        default="artifacts/wechat_web_extract",
        help="Output directory for markdown/json",
    )
    args = parser.parse_args()

    urls = _load_urls(args)
    if not urls:
        raise SystemExit("No URLs provided. Use --url or --url-file.")

    out_dir = Path(args.out_dir)
    summary = []
    for i, url in enumerate(urls, start=1):
        page_html = _fetch_html(url)
        article = _parse_article(url, page_html)
        slug = _slug_from_url(url, i)
        asset_dir = out_dir / f"{slug}_assets"
        _download_images(article.get("images", []), asset_dir, url)
        article["body_markdown"] = _render_body_with_images(
            article.get("body_text", ""), article.get("images", [])
        )
        json_path, md_path = _write_article_files(article, out_dir, slug)
        image_total = len(article.get("images", []))
        image_downloaded = len([img for img in article.get("images", []) if img.get("downloaded")])
        summary.append(
            {
                "url": url,
                "title": article.get("title", ""),
                "author": article.get("author", ""),
                "publish_time": article.get("publish_time", ""),
                "image_total": image_total,
                "image_downloaded": image_downloaded,
                "json_path": str(json_path),
                "md_path": str(md_path),
            }
        )
        print(f"[saved] {md_path} images={image_downloaded}/{image_total}")

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[summary] {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
