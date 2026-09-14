"""
Web-fetch skill — a curl-equivalent that lets agents pull URLs (raw manifests,
API responses, docs) into a turn, with every request shown in the live event
stream.

Deliberately a Python/httpx skill rather than allowlisting the `curl` binary in
the k8s runner: it stays in-process, emits events like the other skills, follows
redirects, and is restricted to http(s) — no arbitrary executable spawn.

NOTE: `kubectl apply -f <URL>` does NOT need this skill — kubectl fetches URLs
itself, so the existing k8s_kubectl skill already handles remote manifests. Use
web_fetch when you actually need the file's *contents* in the conversation (to
inspect, transform, or save it before applying).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from sirius.events import emit
from sirius.skills import registry

# Body cap fed back to the model. The agent loop caps tool results again at
# ~24k chars; this keeps us from buffering a huge download into memory first.
_MAX_BODY_CHARS = 100_000
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def web_fetch(
    url: str,
    method: str = "GET",
    headers: Optional[dict] = None,
    data: str = "",
    timeout: int = 30,
    insecure: bool = False,
    max_bytes: int = _MAX_BODY_CHARS,
    **_: Any,
) -> dict:
    """Fetch a URL over http(s) and return status + headers + (capped) body.

    Read verbs (GET/HEAD/OPTIONS) are logged at info; anything that can mutate
    remote state (POST/PUT/PATCH/DELETE) is logged at warn so it stands out in
    the event stream. Set insecure=True (like `curl -k`) to skip TLS verification
    — needed only behind a TLS-intercepting proxy; avoid it otherwise.
    """
    verb = (method or "GET").upper()
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "error": f"unsupported URL scheme {parsed.scheme!r}; "
                "only http/https are allowed", "url": url}

    mutating = verb not in _READ_METHODS
    emit("skill_invoked", f"$ curl {'-k ' if insecure else ''}-X {verb} {url}",
         severity="warn" if (mutating or insecure) else "info",
         payload={"url": url, "method": verb, "insecure": insecure})

    try:
        with httpx.Client(follow_redirects=True, timeout=timeout,
                          verify=not insecure) as client:
            resp = client.request(verb, url, headers=headers or None,
                                  content=data or None)
    except httpx.HTTPError as e:
        emit("status", f"web_fetch failed: {e}", severity="danger",
             payload={"url": url, "error": str(e)})
        return {"ok": False, "error": str(e), "url": url}

    body = resp.text or ""
    truncated = len(body) > max_bytes
    if truncated:
        body = body[:max_bytes] + f"\n…[truncated {len(resp.text) - max_bytes:,} chars]"

    ok = resp.is_success
    emit("status", f"{verb} {url} → {resp.status_code} ({len(resp.text):,} bytes)",
         severity="success" if ok else "danger",
         payload={"url": str(resp.url), "status_code": resp.status_code})

    return {
        "ok": ok,
        "status_code": resp.status_code,
        "url": str(resp.url),  # final URL after redirects
        "content_type": resp.headers.get("content-type", ""),
        "truncated": truncated,
        "body": body,
    }


# ── Registration ─────────────────────────────────────────────────────────────
registry.skill(
    "web_fetch",
    "Fetch a URL over http(s) (a curl equivalent) and return status, headers, and "
    "body — e.g. to pull a raw manifest, API response, or docs into the turn. "
    "Follows redirects. Note: `kubectl apply -f <URL>` already fetches URLs on its "
    "own via k8s_kubectl; use web_fetch when you need the file contents themselves.",
    {"type": "object",
     "properties": {
         "url": {"type": "string", "description": "http(s) URL to fetch"},
         "method": {"type": "string",
                    "description": "HTTP method (GET/HEAD/POST/…); default GET"},
         "headers": {"type": "object",
                     "description": "optional request headers"},
         "data": {"type": "string",
                  "description": "optional request body for POST/PUT/PATCH"},
         "insecure": {"type": "boolean",
                      "description": "skip TLS verification (like curl -k); only "
                                     "for TLS-intercepting proxies"}},
     "required": ["url"]},
)(web_fetch)
