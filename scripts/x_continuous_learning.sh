#!/usr/bin/env bash
set -euo pipefail

# Continuous X learning runner.
# Repeatedly collects mixed-media posts and exports learning artifacts.
#
# Usage:
#   scripts/x_continuous_learning.sh            # default 3 rounds
#   scripts/x_continuous_learning.sh 5          # 5 rounds
#
# Notes:
# - Uses agop profile and default bound device.
# - Enforces anti-stall behavior via prompt constraints.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

ROUNDS="${1:-3}"
if ! [[ "$ROUNDS" =~ ^[0-9]+$ ]] || [[ "$ROUNDS" -lt 1 ]]; then
  echo "Invalid rounds: $ROUNDS"
  exit 1
fi

mkdir -p artifacts/logs

if ! command -v agop >/dev/null 2>&1; then
  echo "agop not found in PATH"
  exit 1
fi

run_cmd() {
  PATH="$PWD/venv/bin:$PATH" agop python main.py "$@"
}

echo "== X Continuous Learning: rounds=$ROUNDS =="

for round in $(seq 1 "$ROUNDS"); do
  ts="$(date +%Y%m%d_%H%M%S)"
  prep_log="artifacts/logs/x_learn_round${round}_prep_${ts}.log"
  learn_log="artifacts/logs/x_learn_round${round}_collect_${ts}.log"

  echo ""
  echo "== Round $round/$ROUNDS: prep @elonmusk posts =="
  run_cmd \
    --max-steps 70 \
    --takeover-policy never \
    --no-experience-fast-path \
    "进入X应用@elonmusk主页并停留在Posts列表页（非详情页）。若不在X则先回到X。完成后finish返回OK_READY_ELON_POSTS。" \
    | tee "$prep_log"

  echo ""
  echo "== Round $round/$ROUNDS: mixed-media learning =="
  run_cmd \
    --max-steps 120 \
    --takeover-policy never \
    --no-experience-fast-path \
    "在X应用@elonmusk主页执行持续学习第${round}轮：采集3条不同媒体类型帖子（优先text/image/video）。硬约束：1) 每条都进详情先执行 do(action=\"Note\", message=\"x_post_meta idx=1\")/idx=2/idx=3；2) 评论处理规则：每条最多一次Swipe查看评论，若评论不可见立即按null处理并继续，禁止再次Swipe；3) 视频帖特殊规则：不要在视频播放器层滑动找评论，先点击作者/正文/时间进入线程详情层，若仍看不到评论则直接null；4) 同一界面同一动作不超过2次；5) 任一方法连续失败3次立即结束本轮并finish(message=\"need_takeover\"); 6) 三条完成后必须执行 do(action=\"Call_API\", instruction=\"x_export_to_download\") 再finish(message=\"x_learning_round_${round}_done\")。" \
    | tee "$learn_log"

  latest_rules="artifacts/x_extract/x_learning_rules.json"
  if [[ -f "$latest_rules" ]]; then
    echo "-- Learning snapshot after round $round --"
    jq '{updated_at,total_events,top_rules:(.rules[:3])}' "$latest_rules" || true
  fi
done

echo ""
echo "== Continuous learning finished =="
echo "Rules: artifacts/x_extract/x_learning_rules.json"
