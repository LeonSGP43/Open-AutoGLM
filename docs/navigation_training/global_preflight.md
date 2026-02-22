# 全局前置规则（所有开图任务通用）

## 1. 运行参数

训练阶段建议固定使用：

```bash
PATH="$PWD/venv/bin:$PATH" agop python main.py \
  --training-mode \
  --takeover-policy auto \
  --navigation-map \
  --no-navigation-fast-path \
  "任务描述"
```

## 2. 设备与环境检查

```bash
command -v agop
agop env | rg '^PHONE_AGENT_'
PATH="$PWD/venv/bin:$PATH" agop python main.py --list-devices
```

## 3. 横屏归一化

- 系统已接入 `orientation-lock-normalization` 任务技能。
- 每次任务开始会先检查横屏/自动旋转状态，必要时先关闭自动旋转并回到竖屏。
- 若 3 次尝试仍无法恢复竖屏，可触发 `Take_over` 人工纠偏后继续。

## 4. 执行与复盘建议

1. 同一任务连续执行 3~5 次。
2. 偏航时优先接管纠偏，不要直接中断。
3. 尽量跑到 `finish`，确保经验库与导航图写入完整。
4. 记录 `PASS/SKIP/FAIL`，其中“App 不存在”统一记为 `SKIP`。

## 5. 数据观测（可选）

```bash
sqlite3 ~/.openautoglm/navigation_map.db \
  "select count(*) as states from navigation_states; \
   select count(*) as transitions from state_transitions;"

ls -lt artifacts/token_usage | head -n 10
```
