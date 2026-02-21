#!/usr/bin/env python3
"""
End-to-end AI tap accuracy test on Android devices.

What this script validates:
1) Draw a visible target point on a test page on the phone.
2) Ask the model (image + instruction) to click that target center.
3) Execute the model action via the same ActionHandler runtime path.
4) Read back the actual landed tap marker from screenshot.
5) Save per-sample overlay images and a JSON report with error statistics.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from statistics import mean
import argparse

from PIL import Image, ImageChops, ImageDraw

from phone_agent.actions.handler import ActionHandler, parse_action
from phone_agent.device_factory import DeviceType, get_device_factory, set_device_type
from phone_agent.model import ModelClient, ModelConfig
from phone_agent.model.client import MessageBuilder


HTML_NAME = "ai_coord_test.html"


@dataclass
class AISampleResult:
    index: int
    expected_x: int
    expected_y: int
    target_x: int | None
    target_y: int | None
    ai_x: int | None
    ai_y: int | None
    landed_x: int | None
    landed_y: int | None
    detected_target: bool
    detected_landed: bool
    dx: float | None
    dy: float | None
    distance: float | None
    action_raw: str | None
    reason: str | None
    overlay_path: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate model-driven tap accuracy by drawing a target point on device, "
            "asking AI to tap it, and measuring actual landed point."
        )
    )
    parser.add_argument("--device-id", default=None, help="ADB device ID (optional).")
    parser.add_argument("--rows", type=int, default=4, help="Grid rows.")
    parser.add_argument("--cols", type=int, default=3, help="Grid cols.")
    parser.add_argument(
        "--margin-ratio",
        type=float,
        default=0.12,
        help="Safe margin ratio inside viewport for target points.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8766,
        help="Local HTTP port; use 0 for auto-pick.",
    )
    parser.add_argument(
        "--browser-package",
        default="com.android.chrome",
        help="Browser package name used to open test page.",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=0.45,
        help="Wait time after AI tap before screenshot readback.",
    )
    parser.add_argument(
        "--coordinate-mode",
        choices=["auto", "absolute", "relative", "normalized"],
        default="auto",
        help="PHONE_AGENT_COORDINATE_MODE used by ActionHandler.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("PHONE_AGENT_BASE_URL", "http://localhost:8000/v1"),
        help="Model API base URL.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("PHONE_AGENT_MODEL", "autoglm-phone-9b"),
        help="Model name.",
    )
    parser.add_argument(
        "--apikey",
        default=os.getenv("PHONE_AGENT_API_KEY", "EMPTY"),
        help="Model API key.",
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "anthropic"],
        default=os.getenv("PHONE_AGENT_PROVIDER", "openai"),
        help="Model API provider format.",
    )
    parser.add_argument(
        "--anthropic-version",
        default=os.getenv("PHONE_AGENT_ANTHROPIC_VERSION", "2023-06-01"),
        help="Anthropic version header.",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=1200, help="Max output tokens for model."
    )
    parser.add_argument(
        "--output-json",
        default="artifacts/coord_calibration/ai_click_accuracy_report.json",
        help="Path to write JSON report.",
    )
    parser.add_argument(
        "--overlays-dir",
        default="artifacts/coord_calibration/ai_click_overlays",
        help="Directory for per-sample overlay screenshots.",
    )
    parser.add_argument(
        "--pass-hit-rate",
        type=float,
        default=0.9,
        help="Pass threshold for hit rate.",
    )
    parser.add_argument(
        "--pass-p95-px",
        type=float,
        default=35.0,
        help="Pass threshold for p95 landed error (px).",
    )
    parser.add_argument(
        "--no-force-portrait",
        dest="force_portrait",
        action="store_false",
        help="Do not force portrait orientation during test.",
    )
    parser.set_defaults(force_portrait=True)
    return parser.parse_args()


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def adb_prefix(device_id: str | None) -> list[str]:
    return ["adb", "-s", device_id] if device_id else ["adb"]


def run_cmd(cmd: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_adb(prefix: list[str], args: list[str], *, timeout: float = 30) -> None:
    result = run_cmd(prefix + args, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"ADB command failed: {' '.join(prefix + args)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def capture_screenshot(prefix: list[str]) -> Image.Image:
    raw = subprocess.check_output(prefix + ["exec-out", "screencap", "-p"], timeout=20)
    png = raw if raw.startswith(b"\x89PNG") else raw.replace(b"\r\n", b"\n")
    img = Image.open(BytesIO(png)).convert("RGB")
    img.load()
    return img


def _range_mask(
    channel: Image.Image, *, minimum: int | None = None, maximum: int | None = None
) -> Image.Image:
    if minimum is not None and maximum is not None:
        return channel.point(lambda v: 255 if minimum <= v <= maximum else 0, mode="1")
    if minimum is not None:
        return channel.point(lambda v: 255 if v >= minimum else 0, mode="1")
    if maximum is not None:
        return channel.point(lambda v: 255 if v <= maximum else 0, mode="1")
    return channel.point(lambda v: 255, mode="1")


def color_bbox(
    img: Image.Image,
    *,
    r_min: int | None = None,
    r_max: int | None = None,
    g_min: int | None = None,
    g_max: int | None = None,
    b_min: int | None = None,
    b_max: int | None = None,
) -> tuple[int, int, int, int] | None:
    r, g, b = img.split()
    mask = _range_mask(r, minimum=r_min, maximum=r_max)
    mask = ImageChops.logical_and(mask, _range_mask(g, minimum=g_min, maximum=g_max))
    mask = ImageChops.logical_and(mask, _range_mask(b, minimum=b_min, maximum=b_max))
    return mask.getbbox()


def detect_viewport_bbox(img: Image.Image) -> tuple[int, int, int, int]:
    # Viewport background is green.
    bbox = color_bbox(img, r_max=90, g_min=130, g_max=240, b_max=90)
    if bbox is None:
        raise RuntimeError(
            "Unable to locate test viewport (green region not found). "
            "Confirm device is showing the AI coordinate test page."
        )
    return bbox


def detect_target_center(img: Image.Image) -> tuple[int, int] | None:
    # Target center is drawn as cyan core block rgb(0,255,255).
    core_bbox = color_bbox(img, r_max=70, g_min=210, b_min=210)
    if core_bbox is None:
        return None
    w = core_bbox[2] - core_bbox[0]
    h = core_bbox[3] - core_bbox[1]
    if w < 2 or h < 2:
        return None
    cx = int(round((core_bbox[0] + core_bbox[2] - 1) / 2.0))
    cy = int(round((core_bbox[1] + core_bbox[3] - 1) / 2.0))
    return cx, cy


def detect_marker_center(img: Image.Image) -> tuple[int, int] | None:
    # Red exact core block, fallback to magenta marker body.
    core_bbox = color_bbox(img, r_min=200, g_max=70, b_max=70)
    if core_bbox is not None:
        w = core_bbox[2] - core_bbox[0]
        h = core_bbox[3] - core_bbox[1]
        if w >= 1 and h >= 1:
            cx = int(round((core_bbox[0] + core_bbox[2] - 1) / 2.0))
            cy = int(round((core_bbox[1] + core_bbox[3] - 1) / 2.0))
            return cx, cy

    bbox = color_bbox(img, r_min=170, g_max=120, b_min=170)
    if bbox is None:
        return None
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    if w < 6 or h < 6:
        return None
    cx = int(round((bbox[0] + bbox[2] - 1) / 2.0))
    cy = int(round((bbox[1] + bbox[3] - 1) / 2.0))
    return cx, cy


def grid_points(
    bbox: tuple[int, int, int, int], rows: int, cols: int, margin_ratio: float
) -> list[tuple[int, int]]:
    left, top, right, bottom = bbox
    width = max(1, right - left)
    height = max(1, bottom - top)
    margin_x = max(10, int(width * margin_ratio))
    margin_y = max(10, int(height * margin_ratio))

    x0 = left + margin_x
    x1 = max(x0, right - 1 - margin_x)
    y0 = top + margin_y
    y1 = max(y0, bottom - 1 - margin_y)

    xs: list[int] = []
    ys: list[int] = []
    for c in range(cols):
        if cols == 1:
            xs.append(int(round((x0 + x1) / 2)))
        else:
            xs.append(int(round(x0 + (x1 - x0) * c / (cols - 1))))
    for r in range(rows):
        if rows == 1:
            ys.append(int(round((y0 + y1) / 2)))
        else:
            ys.append(int(round(y0 + (y1 - y0) * r / (rows - 1))))

    points: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for y in ys:
        for x in xs:
            p = (x, y)
            if p not in seen:
                points.append(p)
                seen.add(p)
    return points


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return float("nan")
    idx = max(0, min(len(sorted_values) - 1, math.ceil(pct * len(sorted_values)) - 1))
    return sorted_values[idx]


def write_test_page(path: Path) -> None:
    html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
  <title>AI Tap Accuracy Test</title>
  <style>
    html, body { margin: 0; width: 100%; height: 100%; overflow: hidden; background: #000; }
    canvas { display: block; width: 100%; height: 100%; touch-action: none; }
  </style>
</head>
<body>
  <canvas id="c"></canvas>
  <script>
    const c = document.getElementById('c');
    const ctx = c.getContext('2d', { alpha: false });
    let marker = null;
    let target = null;

    function readTargetFromQuery() {
      const sp = new URLSearchParams(window.location.search);
      const packed = sp.get('t');
      if (packed && packed.includes(',')) {
        const parts = packed.split(',');
        const tx2 = Number(parts[0]);
        const ty2 = Number(parts[1]);
        if (Number.isFinite(tx2) && Number.isFinite(ty2)) {
          target = { x: tx2, y: ty2 };
          return;
        }
      }
      const tx = Number(sp.get('tx'));
      const ty = Number(sp.get('ty'));
      if (Number.isFinite(tx) && Number.isFinite(ty)) {
        target = { x: tx, y: ty };
      }
    }

    function drawGrid() {
      const w = c.width;
      const h = c.height;
      ctx.fillStyle = 'rgb(0,180,0)';
      ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = 'rgba(255,255,255,0.20)';
      ctx.lineWidth = 1;
      const cols = 10, rows = 16;
      for (let i = 1; i < cols; i++) {
        const x = Math.round(w * i / cols) + 0.5;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      }
      for (let j = 1; j < rows; j++) {
        const y = Math.round(h * j / rows) + 0.5;
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
      }
    }

    function drawTarget() {
      if (!target) return;
      const x = target.x;
      const y = target.y;
      // Yellow ring
      ctx.strokeStyle = 'rgb(255,255,0)';
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(x, y, 24, 0, Math.PI * 2);
      ctx.stroke();
      // Cyan crosshair
      ctx.strokeStyle = 'rgb(0,255,255)';
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(x - 18, y); ctx.lineTo(x + 18, y);
      ctx.moveTo(x, y - 18); ctx.lineTo(x, y + 18);
      ctx.stroke();
      // Cyan core block for robust readback.
      const cx = Math.round(x), cy = Math.round(y);
      ctx.fillStyle = 'rgb(0,255,255)';
      ctx.fillRect(cx - 2, cy - 2, 5, 5);
      // Label without numeric coordinates to avoid coordinate-frame leakage.
      ctx.fillStyle = 'rgb(255,255,255)';
      ctx.font = '24px sans-serif';
      ctx.fillText("TARGET", Math.min(c.width - 180, cx + 30), Math.max(40, cy - 30));
    }

    function drawMarker() {
      if (!marker) return;
      ctx.fillStyle = 'rgb(255,0,255)';
      ctx.beginPath();
      ctx.arc(marker.x, marker.y, 18, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = 'rgb(255,255,255)';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(marker.x, marker.y, 22, 0, Math.PI * 2);
      ctx.stroke();
      // Red exact core block
      const cx = Math.round(marker.x), cy = Math.round(marker.y);
      ctx.fillStyle = 'rgb(255,0,0)';
      ctx.fillRect(cx - 1, cy - 1, 3, 3);
    }

    function render() {
      drawGrid();
      drawTarget();
      drawMarker();
    }

    function resize() {
      c.width = Math.max(1, Math.floor(window.innerWidth));
      c.height = Math.max(1, Math.floor(window.innerHeight));
      render();
    }

    window.addEventListener('resize', resize);
    window.addEventListener('orientationchange', resize);

    c.addEventListener('pointerdown', (ev) => {
      marker = { x: ev.clientX, y: ev.clientY };
      render();
    }, { passive: true });

    readTargetFromQuery();
    resize();
  </script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def wait_for_http(port: int, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"Local test server failed to start on 127.0.0.1:{port}")


def start_http_server(root: Path, port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(root),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for_http(port)
    return proc


def launch_test_page(
    prefix: list[str],
    port: int,
    browser_package: str | None,
    target_x: int,
    target_y: int,
) -> None:
    url = f"http://127.0.0.1:{port}/{HTML_NAME}?t={target_x},{target_y}"
    args = [
        "shell",
        "am",
        "start",
        "-W",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        url,
    ]
    if browser_package:
        args.extend(["-p", browser_package])
    run_adb(prefix, args, timeout=30)


def get_system_setting(prefix: list[str], key: str) -> str:
    result = run_cmd(prefix + ["shell", "settings", "get", "system", key], timeout=10)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def put_system_setting(prefix: list[str], key: str, value: str) -> None:
    run_adb(prefix, ["shell", "settings", "put", "system", key, value], timeout=10)


def build_model_client(args: argparse.Namespace) -> ModelClient:
    model_config = ModelConfig(
        base_url=args.base_url,
        api_key=args.apikey,
        provider=args.provider,
        model_name=args.model,
        anthropic_version=args.anthropic_version,
        max_tokens=args.max_tokens,
        temperature=0.0,
        top_p=0.85,
        frequency_penalty=0.0,
        lang="cn",
    )
    return ModelClient(model_config)


def ask_ai_for_tap(
    model_client: ModelClient,
    screenshot: Image.Image,
    current_app: str,
) -> tuple[str, dict | None, str | None]:
    buf = BytesIO()
    screenshot.save(buf, format="PNG")
    # Encode screenshot for model input.
    import base64

    image_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    system_prompt = (
        "You are a strict Android tap testing agent.\n"
        "You receive one screenshot containing a CYAN target crosshair with a yellow ring.\n"
        "Your task is only to tap the center of that target.\n"
        "Do not infer coordinates from any on-screen text; use visual target position only.\n"
        "Return exactly one action string in this format:\n"
        "do(action=\"Tap\", element=[x, y])\n"
        "Use absolute pixel coordinates for this screenshot."
    )

    screen_width, screen_height = screenshot.size
    screen_info = MessageBuilder.build_screen_info(
        current_app=current_app,
        screen_width=screen_width,
        screen_height=screen_height,
    )
    user_text = (
        "Tap the exact center of the cyan target crosshair (with yellow ring). "
        "Do not tap any other position.\n\n"
        f"{screen_info}"
    )

    messages = [
        MessageBuilder.create_system_message(system_prompt),
        MessageBuilder.create_user_message(text=user_text, image_base64=image_base64),
    ]
    response = model_client.request(messages)

    try:
        action = parse_action(response.action)
        return response.action, action, None
    except Exception as e:
        return response.action, None, f"parse_action_failed: {e}"


def save_overlay(
    image: Image.Image,
    expected: tuple[int, int],
    target: tuple[int, int] | None,
    ai_point: tuple[int, int] | None,
    landed: tuple[int, int] | None,
    out_path: Path,
) -> None:
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)

    def draw_point(p: tuple[int, int], color: tuple[int, int, int], label: str) -> None:
        x, y = p
        r = 12
        draw.ellipse((x - r, y - r, x + r, y + r), outline=color, width=3)
        draw.line((x - 18, y, x + 18, y), fill=color, width=2)
        draw.line((x, y - 18, x, y + 18), fill=color, width=2)
        draw.text((x + 16, y + 8), f"{label} ({x}, {y})", fill=color)

    draw_point(expected, (255, 255, 255), "EXP")
    if target is not None:
        draw_point(target, (0, 255, 255), "TGT")
    if ai_point is not None:
        draw_point(ai_point, (255, 165, 0), "AI")
    if landed is not None:
        draw_point(landed, (255, 0, 255), "LAND")
    if target is not None and landed is not None:
        draw.line((target[0], target[1], landed[0], landed[1]), fill=(255, 0, 0), width=2)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG")


def compute_report(samples: list[AISampleResult], args: argparse.Namespace) -> dict:
    detected = [s for s in samples if s.detected_target and s.detected_landed and s.distance is not None]
    distances = sorted(float(s.distance) for s in detected)
    dxs = [float(s.dx) for s in detected if s.dx is not None]
    dys = [float(s.dy) for s in detected if s.dy is not None]

    hit_rate = len(detected) / len(samples) if samples else 0.0
    mean_distance = mean(distances) if distances else float("nan")
    p95_distance = percentile(distances, 0.95) if distances else float("nan")
    max_distance = max(distances) if distances else float("nan")
    bias_x = mean(dxs) if dxs else float("nan")
    bias_y = mean(dys) if dys else float("nan")

    passed = (
        hit_rate >= args.pass_hit_rate
        and (not math.isnan(p95_distance) and p95_distance <= args.pass_p95_px)
    )

    miss_reasons: dict[str, int] = {}
    for s in samples:
        if s.detected_target and s.detected_landed and s.distance is not None:
            continue
        key = s.reason or "unknown"
        miss_reasons[key] = miss_reasons.get(key, 0) + 1

    # Infer uniform coordinate scaling mismatch between AI output space and screen space.
    # ai ~= target * scale  =>  target ~= ai * inv_scale
    scale_x_samples: list[float] = []
    scale_y_samples: list[float] = []
    for s in samples:
        if (
            s.target_x is not None
            and s.target_y is not None
            and s.ai_x is not None
            and s.ai_y is not None
            and s.target_x != 0
            and s.target_y != 0
        ):
            scale_x_samples.append(float(s.ai_x) / float(s.target_x))
            scale_y_samples.append(float(s.ai_y) / float(s.target_y))

    inferred_scale_x = percentile(sorted(scale_x_samples), 0.5) if scale_x_samples else float("nan")
    inferred_scale_y = percentile(sorted(scale_y_samples), 0.5) if scale_y_samples else float("nan")

    compensated_distances: list[float] = []
    if (
        not math.isnan(inferred_scale_x)
        and not math.isnan(inferred_scale_y)
        and inferred_scale_x > 1e-6
        and inferred_scale_y > 1e-6
    ):
        inv_x = 1.0 / inferred_scale_x
        inv_y = 1.0 / inferred_scale_y
        for s in samples:
            if (
                s.target_x is None
                or s.target_y is None
                or s.landed_x is None
                or s.landed_y is None
            ):
                continue
            cx = s.landed_x * inv_x
            cy = s.landed_y * inv_y
            compensated_distances.append(math.hypot(cx - s.target_x, cy - s.target_y))

    compensated_distances.sort()
    compensated_mean = mean(compensated_distances) if compensated_distances else float("nan")
    compensated_p95 = percentile(compensated_distances, 0.95) if compensated_distances else float("nan")
    compensated_max = max(compensated_distances) if compensated_distances else float("nan")

    return {
        "summary": {
            "total_points": len(samples),
            "detected_points": len(detected),
            "missed_points": len(samples) - len(detected),
            "hit_rate": hit_rate,
            "mean_distance_px": mean_distance,
            "p95_distance_px": p95_distance,
            "max_distance_px": max_distance,
            "bias_x_px": bias_x,
            "bias_y_px": bias_y,
            "coordinate_mode": args.coordinate_mode,
            "pass_threshold_hit_rate": args.pass_hit_rate,
            "pass_threshold_p95_px": args.pass_p95_px,
            "passed": passed,
            "miss_reasons": miss_reasons,
            "inferred_ai_scale_x": inferred_scale_x,
            "inferred_ai_scale_y": inferred_scale_y,
            "inferred_inv_scale_x": (1.0 / inferred_scale_x) if not math.isnan(inferred_scale_x) and inferred_scale_x > 1e-6 else float("nan"),
            "inferred_inv_scale_y": (1.0 / inferred_scale_y) if not math.isnan(inferred_scale_y) and inferred_scale_y > 1e-6 else float("nan"),
            "compensated_mean_distance_px": compensated_mean,
            "compensated_p95_distance_px": compensated_p95,
            "compensated_max_distance_px": compensated_max,
        },
        "samples": [asdict(s) for s in samples],
    }


def main() -> int:
    args = parse_args()

    if args.rows < 1 or args.cols < 1:
        print("rows and cols must be >= 1", file=sys.stderr)
        return 2
    if not (0.0 <= args.margin_ratio <= 0.45):
        print("margin-ratio must be between 0 and 0.45", file=sys.stderr)
        return 2

    os.environ["PHONE_AGENT_COORDINATE_MODE"] = args.coordinate_mode
    set_device_type(DeviceType.ADB)

    port = get_free_port() if args.port == 0 else args.port
    prefix = adb_prefix(args.device_id)
    output_json = Path(args.output_json).resolve()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    overlays_dir = Path(args.overlays_dir).resolve()
    overlays_dir.mkdir(parents=True, exist_ok=True)

    model_client = build_model_client(args)
    action_handler = ActionHandler(device_id=args.device_id)
    device_factory = get_device_factory()

    server_proc: subprocess.Popen | None = None
    prev_accel: str | None = None
    prev_user_rotation: str | None = None
    with tempfile.TemporaryDirectory(prefix="ai-coord-accuracy-") as temp_dir:
        root = Path(temp_dir)
        write_test_page(root / HTML_NAME)

        try:
            if args.force_portrait:
                prev_accel = get_system_setting(prefix, "accelerometer_rotation")
                prev_user_rotation = get_system_setting(prefix, "user_rotation")
                put_system_setting(prefix, "accelerometer_rotation", "0")
                put_system_setting(prefix, "user_rotation", "0")
                time.sleep(0.4)

            server_proc = start_http_server(root, port)
            run_adb(prefix, ["reverse", f"tcp:{port}", f"tcp:{port}"])

            # Prime page and detect viewport.
            launch_test_page(prefix, port, args.browser_package, 100, 100)
            time.sleep(0.8)
            baseline = capture_screenshot(prefix)
            viewport = detect_viewport_bbox(baseline)
            screen_width, screen_height = baseline.size

            calib_target = detect_target_center(baseline)
            if calib_target is None:
                raise RuntimeError(
                    "Unable to detect calibration target. "
                    "Check that browser page loaded correctly."
                )

            viewport_left, viewport_top, viewport_right, viewport_bottom = viewport
            dpr_x = (calib_target[0] - viewport_left) / 100.0
            dpr_y = (calib_target[1] - viewport_top) / 100.0
            if dpr_x <= 0.2 or dpr_y <= 0.2:
                dpr_x = 1.0
                dpr_y = 1.0

            css_width = int(round((viewport_right - viewport_left) / dpr_x))
            css_height = int(round((viewport_bottom - viewport_top) / dpr_y))
            css_width = max(1, css_width)
            css_height = max(1, css_height)
            points_css = grid_points((0, 0, css_width, css_height), args.rows, args.cols, args.margin_ratio)

            print(
                f"[info] screen={screen_width}x{screen_height}, "
                f"viewport={viewport}, css={css_width}x{css_height}, dpr=({dpr_x:.3f},{dpr_y:.3f}), "
                f"points={len(points_css)}"
            )

            samples: list[AISampleResult] = []

            for idx, (css_x, css_y) in enumerate(points_css, start=1):
                launch_test_page(prefix, port, args.browser_package, css_x, css_y)
                time.sleep(0.8)
                expected_x = int(round(viewport_left + css_x * dpr_x))
                expected_y = int(round(viewport_top + css_y * dpr_y))

                before = capture_screenshot(prefix)
                target = detect_target_center(before)
                if target is None:
                    sample = AISampleResult(
                        index=idx,
                        expected_x=expected_x,
                        expected_y=expected_y,
                        target_x=None,
                        target_y=None,
                        ai_x=None,
                        ai_y=None,
                        landed_x=None,
                        landed_y=None,
                        detected_target=False,
                        detected_landed=False,
                        dx=None,
                        dy=None,
                        distance=None,
                        action_raw=None,
                        reason="target_not_found",
                        overlay_path=None,
                    )
                    samples.append(sample)
                    continue

                current_app = device_factory.get_current_app(args.device_id)
                action_raw, action, action_err = ask_ai_for_tap(
                    model_client=model_client,
                    screenshot=before,
                    current_app=current_app,
                )

                if action is None:
                    overlay_path = overlays_dir / f"sample_{idx:03d}.png"
                    save_overlay(
                        image=before,
                        expected=(expected_x, expected_y),
                        target=target,
                        ai_point=None,
                        landed=None,
                        out_path=overlay_path,
                    )
                    samples.append(
                        AISampleResult(
                            index=idx,
                            expected_x=expected_x,
                            expected_y=expected_y,
                            target_x=target[0],
                            target_y=target[1],
                            ai_x=None,
                            ai_y=None,
                            landed_x=None,
                            landed_y=None,
                            detected_target=True,
                            detected_landed=False,
                            dx=None,
                            dy=None,
                            distance=None,
                            action_raw=action_raw,
                            reason=action_err or "parse_failed",
                            overlay_path=str(overlay_path),
                        )
                    )
                    continue

                if action.get("_metadata") != "do" or action.get("action") != "Tap":
                    overlay_path = overlays_dir / f"sample_{idx:03d}.png"
                    save_overlay(
                        image=before,
                        expected=(expected_x, expected_y),
                        target=target,
                        ai_point=None,
                        landed=None,
                        out_path=overlay_path,
                    )
                    samples.append(
                        AISampleResult(
                            index=idx,
                            expected_x=expected_x,
                            expected_y=expected_y,
                            target_x=target[0],
                            target_y=target[1],
                            ai_x=None,
                            ai_y=None,
                            landed_x=None,
                            landed_y=None,
                            detected_target=True,
                            detected_landed=False,
                            dx=None,
                            dy=None,
                            distance=None,
                            action_raw=action_raw,
                            reason="non_tap_action",
                            overlay_path=str(overlay_path),
                        )
                    )
                    continue

                ai_point: tuple[int, int] | None = None
                element = action.get("element")
                if (
                    isinstance(element, list)
                    and len(element) >= 2
                    and isinstance(element[0], (int, float))
                    and isinstance(element[1], (int, float))
                ):
                    ai_point = (int(round(float(element[0]))), int(round(float(element[1]))))

                exec_result = action_handler.execute(action, screen_width, screen_height)
                if not exec_result.success:
                    overlay_path = overlays_dir / f"sample_{idx:03d}.png"
                    save_overlay(
                        image=before,
                        expected=(expected_x, expected_y),
                        target=target,
                        ai_point=ai_point,
                        landed=None,
                        out_path=overlay_path,
                    )
                    samples.append(
                        AISampleResult(
                            index=idx,
                            expected_x=expected_x,
                            expected_y=expected_y,
                            target_x=target[0],
                            target_y=target[1],
                            ai_x=ai_point[0] if ai_point else None,
                            ai_y=ai_point[1] if ai_point else None,
                            landed_x=None,
                            landed_y=None,
                            detected_target=True,
                            detected_landed=False,
                            dx=None,
                            dy=None,
                            distance=None,
                            action_raw=action_raw,
                            reason=exec_result.message or "tap_execute_failed",
                            overlay_path=str(overlay_path),
                        )
                    )
                    continue

                time.sleep(max(0.0, args.settle_seconds))
                after = capture_screenshot(prefix)
                landed = detect_marker_center(after)

                overlay_path = overlays_dir / f"sample_{idx:03d}.png"
                save_overlay(
                    image=after,
                    expected=(expected_x, expected_y),
                    target=target,
                    ai_point=ai_point,
                    landed=landed,
                    out_path=overlay_path,
                )

                if landed is None:
                    samples.append(
                        AISampleResult(
                            index=idx,
                            expected_x=expected_x,
                            expected_y=expected_y,
                            target_x=target[0],
                            target_y=target[1],
                            ai_x=ai_point[0] if ai_point else None,
                            ai_y=ai_point[1] if ai_point else None,
                            landed_x=None,
                            landed_y=None,
                            detected_target=True,
                            detected_landed=False,
                            dx=None,
                            dy=None,
                            distance=None,
                            action_raw=action_raw,
                            reason="no_landed_marker",
                            overlay_path=str(overlay_path),
                        )
                    )
                    continue

                dx = landed[0] - target[0]
                dy = landed[1] - target[1]
                dist = math.hypot(dx, dy)
                samples.append(
                    AISampleResult(
                        index=idx,
                        expected_x=expected_x,
                        expected_y=expected_y,
                        target_x=target[0],
                        target_y=target[1],
                        ai_x=ai_point[0] if ai_point else None,
                        ai_y=ai_point[1] if ai_point else None,
                        landed_x=landed[0],
                        landed_y=landed[1],
                        detected_target=True,
                        detected_landed=True,
                        dx=dx,
                        dy=dy,
                        distance=dist,
                        action_raw=action_raw,
                        reason=None,
                        overlay_path=str(overlay_path),
                    )
                )
                print(
                    f"[sample {idx:03d}] target=({target[0]},{target[1]}) "
                    f"ai={ai_point} landed={landed} dist={dist:.2f}px"
                )

            report = compute_report(samples, args)
            output_json.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            summary = report["summary"]
            print("[result]")
            print(f"  total_points: {summary['total_points']}")
            print(f"  detected_points: {summary['detected_points']}")
            print(f"  missed_points: {summary['missed_points']}")
            print(f"  hit_rate: {summary['hit_rate']:.4f}")
            print(f"  mean_distance_px: {summary['mean_distance_px']:.2f}")
            print(f"  p95_distance_px: {summary['p95_distance_px']:.2f}")
            print(f"  max_distance_px: {summary['max_distance_px']:.2f}")
            print(f"  bias_x_px: {summary['bias_x_px']:.2f}")
            print(f"  bias_y_px: {summary['bias_y_px']:.2f}")
            print(f"  inferred_ai_scale_x: {summary['inferred_ai_scale_x']:.6f}")
            print(f"  inferred_ai_scale_y: {summary['inferred_ai_scale_y']:.6f}")
            print(f"  inferred_inv_scale_x: {summary['inferred_inv_scale_x']:.6f}")
            print(f"  inferred_inv_scale_y: {summary['inferred_inv_scale_y']:.6f}")
            print(
                f"  compensated_mean_distance_px: {summary['compensated_mean_distance_px']:.2f}"
            )
            print(
                f"  compensated_p95_distance_px: {summary['compensated_p95_distance_px']:.2f}"
            )
            print(
                f"  compensated_max_distance_px: {summary['compensated_max_distance_px']:.2f}"
            )
            print(f"  passed: {summary['passed']}")
            print(f"  coordinate_mode: {summary['coordinate_mode']}")
            print(f"  report_json: {output_json}")
            print(f"  overlays_dir: {overlays_dir}")

            return 0 if summary["passed"] else 1
        finally:
            if args.force_portrait:
                with contextlib.suppress(Exception):
                    if prev_accel not in (None, ""):
                        put_system_setting(prefix, "accelerometer_rotation", prev_accel)
                with contextlib.suppress(Exception):
                    if prev_user_rotation not in (None, ""):
                        put_system_setting(prefix, "user_rotation", prev_user_rotation)
            with contextlib.suppress(Exception):
                run_adb(prefix, ["reverse", "--remove", f"tcp:{port}"])
            if server_proc is not None:
                with contextlib.suppress(Exception):
                    server_proc.terminate()
                    server_proc.wait(timeout=2)
                with contextlib.suppress(Exception):
                    if server_proc.poll() is None:
                        server_proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
