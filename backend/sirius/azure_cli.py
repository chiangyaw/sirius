"""
Azure CLI integration for the Sirius agents.

The Azure sibling to `aws_sso.py` + `terraform_runner.py`'s AWS helpers. It gives
Nova / Defender / Terra / Raven a first-class `az` capability so any Azure
onboarding or demo deploy "just works" from inside the running backend:

  • azure_login    — web-UI-driven `az login --use-device-code`; the verification
    URL + code stream to the event panel (exactly like the aws_sso flow). Re-auth
    on token expiry is another click — no server restart.
  • azure_whoami / azure_account_list / azure_set_subscription — identity +
    subscription selection.
  • azure_cli      — a general `az ...` runner. Read-only verbs (show/list/get…)
    run freely; mutating verbs (create/delete/deploy…) are double-gated exactly
    like `terraform apply` (config flag + typed human confirmation).

Credential rules (same as terraform_runner / aws_sso):
  • Credentials come from the process environment and the az CLI's own token
    cache (~/.azure/) ONLY — never written to files by us, never placed on a
    command line, never echoed.
  • `az account show` prints identity, never tokens.
  • Commands are always argument LISTS (shell=False) — no shell interpolation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
from typing import Any

from sirius import infra_gate
from sirius.config import AzureConfig, load_config
from sirius.events import current_session, emit
from sirius.skills import registry

_log = logging.getLogger("sirius.azure")

# Only one interactive login at a time (mirrors aws_sso).
_login_lock = threading.Lock()
_login_in_progress = False

# `az login --use-device-code` prints e.g.:
#   To sign in, use a web browser to open the page https://microsoft.com/devicelogin
#   and enter the code ABCD1234 to authenticate.
_URL_RE = re.compile(r"https://\S+")
_CODE_RE = re.compile(r"enter the code (\S+) to authenticate", re.IGNORECASE)

# az subcommands that only READ — safe to run without human confirmation. Anything
# not matched here is treated as mutating and must pass the infra gate.
_READ_VERBS = {
    "show", "list", "get", "version", "check-name", "export", "wait",
    "list-locations", "list-sizes", "list-skus", "help",
}


def _az() -> str:
    return shutil.which("az") or "az"


# ── Identity ────────────────────────────────────────────────────────────────────
def azure_whoami(**_: Any) -> dict:
    """Report the current Azure identity/subscription WITHOUT printing any secret."""
    az = shutil.which("az")
    if not az:
        return {"ok": False, "error": "az (Azure CLI) not installed"}
    emit("azure_step", "$ az account show", payload={"phase": "preflight"})
    try:
        out = subprocess.run(
            [az, "account", "show", "-o", "json"],
            capture_output=True, text=True, timeout=30, env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "az account show timed out"}
    if out.returncode != 0:
        err = (out.stderr or "").strip()[:300]
        emit("azure_login", "Not signed in to Azure", severity="warn",
             payload={"error": err})
        return {"ok": False, "error": err or "not signed in"}
    acct = json.loads(out.stdout or "{}")
    user = (acct.get("user") or {}).get("name")
    emit("azure_login",
         f"Azure identity OK (subscription {acct.get('name')})",
         severity="success",
         payload={"subscription_id": acct.get("id"),
                  "subscription_name": acct.get("name"),
                  "tenant_id": acct.get("tenantId"), "user": user})
    return {"ok": True, "subscription_id": acct.get("id"),
            "subscription_name": acct.get("name"),
            "tenant_id": acct.get("tenantId"), "user": user}


def azure_account_list(**_: Any) -> dict:
    """List the subscriptions the current identity can see (ids/names/tenants)."""
    az = shutil.which("az")
    if not az:
        return {"ok": False, "error": "az (Azure CLI) not installed"}
    try:
        out = subprocess.run(
            [az, "account", "list", "-o", "json"],
            capture_output=True, text=True, timeout=30, env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "az account list timed out"}
    if out.returncode != 0:
        return {"ok": False, "error": (out.stderr or "").strip()[:300]}
    raw = json.loads(out.stdout or "[]")
    subs = [{"id": s.get("id"), "name": s.get("name"),
             "tenant_id": s.get("tenantId"), "is_default": s.get("isDefault")}
            for s in raw]
    return {"ok": True, "subscriptions": subs}


def azure_set_subscription(subscription: str, **_: Any) -> dict:
    """Select the active Azure subscription (by id or name) for this process."""
    if not subscription:
        return {"ok": False, "error": "subscription (id or name) required"}
    az = shutil.which("az")
    if not az:
        return {"ok": False, "error": "az (Azure CLI) not installed"}
    emit("azure_step", f"$ az account set --subscription {subscription}",
         payload={"phase": "subscription"})
    out = subprocess.run(
        [az, "account", "set", "--subscription", subscription],
        capture_output=True, text=True, timeout=30, env=os.environ.copy(),
    )
    if out.returncode != 0:
        return {"ok": False, "error": (out.stderr or "").strip()[:300]}
    return azure_whoami()


# ── Device-code login (mirrors aws_sso._run_login / begin_login) ─────────────────
def _run_login(session_id: str, cfg: AzureConfig) -> None:
    """Blocking `az login --use-device-code` worker — runs in a background thread."""
    global _login_in_progress
    cmd = [_az(), "login", "--use-device-code"]
    if cfg.tenant_id:
        cmd += ["--tenant", cfg.tenant_id]
    emit("azure_login", "Starting Azure device-code login…", session_id=session_id,
         payload={"tenant": cfg.tenant_id or None})
    prompt_seen = False
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=os.environ.copy(),
        )
    except FileNotFoundError:
        emit("azure_login", "az (Azure CLI) not found on PATH", severity="danger",
             session_id=session_id)
        with _login_lock:
            _login_in_progress = False
        return

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            url_match = _URL_RE.search(line)
            code_match = _CODE_RE.search(line)
            if url_match and not prompt_seen:
                prompt_seen = True
                emit("azure_login", "Open this URL and enter the code to sign in to Azure",
                     severity="warn", session_id=session_id,
                     payload={"url": url_match.group(0),
                              "user_code": code_match.group(1) if code_match else None})
            else:
                emit("azure_login", line, session_id=session_id)
        code = proc.wait()
    finally:
        with _login_lock:
            _login_in_progress = False

    if code != 0:
        emit("azure_login", f"Azure login failed (exit {code})",
             severity="danger", session_id=session_id, payload={"exit_code": code})
        return

    # Select the configured subscription (if any), then report identity.
    if cfg.subscription_id:
        subprocess.run(
            [_az(), "account", "set", "--subscription", cfg.subscription_id],
            capture_output=True, text=True, env=os.environ.copy(),
        )
    st = azure_whoami()
    if st.get("ok"):
        emit("azure_login",
             f"Azure login complete (subscription {st.get('subscription_name')})",
             severity="success", session_id=session_id,
             payload={"subscription_id": st.get("subscription_id"),
                      "subscription_name": st.get("subscription_name"),
                      "tenant_id": st.get("tenant_id")})
    else:
        emit("azure_login", "Login finished but identity check failed",
             severity="danger", session_id=session_id, payload={"error": st.get("error")})


def begin_login(session_id: str, cfg: AzureConfig) -> dict[str, Any]:
    """Kick off `az login --use-device-code` in the background. Returns immediately."""
    global _login_in_progress
    if shutil.which("az") is None:
        return {"ok": False, "error": "az (Azure CLI) not installed"}
    with _login_lock:
        if _login_in_progress:
            return {"ok": False, "error": "an Azure login is already in progress"}
        _login_in_progress = True
    threading.Thread(target=_run_login, args=(session_id, cfg), daemon=True).start()
    return {"ok": True, "started": True, "tenant": cfg.tenant_id or None}


def start_login_for_current_session() -> dict[str, Any]:
    """Kick off Azure login for the session the current skill is serving.

    Used by the azure preflight so a deploy prompt can start the login on demand
    (the sign-in link streams to the UI)."""
    sid = current_session.get()
    if not sid:
        return {"ok": False, "error": "no active session for Azure login"}
    return begin_login(sid, load_config().azure)


def azure_login(**_: Any) -> dict:
    """Skill: start Azure device-code login for the current session."""
    res = start_login_for_current_session()
    if res.get("ok"):
        res["message"] = ("Azure device-code login started — open the sign-in link "
                          "in the event stream, enter the code, then ask me to retry.")
    return res


def _preflight() -> dict:
    """Verify an Azure session; if absent, start device-code login and ask the user
    to sign in and retry. Returns azure_whoami's result on success."""
    pre = azure_whoami()
    if pre.get("ok"):
        return pre
    login = start_login_for_current_session()
    if login.get("ok"):
        return {"ok": False, "needs_auth": True,
                "error": "No Azure session. I've started device-code login — open the "
                         "sign-in link in the event stream, complete it, then ask me to "
                         "run this again."}
    return {"ok": False, "error": "Azure preflight failed", "detail": pre,
            "hint": login.get("error")}


# ── General `az` passthrough runner ─────────────────────────────────────────────
def _run_streamed(cmd: list[str], title: str) -> dict:
    """Run an az command, emitting each stdout line as an azure_step event."""
    emit("azure_step", f"$ {' '.join(cmd)}", payload={"cmd": cmd, "phase": title})
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=os.environ.copy(),  # inherits az session + ARM_*
        )
    except FileNotFoundError:
        emit("error", f"{cmd[0]} not found on PATH", severity="danger")
        return {"ok": False, "error": f"{cmd[0]} not installed"}

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        lines.append(line)
        low = line.lower()
        sev = "danger" if ("error" in low or "failed" in low) else "info"
        emit("azure_step", line, severity=sev, payload={"phase": title})
    code = proc.wait()
    ok = code == 0
    emit("azure_step", f"{title} exited with code {code}",
         severity="success" if ok else "danger",
         payload={"phase": title, "exit_code": code})
    return {"ok": ok, "exit_code": code, "output": "\n".join(lines[-80:])}


def _resource_group(tokens: list[str]) -> str:
    """Pull the -g/--resource-group value from az args, if present."""
    for i, t in enumerate(tokens):
        if t in ("-g", "--resource-group") and i + 1 < len(tokens):
            return tokens[i + 1]
        if t.startswith("--resource-group="):
            return t.split("=", 1)[1]
    return ""


def _action_verb(tokens: list[str]) -> str:
    """The last non-flag token (az group/subcommand action, e.g. 'create')."""
    verb = ""
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            if "=" not in t:
                skip_next = True  # a flag that likely takes a value
            continue
        verb = t
    return verb


def azure_cli(args: Any = None, **_: Any) -> dict:
    """Run a general `az` command. `args` is a list of arguments WITHOUT the leading
    `az` (a string is accepted and shlex-split). Read-only verbs run freely;
    mutating verbs are gated (typed human confirmation) like terraform apply."""
    if isinstance(args, str):
        tokens = shlex.split(args)
    elif isinstance(args, list):
        tokens = [str(a) for a in args]
    else:
        return {"ok": False, "error": "args must be a list (or string) of az arguments"}
    if not tokens:
        return {"ok": False, "error": "empty az command"}
    if tokens[0] == "az":  # tolerate a leading 'az'
        tokens = tokens[1:]
    if not tokens:
        return {"ok": False, "error": "empty az command"}
    if tokens[0] in ("login", "logout"):
        return {"ok": False, "error": "use the azure_login skill for sign-in/out"}

    verb = _action_verb(tokens)
    mutating = verb not in _READ_VERBS
    # Ensure JSON output unless the caller already chose an output format.
    if not any(t in ("-o", "--output") or t.startswith("--output=") for t in tokens):
        tokens = tokens + ["-o", "json"]

    if mutating:
        # Preflight BEFORE the gate so a missing session doesn't burn the one-shot
        # human authorization (identical reasoning to terraform_apply).
        pre = _preflight()
        if not pre.get("ok"):
            return pre
        resource = _resource_group(tokens) or "azure"
        refused = infra_gate.guard(resource, f"az {verb}")
        if refused:
            return refused
    else:
        pre = _preflight()
        if not pre.get("ok"):
            return pre

    return _run_streamed([_az(), *tokens], f"az {verb or tokens[0]}")


# ── Skill registration ──────────────────────────────────────────────────────────
registry.skill("azure_login",
               "Start Azure device-code sign-in (`az login --use-device-code`). The "
               "verification URL + code stream to the event panel; complete it in a "
               "browser, then retry your Azure action.",
               {"type": "object", "properties": {}})(azure_login)
registry.skill("azure_whoami",
               "Show the current Azure identity + active subscription (no secrets).",
               {"type": "object", "properties": {}})(azure_whoami)
registry.skill("azure_account_list",
               "List the Azure subscriptions the current identity can access.",
               {"type": "object", "properties": {}})(azure_account_list)
registry.skill("azure_set_subscription",
               "Select the active Azure subscription (by id or name).",
               {"type": "object", "properties": {
                   "subscription": {"type": "string",
                                    "description": "subscription id or name"}},
                "required": ["subscription"]})(azure_set_subscription)
registry.skill("azure_cli",
               "Run an Azure CLI command. Pass `args` as a list of az arguments "
               "WITHOUT the leading 'az' (e.g. [\"group\",\"list\"] or "
               "[\"group\",\"create\",\"-n\",\"demo-rg\",\"-l\",\"southeastasia\"]). "
               "Read-only commands (show/list/get) run immediately; mutating commands "
               "(create/delete/deploy/update…) need typed human confirmation, so on a "
               "confirm_required refusal, summarize the change and ask the user to type "
               "the resource-group name (or 'azure') to confirm, then retry.",
               {"type": "object", "properties": {
                   "args": {"type": "array", "items": {"type": "string"},
                            "description": "az arguments without the leading 'az'"}},
                "required": ["args"]})(azure_cli)
