#!/usr/bin/env bash
set -euo pipefail

# Collect Musk posts from current @elonmusk Posts list state.
# Usage:
#   scripts/x_collect_musk_posts.sh 3 10
# This collects post_index [3..10], one by one, with up to 3 attempts each.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

START_INDEX="${1:-3}"
END_INDEX="${2:-10}"

if ! command -v agop >/dev/null 2>&1; then
  echo "agop not found in PATH"
  exit 1
fi

mkdir -p artifacts/logs artifacts/x_musk_posts

run_one_attempt() {
  local idx="$1"
  local attempt="$2"
  local ts log_file raw result_file
  ts="$(date +%Y%m%d_%H%M%S)"
  log_file="artifacts/logs/x_musk_post$(printf '%02d' "$idx")_attempt${attempt}_${ts}.log"
  result_file="artifacts/x_musk_posts/post_$(printf '%02d' "$idx").json"

  local prompt
  prompt="你现在在@elonmusk主页Posts列表页。先在列表中部向上滑动一次，使下一条帖子成为顶部。然后采集当前最上方帖子作为post_index=${idx}：1) 打开帖子详情；2) 提取post_text、post_time、heat{replies,reposts,likes,bookmarks,views}；3) 不允许在评论区继续下滑，只提取当前屏幕可见前3条评论top_comments[{comment_text,comment_heat}]，不可见填null；4) 返回列表页。硬约束：同一界面不得重复同一动作超过2次，若发现动作循环或无法继续，立即finish输出need_takeover=true。完成后finish输出严格JSON：{\\\"need_takeover\\\":false,\\\"post\\\":{\\\"post_index\\\":${idx},\\\"post_text\\\":\\\"...\\\",\\\"post_time\\\":\\\"...\\\",\\\"heat\\\":{\\\"replies\\\":\\\"...\\\",\\\"reposts\\\":\\\"...\\\",\\\"likes\\\":\\\"...\\\",\\\"bookmarks\\\":\\\"...\\\",\\\"views\\\":\\\"...\\\"},\\\"top_comments\\\":[{\\\"comment_text\\\":\\\"...\\\",\\\"comment_heat\\\":\\\"...\\\"}]}}。若失败输出{\\\"need_takeover\\\":true,\\\"post\\\":null,\\\"error\\\":\\\"...\\\"}。"

  echo "== post ${idx} attempt ${attempt} =="
  PATH="$PWD/venv/bin:$PATH" agop python main.py \
    --max-steps 45 \
    --takeover-policy never \
    --no-experience-fast-path \
    "$prompt" | tee "$log_file"

  raw="$(sed -n 's/^Result: //p' "$log_file" | tail -n1 || true)"
  if [[ -z "$raw" ]]; then
    echo "no Result line found"
    return 1
  fi

  # Decode escaped JSON object while preserving embedded escaped quotes in text.
  printf '%s' "$raw" \
    | sed 's/\\\\\\\"/__ESC_Q__/g; s/\\"/"/g; s/__ESC_Q__/\\"/g' \
    > "$result_file.tmp"
  if jq . "$result_file.tmp" >/dev/null 2>&1; then
    mv "$result_file.tmp" "$result_file"
    echo "saved: $result_file"
    return 0
  fi

  echo "invalid JSON result"
  rm -f "$result_file.tmp"
  return 1
}

for idx in $(seq "$START_INDEX" "$END_INDEX"); do
  ok=0
  for attempt in 1 2 3; do
    if run_one_attempt "$idx" "$attempt"; then
      ok=1
      break
    fi
    sleep 1
  done

  if [[ "$ok" -eq 0 ]]; then
    fallback_file="artifacts/x_musk_posts/post_$(printf '%02d' "$idx").json"
    cat > "$fallback_file" <<EOF
{"need_takeover":true,"post":null,"error":"post_${idx} failed after 3 attempts"}
EOF
    echo "saved fallback: $fallback_file"
    break
  fi
done

echo "done"
