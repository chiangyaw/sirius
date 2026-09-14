"""
Agent loop — native Anthropic tool-use, with every step emitted as an event.

An `Agent` is a persona (system prompt) + a subset of the skill registry. A turn:
  1. emit user_prompt + agent_start
  2. (if AIRS on) scan the user prompt — a block ends the turn immediately
  3. loop: call Claude; on tool_use, dispatch skills and feed results back
  4. (if AIRS on) scan the final response
  5. emit agent_end and return the text

Blocking work (LLM create, AIRS scan, skill handlers) runs in an executor with
the current contextvars copied in, so events emitted from worker threads still
carry the right session/agent/turn.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from sirius.airs import PrismaAIRS, PrismaAIRSBlocked
from sirius.events import current_agent, current_session, current_turn, emit
from sirius.llm import LLMBackend
from sirius.skills import registry

_log = logging.getLogger("sirius.agent")


# ── Turn cancellation ────────────────────────────────────────────────────────
# A running turn can be interrupted (the UI's Esc / Stop button hits
# /api/chat/cancel). We can't kill a blocking LLM/tool call mid-flight, so the
# loop checks these flags at step boundaries and after each tool call, then
# stops cleanly. Keyed by session_id — each UI window runs one turn at a time.
_cancel_requested: set[str] = set()   # cancel asked for, not yet acted on
_cancelled_turns: set[str] = set()    # last turn for this session actually stopped

STOPPED_NOTE = "⏹ Stopped."


def request_cancel(session_id: str) -> None:
    """Ask the in-flight turn for this session to stop at its next checkpoint."""
    _cancel_requested.add(session_id)


def consume_cancelled(session_id: str) -> bool:
    """True (once) if the session's last turn ended because it was cancelled."""
    was = session_id in _cancelled_turns
    _cancelled_turns.discard(session_id)
    return was


async def run_sync(func, *args, **kwargs):
    """Run a blocking call in a thread with the current context copied in."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(None, lambda: ctx.run(func, *args, **kwargs))


@dataclass
class Agent:
    name: str
    system: str
    skill_names: list[str] = field(default_factory=list)
    model: Optional[str] = None  # overrides the backend default (multi-agent model routing)

    def tools(self) -> list[dict]:
        return registry.tools(self.skill_names)


def _stringify(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, indent=2)
    except TypeError:
        return str(result)


# A single tool result fed back to the model is capped so one huge API payload
# (e.g. a Cortex management_logs / get_policies_list dump) can't blow the context
# window — it gets re-sent on every subsequent step of the turn.
_MAX_TOOL_RESULT_CHARS = 24_000


def _cap(text: str, limit: int = _MAX_TOOL_RESULT_CHARS) -> str:
    """Bound an oversized tool result, keeping head + tail with a marker between."""
    if len(text) <= limit:
        return text
    head = text[: limit * 3 // 4]
    tail = text[-limit // 4 :]
    dropped = len(text) - len(head) - len(tail)
    return (f"{head}\n\n…[{dropped:,} chars truncated to fit the context window; "
            f"re-query with a filter/limit for the rest]…\n\n{tail}")


def _compact_turn(messages: list[dict], start: int, user_message: str, reply: str) -> None:
    """Collapse everything appended this turn (verbose tool_use/tool_result blocks)
    down to a plain user/assistant text pair — the same shape _rehydrate_messages
    rebuilds from the audit log. Keeps the session's carried context small across
    turns and stops one bloated turn from poisoning the next."""
    del messages[start:]
    messages.append({"role": "user", "content": user_message})
    if reply:
        messages.append({"role": "assistant", "content": reply})


def _assistant_content(msg) -> list[dict]:
    """Convert an Anthropic Message's content blocks back into dict form."""
    out: list[dict] = []
    for b in msg.content:
        btype = getattr(b, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": b.text})
        elif btype == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


async def run_turn(
    agent: Agent,
    llm: LLMBackend,
    airs: Optional[PrismaAIRS],
    session_id: str,
    user_message: str,
    messages: list[dict],
    airs_enabled: bool = True,
    max_tokens: int = 4096,
    max_steps: int = 8,
    stream: bool = False,
) -> str:
    """Run one user turn. Appends to `messages` in place. Returns final text."""
    turn_id = uuid.uuid4().hex[:12]
    current_session.set(session_id)
    current_agent.set(agent.name)
    current_turn.set(turn_id)

    # Clear any stale cancellation state from a previous turn/window.
    _cancel_requested.discard(session_id)
    _cancelled_turns.discard(session_id)

    emit("user_prompt", user_message, payload={"text": user_message}, parent_id=None)
    emit("agent_start", f"{agent.name} started",
         payload={"agent": agent.name, "airs_enabled": airs_enabled}, parent_id=turn_id)

    # 1. Scan the incoming prompt.
    if airs_enabled and airs is not None:
        try:
            await run_sync(airs.scan, "prompt", prompt=user_message)
        except PrismaAIRSBlocked as blk:
            text = f"🛡️ Request blocked by Prisma AIRS — detected: {blk.reason}."
            emit("blocked", "Prompt blocked by Prisma AIRS", severity="danger",
                 payload={"reason": blk.reason, "direction": "prompt"}, parent_id=turn_id)
            emit("agent_end", f"{agent.name} finished (blocked)", parent_id=turn_id)
            return text

    hist_start = len(messages)  # where this turn begins, for end-of-turn compaction
    messages.append({"role": "user", "content": user_message})
    tools = agent.tools()
    model = agent.model or llm.model  # per-agent model override (multi-agent routing)

    def _on_text(delta: str) -> None:
        """Stream a text delta to the UI (out-of-band, unbuffered). Runs in the
        executor thread; contextvars are copied in so it tags the right session."""
        emit("llm_delta", "", payload={"text": delta}, parent_id=turn_id, buffer=False)

    def _stopped() -> str:
        """Interrupted at a checkpoint — emit, compact this turn away, return note."""
        _cancel_requested.discard(session_id)
        _cancelled_turns.add(session_id)
        emit("status", "⏹ Turn interrupted by user", severity="warn", parent_id=turn_id)
        emit("agent_end", f"{agent.name} finished (stopped)", parent_id=turn_id)
        _compact_turn(messages, hist_start, user_message, STOPPED_NOTE)
        return STOPPED_NOTE

    final_text = ""
    for step in range(max_steps):
        if session_id in _cancel_requested:
            return _stopped()
        emit("llm_request", f"Calling {model} (step {step + 1})",
             payload={"model": model, "tools": [t["name"] for t in tools]},
             parent_id=turn_id)
        try:
            if stream:
                msg = await run_sync(
                    llm.stream, messages, agent.system, tools or None, max_tokens, model, _on_text
                )
            else:
                msg = await run_sync(
                    llm.create, messages, agent.system, tools or None, max_tokens, model
                )
        except Exception as e:  # noqa: BLE001
            emit("error", f"LLM error: {e}", severity="danger",
                 payload={"error": str(e)}, parent_id=turn_id)
            # Drop this turn's partial history so a failure (e.g. context-too-long)
            # can't poison every subsequent turn on the same session.
            del messages[hist_start:]
            return f"⚠️ LLM error: {e}"

        text_parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
        step_text = "\n".join(text_parts).strip()
        if step_text:
            emit("llm_response", step_text, payload={"text": step_text}, parent_id=turn_id)

        messages.append({"role": "assistant", "content": _assistant_content(msg)})

        stop_reason = getattr(msg, "stop_reason", None)
        tool_uses = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
        _log.info("agent step stop_reason=%s tool_uses=%d text_len=%d",
                  stop_reason, len(tool_uses), len(step_text))
        if not tool_uses or stop_reason != "tool_use":
            final_text = step_text
            # A response cut off at the max_tokens ceiling leaves a truncated,
            # unexecutable tool call — the tool NEVER ran, so nothing was produced.
            # Any preamble text ("I'll compile that now…") is misleading on its own,
            # so always surface the truncation instead of silently dropping the call.
            if stop_reason == "max_tokens":
                emit("error", "Output truncated at the max_tokens limit — action did not complete",
                     severity="danger", payload={"stop_reason": stop_reason},
                     parent_id=turn_id)
                note = (
                    "⚠️ I hit the output length limit while composing that "
                    "(often a large report or tool call), so it did **not** complete "
                    "and nothing was saved. Try asking for a more concise version, "
                    "fewer/shorter findings, or raise `llm.max_tokens`."
                )
                final_text = f"{final_text}\n\n{note}" if final_text else note
            break

        # 3. Dispatch tool calls. Bail before starting if a stop arrived during
        # the LLM call, so we don't kick off tool work the user cancelled.
        if session_id in _cancel_requested:
            return _stopped()
        tool_results: list[dict] = []
        for tu in tool_uses:
            emit("tool_call", f"→ {tu.name}", severity="info",
                 payload={"tool": tu.name, "input": tu.input}, parent_id=turn_id)
            skill = registry.get(tu.name)
            if tu.name not in agent.skill_names:
                # Isolation guard: an agent may only call tools in its own persona's
                # skill set, even if the tool exists in the shared global registry.
                result = f"error: skill '{tu.name}' is not available to agent '{agent.name}'"
                sev = "danger"
            elif skill is None:
                result = f"error: unknown skill '{tu.name}'"
                sev = "danger"
            else:
                try:
                    result = await run_sync(skill.run, **(tu.input or {}))
                    sev = "success"
                except Exception as e:  # noqa: BLE001
                    result = f"error: {e}"
                    sev = "danger"
            result_str = _stringify(result)
            emit("tool_result", f"← {tu.name}", severity=sev,
                 payload={"tool": tu.name, "result": result_str[:4000]}, parent_id=turn_id)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": _cap(result_str),
            })
            # A stop can land while a long tool ran — bail after it finishes
            # rather than looping back into another LLM/tool round.
            if session_id in _cancel_requested:
                return _stopped()
        messages.append({"role": "user", "content": tool_results})
    else:
        # Step budget exhausted with the last step still mid-tool-call, so we never
        # got a closing message. Rather than drop everything and return "(no
        # response)", make one final tool-FREE call to force a wrap-up of the work
        # already done (the tool results are all in `messages`).
        emit("status", "Reached max reasoning steps — summarizing work so far",
             severity="warn", parent_id=turn_id)
        if not final_text:
            wrap_prompt = (
                "You've reached the step limit for this turn. Do not call any more "
                "tools. Summarize for the user what you accomplished and what (if "
                "anything) is left to finish."
            )
            messages.append({"role": "user", "content": wrap_prompt})
            try:
                if stream:
                    msg = await run_sync(
                        llm.stream, messages, agent.system, None, max_tokens, model, _on_text
                    )
                else:
                    msg = await run_sync(
                        llm.create, messages, agent.system, None, max_tokens, model
                    )
                final_text = "\n".join(
                    b.text for b in msg.content if getattr(b, "type", None) == "text"
                ).strip()
                if final_text:
                    emit("llm_response", final_text,
                         payload={"text": final_text}, parent_id=turn_id)
                messages.append({"role": "assistant", "content": _assistant_content(msg)})
            except Exception as e:  # noqa: BLE001
                emit("error", f"LLM error during wrap-up: {e}", severity="danger",
                     payload={"error": str(e)}, parent_id=turn_id)

    # 4. Scan the final response.
    if final_text and airs_enabled and airs is not None:
        try:
            await run_sync(airs.scan, "response", prompt=user_message, response=final_text)
        except PrismaAIRSBlocked as blk:
            blocked = f"🛡️ Response blocked by Prisma AIRS — detected: {blk.reason}."
            emit("blocked", "Response blocked by Prisma AIRS", severity="danger",
                 payload={"reason": blk.reason, "direction": "response"}, parent_id=turn_id)
            emit("agent_end", f"{agent.name} finished (response blocked)", parent_id=turn_id)
            _compact_turn(messages, hist_start, user_message, blocked)
            return blocked

    reply = final_text or "(no response)"
    _compact_turn(messages, hist_start, user_message, reply)
    emit("agent_end", f"{agent.name} finished", severity="success", parent_id=turn_id)
    return reply
