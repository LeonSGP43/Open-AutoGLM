# Clash 任务集（可选）

## 适用范围

- App 名称：Clash
- Android 包名：`com.github.kr328.clash`

## 前置检查

```bash
adb -s <device_id> shell pm list packages | rg -i '^package:com.github.kr328.clash$'
```

- 命中：执行本目录任务
- 未命中：该 App 全部任务标记 `SKIP`

## 任务列表

1. `docs/navigation_training/apps/clash/01_open_home.md`
2. `docs/navigation_training/apps/clash/02_enable_proxy.md`
3. `docs/navigation_training/apps/clash/03_disable_proxy.md`
4. `docs/navigation_training/apps/clash/04_switch_node.md`

## 执行建议

- 建议顺序：01 -> 02 -> 03 -> 04
- 每个任务建议连续跑 3~5 次，累积稳定转移样本
