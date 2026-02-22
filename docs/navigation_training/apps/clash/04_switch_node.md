# Clash 任务 04：切换节点并回主页

## 目标

- 打开 Clash
- 进入“代理”页面
- 切换到另一个可用节点
- 返回 Clash 主页并 `finish`

## 前置检查（存在才执行）

```bash
adb -s <device_id> shell pm list packages | rg -i '^package:com.github.kr328.clash$'
```

- 未命中：记 `SKIP`，跳过本任务

## 执行命令

```bash
PATH="$PWD/venv/bin:$PATH" agop python main.py \
  --training-mode --takeover-policy auto --navigation-map --no-navigation-fast-path \
  "打开Clash，进入代理页面并切换到另一个可用节点，然后返回Clash主页并结束任务"
```

## 成功判定

- Agent 输出 `finish`
- 结果语义明确表示“已切换节点并返回主页”

## 失败处理

- 若节点组为空/异常，可 `Take_over` 手动选择可用组后继续

## 建议重复次数

- 3~5 次
