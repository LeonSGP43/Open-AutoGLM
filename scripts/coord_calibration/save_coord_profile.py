#!/usr/bin/env python3
"""
Save per-device coordinate scale profile from AI click accuracy report.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Save coordinate scale profile from test report."
    )
    parser.add_argument(
        "--report",
        required=True,
        help="Path to ai_click_accuracy_report*.json",
    )
    parser.add_argument("--device-id", required=True, help="ADB device ID")
    parser.add_argument(
        "--provider",
        default=os.getenv("PHONE_AGENT_PROVIDER", "openai"),
        help="Model provider (default from PHONE_AGENT_PROVIDER)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("PHONE_AGENT_MODEL", "autoglm-phone-9b"),
        help="Model name (default from PHONE_AGENT_MODEL)",
    )
    parser.add_argument(
        "--profile-file",
        default=str(Path.home() / ".openautoglm" / "coord_profiles.json"),
        help="Profile file path",
    )
    parser.add_argument(
        "--force-scale-x",
        type=float,
        default=None,
        help="Override scale_x instead of using inferred_inv_scale_x from report",
    )
    parser.add_argument(
        "--force-scale-y",
        type=float,
        default=None,
        help="Override scale_y instead of using inferred_inv_scale_y from report",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    report_path = Path(args.report).expanduser().resolve()
    profile_path = Path(args.profile_file).expanduser().resolve()

    if not report_path.exists():
        raise SystemExit(f"report not found: {report_path}")

    report = load_json(report_path)
    summary = report.get("summary", {})
    inv_x = summary.get("inferred_inv_scale_x")
    inv_y = summary.get("inferred_inv_scale_y")

    scale_x = args.force_scale_x if args.force_scale_x is not None else inv_x
    scale_y = args.force_scale_y if args.force_scale_y is not None else inv_y

    if scale_x is None or scale_y is None:
        raise SystemExit(
            "missing scale in report; run scripts/coord_calibration/calc_ai_coord_scale.py first"
        )

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    if profile_path.exists():
        data = load_json(profile_path)
    else:
        data = {"version": 1, "profiles": {}}

    profiles = data.setdefault("profiles", {})
    device_profiles = profiles.setdefault(args.device_id, {})
    key = f"{args.provider}::{args.model}"
    device_profiles[key] = {
        "scale_x": float(scale_x),
        "scale_y": float(scale_y),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source_report": str(report_path),
    }

    profile_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"saved: {profile_path}")
    print(f"device: {args.device_id}")
    print(f"key: {key}")
    print(f"scale_x: {float(scale_x):.6f}")
    print(f"scale_y: {float(scale_y):.6f}")
    print(
        "use with: PHONE_AGENT_COORD_PROFILE_FILE="
        f"{profile_path} <env-prefix> python main.py --device-id {args.device_id} \"任务\""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
