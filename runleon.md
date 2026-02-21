# RunLeon 快速上手（`agop` 设备前缀版）

本文档用于团队成员在 Leon 机器上快速跑通 `Open-AutoGLM`，统一使用 `agop` 前缀（Oppo 设备位）。

## 1. 核心原则

- 运行依赖环境变量（模型网关、模型名、Provider、API Key、设备 ID、坐标补偿配置）。
- 不手动 `export`，统一通过前缀注入。
- 当前设备前缀统一为：`agop`（无连接符）。

推荐命令：

```bash
PATH="$PWD/venv/bin:$PATH" agop python main.py "打开微信"
```

## 2. 当前 Leon 设备配置（`agop`）

`agop` 从 `~/.env-prefix/agop.env` 读取配置。当前建议变量如下：

```bash
PHONE_AGENT_BASE_URL="https://apileon.leonai.top/api"
PHONE_AGENT_MODEL="claude-sonnet-4-5-20250929"
PHONE_AGENT_PROVIDER="anthropic"
PHONE_AGENT_ANTHROPIC_VERSION="2023-06-01"
PHONE_AGENT_API_KEY="<在本机 agop.env 维护>"
PHONE_AGENT_DEVICE_ID="AMBNUT3926006417"
PHONE_AGENT_COORD_PROFILE_FILE="$HOME/.openautoglm/coord_profiles.json"
ENV_PREFIX_REQUIRED="PHONE_AGENT_BASE_URL,PHONE_AGENT_MODEL,PHONE_AGENT_API_KEY,PHONE_AGENT_PROVIDER,PHONE_AGENT_DEVICE_ID"
```

说明：

- `PHONE_AGENT_DEVICE_ID` 固化后可直接省略 `--device-id`。
- 坐标补偿默认走 profile 文件自动加载（按 `device_id + provider + model`）。
- 当前设备最新补偿（已写入 `~/.openautoglm/coord_profiles.json`）：
  - `device_id`: `AMBNUT3926006417`
  - `provider::model`: `anthropic::claude-sonnet-4-5-20250929`
  - `scale_x`: `1.5169811320754716`
  - `scale_y`: `1.5248226950354609`
  - 实测精度（4x4，2026-02-21）：`mean=4.46px`，`p95=7.07px`，`max=7.07px`

## 3. 首次配置（团队成员）

### 3.1 准备项目

```bash
git clone https://github.com/LeonSGP43/Open-AutoGLM.git
cd Open-AutoGLM
python3.13 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3.2 安装 env-prefix 并启用 `agop`

```bash
cd /Users/leon/Desktop/env_man/env-ccPrefix
./install.sh
```

如需新机器手动创建：

```bash
cat > /Users/leon/Desktop/env_man/env-ccPrefix/config/agop.env <<'EOF'
export PHONE_AGENT_BASE_URL="https://apileon.leonai.top/api"
export PHONE_AGENT_MODEL="claude-sonnet-4-5-20250929"
export PHONE_AGENT_API_KEY="替换成你的key"
export PHONE_AGENT_PROVIDER="anthropic"
export PHONE_AGENT_ANTHROPIC_VERSION="2023-06-01"
export PHONE_AGENT_DEVICE_ID="AMBNUT3926006417"
export PHONE_AGENT_COORD_PROFILE_FILE="$HOME/.openautoglm/coord_profiles.json"
export ENV_PREFIX_REQUIRED="PHONE_AGENT_BASE_URL,PHONE_AGENT_MODEL,PHONE_AGENT_API_KEY,PHONE_AGENT_PROVIDER,PHONE_AGENT_DEVICE_ID"
EOF
```

## 4. 验证前缀是否生效

```bash
command -v agop
agop env | rg '^PHONE_AGENT_'
```

预期：看到 `PHONE_AGENT_BASE_URL / MODEL / PROVIDER / API_KEY / DEVICE_ID / COORD_PROFILE_FILE`。

## 5. 当前设备启动方式（更新后）

### 常用命令

```bash
cd /path/to/Open-AutoGLM

# 列设备
PATH="$PWD/venv/bin:$PATH" agop python main.py --list-devices

# 直接跑任务（默认走 PHONE_AGENT_DEVICE_ID）
PATH="$PWD/venv/bin:$PATH" agop python main.py "打开微信在公众号里搜索新年文章"

# 临时覆盖设备（可选）
PATH="$PWD/venv/bin:$PATH" agop python main.py --device-id <other_device_id> "打开微信"
```

## 6. 未来系统使用方式（多设备推荐）

新手机接入请优先看：

- `docs/new_device_calibration.md`

### 6.1 一机一前缀（推荐）

- 每个设备一个前缀文件：例如 `agop.env`、`agmi.env`、`agvivo.env`。
- 每个前缀固定 `PHONE_AGENT_DEVICE_ID`，并共用或分开 `PHONE_AGENT_COORD_PROFILE_FILE`。

### 6.2 首次接入新设备：先标定再使用

1. 跑 AI 闭环点击精度测试：

```bash
PATH="$PWD/venv/bin:$PATH" agop python scripts/coord_calibration/calc_ai_coord_scale.py \
  --device-id <adb_device_id> \
  --rows 3 --cols 3
```

默认输出固定到：

- `artifacts/coord_calibration/ai_click_accuracy_report.json`
- `artifacts/coord_calibration/ai_click_overlays/`

2. 将报告写入 profile：

```bash
PATH="$PWD/venv/bin:$PATH" python scripts/coord_calibration/save_coord_profile.py \
  --report artifacts/coord_calibration/ai_click_accuracy_report.json \
  --device-id <adb_device_id> \
  --provider anthropic \
  --model claude-sonnet-4-5-20250929
```

3. 之后正常跑任务即可自动补偿：

```bash
PATH="$PWD/venv/bin:$PATH" agop python main.py "你的任务"
```

查看当前 profile：

```bash
cat ~/.openautoglm/coord_profiles.json
```

### 6.3 什么时候需要重新标定

- 换手机
- 换模型 / Provider
- 系统大版本升级后出现明显偏点
- 屏幕分辨率或显示缩放策略变化

## 7. 快速排障

- 设备 `unauthorized`：

```bash
adb devices -l
```

在手机上重新允许 USB 调试后重试。

- 任务中持续点偏：
  1. 先跑 `scripts/coord_calibration/calc_ai_coord_scale.py` 看 `mean/p95`。
  2. 再执行 `scripts/coord_calibration/save_coord_profile.py` 落盘补偿。
  3. 确认 `PHONE_AGENT_COORD_PROFILE_FILE` 指向正确文件。

## 8. 安全规范

- 不要把真实 `PHONE_AGENT_API_KEY` 提交到 Git 仓库。
- 推荐仅在本机 `~/.env-prefix/agop.env` 保存密钥。
