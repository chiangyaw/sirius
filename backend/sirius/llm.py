"""
LLM abstraction for Sirius — Claude on GCP Vertex AI.

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


class VertexBackend(LLMBackend):
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


def get_backend(cfg: SiriusConfig) -> LLMBackend:
    """Build the configured LLM backend, falling back to Echo if Vertex can't init."""
    if cfg.llm.backend != "vertex":
        raise ValueError(f"Unsupported llm.backend: {cfg.llm.backend!r} (only 'vertex')")
    try:
        return VertexBackend(cfg)
    except Exception as e:  # noqa: BLE001 - degrade gracefully for demos
        _log.warning("Vertex backend unavailable (%s) — falling back to Echo backend.", e)
        return EchoBackend()
