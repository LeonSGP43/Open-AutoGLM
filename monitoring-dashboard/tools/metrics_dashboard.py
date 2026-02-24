#!/usr/bin/env python3
"""Generate a visual monitoring dashboard HTML from metrics summary JSON."""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _module_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    return _module_root().parent


def _default_reports_dir() -> Path:
    return _module_root() / "data"


def _default_out_file() -> Path:
    return _module_root() / "public" / "metrics_dashboard.html"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.2f}%"


def _fmt_num(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def _latest_metrics_json(reports_dir: Path) -> Path | None:
    files = sorted(reports_dir.glob("metrics_summary_*.json"))
    return files[-1] if files else None


def _run_metrics_report(module_root: Path, repo_root: Path, reports_dir: Path) -> Path | None:
    cmd = [
        sys.executable,
        str(module_root / "tools" / "metrics_report.py"),
        "--out-dir",
        str(reports_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(repo_root))
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        return None
    return _latest_metrics_json(reports_dir)


def _build_failure_bars(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty">No failure rows.</div>'
    max_count = max(int(item.get("count", 0) or 0) for item in rows) or 1
    blocks: list[str] = []
    for item in rows[:10]:
        reason = html.escape(str(item.get("failure_reason", "") or "-"))
        count = int(item.get("count", 0) or 0)
        width = int((count / max_count) * 100)
        blocks.append(
            "<div class='bar-row'>"
            f"<div class='bar-label'>{reason}</div>"
            f"<div class='bar-wrap'><div class='bar-fill' style='width:{width}%'></div></div>"
            f"<div class='bar-count'>{count}</div>"
            "</div>"
        )
    return "\n".join(blocks)


def _build_task_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            "<tr><td colspan='7' class='empty-cell'>"
            "No task-level rows in database."
            "</td></tr>"
        )
    out: list[str] = []
    for item in rows[:12]:
        out.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('task_signature', ''))[:140])}</td>"
            f"<td>{int(item.get('runs', 0) or 0)}</td>"
            f"<td>{int(item.get('successes', 0) or 0)}</td>"
            f"<td>{_fmt_pct(float(item.get('success_rate', 0.0) or 0.0))}</td>"
            f"<td>{_fmt_pct(float(item.get('error_rate', 0.0) or 0.0))}</td>"
            f"<td>{html.escape(str(item.get('top_failure_reason', '') or '-'))}</td>"
            f"<td>{html.escape(str(item.get('top_failure_count', 0) or 0))}</td>"
            "</tr>"
        )
    return "\n".join(out)


def _infer_case_status(item: dict[str, Any]) -> str:
    finish = int(item.get("finish_steps", 0) or 0)
    exec_fail = int(item.get("execution_fail_steps", 0) or 0)
    sem_fail = int(item.get("semantic_fail_steps", 0) or 0)
    if finish > 0 and exec_fail == 0 and sem_fail == 0:
        return "finished_clean"
    if finish > 0:
        return "finished_with_failures"
    if exec_fail > 0 or sem_fail > 0:
        return "incomplete_or_failed"
    return "unknown"


def _build_case_rows(rows: list[dict[str, Any]], limit: int = 20) -> str:
    if not rows:
        return "<tr><td colspan='10' class='empty-cell' data-i18n='empty.cases'></td></tr>"

    out: list[str] = []
    for item in list(rows)[-max(1, limit) :][::-1]:
        task_raw = str(item.get("task", "") or "")
        start_raw = str(item.get("start", "") or "-")
        file_raw = str(item.get("file", "") or "-")
        task = html.escape(task_raw[:120])
        start = html.escape(start_raw)
        duration = _fmt_num(float(item.get("duration_sec", 0.0) or 0.0), digits=1)
        steps = int(item.get("step_count", 0) or 0)
        exec_fail = int(item.get("execution_fail_steps", 0) or 0)
        sem_fail = int(item.get("semantic_fail_steps", 0) or 0)
        finish = int(item.get("finish_steps", 0) or 0)
        tokens = item.get("tokens_total")
        tokens_text = str(int(tokens)) if isinstance(tokens, int) else "-"
        log_file = html.escape(file_raw)
        status = _infer_case_status(item)
        search_blob = html.escape(f"{task_raw} {file_raw} {start_raw}".lower(), quote=True)
        out.append(
            f"<tr data-case-status='{status}' data-case-search='{search_blob}'>"
            f"<td>{start}</td>"
            f"<td>{task}</td>"
            f"<td>{duration}</td>"
            f"<td>{steps}</td>"
            f"<td>{exec_fail}</td>"
            f"<td>{sem_fail}</td>"
            f"<td>{finish}</td>"
            f"<td>{tokens_text}</td>"
            f"<td><span class='status-chip' data-status='{status}'></span></td>"
            f"<td>{log_file}</td>"
            "</tr>"
        )
    return "\n".join(out)


def _build_dashboard_html(payload: dict[str, Any], json_name: str, refresh_tip: str) -> str:
    db = payload.get("db_summary", {}) if isinstance(payload.get("db_summary"), dict) else {}
    usage = payload.get("usage_summary", {}) if isinstance(payload.get("usage_summary"), dict) else {}
    inputs = payload.get("inputs", {}) if isinstance(payload.get("inputs"), dict) else {}
    recent_runs = payload.get("recent_runs", [])
    if not isinstance(recent_runs, list):
        recent_runs = []
    compare = usage.get("compare", {}) if isinstance(usage.get("compare"), dict) else {}

    cards = [
        ("card.task_runs", str(int(db.get("overall_runs", 0) or 0))),
        ("card.task_success_rate", _fmt_pct(float(db.get("overall_success_rate", 0.0) or 0.0))),
        ("card.task_error_rate", _fmt_pct(float(db.get("overall_error_rate", 0.0) or 0.0))),
        ("card.action_success_rate", _fmt_pct(float(db.get("action_success_rate", 0.0) or 0.0))),
        ("card.semantic_failure_rate", _fmt_pct(float(db.get("semantic_failure_rate", 0.0) or 0.0))),
        ("card.token_log_runs", str(int(usage.get("runs", 0) or 0))),
        ("card.avg_duration", _fmt_num(float(usage.get("avg_duration_sec", 0.0) or 0.0))),
        ("card.p90_duration", _fmt_num(float(usage.get("p90_duration_sec", 0.0) or 0.0))),
    ]
    card_html = "\n".join(
        (
            "<div class='card'>"
            f"<div class='card-k' data-i18n='{html.escape(k)}'></div>"
            f"<div class='card-v'>{html.escape(v)}</div>"
            "</div>"
        )
        for k, v in cards
    )

    compare_rows = ""
    if compare:
        rows = [
            (
                "metric.duration",
                compare.get("prev_avg_duration_sec"),
                compare.get("recent_avg_duration_sec"),
                compare.get("duration_change_pct"),
            ),
            (
                "metric.steps",
                compare.get("prev_avg_steps"),
                compare.get("recent_avg_steps"),
                compare.get("steps_change_pct"),
            ),
            (
                "metric.tokens",
                compare.get("prev_avg_tokens"),
                compare.get("recent_avg_tokens"),
                compare.get("tokens_change_pct"),
            ),
            (
                "metric.semantic_fail_steps",
                compare.get("prev_avg_semantic_fail_steps"),
                compare.get("recent_avg_semantic_fail_steps"),
                compare.get("semantic_fail_change_pct"),
            ),
        ]
        out: list[str] = []
        for metric_key, prev, recent, delta in rows:
            if delta is None:
                delta_text = "n/a"
                delta_class = "delta-na"
            else:
                d = float(delta)
                delta_text = f"{d * 100:+.2f}%"
                delta_class = "delta-up" if d > 0 else ("delta-down" if d < 0 else "delta-flat")
            out.append(
                "<tr>"
                f"<td data-i18n='{metric_key}'></td>"
                f"<td>{_fmt_num(prev)}</td>"
                f"<td>{_fmt_num(recent)}</td>"
                f"<td class='{delta_class}'>{delta_text}</td>"
                "</tr>"
            )
        compare_rows = "\n".join(out)
    else:
        compare_rows = "<tr><td colspan='4' class='empty-cell' data-i18n='empty.compare'></td></tr>"

    failure_bars = _build_failure_bars(db.get("top_failures", []))
    task_rows = _build_task_rows(db.get("tasks", []))
    case_rows = _build_case_rows(recent_runs, limit=20)
    generated_at = html.escape(str(payload.get("generated_at", "")))
    db_path = html.escape(str(inputs.get("db_path", "-")))
    token_usage_dir = html.escape(str(inputs.get("token_usage_dir", "-")))
    compare_window = html.escape(str(inputs.get("compare_window", "-")))
    topn = html.escape(str(inputs.get("topn", "-")))
    recent_runs_json = json.dumps(recent_runs[-40:], ensure_ascii=False)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Open-AutoGLM Metrics Dashboard</title>
  <style>
    :root {{
      --bg: #f6f7f5;
      --panel: #ffffff;
      --ink: #1f2a28;
      --muted: #64726c;
      --line: #d7ddd9;
      --accent: #0f766e;
      --accent-soft: #d6f0ed;
      --warn: #b45309;
      --danger: #b91c1c;
      --ok: #166534;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: radial-gradient(1200px 700px at 85% -10%, #d9efe6 0%, var(--bg) 56%);
      font: 14px/1.45 "IBM Plex Sans", "Source Sans 3", "Noto Sans SC", "PingFang SC", "Helvetica Neue", sans-serif;
    }}
    .wrap {{ max-width: 1240px; margin: 0 auto; padding: 24px; }}
    .hero {{
      background: linear-gradient(135deg, #0f766e, #14532d);
      color: #eefcf9;
      border-radius: 16px;
      padding: 18px 20px;
      box-shadow: 0 14px 30px rgba(15, 118, 110, .18);
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: start;
    }}
    .hero h1 {{ margin: 0 0 4px; font-size: 22px; letter-spacing: .4px; }}
    .hero p {{ margin: 2px 0; opacity: .9; }}
    .lang-switch {{ display: inline-flex; gap: 6px; }}
    .lang-btn {{
      border: 1px solid rgba(238,252,249,.6);
      color: #eefcf9;
      background: transparent;
      border-radius: 999px;
      padding: 4px 10px;
      cursor: pointer;
      font: inherit;
    }}
    .lang-btn.active {{
      background: rgba(238,252,249,.18);
      border-color: #eefcf9;
    }}
    .section {{ margin-top: 18px; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 16px; box-shadow: 0 8px 20px rgba(20, 60, 44, .05); }}
    .section h2 {{ margin: 0 0 10px; font-size: 16px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }}
    .card {{ border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; background: #fbfdfc; transition: transform .16s ease, box-shadow .16s ease; }}
    .card:hover {{ transform: translateY(-1px); box-shadow: 0 8px 14px rgba(16, 84, 68, .12); }}
    .card-k {{ font-size: 12px; color: var(--muted); min-height: 18px; }}
    .card-v {{ font-size: 21px; font-weight: 700; margin-top: 3px; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    @media (max-width: 980px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    .subt {{ color: var(--muted); font-size: 12px; margin-bottom: 8px; }}
    .table-wrap {{ overflow: auto; max-height: 460px; border: 1px solid #e4ebe7; border-radius: 10px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; min-width: 680px; }}
    th, td {{ border-bottom: 1px solid var(--line); text-align: left; padding: 7px 6px; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 700; position: sticky; top: 0; background: #f4faf8; z-index: 1; }}
    tbody tr:nth-child(odd) {{ background: #fcfefd; }}
    tbody tr:hover {{ background: #eef9f4; }}
    .empty-cell, .empty {{ color: var(--muted); font-style: italic; padding: 8px 0; }}
    .bar-row {{ display: grid; grid-template-columns: 1.4fr 2fr 60px; align-items: center; gap: 8px; margin-bottom: 7px; }}
    .bar-label {{ font-size: 12px; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .bar-wrap {{ height: 10px; border-radius: 999px; background: #e5ece8; overflow: hidden; }}
    .bar-fill {{ height: 100%; background: linear-gradient(90deg, #0f766e, #14b8a6); }}
    .bar-count {{ text-align: right; color: var(--muted); font-size: 12px; }}
    .delta-up {{ color: var(--danger); font-weight: 700; }}
    .delta-down {{ color: var(--ok); font-weight: 700; }}
    .delta-flat {{ color: var(--muted); font-weight: 700; }}
    .delta-na {{ color: var(--warn); font-weight: 700; }}
    #trendSvg {{ width: 100%; height: 240px; border: 1px dashed #c8d1cc; border-radius: 8px; background: #fff; }}
    .legend {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 6px; color: var(--muted); font-size: 12px; }}
    .lg::before {{ content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }}
    .lg.duration::before {{ background: #0f766e; }}
    .lg.steps::before {{ background: #0369a1; }}
    .lg.tokens::before {{ background: #ca8a04; }}
    .mono {{ font-family: "JetBrains Mono", "SFMono-Regular", Menlo, Monaco, Consolas, monospace; word-break: break-all; }}
    .status-chip {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      border: 1px solid transparent;
      white-space: nowrap;
    }}
    .status-chip[data-status='finished_clean'] {{ color: #166534; background: #e9f9ef; border-color: #b9ebc9; }}
    .status-chip[data-status='finished_with_failures'] {{ color: #b45309; background: #fff3df; border-color: #f6d5a0; }}
    .status-chip[data-status='incomplete_or_failed'] {{ color: #b91c1c; background: #feeceb; border-color: #f4b6b6; }}
    .status-chip[data-status='unknown'] {{ color: #475569; background: #eef2f7; border-color: #d6dce5; }}
    .case-tools {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) 180px auto 1fr;
      gap: 8px;
      align-items: center;
      margin-bottom: 10px;
    }}
    .case-input, .case-select, .case-btn {{
      border: 1px solid #cdd8d2;
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      height: 34px;
      padding: 0 10px;
      font: inherit;
    }}
    .case-btn {{
      width: max-content;
      background: #eff8f5;
      border-color: #b8ddd3;
      cursor: pointer;
    }}
    .case-counter {{
      justify-self: end;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 860px) {{
      .case-tools {{
        grid-template-columns: 1fr 1fr;
      }}
      .case-counter {{
        justify-self: start;
      }}
    }}
    .foot {{ margin-top: 10px; color: var(--muted); font-size: 12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div>
        <h1 data-i18n="title"></h1>
        <p><span data-i18n="label.generated"></span>: {generated_at}</p>
        <p><span data-i18n="label.source_json"></span>: {html.escape(json_name)}</p>
      </div>
      <div class="lang-switch">
        <button id="btnEn" class="lang-btn" type="button">English</button>
        <button id="btnZh" class="lang-btn" type="button">中文</button>
      </div>
    </div>

    <div class="section">
      <h2 data-i18n="section.overview"></h2>
      <div class="cards">{card_html}</div>
    </div>

    <div class="section">
      <h2 data-i18n="section.trend"></h2>
      <div class="subt" data-i18n="subtitle.trend"></div>
      <svg id="trendSvg" viewBox="0 0 960 240" preserveAspectRatio="none"></svg>
      <div class="legend">
        <span class="lg duration" data-i18n="legend.duration"></span>
        <span class="lg steps" data-i18n="legend.steps"></span>
        <span class="lg tokens" data-i18n="legend.tokens"></span>
      </div>
    </div>

    <div class="grid">
      <div class="section">
        <h2 data-i18n="section.failures"></h2>
        {failure_bars}
      </div>
      <div class="section">
        <h2 data-i18n="section.optimization"></h2>
        <div class="subt" data-i18n="subtitle.optimization"></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th data-i18n="table.metric"></th><th data-i18n="table.previous"></th><th data-i18n="table.recent"></th><th data-i18n="table.change"></th></tr></thead>
            <tbody>{compare_rows}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="section">
      <h2 data-i18n="section.top_tasks"></h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th data-i18n="task.task_signature"></th><th data-i18n="task.runs"></th><th data-i18n="task.successes"></th><th data-i18n="task.success_rate"></th>
              <th data-i18n="task.error_rate"></th><th data-i18n="task.top_failure"></th><th data-i18n="task.count"></th>
            </tr>
          </thead>
          <tbody>{task_rows}</tbody>
        </table>
      </div>
    </div>

    <div class="section">
      <h2 data-i18n="section.case_details"></h2>
      <div class="subt" data-i18n="subtitle.case_details"></div>
      <div class="case-tools">
        <input id="caseSearch" class="case-input" type="search" data-ph-i18n="case.search_placeholder" />
        <select id="caseStatus" class="case-select">
          <option value="" data-i18n="case.filter_all"></option>
          <option value="finished_clean" data-i18n="status.finished_clean"></option>
          <option value="finished_with_failures" data-i18n="status.finished_with_failures"></option>
          <option value="incomplete_or_failed" data-i18n="status.incomplete_or_failed"></option>
          <option value="unknown" data-i18n="status.unknown"></option>
        </select>
        <button id="caseReset" class="case-btn" type="button" data-i18n="case.reset_filter"></button>
        <span id="caseCounter" class="case-counter"></span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th data-i18n="case.start_time"></th>
              <th data-i18n="case.task"></th>
              <th data-i18n="case.duration"></th>
              <th data-i18n="case.steps"></th>
              <th data-i18n="case.exec_fail"></th>
              <th data-i18n="case.semantic_fail"></th>
              <th data-i18n="case.finish_steps"></th>
              <th data-i18n="case.tokens"></th>
              <th data-i18n="case.status"></th>
              <th data-i18n="case.log_file"></th>
            </tr>
          </thead>
          <tbody id="caseTbody">{case_rows}</tbody>
        </table>
      </div>
    </div>

    <div class="section">
      <h2 data-i18n="section.data_scope"></h2>
      <div class="table-wrap">
        <table>
          <tbody>
            <tr><th data-i18n="scope.db_path"></th><td class="mono">{db_path}</td></tr>
            <tr><th data-i18n="scope.token_dir"></th><td class="mono">{token_usage_dir}</td></tr>
            <tr><th data-i18n="scope.topn"></th><td>{topn}</td></tr>
            <tr><th data-i18n="scope.compare_window"></th><td>{compare_window}</td></tr>
          </tbody>
        </table>
      </div>
      <div class="table-wrap" style="margin-top:10px;">
        <table>
          <thead><tr><th data-i18n="dict.metric"></th><th data-i18n="dict.meaning"></th><th data-i18n="dict.source"></th></tr></thead>
          <tbody>
            <tr><td data-i18n="dict.task_runs.k"></td><td data-i18n="dict.task_runs.v"></td><td data-i18n="dict.task_runs.s"></td></tr>
            <tr><td data-i18n="dict.task_success.k"></td><td data-i18n="dict.task_success.v"></td><td data-i18n="dict.task_success.s"></td></tr>
            <tr><td data-i18n="dict.action_success.k"></td><td data-i18n="dict.action_success.v"></td><td data-i18n="dict.action_success.s"></td></tr>
            <tr><td data-i18n="dict.semantic_failure.k"></td><td data-i18n="dict.semantic_failure.v"></td><td data-i18n="dict.semantic_failure.s"></td></tr>
            <tr><td data-i18n="dict.duration_steps_tokens.k"></td><td data-i18n="dict.duration_steps_tokens.v"></td><td data-i18n="dict.duration_steps_tokens.s"></td></tr>
            <tr><td data-i18n="dict.case_details.k"></td><td data-i18n="dict.case_details.v"></td><td data-i18n="dict.case_details.s"></td></tr>
          </tbody>
        </table>
      </div>
      <div class="foot"><span data-i18n="label.refresh_tip"></span>: <code>{html.escape(refresh_tip)}</code></div>
    </div>
  </div>

  <script>
    const runs = {recent_runs_json};
    const svg = document.getElementById('trendSvg');
    const W = 960, H = 240, PAD = 18;
    const I18N = {{
      en: {{
        title: "Open-AutoGLM Monitoring Dashboard",
        "label.generated": "Generated",
        "label.source_json": "Source JSON",
        "label.refresh_tip": "Refresh command",
        "section.overview": "Overview",
        "section.trend": "Run Trend (Last 40 Runs)",
        "subtitle.trend": "Each line is normalized independently for readability.",
        "section.failures": "Top Failure Reasons",
        "section.optimization": "Optimization Effect",
        "subtitle.optimization": "Recent window vs previous window.",
        "section.top_tasks": "Top Tasks",
        "section.case_details": "Recent Case Details (Last 20 Runs)",
        "subtitle.case_details": "Each row is a concrete run case from token logs.",
        "section.data_scope": "Data Scope and Metric Meaning",
        "table.metric": "Metric",
        "table.previous": "Previous",
        "table.recent": "Recent",
        "table.change": "Change",
        "legend.duration": "Duration",
        "legend.steps": "Steps",
        "legend.tokens": "Tokens",
        "metric.duration": "Duration (s)",
        "metric.steps": "Steps",
        "metric.tokens": "Tokens",
        "metric.semantic_fail_steps": "Semantic Fail Steps",
        "task.task_signature": "Task Signature",
        "task.runs": "Runs",
        "task.successes": "Successes",
        "task.success_rate": "Success Rate",
        "task.error_rate": "Error Rate",
        "task.top_failure": "Top Failure",
        "task.count": "Count",
        "case.start_time": "Start Time",
        "case.task": "Task",
        "case.duration": "Duration(s)",
        "case.steps": "Steps",
        "case.exec_fail": "Exec Fail",
        "case.semantic_fail": "Semantic Fail",
        "case.finish_steps": "Finish Steps",
        "case.tokens": "Tokens",
        "case.status": "Status",
        "case.log_file": "Log File",
        "case.search_placeholder": "Search task/log/start time...",
        "case.filter_all": "All statuses",
        "case.reset_filter": "Reset",
        "case.counter": "Showing {{visible}}/{{total}} cases",
        "status.finished_clean": "Finished (clean)",
        "status.finished_with_failures": "Finished (with failures)",
        "status.incomplete_or_failed": "Incomplete/Failed",
        "status.unknown": "Unknown",
        "scope.db_path": "Experience DB path",
        "scope.token_dir": "Token usage log dir",
        "scope.topn": "Top-N table limit",
        "scope.compare_window": "Comparison window size",
        "dict.metric": "Metric",
        "dict.meaning": "Meaning",
        "dict.source": "Source / Formula",
        "dict.task_runs.k": "Task Runs",
        "dict.task_runs.v": "Total number of historical task executions.",
        "dict.task_runs.s": "SUM(runs) from table task_outcome_stats in experience.db.",
        "dict.task_success.k": "Task Success/Error Rate",
        "dict.task_success.v": "Historical task-level pass/fail ratio.",
        "dict.task_success.s": "SUM(successes)/SUM(runs), and 1-success rate.",
        "dict.action_success.k": "Action Success Rate",
        "dict.action_success.v": "Step-level tool action success ratio.",
        "dict.action_success.s": "SUM(successes)/SUM(attempts) from action_stats.",
        "dict.semantic_failure.k": "Semantic Failure Rate",
        "dict.semantic_failure.v": "Steps that executed but failed semantically.",
        "dict.semantic_failure.s": "SUM(semantic_failures)/SUM(attempts) from action_stats.",
        "dict.duration_steps_tokens.k": "Duration/Steps/Tokens",
        "dict.duration_steps_tokens.v": "Runtime efficiency indicators per run.",
        "dict.duration_steps_tokens.s": "Parsed from artifacts/token_usage/token_usage_*.jsonl step events.",
        "dict.case_details.k": "Recent Case Details",
        "dict.case_details.v": "Concrete run samples with timestamps, task text, and failure counts.",
        "dict.case_details.s": "Last 20 rows from recent_runs in metrics_summary JSON.",
        "empty.compare": "Not enough runs for recent-vs-previous window comparison.",
        "empty.cases": "No recent run cases in token logs."
      }},
      zh: {{
        title: "Open-AutoGLM 监控看板",
        "label.generated": "生成时间",
        "label.source_json": "数据快照",
        "label.refresh_tip": "刷新命令",
        "section.overview": "总览",
        "section.trend": "运行趋势（最近40次）",
        "subtitle.trend": "三条线分别独立归一化，便于对比趋势。",
        "section.failures": "主要失败原因",
        "section.optimization": "优化效果",
        "subtitle.optimization": "最近窗口与前一窗口对比。",
        "section.top_tasks": "高频任务",
        "section.case_details": "案例明细（最近20次运行）",
        "subtitle.case_details": "每一行都是可追溯的真实运行案例（来自 token 日志）。",
        "section.data_scope": "数据范围与指标含义",
        "table.metric": "指标",
        "table.previous": "前一窗口",
        "table.recent": "最近窗口",
        "table.change": "变化",
        "legend.duration": "耗时",
        "legend.steps": "步数",
        "legend.tokens": "Token",
        "metric.duration": "耗时（秒）",
        "metric.steps": "步数",
        "metric.tokens": "Token",
        "metric.semantic_fail_steps": "语义失败步数",
        "task.task_signature": "任务签名",
        "task.runs": "运行次数",
        "task.successes": "成功次数",
        "task.success_rate": "成功率",
        "task.error_rate": "错误率",
        "task.top_failure": "主要失败原因",
        "task.count": "次数",
        "case.start_time": "开始时间",
        "case.task": "任务",
        "case.duration": "耗时(秒)",
        "case.steps": "步数",
        "case.exec_fail": "执行失败步数",
        "case.semantic_fail": "语义失败步数",
        "case.finish_steps": "完成动作步数",
        "case.tokens": "Token",
        "case.status": "状态",
        "case.log_file": "日志文件",
        "case.search_placeholder": "搜索任务/日志/开始时间...",
        "case.filter_all": "全部状态",
        "case.reset_filter": "重置",
        "case.counter": "显示 {{visible}}/{{total}} 条案例",
        "status.finished_clean": "已完成（无失败）",
        "status.finished_with_failures": "已完成（有失败）",
        "status.incomplete_or_failed": "未完成/失败",
        "status.unknown": "未知",
        "scope.db_path": "经验库 DB 路径",
        "scope.token_dir": "Token 日志目录",
        "scope.topn": "Top-N 行数限制",
        "scope.compare_window": "对比窗口大小",
        "dict.metric": "指标",
        "dict.meaning": "含义",
        "dict.source": "来源 / 计算方式",
        "dict.task_runs.k": "任务运行次数",
        "dict.task_runs.v": "历史任务执行总次数。",
        "dict.task_runs.s": "来自 experience.db 的 task_outcome_stats 表 SUM(runs)。",
        "dict.task_success.k": "任务成功率/错误率",
        "dict.task_success.v": "历史任务维度的通过与失败比例。",
        "dict.task_success.s": "SUM(successes)/SUM(runs)，错误率=1-成功率。",
        "dict.action_success.k": "动作成功率",
        "dict.action_success.v": "步骤级工具动作执行成功比例。",
        "dict.action_success.s": "来自 action_stats 表 SUM(successes)/SUM(attempts)。",
        "dict.semantic_failure.k": "语义失败率",
        "dict.semantic_failure.v": "动作执行了但语义不达标的比例。",
        "dict.semantic_failure.s": "来自 action_stats 表 SUM(semantic_failures)/SUM(attempts)。",
        "dict.duration_steps_tokens.k": "耗时/步数/Token",
        "dict.duration_steps_tokens.v": "每次运行的效率指标。",
        "dict.duration_steps_tokens.s": "解析 artifacts/token_usage/token_usage_*.jsonl 的 step 事件得到。",
        "dict.case_details.k": "案例明细",
        "dict.case_details.v": "带时间、任务文本、失败步数的真实运行样本。",
        "dict.case_details.s": "来自 metrics_summary JSON 中 recent_runs 的最近20条。",
        "empty.compare": "运行次数不足，暂时无法做前后窗口对比。",
        "empty.cases": "token 日志里暂无近期案例。"
      }}
    }};

    function pickLang() {{
      const q = new URLSearchParams(window.location.search).get('lang');
      if (q === 'zh' || q === 'en') return q;
      const cached = localStorage.getItem('dashboard_lang');
      if (cached === 'zh' || cached === 'en') return cached;
      return (navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en';
    }}

    function t(lang, key) {{
      return (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
    }}

    function fmtCounter(lang, visible, total) {{
      return t(lang, 'case.counter')
        .replace('{{visible}}', String(visible))
        .replace('{{total}}', String(total));
    }}

    let activeLang = 'en';

    function applyI18n(lang) {{
      activeLang = lang;
      document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
      localStorage.setItem('dashboard_lang', lang);
      document.querySelectorAll('[data-i18n]').forEach((el) => {{
        const k = el.getAttribute('data-i18n');
        el.textContent = t(lang, k);
      }});
      document.querySelectorAll('[data-ph-i18n]').forEach((el) => {{
        const k = el.getAttribute('data-ph-i18n');
        el.setAttribute('placeholder', t(lang, k));
      }});
      document.querySelectorAll('[data-status]').forEach((el) => {{
        const status = el.getAttribute('data-status');
        el.textContent = t(lang, `status.${{status}}`);
      }});
      document.getElementById('btnEn').classList.toggle('active', lang === 'en');
      document.getElementById('btnZh').classList.toggle('active', lang === 'zh');
      applyCaseFilters();
    }}

    function applyCaseFilters() {{
      const searchEl = document.getElementById('caseSearch');
      const statusEl = document.getElementById('caseStatus');
      const tbody = document.getElementById('caseTbody');
      const counter = document.getElementById('caseCounter');
      if (!searchEl || !statusEl || !tbody || !counter) {{
        return;
      }}
      const q = String(searchEl.value || '').trim().toLowerCase();
      const status = String(statusEl.value || '').trim();
      const rows = Array.from(tbody.querySelectorAll('tr'));
      let total = 0;
      let visible = 0;
      rows.forEach((row) => {{
        if (row.querySelector('.empty-cell')) {{
          return;
        }}
        total += 1;
        const blob = String(row.getAttribute('data-case-search') || '');
        const rowStatus = String(row.getAttribute('data-case-status') || '');
        const qMatch = !q || blob.includes(q);
        const sMatch = !status || rowStatus === status;
        const show = qMatch && sMatch;
        row.style.display = show ? '' : 'none';
        if (show) {{
          visible += 1;
        }}
      }});
      counter.textContent = fmtCounter(activeLang, visible, total);
    }}

    function buildSeries(key) {{
      return runs.map((r) => Number(r[key] || 0));
    }}
    function normalize(series) {{
      if (!series.length) return [];
      const min = Math.min(...series);
      const max = Math.max(...series);
      if (max === min) return series.map(() => 0.5);
      return series.map((v) => (v - min) / (max - min));
    }}
    function polyline(norm, color) {{
      if (!norm.length) return '';
      const step = (W - PAD * 2) / Math.max(1, norm.length - 1);
      const pts = norm.map((v, i) => {{
        const x = PAD + i * step;
        const y = PAD + (1 - v) * (H - PAD * 2);
        return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
      }}).join(' ');
      return `<polyline points="${{pts}}" fill="none" stroke="${{color}}" stroke-width="2"/>`;
    }}
    function renderTrend() {{
      const duration = normalize(buildSeries('duration_sec'));
      const steps = normalize(buildSeries('step_count'));
      const tokens = normalize(buildSeries('tokens_total'));
      const grid = [];
      for (let i = 0; i <= 4; i++) {{
        const y = PAD + i * (H - PAD * 2) / 4;
        grid.push(`<line x1="${{PAD}}" y1="${{y}}" x2="${{W - PAD}}" y2="${{y}}" stroke="#edf1ee" stroke-width="1"/>`);
      }}
      svg.innerHTML = [
        `<rect x="0" y="0" width="${{W}}" height="${{H}}" fill="transparent"/>`,
        ...grid,
        polyline(duration, '#0f766e'),
        polyline(steps, '#0369a1'),
        polyline(tokens, '#ca8a04'),
      ].join('');
    }}

    const lang = pickLang();
    applyI18n(lang);
    renderTrend();
    document.getElementById('btnEn').addEventListener('click', () => applyI18n('en'));
    document.getElementById('btnZh').addEventListener('click', () => applyI18n('zh'));
    document.getElementById('caseSearch').addEventListener('input', applyCaseFilters);
    document.getElementById('caseStatus').addEventListener('change', applyCaseFilters);
    document.getElementById('caseReset').addEventListener('click', () => {{
      document.getElementById('caseSearch').value = '';
      document.getElementById('caseStatus').value = '';
      applyCaseFilters();
    }});
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build HTML dashboard from metrics summary JSON.")
    parser.add_argument(
        "--reports-dir",
        default=str(_default_reports_dir()),
        help="Directory containing metrics_summary_*.json files.",
    )
    parser.add_argument(
        "--metrics-json",
        default="",
        help="Path to metrics_summary_*.json. If empty, latest file in reports dir is used.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh metrics first by running monitoring-dashboard/tools/metrics_report.py.",
    )
    parser.add_argument(
        "--out-file",
        default=str(_default_out_file()),
        help="Output HTML path.",
    )
    args = parser.parse_args()

    module_root = _module_root()
    repo_root = _repo_root()
    reports_dir = Path(args.reports_dir).expanduser()
    reports_dir.mkdir(parents=True, exist_ok=True)

    metrics_path: Path | None = None
    if args.refresh:
        metrics_path = _run_metrics_report(module_root, repo_root, reports_dir)
    if not metrics_path and args.metrics_json:
        metrics_path = Path(args.metrics_json).expanduser()
    if not metrics_path:
        metrics_path = _latest_metrics_json(reports_dir)
    if not metrics_path or not metrics_path.exists():
        raise SystemExit(
            "No metrics_summary JSON found. Run monitoring-dashboard/tools/metrics_report.py first."
        )

    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    out_file = Path(args.out_file).expanduser()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    refresh_tip = (
        "cd monitoring-dashboard && "
        "python tools/metrics_dashboard.py --refresh --reports-dir data --out-file public/metrics_dashboard.html"
    )
    out_file.write_text(
        _build_dashboard_html(payload, metrics_path.name, refresh_tip=refresh_tip),
        encoding="utf-8",
    )
    print(f"Dashboard HTML: {out_file}")


if __name__ == "__main__":
    main()
