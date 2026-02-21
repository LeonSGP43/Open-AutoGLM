"""iOS PhoneAgent class for orchestrating iOS phone automation."""

import copy
import json
import re
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from phone_agent.actions.handler import do, finish, parse_action
from phone_agent.actions.handler_ios import IOSActionHandler
from phone_agent.config import get_messages, get_system_prompt
from phone_agent.experience import ExperienceHint, ExperienceStore
from phone_agent.model import ModelClient, ModelConfig
from phone_agent.model.client import MessageBuilder
from phone_agent.skills import build_task_skill_prompt
from phone_agent.xctest import XCTestConnection, get_current_app, get_screenshot


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

    def __post_init__(self):
        if self.system_prompt is None:
            self.system_prompt = get_system_prompt(self.lang)


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
        self._current_task: str = ""

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
        self._current_task = task or ""
        self._prepare_runtime_system_prompt(task)

        # First step with user prompt
        result = self._execute_step(task, is_first=True)

        if result.finished:
            return result.message or "Task completed"

        # Continue until finished or max steps reached
        while self._step_count < self.agent_config.max_steps:
            result = self._execute_step(is_first=False)

            if result.finished:
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
            self._prepare_runtime_system_prompt(task)

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

        # Get model response
        try:
            response = None
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

        if self.agent_config.verbose:
            # Print thinking process
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "=" * 50)
            print(f"💭 {msgs['thinking']}:")
            print("-" * 50)
            print(response.thinking)
            print("-" * 50)
            print(f"🎯 {msgs['action']}:")
            print(json.dumps(action, ensure_ascii=False, indent=2))
            print("=" * 50 + "\n")

        # Remove image from context to save space
        self._context[-1] = MessageBuilder.remove_images_from_message(self._context[-1])

        # Execute action
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

        # Add assistant response to context
        self._context.append(
            MessageBuilder.create_assistant_message(
                f"<think>{response.thinking}</think><answer>{response.action}</answer>"
            )
        )

        # Check if finished
        finished = action.get("_metadata") == "finish" or result.should_finish

        if finished and self.agent_config.verbose:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "🎉 " + "=" * 48)
            print(
                f"✅ {msgs['task_completed']}: {result.message or action.get('message', msgs['done'])}"
            )
            print("=" * 50 + "\n")

        action_name = str(action.get("action", "") or "")
        skip_learning = action_name in {"Take_over", "Interact"}
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
            thinking=response.thinking,
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
