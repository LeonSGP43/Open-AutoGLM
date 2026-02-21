"""Model client for AI inference using OpenAI- or Anthropic-compatible API."""

import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional when using anthropic provider only
    OpenAI = None

from phone_agent.config.i18n import get_message


@dataclass
class ModelConfig:
    """Configuration for the AI model."""

    base_url: str = "http://localhost:8000/v1"
    api_key: str = "EMPTY"
    provider: str = "openai"  # Supported: "openai", "anthropic"
    model_name: str = "autoglm-phone-9b"
    max_tokens: int = 3000
    temperature: float = 0.0
    top_p: float = 0.85
    frequency_penalty: float = 0.2
    anthropic_version: str = "2023-06-01"
    timeout: float = 120.0
    extra_body: dict[str, Any] = field(default_factory=dict)
    lang: str = "cn"  # Language for UI messages: 'cn' or 'en'

    def __post_init__(self) -> None:
        self.provider = self.provider.lower().strip()
        if self.provider not in {"openai", "anthropic"}:
            raise ValueError(
                f"Unsupported provider '{self.provider}'. "
                "Supported providers are: openai, anthropic."
            )


@dataclass
class ModelResponse:
    """Response from the AI model."""

    thinking: str
    action: str
    raw_content: str
    # Performance metrics
    time_to_first_token: float | None = None  # Time to first token (seconds)
    time_to_thinking_end: float | None = None  # Time to thinking end (seconds)
    total_time: float | None = None  # Total inference time (seconds)


class ModelClient:
    """
    Client for interacting with OpenAI- or Anthropic-compatible vision-language models.

    Args:
        config: Model configuration.
    """

    def __init__(self, config: ModelConfig | None = None):
        self.config = config or ModelConfig()
        self.client = None
        self.anthropic_messages_url = None

        if self.config.provider == "openai":
            if OpenAI is None:
                raise ImportError(
                    "openai package is required for provider=openai. "
                    "Install it with: pip install openai"
                )
            self.client = OpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                timeout=self.config.timeout,
            )
        else:
            self.anthropic_messages_url = self._build_anthropic_messages_url(
                self.config.base_url
            )

    def request(self, messages: list[dict[str, Any]]) -> ModelResponse:
        """
        Send a request to the model.

        Args:
            messages: List of message dictionaries in OpenAI format.

        Returns:
            ModelResponse containing thinking and action.

        Raises:
            ValueError: If the response cannot be parsed.
        """
        if self.config.provider == "anthropic":
            return self._request_anthropic(messages)
        return self._request_openai(messages)

    def _request_openai(self, messages: list[dict[str, Any]]) -> ModelResponse:
        """Send a request via OpenAI-compatible chat completions."""
        # Start timing
        start_time = time.time()
        time_to_first_token = None
        time_to_thinking_end = None

        if self.client is None:
            raise ValueError("OpenAI client is not initialized.")

        stream = self.client.chat.completions.create(
            messages=messages,
            model=self.config.model_name,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            top_p=self.config.top_p,
            frequency_penalty=self.config.frequency_penalty,
            extra_body=self.config.extra_body,
            stream=True,
        )

        raw_content = ""
        buffer = ""  # Buffer to hold content that might be part of a marker
        action_markers = ["finish(message=", "do(action="]
        in_action_phase = False  # Track if we've entered the action phase
        first_token_received = False

        for chunk in stream:
            if len(chunk.choices) == 0:
                continue
            if chunk.choices[0].delta.content is not None:
                content = chunk.choices[0].delta.content
                raw_content += content

                # Record time to first token
                if not first_token_received:
                    time_to_first_token = time.time() - start_time
                    first_token_received = True

                if in_action_phase:
                    # Already in action phase, just accumulate content without printing
                    continue

                buffer += content

                # Check if any marker is fully present in buffer
                marker_found = False
                for marker in action_markers:
                    if marker in buffer:
                        # Marker found, print everything before it
                        thinking_part = buffer.split(marker, 1)[0]
                        print(thinking_part, end="", flush=True)
                        print()  # Print newline after thinking is complete
                        in_action_phase = True
                        marker_found = True

                        # Record time to thinking end
                        if time_to_thinking_end is None:
                            time_to_thinking_end = time.time() - start_time

                        break

                if marker_found:
                    continue  # Continue to collect remaining content

                # Check if buffer ends with a prefix of any marker
                # If so, don't print yet (wait for more content)
                is_potential_marker = False
                for marker in action_markers:
                    for i in range(1, len(marker)):
                        if buffer.endswith(marker[:i]):
                            is_potential_marker = True
                            break
                    if is_potential_marker:
                        break

                if not is_potential_marker:
                    # Safe to print the buffer
                    print(buffer, end="", flush=True)
                    buffer = ""

        # Calculate total time
        total_time = time.time() - start_time

        # Parse thinking and action from response
        thinking, action = self._parse_response(raw_content)

        self._print_performance_metrics(
            time_to_first_token=time_to_first_token,
            time_to_thinking_end=time_to_thinking_end,
            total_time=total_time,
        )

        return ModelResponse(
            thinking=thinking,
            action=action,
            raw_content=raw_content,
            time_to_first_token=time_to_first_token,
            time_to_thinking_end=time_to_thinking_end,
            total_time=total_time,
        )

    def _request_anthropic(self, messages: list[dict[str, Any]]) -> ModelResponse:
        """Send a request via Anthropic-compatible messages API."""
        start_time = time.time()
        system_prompt, anthropic_messages = self._convert_messages_to_anthropic(messages)

        payload: dict[str, Any] = {
            "model": self.config.model_name,
            "messages": anthropic_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if self.config.extra_body:
            payload.update(self.config.extra_body)

        if not self.anthropic_messages_url:
            raise ValueError("Anthropic endpoint is not initialized.")

        request = Request(
            self.anthropic_messages_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self.config.api_key,
                "anthropic-version": self.config.anthropic_version,
            },
        )

        try:
            with urlopen(request, timeout=self.config.timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Anthropic API error ({e.code}): {error_body}") from e
        except URLError as e:
            raise RuntimeError(f"Anthropic API connection error: {e.reason}") from e

        if response_data.get("type") == "error":
            error = response_data.get("error", {})
            err_type = error.get("type", "unknown_error")
            err_msg = error.get("message", "Unknown Anthropic API error")
            raise RuntimeError(f"Anthropic API error ({err_type}): {err_msg}")

        raw_content = self._extract_anthropic_text(response_data.get("content", []))
        thinking, action = self._parse_response(raw_content)

        if thinking:
            print(thinking, end="", flush=True)
            print()

        total_time = time.time() - start_time
        time_to_thinking_end = total_time if thinking else None

        self._print_performance_metrics(
            time_to_first_token=None,
            time_to_thinking_end=time_to_thinking_end,
            total_time=total_time,
        )

        return ModelResponse(
            thinking=thinking,
            action=action,
            raw_content=raw_content,
            time_to_first_token=None,
            time_to_thinking_end=time_to_thinking_end,
            total_time=total_time,
        )

    def _print_performance_metrics(
        self,
        time_to_first_token: float | None,
        time_to_thinking_end: float | None,
        total_time: float | None,
    ) -> None:
        """Print unified performance metrics for model inference."""
        lang = self.config.lang
        print()
        print("=" * 50)
        print(f"⏱️  {get_message('performance_metrics', lang)}:")
        print("-" * 50)
        if time_to_first_token is not None:
            print(
                f"{get_message('time_to_first_token', lang)}: {time_to_first_token:.3f}s"
            )
        if time_to_thinking_end is not None:
            print(
                f"{get_message('time_to_thinking_end', lang)}:        {time_to_thinking_end:.3f}s"
            )
        if total_time is not None:
            print(
                f"{get_message('total_inference_time', lang)}:          {total_time:.3f}s"
            )
        print("=" * 50)

    @staticmethod
    def _build_anthropic_messages_url(base_url: str) -> str:
        """Build Anthropic /v1/messages endpoint from a base URL."""
        normalized = base_url.rstrip("/")
        if normalized.endswith("/messages"):
            return normalized
        if normalized.endswith("/v1"):
            return normalized + "/messages"
        return urljoin(normalized + "/", "v1/messages")

    @staticmethod
    def _extract_anthropic_text(content_blocks: Any) -> str:
        """Extract text payload from Anthropic response content blocks."""
        if isinstance(content_blocks, str):
            return content_blocks
        if not isinstance(content_blocks, list):
            return ""

        texts: list[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"text", "thinking"} and isinstance(block.get("text"), str):
                texts.append(block["text"])
        return "".join(texts)

    def _convert_messages_to_anthropic(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """Convert internal OpenAI-style messages to Anthropic messages payload."""
        system_segments: list[str] = []
        anthropic_messages: list[dict[str, Any]] = []

        for message in messages:
            role = str(message.get("role", "user")).lower()
            content = message.get("content", "")

            if role == "system":
                system_text = self._extract_text_from_content(content)
                if system_text:
                    system_segments.append(system_text)
                continue

            if role not in {"user", "assistant"}:
                role = "user"

            anthropic_messages.append(
                {
                    "role": role,
                    "content": self._convert_content_to_anthropic_blocks(content),
                }
            )

        if not anthropic_messages:
            anthropic_messages.append(
                {"role": "user", "content": [{"type": "text", "text": ""}]}
            )

        return "\n\n".join(system_segments), anthropic_messages

    def _convert_content_to_anthropic_blocks(self, content: Any) -> list[dict[str, Any]]:
        """Convert one message content field into Anthropic content blocks."""
        if isinstance(content, str):
            return [{"type": "text", "text": content}]

        blocks: list[dict[str, Any]] = []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        blocks.append({"type": "text", "text": text})
                elif item_type == "image_url":
                    image_payload = item.get("image_url", {})
                    if isinstance(image_payload, dict):
                        image_url = image_payload.get("url")
                        image_block = self._convert_data_url_to_anthropic_image(
                            image_url
                        )
                        if image_block:
                            blocks.append(image_block)

        if not blocks:
            return [{"type": "text", "text": ""}]
        return blocks

    @staticmethod
    def _extract_text_from_content(content: Any) -> str:
        """Extract plain text from content field for system prompts."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        texts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join([t for t in texts if isinstance(t, str)])

    @staticmethod
    def _convert_data_url_to_anthropic_image(image_url: Any) -> dict[str, Any] | None:
        """Convert OpenAI image_url data URI to Anthropic image block."""
        if not isinstance(image_url, str):
            return None
        if not image_url.startswith("data:") or ";base64," not in image_url:
            return None

        meta_part, data_part = image_url.split(";base64,", 1)
        media_type = meta_part[5:] if len(meta_part) > 5 else "image/png"
        if not media_type:
            media_type = "image/png"

        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data_part,
            },
        }

    def _parse_response(self, content: str) -> tuple[str, str]:
        """
        Parse the model response into thinking and action parts.

        Parsing rules:
        1. If content contains 'finish(message=', everything before is thinking,
           everything from 'finish(message=' onwards is action.
        2. If rule 1 doesn't apply but content contains 'do(action=',
           everything before is thinking, everything from 'do(action=' onwards is action.
        3. Fallback: If content contains '<answer>', use legacy parsing with XML tags.
        4. Otherwise, return empty thinking and full content as action.

        Args:
            content: Raw response content.

        Returns:
            Tuple of (thinking, action).
        """
        # Rule 1: Check for finish(message=
        if "finish(message=" in content:
            parts = content.split("finish(message=", 1)
            thinking = parts[0].strip()
            action = "finish(message=" + parts[1]
            return thinking, action

        # Rule 2: Check for do(action=
        if "do(action=" in content:
            parts = content.split("do(action=", 1)
            thinking = parts[0].strip()
            action = "do(action=" + parts[1]
            return thinking, action

        # Rule 3: Fallback to legacy XML tag parsing
        if "<answer>" in content:
            parts = content.split("<answer>", 1)
            thinking = parts[0].replace("<think>", "").replace("</think>", "").strip()
            action = parts[1].replace("</answer>", "").strip()
            return thinking, action

        # Rule 4: No markers found, return content as action
        return "", content


class MessageBuilder:
    """Helper class for building conversation messages."""

    @staticmethod
    def create_system_message(content: str) -> dict[str, Any]:
        """Create a system message."""
        return {"role": "system", "content": content}

    @staticmethod
    def create_user_message(
        text: str, image_base64: str | None = None
    ) -> dict[str, Any]:
        """
        Create a user message with optional image.

        Args:
            text: Text content.
            image_base64: Optional base64-encoded image.

        Returns:
            Message dictionary.
        """
        content = []

        if image_base64:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_base64}"},
                }
            )

        content.append({"type": "text", "text": text})

        return {"role": "user", "content": content}

    @staticmethod
    def create_assistant_message(content: str) -> dict[str, Any]:
        """Create an assistant message."""
        return {"role": "assistant", "content": content}

    @staticmethod
    def remove_images_from_message(message: dict[str, Any]) -> dict[str, Any]:
        """
        Remove image content from a message to save context space.

        Args:
            message: Message dictionary.

        Returns:
            Message with images removed.
        """
        if isinstance(message.get("content"), list):
            message["content"] = [
                item for item in message["content"] if item.get("type") == "text"
            ]
        return message

    @staticmethod
    def build_screen_info(current_app: str, **extra_info) -> str:
        """
        Build screen info string for the model.

        Args:
            current_app: Current app name.
            **extra_info: Additional info to include.

        Returns:
            JSON string with screen info.
        """
        info = {"current_app": current_app, **extra_info}
        return json.dumps(info, ensure_ascii=False)
