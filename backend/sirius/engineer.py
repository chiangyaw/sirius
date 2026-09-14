"""
Engineer — content-engineering skills (Feature: build & ship Cortex content).

The Engineer agent authors Cortex XSIAM content (playbooks + automation scripts)
as code with the demisto-sdk, deploys it to the live tenant, and can delete it
again. It is the "SOAR content developer" persona on the Sirius platform.

How it talks to Cortex:
  • demisto-sdk (validate / upload) runs from the .venv-demisto interpreter against
    the `cortex-content/` pack repo. Deploy = `demisto-sdk upload`.
  • Live content management the SDK doesn't cover (listing/deleting playbooks &
    scripts on the tenant) goes through cortex-content/tools/demisto_api.py, also
    run in .venv-demisto (the backend venv has no demisto_client).

Both use the STANDARD-auth credential trio demisto-sdk needs — DEMISTO_BASE_URL /
DEMISTO_API_KEY / XSIAM_AUTH_ID, falling back to CORTEX_FQDN / CORTEX_API_KEY /
CORTEX_API_KEY_ID from backend/.env. (The XDR public_api ADVANCED key used by the
native cortex_* skills does NOT work here — see the demisto-sdk-playbook-workflow
memory.) Every step emits an `engineer_step` / `status` event for the UI.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from sirius.events import emit
from sirius.skills import registry

_log = logging.getLogger("sirius.engineer")

# repo root = <root>/backend/sirius/engineer.py -> parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV = _REPO_ROOT / ".venv-demisto"
_SDK = _VENV / "bin" / "demisto-sdk"
_VENV_PY = _VENV / "bin" / "python"
_CONTENT_DIR = _REPO_ROOT / "cortex-content"
_PACK_DIR = _CONTENT_DIR / "Packs" / "SiriusDemo"
_API_TOOL = _CONTENT_DIR / "tools" / "demisto_api.py"


# ── credentials / environment ────────────────────────────────────────────────
def _demisto_env() -> dict[str, str]:
    """Subprocess environment carrying the STANDARD demisto-sdk credential trio.

    Maps DEMISTO_* (preferred) or the CORTEX_* fallback into the DEMISTO_* names
    both demisto-sdk and demisto_client read — exactly like cortex-content/upload.sh.
    """
    env = os.environ.copy()

    def pick(*names: str) -> str:
        for n in names:
            v = env.get(n)
            if v:
                return v.strip().strip('"')
        return ""

    env["DEMISTO_BASE_URL"] = pick("DEMISTO_BASE_URL", "CORTEX_FQDN")
    env["DEMISTO_API_KEY"] = pick("DEMISTO_API_KEY", "CORTEX_API_KEY")
    env["XSIAM_AUTH_ID"] = pick("XSIAM_AUTH_ID", "DEMISTO_API_KEY_ID", "CORTEX_API_KEY_ID")
    env["DEMISTO_SDK_IGNORE_CONTENT_WARNING"] = "1"
    return env


def _creds_ok(env: dict[str, str]) -> tuple[bool, str]:
    missing = [k for k in ("DEMISTO_BASE_URL", "DEMISTO_API_KEY", "XSIAM_AUTH_ID")
               if not env.get(k)]
    if missing:
        return False, ("missing Cortex credentials: " + ", ".join(missing) +
                       " — set them (or the CORTEX_* fallback) in backend/.env")
    return True, ""


# ── subprocess helpers ───────────────────────────────────────────────────────
def _stream(cmd: list[str], phase: str, env: dict[str, str], cwd: Path) -> dict:
    """Run a command, streaming each line as an engineer_step event. Secrets never
    appear on the command line — creds are passed only through the environment."""
    emit("engineer_step", f"$ {' '.join(cmd)}", payload={"phase": phase})
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, cwd=str(cwd), env=env)
    except FileNotFoundError:
        emit("error", f"{cmd[0]} not found", severity="danger")
        return {"ok": False, "error": f"{cmd[0]} not found on PATH"}
    out: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            out.append(line)
            emit("engineer_step", line,
                 severity="danger" if "error" in line.lower() else "info",
                 payload={"phase": phase})
    code = proc.wait()
    emit("engineer_step", f"{phase} exited {code}",
         severity="success" if code == 0 else "danger", payload={"phase": phase})
    return {"ok": code == 0, "exit_code": code, "output": "\n".join(out[-80:])}


def _run_api(subcmd: str, *args: str) -> dict:
    """Invoke the demisto_client bridge in .venv-demisto and parse its JSON reply."""
    env = _demisto_env()
    ok, err = _creds_ok(env)
    if not ok:
        return {"ok": False, "error": err}
    if not _VENV_PY.exists():
        return {"ok": False, "error": f"demisto venv python not found at {_VENV_PY}"}
    cmd = [str(_VENV_PY), str(_API_TOOL), subcmd, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              cwd=str(_CONTENT_DIR), timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{subcmd} timed out"}
    stdout = (proc.stdout or "").strip()
    # The bridge prints exactly one JSON object as its last line.
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return {"ok": False, "error": (proc.stderr or stdout or "no output")[:400]}


# ── content authoring ────────────────────────────────────────────────────────
def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "Untitled"


def _uuid() -> str:
    return str(uuid.uuid4())


def engineer_scaffold_playbook(name: str, description: str = "", **_: Any) -> dict:
    """Generate a minimal, valid Cortex XSIAM playbook (start → title) and write it
    into the SiriusDemo pack. Returns the file path + YAML so the agent can show it,
    extend it, then deploy it. Use this as the starting point for a new playbook."""
    if not name:
        return {"ok": False, "error": "name is required"}
    slug = _slug(name)
    start_id, done_id = _uuid(), _uuid()
    desc = description or f"{name} — generated by the Sirius Engineer agent."
    yaml_text = f"""id: {name}
version: -1
name: {name}
description: {json.dumps(desc)}
starttaskid: "0"
tasks:
  "0":
    id: "0"
    taskid: {start_id}
    type: start
    task:
      id: {start_id}
      version: -1
      name: ""
      iscommand: false
      brand: ""
    nexttasks:
      '#none#':
      - "1"
    separatecontext: false
    continueonerrortype: ""
    view: |-
      {{"position": {{"x": 450, "y": 50}}}}
    note: false
    timertriggers: []
    ignoreworker: false
    skipunavailable: false
    quietmode: 0
    isoversize: false
    isautoswitchedtoquietmode: false
  "1":
    id: "1"
    taskid: {done_id}
    type: title
    task:
      id: {done_id}
      version: -1
      name: Done
      type: title
      iscommand: false
      brand: ""
    separatecontext: false
    continueonerrortype: ""
    view: |-
      {{"position": {{"x": 450, "y": 200}}}}
    note: false
    timertriggers: []
    ignoreworker: false
    skipunavailable: false
    quietmode: 0
    isoversize: false
    isautoswitchedtoquietmode: false
view: |-
  {{
    "linkLabelsPosition": {{}},
    "paper": {{
      "dimensions": {{
        "height": 295,
        "width": 380,
        "x": 450,
        "y": 50
      }}
    }}
  }}
inputs: []
outputs: []
fromversion: 6.10.0
marketplaces:
- marketplacev2
- platform
"""
    dest = _PACK_DIR / "Playbooks" / f"playbook-{slug}.yml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml_text, encoding="utf-8")
    rel = dest.relative_to(_CONTENT_DIR)
    emit("skill_invoked", f"Engineer: scaffolded playbook '{name}'",
         severity="success", payload={"file": str(rel)})
    return {"ok": True, "playbook_id": name, "file": str(rel),
            "abs_path": str(dest), "yaml": yaml_text}


def engineer_write_content(kind: str, name: str, content: str, **_: Any) -> dict:
    """Write raw YAML content for a playbook or automation script into the SiriusDemo
    pack. kind = 'playbook' or 'script'. Use this when you have authored the full
    content-item YAML yourself; use engineer_scaffold_playbook for a fresh skeleton."""
    kind = (kind or "").strip().lower()
    if kind not in ("playbook", "script"):
        return {"ok": False, "error": "kind must be 'playbook' or 'script'"}
    if not (name and content):
        return {"ok": False, "error": "name and content are required"}
    slug = _slug(name)
    if kind == "playbook":
        dest = _PACK_DIR / "Playbooks" / f"playbook-{slug}.yml"
    else:
        dest = _PACK_DIR / "Scripts" / f"script-{slug}.yml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    rel = dest.relative_to(_CONTENT_DIR)
    result: dict[str, Any] = {"ok": True, "kind": kind, "file": str(rel),
                              "abs_path": str(dest)}
    # A playbook uploads fine without a top-level `view:` (canvas paper
    # dimensions) but then cannot be opened in the Cortex graphical Editor.
    # Warn so the author adds one before deploying.
    if kind == "playbook" and not re.search(r"(?m)^view:", content):
        warning = ("playbook has no top-level `view:` block — it will deploy but "
                   "will not open in the Cortex graphical Editor. Add a top-level "
                   "`view: |-` with paper dimensions (see the scaffold output).")
        result["warning"] = warning
        emit("engineer_step", f"WARNING: {warning}", severity="warn",
             payload={"file": str(rel)})
    emit("skill_invoked", f"Engineer: wrote {kind} '{name}'",
         severity="success", payload={"file": str(rel)})
    return result


def engineer_list_content(**_: Any) -> dict:
    """List the local content items (playbooks + scripts) in the SiriusDemo pack."""
    emit("skill_invoked", "Engineer: list local pack content")
    playbooks = sorted(str(p.relative_to(_CONTENT_DIR))
                       for p in (_PACK_DIR / "Playbooks").glob("*.yml"))
    scripts = sorted(str(p.relative_to(_CONTENT_DIR))
                     for p in (_PACK_DIR / "Scripts").glob("*.yml"))
    return {"ok": True, "pack": str(_PACK_DIR.relative_to(_REPO_ROOT)),
            "playbooks": playbooks, "scripts": scripts}


# ── target resolution for validate/deploy ────────────────────────────────────
def _resolve_target(target: str) -> Path:
    """Resolve a validate/deploy target to an absolute path. Empty -> whole pack.
    Accepts an absolute path, a path relative to cortex-content/, or a bare file
    name (looked up under the pack's Playbooks/ then Scripts/)."""
    if not target:
        return _PACK_DIR
    p = Path(target)
    if p.is_absolute() and p.exists():
        return p
    cand = _CONTENT_DIR / target
    if cand.exists():
        return cand
    for sub in ("Playbooks", "Scripts"):
        cand = _PACK_DIR / sub / target
        if cand.exists():
            return cand
    # Accept a bare content-item name (what scaffold returns as playbook_id) by
    # trying the slugged file naming the pack uses.
    slug = _slug(target)
    for sub, prefix in (("Playbooks", "playbook-"), ("Scripts", "script-")):
        cand = _PACK_DIR / sub / f"{prefix}{slug}.yml"
        if cand.exists():
            return cand
    return _CONTENT_DIR / target  # let the sdk report a clear not-found


def _sdk_input(path: Path) -> str:
    """demisto-sdk resolves `-i` against its detected CONTENT_PATH, which here
    falls back to the cwd ('.') because cortex-content/ has no git 'origin' remote.
    An absolute path then blows up in get_relative_path (`relative_to('.')`), so
    pass a path relative to _CONTENT_DIR (which is the subprocess cwd) instead."""
    try:
        return str(path.relative_to(_CONTENT_DIR))
    except ValueError:
        return str(path)  # outside the content repo — let the sdk report it


def engineer_validate(target: str = "", **_: Any) -> dict:
    """Validate content with demisto-sdk before deploying. target defaults to the
    whole SiriusDemo pack; pass a file path/name to validate just one item."""
    if not _SDK.exists():
        return {"ok": False, "error": f"demisto-sdk not found at {_SDK}"}
    path = _resolve_target(target)
    emit("skill_invoked", f"Engineer: validate {path.name}",
         payload={"target": str(path)})
    env = _demisto_env()
    return _stream([str(_SDK), "validate", "-i", _sdk_input(path)], "validate", env, _CONTENT_DIR)


def engineer_deploy(target: str = "", **_: Any) -> dict:
    """Deploy (upload) content to the live Cortex XSIAM tenant with demisto-sdk.
    target defaults to the whole SiriusDemo pack; pass a playbook/script file
    path/name to upload just that item. Requires the STANDARD demisto credentials."""
    if not _SDK.exists():
        return {"ok": False, "error": f"demisto-sdk not found at {_SDK}"}
    env = _demisto_env()
    ok, err = _creds_ok(env)
    if not ok:
        emit("error", f"Engineer deploy blocked: {err}", severity="danger")
        return {"ok": False, "error": err}
    path = _resolve_target(target)
    emit("skill_invoked", f"Engineer: deploy {path.name} to Cortex",
         severity="warn", payload={"target": str(path),
                                   "tenant": env.get("DEMISTO_BASE_URL")})
    res = _stream([str(_SDK), "upload", "-i", _sdk_input(path),
                   "--marketplace", "marketplacev2", "--insecure"],
                  "upload", env, _CONTENT_DIR)
    if res.get("ok"):
        emit("status", f"Deployed {path.name} to {env.get('DEMISTO_BASE_URL')}",
             severity="success", payload={"target": str(path)})
    return res


# ── live content management (list / delete) ──────────────────────────────────
def engineer_list_playbooks(query: str = "", **_: Any) -> dict:
    """List playbooks that exist on the live Cortex tenant (optionally filtered by
    a name query). Use it to find the exact playbook id to delete."""
    emit("skill_invoked", "Engineer: list live playbooks", payload={"query": query})
    res = _run_api("list-playbooks", query or "")
    emit("status", f"Live playbooks: {res.get('count', '?')}"
         + ("" if res.get("ok") else f" — {res.get('error','')}"),
         severity="success" if res.get("ok") else "danger", payload=res)
    return res


def engineer_delete_playbook(playbook_id: str, **_: Any) -> dict:
    """Delete a playbook from the live Cortex tenant by its id (POST /playbook/delete).
    Get the exact id from engineer_list_playbooks first."""
    if not playbook_id:
        return {"ok": False, "error": "playbook_id is required"}
    emit("skill_invoked", f"Engineer: DELETE playbook '{playbook_id}'",
         severity="warn", payload={"playbook_id": playbook_id})
    res = _run_api("delete-playbook", playbook_id)
    emit("status",
         f"Deleted playbook '{playbook_id}'" if res.get("ok")
         else f"Delete failed: {res.get('error','')}",
         severity="success" if res.get("ok") else "danger", payload=res)
    return res


def engineer_delete_script(script_id: str, **_: Any) -> dict:
    """Delete an automation script from the live Cortex tenant by its id."""
    if not script_id:
        return {"ok": False, "error": "script_id is required"}
    emit("skill_invoked", f"Engineer: DELETE script '{script_id}'",
         severity="warn", payload={"script_id": script_id})
    res = _run_api("delete-script", script_id)
    emit("status",
         f"Deleted script '{script_id}'" if res.get("ok")
         else f"Delete failed: {res.get('error','')}",
         severity="success" if res.get("ok") else "danger", payload=res)
    return res


def engineer_status(**_: Any) -> dict:
    """Report whether Engineer is ready: demisto-sdk present, credentials set, and
    the tenant reachable (by listing playbooks)."""
    emit("skill_invoked", "Engineer: readiness check")
    env = _demisto_env()
    ok, err = _creds_ok(env)
    status = {
        "sdk_present": _SDK.exists(),
        "venv_python": _VENV_PY.exists(),
        "credentials_ok": ok,
        "tenant": env.get("DEMISTO_BASE_URL") or None,
        "auth_id": env.get("XSIAM_AUTH_ID") or None,
    }
    if not ok:
        status.update(ok=False, error=err)
        emit("status", f"Engineer not ready: {err}", severity="danger", payload=status)
        return status
    live = _run_api("list-playbooks", "Sirius")
    status["ok"] = bool(live.get("ok"))
    status["tenant_reachable"] = bool(live.get("ok"))
    status["sirius_playbooks"] = live.get("playbooks", []) if live.get("ok") else []
    if not live.get("ok"):
        status["error"] = live.get("error")
    emit("status", "Engineer ready" if status["ok"] else "Engineer: tenant unreachable",
         severity="success" if status["ok"] else "danger", payload=status)
    return status


# ── register skills ──────────────────────────────────────────────────────────
_EMPTY = {"type": "object", "properties": {}}

registry.skill("engineer_status",
               "Check Engineer readiness: demisto-sdk present, credentials set, tenant reachable.",
               _EMPTY)(engineer_status)
registry.skill("engineer_list_content",
               "List the local playbooks + scripts in the SiriusDemo content pack.",
               _EMPTY)(engineer_list_content)
registry.skill("engineer_scaffold_playbook",
               "Generate a minimal valid Cortex XSIAM playbook skeleton and write it into the pack.",
               {"type": "object",
                "properties": {"name": {"type": "string", "description": "Playbook name/id."},
                               "description": {"type": "string"}},
                "required": ["name"]})(engineer_scaffold_playbook)
registry.skill("engineer_write_content",
               "Write full YAML for a playbook or automation script into the SiriusDemo pack.",
               {"type": "object",
                "properties": {"kind": {"type": "string", "enum": ["playbook", "script"]},
                               "name": {"type": "string"},
                               "content": {"type": "string", "description": "The complete YAML."}},
                "required": ["kind", "name", "content"]})(engineer_write_content)
registry.skill("engineer_validate",
               "Validate content with demisto-sdk (defaults to the whole SiriusDemo pack).",
               {"type": "object",
                "properties": {"target": {"type": "string",
                                          "description": "File path/name, or empty for the whole pack."}}}
               )(engineer_validate)
registry.skill("engineer_deploy",
               "Deploy (upload) content to the live Cortex XSIAM tenant with demisto-sdk.",
               {"type": "object",
                "properties": {"target": {"type": "string",
                                          "description": "Playbook/script file path/name, or empty for the whole pack."}}}
               )(engineer_deploy)
registry.skill("engineer_list_playbooks",
               "List playbooks on the live Cortex tenant (optional name query) to find ids.",
               {"type": "object",
                "properties": {"query": {"type": "string"}}})(engineer_list_playbooks)
registry.skill("engineer_delete_playbook",
               "Delete a playbook from the live Cortex tenant by id.",
               {"type": "object",
                "properties": {"playbook_id": {"type": "string"}},
                "required": ["playbook_id"]})(engineer_delete_playbook)
registry.skill("engineer_delete_script",
               "Delete an automation script from the live Cortex tenant by id.",
               {"type": "object",
                "properties": {"script_id": {"type": "string"}},
                "required": ["script_id"]})(engineer_delete_script)
