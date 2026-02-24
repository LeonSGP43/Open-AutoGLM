#!/usr/bin/env python3
"""Generate consolidated task/learning metrics report for Open-AutoGLM."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any


def _module_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    return _module_root().parent


def _default_experience_db() -> Path:
    return Path(
        os.getenv(
            "PHONE_AGENT_EXPERIENCE_DB",
            str(Path.home() / ".openautoglm" / "experience.db"),
        )
    ).expanduser()


def _default_token_usage_dir() -> Path:
    return _repo_root() / "artifacts" / "token_usage"


def _default_out_dir() -> Path:
    return _module_root() / "data"


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    text = str(ts).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _fmt_pct(num: float) -> str:
    return f"{num * 100:.2f}%"


def _fmt_float(num: float | None, digits: int = 2) -> str:
    if num is None:
        return "-"
    return f"{num:.{digits}f}"


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    rank = (len(values) - 1) * p
    lo = int(rank)
    hi = min(lo + 1, len(values) - 1)
    frac = rank - lo
    return float(values[lo] * (1.0 - frac) + values[hi] * frac)


def _to_iso(ts: int | None) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds")
    except Exception:
        return "-"


@dataclass
class RunUsage:
    file: str
    task: str
    provider: str
    model: str
    device_id: str
    start: datetime | None
    end: datetime | None
    duration_sec: float
    step_count: int
    execution_fail_steps: int
    semantic_fail_steps: int
    finish_steps: int
    tokens_total: int | None


def _serialize_runs(runs: list[RunUsage], limit: int = 120) -> list[dict[str, Any]]:
    tail = runs[-max(1, int(limit)) :]
    rows: list[dict[str, Any]] = []
    for item in tail:
        rows.append(
            {
                "file": item.file,
                "task": item.task[:120],
                "provider": item.provider,
                "model": item.model,
                "device_id": item.device_id,
                "start": item.start.isoformat(timespec="seconds") if item.start else None,
                "end": item.end.isoformat(timespec="seconds") if item.end else None,
                "duration_sec": item.duration_sec,
                "step_count": item.step_count,
                "execution_fail_steps": item.execution_fail_steps,
                "semantic_fail_steps": item.semantic_fail_steps,
                "finish_steps": item.finish_steps,
                "tokens_total": item.tokens_total,
            }
        )
    return rows


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except Exception:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    except Exception:
        return []
    return rows


def _extract_total_tokens(record: dict[str, Any]) -> int | None:
    totals = record.get("running_totals")
    if not isinstance(totals, dict):
        return None
    total = totals.get("total_tokens")
    if isinstance(total, int):
        return total
    p = totals.get("prompt_tokens", totals.get("input_tokens"))
    c = totals.get("completion_tokens", totals.get("output_tokens"))
    if isinstance(p, int) and isinstance(c, int):
        return p + c
    return None


def _collect_usage_runs(token_usage_dir: Path) -> list[RunUsage]:
    runs: list[RunUsage] = []
    if not token_usage_dir.exists():
        return runs

    files = sorted(token_usage_dir.glob("token_usage_*.jsonl"))
    for path in files:
        rows = _load_jsonl(path)
        if not rows:
            continue
        run_start = next((r for r in rows if r.get("event") == "run_start"), None)
        step_rows = [r for r in rows if r.get("event") == "step"]
        if not step_rows:
            continue

        start_dt = _parse_iso((run_start or {}).get("timestamp")) or _parse_iso(
            step_rows[0].get("timestamp")
        )
        end_dt = _parse_iso(step_rows[-1].get("timestamp"))
        duration = 0.0
        if start_dt and end_dt:
            duration = max(0.0, (end_dt - start_dt).total_seconds())

        tokens_total = _extract_total_tokens(step_rows[-1])
        finish_steps = sum(1 for row in step_rows if row.get("action_metadata") == "finish")
        exec_fail = sum(1 for row in step_rows if row.get("success") is False)
        semantic_fail = sum(1 for row in step_rows if row.get("semantic_success") is False)

        runs.append(
            RunUsage(
                file=path.name,
                task=str((run_start or {}).get("task") or ""),
                provider=str((run_start or {}).get("provider") or step_rows[0].get("provider") or ""),
                model=str((run_start or {}).get("model") or step_rows[0].get("model") or ""),
                device_id=str((run_start or {}).get("device_id") or ""),
                start=start_dt,
                end=end_dt,
                duration_sec=duration,
                step_count=len(step_rows),
                execution_fail_steps=exec_fail,
                semantic_fail_steps=semantic_fail,
                finish_steps=finish_steps,
                tokens_total=tokens_total,
            )
        )
    runs.sort(key=lambda item: (item.start or datetime.min))
    return runs


def _query_db(db_path: Path, topn: int) -> dict[str, Any]:
    empty = {
        "db_found": False,
        "overall_runs": 0,
        "overall_successes": 0,
        "overall_success_rate": 0.0,
        "overall_error_rate": 0.0,
        "action_attempts": 0,
        "action_successes": 0,
        "action_success_rate": 0.0,
        "semantic_failures": 0,
        "semantic_failure_rate": 0.0,
        "tasks": [],
        "top_failures": [],
    }
    if not db_path.exists():
        return empty

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(runs),0) AS runs, COALESCE(SUM(successes),0) AS successes "
            "FROM task_outcome_stats"
        ).fetchone()
        overall_runs = int(row["runs"] if row else 0)
        overall_successes = int(row["successes"] if row else 0)

        row2 = conn.execute(
            "SELECT COALESCE(SUM(attempts),0) AS attempts, COALESCE(SUM(successes),0) AS successes, "
            "COALESCE(SUM(semantic_failures),0) AS semantic_failures "
            "FROM action_stats"
        ).fetchone()
        action_attempts = int(row2["attempts"] if row2 else 0)
        action_successes = int(row2["successes"] if row2 else 0)
        semantic_failures = int(row2["semantic_failures"] if row2 else 0)

        tasks: list[dict[str, Any]] = []
        for r in conn.execute(
            "SELECT task_signature, runs, successes, updated_at "
            "FROM task_outcome_stats ORDER BY runs DESC, updated_at DESC LIMIT ?",
            (max(1, topn),),
        ).fetchall():
            task = str(r["task_signature"] or "")
            runs = int(r["runs"] or 0)
            successes = int(r["successes"] or 0)
            top_failure = conn.execute(
                "SELECT failure_reason, failures FROM task_failure_stats "
                "WHERE task_signature = ? ORDER BY failures DESC, updated_at DESC LIMIT 1",
                (task,),
            ).fetchone()
            tasks.append(
                {
                    "task_signature": task,
                    "runs": runs,
                    "successes": successes,
                    "success_rate": _safe_div(successes, runs),
                    "error_rate": 1.0 - _safe_div(successes, runs),
                    "updated_at": int(r["updated_at"] or 0),
                    "top_failure_reason": (
                        str(top_failure["failure_reason"] or "") if top_failure else ""
                    ),
                    "top_failure_count": int(top_failure["failures"] or 0) if top_failure else 0,
                }
            )

        top_failures = [
            {"failure_reason": str(r["failure_reason"] or ""), "count": int(r["count"] or 0)}
            for r in conn.execute(
                "SELECT failure_reason, COALESCE(SUM(failures),0) AS count "
                "FROM task_failure_stats GROUP BY failure_reason "
                "ORDER BY count DESC LIMIT ?",
                (max(1, topn),),
            ).fetchall()
        ]

        return {
            "db_found": True,
            "overall_runs": overall_runs,
            "overall_successes": overall_successes,
            "overall_success_rate": _safe_div(overall_successes, overall_runs),
            "overall_error_rate": 1.0 - _safe_div(overall_successes, overall_runs),
            "action_attempts": action_attempts,
            "action_successes": action_successes,
            "action_success_rate": _safe_div(action_successes, action_attempts),
            "semantic_failures": semantic_failures,
            "semantic_failure_rate": _safe_div(semantic_failures, action_attempts),
            "tasks": tasks,
            "top_failures": top_failures,
        }
    finally:
        conn.close()


def _usage_summary(runs: list[RunUsage], compare_window: int) -> dict[str, Any]:
    if not runs:
        return {
            "runs": 0,
            "avg_duration_sec": 0.0,
            "p50_duration_sec": 0.0,
            "p90_duration_sec": 0.0,
            "avg_steps": 0.0,
            "avg_exec_fail_steps": 0.0,
            "avg_semantic_fail_steps": 0.0,
            "avg_tokens_total": 0.0,
            "runs_with_finish": 0,
            "compare": None,
        }

    durations = sorted([item.duration_sec for item in runs])
    steps = [item.step_count for item in runs]
    exec_fails = [item.execution_fail_steps for item in runs]
    semantic_fails = [item.semantic_fail_steps for item in runs]
    tokens = [item.tokens_total for item in runs if isinstance(item.tokens_total, int)]
    finish_count = sum(1 for item in runs if item.finish_steps > 0)

    summary: dict[str, Any] = {
        "runs": len(runs),
        "avg_duration_sec": mean(durations) if durations else 0.0,
        "p50_duration_sec": _percentile(durations, 0.50) or 0.0,
        "p90_duration_sec": _percentile(durations, 0.90) or 0.0,
        "avg_steps": mean(steps) if steps else 0.0,
        "avg_exec_fail_steps": mean(exec_fails) if exec_fails else 0.0,
        "avg_semantic_fail_steps": mean(semantic_fails) if semantic_fails else 0.0,
        "avg_tokens_total": mean(tokens) if tokens else 0.0,
        "runs_with_finish": finish_count,
        "compare": None,
    }

    w = max(1, int(compare_window))
    if len(runs) >= (2 * w):
        prev = runs[-2 * w : -w]
        recent = runs[-w:]

        def _avg(vals: list[float]) -> float:
            return mean(vals) if vals else 0.0

        prev_duration = _avg([r.duration_sec for r in prev])
        recent_duration = _avg([r.duration_sec for r in recent])
        prev_steps = _avg([float(r.step_count) for r in prev])
        recent_steps = _avg([float(r.step_count) for r in recent])
        prev_tokens = _avg([float(r.tokens_total) for r in prev if isinstance(r.tokens_total, int)])
        recent_tokens = _avg([float(r.tokens_total) for r in recent if isinstance(r.tokens_total, int)])
        prev_sem_fail = _avg([float(r.semantic_fail_steps) for r in prev])
        recent_sem_fail = _avg([float(r.semantic_fail_steps) for r in recent])

        def _delta_pct(new: float, old: float) -> float | None:
            if old == 0:
                return 0.0 if new == 0 else None
            return (new - old) / old

        summary["compare"] = {
            "window": w,
            "prev_avg_duration_sec": prev_duration,
            "recent_avg_duration_sec": recent_duration,
            "duration_change_pct": _delta_pct(recent_duration, prev_duration),
            "prev_avg_steps": prev_steps,
            "recent_avg_steps": recent_steps,
            "steps_change_pct": _delta_pct(recent_steps, prev_steps),
            "prev_avg_tokens": prev_tokens,
            "recent_avg_tokens": recent_tokens,
            "tokens_change_pct": _delta_pct(recent_tokens, prev_tokens),
            "prev_avg_semantic_fail_steps": prev_sem_fail,
            "recent_avg_semantic_fail_steps": recent_sem_fail,
            "semantic_fail_change_pct": _delta_pct(recent_sem_fail, prev_sem_fail),
        }

    return summary


def _render_markdown(
    db_summary: dict[str, Any],
    usage_summary: dict[str, Any],
    token_dir: Path,
    db_path: Path,
    generated_at: str,
) -> str:
    lines: list[str] = []
    lines.append("# Open-AutoGLM Metrics Report")
    lines.append("")
    lines.append(f"- Generated At: {generated_at}")
    lines.append(f"- Experience DB: `{db_path}`")
    lines.append(f"- Token Usage Dir: `{token_dir}`")
    lines.append("")

    lines.append("## Overview")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Task Runs (DB) | {db_summary['overall_runs']} |")
    lines.append(f"| Task Successes (DB) | {db_summary['overall_successes']} |")
    lines.append(f"| Task Success Rate (DB) | {_fmt_pct(db_summary['overall_success_rate'])} |")
    lines.append(f"| Task Error Rate (DB) | {_fmt_pct(db_summary['overall_error_rate'])} |")
    lines.append(f"| Action Attempts (DB) | {db_summary['action_attempts']} |")
    lines.append(f"| Action Success Rate (DB) | {_fmt_pct(db_summary['action_success_rate'])} |")
    lines.append(f"| Semantic Failure Rate (DB) | {_fmt_pct(db_summary['semantic_failure_rate'])} |")
    lines.append(f"| Runs (Token Logs) | {usage_summary['runs']} |")
    lines.append(f"| Avg Duration / run (s) | {_fmt_float(usage_summary['avg_duration_sec'])} |")
    lines.append(f"| P50 Duration / run (s) | {_fmt_float(usage_summary['p50_duration_sec'])} |")
    lines.append(f"| P90 Duration / run (s) | {_fmt_float(usage_summary['p90_duration_sec'])} |")
    lines.append(f"| Avg Steps / run | {_fmt_float(usage_summary['avg_steps'])} |")
    lines.append(f"| Avg Semantic Fail Steps / run | {_fmt_float(usage_summary['avg_semantic_fail_steps'])} |")
    lines.append(f"| Avg Tokens / run | {_fmt_float(usage_summary['avg_tokens_total'])} |")
    lines.append("")

    tasks = db_summary.get("tasks") or []
    lines.append("## Top Tasks")
    lines.append("")
    if not tasks:
        lines.append("_No task-level rows in DB yet._")
    else:
        lines.append("| Task Signature | Runs | Successes | Success Rate | Error Rate | Top Failure | Updated At |")
        lines.append("|---|---:|---:|---:|---:|---|---|")
        for row in tasks:
            lines.append(
                "| {task} | {runs} | {succ} | {sr} | {er} | {fr} ({fc}) | {updated} |".format(
                    task=row.get("task_signature", "")[:120],
                    runs=row.get("runs", 0),
                    succ=row.get("successes", 0),
                    sr=_fmt_pct(float(row.get("success_rate", 0.0))),
                    er=_fmt_pct(float(row.get("error_rate", 0.0))),
                    fr=(row.get("top_failure_reason", "") or "-")[:80],
                    fc=row.get("top_failure_count", 0),
                    updated=_to_iso(int(row.get("updated_at", 0) or 0)),
                )
            )
    lines.append("")

    fails = db_summary.get("top_failures") or []
    lines.append("## Top Failure Reasons")
    lines.append("")
    if not fails:
        lines.append("_No failure rows in DB yet._")
    else:
        lines.append("| Failure Reason | Count |")
        lines.append("|---|---:|")
        for row in fails:
            lines.append(
                f"| {(row.get('failure_reason', '') or '-').replace('|', '/')} | {row.get('count', 0)} |"
            )
    lines.append("")

    compare = usage_summary.get("compare")
    lines.append("## Optimization Effect")
    lines.append("")
    if not compare:
        lines.append("_Not enough runs for recent-vs-previous window comparison._")
    else:
        lines.append(
            f"Window: recent `{compare['window']}` runs vs previous `{compare['window']}` runs."
        )
        lines.append("")
        lines.append("| Metric | Previous | Recent | Change |")
        lines.append("|---|---:|---:|---:|")
        lines.append(
            f"| Avg Duration (s) | {_fmt_float(compare['prev_avg_duration_sec'])} | "
            f"{_fmt_float(compare['recent_avg_duration_sec'])} | "
            f"{_format_change(compare['duration_change_pct'])} |"
        )
        lines.append(
            f"| Avg Steps | {_fmt_float(compare['prev_avg_steps'])} | "
            f"{_fmt_float(compare['recent_avg_steps'])} | "
            f"{_format_change(compare['steps_change_pct'])} |"
        )
        lines.append(
            f"| Avg Tokens | {_fmt_float(compare['prev_avg_tokens'])} | "
            f"{_fmt_float(compare['recent_avg_tokens'])} | "
            f"{_format_change(compare['tokens_change_pct'])} |"
        )
        lines.append(
            f"| Avg Semantic Fail Steps | {_fmt_float(compare['prev_avg_semantic_fail_steps'])} | "
            f"{_fmt_float(compare['recent_avg_semantic_fail_steps'])} | "
            f"{_format_change(compare['semantic_fail_change_pct'])} |"
        )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _format_change(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.2f}%"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate success/error/speed metrics report from DB and token logs."
    )
    parser.add_argument(
        "--db-path",
        default=str(_default_experience_db()),
        help="Path to experience.db (default from PHONE_AGENT_EXPERIENCE_DB).",
    )
    parser.add_argument(
        "--token-usage-dir",
        default=str(_default_token_usage_dir()),
        help="Directory containing token_usage_*.jsonl files.",
    )
    parser.add_argument(
        "--topn",
        type=int,
        default=10,
        help="Top-N rows for task/failure tables.",
    )
    parser.add_argument(
        "--compare-window",
        type=int,
        default=20,
        help="Runs per window for optimization effect comparison.",
    )
    parser.add_argument(
        "--out-dir",
        default=str(_default_out_dir()),
        help="Output directory for report files.",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser()
    token_dir = Path(args.token_usage_dir).expanduser()
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    db_summary = _query_db(db_path, topn=max(1, args.topn))
    usage_runs = _collect_usage_runs(token_dir)
    usage_summary = _usage_summary(usage_runs, compare_window=max(1, args.compare_window))

    generated_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        "generated_at": generated_at,
        "inputs": {
            "db_path": str(db_path),
            "token_usage_dir": str(token_dir),
            "topn": int(args.topn),
            "compare_window": int(args.compare_window),
        },
        "db_summary": db_summary,
        "usage_summary": usage_summary,
        "recent_runs": _serialize_runs(usage_runs, limit=160),
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"metrics_summary_{ts}.json"
    md_path = out_dir / f"metrics_summary_{ts}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(
        _render_markdown(db_summary, usage_summary, token_dir, db_path, generated_at),
        encoding="utf-8",
    )

    print(f"Report JSON: {json_path}")
    print(f"Report Markdown: {md_path}")


if __name__ == "__main__":
    main()
