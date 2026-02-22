#!/usr/bin/env python3
"""
Phone Agent CLI - AI-powered phone automation.

Usage:
    python main.py [OPTIONS]

Environment Variables:
    PHONE_AGENT_BASE_URL: Model API base URL (default: http://localhost:8000/v1)
    PHONE_AGENT_MODEL: Model name (default: autoglm-phone-9b)
    PHONE_AGENT_API_KEY: API key for model authentication (default: EMPTY)
    PHONE_AGENT_PROVIDER: Model API provider format (default: openai)
    PHONE_AGENT_ANTHROPIC_VERSION: Anthropic API version header (default: 2023-06-01)
    PHONE_AGENT_MAX_STEPS: Maximum steps per task (default: 100)
    PHONE_AGENT_DEVICE_ID: ADB device ID for multi-device setups
    PHONE_AGENT_SAVE_TOKEN_USAGE: Save per-step token usage logs (default: true)
    PHONE_AGENT_TOKEN_USAGE_DIR: Directory for token usage logs (default: artifacts/token_usage)
    PHONE_AGENT_EXPERIENCE_FAST_PATH: Enable high-confidence fast path (default: true)
    PHONE_AGENT_EXPERIENCE_FAST_PATH_CONFIDENCE: Min confidence for fast path (default: 0.72)
    PHONE_AGENT_EXPERIENCE_FAST_PATH_MIN_ATTEMPTS: Min attempts for fast path (default: 3)
    PHONE_AGENT_EXPERIENCE_FAST_PATH_MAX_STREAK: Max consecutive fast-path steps (default: 4)
    PHONE_AGENT_EXPERIENCE_EXPLORATION_RATE: Prob. to skip fast path for exploration (default: 0.08)
    PHONE_AGENT_EXPERIENCE_FAST_PATH_EXACT_ONLY: Use fast path only for exact screen-state matches (default: true)
    PHONE_AGENT_EXPERIENCE_SENSITIVE_GATE: Block fast path for sensitive tap/type contexts (default: true)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional when using anthropic provider only
    OpenAI = None

from phone_agent import PhoneAgent
from phone_agent.agent import AgentConfig
from phone_agent.agent_ios import IOSAgentConfig, IOSPhoneAgent
from phone_agent.config.apps import list_supported_apps
from phone_agent.config.apps_harmonyos import list_supported_apps as list_harmonyos_apps
from phone_agent.config.apps_ios import list_supported_apps as list_ios_apps
from phone_agent.device_factory import DeviceType, get_device_factory, set_device_type
from phone_agent.model import ModelConfig
from phone_agent.xctest import XCTestConnection
from phone_agent.xctest import list_devices as list_ios_devices

DEFAULT_HTTP_USER_AGENT = "Open-AutoGLM/0.1"


def env_flag(name: str, default: bool = False) -> bool:
    """Parse boolean environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def env_int(name: str, default: int, minimum: int = 0) -> int:
    """Parse integer environment variable with lower bound."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def env_float(name: str, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    """Parse float environment variable clamped to [minimum, maximum]."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def build_takeover_callback(lang: str = "cn"):
    """Build manual takeover callback with explicit start/end markers."""

    def _callback(message: str) -> None:
        print("\n" + "=" * 50)
        if lang == "en":
            print("[takeover] Manual intervention requested by agent.")
            print(f"[takeover] Reason: {message}")
            answer = input(
                "[takeover] Operate on the phone now. Press Enter to resume AI (or type 'abort' to stop): "
            ).strip()
        else:
            print("[takeover] Agent 请求人工干预。")
            print(f"[takeover] 原因: {message}")
            answer = input(
                "[takeover] 请在手机上手动操作，完成后回车继续（输入 abort 可终止）: "
            ).strip()
        if answer.lower() in {"abort", "quit", "exit", "q"}:
            raise KeyboardInterrupt("Manual takeover aborted by user.")
        if lang == "en":
            print("[takeover] Manual intervention finished, resuming agent.")
        else:
            print("[takeover] 人工干预结束，继续由 Agent 执行。")
        print("=" * 50 + "\n")

    return _callback


def check_system_requirements(
    device_type: DeviceType = DeviceType.ADB, wda_url: str = "http://localhost:8100"
) -> bool:
    """
    Check system requirements before running the agent.

    Checks:
    1. ADB/HDC/iOS tools installed
    2. At least one device connected
    3. ADB Keyboard installed on the device (for ADB only)
    4. WebDriverAgent running (for iOS only)

    Args:
        device_type: Type of device tool (ADB, HDC, or IOS).
        wda_url: WebDriverAgent URL (for iOS only).

    Returns:
        True if all checks pass, False otherwise.
    """
    print("🔍 Checking system requirements...")
    print("-" * 50)

    all_passed = True

    # Determine tool name and command
    if device_type == DeviceType.IOS:
        tool_name = "libimobiledevice"
        tool_cmd = "idevice_id"
    else:
        tool_name = "ADB" if device_type == DeviceType.ADB else "HDC"
        tool_cmd = "adb" if device_type == DeviceType.ADB else "hdc"

    # Check 1: Tool installed
    print(f"1. Checking {tool_name} installation...", end=" ")
    if shutil.which(tool_cmd) is None:
        print("❌ FAILED")
        print(f"   Error: {tool_name} is not installed or not in PATH.")
        print(f"   Solution: Install {tool_name}:")
        if device_type == DeviceType.ADB:
            print("     - macOS: brew install android-platform-tools")
            print("     - Linux: sudo apt install android-tools-adb")
            print(
                "     - Windows: Download from https://developer.android.com/studio/releases/platform-tools"
            )
        elif device_type == DeviceType.HDC:
            print(
                "     - Download from HarmonyOS SDK or https://gitee.com/openharmony/docs"
            )
            print("     - Add to PATH environment variable")
        else:  # IOS
            print("     - macOS: brew install libimobiledevice")
            print("     - Linux: sudo apt-get install libimobiledevice-utils")
        all_passed = False
    else:
        # Double check by running version command
        try:
            if device_type == DeviceType.ADB:
                version_cmd = [tool_cmd, "version"]
            elif device_type == DeviceType.HDC:
                version_cmd = [tool_cmd, "-v"]
            else:  # IOS
                version_cmd = [tool_cmd, "-ln"]

            result = subprocess.run(
                version_cmd, capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                version_line = result.stdout.strip().split("\n")[0]
                print(f"✅ OK ({version_line if version_line else 'installed'})")
            else:
                print("❌ FAILED")
                print(f"   Error: {tool_name} command failed to run.")
                all_passed = False
        except FileNotFoundError:
            print("❌ FAILED")
            print(f"   Error: {tool_name} command not found.")
            all_passed = False
        except subprocess.TimeoutExpired:
            print("❌ FAILED")
            print(f"   Error: {tool_name} command timed out.")
            all_passed = False

    # If ADB is not installed, skip remaining checks
    if not all_passed:
        print("-" * 50)
        print("❌ System check failed. Please fix the issues above.")
        return False

    # Check 2: Device connected
    print("2. Checking connected devices...", end=" ")
    try:
        if device_type == DeviceType.ADB:
            result = subprocess.run(
                ["adb", "devices"], capture_output=True, text=True, timeout=10
            )
            lines = result.stdout.strip().split("\n")
            # Filter out header and empty lines, look for 'device' status
            devices = [
                line for line in lines[1:] if line.strip() and "\tdevice" in line
            ]
        elif device_type == DeviceType.HDC:
            result = subprocess.run(
                ["hdc", "list", "targets"], capture_output=True, text=True, timeout=10
            )
            lines = result.stdout.strip().split("\n")
            devices = [line for line in lines if line.strip()]
        else:  # IOS
            ios_devices = list_ios_devices()
            devices = [d.device_id for d in ios_devices]

        if not devices:
            print("❌ FAILED")
            print("   Error: No devices connected.")
            print("   Solution:")
            if device_type == DeviceType.ADB:
                print("     1. Enable USB debugging on your Android device")
                print("     2. Connect via USB and authorize the connection")
                print(
                    "     3. Or connect remotely: python main.py --connect <ip>:<port>"
                )
            elif device_type == DeviceType.HDC:
                print("     1. Enable USB debugging on your HarmonyOS device")
                print("     2. Connect via USB and authorize the connection")
                print(
                    "     3. Or connect remotely: python main.py --device-type hdc --connect <ip>:<port>"
                )
            else:  # IOS
                print("     1. Connect your iOS device via USB")
                print("     2. Unlock device and tap 'Trust This Computer'")
                print("     3. Verify: idevice_id -l")
                print("     4. Or connect via WiFi using device IP")
            all_passed = False
        else:
            if device_type == DeviceType.ADB:
                device_ids = [d.split("\t")[0] for d in devices]
            elif device_type == DeviceType.HDC:
                device_ids = [d.strip() for d in devices]
            else:  # IOS
                device_ids = devices
            print(
                f"✅ OK ({len(devices)} device(s): {', '.join(device_ids[:2])}{'...' if len(device_ids) > 2 else ''})"
            )
    except subprocess.TimeoutExpired:
        print("❌ FAILED")
        print(f"   Error: {tool_name} command timed out.")
        all_passed = False
    except Exception as e:
        print("❌ FAILED")
        print(f"   Error: {e}")
        all_passed = False

    # If no device connected, skip ADB Keyboard check
    if not all_passed:
        print("-" * 50)
        print("❌ System check failed. Please fix the issues above.")
        return False

    # Check 3: ADB Keyboard installed (only for ADB) or WebDriverAgent (for iOS)
    if device_type == DeviceType.ADB:
        print("3. Checking ADB Keyboard...", end=" ")
        try:
            result = subprocess.run(
                ["adb", "shell", "ime", "list", "-s"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ime_list = result.stdout.strip()

            if "com.android.adbkeyboard/.AdbIME" in ime_list:
                print("✅ OK")
            else:
                print("❌ FAILED")
                print("   Error: ADB Keyboard is not installed on the device.")
                print("   Solution:")
                print("     1. Download ADB Keyboard APK from:")
                print(
                    "        https://github.com/senzhk/ADBKeyBoard/blob/master/ADBKeyboard.apk"
                )
                print("     2. Install it on your device: adb install ADBKeyboard.apk")
                print(
                    "     3. Enable it in Settings > System > Languages & Input > Virtual Keyboard"
                )
                all_passed = False
        except subprocess.TimeoutExpired:
            print("❌ FAILED")
            print("   Error: ADB command timed out.")
            all_passed = False
        except Exception as e:
            print("❌ FAILED")
            print(f"   Error: {e}")
            all_passed = False
    elif device_type == DeviceType.HDC:
        # For HDC, skip keyboard check as it uses different input method
        print("3. Skipping keyboard check for HarmonyOS...", end=" ")
        print("✅ OK (using native input)")
    else:  # IOS
        # Check WebDriverAgent
        print(f"3. Checking WebDriverAgent ({wda_url})...", end=" ")
        try:
            conn = XCTestConnection(wda_url=wda_url)

            if conn.is_wda_ready():
                print("✅ OK")
                # Get WDA status for additional info
                status = conn.get_wda_status()
                if status:
                    session_id = status.get("sessionId", "N/A")
                    print(f"   Session ID: {session_id}")
            else:
                print("❌ FAILED")
                print("   Error: WebDriverAgent is not running or not accessible.")
                print("   Solution:")
                print("     1. Run WebDriverAgent on your iOS device via Xcode")
                print("     2. For USB: Set up port forwarding: iproxy 8100 8100")
                print(
                    "     3. For WiFi: Use device IP, e.g., --wda-url http://192.168.1.100:8100"
                )
                print("     4. Verify in browser: open http://localhost:8100/status")
                all_passed = False
        except Exception as e:
            print("❌ FAILED")
            print(f"   Error: {e}")
            all_passed = False

    print("-" * 50)

    if all_passed:
        print("✅ All system checks passed!\n")
    else:
        print("❌ System check failed. Please fix the issues above.")

    return all_passed


def _build_anthropic_messages_url(base_url: str) -> str:
    """Build Anthropic /v1/messages endpoint from a base URL."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return normalized + "/messages"
    return normalized + "/v1/messages"


def check_model_api(
    base_url: str,
    model_name: str,
    api_key: str = "EMPTY",
    provider: str = "openai",
    anthropic_version: str = "2023-06-01",
) -> bool:
    """
    Check if the model API is accessible and the specified model exists.

    Checks:
    1. Network connectivity to the API endpoint
    2. Model exists in the available models list

    Args:
        base_url: The API base URL
        model_name: The model name to check
        api_key: The API key for authentication

    Returns:
        True if all checks pass, False otherwise.
    """
    print("🔍 Checking model API...")
    print("-" * 50)

    all_passed = True

    provider = provider.lower().strip()

    # Check 1: Network connectivity and basic inference
    if provider == "anthropic":
        endpoint = _build_anthropic_messages_url(base_url)
        print(f"1. Checking API connectivity ({endpoint})...", end=" ")
        try:
            payload = {
                "model": model_name,
                "max_tokens": 5,
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "Hi"}]}
                ],
            }
            req = Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={
                    "content-type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": anthropic_version,
                    "user-agent": DEFAULT_HTTP_USER_AGENT,
                },
            )
            with urlopen(req, timeout=30.0) as resp:
                response_data = json.loads(resp.read().decode("utf-8"))

            if response_data.get("content"):
                print("✅ OK")
            else:
                print("❌ FAILED")
                print("   Error: Received empty response from API")
                all_passed = False
        except HTTPError as e:
            print("❌ FAILED")
            error_body = e.read().decode("utf-8", errors="replace")
            print(f"   Error: Anthropic API error ({e.code}): {error_body}")
            print("   Solution:")
            print("     1. Check if the model server is running")
            print("     2. Verify base URL, model, and API key")
            print(
                f"     3. Try: curl {endpoint} -H 'x-api-key: <key>' -H 'anthropic-version: {anthropic_version}'"
            )
            all_passed = False
        except URLError as e:
            print("❌ FAILED")
            print(f"   Error: Cannot connect to {endpoint}: {e.reason}")
            all_passed = False
        except Exception as e:
            print("❌ FAILED")
            error_msg = str(e)
            if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
                print(f"   Error: Connection to {endpoint} timed out")
                print("   Solution:")
                print("     1. Check your network connection")
                print("     2. Verify the server is responding")
            else:
                print(f"   Error: {error_msg}")
            all_passed = False
    else:
        print(f"1. Checking API connectivity ({base_url})...", end=" ")
        try:
            if OpenAI is None:
                raise ImportError(
                    "openai package is required for provider=openai. "
                    "Install it with: pip install openai"
                )
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=30.0)
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5,
                temperature=0.0,
                stream=False,
            )

            if response.choices and len(response.choices) > 0:
                print("✅ OK")
            else:
                print("❌ FAILED")
                print("   Error: Received empty response from API")
                all_passed = False
        except Exception as e:
            print("❌ FAILED")
            error_msg = str(e)
            if "Connection refused" in error_msg or "Connection error" in error_msg:
                print(f"   Error: Cannot connect to {base_url}")
                print("   Solution:")
                print("     1. Check if the model server is running")
                print("     2. Verify the base URL is correct")
                print(f"     3. Try: curl {base_url}/chat/completions")
            elif "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
                print(f"   Error: Connection to {base_url} timed out")
                print("   Solution:")
                print("     1. Check your network connection")
                print("     2. Verify the server is responding")
            elif (
                "Name or service not known" in error_msg
                or "nodename nor servname" in error_msg
            ):
                print(f"   Error: Cannot resolve hostname")
                print("   Solution:")
                print("     1. Check the URL is correct")
                print("     2. Verify DNS settings")
            else:
                print(f"   Error: {error_msg}")
            all_passed = False

    print("-" * 50)

    if all_passed:
        print("✅ Model API checks passed!\n")
    else:
        print("❌ Model API check failed. Please fix the issues above.")

    return all_passed


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Phone Agent - AI-powered phone automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run with default settings (Android)
    python main.py

    # Specify model endpoint
    python main.py --base-url http://localhost:8000/v1

    # Use API key for authentication
    python main.py --apikey sk-xxxxx

    # Run with specific device
    python main.py --device-id emulator-5554

    # Connect to remote device
    python main.py --connect 192.168.1.100:5555

    # List connected devices
    python main.py --list-devices

    # Enable TCP/IP on USB device and get connection info
    python main.py --enable-tcpip

    # List supported apps
    python main.py --list-apps

    # iOS specific examples
    # Run with iOS device
    python main.py --device-type ios "Open Safari and search for iPhone tips"

    # Use WiFi connection for iOS
    python main.py --device-type ios --wda-url http://192.168.1.100:8100

    # List connected iOS devices
    python main.py --device-type ios --list-devices

    # Check WebDriverAgent status
    python main.py --device-type ios --wda-status

    # Pair with iOS device
    python main.py --device-type ios --pair
        """,
    )

    # Model options
    parser.add_argument(
        "--base-url",
        type=str,
        default=os.getenv("PHONE_AGENT_BASE_URL", "http://localhost:8000/v1"),
        help="Model API base URL",
    )

    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("PHONE_AGENT_MODEL", "autoglm-phone-9b"),
        help="Model name",
    )

    parser.add_argument(
        "--apikey",
        type=str,
        default=os.getenv("PHONE_AGENT_API_KEY", "EMPTY"),
        help="API key for model authentication",
    )

    parser.add_argument(
        "--provider",
        type=str,
        choices=["openai", "anthropic"],
        default=os.getenv("PHONE_AGENT_PROVIDER", "openai"),
        help="Model API provider format (openai or anthropic, default: openai)",
    )

    parser.add_argument(
        "--anthropic-version",
        type=str,
        default=os.getenv("PHONE_AGENT_ANTHROPIC_VERSION", "2023-06-01"),
        help="Anthropic API version header (only used when --provider anthropic)",
    )

    parser.add_argument(
        "--max-steps",
        type=int,
        default=int(os.getenv("PHONE_AGENT_MAX_STEPS", "100")),
        help="Maximum steps per task",
    )

    token_usage_group = parser.add_mutually_exclusive_group()
    token_usage_group.add_argument(
        "--save-token-usage",
        dest="save_token_usage",
        action="store_true",
        help="Save per-step token usage to JSONL",
    )
    token_usage_group.add_argument(
        "--no-save-token-usage",
        dest="save_token_usage",
        action="store_false",
        help="Disable per-step token usage logging",
    )
    parser.set_defaults(
        save_token_usage=env_flag("PHONE_AGENT_SAVE_TOKEN_USAGE", True)
    )
    parser.add_argument(
        "--token-usage-dir",
        type=str,
        default=os.getenv("PHONE_AGENT_TOKEN_USAGE_DIR", "artifacts/token_usage"),
        help="Directory for per-step token usage logs",
    )

    fast_path_group = parser.add_mutually_exclusive_group()
    fast_path_group.add_argument(
        "--experience-fast-path",
        dest="experience_fast_path",
        action="store_true",
        help="Enable high-confidence fast path from experience store",
    )
    fast_path_group.add_argument(
        "--no-experience-fast-path",
        dest="experience_fast_path",
        action="store_false",
        help="Disable fast-path and always use model decisions",
    )
    parser.set_defaults(
        experience_fast_path=env_flag("PHONE_AGENT_EXPERIENCE_FAST_PATH", True)
    )
    parser.add_argument(
        "--experience-fast-path-confidence",
        type=float,
        default=env_float("PHONE_AGENT_EXPERIENCE_FAST_PATH_CONFIDENCE", 0.72, 0.0, 1.0),
        help="Minimum confidence [0,1] to use fast path (default: 0.72)",
    )
    parser.add_argument(
        "--experience-fast-path-min-attempts",
        type=int,
        default=env_int("PHONE_AGENT_EXPERIENCE_FAST_PATH_MIN_ATTEMPTS", 3, minimum=1),
        help="Minimum historical attempts before fast path (default: 3)",
    )
    parser.add_argument(
        "--experience-fast-path-max-streak",
        type=int,
        default=env_int("PHONE_AGENT_EXPERIENCE_FAST_PATH_MAX_STREAK", 4, minimum=1),
        help="Max consecutive fast-path steps before forcing model step (default: 4)",
    )
    parser.add_argument(
        "--experience-exploration-rate",
        type=float,
        default=env_float("PHONE_AGENT_EXPERIENCE_EXPLORATION_RATE", 0.08, 0.0, 1.0),
        help="Probability [0,1] to skip fast path for exploration (default: 0.08)",
    )
    exact_only_group = parser.add_mutually_exclusive_group()
    exact_only_group.add_argument(
        "--experience-fast-path-exact-only",
        dest="experience_fast_path_exact_only",
        action="store_true",
        help="Allow fast path only for exact state matches",
    )
    exact_only_group.add_argument(
        "--no-experience-fast-path-exact-only",
        dest="experience_fast_path_exact_only",
        action="store_false",
        help="Allow app-level fast path fallback (less strict)",
    )
    parser.set_defaults(
        experience_fast_path_exact_only=env_flag(
            "PHONE_AGENT_EXPERIENCE_FAST_PATH_EXACT_ONLY", True
        )
    )
    sensitive_gate_group = parser.add_mutually_exclusive_group()
    sensitive_gate_group.add_argument(
        "--experience-sensitive-gate",
        dest="experience_sensitive_gate",
        action="store_true",
        help="Block fast path for sensitive tap/type contexts",
    )
    sensitive_gate_group.add_argument(
        "--no-experience-sensitive-gate",
        dest="experience_sensitive_gate",
        action="store_false",
        help="Disable sensitive fast-path guard (not recommended)",
    )
    parser.set_defaults(
        experience_sensitive_gate=env_flag("PHONE_AGENT_EXPERIENCE_SENSITIVE_GATE", True)
    )

    # Device options
    parser.add_argument(
        "--device-id",
        "-d",
        type=str,
        default=os.getenv("PHONE_AGENT_DEVICE_ID"),
        help="ADB device ID",
    )

    parser.add_argument(
        "--connect",
        "-c",
        type=str,
        metavar="ADDRESS",
        help="Connect to remote device (e.g., 192.168.1.100:5555)",
    )

    parser.add_argument(
        "--disconnect",
        type=str,
        nargs="?",
        const="all",
        metavar="ADDRESS",
        help="Disconnect from remote device (or 'all' to disconnect all)",
    )

    parser.add_argument(
        "--list-devices", action="store_true", help="List connected devices and exit"
    )

    parser.add_argument(
        "--enable-tcpip",
        type=int,
        nargs="?",
        const=5555,
        metavar="PORT",
        help="Enable TCP/IP debugging on USB device (default port: 5555)",
    )

    # iOS specific options
    parser.add_argument(
        "--wda-url",
        type=str,
        default=os.getenv("PHONE_AGENT_WDA_URL", "http://localhost:8100"),
        help="WebDriverAgent URL for iOS (default: http://localhost:8100)",
    )

    parser.add_argument(
        "--pair",
        action="store_true",
        help="Pair with iOS device (required for some operations)",
    )

    parser.add_argument(
        "--wda-status",
        action="store_true",
        help="Show WebDriverAgent status and exit (iOS only)",
    )

    # Other options
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Suppress verbose output"
    )

    parser.add_argument(
        "--list-apps", action="store_true", help="List supported apps and exit"
    )

    parser.add_argument(
        "--lang",
        type=str,
        choices=["cn", "en"],
        default=os.getenv("PHONE_AGENT_LANG", "cn"),
        help="Language for system prompt (cn or en, default: cn)",
    )

    parser.add_argument(
        "--device-type",
        type=str,
        choices=["adb", "hdc", "ios"],
        default=os.getenv("PHONE_AGENT_DEVICE_TYPE", "adb"),
        help="Device type: adb for Android, hdc for HarmonyOS, ios for iPhone (default: adb)",
    )

    training_group = parser.add_mutually_exclusive_group()
    training_group.add_argument(
        "--training-mode",
        dest="training_mode",
        action="store_true",
        help="Enable training mode (allow takeover for human correction)",
    )
    training_group.add_argument(
        "--no-training-mode",
        dest="training_mode",
        action="store_false",
        help="Disable training mode",
    )
    parser.set_defaults(training_mode=env_flag("PHONE_AGENT_TRAINING_MODE", False))

    parser.add_argument(
        "--takeover-policy",
        type=str,
        choices=["auto", "always", "never"],
        default=os.getenv("PHONE_AGENT_TAKEOVER_POLICY", "auto"),
        help=(
            "Takeover policy: auto (training mode or mandatory auth only), "
            "always, or never (default: auto)"
        ),
    )

    parser.add_argument(
        "task",
        nargs="?",
        type=str,
        help="Task to execute (interactive mode if not provided)",
    )

    return parser.parse_args()


def handle_ios_device_commands(args) -> bool:
    """
    Handle iOS device-related commands.

    Returns:
        True if a device command was handled (should exit), False otherwise.
    """
    conn = XCTestConnection(wda_url=args.wda_url)

    # Handle --list-devices
    if args.list_devices:
        devices = list_ios_devices()
        if not devices:
            print("No iOS devices connected.")
            print("\nTroubleshooting:")
            print("  1. Connect device via USB")
            print("  2. Unlock device and trust this computer")
            print("  3. Run: idevice_id -l")
        else:
            print("Connected iOS devices:")
            print("-" * 70)
            for device in devices:
                conn_type = device.connection_type.value
                model_info = f"{device.model}" if device.model else "Unknown"
                ios_info = f"iOS {device.ios_version}" if device.ios_version else ""
                name_info = device.device_name or "Unnamed"

                print(f"  ✓ {name_info}")
                print(f"    UUID: {device.device_id}")
                print(f"    Model: {model_info}")
                print(f"    OS: {ios_info}")
                print(f"    Connection: {conn_type}")
                print("-" * 70)
        return True

    # Handle --pair
    if args.pair:
        print("Pairing with iOS device...")
        success, message = conn.pair_device(args.device_id)
        print(f"{'✓' if success else '✗'} {message}")
        return True

    # Handle --wda-status
    if args.wda_status:
        print(f"Checking WebDriverAgent status at {args.wda_url}...")
        print("-" * 50)

        if conn.is_wda_ready():
            print("✓ WebDriverAgent is running")

            status = conn.get_wda_status()
            if status:
                print(f"\nStatus details:")
                value = status.get("value", {})
                print(f"  Session ID: {status.get('sessionId', 'N/A')}")
                print(f"  Build: {value.get('build', {}).get('time', 'N/A')}")

                current_app = value.get("currentApp", {})
                if current_app:
                    print(f"\nCurrent App:")
                    print(f"  Bundle ID: {current_app.get('bundleId', 'N/A')}")
                    print(f"  Process ID: {current_app.get('pid', 'N/A')}")
        else:
            print("✗ WebDriverAgent is not running")
            print("\nPlease start WebDriverAgent on your iOS device:")
            print("  1. Open WebDriverAgent.xcodeproj in Xcode")
            print("  2. Select your device")
            print("  3. Run WebDriverAgentRunner (Product > Test or Cmd+U)")
            print(f"  4. For USB: Run port forwarding: iproxy 8100 8100")

        return True

    return False


def handle_device_commands(args) -> bool:
    """
    Handle device-related commands.

    Returns:
        True if a device command was handled (should exit), False otherwise.
    """
    device_type = (
        DeviceType.ADB
        if args.device_type == "adb"
        else (DeviceType.HDC if args.device_type == "hdc" else DeviceType.IOS)
    )

    # Handle iOS-specific commands
    if device_type == DeviceType.IOS:
        return handle_ios_device_commands(args)

    device_factory = get_device_factory()
    ConnectionClass = device_factory.get_connection_class()
    conn = ConnectionClass()

    # Handle --list-devices
    if args.list_devices:
        devices = device_factory.list_devices()
        if not devices:
            print("No devices connected.")
        else:
            print("Connected devices:")
            print("-" * 60)
            for device in devices:
                status_icon = "✓" if device.status == "device" else "✗"
                conn_type = device.connection_type.value
                model_info = f" ({device.model})" if device.model else ""
                print(
                    f"  {status_icon} {device.device_id:<30} [{conn_type}]{model_info}"
                )
        return True

    # Handle --connect
    if args.connect:
        print(f"Connecting to {args.connect}...")
        success, message = conn.connect(args.connect)
        print(f"{'✓' if success else '✗'} {message}")
        if success:
            # Set as default device
            args.device_id = args.connect
        return not success  # Continue if connection succeeded

    # Handle --disconnect
    if args.disconnect:
        if args.disconnect == "all":
            print("Disconnecting all remote devices...")
            success, message = conn.disconnect()
        else:
            print(f"Disconnecting from {args.disconnect}...")
            success, message = conn.disconnect(args.disconnect)
        print(f"{'✓' if success else '✗'} {message}")
        return True

    # Handle --enable-tcpip
    if args.enable_tcpip:
        port = args.enable_tcpip
        print(f"Enabling TCP/IP debugging on port {port}...")

        success, message = conn.enable_tcpip(port, args.device_id)
        print(f"{'✓' if success else '✗'} {message}")

        if success:
            # Try to get device IP
            ip = conn.get_device_ip(args.device_id)
            if ip:
                print(f"\nYou can now connect remotely using:")
                print(f"  python main.py --connect {ip}:{port}")
                print(f"\nOr via ADB directly:")
                print(f"  adb connect {ip}:{port}")
            else:
                print("\nCould not determine device IP. Check device WiFi settings.")
        return True

    return False


def main():
    """Main entry point."""
    args = parse_args()
    args.experience_fast_path_confidence = max(
        0.0, min(1.0, float(args.experience_fast_path_confidence))
    )
    args.experience_exploration_rate = max(
        0.0, min(1.0, float(args.experience_exploration_rate))
    )
    args.experience_fast_path_min_attempts = max(
        1, int(args.experience_fast_path_min_attempts)
    )
    args.experience_fast_path_max_streak = max(1, int(args.experience_fast_path_max_streak))

    # Set device type globally based on args
    if args.device_type == "adb":
        device_type = DeviceType.ADB
    elif args.device_type == "hdc":
        device_type = DeviceType.HDC
    else:  # ios
        device_type = DeviceType.IOS

    # Set device type globally for non-iOS devices
    if device_type != DeviceType.IOS:
        set_device_type(device_type)

    # Enable HDC verbose mode if using HDC
    if device_type == DeviceType.HDC:
        from phone_agent.hdc import set_hdc_verbose

        set_hdc_verbose(True)

    # Propagate runtime policy to action handlers.
    os.environ["PHONE_AGENT_TRAINING_MODE"] = "1" if args.training_mode else "0"
    os.environ["PHONE_AGENT_TAKEOVER_POLICY"] = args.takeover_policy

    # Handle --list-apps (no system check needed)
    if args.list_apps:
        if device_type == DeviceType.HDC:
            print("Supported HarmonyOS apps:")
            apps = list_harmonyos_apps()
        elif device_type == DeviceType.IOS:
            print("Supported iOS apps:")
            print("\nNote: For iOS apps, Bundle IDs are configured in:")
            print("  phone_agent/config/apps_ios.py")
            print("\nCurrently configured apps:")
            apps = list_ios_apps()
        else:
            print("Supported Android apps:")
            apps = list_supported_apps()

        for app in sorted(apps):
            print(f"  - {app}")

        if device_type == DeviceType.IOS:
            print(
                "\nTo add iOS apps, find the Bundle ID and add to APP_PACKAGES_IOS dictionary."
            )
        return

    # Handle device commands (these may need partial system checks)
    if handle_device_commands(args):
        return

    # Run system requirements check before proceeding
    if not check_system_requirements(
        device_type,
        wda_url=args.wda_url
        if device_type == DeviceType.IOS
        else "http://localhost:8100",
    ):
        sys.exit(1)

    # Check model API connectivity and model availability
    if not check_model_api(
        args.base_url,
        args.model,
        args.apikey,
        provider=args.provider,
        anthropic_version=args.anthropic_version,
    ):
        sys.exit(1)

    # Create configurations and agent based on device type
    model_config = ModelConfig(
        base_url=args.base_url,
        model_name=args.model,
        api_key=args.apikey,
        provider=args.provider,
        anthropic_version=args.anthropic_version,
        lang=args.lang,
    )

    if device_type == DeviceType.IOS:
        # Create iOS agent
        agent_config = IOSAgentConfig(
            max_steps=args.max_steps,
            wda_url=args.wda_url,
            device_id=args.device_id,
            verbose=not args.quiet,
            lang=args.lang,
            save_token_usage=args.save_token_usage,
            token_usage_dir=args.token_usage_dir,
            experience_fast_path=args.experience_fast_path,
            experience_fast_path_confidence=args.experience_fast_path_confidence,
            experience_fast_path_min_attempts=args.experience_fast_path_min_attempts,
            experience_fast_path_max_streak=args.experience_fast_path_max_streak,
            experience_exploration_rate=args.experience_exploration_rate,
            experience_fast_path_exact_only=args.experience_fast_path_exact_only,
            experience_sensitive_gate=args.experience_sensitive_gate,
        )

        agent = IOSPhoneAgent(
            model_config=model_config,
            agent_config=agent_config,
            takeover_callback=build_takeover_callback(args.lang),
        )
    else:
        # Create Android/HarmonyOS agent
        agent_config = AgentConfig(
            max_steps=args.max_steps,
            device_id=args.device_id,
            verbose=not args.quiet,
            lang=args.lang,
            save_token_usage=args.save_token_usage,
            token_usage_dir=args.token_usage_dir,
            experience_fast_path=args.experience_fast_path,
            experience_fast_path_confidence=args.experience_fast_path_confidence,
            experience_fast_path_min_attempts=args.experience_fast_path_min_attempts,
            experience_fast_path_max_streak=args.experience_fast_path_max_streak,
            experience_exploration_rate=args.experience_exploration_rate,
            experience_fast_path_exact_only=args.experience_fast_path_exact_only,
            experience_sensitive_gate=args.experience_sensitive_gate,
        )

        agent = PhoneAgent(
            model_config=model_config,
            agent_config=agent_config,
            takeover_callback=build_takeover_callback(args.lang),
        )

    # Print header
    print("=" * 50)
    if device_type == DeviceType.IOS:
        print("Phone Agent iOS - AI-powered iOS automation")
    else:
        print("Phone Agent - AI-powered phone automation")
    print("=" * 50)
    print(f"Model: {model_config.model_name}")
    print(f"Base URL: {model_config.base_url}")
    print(f"Provider: {model_config.provider}")
    print(f"Max Steps: {agent_config.max_steps}")
    print(f"Language: {agent_config.lang}")
    print(f"Device Type: {args.device_type.upper()}")
    print(f"Training Mode: {'ON' if args.training_mode else 'OFF'}")
    print(f"Takeover Policy: {args.takeover_policy}")
    print(f"Token Usage Log: {'ON' if args.save_token_usage else 'OFF'}")
    if args.save_token_usage:
        print(f"Token Usage Dir: {args.token_usage_dir}")
    print(f"Experience Fast Path: {'ON' if args.experience_fast_path else 'OFF'}")
    if args.experience_fast_path:
        print(
            "Experience Fast Path Config: "
            f"conf>={args.experience_fast_path_confidence:.2f}, "
            f"min_attempts={args.experience_fast_path_min_attempts}, "
            f"max_streak={args.experience_fast_path_max_streak}, "
            f"exploration={args.experience_exploration_rate:.2f}, "
            f"exact_only={'ON' if args.experience_fast_path_exact_only else 'OFF'}, "
            f"sensitive_gate={'ON' if args.experience_sensitive_gate else 'OFF'}"
        )

    # Show iOS-specific config
    if device_type == DeviceType.IOS:
        print(f"WDA URL: {args.wda_url}")

    # Show device info
    if device_type == DeviceType.IOS:
        devices = list_ios_devices()
        if agent_config.device_id:
            print(f"Device: {agent_config.device_id}")
        elif devices:
            device = devices[0]
            print(f"Device: {device.device_name or device.device_id[:16]}")
            if device.model and device.ios_version:
                print(f"        {device.model}, iOS {device.ios_version}")
    else:
        device_factory = get_device_factory()
        devices = device_factory.list_devices()
        if agent_config.device_id:
            print(f"Device: {agent_config.device_id}")
        elif devices:
            print(f"Device: {devices[0].device_id} (auto-detected)")

    print("=" * 50)

    # Run with provided task or enter interactive mode
    if args.task:
        print(f"\nTask: {args.task}\n")
        result = agent.run(args.task)
        print(f"\nResult: {result}")
    else:
        # Interactive mode
        print("\nEntering interactive mode. Type 'quit' to exit.\n")

        while True:
            try:
                task = input("Enter your task: ").strip()

                if task.lower() in ("quit", "exit", "q"):
                    print("Goodbye!")
                    break

                if not task:
                    continue

                print()
                result = agent.run(task)
                print(f"\nResult: {result}\n")
                agent.reset()

            except KeyboardInterrupt:
                print("\n\nInterrupted. Goodbye!")
                break
            except Exception as e:
                print(f"\nError: {e}\n")


if __name__ == "__main__":
    main()
