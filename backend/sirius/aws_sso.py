"""
Web-UI-driven AWS SSO login for the Terraform agent.

Sirius runs locally for a single user. Rather than making the operator run
`aws sso login` in the launching shell *before* `sirius serve` (and restart the
server whenever the SSO token expires), this module drives `aws sso login` from
the running backend:

  • ensure_profile() writes a NON-SECRET profile into ~/.aws/config from config.yaml.
  • begin_login() shells out to `aws sso login`, streams the verification URL to the
    UI as `aws_sso` events, and sets os.environ["AWS_PROFILE"] IN THE RUNNING
    PROCESS — so every later terraform subprocess (which does os.environ.copy())
    inherits it with no restart. Re-auth on expiry is just another click.
  • status() reports the current identity via `aws sts get-caller-identity`.

Credential rules (same as terraform_runner): the only thing written is the
non-secret ~/.aws/config profile. The SSO token itself is minted by the aws CLI
into ~/.aws/sso/cache/; Sirius never reads, writes, echoes, or emits it.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from sirius.config import AWSConfig, load_config
from sirius.events import current_session, emit

_log = logging.getLogger("sirius.aws_sso")

# Only one interactive login at a time.
_login_lock = threading.Lock()
_login_in_progress = False

# Matches the verification URL and the "XXXX-XXXX" user code aws prints.
_URL_RE = re.compile(r"https://\S+")
_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")


def _aws() -> str:
    return shutil.which("aws") or "aws"


def _aws_config_path() -> Path:
    """Honor AWS_CONFIG_FILE, else the standard ~/.aws/config."""
    override = os.environ.get("AWS_CONFIG_FILE")
    return Path(override).expanduser() if override else Path.home() / ".aws" / "config"


def ensure_profile(cfg: AWSConfig) -> None:
    """Create/update the ~/.aws/config profile from (non-secret) config.yaml settings.

    Idempotent and surgical: only the one `[profile <name>]` section is touched;
    any other profiles/sections are preserved verbatim.
    """
    path = _aws_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)

    # aws CLI names non-default profiles "[profile NAME]" in the config file.
    section = "default" if cfg.profile == "default" else f"profile {cfg.profile}"
    if not parser.has_section(section):
        parser.add_section(section)

    parser[section]["sso_start_url"] = cfg.sso_start_url
    parser[section]["sso_region"] = cfg.sso_region or cfg.region
    parser[section]["sso_account_id"] = cfg.account_id
    parser[section]["sso_role_name"] = cfg.role_name
    parser[section]["region"] = cfg.region or cfg.sso_region
    parser[section]["output"] = "json"

    with open(path, "w", encoding="utf-8") as f:
        parser.write(f)
    _log.info("ensured aws profile %r in %s", cfg.profile, path)


def status(profile: str | None = None) -> dict[str, Any]:
    """Report the current AWS identity WITHOUT printing any secret."""
    aws = shutil.which("aws")
    if not aws:
        return {"authenticated": False, "error": "aws CLI not installed"}
    env = os.environ.copy()
    if profile:
        env["AWS_PROFILE"] = profile
    try:
        out = subprocess.run(
            [aws, "sts", "get-caller-identity", "--output", "json"],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"authenticated": False, "error": "get-caller-identity timed out"}
    if out.returncode != 0:
        return {"authenticated": False, "error": (out.stderr or "").strip()[:300]}
    ident = json.loads(out.stdout or "{}")
    return {"authenticated": True, "account": ident.get("Account"),
            "arn": ident.get("Arn"), "profile": env.get("AWS_PROFILE")}


def _run_login(session_id: str, cfg: AWSConfig) -> None:
    """Blocking `aws sso login` worker — runs in a background thread."""
    global _login_in_progress
    aws = _aws()
    cmd = [aws, "sso", "login", "--profile", cfg.profile]
    emit("aws_sso", "Starting AWS SSO login…", session_id=session_id,
         payload={"profile": cfg.profile})
    url_seen = False
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=os.environ.copy(),
        )
    except FileNotFoundError:
        emit("aws_sso", "aws CLI not found on PATH", severity="danger",
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
            if url_match and not url_seen:
                url_seen = True
                emit("aws_sso", "Open this URL to sign in to AWS",
                     severity="warn", session_id=session_id,
                     payload={"url": url_match.group(0),
                              "user_code": code_match.group(0) if code_match else None})
            else:
                emit("aws_sso", line, session_id=session_id)
        code = proc.wait()
    finally:
        with _login_lock:
            _login_in_progress = False

    if code == 0:
        st = status(cfg.profile)
        if st.get("authenticated"):
            emit("aws_sso",
                 f"AWS SSO login complete (account {st.get('account')})",
                 severity="success", session_id=session_id,
                 payload={"account": st.get("account"), "arn": st.get("arn"),
                          "profile": cfg.profile})
        else:
            emit("aws_sso", "Login finished but identity check failed",
                 severity="danger", session_id=session_id,
                 payload={"error": st.get("error")})
    else:
        emit("aws_sso", f"AWS SSO login failed (exit {code})",
             severity="danger", session_id=session_id, payload={"exit_code": code})


def begin_login(session_id: str, cfg: AWSConfig) -> dict[str, Any]:
    """Ensure the profile exists, set AWS_PROFILE for the running process, and
    kick off `aws sso login` in the background. Returns immediately."""
    global _login_in_progress
    if not cfg.configured():
        return {"ok": False, "error": "aws SSO not configured in config.yaml "
                "(need sso_start_url, account_id, role_name)"}

    with _login_lock:
        if _login_in_progress:
            return {"ok": False, "error": "an AWS SSO login is already in progress"}
        _login_in_progress = True

    try:
        ensure_profile(cfg)
    except Exception as e:  # noqa: BLE001 — surface config-write failures to the UI
        with _login_lock:
            _login_in_progress = False
        return {"ok": False, "error": f"could not write ~/.aws/config: {e}"}

    # Make every future terraform subprocess (os.environ.copy()) use this profile,
    # WITHOUT restarting the server — this is the whole point of the flow.
    os.environ["AWS_PROFILE"] = cfg.profile

    threading.Thread(target=_run_login, args=(session_id, cfg), daemon=True).start()
    return {"ok": True, "started": True, "profile": cfg.profile}


def start_login_for_current_session() -> dict[str, Any]:
    """Kick off SSO login for the session the current skill is serving.

    Used by the terraform preflight so a `terraform apply` prompt can start the
    login on demand (the sign-in link streams to the UI). Reads the AWS config and
    the active session from the contextvars set by the agent loop.
    """
    sid = current_session.get()
    if not sid:
        return {"ok": False, "error": "no active session for AWS SSO login"}
    return begin_login(sid, load_config().aws)
