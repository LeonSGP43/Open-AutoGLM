# 新手机接入标定指南（坐标补偿）

适用场景：

- 新增一台 Android 手机接入 Open-AutoGLM
- 更换了模型或 Provider（例如从 `anthropic` 切到 `openai`）
- 设备系统升级后出现明显“点偏中间/点偏下方”等现象

目标：

- 先做闭环精度测试
- 自动产出并落盘补偿参数
- 让后续任务运行时自动加载补偿（无需每次手写缩放参数）

---

## 1. 前置条件

1. 设备已连接并授权 USB 调试
2. 项目依赖已安装
3. 能使用你的环境前缀（如 `agop`）

快速检查：

```bash
adb devices -l
PATH="$PWD/venv/bin:$PATH" agop python main.py --list-devices
```

---

## 2. 运行 AI 闭环点击测试

建议先用 `3x3`：

```bash
PATH="$PWD/venv/bin:$PATH" agop python scripts/coord_calibration/calc_ai_coord_scale.py \
  --device-id <adb_device_id> \
  --rows 3 --cols 3
```

输出包含：

- 报告文件：`artifacts/coord_calibration/ai_click_accuracy_report.json`
- 可视化图片目录：`artifacts/coord_calibration/ai_click_overlays/`

---

## 3. 提取并保存补偿参数

将报告写入 profile（按 `device_id + provider + model` 存储）：

```bash
PATH="$PWD/venv/bin:$PATH" python scripts/coord_calibration/save_coord_profile.py \
  --report artifacts/coord_calibration/ai_click_accuracy_report.json \
  --device-id <adb_device_id> \
  --provider anthropic \
  --model claude-sonnet-4-5-20250929
```

默认写入：

`~/.openautoglm/coord_profiles.json`

---

## 4. 运行时自动加载补偿

在前缀环境（如 `agop.env`）中配置：

```bash
export PHONE_AGENT_DEVICE_ID="<adb_device_id>"
export PHONE_AGENT_COORD_PROFILE_FILE="$HOME/.openautoglm/coord_profiles.json"
```

然后直接跑任务：

```bash
PATH="$PWD/venv/bin:$PATH" agop python main.py "你的任务"
```

系统会自动按 `(device_id, provider, model)` 加载补偿。

---

## 5. 如何判断是否标定成功

建议阈值（可按业务调整）：

- `hit_rate >= 0.95`
- `p95_distance_px <= 15`（一般业务）

实战经验：

- `p95 <= 5~8px`：可认为非常稳定
- `p95 > 30px`：通常存在坐标系不一致或页面缩放问题，应重新标定

参考：Leon 当前设备（`AMBNUT3926006417`）在 `anthropic::claude-sonnet-4-5-20250929` 下，
最新实测（4x4）为：

- `mean_distance_px = 4.46`
- `p95_distance_px = 7.07`
- `max_distance_px = 7.07`

对应 profile 系数：

- `scale_x = 1.5169811320754716`
- `scale_y = 1.5248226950354609`

---

## 6. 常见问题

1. 为什么每台手机都可能不同？
- 分辨率、系统缩放、厂商渲染与浏览器视口策略不同，都会影响模型坐标空间。

2. 为什么同一手机换模型也要重标定？
- 模型可能使用不同视觉坐标基准；补偿是“设备 + 模型”联合属性。

3. 可以手动覆盖吗？
- 可以，使用：
  - `PHONE_AGENT_MODEL_COORD_SCALE_X`
  - `PHONE_AGENT_MODEL_COORD_SCALE_Y`
- 手动值优先级高于 profile 文件。

4. 什么时候需要重新标定？
- 换手机、换模型、系统大版本更新、或出现稳定偏点时。

---

## 7. 推荐接入流程（新手机）

1. 连接设备并确认 `adb devices -l`
2. 运行 `scripts/coord_calibration/calc_ai_coord_scale.py`
3. 运行 `scripts/coord_calibration/save_coord_profile.py`
4. 在对应前缀 `.env` 中设置 `PHONE_AGENT_DEVICE_ID` + `PHONE_AGENT_COORD_PROFILE_FILE`
5. 用 1~2 个真实任务验证（如微信搜索、短视频应用操作）
