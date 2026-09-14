"""
AWS CLI integration for the Sirius agents.

The AWS sibling to `azure_cli.py`. `terraform_runner.py` already gives the agents
Terraform-over-AWS and `aws_whoami`, and `aws_sso.py` handles sign-in, but there
was no general `aws ...` reach. This module adds one so an agent can inspect and,
when needed, directly fix AWS resources that live OUTSIDE any terraform state —
e.g. verifying and cleaning up resources orphaned when a project's state was lost.

  • aws_cli — a general `aws ...` runner. Read-only operations (describe-*/list-*/
    get-*/…) run freely so the agent can VERIFY what exists; mutating operations
    (delete-*/create-*/terminate-*/…) are gated exactly like terraform apply
    (config flag + typed human confirmation). The confirmation is STICKY: one
    typed "aws" covers the rest of the session, so cleaning up a batch of
    resources doesn't prompt on every call.

Credential rules (same as terraform_runner / aws_sso / azure_cli):
  • Credentials come from the process environment (and the aws CLI's own SSO
    cache) ONLY — never written to files by us, never placed on a command line,
    never echoed. AWS_SESSION_TOKEN is honored transparently.
  • Identity is confirmed via `aws sts get-caller-identity` (prints identity, not
    secrets) before any mutation, reusing terraform_runner's preflight (which can
    kick off SSO login on demand).
  • Commands are always argument LISTS (shell=False) — no shell interpolation.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import Any

from sirius import infra_gate
from sirius.events import emit
from sirius.skills import registry
from sirius.terraform_runner import _preflight  # AWS creds check + on-demand SSO login

# AWS operations that only READ. AWS is consistent: read ops almost always start
# with one of these verb prefixes. Anything not matched is treated as MUTATING and
# must pass the infra gate (safe default — unknown ops require confirmation).
_READ_PREFIXES = (
    "describe-", "list-", "get-", "lookup-", "search-", "batch-get-", "head-",
    "view-", "preview-", "estimate-", "check-",
)
_READ_EXACT = {
    "ls", "help", "wait", "search", "scan", "query", "select",
    "get-caller-identity", "filter-log-events", "test-event-pattern",
}


def _aws() -> str:
    return shutil.which("aws") or "aws"


def _operation(tokens: list[str]) -> str:
    """The AWS operation token — the SECOND non-flag token (`aws <service> <op> …`).

    Falls back to the first non-flag token for single-word forms (e.g. `s3 ls`).
    Flags and their values are skipped so `--region us-west-2 ec2 describe-vpcs`
    still resolves to `describe-vpcs`.
    """
    positionals: list[str] = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            if "=" not in t:
                skip_next = True  # a flag that likely takes a value
            continue
        positionals.append(t)
    if len(positionals) >= 2:
        return positionals[1]
    return positionals[0] if positionals else ""


def _is_read(op: str) -> bool:
    op = op.lower()
    return op in _READ_EXACT or op.startswith(_READ_PREFIXES)


def aws_cli(args: Any = None, **_: Any) -> dict:
    """Run a general `aws` command. `args` is a list of arguments WITHOUT the leading
    `aws` (a string is accepted and shlex-split). Read-only operations run freely;
    mutating operations are gated (typed human confirmation, sticky for the session)
    like terraform apply."""
    if isinstance(args, str):
        tokens = shlex.split(args)
    elif isinstance(args, list):
        tokens = [str(a) for a in args]
    else:
        return {"ok": False, "error": "args must be a list (or string) of aws arguments"}
    if not tokens:
        return {"ok": False, "error": "empty aws command"}
    if tokens[0] == "aws":  # tolerate a leading 'aws'
        tokens = tokens[1:]
    if not tokens:
        return {"ok": False, "error": "empty aws command"}
    if tokens[0] in ("configure", "sso"):
        return {"ok": False, "error": "use the AWS sign-in flow (aws_whoami / SSO login), not aws configure/sso here"}

    service = next((t for t in tokens if not t.startswith("-")), "")
    op = _operation(tokens)
    mutating = not _is_read(op)
    # Ensure JSON output unless the caller already chose one.
    if not any(t in ("--output",) or t.startswith("--output=") for t in tokens):
        tokens = tokens + ["--output", "json"]

    # Preflight BEFORE the gate (a missing session must not burn the confirmation).
    pre = _preflight()
    if not pre.get("ok"):
        return pre

    if mutating:
        # One typed "aws" confirmation covers the session (sticky) so batch cleanup
        # doesn't prompt on every delete. Scope is still enforced per-agent.
        refused = infra_gate.guard("aws", f"aws {service} {op}".strip(), sticky=True)
        if refused:
            return refused

    return _run_streamed([_aws(), *tokens], f"aws {service} {op}".strip())


def _run_streamed(cmd: list[str], title: str) -> dict:
    """Run an aws command, emitting each stdout line as an aws_step event."""
    emit("aws_step", f"$ {' '.join(cmd)}", payload={"cmd": cmd, "phase": title})
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=os.environ.copy(),  # inherits AWS_* + SSO cache
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
        sev = "danger" if ("error" in low or "denied" in low or "failed" in low) else "info"
        emit("aws_step", line, severity=sev, payload={"phase": title})
    code = proc.wait()
    ok = code == 0
    emit("aws_step", f"{title} exited with code {code}",
         severity="success" if ok else "danger",
         payload={"phase": title, "exit_code": code})
    return {"ok": ok, "exit_code": code, "output": "\n".join(lines[-120:])}


# ── Skill registration ──────────────────────────────────────────────────────────
registry.skill("aws_cli",
               "Run an AWS CLI command. Pass `args` as a list of aws arguments WITHOUT "
               "the leading 'aws' (e.g. [\"ec2\",\"describe-vpcs\",\"--region\",\"us-west-2\"] "
               "or [\"iam\",\"delete-role\",\"--role-name\",\"foo\"]). Read-only operations "
               "(describe-/list-/get-) run immediately — use them to inspect or verify AWS "
               "resources, including ones NOT managed by terraform. Mutating operations "
               "(delete-/create-/terminate-/detach-…) need typed human confirmation: on a "
               "confirm_required refusal, summarize exactly what will change and ask the user "
               "to type 'aws' to confirm, then retry. That one confirmation is sticky for the "
               "session, so a batch of deletes won't re-prompt. Always pass an explicit "
               "--region. Credentials come from the environment only.",
               {"type": "object", "properties": {
                   "args": {"type": "array", "items": {"type": "string"},
                            "description": "aws arguments without the leading 'aws'"}},
                "required": ["args"]})(aws_cli)
