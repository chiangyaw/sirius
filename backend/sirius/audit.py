"""
Audit trail — append-only prompt/response history (Feature: History modal).

Every agent turn (prompt + reply, plus metadata) is appended as one JSON object
per line to an audit log. This is the durable record the UI's History modal reads,
independent of any browser session — it survives reloads, restarts, and different
browsers.

Storage: a JSONL file at the backend root (audit_log.jsonl), overridable with the
SIRIUS_AUDIT_PATH env var. Writes are guarded by a lock so concurrent turns don't
interleave lines. Nothing here logs secret values — callers pass only the prompt,
the reply, and demo metadata (scenario / AIRS state).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger("sirius.audit")
_lock = threading.Lock()


def _audit_path() -> Path:
    """The JSONL audit file (backend root), overridable for tests/deployments."""
    override = os.environ.get("SIRIUS_AUDIT_PATH")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "audit_log.jsonl"


def record(
    *,
    session_id: str,
    agent: str,
    prompt: str,
    reply: str,
    mode: Optional[str] = None,
    scenario: Optional[str] = None,
    airs_enabled: bool = False,
    status: str = "ok",
    error: Optional[str] = None,
) -> dict[str, Any]:
    """Append one turn to the audit log and return the stored entry."""
    entry = {
        "id": uuid.uuid4().hex,
        "ts": int(time.time() * 1000),
        "session_id": session_id,
        "agent": agent,
        "mode": mode,
        "scenario": scenario,
        "airs_enabled": airs_enabled,
        "status": status,
        "prompt": prompt,
        "reply": reply,
        "error": error,
    }
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock:
            with _audit_path().open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as e:  # never let audit failure break a chat turn
        _log.warning("audit write failed: %s", e)
    return entry


def _read_raw() -> list[dict[str, Any]]:
    """All entries in file order (chronological, append-only). Skips corrupt lines."""
    path = _audit_path()
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with _lock:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue  # skip a corrupt line rather than fail the whole read
    return entries


def read_all(limit: Optional[int] = None, session_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Return audit entries, newest first. Optionally filter to one session and cap count."""
    entries = _read_raw()
    if session_id is not None:
        entries = [e for e in entries if e.get("session_id") == session_id]
    entries.reverse()  # newest first
    if limit is not None:
        return entries[:limit]
    return entries


def read_session(session_id: str) -> list[dict[str, Any]]:
    """All entries for one session, chronological (oldest first)."""
    entries = [e for e in _read_raw() if e.get("session_id") == session_id]
    entries.sort(key=lambda e: e.get("ts", 0))
    return entries


def _infer_mode(entry: dict[str, Any]) -> str:
    """Mode for a turn — stored value, else inferred from legacy entries."""
    return entry.get("mode") or ("airs" if entry.get("scenario") else "general")


def list_conversations() -> list[dict[str, Any]]:
    """Group audit entries by session into conversation summaries, newest first."""
    convos: dict[str, dict[str, Any]] = {}
    for e in _read_raw():  # file order = chronological (append-only)
        sid = e.get("session_id")
        if not sid:
            continue
        ts = e.get("ts", 0)
        c = convos.get(sid)
        if c is None:
            title = (e.get("prompt") or "").strip().replace("\n", " ")
            if len(title) > 60:
                title = title[:60].rstrip() + "…"
            convos[sid] = {
                "session_id": sid,
                "title": title or "(empty prompt)",
                "mode": _infer_mode(e),
                "scenario": e.get("scenario"),
                "agent": e.get("agent"),
                "airs_enabled": bool(e.get("airs_enabled")),
                "count": 1,
                "created_ts": ts,
                "updated_ts": ts,
            }
        else:
            c["count"] += 1
            c["updated_ts"] = max(c["updated_ts"], ts)
            c["created_ts"] = min(c["created_ts"], ts)
            # Mode/scenario/airs reflect the latest turn of the conversation.
            c["mode"] = _infer_mode(e)
            c["scenario"] = e.get("scenario")
            c["airs_enabled"] = bool(e.get("airs_enabled"))
            c["agent"] = e.get("agent")
    return sorted(convos.values(), key=lambda c: c["updated_ts"], reverse=True)


def delete_session(session_id: str) -> int:
    """Remove all turns for one session. Returns the number of entries removed."""
    path = _audit_path()
    with _lock:
        if not path.exists():
            return 0
        kept: list[str] = []
        removed = 0
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    if json.loads(stripped).get("session_id") == session_id:
                        removed += 1
                        continue
                except json.JSONDecodeError:
                    pass  # keep unparseable lines rather than silently drop them
                kept.append(stripped)
        try:
            if kept:
                path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            else:
                path.unlink()
        except OSError as e:
            _log.warning("audit delete_session failed: %s", e)
            return 0
    return removed


def clear() -> int:
    """Delete all audit history. Returns the number of entries removed."""
    path = _audit_path()
    with _lock:
        if not path.exists():
            return 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                count = sum(1 for line in fh if line.strip())
        except OSError:
            count = 0
        try:
            path.unlink()
        except OSError as e:
            _log.warning("audit clear failed: %s", e)
            return 0
    return count
