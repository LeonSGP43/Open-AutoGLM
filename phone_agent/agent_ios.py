"""iOS PhoneAgent class for orchestrating iOS phone automation."""

import copy
import json
import os
import random
import re
import time
import traceback
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Callable

from phone_agent.actions.handler import do, finish, parse_action
from phone_agent.actions.handler_ios import IOSActionHandler
from phone_agent.config import get_messages, get_system_prompt
from phone_agent.experience import ExperienceHint, ExperienceStore
from phone_agent.model import ModelClient, ModelConfig
from phone_agent.model.client import MessageBuilder, ModelResponse
from phone_agent.navigation import NavigationActionHint, NavigationMapStore
from phone_agent.skills import build_task_skill_prompt
from phone_agent.xctest import XCTestConnection, get_current_app, get_screenshot


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


@dataclass
class IOSAgentConfig:
    """Configuration for the iOS PhoneAgent."""

    max_steps: int = 100
    wda_url: str = "http://localhost:8100"
    session_id: str | None = None
    device_id: str | None = None  # iOS device UDID
    lang: str = "cn"
    system_prompt: str | None = None
    verbose: bool = True
    save_token_usage: bool = True
    token_usage_dir: str = "artifacts/token_usage"
    experience_fast_path: bool = True
    experience_fast_path_confidence: float = 0.72
    experience_fast_path_min_attempts: int = 3
    experience_fast_path_max_streak: int = 4
    experience_exploration_rate: float = 0.08
    experience_fast_path_exact_only: bool = True
    experience_sensitive_gate: bool = True
    navigation_map_enabled: bool = True
    navigation_fast_path: bool = False
    navigation_fast_path_confidence: float = 0.82
    navigation_fast_path_min_attempts: int = 6

    def __post_init__(self):
        if self.system_prompt is None:
            self.system_prompt = get_system_prompt(self.lang)
        self.experience_fast_path = _env_flag(
            "PHONE_AGENT_EXPERIENCE_FAST_PATH", self.experience_fast_path
        )
        self.experience_fast_path_confidence = _env_float(
            "PHONE_AGENT_EXPERIENCE_FAST_PATH_CONFIDENCE",
            self.experience_fast_path_confidence,
            minimum=0.0,
            maximum=1.0,
        )
        self.experience_fast_path_min_attempts = _env_int(
            "PHONE_AGENT_EXPERIENCE_FAST_PATH_MIN_ATTEMPTS",
            self.experience_fast_path_min_attempts,
            minimum=1,
        )
        self.experience_fast_path_max_streak = _env_int(
            "PHONE_AGENT_EXPERIENCE_FAST_PATH_MAX_STREAK",
            self.experience_fast_path_max_streak,
            minimum=1,
        )
        self.experience_exploration_rate = _env_float(
            "PHONE_AGENT_EXPERIENCE_EXPLORATION_RATE",
            self.experience_exploration_rate,
            minimum=0.0,
            maximum=1.0,
        )
        self.experience_fast_path_exact_only = _env_flag(
            "PHONE_AGENT_EXPERIENCE_FAST_PATH_EXACT_ONLY",
            self.experience_fast_path_exact_only,
        )
        self.experience_sensitive_gate = _env_flag(
            "PHONE_AGENT_EXPERIENCE_SENSITIVE_GATE",
            self.experience_sensitive_gate,
        )
        self.navigation_map_enabled = _env_flag(
            "PHONE_AGENT_NAVIGATION_MAP_ENABLED", self.navigation_map_enabled
        )
        self.navigation_fast_path = _env_flag(
            "PHONE_AGENT_NAVIGATION_FAST_PATH", self.navigation_fast_path
        )
        self.navigation_fast_path_confidence = _env_float(
            "PHONE_AGENT_NAVIGATION_FAST_PATH_CONFIDENCE",
            self.navigation_fast_path_confidence,
            minimum=0.0,
            maximum=1.0,
        )
        self.navigation_fast_path_min_attempts = _env_int(
            "PHONE_AGENT_NAVIGATION_FAST_PATH_MIN_ATTEMPTS",
            self.navigation_fast_path_min_attempts,
            minimum=1,
        )


@dataclass
class StepResult:
    """Result of a single agent step."""

    success: bool
    finished: bool
    action: dict[str, Any] | None
    thinking: str
    message: str | None = None


class IOSPhoneAgent:
    """
    AI-powered agent for automating iOS phone interactions.

    The agent uses a vision-language model to understand screen content
    and decide on actions to complete user tasks via WebDriverAgent.

    Args:
        model_config: Configuration for the AI model.
        agent_config: Configuration for the iOS agent behavior.
        confirmation_callback: Optional callback for sensitive action confirmation.
        takeover_callback: Optional callback for takeover requests.

    Example:
        >>> from phone_agent.agent_ios import IOSPhoneAgent, IOSAgentConfig
        >>> from phone_agent.model import ModelConfig
        >>>
        >>> model_config = ModelConfig(base_url="http://localhost:8000/v1")
        >>> agent_config = IOSAgentConfig(wda_url="http://localhost:8100")
        >>> agent = IOSPhoneAgent(model_config, agent_config)
        >>> agent.run("Open Safari and search for Apple")
    """

    def __init__(
        self,
        model_config: ModelConfig | None = None,
        agent_config: IOSAgentConfig | None = None,
        confirmation_callback: Callable[[str], bool] | None = None,
        takeover_callback: Callable[[str], None] | None = None,
    ):
        self.model_config = model_config or ModelConfig()
        self.agent_config = agent_config or IOSAgentConfig()

        self.model_client = ModelClient(self.model_config)

        # Initialize WDA connection and create session if needed
        self.wda_connection = XCTestConnection(wda_url=self.agent_config.wda_url)

        # Auto-create session if not provided
        if self.agent_config.session_id is None:
            success, session_id = self.wda_connection.start_wda_session()
            if success and session_id != "session_started":
                self.agent_config.session_id = session_id
                if self.agent_config.verbose:
                    print(f"✅ Created WDA session: {session_id}")
            elif self.agent_config.verbose:
                print(f"⚠️  Using default WDA session (no explicit session ID)")

        self.action_handler = IOSActionHandler(
            wda_url=self.agent_config.wda_url,
            session_id=self.agent_config.session_id,
            confirmation_callback=confirmation_callback,
            takeover_callback=takeover_callback,
        )

        self._context: list[dict[str, Any]] = []
        self._step_count = 0
        self._runtime_system_prompt: str = self.agent_config.system_prompt or get_system_prompt(
            self.agent_config.lang
        )
        self._active_task_skills: list[str] = []
        self._experience_store = ExperienceStore()
        self._navigation_store = NavigationMapStore(enabled=self.agent_config.navigation_map_enabled)
        self._current_task: str = ""
        self._token_usage_log_file: str | None = None
        self._token_usage_totals: dict[str, int] = {}
        self._fast_path_streak = 0
        self._pending_transition: dict[str, Any] | None = None

    def run(self, task: str) -> str:
        """
        Run the agent to complete a task.

        Args:
            task: Natural language description of the task.

        Returns:
            Final message from the agent.
        """
        self._context = []
        self._step_count = 0
        self._fast_path_streak = 0
        self._pending_transition = None
        self._current_task = task or ""
        self._prepare_runtime_system_prompt(task)
        self._start_token_usage_log()

        # First step with user prompt
        result = self._execute_step(task, is_first=True)

        if result.finished:
            self._pending_transition = None
            return result.message or "Task completed"

        # Continue until finished or max steps reached
        while self._step_count < self.agent_config.max_steps:
            result = self._execute_step(is_first=False)

            if result.finished:
                self._pending_transition = None
                return result.message or "Task completed"

        return "Max steps reached"

    def step(self, task: str | None = None) -> StepResult:
        """
        Execute a single step of the agent.

        Useful for manual control or debugging.

        Args:
            task: Task description (only needed for first step).

        Returns:
            StepResult with step details.
        """
        is_first = len(self._context) == 0

        if is_first and not task:
            raise ValueError("Task is required for the first step")

        if is_first and task:
            self._current_task = task
            self._fast_path_streak = 0
            self._pending_transition = None
            self._prepare_runtime_system_prompt(task)
            self._start_token_usage_log()

        return self._execute_step(task, is_first)

    def reset(self) -> None:
        """Reset the agent state for a new task."""
        self._context = []
        self._step_count = 0
        self._runtime_system_prompt = self.agent_config.system_prompt or get_system_prompt(
            self.agent_config.lang
        )
        self._active_task_skills = []
        self._current_task = ""
        self._token_usage_log_file = None
        self._token_usage_totals = {}
        self._fast_path_streak = 0
        self._pending_transition = None

    def _start_token_usage_log(self) -> None:
        """Initialize per-run token usage log file."""
        if not self.agent_config.save_token_usage:
            self._token_usage_log_file = None
            self._token_usage_totals = {}
            return
        if self._token_usage_log_file:
            return

        os.makedirs(self.agent_config.token_usage_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", self.model_config.model_name)
        safe_device = re.sub(
            r"[^A-Za-z0-9._-]+", "_", self.agent_config.device_id or "auto"
        )
        filename = (
            f"token_usage_{timestamp}_{self.model_config.provider}_"
            f"{safe_model}_{safe_device}_{os.getpid()}.jsonl"
        )
        self._token_usage_log_file = os.path.join(self.agent_config.token_usage_dir, filename)
        self._token_usage_totals = {}

        run_start_record = {
            "event": "run_start",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "provider": self.model_config.provider,
            "model": self.model_config.model_name,
            "device_id": self.agent_config.device_id or "auto",
            "max_steps": self.agent_config.max_steps,
            "task": self._current_task,
        }
        with open(self._token_usage_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(run_start_record, ensure_ascii=False) + "\n")
        if self.agent_config.verbose:
            print(f"[usage] token usage log: {self._token_usage_log_file}")

    def _record_token_usage(
        self,
        response: ModelResponse | None,
        current_app: str,
        action: dict[str, Any],
        success: bool,
        finished: bool,
        decision_source: str,
    ) -> None:
        """Append one JSONL record for each step with token usage."""
        if not self.agent_config.save_token_usage:
            return
        if not self._token_usage_log_file:
            self._start_token_usage_log()
        if not self._token_usage_log_file:
            return

        usage = response.usage if response else {}
        if usage is None:
            usage = {}
        for key, value in usage.items():
            if isinstance(value, int):
                self._token_usage_totals[key] = self._token_usage_totals.get(key, 0) + value

        approx_total_tokens = usage.get("total_tokens")
        if approx_total_tokens is None:
            prompt_like = usage.get("prompt_tokens", usage.get("input_tokens"))
            completion_like = usage.get("completion_tokens", usage.get("output_tokens"))
            if isinstance(prompt_like, int) and isinstance(completion_like, int):
                approx_total_tokens = prompt_like + completion_like

        record = {
            "event": "step",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "step": self._step_count,
            "provider": self.model_config.provider,
            "model": self.model_config.model_name,
            "current_app": current_app,
            "action_metadata": action.get("_metadata"),
            "action_name": action.get("action"),
            "success": success,
            "finished": finished,
            "decision_source": decision_source,
            "usage": usage,
            "approx_total_tokens": approx_total_tokens,
            "running_totals": self._token_usage_totals,
        }
        with open(self._token_usage_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _append_experience_hint(
        self, text_content: str, hint: ExperienceHint | None
    ) -> str:
        """Append compact experience hint into current step prompt."""
        if hint is None:
            return text_content
        action_json = json.dumps(hint.action, ensure_ascii=False)
        if len(action_json) > 320:
            action_json = action_json[:320] + "..."
        if self.agent_config.lang == "en":
            hint_text = (
                "Historical best action for similar state "
                f"(source={hint.source}, confidence={hint.confidence:.2f}, "
                f"attempts={hint.attempts}, success_rate={hint.success_rate:.0%}): {action_json}. "
                "Prefer this action if the current UI matches."
            )
            return f"{text_content}\n\n** Experience Hint **\n{hint_text}"
        hint_text = (
            "相似状态历史较优动作"
            f"（来源={hint.source}，置信度={hint.confidence:.2f}，样本={hint.attempts}，成功率={hint.success_rate:.0%}）："
            f"{action_json}。若当前界面匹配，优先尝试该动作。"
        )
        return f"{text_content}\n\n** 经验提示 **\n{hint_text}"

    def _prepare_runtime_system_prompt(self, task: str) -> None:
        """Build task-aware runtime system prompt with matched task skills."""
        base_prompt = self.agent_config.system_prompt or get_system_prompt(self.agent_config.lang)
        skill_prompt, skill_names = build_task_skill_prompt(task, self.agent_config.lang)
        self._active_task_skills = skill_names
        if skill_prompt:
            self._runtime_system_prompt = f"{base_prompt}\n\n{skill_prompt}"
            if self.agent_config.verbose:
                print(f"[task-skills] activated: {', '.join(skill_names)}")
        else:
            self._runtime_system_prompt = base_prompt

    @staticmethod
    def _is_retryable_model_error(error: Exception) -> bool:
        """Return True when model error is likely transient or policy-filter retryable."""
        message = str(error).lower()
        retry_keywords = (
            "content filtering policy",
            "http error 500",
            "anthropic api error (500)",
            "service unavailable",
            "connection reset",
            "connection aborted",
            "timed out",
            "temporarily unavailable",
        )
        return any(keyword in message for keyword in retry_keywords)

    @staticmethod
    def _strip_assistant_thinking(content: Any) -> Any:
        """Drop verbose assistant thinking blocks to reduce risky context carry-over."""
        if not isinstance(content, str):
            return content
        stripped = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if stripped:
            return stripped
        return '<answer>do(action="Wait", duration="1 seconds")</answer>'

    def _build_retry_context(self, attempt: int) -> list[dict[str, Any]]:
        """Build a compact context for retry to improve robustness."""
        if not self._context:
            return []

        compact_context: list[dict[str, Any]] = []
        has_system = self._context and self._context[0].get("role") == "system"

        start_idx = 1 if has_system else 0
        base_messages = self._context[start_idx:]
        tail_window = max(6, 12 - attempt * 2)
        tail_messages = base_messages[-tail_window:]

        if has_system:
            compact_context.append(copy.deepcopy(self._context[0]))
            compact_context.append(
                MessageBuilder.create_system_message(
                    "Stability requirement: provide minimal reasoning and one valid action only."
                )
            )

        for message in tail_messages:
            msg = copy.deepcopy(message)
            if msg.get("role") == "assistant":
                msg["content"] = self._strip_assistant_thinking(msg.get("content"))
            compact_context.append(msg)

        return compact_context

    @staticmethod
    def _is_fast_path_safe_action(action: dict[str, Any]) -> bool:
        """Allow fast-path only for bounded, non-sensitive action types."""
        if not isinstance(action, dict):
            return False
        if action.get("_metadata") != "do":
            return False
        action_name = str(action.get("action", "") or "")
        allowed = {
            "Launch",
            "Tap",
            "Swipe",
            "Back",
            "Home",
            "Double Tap",
            "Long Press",
            "Wait",
            "Type",
            "Type_Name",
        }
        return action_name in allowed

    @staticmethod
    def _action_to_context_text(action: dict[str, Any]) -> str:
        """Serialize action dict into stable text for assistant context."""
        if not isinstance(action, dict):
            return str(action)
        meta = action.get("_metadata")
        if meta == "finish":
            message = action.get("message", "")
            return f"finish(message={json.dumps(message, ensure_ascii=False)})"
        if meta != "do":
            return json.dumps(action, ensure_ascii=False)
        fields = []
        if "action" in action:
            fields.append(f"action={json.dumps(action.get('action'), ensure_ascii=False)}")
        for key, value in action.items():
            if key in {"_metadata", "action"}:
                continue
            fields.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
        return f"do({', '.join(fields)})"

    def _should_use_fast_path(
        self,
        hint: ExperienceHint | None,
        current_app: str,
        screen_hash: str,
    ) -> bool:
        """Gate fast-path execution with safety, confidence and exploration checks."""
        if hint is None:
            return False
        if not self.agent_config.experience_fast_path:
            return False
        if self.agent_config.experience_fast_path_exact_only and hint.source != "exact":
            return False
        if self._fast_path_streak >= self.agent_config.experience_fast_path_max_streak:
            return False
        if hint.attempts < self.agent_config.experience_fast_path_min_attempts:
            return False
        if hint.confidence < self.agent_config.experience_fast_path_confidence:
            return False
        if hint.consecutive_failures > 0:
            return False
        if not self._is_fast_path_safe_action(hint.action):
            return False
        if self.agent_config.experience_sensitive_gate and self._is_sensitive_fast_path_action(
            hint.action, current_app
        ):
            return False
        action_name = str(hint.action.get("action", "") or "")
        if action_name in {"Tap", "Type", "Type_Name"}:
            # Enforce stricter thresholds on direct mutation actions.
            if hint.confidence < max(0.90, self.agent_config.experience_fast_path_confidence):
                return False
            if hint.attempts < max(8, self.agent_config.experience_fast_path_min_attempts):
                return False
        if self._experience_store.is_action_in_cooldown(
            task=self._current_task,
            current_app=current_app,
            screen_hash=screen_hash,
            action=hint.action,
        ):
            return False
        if self.agent_config.experience_exploration_rate > 0:
            if random.random() < self.agent_config.experience_exploration_rate:
                if self.agent_config.verbose:
                    print("[experience-fast-path] skipped due to exploration")
                return False
        return True

    def _resolve_pending_transition(self, current_state_id: str) -> None:
        """Resolve previous-step transition once the next state is observed."""
        pending = self._pending_transition
        if pending is None:
            return
        self._pending_transition = None
        if not current_state_id:
            return
        try:
            self._navigation_store.observe_transition(
                from_state_id=str(pending.get("from_state_id") or ""),
                to_state_id=current_state_id,
                action=pending.get("action") or {},
                success=bool(pending.get("success")),
                latency_ms=int(pending.get("latency_ms") or 0),
            )
        except Exception:
            return

    def _should_use_navigation_fast_path(
        self, hint: NavigationActionHint | None, current_app: str
    ) -> bool:
        """Gate map-derived fast path using conservative safety checks."""
        if hint is None:
            return False
        if not self.agent_config.navigation_fast_path:
            return False
        if self._fast_path_streak >= self.agent_config.experience_fast_path_max_streak:
            return False
        if hint.attempts < self.agent_config.navigation_fast_path_min_attempts:
            return False
        if hint.confidence < self.agent_config.navigation_fast_path_confidence:
            return False
        if hint.consecutive_failures > 0:
            return False
        if not self._is_fast_path_safe_action(hint.action):
            return False
        if self.agent_config.experience_sensitive_gate and self._is_sensitive_fast_path_action(
            hint.action, current_app
        ):
            return False
        action_name = str(hint.action.get("action", "") or "")
        if action_name in {"Tap", "Type", "Type_Name"}:
            if hint.confidence < max(0.92, self.agent_config.navigation_fast_path_confidence):
                return False
            if hint.attempts < max(10, self.agent_config.navigation_fast_path_min_attempts):
                return False
        return True

    @staticmethod
    def _contains_sensitive_keyword(text: str) -> bool:
        normalized = (text or "").lower()
        if not normalized:
            return False
        keywords = (
            "支付",
            "付款",
            "转账",
            "收款",
            "红包",
            "银行",
            "银行卡",
            "提现",
            "充值",
            "下单",
            "购买",
            "订单",
            "提交",
            "确认支付",
            "删除",
            "清空",
            "移除",
            "发送",
            "群发",
            "发布",
            "授权",
            "同意",
            "允许",
            "验证码",
            "密码",
            "payment",
            "pay",
            "transfer",
            "bank",
            "withdraw",
            "checkout",
            "order",
            "purchase",
            "delete",
            "remove",
            "send",
            "submit",
            "authorize",
            "approve",
            "password",
            "otp",
            "verification code",
        )
        return any(keyword in normalized for keyword in keywords)

    def _is_sensitive_fast_path_action(self, action: dict[str, Any], current_app: str) -> bool:
        """Block fast-path for risky contexts on tap/type style actions."""
        if not isinstance(action, dict):
            return True
        action_name = str(action.get("action", "") or "")
        if action_name not in {"Tap", "Type", "Type_Name"}:
            return False
        if str(action.get("message", "") or "").strip():
            return True
        text = str(action.get("text", "") or "")
        context_blob = " ".join(
            [
                self._current_task or "",
                current_app or "",
                text,
            ]
        )
        return self._contains_sensitive_keyword(context_blob)

    def _execute_step(
        self, user_prompt: str | None = None, is_first: bool = False
    ) -> StepResult:
        """Execute a single step of the agent loop."""
        self._step_count += 1

        # Capture current screen state
        screenshot = get_screenshot(
            wda_url=self.agent_config.wda_url,
            session_id=self.agent_config.session_id,
            device_id=self.agent_config.device_id,
        )
        current_app = get_current_app(
            wda_url=self.agent_config.wda_url, session_id=self.agent_config.session_id
        )
        screen_hash = ExperienceStore.build_screen_hash(
            screenshot.base64_data, screenshot.width, screenshot.height
        )
        current_state_id = self._navigation_store.observe_state(current_app=current_app, screen_hash=screen_hash)
        self._resolve_pending_transition(current_state_id)
        hint = self._experience_store.get_hint(
            task=self._current_task or (user_prompt or ""),
            current_app=current_app,
            screen_hash=screen_hash,
        )
        if hint and self.agent_config.verbose:
            print(
                f"[experience] hint source={hint.source} conf={hint.confidence:.2f} "
                f"attempts={hint.attempts} success={hint.success_rate:.0%}"
            )
        nav_hint = self._navigation_store.get_best_action(
            from_state_id=current_state_id,
            min_attempts=self.agent_config.navigation_fast_path_min_attempts,
            min_confidence=self.agent_config.navigation_fast_path_confidence,
        )
        if nav_hint and self.agent_config.verbose:
            print(
                f"[navigation] hint conf={nav_hint.confidence:.2f} "
                f"attempts={nav_hint.attempts} success={nav_hint.success_rate:.0%}"
            )

        # Build messages
        if is_first:
            self._context.append(
                MessageBuilder.create_system_message(self._runtime_system_prompt)
            )

            screen_info = MessageBuilder.build_screen_info(
                current_app,
                screen_width=screenshot.width,
                screen_height=screenshot.height,
            )
            text_content = f"{user_prompt}\n\n{screen_info}"
            text_content = self._append_experience_hint(text_content, hint)

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )
        else:
            screen_info = MessageBuilder.build_screen_info(
                current_app,
                screen_width=screenshot.width,
                screen_height=screenshot.height,
            )
            text_content = f"** Screen Info **\n\n{screen_info}"
            text_content = self._append_experience_hint(text_content, hint)

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )

        msgs = get_messages(self.agent_config.lang)
        print("\n" + "=" * 50)
        print(f"💭 {msgs['thinking']}:")
        print("-" * 50)

        response: ModelResponse | None = None
        action: dict[str, Any] | None = None
        decision_source = "model"
        thinking_for_step = ""

        if self._should_use_fast_path(hint, current_app, screen_hash):
            decision_source = "experience_fast_path"
            action = copy.deepcopy(hint.action)
            thinking_for_step = (
                "Using high-confidence historical action "
                f"(source={hint.source}, confidence={hint.confidence:.2f}, attempts={hint.attempts})."
            )
            if self.agent_config.verbose:
                print(f"[experience-fast-path] {thinking_for_step}")
                print("-" * 50)
                print(f"🎯 {msgs['action']}:")
                print(json.dumps(action, ensure_ascii=False, indent=2))
                print("=" * 50 + "\n")
        elif self._should_use_navigation_fast_path(nav_hint, current_app):
            decision_source = "navigation_fast_path"
            action = copy.deepcopy(nav_hint.action)
            thinking_for_step = (
                "Using map transition action "
                f"(confidence={nav_hint.confidence:.2f}, attempts={nav_hint.attempts})."
            )
            if self.agent_config.verbose:
                print(f"[navigation-fast-path] {thinking_for_step}")
                print("-" * 50)
                print(f"🎯 {msgs['action']}:")
                print(json.dumps(action, ensure_ascii=False, indent=2))
                print("=" * 50 + "\n")
        else:
            # Get model response
            try:
                last_error: Exception | None = None
                max_attempts = 3

                for attempt in range(max_attempts):
                    try:
                        response = self.model_client.request(self._context)
                        break
                    except Exception as e:
                        last_error = e
                        should_retry = attempt < (max_attempts - 1) and self._is_retryable_model_error(e)
                        if not should_retry:
                            raise
                        if self.agent_config.verbose:
                            print(
                                f"[model-retry] attempt {attempt + 1}/{max_attempts} failed: {e}"
                            )
                        self._context = self._build_retry_context(attempt)
                        if self.agent_config.verbose:
                            print(
                                f"[model-retry] retrying with compact context size={len(self._context)}"
                            )

                if response is None:
                    raise RuntimeError(f"Model response is empty after retries: {last_error}")
            except Exception as e:
                if self.agent_config.verbose:
                    traceback.print_exc()
                return StepResult(
                    success=False,
                    finished=True,
                    action=None,
                    thinking="",
                    message=f"Model error: {e}",
                )

            # Parse action from response
            try:
                action = parse_action(response.action)
            except ValueError:
                if self.agent_config.verbose:
                    traceback.print_exc()
                action = finish(message=response.action)
            thinking_for_step = response.thinking

            if self.agent_config.verbose:
                # Print thinking process
                print("-" * 50)
                print(f"🎯 {msgs['action']}:")
                print(json.dumps(action, ensure_ascii=False, indent=2))
                print("=" * 50 + "\n")

        if action is None:
            return StepResult(
                success=False,
                finished=True,
                action=None,
                thinking="",
                message="No action resolved from fast path/model.",
            )

        # Remove image from context to save space
        self._context[-1] = MessageBuilder.remove_images_from_message(self._context[-1])

        # Execute action
        action_started_at = time.perf_counter()
        try:
            result = self.action_handler.execute(
                action, screenshot.width, screenshot.height
            )
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            result = self.action_handler.execute(
                finish(message=str(e)), screenshot.width, screenshot.height
            )
        action_latency_ms = int((time.perf_counter() - action_started_at) * 1000)

        # Add assistant response to context
        action_text = (
            response.action if response is not None else self._action_to_context_text(action)
        )
        self._context.append(
            MessageBuilder.create_assistant_message(
                f"<think>{thinking_for_step}</think><answer>{action_text}</answer>"
            )
        )

        # Check if finished
        finished = action.get("_metadata") == "finish" or result.should_finish

        self._record_token_usage(
            response=response,
            current_app=current_app,
            action=action,
            success=result.success,
            finished=finished,
            decision_source=decision_source,
        )

        if decision_source in {"experience_fast_path", "navigation_fast_path"} and result.success:
            self._fast_path_streak += 1
        else:
            self._fast_path_streak = 0

        if finished and self.agent_config.verbose:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "🎉 " + "=" * 48)
            print(
                f"✅ {msgs['task_completed']}: {result.message or action.get('message', msgs['done'])}"
            )
            print("=" * 50 + "\n")

        action_name = str(action.get("action", "") or "")
        skip_learning = action_name in {"Take_over", "Interact"}
        if action.get("_metadata") == "do" and not skip_learning and current_state_id:
            self._pending_transition = {
                "from_state_id": current_state_id,
                "action": copy.deepcopy(action),
                "success": bool(result.success),
                "latency_ms": action_latency_ms,
            }
        else:
            self._pending_transition = None
        if not skip_learning:
            self._experience_store.observe(
                task=self._current_task or (user_prompt or ""),
                current_app=current_app,
                screen_hash=screen_hash,
                action=action,
                success=result.success,
                finished=finished,
                message=result.message or action.get("message"),
            )

        return StepResult(
            success=result.success,
            finished=finished,
            action=action,
            thinking=thinking_for_step,
            message=result.message or action.get("message"),
        )

    @property
    def context(self) -> list[dict[str, Any]]:
        """Get the current conversation context."""
        return self._context.copy()

    @property
    def step_count(self) -> int:
        """Get the current step count."""
        return self._step_count
