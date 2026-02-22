# Clash 任务 01：打开并停留主页

## 目标

- 打开 Clash
- 停留在 Clash 主页并 `finish`

## 前置检查（存在才执行）

```bash
adb -s <device_id> shell pm list packages | rg -i '^package:com.github.kr328.clash$'
```

- 未命中：记 `SKIP`，跳过本任务

## 执行命令

```bash
PATH="$PWD/venv/bin:$PATH" agop python main.py \
  --training-mode --takeover-policy auto --navigation-map --no-navigation-fast-path \
  "打开Clash并停留在主页"
```

## 成功判定

- Agent 输出 `finish`
- 结果语义明确表示“已在 Clash 主页”

## 建议重复次数

- 3~5 次
