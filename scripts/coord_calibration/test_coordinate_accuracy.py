#!/usr/bin/env python3
"""
End-to-end coordinate tap accuracy test for Android devices.

This script validates the current coordinate mapping path in Open-AutoGLM by:
1) opening a generated touch-test page on the device,
2) sending taps through ActionHandler (same path used by runtime),
3) reading back the actual touch marker from screenshots,
4) reporting quantitative error metrics.
"""

from __future__ import annotations

import argparse
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

from PIL import Image, ImageChops

from phone_agent.actions.handler import ActionHandler
from phone_agent.device_factory import DeviceType, set_device_type


HTML_NAME = "coord_test.html"


@dataclass
class SampleResult:
    index: int
    expected_x: int
    expected_y: int
    detected: bool
    actual_x: int | None
    actual_y: int | None
    dx: float | None
    dy: float | None
    distance: float | None
    reason: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Quantitatively test tap coordinate accuracy on connected Android device."
    )
    parser.add_argument("--device-id", default=None, help="ADB device ID (optional).")
    parser.add_argument(
        "--rows", type=int, default=6, help="Grid rows inside detected viewport."
    )
    parser.add_argument(
        "--cols", type=int, default=4, help="Grid columns inside detected viewport."
    )
    parser.add_argument(
        "--margin-ratio",
        type=float,
        default=0.10,
        help="Safe margin ratio inside viewport for grid generation.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Local HTTP port. Use 0 to auto-pick a free port.",
    )
    parser.add_argument(
        "--browser-package",
        default=None,
        help="Optional browser package name for `am start -p`, e.g. com.android.chrome.",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=0.15,
        help="Extra wait time after tap before screenshot.",
    )
    parser.add_argument(
        "--pass-hit-rate",
        type=float,
        default=0.95,
        help="Pass threshold for hit rate (0~1).",
    )
    parser.add_argument(
        "--pass-p95-px",
        type=float,
        default=15.0,
        help="Pass threshold for p95 error in pixels.",
    )
    parser.add_argument(
        "--output-json",
        default="artifacts/coord_calibration/coord_accuracy_report.json",
        help="Path to write JSON report.",
    )
    parser.add_argument(
        "--coordinate-mode",
        choices=["auto", "absolute", "relative", "normalized"],
        default="absolute",
        help="PHONE_AGENT_COORDINATE_MODE during test.",
    )
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
        return channel.point(
            lambda v: 255 if minimum <= v <= maximum else 0, mode="1"
        )
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
    # Test page background is green: rgb(0, 180, 0)
    bbox = color_bbox(
        img, r_max=90, g_min=130, g_max=240, b_max=90
    )
    if bbox is None:
        raise RuntimeError(
            "Unable to locate test viewport (green region not found). "
            "Confirm device is showing the coordinate test page."
        )
    return bbox


def detect_marker_center(img: Image.Image) -> tuple[int, int] | None:
    # Prefer the red core marker first. It is drawn as a tiny square exactly at tap
    # coordinates and remains reliable near edges.
    core_bbox = color_bbox(
        img, r_min=200, g_max=70, b_max=70
    )
    if core_bbox is not None:
        core_w = core_bbox[2] - core_bbox[0]
        core_h = core_bbox[3] - core_bbox[1]
        if core_w >= 1 and core_h >= 1:
            center_x = int(round((core_bbox[0] + core_bbox[2] - 1) / 2.0))
            center_y = int(round((core_bbox[1] + core_bbox[3] - 1) / 2.0))
            return center_x, center_y

    # Marker is magenta: rgb(255, 0, 255)
    bbox = color_bbox(
        img, r_min=170, g_max=120, b_min=170
    )
    if bbox is None:
        return None
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    if width < 6 or height < 6:
        return None
    center_x = int(round((bbox[0] + bbox[2] - 1) / 2.0))
    center_y = int(round((bbox[1] + bbox[3] - 1) / 2.0))
    return center_x, center_y


def grid_points(
    bbox: tuple[int, int, int, int], rows: int, cols: int, margin_ratio: float
) -> list[tuple[int, int]]:
    left, top, right, bottom = bbox
    width = max(1, right - left)
    height = max(1, bottom - top)
    margin_x = max(8, int(width * margin_ratio))
    margin_y = max(8, int(height * margin_ratio))

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
  <title>Coordinate Accuracy Test</title>
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

    function drawGrid() {
      const w = c.width;
      const h = c.height;
      ctx.fillStyle = 'rgb(0,180,0)';
      ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = 'rgba(255,255,255,0.2)';
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
      // Exact tap core pixel block for robust readback near edges.
      const cx = Math.round(marker.x);
      const cy = Math.round(marker.y);
      ctx.fillStyle = 'rgb(255,0,0)';
      ctx.fillRect(cx - 1, cy - 1, 3, 3);
    }

    function render() {
      drawGrid();
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


def launch_test_page(prefix: list[str], port: int, browser_package: str | None) -> None:
    url = f"http://127.0.0.1:{port}/{HTML_NAME}"
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


def compute_report(samples: list[SampleResult], args: argparse.Namespace) -> dict:
    detected = [s for s in samples if s.detected and s.distance is not None]
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
        if s.detected:
            continue
        key = s.reason or "unknown"
        miss_reasons[key] = miss_reasons.get(key, 0) + 1

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
            "pass_threshold_hit_rate": args.pass_hit_rate,
            "pass_threshold_p95_px": args.pass_p95_px,
            "passed": passed,
            "miss_reasons": miss_reasons,
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

    server_proc: subprocess.Popen | None = None
    with tempfile.TemporaryDirectory(prefix="coord-accuracy-") as temp_dir:
        root = Path(temp_dir)
        write_test_page(root / HTML_NAME)

        try:
            server_proc = start_http_server(root, port)
            run_adb(prefix, ["reverse", f"tcp:{port}", f"tcp:{port}"])
            launch_test_page(prefix, port, args.browser_package)
            time.sleep(1.0)

            baseline = capture_screenshot(prefix)
            viewport = detect_viewport_bbox(baseline)
            points = grid_points(viewport, args.rows, args.cols, args.margin_ratio)
            screen_width, screen_height = baseline.size

            handler = ActionHandler(device_id=args.device_id)
            prev_marker = detect_marker_center(baseline)
            samples: list[SampleResult] = []

            print(
                f"[info] screen={screen_width}x{screen_height}, "
                f"viewport={viewport}, points={len(points)}"
            )

            for idx, (x, y) in enumerate(points, start=1):
                action = {"_metadata": "do", "action": "Tap", "element": [x, y]}
                result = handler.execute(action, screen_width, screen_height)
                if not result.success:
                    samples.append(
                        SampleResult(
                            index=idx,
                            expected_x=x,
                            expected_y=y,
                            detected=False,
                            actual_x=None,
                            actual_y=None,
                            dx=None,
                            dy=None,
                            distance=None,
                            reason=result.message or "action_failed",
                        )
                    )
                    continue

                time.sleep(max(0.0, args.settle_seconds))
                img = capture_screenshot(prefix)
                marker = detect_marker_center(img)

                if marker is None:
                    samples.append(
                        SampleResult(
                            index=idx,
                            expected_x=x,
                            expected_y=y,
                            detected=False,
                            actual_x=None,
                            actual_y=None,
                            dx=None,
                            dy=None,
                            distance=None,
                            reason="no_marker",
                        )
                    )
                    continue

                if (
                    prev_marker is not None
                    and abs(marker[0] - prev_marker[0]) <= 2
                    and abs(marker[1] - prev_marker[1]) <= 2
                ):
                    samples.append(
                        SampleResult(
                            index=idx,
                            expected_x=x,
                            expected_y=y,
                            detected=False,
                            actual_x=marker[0],
                            actual_y=marker[1],
                            dx=None,
                            dy=None,
                            distance=None,
                            reason="marker_unchanged",
                        )
                    )
                    continue

                dx = marker[0] - x
                dy = marker[1] - y
                dist = math.hypot(dx, dy)
                samples.append(
                    SampleResult(
                        index=idx,
                        expected_x=x,
                        expected_y=y,
                        detected=True,
                        actual_x=marker[0],
                        actual_y=marker[1],
                        dx=dx,
                        dy=dy,
                        distance=dist,
                        reason=None,
                    )
                )
                prev_marker = marker

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
            print(f"  passed: {summary['passed']}")
            print(f"  report_json: {output_json}")

            return 0 if summary["passed"] else 1

        finally:
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
