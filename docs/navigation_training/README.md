# 导航建图训练任务集（模块化）

本目录用于“有目的的开图训练”。  
设计原则：**一个任务一个文档**、**存在该 App 才执行，不存在就跳过**。

## 使用方式

1. 先看全局前置规则：`docs/navigation_training/global_preflight.md`
2. 再进入具体 App 目录，按任务编号执行（建议每个任务连续 3~5 次）
3. 训练阶段统一使用：

```bash
PATH="$PWD/venv/bin:$PATH" agop python main.py \
  --training-mode \
  --takeover-policy auto \
  --navigation-map \
  --no-navigation-fast-path \
  "你的任务"
```

## 任务索引

- Clash
  - 总览：`docs/navigation_training/apps/clash/README.md`
  - 任务 01：`docs/navigation_training/apps/clash/01_open_home.md`
  - 任务 02：`docs/navigation_training/apps/clash/02_enable_proxy.md`
  - 任务 03：`docs/navigation_training/apps/clash/03_disable_proxy.md`
  - 任务 04：`docs/navigation_training/apps/clash/04_switch_node.md`

## 跳过策略（必读）

- 每个 App 任务都带有“包名前置检查”。
- 若设备不存在该 App，则记录 `SKIP` 并继续下一个任务，不视为失败。
- 示例（Clash）：

```bash
adb -s <device_id> shell pm list packages | rg -i '^package:com.github.kr328.clash$'
```

## 说明

- 系统已内置 `orientation-lock-normalization` 任务技能：任务启动会先做横竖屏与自动旋转归一化（必要时先恢复竖屏，再继续业务动作）。
