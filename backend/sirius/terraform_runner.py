"""
Terraform build/destroy runner (Feature 4).

Wraps the terraform CLI, streaming every line as a `terraform_step` event so the
UI shows the plan/apply/destroy progress live. Ported from Alfred's terraform
skill playbook, with the same hard credential rules:

  • Credentials come from the process environment ONLY — never written to files,
    never placed on a command line, never echoed.
  • AWS TEMPORARY credentials are supported: AWS_SESSION_TOKEN is honored (it's
    just inherited from the environment by terraform + the aws CLI).

Projects live one folder per project under <repo>/terraform/<name>/.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sirius import infra_gate
from sirius.events import emit
from sirius.skills import registry

_log = logging.getLogger("sirius.terraform")

# <repo root>/terraform  (this file is <repo>/backend/sirius/terraform_runner.py)
TERRAFORM_ROOT = Path(__file__).resolve().parents[2] / "terraform"

# Persisted apply/destroy history (backend root; survives restarts/sessions).
INVENTORY_PATH = Path(__file__).resolve().parent.parent / "terraform_inventory.json"

# Guard so a bad/injected project name can't escape the terraform/ directory.
_SAFE_NAME = __import__("re").compile(r"^[a-zA-Z0-9._-]+$")


def _project_dir(name: str) -> Path:
    if not _SAFE_NAME.match(name or ""):
        raise ValueError(f"invalid terraform project name: {name!r}")
    d = (TERRAFORM_ROOT / name).resolve()
    if not str(d).startswith(str(TERRAFORM_ROOT.resolve())):
        raise ValueError("project path escapes terraform root")
    return d


def _run_streamed(cmd: list[str], title: str, cwd: Path | None = None) -> dict:
    """Run a command, emitting each stdout line as a terraform_step event."""
    emit("terraform_step", f"$ {' '.join(cmd)}", payload={"cmd": cmd, "phase": title})
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=os.environ.copy(),  # inherits AWS_* incl. AWS_SESSION_TOKEN
        )
    except FileNotFoundError:
        emit("error", f"{cmd[0]} not found on PATH", severity="danger")
        return {"ok": False, "error": f"{cmd[0]} not installed"}

    assert proc.stdout is not None
    err_lines: list[str] = []
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        lines.append(line)
        low = line.lower()
        # Keep the Terraform error block (the actionable bit) separate from the
        # general tail so the agent always sees WHY an apply failed — even when the
        # error scrolls out of the last-N-lines window. Terraform renders errors in
        # a box drawn with │ ╷ ╵, plus the "Error:" heading itself.
        is_err_text = "error" in low or "failed" in low
        if is_err_text or line.lstrip().startswith(("│", "╷", "╵")):
            err_lines.append(line)
        sev = "danger" if is_err_text else "info"
        emit("terraform_step", line, severity=sev, payload={"phase": title})
    code = proc.wait()
    ok = code == 0
    emit(
        "terraform_step",
        f"{title} exited with code {code}",
        severity="success" if ok else "danger",
        payload={"phase": title, "exit_code": code},
    )
    result = {"ok": ok, "exit_code": code, "output": "\n".join(lines[-120:])}
    if not ok and err_lines:
        # Capped, and the END kept (the root-cause heading is usually last).
        result["terraform_error"] = "\n".join(err_lines)[-4000:]
    return result


def _terraform() -> str:
    return shutil.which("terraform") or "terraform"


# ── Skills ────────────────────────────────────────────────────────────────────
def aws_whoami(**_: Any) -> dict:
    """Confirm AWS credentials are present WITHOUT printing them."""
    aws = shutil.which("aws")
    if not aws:
        return {"ok": False, "error": "aws CLI not installed"}
    emit("terraform_step", "$ aws sts get-caller-identity", payload={"phase": "preflight"})
    try:
        out = subprocess.run(
            [aws, "sts", "get-caller-identity", "--output", "json"],
            capture_output=True, text=True, timeout=30, env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "aws sts get-caller-identity timed out"}
    if out.returncode != 0:
        emit("error", "AWS credentials not valid/available", severity="danger",
             payload={"stderr": out.stderr.strip()[:500]})
        return {"ok": False, "error": out.stderr.strip()[:500]}
    ident = json.loads(out.stdout or "{}")
    using_session = bool(os.environ.get("AWS_SESSION_TOKEN"))
    emit("terraform_step",
         f"AWS identity OK (account {ident.get('Account')}, session_token={using_session})",
         severity="success", payload={"account": ident.get("Account"),
                                      "arn": ident.get("Arn"),
                                      "temporary_credentials": using_session})
    return {"ok": True, "account": ident.get("Account"), "arn": ident.get("Arn"),
            "temporary_credentials": using_session}


def _preflight() -> dict:
    """Verify AWS creds; if absent, start SSO login (non-blocking) and tell the
    user to sign in and retry. Returns aws_whoami's result on success."""
    pre = aws_whoami()
    if pre.get("ok"):
        return pre
    # No valid AWS session — offer on-demand SSO login (link streams to the UI).
    from sirius import aws_sso  # lazy import to avoid any import-order coupling
    login = aws_sso.start_login_for_current_session()
    if login.get("ok"):
        return {"ok": False, "needs_auth": True,
                "error": "No valid AWS session. I've started AWS SSO login — open the "
                         "sign-in link in the event stream, complete it, then ask me to "
                         "run this again."}
    # SSO not configured or couldn't start — return the plain preflight failure.
    return {"ok": False, "error": "AWS preflight failed", "detail": pre,
            "hint": login.get("error")}


def terraform_list_projects(**_: Any) -> dict:
    if not TERRAFORM_ROOT.exists():
        return {"projects": []}
    projects = sorted(p.name for p in TERRAFORM_ROOT.iterdir()
                      if p.is_dir() and any(p.glob("*.tf")))
    return {"projects": projects}


def terraform_plan(project: str, **_: Any) -> dict:
    d = _project_dir(project)
    if not d.exists():
        return {"ok": False, "error": f"no such project: {project}"}
    pre = _preflight()
    if not pre.get("ok"):
        return pre
    # Warn (don't block) on account mismatch: plan is read-only and seeing the diff
    # against the wrong account is itself informative.
    _account_guard(project, d, pre.get("account", ""), "plan", block=False)
    _run_streamed([_terraform(), f"-chdir={d}", "init", "-input=false"], "init")
    return _run_streamed(
        [_terraform(), f"-chdir={d}", "plan", "-input=false", "-no-color"], "plan"
    )


def terraform_apply(project: str, **_: Any) -> dict:
    d = _project_dir(project)
    if not d.exists():
        return {"ok": False, "error": f"no such project: {project}"}
    # Preflight BEFORE the gate: guard() consumes the one-shot human authorization,
    # so if we checked creds after it and they were missing, the grant would be
    # burned and the user would have to type the confirmation name a second time.
    pre = _preflight()
    if not pre.get("ok"):
        return pre
    # Block an apply against a different AWS account than this state was built on —
    # before the gate, so a mismatch never burns the human confirmation.
    mismatch = _account_guard(project, d, pre.get("account", ""), "apply")
    if mismatch:
        return mismatch
    refused = infra_gate.guard(project, "apply")  # per-agent scope + human ack
    if refused:
        return refused
    _run_streamed([_terraform(), f"-chdir={d}", "init", "-input=false"], "init")
    res = _run_streamed(
        [_terraform(), f"-chdir={d}", "apply", "-auto-approve", "-input=false", "-no-color"],
        "apply",
    )
    # Ground the result in the actual post-apply state so the agent can't report a
    # success terraform didn't produce (a clean plan can still fail mid-apply).
    resources = _state_resources(d)
    res["resource_count"] = len(resources)
    res["applied"] = bool(resources)
    if res.get("ok"):
        out = subprocess.run(
            [_terraform(), f"-chdir={d}", "output", "-json"],
            capture_output=True, text=True, env=os.environ.copy(),
        )
        try:
            res["outputs"] = json.loads(out.stdout or "{}")
        except json.JSONDecodeError:
            res["outputs"] = {}
        _record_inventory(project, "apply", len(resources), res.get("outputs"))
        # Bind this project's state to the account it was applied against, so a
        # later apply/destroy from a different identity is refused.
        _write_account_marker(d, pre.get("account", ""), pre.get("arn", ""))
    else:
        # Make failure unmistakable to the model — it must not claim success — and
        # hand it the Terraform error block so it can diagnose and self-heal.
        tf_err = res.get("terraform_error") or ""
        res.setdefault("error",
                        f"terraform apply failed (exit {res.get('exit_code')}); "
                        f"{len(resources)} resource(s) in state. "
                        + (f"Terraform error:\n{tf_err}" if tf_err else "See output above."))
    return res


def terraform_destroy(project: str, **_: Any) -> dict:
    d = _project_dir(project)
    if not d.exists():
        return {"ok": False, "error": f"no such project: {project}"}
    # Preflight before the gate — see terraform_apply for why (don't burn the
    # one-shot authorization if AWS creds turn out to be missing).
    pre = _preflight()
    if not pre.get("ok"):
        return pre
    # Never destroy against the wrong account — the recorded resources don't live
    # there. Check before the gate so a mismatch doesn't consume the confirmation.
    mismatch = _account_guard(project, d, pre.get("account", ""), "destroy")
    if mismatch:
        return mismatch
    refused = infra_gate.guard(project, "destroy")  # per-agent scope + human ack
    if refused:
        return refused
    res = _run_streamed(
        [_terraform(), f"-chdir={d}", "destroy", "-auto-approve", "-input=false", "-no-color"],
        "destroy",
    )
    if res.get("ok"):
        _record_inventory(project, "destroy", len(_state_resources(d)))
        # State is now empty; drop the account binding so a legitimate later rebuild
        # in this dir adopts whatever account it's next applied against.
        _clear_account_marker(d)
    return res


def terraform_write_project(project: str, files: Any, description: str = "",
                            **_: Any) -> dict:
    """Create/update a terraform project by writing .tf files under terraform/<project>/.

    `files` is a list of {"filename": str, "content": str}. Scope-checked only (the
    calling agent must be in scope for the project) — authoring local files needs no
    typed confirmation; that's reserved for apply/destroy. Never writes outside the
    project dir.
    """
    if not _SAFE_NAME.match(project or ""):
        return {"ok": False, "error": f"invalid terraform project name: {project!r}"}
    d = (TERRAFORM_ROOT / project).resolve()
    if not str(d).startswith(str(TERRAFORM_ROOT.resolve())):
        return {"ok": False, "error": "project path escapes terraform root"}
    if not isinstance(files, list) or not files:
        return {"ok": False, "error": "files must be a non-empty list of {filename, content}"}

    refused = infra_gate.guard(project, "create", confirm=False)  # scope only (local files)
    if refused:
        return refused

    # Validate all filenames before writing anything (no traversal, simple names).
    plan: list[tuple[Path, str]] = []
    for f in files:
        name = (f or {}).get("filename", "")
        content = (f or {}).get("content", "")
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return {"ok": False, "error": f"invalid filename: {name!r}"}
        fp = (d / name).resolve()
        if not str(fp).startswith(str(d) + os.sep) and str(fp) != str(d / name):
            return {"ok": False, "error": f"filename escapes project dir: {name!r}"}
        plan.append((fp, content))

    existed = d.exists()
    d.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for fp, content in plan:
        fp.write_text(content, encoding="utf-8")
        written.append(fp.name)
        emit("terraform_step", f"wrote {project}/{fp.name} ({len(content)} bytes)",
             payload={"file": fp.name, "project": project})
    emit("status",
         f"{'Updated' if existed else 'Created'} terraform project {project} "
         f"({len(written)} files)",
         severity="success", payload={"project": project, "files": written})
    return {"ok": True, "project": project, "created": not existed,
            "files": written, "path": str(d), "description": description}


def terraform_remove_project(project: str, force: bool = False, **_: Any) -> dict:
    """Delete a terraform project folder to reclaim local space. Scope-checked only
    (no typed confirmation — it's a local file op).

    Refuses if the project still has live resources in state (run terraform_destroy
    first) unless force=True.
    """
    if not _SAFE_NAME.match(project or ""):
        return {"ok": False, "error": f"invalid terraform project name: {project!r}"}
    d = (TERRAFORM_ROOT / project).resolve()
    if not str(d).startswith(str(TERRAFORM_ROOT.resolve())):
        return {"ok": False, "error": "project path escapes terraform root"}
    if not d.exists():
        return {"ok": False, "error": f"no such project: {project}"}

    refused = infra_gate.guard(project, "remove", confirm=False)  # scope only (local files)
    if refused:
        return refused

    live = _state_resources(d)
    if live and not force:
        return {"ok": False,
                "error": (f"project {project!r} still has {len(live)} live resource(s) in "
                          "state — run terraform_destroy first, or pass force=true."),
                "live_resources": live}

    emit("terraform_step", f"Removing project folder terraform/{project}…",
         severity="warn", payload={"project": project})
    shutil.rmtree(d)
    emit("status", f"Removed terraform project {project}", severity="success",
         payload={"project": project})
    return {"ok": True, "removed": project}


# ── State inspection + persisted inventory ──────────────────────────────────────
def _resources_from_statefile(d: Path) -> list[str] | None:
    """Parse managed-resource addresses straight from a local terraform.tfstate.

    Reliable even when the project isn't initialized in this checkout (no
    .terraform/ dir) and needs no AWS creds. Returns None if there's no local
    state file (e.g. a remote backend) so the caller can fall back to the CLI.
    """
    f = d / "terraform.tfstate"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return None
    addrs: list[str] = []
    for r in data.get("resources", []):
        if r.get("mode") == "data":  # skip data sources — not provisioned infra
            continue
        base = f"{r.get('type')}.{r.get('name')}"
        module = r.get("module")
        for inst in r.get("instances", [{}]):
            addr = f"{module}.{base}" if module else base
            idx = inst.get("index_key")
            if idx is not None:
                addr += f"[{idx!r}]"
            addrs.append(addr)
    return addrs


def _state_resources(d: Path) -> list[str]:
    """Resource addresses currently in a project's terraform state ([] if none).

    Prefers the local state file (works without init/creds); falls back to
    `terraform state list` for remote backends.
    """
    from_file = _resources_from_statefile(d)
    if from_file is not None:
        return from_file
    try:
        out = subprocess.run(
            [_terraform(), f"-chdir={d}", "state", "list"],
            capture_output=True, text=True, timeout=60, env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return []
    if out.returncode != 0:
        return []  # no state / not initialized
    return [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]


def _outputs(d: Path) -> dict:
    """Output name -> value, with sensitive values redacted. Prefers the local
    state file (no init needed); falls back to `terraform output -json`."""
    f = d / "terraform.tfstate"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            return {}
        return {name: ("<sensitive>" if meta.get("sensitive") else meta.get("value"))
                for name, meta in (data.get("outputs") or {}).items()}
    try:
        out = subprocess.run(
            [_terraform(), f"-chdir={d}", "output", "-json"],
            capture_output=True, text=True, timeout=60, env=os.environ.copy(),
        )
        raw = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
        return {name: ("<sensitive>" if meta.get("sensitive") else meta.get("value"))
                for name, meta in raw.items()}
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


def _read_inventory() -> list[dict]:
    if not INVENTORY_PATH.exists():
        return []
    try:
        return json.loads(INVENTORY_PATH.read_text(encoding="utf-8")) or []
    except (json.JSONDecodeError, OSError):
        return []


def _record_inventory(project: str, action: str, resource_count: int,
                      outputs: dict | None = None) -> None:
    """Append an apply/destroy event to the persisted inventory (best-effort)."""
    entry = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": project,
        "action": action,               # "apply" | "destroy"
        "resource_count": resource_count,
        "outputs": list((outputs or {}).keys()),  # output names only, no secret values
    }
    try:
        hist = _read_inventory()
        hist.append(entry)
        INVENTORY_PATH.write_text(json.dumps(hist[-500:], indent=2), encoding="utf-8")
    except OSError as e:  # pragma: no cover
        _log.warning("could not write inventory: %s", e)


# ── AWS account guard ──────────────────────────────────────────────────────────
# A project's terraform state is only meaningful against the ONE AWS account it was
# applied to. Re-applying/destroying it while signed in to a DIFFERENT account
# diffs against phantom resources and can create cross-account confusion (exactly
# the footgun that orphaned resources when a reused project dir carried stale state
# from another account). We record the account at apply time in a per-project
# sidecar and refuse mutating ops when the current identity doesn't match.
_ACCOUNT_MARKER = ".sirius_account.json"


def _account_marker_path(d: Path) -> Path:
    return d / _ACCOUNT_MARKER


def _read_account_marker(d: Path) -> Optional[dict]:
    f = _account_marker_path(d)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return None


def _write_account_marker(d: Path, account: str, arn: str = "") -> None:
    if not account:
        return
    try:
        _account_marker_path(d).write_text(
            json.dumps({"account": account, "arn": arn,
                        "iso": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                       indent=2),
            encoding="utf-8")
    except OSError as e:  # pragma: no cover
        _log.warning("could not write account marker: %s", e)


def _clear_account_marker(d: Path) -> None:
    try:
        _account_marker_path(d).unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass


def _account_guard(project: str, d: Path, current_account: str,
                   op: str, block: bool = True) -> Optional[dict]:
    """Compare the current AWS account to the one this project's state was applied
    against. On mismatch, emit a `blocked` event and (if `block`) return a refusal
    dict; otherwise return None. Legacy state with no recorded account is allowed
    with a warning (we can't know its true account until the next apply records it).
    """
    if not current_account:
        return None  # preflight already validated creds; nothing to compare
    recorded = (_read_account_marker(d) or {}).get("account")
    if recorded and recorded != current_account:
        msg = (f"Account mismatch for terraform project {project!r}: its state was applied "
               f"against AWS account {recorded}, but the current identity is account "
               f"{current_account}. A {op} here would diff against the wrong account and can "
               f"create phantom or cross-account resources. Either start a NEW, uniquely-named "
               f"project for account {current_account}, or switch credentials back to account "
               f"{recorded} for this deployment.")
        emit("blocked",
             f"account mismatch — {project} state is account {recorded}, you are {current_account}",
             severity="danger",
             payload={"project": project, "op": op, "recorded_account": recorded,
                      "current_account": current_account})
        if block:
            return {"ok": False, "refused": True, "account_mismatch": True,
                    "recorded_account": recorded, "current_account": current_account,
                    "reason": msg}
        return None
    if not recorded and _resources_from_statefile(d):
        emit("terraform_step",
             f"Note: {project} has existing state but no recorded AWS account; "
             f"treating it as account {current_account}.",
             severity="warn",
             payload={"project": project, "current_account": current_account})
    return None


def terraform_status(**_: Any) -> dict:
    """What's provisioned right now — reads terraform state for every project."""
    projects = []
    if TERRAFORM_ROOT.exists():
        for p in sorted(TERRAFORM_ROOT.iterdir()):
            if not (p.is_dir() and any(p.glob("*.tf"))):
                continue
            resources = _state_resources(p)
            projects.append({
                "project": p.name,
                "applied": bool(resources),
                "resource_count": len(resources),
                "resources": resources[:100],
                "outputs": list(_outputs(p).keys()) if resources else [],
            })
    live = [x["project"] for x in projects if x["applied"]]
    emit("terraform_step",
         f"Infra status: {len(live)} project(s) provisioned"
         + (f" ({', '.join(live)})" if live else ""),
         payload={"phase": "status", "provisioned": live})
    return {"projects": projects, "provisioned": live}


def terraform_state_list(project: str, **_: Any) -> dict:
    """List the resources in one project's terraform state."""
    d = _project_dir(project)
    if not d.exists():
        return {"ok": False, "error": f"no such project: {project}"}
    resources = _state_resources(d)
    return {"ok": True, "project": project, "applied": bool(resources),
            "resource_count": len(resources), "resources": resources,
            "outputs": _outputs(d) if resources else {}}


def terraform_history(limit: int = 50, **_: Any) -> dict:
    """Recent apply/destroy events recorded by Sirius (persisted across sessions)."""
    hist = _read_inventory()
    return {"count": len(hist), "events": hist[-max(1, int(limit)):][::-1]}


_PROJECT_SCHEMA = {
    "type": "object",
    "properties": {"project": {"type": "string", "description": "terraform/<name> folder"}},
    "required": ["project"],
}
registry.skill("aws_whoami", "Verify AWS credentials are available (no secrets printed).",
               {"type": "object", "properties": {}})(aws_whoami)
registry.skill("terraform_list_projects", "List available terraform projects.",
               {"type": "object", "properties": {}})(terraform_list_projects)
registry.skill("terraform_plan", "Run terraform init + plan for a project.",
               _PROJECT_SCHEMA)(terraform_plan)
registry.skill("terraform_apply", "Run terraform init + apply (-auto-approve) for a project.",
               _PROJECT_SCHEMA)(terraform_apply)
registry.skill("terraform_destroy", "Run terraform destroy (-auto-approve) for a project.",
               _PROJECT_SCHEMA)(terraform_destroy)
registry.skill("terraform_status",
               "Show what infrastructure is currently provisioned (reads terraform "
               "state for every project).", {"type": "object", "properties": {}})(terraform_status)
registry.skill("terraform_state_list",
               "List the resources in one project's terraform state.",
               _PROJECT_SCHEMA)(terraform_state_list)
registry.skill("terraform_history",
               "Show recent terraform apply/destroy events recorded by Sirius "
               "(persisted across sessions).",
               {"type": "object", "properties": {
                   "limit": {"type": "integer", "description": "max events (default 50)"}}})(
                       terraform_history)
registry.skill("terraform_write_project",
               "Create/update a terraform project by writing .tf files under "
               "terraform/<project>/. Gated (create): the project must be authorized. "
               "You author the file contents. Never put secrets in files.",
               {"type": "object", "properties": {
                   "project": {"type": "string", "description": "terraform/<name> folder"},
                   "description": {"type": "string",
                                   "description": "human summary of what this project provisions"},
                   "files": {"type": "array", "description": "the .tf files to write",
                             "items": {"type": "object", "properties": {
                                 "filename": {"type": "string", "description": "e.g. main.tf"},
                                 "content": {"type": "string"}},
                                 "required": ["filename", "content"]}}},
                "required": ["project", "files"]})(terraform_write_project)
registry.skill("terraform_remove_project",
               "Delete a terraform project folder to reclaim local space. Gated "
               "(remove). Refuses if the project still has live resources unless "
               "force=true — destroy first.",
               {"type": "object", "properties": {
                   "project": {"type": "string", "description": "terraform/<name> folder"},
                   "force": {"type": "boolean",
                             "description": "remove even if state still has resources"}},
                "required": ["project"]})(terraform_remove_project)
