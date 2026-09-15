"""
LLM abstraction for Sirius — Claude via Vertex AI, the direct Anthropic API, or
Amazon Bedrock (pick with `llm.backend` in config.yaml / the onboarding wizard).

Unlike Alfred's text-only `complete()`, Sirius needs native Anthropic tool-use
so the agent loop can call skills. `LLMBackend.create()` returns the raw
Anthropic `Message` (with tool_use blocks); `complete()` is a text convenience.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from sirius.config import SiriusConfig

_log = logging.getLogger("sirius.llm")


class LLMBackend(ABC):
    @abstractmethod
    def create(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        max_tokens: int = 4096,
        model: Optional[str] = None,
    ) -> Any:
        """Return the raw Anthropic Message (supports tool_use).

        `model` overrides the backend default for this call (multi-agent routing).
        """

    def stream(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        max_tokens: int = 4096,
        model: Optional[str] = None,
        on_text: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """Stream text deltas via on_text(delta); return the final Anthropic Message.

        Default (non-streaming) implementation: call create() and emit each text
        block once. Backends with a native streaming API should override this.
        """
        msg = self.create(messages, system, tools, max_tokens, model)
        if on_text:
            for b in msg.content:
                if getattr(b, "type", None) == "text" and getattr(b, "text", ""):
                    on_text(b.text)
        return msg

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        msg = self.create([{"role": "user", "content": prompt}], system=system)
        parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
        return "\n".join(parts).strip()

    @property
    def model(self) -> str:  # pragma: no cover - trivial
        return getattr(self, "_model", "unknown")


class _ClientBackend(LLMBackend):
    """Shared create()/stream() for any backend backed by an Anthropic-SDK client.

    Subclasses only build `self._client` (an `Anthropic`, `AnthropicVertex`, or
    `AnthropicBedrock`) and set `self._model` / `self._max_tokens`; all three
    clients expose the same `messages.create` / `messages.stream` surface.
    """

    _client: Any
    _model: str
    _max_tokens: int

    def create(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        max_tokens: int = 4096,
        model: Optional[str] = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model or self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        return self._client.messages.create(**kwargs)

    def stream(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        max_tokens: int = 4096,
        model: Optional[str] = None,
        on_text: Optional[Callable[[str], None]] = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model or self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        # text_stream yields only text deltas (tool_use/thinking are captured in the
        # final message). Runs inside run_sync's executor thread — on_text may emit.
        with self._client.messages.stream(**kwargs) as s:
            for text in s.text_stream:
                if on_text:
                    on_text(text)
            return s.get_final_message()


class VertexBackend(_ClientBackend):
    """Claude models served through Vertex AI via the anthropic[vertex] SDK.

    Credentials come from Application Default Credentials (ADC) —
    `gcloud auth application-default login` or GOOGLE_APPLICATION_CREDENTIALS.
    Project + region come from config (with env fallbacks).
    """

    def __init__(self, cfg: SiriusConfig) -> None:
        try:
            from anthropic import AnthropicVertex
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "anthropic[vertex] not installed. Run: pip install 'anthropic[vertex]'"
            ) from e

        project = cfg.vertex_project()
        region = cfg.vertex_region()
        if not project:
            raise RuntimeError(
                "Vertex project not set. Set llm.vertex_project in config.yaml or "
                "GOOGLE_CLOUD_PROJECT in .env."
            )
        self._client = AnthropicVertex(project_id=project, region=region)
        self._model = cfg.llm.model
        self._max_tokens = cfg.llm.max_tokens
        _log.info("Vertex backend ready (project=%s region=%s model=%s)",
                  project, region, self._model)


class AnthropicBackend(_ClientBackend):
    """Claude via the direct Anthropic API (console.anthropic.com API key).

    The key is read from ANTHROPIC_API_KEY in the environment (.env) — never from
    config.yaml. Model ids are the plain Anthropic ids (e.g. `claude-sonnet-5`).
    """

    def __init__(self, cfg: SiriusConfig) -> None:
        try:
            from anthropic import Anthropic
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "anthropic not installed. Run: pip install anthropic"
            ) from e

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Add it to backend/.env "
                "(get a key at https://console.anthropic.com)."
            )
        self._client = Anthropic()  # reads ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL from env
        self._model = cfg.llm.model
        self._max_tokens = cfg.llm.max_tokens
        _log.info("Anthropic (direct API) backend ready (model=%s)", self._model)


class BedrockBackend(_ClientBackend):
    """Claude via Amazon Bedrock (anthropic[bedrock] + boto3).

    Credentials come from the standard AWS chain (env vars / profile / SSO / role);
    supports AWS_SESSION_TOKEN. Model ids are Bedrock ids/inference-profile ARNs
    (e.g. `anthropic.claude-sonnet-4-20250514-v1:0` or a cross-region profile id).
    """

    def __init__(self, cfg: SiriusConfig) -> None:
        try:
            from anthropic import AnthropicBedrock
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "anthropic[bedrock] not installed. Run: pip install 'anthropic[bedrock]'"
            ) from e

        region = (
            cfg.llm.bedrock_region
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        # AnthropicBedrock reads the AWS credential chain via boto3 (env/profile/role).
        self._client = AnthropicBedrock(aws_region=region)
        self._model = cfg.llm.model
        self._max_tokens = cfg.llm.max_tokens
        _log.info("Bedrock backend ready (region=%s model=%s)", region, self._model)


class EchoBackend(LLMBackend):
    """No-network fallback used when Vertex isn't configured — keeps the UI/event
    flow demoable without credentials. Never calls tools."""

    _model = "echo"

    def create(self, messages, system=None, tools=None, max_tokens=4096, model=None):  # noqa: D401
        from types import SimpleNamespace

        last = ""
        for m in reversed(messages):
            if m["role"] == "user":
                content = m["content"]
                last = content if isinstance(content, str) else str(content)
                break
        text = (
            "⚠️ Vertex AI is not configured, so this is the Echo backend. "
            "Set up GCP ADC + llm.vertex_project to use Claude.\n\n"
            f"You said: {last}"
        )
        block = SimpleNamespace(type="text", text=text)
        return SimpleNamespace(content=[block], stop_reason="end_turn", role="assistant")

    def stream(self, messages, system=None, tools=None, max_tokens=4096, model=None, on_text=None):
        msg = self.create(messages, system, tools, max_tokens, model)
        if on_text:
            text = msg.content[0].text
            for i in range(0, len(text), 24):  # chunk so the effect is visible w/o Vertex
                on_text(text[i:i + 24])
        return msg


_BUILDERS: dict[str, type[LLMBackend]] = {
    "vertex": VertexBackend,
    "direct": AnthropicBackend,
    "bedrock": BedrockBackend,
}


def get_backend(cfg: SiriusConfig) -> LLMBackend:
    """Build the configured LLM backend, falling back to Echo if it can't init.

    `llm.backend` selects the provider: vertex | direct | bedrock | echo.
    A real backend that fails to initialize (missing creds/SDK) degrades to Echo
    so the UI/event flow stays demoable — except an unknown name, which is a
    config error and raises.
    """
    backend = (cfg.llm.backend or "vertex").lower()
    if backend == "echo":
        return EchoBackend()
    builder = _BUILDERS.get(backend)
    if builder is None:
        raise ValueError(
            f"Unsupported llm.backend: {cfg.llm.backend!r} "
            "(expected vertex | direct | bedrock | echo)"
        )
    try:
        return builder(cfg)
    except Exception as e:  # noqa: BLE001 - degrade gracefully for demos
        _log.warning("%s backend unavailable (%s) — falling back to Echo backend.",
                     backend, e)
        return EchoBackend()
