"""Action handler for processing AI model outputs."""

import ast
import json
import os
import re
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

from phone_agent.config.timing import TIMING_CONFIG
from phone_agent.device_factory import get_device_factory


@dataclass
class ActionResult:
    """Result of an action execution."""

    success: bool
    should_finish: bool
    message: str | None = None
    requires_confirmation: bool = False


class ActionHandler:
    """
    Handles execution of actions from AI model output.

    Args:
        device_id: Optional ADB device ID for multi-device setups.
        confirmation_callback: Optional callback for sensitive action confirmation.
            Should return True to proceed, False to cancel.
        takeover_callback: Optional callback for takeover requests (login, captcha).
    """

    def __init__(
        self,
        device_id: str | None = None,
        confirmation_callback: Callable[[str], bool] | None = None,
        takeover_callback: Callable[[str], None] | None = None,
    ):
        self.device_id = device_id
        self.confirmation_callback = confirmation_callback or self._default_confirmation
        self.takeover_callback = takeover_callback or self._default_takeover
        self.coordinate_mode = os.getenv("PHONE_AGENT_COORDINATE_MODE", "auto").lower()
        if self.coordinate_mode not in {"auto", "absolute", "relative", "normalized"}:
            self.coordinate_mode = "auto"
        self.model_coord_scale_x, self.model_coord_scale_y = self._resolve_model_coord_scale()
        self._last_tap_point: tuple[int, int] | None = None
        self._same_tap_count = 0

    @staticmethod
    def _safe_float_env(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _resolve_model_coord_scale(self) -> tuple[float, float]:
        """
        Resolve model coordinate scale from env vars or optional per-device profile file.

        Priority:
        1) Explicit env vars PHONE_AGENT_MODEL_COORD_SCALE_X/Y
        2) Profile file lookup by (device_id, provider, model)
        3) Default 1.0, 1.0
        """
        scale_x_env = os.getenv("PHONE_AGENT_MODEL_COORD_SCALE_X")
        scale_y_env = os.getenv("PHONE_AGENT_MODEL_COORD_SCALE_Y")
        if scale_x_env is not None or scale_y_env is not None:
            return (
                self._safe_float_env("PHONE_AGENT_MODEL_COORD_SCALE_X", 1.0),
                self._safe_float_env("PHONE_AGENT_MODEL_COORD_SCALE_Y", 1.0),
            )

        profile_file = os.getenv(
            "PHONE_AGENT_COORD_PROFILE_FILE",
            str(Path.home() / ".openautoglm" / "coord_profiles.json"),
        )
        if not self.device_id:
            return 1.0, 1.0

        try:
            path = Path(profile_file).expanduser()
            if not path.exists():
                return 1.0, 1.0
            data = json.loads(path.read_text(encoding="utf-8"))
            profiles = data.get("profiles", {})
            device_profiles = profiles.get(self.device_id, {})
            provider = os.getenv("PHONE_AGENT_PROVIDER", "openai")
            model = os.getenv("PHONE_AGENT_MODEL", "autoglm-phone-9b")
            key = f"{provider}::{model}"
            item = device_profiles.get(key)
            if not isinstance(item, dict):
                return 1.0, 1.0
            sx = float(item.get("scale_x", 1.0))
            sy = float(item.get("scale_y", 1.0))
            print(
                f"[coord] loaded profile scale for {self.device_id} {key}: "
                f"x{sx:.3f}, y{sy:.3f}"
            )
            return sx, sy
        except Exception as e:
            print(f"[coord] profile load failed: {e}")
            return 1.0, 1.0

    def execute(
        self, action: dict[str, Any], screen_width: int, screen_height: int
    ) -> ActionResult:
        """
        Execute an action from the AI model.

        Args:
            action: The action dictionary from the model.
            screen_width: Current screen width in pixels.
            screen_height: Current screen height in pixels.

        Returns:
            ActionResult indicating success and whether to finish.
        """
        action_type = action.get("_metadata")

        if action_type == "finish":
            return ActionResult(
                success=True, should_finish=True, message=action.get("message")
            )

        if action_type != "do":
            return ActionResult(
                success=False,
                should_finish=True,
                message=f"Unknown action type: {action_type}",
            )

        action_name = action.get("action")
        handler_method = self._get_handler(action_name)

        if handler_method is None:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Unknown action: {action_name}",
            )

        try:
            return handler_method(action, screen_width, screen_height)
        except Exception as e:
            return ActionResult(
                success=False, should_finish=False, message=f"Action failed: {e}"
            )

    def _get_handler(self, action_name: str) -> Callable | None:
        """Get the handler method for an action."""
        handlers = {
            "Launch": self._handle_launch,
            "Tap": self._handle_tap,
            "Type": self._handle_type,
            "Type_Name": self._handle_type,
            "Swipe": self._handle_swipe,
            "Back": self._handle_back,
            "Home": self._handle_home,
            "Double Tap": self._handle_double_tap,
            "Long Press": self._handle_long_press,
            "Wait": self._handle_wait,
            "Take_over": self._handle_takeover,
            "Note": self._handle_note,
            "Call_API": self._handle_call_api,
            "Interact": self._handle_interact,
        }
        return handlers.get(action_name)

    def _clamp_point(self, x: int, y: int, width: int, height: int) -> tuple[int, int]:
        """Clamp point into current screen bounds."""
        clamped_x = max(0, min(x, max(0, width - 1)))
        clamped_y = max(0, min(y, max(0, height - 1)))
        return clamped_x, clamped_y

    def _convert_element_to_absolute(
        self, element: list[int | float], screen_width: int, screen_height: int
    ) -> tuple[int, int, str]:
        """
        Convert model coordinates into absolute pixels.

        Supported coordinate formats:
        - absolute pixel: [x, y]
        - relative 0-1000: [x, y]
        - normalized 0-1: [x, y]
        """
        if len(element) < 2:
            raise ValueError("Invalid element coordinates")

        raw_x = float(element[0])
        raw_y = float(element[1])
        scaled_x = raw_x * self.model_coord_scale_x
        scaled_y = raw_y * self.model_coord_scale_y
        scale_applied = (
            abs(self.model_coord_scale_x - 1.0) > 1e-6
            or abs(self.model_coord_scale_y - 1.0) > 1e-6
        )

        def as_absolute() -> tuple[int, int]:
            return self._clamp_point(
                int(round(scaled_x if scale_applied else raw_x)),
                int(round(scaled_y if scale_applied else raw_y)),
                screen_width,
                screen_height,
            )

        def as_relative_1000() -> tuple[int, int]:
            x = int(round(raw_x / 1000.0 * screen_width))
            y = int(round(raw_y / 1000.0 * screen_height))
            return self._clamp_point(x, y, screen_width, screen_height)

        def as_normalized() -> tuple[int, int]:
            x = int(round(raw_x * screen_width))
            y = int(round(raw_y * screen_height))
            return self._clamp_point(x, y, screen_width, screen_height)

        if self.coordinate_mode == "absolute":
            x, y = as_absolute()
            mode = "absolute(forced)"
            if scale_applied:
                mode += (
                    f"+scale(x{self.model_coord_scale_x:.3f},"
                    f"y{self.model_coord_scale_y:.3f})"
                )
            return x, y, mode
        if self.coordinate_mode == "relative":
            x, y = as_relative_1000()
            return x, y, "relative-1000(forced)"
        if self.coordinate_mode == "normalized":
            x, y = as_normalized()
            return x, y, "normalized(forced)"

        # Auto mode:
        # 1) 0~1 range: normalized
        # 2) values already in screen bounds: absolute
        # 3) values out of bounds but <=1000: legacy relative-1000
        # 4) fallback absolute + clamp
        if 0.0 <= raw_x <= 1.0 and 0.0 <= raw_y <= 1.0:
            x, y = as_normalized()
            return x, y, "normalized(auto)"

        if 0.0 <= raw_x <= screen_width and 0.0 <= raw_y <= screen_height:
            x, y = as_absolute()
            mode = "absolute(auto)"
            if scale_applied:
                mode += (
                    f"+scale(x{self.model_coord_scale_x:.3f},"
                    f"y{self.model_coord_scale_y:.3f})"
                )
            return x, y, mode

        if 0.0 <= raw_x <= 1000.0 and 0.0 <= raw_y <= 1000.0:
            x, y = as_relative_1000()
            return x, y, "relative-1000(auto)"

        x, y = as_absolute()
        mode = "absolute-clamped(auto)"
        if scale_applied:
            mode += (
                f"+scale(x{self.model_coord_scale_x:.3f},"
                f"y{self.model_coord_scale_y:.3f})"
            )
        return x, y, mode

    def _resolve_tap_point(
        self, x: int, y: int, width: int, height: int
    ) -> tuple[int, int]:
        """
        Resolve tap point with anti-loop micro offsets for repeated same taps.

        This reduces stuck loops when UI does not react to an exact point.
        """
        if self._last_tap_point == (x, y):
            self._same_tap_count += 1
        else:
            self._same_tap_count = 0
            self._last_tap_point = (x, y)

        if self._same_tap_count < 2:
            return x, y

        offsets = [
            (12, 0),
            (-12, 0),
            (0, -12),
            (0, 12),
            (18, -18),
            (-18, -18),
            (18, 18),
            (-18, 18),
        ]
        offset = offsets[(self._same_tap_count - 2) % len(offsets)]
        adjusted = self._clamp_point(x + offset[0], y + offset[1], width, height)
        print(
            f"[tap-adjust] repeated tap detected, apply offset {offset}: ({x}, {y}) -> {adjusted}"
        )
        return adjusted

    def _handle_launch(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle app launch action."""
        app_name = action.get("app")
        if not app_name:
            return ActionResult(False, False, "No app name specified")

        device_factory = get_device_factory()
        success = device_factory.launch_app(app_name, self.device_id)
        if success:
            return ActionResult(True, False)
        return ActionResult(False, False, f"App not found: {app_name}")

    def _handle_tap(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle tap action."""
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y, mode = self._convert_element_to_absolute(element, width, height)
        x, y = self._resolve_tap_point(x, y, width, height)
        print(f"[coord] tap {element} -> ({x}, {y}) via {mode}")

        # Check for sensitive operation
        if "message" in action:
            if not self.confirmation_callback(action["message"]):
                return ActionResult(
                    success=False,
                    should_finish=True,
                    message="User cancelled sensitive operation",
                )

        device_factory = get_device_factory()
        device_factory.tap(x, y, self.device_id)
        return ActionResult(True, False)

    def _handle_type(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle text input action."""
        text = action.get("text", "")

        device_factory = get_device_factory()

        # Switch to ADB keyboard
        original_ime = device_factory.detect_and_set_adb_keyboard(self.device_id)
        time.sleep(TIMING_CONFIG.action.keyboard_switch_delay)

        # Clear existing text and type new text
        device_factory.clear_text(self.device_id)
        time.sleep(TIMING_CONFIG.action.text_clear_delay)

        # Handle multiline text by splitting on newlines
        device_factory.type_text(text, self.device_id)
        time.sleep(TIMING_CONFIG.action.text_input_delay)

        # Restore original keyboard
        device_factory.restore_keyboard(original_ime, self.device_id)
        time.sleep(TIMING_CONFIG.action.keyboard_restore_delay)

        return ActionResult(True, False)

    def _handle_swipe(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle swipe action."""
        start = action.get("start")
        end = action.get("end")

        if not start or not end:
            return ActionResult(False, False, "Missing swipe coordinates")

        start_x, start_y, start_mode = self._convert_element_to_absolute(
            start, width, height
        )
        end_x, end_y, end_mode = self._convert_element_to_absolute(end, width, height)
        print(
            f"[coord] swipe start {start} -> ({start_x}, {start_y}) via {start_mode}, "
            f"end {end} -> ({end_x}, {end_y}) via {end_mode}"
        )

        device_factory = get_device_factory()
        device_factory.swipe(start_x, start_y, end_x, end_y, device_id=self.device_id)
        return ActionResult(True, False)

    def _handle_back(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle back button action."""
        device_factory = get_device_factory()
        device_factory.back(self.device_id)
        return ActionResult(True, False)

    def _handle_home(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle home button action."""
        device_factory = get_device_factory()
        device_factory.home(self.device_id)
        return ActionResult(True, False)

    def _handle_double_tap(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle double tap action."""
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y, mode = self._convert_element_to_absolute(element, width, height)
        print(f"[coord] double tap {element} -> ({x}, {y}) via {mode}")
        device_factory = get_device_factory()
        device_factory.double_tap(x, y, self.device_id)
        return ActionResult(True, False)

    def _handle_long_press(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle long press action."""
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y, mode = self._convert_element_to_absolute(element, width, height)
        print(f"[coord] long press {element} -> ({x}, {y}) via {mode}")
        device_factory = get_device_factory()
        device_factory.long_press(x, y, device_id=self.device_id)
        return ActionResult(True, False)

    def _handle_wait(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle wait action."""
        duration_str = action.get("duration", "1 seconds")
        try:
            duration = float(duration_str.replace("seconds", "").strip())
        except ValueError:
            duration = 1.0

        time.sleep(duration)
        return ActionResult(True, False)

    def _handle_takeover(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle takeover request (login, captcha, etc.)."""
        message = action.get("message", "User intervention required")
        self.takeover_callback(message)
        return ActionResult(True, False)

    def _handle_note(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle note action (placeholder for content recording)."""
        # This action is typically used for recording page content
        # Implementation depends on specific requirements
        return ActionResult(True, False)

    def _handle_call_api(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle API call action (placeholder for summarization)."""
        # This action is typically used for content summarization
        # Implementation depends on specific requirements
        return ActionResult(True, False)

    def _handle_interact(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle interaction request (user choice needed)."""
        # This action signals that user input is needed
        return ActionResult(True, False, message="User interaction required")

    def _send_keyevent(self, keycode: str) -> None:
        """Send a keyevent to the device."""
        from phone_agent.device_factory import DeviceType, get_device_factory
        from phone_agent.hdc.connection import _run_hdc_command

        device_factory = get_device_factory()

        # Handle HDC devices with HarmonyOS-specific keyEvent command
        if device_factory.device_type == DeviceType.HDC:
            hdc_prefix = ["hdc", "-t", self.device_id] if self.device_id else ["hdc"]
            
            # Map common keycodes to HarmonyOS keyEvent codes
            # KEYCODE_ENTER (66) -> 2054 (HarmonyOS Enter key code)
            if keycode == "KEYCODE_ENTER" or keycode == "66":
                _run_hdc_command(
                    hdc_prefix + ["shell", "uitest", "uiInput", "keyEvent", "2054"],
                    capture_output=True,
                    text=True,
                )
            else:
                # For other keys, try to use the numeric code directly
                # If keycode is a string like "KEYCODE_ENTER", convert it
                try:
                    # Try to extract numeric code from string or use as-is
                    if keycode.startswith("KEYCODE_"):
                        # For now, only handle ENTER, other keys may need mapping
                        if "ENTER" in keycode:
                            _run_hdc_command(
                                hdc_prefix + ["shell", "uitest", "uiInput", "keyEvent", "2054"],
                                capture_output=True,
                                text=True,
                            )
                        else:
                            # Fallback to ADB-style command for unsupported keys
                            subprocess.run(
                                hdc_prefix + ["shell", "input", "keyevent", keycode],
                                capture_output=True,
                                text=True,
                            )
                    else:
                        # Assume it's a numeric code
                        _run_hdc_command(
                            hdc_prefix + ["shell", "uitest", "uiInput", "keyEvent", str(keycode)],
                            capture_output=True,
                            text=True,
                        )
                except Exception:
                    # Fallback to ADB-style command
                    subprocess.run(
                        hdc_prefix + ["shell", "input", "keyevent", keycode],
                        capture_output=True,
                        text=True,
                    )
        else:
            # ADB devices use standard input keyevent command
            cmd_prefix = ["adb", "-s", self.device_id] if self.device_id else ["adb"]
            subprocess.run(
                cmd_prefix + ["shell", "input", "keyevent", keycode],
                capture_output=True,
                text=True,
            )

    @staticmethod
    def _default_confirmation(message: str) -> bool:
        """Default confirmation callback using console input."""
        response = input(f"Sensitive operation: {message}\nConfirm? (Y/N): ")
        return response.upper() == "Y"

    @staticmethod
    def _default_takeover(message: str) -> None:
        """Default takeover callback using console input."""
        input(f"{message}\nPress Enter after completing manual operation...")


def parse_action(response: str) -> dict[str, Any]:
    """
    Parse action from model response.

    Args:
        response: Raw response string from the model.

    Returns:
        Parsed action dictionary.

    Raises:
        ValueError: If the response cannot be parsed.
    """
    print(f"Parsing action: {response}")
    try:
        response = response.strip()
        if response.startswith('do(action="Type"') or response.startswith(
            'do(action="Type_Name"'
        ):
            text = response.split("text=", 1)[1][1:-2]
            action = {"_metadata": "do", "action": "Type", "text": text}
            return action
        elif response.startswith("do"):
            # Use AST parsing instead of eval for safety
            try:
                # Escape special characters (newlines, tabs, etc.) for valid Python syntax
                response = response.replace('\n', '\\n')
                response = response.replace('\r', '\\r')
                response = response.replace('\t', '\\t')

                tree = ast.parse(response, mode="eval")
                if not isinstance(tree.body, ast.Call):
                    raise ValueError("Expected a function call")

                call = tree.body
                # Extract keyword arguments safely
                action = {"_metadata": "do"}
                for keyword in call.keywords:
                    key = keyword.arg
                    value = ast.literal_eval(keyword.value)
                    action[key] = value

                return action
            except (SyntaxError, ValueError) as e:
                raise ValueError(f"Failed to parse do() action: {e}")

        elif response.startswith("finish"):
            action = {
                "_metadata": "finish",
                "message": response.replace("finish(message=", "")[1:-2],
            }
        else:
            raise ValueError(f"Failed to parse action: {response}")
        return action
    except Exception as e:
        raise ValueError(f"Failed to parse action: {e}")


def do(**kwargs) -> dict[str, Any]:
    """Helper function for creating 'do' actions."""
    kwargs["_metadata"] = "do"
    return kwargs


def finish(**kwargs) -> dict[str, Any]:
    """Helper function for creating 'finish' actions."""
    kwargs["_metadata"] = "finish"
    return kwargs
