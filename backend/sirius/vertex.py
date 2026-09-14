"""
Web-UI-driven GCP Vertex project switching for Sirius.

Sirius runs Claude on Vertex AI against a sandbox GCP project that rotates
roughly every ~5 weeks. A stale project surfaces as
`403 PERMISSION_DENIED / CONSUMER_INVALID`. Rather than editing .env and
restarting the server on every rotation, this module lets the operator switch
the active Vertex project from the Settings modal:

  • status()        — resolved project/region + where the project came from +
    whether ADC credentials are currently obtainable.
  • list_projects() — best-effort `gcloud projects list` to populate the picker.
  • set_project()   — persist SIRIUS_VERTEX_PROJECT/REGION to .env + the running
    process env. web.py then calls rebuild_llm() so the switch takes effect with
    NO restart (VertexBackend resolves project_id via cfg.vertex_project(), which
    reads os.environ first — see config.py).

Credential rules (same as aws_sso / settings): the project id and region are
NON-SECRET and safe to persist to .env. GCP credentials themselves come from
Application Default Credentials (`gcloud auth application-default login`) — Sirius
never reads, writes, echoes, or emits the token; it only reports whether one can
be minted. Switching the project does NOT re-authenticate: the same ADC identity
is reused, so this works as long as that identity has Vertex access on the target
project. If it doesn't, run `gcloud auth application-default login` for the new
project (a local browser flow) and the next call will pick it up.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Optional

from sirius import settings as settings_mod
from sirius.config import load_config
from sirius.events import emit

_log = logging.getLogger("sirius.vertex")


def _gcloud() -> Optional[str]:
    return shutil.which("gcloud")


def _adc_present() -> bool:
    """True if Application Default Credentials can currently mint an access token.

    This confirms `gcloud auth application-default login` (or a service-account
    ADC file) is in place. It does NOT prove the resolved project is valid for
    Vertex — a stale project still only fails on the first messages.create call.
    """
    gcloud = _gcloud()
    if not gcloud:
        return False
    try:
        out = subprocess.run(
            [gcloud, "auth", "application-default", "print-access-token"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return out.returncode == 0 and bool((out.stdout or "").strip())


def _project_source() -> str:
    """Which resolution slot supplied the active project (matches config.py order)."""
    if os.environ.get("SIRIUS_VERTEX_PROJECT"):
        return "SIRIUS_VERTEX_PROJECT (.env)"
    if load_config().llm.vertex_project:
        return "config.yaml"
    if os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
        return "ANTHROPIC_VERTEX_PROJECT_ID"
    if os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return "GOOGLE_CLOUD_PROJECT"
    return "unset"


def status() -> dict[str, Any]:
    """Resolved Vertex project/region + ADC presence. Never returns a token."""
    cfg = load_config()
    return {
        "project": cfg.vertex_project(),
        "region": cfg.vertex_region(),
        "source": _project_source(),
        "adc": _adc_present(),
        "gcloud": _gcloud() is not None,
    }


def list_projects() -> dict[str, Any]:
    """Best-effort `gcloud projects list` for the UI picker (ACTIVE projects only).

    Requires the resourcemanager list permission; a Vertex-only identity may not
    have it, in which case the UI falls back to a free-text project field.
    """
    gcloud = _gcloud()
    if not gcloud:
        return {"ok": False, "error": "gcloud CLI not installed", "projects": []}
    try:
        out = subprocess.run(
            [gcloud, "projects", "list", "--format=json"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "gcloud projects list timed out", "projects": []}
    except OSError as e:  # pragma: no cover - defensive
        return {"ok": False, "error": str(e), "projects": []}
    if out.returncode != 0:
        return {"ok": False, "error": (out.stderr or "").strip()[:300], "projects": []}
    try:
        raw = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return {"ok": False, "error": "could not parse gcloud output", "projects": []}
    projects = [
        {"project_id": p.get("projectId"), "name": p.get("name")}
        for p in raw
        if p.get("projectId") and p.get("lifecycleState", "ACTIVE") == "ACTIVE"
    ]
    projects.sort(key=lambda p: p["project_id"])
    return {"ok": True, "projects": projects}


def set_project(project: str, region: str = "", session_id: Optional[str] = None) -> dict[str, Any]:
    """Persist the Vertex project (and optional region) to .env + the live env.

    Does NOT rebuild the LLM backend — web.py owns `_llm` and calls rebuild_llm()
    after this returns, so the switch takes effect with no restart.
    """
    project = (project or "").strip()
    if not project:
        return {"ok": False, "error": "project id is required"}

    settings_mod._write_env("SIRIUS_VERTEX_PROJECT", project)
    os.environ["SIRIUS_VERTEX_PROJECT"] = project

    region = (region or "").strip()
    if region:
        settings_mod._write_env("SIRIUS_VERTEX_REGION", region)
        os.environ["SIRIUS_VERTEX_REGION"] = region

    emit(
        "status",
        f"Vertex project switched to {project}" + (f" · {region}" if region else ""),
        severity="success",
        session_id=session_id,
        payload={"project": project, "region": region or None},
    )
    _log.info("Vertex project set to %s (region=%s)", project, region or "unchanged")
    return {"ok": True, "project": project, "region": region or None}
