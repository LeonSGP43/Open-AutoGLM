# Clash 任务 02：开启代理

## 目标

- 打开 Clash
- 若状态为“已停止”，切换到“运行中”
- 停留主页并 `finish`

## 前置检查（存在才执行）

```bash
adb -s <device_id> shell pm list packages | rg -i '^package:com.github.kr328.clash$'
```

- 未命中：记 `SKIP`，跳过本任务

## 执行命令

```bash
PATH="$PWD/venv/bin:$PATH" agop python main.py \
  --training-mode --takeover-policy auto --navigation-map --no-navigation-fast-path \
  "打开Clash，如果代理为已停止则开启代理；完成后停留在Clash主页并结束任务"
```

## 成功判定

- Agent 输出 `finish`
- 结果语义明确表示“已运行中/已开启代理”

## 失败处理

- 若卡在非主页或误入子页：优先 `Take_over` 人工纠偏后继续

## 建议重复次数

- 3~5 次
