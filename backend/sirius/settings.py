"""
Web-UI settings — AWS credential management (Feature: Settings modal).

Lets the single local user supply AWS credentials from the web UI instead of the
launching shell, stored as masked secrets. Two mutually-exclusive methods:

  • Access keys — long-lived IAM keys pasted once (persisted, never expire).
  • SSO login   — `aws sso login` (see aws_sso.py), best for temporary creds.

Secrets are written ONLY to .env (consistent with the project rule) AND injected
into the running process's os.environ, so terraform (which does os.environ.copy()
per subprocess) picks them up immediately — no restart. Values are never logged,
emitted, or returned by the status API (only booleans + a last-4 preview).

Scope note: this manages AWS_* only. Other secrets (Cortex/AIRS) are read into
clients at startup, so changing them live would need a restart — out of scope here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import dotenv_values, set_key, unset_key

from sirius import aws_sso

_log = logging.getLogger("sirius.settings")

_AWS_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION")


def _dotenv_path() -> Path:
    """The .env this app loads (backend root), overridable for tests."""
    override = os.environ.get("SIRIUS_DOTENV_PATH")
    if override:
        return Path(override).expanduser()
    # Mirror config._default_config_path(): .env sits at the backend root.
    return Path(__file__).resolve().parent.parent / ".env"


def _write_env(key: str, value: str) -> None:
    path = _dotenv_path()
    if not path.exists():
        path.touch(mode=0o600)
    set_key(str(path), key, value)
    os.chmod(path, 0o600)  # keep secrets owner-only


def _remove_env(key: str) -> None:
    path = _dotenv_path()
    # Only unset if present, so python-dotenv doesn't warn about a missing key.
    if path.exists() and key in dotenv_values(str(path)):
        unset_key(str(path), key)
    os.environ.pop(key, None)


def aws_status() -> dict[str, Any]:
    """Masked AWS credential status — NEVER returns secret values."""
    access = os.environ.get("AWS_ACCESS_KEY_ID", "")
    profile = os.environ.get("AWS_PROFILE", "")
    if access:
        method = "keys"
    elif profile:
        method = "sso"
    else:
        method = "none"

    ident = aws_sso.status(profile or None)
    return {
        "method": method,
        "access_key_last4": access[-4:] if access else None,
        "has_session_token": bool(os.environ.get("AWS_SESSION_TOKEN")),
        "region": os.environ.get("AWS_REGION", ""),
        "sso_profile": profile or None,
        "authenticated": ident.get("authenticated", False),
        "account": ident.get("account"),
        "arn": ident.get("arn"),
    }


def save_aws_keys(access_key_id: str, secret_access_key: str,
                  session_token: str = "", region: str = "") -> dict[str, Any]:
    """Persist pasted AWS keys to .env + live env; clear any SSO profile.

    Access-key env vars take precedence in the AWS credential chain, but we also
    drop AWS_PROFILE so the two methods stay unambiguous (mutually exclusive).
    """
    access_key_id = (access_key_id or "").strip()
    secret_access_key = (secret_access_key or "").strip()
    if not access_key_id or not secret_access_key:
        return {"ok": False, "error": "access_key_id and secret_access_key are required"}

    _write_env("AWS_ACCESS_KEY_ID", access_key_id)
    _write_env("AWS_SECRET_ACCESS_KEY", secret_access_key)
    os.environ["AWS_ACCESS_KEY_ID"] = access_key_id
    os.environ["AWS_SECRET_ACCESS_KEY"] = secret_access_key

    for name, val in (("AWS_SESSION_TOKEN", session_token.strip() if session_token else ""),
                      ("AWS_REGION", region.strip() if region else "")):
        if val:
            _write_env(name, val)
            os.environ[name] = val
        else:
            _remove_env(name)

    # Switch method to keys: drop the SSO profile from .env + env.
    _remove_env("AWS_PROFILE")

    _log.info("AWS access keys saved via settings (id …%s)", access_key_id[-4:])
    return {"ok": True, **aws_status()}


def clear_aws() -> dict[str, Any]:
    """Remove all AWS credential env vars from .env + the running process."""
    for name in (*_AWS_KEYS, "AWS_PROFILE"):
        _remove_env(name)
    _log.info("AWS credentials cleared via settings")
    return {"ok": True, **aws_status()}


def clear_aws_keys_for_sso() -> None:
    """Drop pasted access keys so an SSO profile takes effect (called on SSO login)."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        _remove_env(name)
