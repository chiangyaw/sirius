"""
Event model + async pub/sub bus — the demo centerpiece.

Every meaningful step (agent turn, LLM call, AIRS scan, tool call, terraform
line, scan finding) is emitted as a typed `Event` and streamed to the frontend
over a WebSocket. This is what makes "how it works" visible during a demo.

The bus is thread-safe: skills and LLM/AIRS calls may run in worker threads
(via run_in_executor) but can still `emit()` events that reach the asyncio loop
serving the WebSocket. A per-session history buffer lets a late-connecting UI
replay what already happened.
"""

from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from contextvars import ContextVar
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

EventType = Literal[
    "user_prompt",
    "agent_start",
    "agent_thought",
    "llm_request",
    "llm_response",
    "llm_delta",
    "airs_scan",
    "tool_call",
    "tool_result",
    "skill_invoked",
    "terraform_step",
    "engineer_step",
    "browser_step",
    "aws_sso",
    "aws_step",
    "azure_login",
    "azure_step",
    "scan_finding",
    "blocked",
    "confirm_required",
    "error",
    "agent_end",
    "status",
]

Severity = Literal["info", "success", "warn", "danger"]

_seq = itertools.count(1)


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    seq: int = Field(default_factory=lambda: next(_seq))
    ts: float = Field(default_factory=time.time)
    session_id: str
    agent: str = "sirius"
    type: EventType
    title: str
    severity: Severity = "info"
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_id: Optional[str] = None


# The session a piece of code is currently serving. Skills read this so they
# don't need the session id threaded through every call.
current_session: ContextVar[Optional[str]] = ContextVar("current_session", default=None)
current_agent: ContextVar[str] = ContextVar("current_agent", default="sirius")
current_turn: ContextVar[Optional[str]] = ContextVar("current_turn", default=None)


class EventBus:
    """In-process pub/sub keyed by session id, safe to emit into from any thread."""

    def __init__(self, history_limit: int = 500) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._history: dict[str, list[Event]] = {}
        self._history_limit = history_limit
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def subscribe(self, session_id: str, replay: bool = True) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(session_id, set()).add(q)
        if replay:
            for ev in self._history.get(session_id, []):
                q.put_nowait(ev)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(session_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subscribers.pop(session_id, None)

    def emit(self, event: Event, buffer: bool = True) -> Event:
        """Record + fan out an event. Callable from any thread.

        buffer=False skips the replay history (used for high-frequency llm_delta
        streaming so it can't evict real events from the 500-cap buffer or replay
        stale partial text to a late-connecting UI) — it is still fanned out live.
        """
        if buffer:
            hist = self._history.setdefault(event.session_id, [])
            hist.append(event)
            if len(hist) > self._history_limit:
                del hist[: len(hist) - self._history_limit]

        subs = list(self._subscribers.get(event.session_id, ()))
        if not subs:
            return event

        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._deliver, event, subs)
        else:  # no loop yet — history buffer still captured it
            self._deliver(event, subs)
        return event

    @staticmethod
    def _deliver(event: Event, subs: list[asyncio.Queue]) -> None:
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - unbounded queues here
                pass

    def clear(self, session_id: str) -> None:
        self._history.pop(session_id, None)


# Module-level singleton used across the app.
bus = EventBus()


def emit(
    type: EventType,
    title: str,
    *,
    severity: Severity = "info",
    payload: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
    agent: Optional[str] = None,
    parent_id: Optional[str] = None,
    buffer: bool = True,
) -> Event:
    """Convenience emitter that fills session/agent/turn from the context vars."""
    sid = session_id or current_session.get()
    if not sid:
        # No active session (e.g. a background job with no UI) — drop silently.
        return Event(session_id="_orphan", type=type, title=title, severity=severity)
    ev = Event(
        session_id=sid,
        agent=agent or current_agent.get(),
        type=type,
        title=title,
        severity=severity,
        payload=payload or {},
        parent_id=parent_id if parent_id is not None else current_turn.get(),
    )
    return bus.emit(ev, buffer=buffer)
