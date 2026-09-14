"""
Headless-browser skills (Playwright) — let agents drive a real Chromium browser
to navigate, click, type, snapshot, and screenshot web apps as part of demos and
recon. Every action is shown in the live event stream as a `browser_step` event.

Design notes
------------
* **Persistent per-session browser.** Each Sirius session (see `current_session`
  in events.py) gets its own browser context + page, so an agent can navigate →
  log in → click through a multi-step flow across separate tool calls.

* **Single-thread browser worker.** Playwright's *sync* API is thread-bound: an
  object created on one thread cannot be driven from another. The agent loop runs
  each skill handler in a thread-pool executor (`agent.run_sync`), so every
  Playwright call is funnelled through ONE dedicated worker thread owned by the
  module-level `BrowserManager`. Handlers submit pure browser work to that thread
  and block for the result; all `emit(...)` calls stay on the handler's thread
  (which has the session/agent/turn contextvars) so events route correctly.

* **Dual-use gate.** `browser.enabled` is a master switch and `browser.allowed_hosts`
  is a per-navigation allow-list (empty = any host). A blocked action returns a
  refusal dict (never raises) — same convention as the purple-team skills.

Playwright is imported lazily inside the worker so the module still imports (and
its skills still register, then refuse cleanly) if the browser isn't installed.
"""

from __future__ import annotations

import atexit
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from sirius.config import load_config
from sirius.events import current_session, emit
from sirius.reports import _reports_dir
from sirius.skills import registry

# Visible-text excerpt returned to the model. The agent loop caps tool results
# again at ~24k chars; this keeps page dumps small and useful.
_MAX_TEXT = 4_000
_DEFAULT_TIMEOUT_MS = 30_000


class BrowserManager:
    """Owns the single Playwright worker thread and per-session browser state."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sirius-browser")
        self._pw = None          # Playwright instance (created on the worker thread)
        self._browser = None     # Chromium Browser (created on the worker thread)
        self._sessions: "OrderedDict[str, dict]" = OrderedDict()  # sid -> {context, page}

    # -- these run ON the worker thread --------------------------------------
    def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright  # lazy: optional at import time

        cfg = load_config().browser
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=cfg.headless)

    def _ensure_page(self, session_id: str):
        """Return this session's page, creating the context/page if needed."""
        self._ensure_browser()
        sess = self._sessions.get(session_id)
        if sess is None:
            cfg = load_config().browser
            # Evict least-recently-used sessions over the cap.
            while len(self._sessions) >= max(1, cfg.max_sessions):
                _, old = self._sessions.popitem(last=False)
                try:
                    old["context"].close()
                except Exception:
                    pass
            context = self._browser.new_context()
            sess = {"context": context, "page": context.new_page()}
            self._sessions[session_id] = sess
        else:
            self._sessions.move_to_end(session_id)
        return sess["page"]

    # -- public API (called from handler threads) ----------------------------
    def submit(self, fn: Callable[[], Any]) -> Any:
        """Run fn() on the browser worker thread and block for its result."""
        return self._pool.submit(fn).result()

    def close_session(self, session_id: str) -> bool:
        def _do() -> bool:
            sess = self._sessions.pop(session_id, None)
            if sess is not None:
                try:
                    sess["context"].close()
                except Exception:
                    pass
            return sess is not None

        return self.submit(_do)

    def shutdown(self) -> None:
        def _do() -> None:
            for sess in list(self._sessions.values()):
                try:
                    sess["context"].close()
                except Exception:
                    pass
            self._sessions.clear()
            for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
                if obj is not None:
                    try:
                        getattr(obj, meth)()
                    except Exception:
                        pass

        try:
            self.submit(_do)
        except Exception:
            pass
        self._pool.shutdown(wait=False)


_MANAGER = BrowserManager()
atexit.register(_MANAGER.shutdown)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _gate(url: Optional[str]) -> Optional[dict]:
    """Config gate. Returns a refusal dict if blocked, else None."""
    cfg = load_config().browser
    if not cfg.enabled:
        emit("blocked", "Browser skill disabled (browser.enabled=false)", severity="danger")
        return {"ok": False, "refused": True, "reason": "browser.enabled is false"}
    if url is not None and cfg.allowed_hosts:
        host = (urlparse(url).hostname or "").lower()
        if host not in [h.lower() for h in cfg.allowed_hosts]:
            emit("blocked", f"Browser navigation refused: host {host!r} not allowed",
                 severity="danger", payload={"url": url, "allowed_hosts": cfg.allowed_hosts})
            return {"ok": False, "refused": True,
                    "reason": f"host {host!r} not in browser.allowed_hosts "
                              f"{cfg.allowed_hosts}"}
    return None


def _visible_text(page) -> str:
    try:
        return (page.inner_text("body") or "").strip()
    except Exception:
        return ""


def _sid() -> str:
    return current_session.get() or "default"


# ── Skills ────────────────────────────────────────────────────────────────────
def browser_navigate(url: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS, **_: Any) -> dict:
    """Open a URL in this session's browser and return title + visible text."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "error": f"unsupported URL scheme {parsed.scheme!r}; "
                "only http/https are allowed", "url": url}
    refusal = _gate(url)
    if refusal:
        return refusal
    sid = _sid()
    emit("browser_step", f"↗ navigate {url}", payload={"url": url})

    def _do() -> dict:
        page = _MANAGER._ensure_page(sid)
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return {"title": page.title(), "url": page.url, "text": _visible_text(page)}

    try:
        r = _MANAGER.submit(_do)
    except Exception as e:
        emit("error", f"navigate failed: {e}", severity="danger", payload={"url": url})
        return {"ok": False, "error": str(e), "url": url}
    emit("browser_step", f"✓ {r['title']} ({r['url']})", severity="success",
         payload={"url": r["url"], "title": r["title"]})
    return {"ok": True, "title": r["title"], "url": r["url"],
            "text_excerpt": r["text"][:_MAX_TEXT]}


def browser_click(selector: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS, **_: Any) -> dict:
    """Click the first element matching a CSS or text= selector."""
    refusal = _gate(None)
    if refusal:
        return refusal
    sid = _sid()
    emit("browser_step", f"click {selector}", payload={"selector": selector})

    def _do() -> dict:
        page = _MANAGER._ensure_page(sid)
        page.click(selector, timeout=timeout_ms)
        return {"title": page.title(), "url": page.url}

    try:
        r = _MANAGER.submit(_do)
    except Exception as e:
        emit("error", f"click failed: {e}", severity="danger", payload={"selector": selector})
        return {"ok": False, "error": str(e), "selector": selector}
    return {"ok": True, **r}


def browser_type(selector: str, text: str, submit: bool = False,
                 timeout_ms: int = _DEFAULT_TIMEOUT_MS, **_: Any) -> dict:
    """Fill an input matched by selector; optionally press Enter to submit."""
    refusal = _gate(None)
    if refusal:
        return refusal
    sid = _sid()
    emit("browser_step", f"type into {selector}"
         + (" + Enter" if submit else ""), payload={"selector": selector, "submit": submit})

    def _do() -> dict:
        page = _MANAGER._ensure_page(sid)
        page.fill(selector, text, timeout=timeout_ms)
        if submit:
            page.press(selector, "Enter")
        return {"title": page.title(), "url": page.url}

    try:
        r = _MANAGER.submit(_do)
    except Exception as e:
        emit("error", f"type failed: {e}", severity="danger", payload={"selector": selector})
        return {"ok": False, "error": str(e), "selector": selector}
    return {"ok": True, **r}


def browser_snapshot(**_: Any) -> dict:
    """Return the current page's title, URL, and visible text (no navigation)."""
    refusal = _gate(None)
    if refusal:
        return refusal
    sid = _sid()

    def _do() -> dict:
        page = _MANAGER._ensure_page(sid)
        return {"title": page.title(), "url": page.url, "text": _visible_text(page)}

    try:
        r = _MANAGER.submit(_do)
    except Exception as e:
        emit("error", f"snapshot failed: {e}", severity="danger")
        return {"ok": False, "error": str(e)}
    emit("browser_step", f"snapshot {r['url']}", payload={"url": r["url"]})
    return {"ok": True, "title": r["title"], "url": r["url"],
            "text_excerpt": r["text"][:_MAX_TEXT]}


def browser_screenshot(full_page: bool = False, **_: Any) -> dict:
    """Save a PNG of the current page under the reports dir; return its download URL."""
    refusal = _gate(None)
    if refusal:
        return refusal
    sid = _sid()

    def _do() -> dict:
        page = _MANAGER._ensure_page(sid)
        name = f"screenshot-{uuid.uuid4().hex[:8]}.png"
        page.screenshot(path=str(_reports_dir() / name), full_page=full_page)
        return {"name": name, "title": page.title(), "url": page.url}

    try:
        r = _MANAGER.submit(_do)
    except Exception as e:
        emit("error", f"screenshot failed: {e}", severity="danger")
        return {"ok": False, "error": str(e)}
    download_url = f"/api/reports/{r['name']}"
    emit("browser_step", f"📸 {r['title']}", severity="success",
         payload={"url": r["url"], "download_url": download_url})
    return {"ok": True, "title": r["title"], "url": r["url"], "download_url": download_url}


def browser_close(**_: Any) -> dict:
    """Close this session's browser context (frees the tab/session state)."""
    sid = _sid()
    try:
        closed = _MANAGER.close_session(sid)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    emit("browser_step", "browser session closed" if closed else "no active browser session",
         payload={"closed": closed})
    return {"ok": True, "closed": closed}


# ── Registration ──────────────────────────────────────────────────────────────
_NAV_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "http(s) URL to open"},
        "timeout_ms": {"type": "integer", "description": "navigation timeout in ms (default 30000)"},
    },
    "required": ["url"],
}
_CLICK_SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string",
                     "description": "CSS selector or Playwright text= selector to click"},
        "timeout_ms": {"type": "integer", "description": "timeout in ms (default 30000)"},
    },
    "required": ["selector"],
}
_TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string", "description": "CSS selector of the input to fill"},
        "text": {"type": "string", "description": "text to type into the field"},
        "submit": {"type": "boolean", "description": "press Enter after typing (default false)"},
        "timeout_ms": {"type": "integer", "description": "timeout in ms (default 30000)"},
    },
    "required": ["selector", "text"],
}
_EMPTY_SCHEMA = {"type": "object", "properties": {}}
_SHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "full_page": {"type": "boolean",
                      "description": "capture the full scrollable page (default false)"},
    },
}

registry.skill(
    "browser_navigate",
    "Open a URL in a real Chromium browser (persistent per-session) and return the "
    "page title, final URL, and visible text. Use this to load a web app before "
    "clicking/typing. Restricted to hosts on browser.allowed_hosts.",
    _NAV_SCHEMA,
)(browser_navigate)

registry.skill(
    "browser_click",
    "Click an element on the current browser page by CSS or text= selector. "
    "Navigate first with browser_navigate.",
    _CLICK_SCHEMA,
)(browser_click)

registry.skill(
    "browser_type",
    "Fill a text input on the current browser page (optionally press Enter to submit). "
    "Useful for logging in or filling forms during a multi-step flow.",
    _TYPE_SCHEMA,
)(browser_type)

registry.skill(
    "browser_snapshot",
    "Return the current browser page's title, URL, and visible text without navigating "
    "— e.g. to read the result after a click or form submit.",
    _EMPTY_SCHEMA,
)(browser_snapshot)

registry.skill(
    "browser_screenshot",
    "Save a PNG screenshot of the current browser page and return a download_url "
    "(served by GET /api/reports/{filename}) so it can be viewed in the UI.",
    _SHOT_SCHEMA,
)(browser_screenshot)

registry.skill(
    "browser_close",
    "Close this session's browser context, discarding its tab and login state.",
    _EMPTY_SCHEMA,
)(browser_close)
