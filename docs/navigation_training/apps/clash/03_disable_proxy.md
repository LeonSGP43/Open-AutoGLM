# Clash 任务 03：关闭代理

## 目标

- 打开 Clash
- 若状态为“运行中”，切换到“已停止”
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
  "打开Clash，如果代理为运行中则关闭代理；完成后停留在Clash主页并结束任务"
```

## 成功判定

- Agent 输出 `finish`
- 结果语义明确表示“已停止/已关闭代理”

## 建议重复次数

- 3~5 次
