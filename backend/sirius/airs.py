"""
Prisma AIRS (AI Runtime Security) scanner for Sirius.

Ported from Alfred's AIRSGuardedBackend (alfred/llm.py) and extended to emit
`airs_scan` events into the event stream so the demo can *see* each verdict.

Design differs from Alfred: instead of wrapping the LLM backend, the agent loop
calls this scanner explicitly — `scan("prompt", ...)` before the model and
`scan("response", ...)` after — which keeps native tool-use loops clean and lets
the UI toggle AIRS on/off per request.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from sirius.config import AIRSConfig
from sirius.events import emit

_log = logging.getLogger("sirius.airs")


class PrismaAIRSBlocked(Exception):
    """Raised when Prisma AIRS returns action='block' for a prompt or response."""

    def __init__(self, direction: str, reason: str) -> None:
        self.direction = direction
        self.reason = reason
        super().__init__(f"The {direction} was blocked by Prisma AIRS due to {reason}")


class PrismaAIRS:
    """Thin wrapper over the pan-aisecurity inline scanner.

    Token is read from PANW_AI_SEC_API_KEY. If the SDK or token is missing while
    AIRS is enabled, scanning is disabled (fail-open) with a warning rather than
    crashing — same policy as Alfred.
    """

    def __init__(self, cfg: AIRSConfig) -> None:
        self.cfg = cfg
        self._scanner = None
        self._profile = None
        self._content_cls = None

        api_key = os.environ.get("PANW_AI_SEC_API_KEY")
        if not api_key:
            _log.warning(
                "AIRS enabled but PANW_AI_SEC_API_KEY not set — scanning disabled (fail-open)."
            )
            return
        try:
            import aisecurity
            from aisecurity.generated_openapi_client.models.ai_profile import AiProfile
            from aisecurity.scan.inline.scanner import Scanner
            from aisecurity.scan.models.content import Content

            aisecurity.init(api_key=api_key, api_endpoint=cfg.api_endpoint)
            self._scanner = Scanner()
            self._profile = AiProfile(profile_name=cfg.profile_name)
            self._content_cls = Content
            _log.info("Prisma AIRS ready (profile=%s endpoint=%s)",
                      cfg.profile_name, cfg.api_endpoint)
        except Exception as e:  # noqa: BLE001
            if not cfg.fail_open:
                raise
            _log.warning("AIRS init failed (%s) — scanning disabled (fail-open).", e)
            self._scanner = None

    @property
    def available(self) -> bool:
        return self._scanner is not None

    def scan(
        self,
        direction: str,
        prompt: Optional[str] = None,
        response: Optional[str] = None,
    ) -> None:
        """Scan content, emit an `airs_scan` event, raise PrismaAIRSBlocked on block."""
        if self._scanner is None:
            emit(
                "airs_scan",
                f"AIRS {direction} scan skipped (not configured)",
                severity="warn",
                payload={"direction": direction, "status": "skipped"},
            )
            return

        started = time.time()
        try:
            content = self._content_cls(prompt=prompt, response=response)
            result = self._scanner.sync_scan(ai_profile=self._profile, content=content)
        except Exception as e:  # noqa: BLE001
            if not self.cfg.fail_open:
                raise
            emit(
                "airs_scan",
                f"AIRS {direction} scan error (allowed, fail-open)",
                severity="warn",
                payload={"direction": direction, "error": str(e)},
            )
            _log.warning("AIRS %s scan failed (%s) — fail-open.", direction, e)
            return

        latency_ms = int((time.time() - started) * 1000)
        action = str(getattr(result, "action", "") or "").lower()
        detectors = _detectors(result)
        category = getattr(result, "category", None)
        blocked = action == "block"

        emit(
            "airs_scan",
            f"Prisma AIRS: {direction} {'BLOCKED' if blocked else 'allowed'}",
            severity="danger" if blocked else "success",
            payload={
                "direction": direction,
                "action": action or "allow",
                "category": str(category) if category else None,
                "detectors": detectors,
                "latency_ms": latency_ms,
                "profile": self.cfg.profile_name,
            },
        )
        if blocked:
            raise PrismaAIRSBlocked(direction, ", ".join(detectors) or str(category) or "policy violation")


def _detectors(result) -> list[str]:
    """Extract which detectors fired (injection, dlp, toxic_content, …)."""
    fired: list[str] = []
    for attr in ("prompt_detected", "response_detected"):
        detected = getattr(result, attr, None)
        if not detected:
            continue
        if isinstance(detected, dict):
            items = detected.items()
        elif hasattr(detected, "to_dict"):
            items = detected.to_dict().items()
        else:
            items = vars(detected).items()
        for name, hit in items:
            if hit:
                fired.append(str(name).replace("_", " "))
    return list(dict.fromkeys(fired))  # de-dupe, keep order
