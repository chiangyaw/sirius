"""
CloudFormation deploy runner for the Sirius agents.

Purpose-built for the "onboard an AWS account into Cortex Cloud" flow, but usable
for any CloudFormation template. The cortex-mcp onboarding call
(`cortexmcp_cloud_onboard_aws_account`) returns a link to a CloudFormation trust
template (`reply.manual.CF`, a GCS-hosted URL). This module DEPLOYS that template
straight into the
target AWS account via `aws cloudformation` — the native AWS mechanism — so agents
never wrap the CFT in a Terraform `aws_cloudformation_stack` resource (which drags
in the `cortexcloud` provider and its unprovisioned CORTEXCLOUD_* secrets, the
failure seen in past onboarding attempts).

Skills:
  • cloudformation_deploy  — create a stack from a template URL (or inline body),
    wait for it to finish, then return status + outputs. Mutating: gated with a
    typed stack-name confirmation and event-streamed exactly like terraform apply.
  • cloudformation_status  — describe a stack (read-only, no gate) to verify.
  • cloudformation_delete  — delete a stack (gated) for teardown / cleanup.

Credential rules (identical to aws_cli / terraform_runner / aws_sso):
  • Credentials come from the process environment (and the aws CLI's own SSO
    cache) ONLY — never written to files, never placed on a command line, never
    echoed. AWS_SESSION_TOKEN is honored transparently.
  • Identity is confirmed via terraform_runner._preflight() (which prints identity,
    not secrets, and can kick off SSO login) before any mutation.
  • Commands are always argument LISTS (shell=False) — no shell interpolation.
"""

from __future__ import annotations

import os
import tempfile
import urllib.request
from typing import Any

from sirius import infra_gate
from sirius.aws_cli import _aws, _run_streamed
from sirius.events import emit
from sirius.skills import registry
from sirius.terraform_runner import _preflight  # AWS creds check + on-demand SSO login

# IAM-creating templates (the Cortex trust template creates named IAM roles) need
# these acknowledged; extra capabilities are harmless if the template doesn't use
# them, so we pass the full set by default and let the caller narrow it.
_DEFAULT_CAPABILITIES = ["CAPABILITY_NAMED_IAM", "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"]

# CloudFormation's --template-url ONLY accepts an Amazon S3 URL. Cortex serves its
# onboarding CF template from Google Cloud Storage (…storage.googleapis.com), so we
# download non-S3 URLs and pass the contents inline via --template-body.
_CF_INLINE_LIMIT = 51_200  # bytes: the aws CLI --template-body inline cap


def _is_s3_url(url: str) -> bool:
    host = (url.split("/")[2] if "://" in url else "").lower()
    return host.endswith("amazonaws.com") and ("s3" in host)


def _resolve_template_args(template_url: str, template_body: str) -> tuple[list[str], str, dict]:
    """Return (cli_args, tmp_path_to_clean, error). Produces `--template-url <s3>`
    for real S3 URLs, else downloads/writes the template and returns
    `--template-body file://<tmp>` so GCS-hosted (Cortex) templates deploy too."""
    if template_body:
        content = template_body.encode("utf-8")
    elif _is_s3_url(template_url):
        return (["--template-url", template_url], "", {})
    else:
        try:
            emit("aws_step", f"Downloading template {template_url.split('?')[0]}",
                 payload={"phase": "cloudformation template"})
            content = urllib.request.urlopen(template_url, timeout=60).read()  # noqa: S310
        except Exception as e:  # noqa: BLE001
            return ([], "", {"ok": False, "error": f"could not download template_url: {e}"})
    if len(content) > _CF_INLINE_LIMIT:
        return ([], "", {"ok": False, "error": (
            f"template is {len(content)} bytes (> {_CF_INLINE_LIMIT} inline limit); "
            "it must be uploaded to S3 and deployed with an S3 --template-url — not "
            "supported automatically here.")})
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="cf-template-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)
    return (["--template-body", f"file://{path}"], path, {})


def _parameters_args(parameters: Any) -> list[str]:
    """Turn a {key: value} dict (or a list of {ParameterKey,ParameterValue}) into
    the `--parameters ParameterKey=..,ParameterValue=.. …` argument list."""
    if not parameters:
        return []
    pairs: list[str] = []
    if isinstance(parameters, dict):
        items = parameters.items()
    elif isinstance(parameters, list):
        items = [(p.get("ParameterKey"), p.get("ParameterValue"))
                 for p in parameters if isinstance(p, dict)]
    else:
        return []
    for key, value in items:
        if key is None:
            continue
        pairs.append(f"ParameterKey={key},ParameterValue={value}")
    return ["--parameters", *pairs] if pairs else []


def cloudformation_deploy(stack_name: str = "", template_url: str = "",
                          template_body: str = "", region: str = "",
                          parameters: Any = None, capabilities: Any = None,
                          **_: Any) -> dict:
    """Deploy a CloudFormation stack into the current AWS account and wait for it to
    finish. Provide `template_url` (an S3/HTTPS link to the template — e.g. the
    `manual.CF` link returned by cortex-mcp onboarding) OR an inline `template_body`.
    Non-S3 URLs (e.g. the GCS-hosted Cortex template) are downloaded and deployed
    inline automatically. Mutating: requires typed human confirmation of the stack
    name (like terraform apply)."""
    stack_name = (stack_name or "").strip()
    if not stack_name:
        return {"ok": False, "error": "stack_name is required"}
    if not template_url and not template_body:
        return {"ok": False, "error": "provide either template_url or template_body"}
    if template_url and template_body:
        return {"ok": False, "error": "provide only one of template_url / template_body"}
    region = (region or "").strip()
    if not region:
        return {"ok": False, "error": "region is required (pass an explicit AWS region)"}

    caps = capabilities if isinstance(capabilities, list) and capabilities else _DEFAULT_CAPABILITIES

    # Preflight BEFORE the gate (a missing session must not burn the confirmation).
    pre = _preflight()
    if not pre.get("ok"):
        return pre

    # Human confirmation for a real, IAM-creating infra change — type the stack name.
    refused = infra_gate.guard(stack_name, "apply")
    if refused:
        return refused

    # Resolve the template into CLI args. CloudFormation --template-url only accepts
    # S3 URLs; Cortex serves its CF template from GCS, so this downloads it and
    # passes it inline via --template-body (cleaned up in finally).
    tmpl_args, tmpl_tmp, tmpl_err = _resolve_template_args(template_url, template_body)
    if tmpl_err:
        return tmpl_err
    try:
        emit("aws_step", f"Deploying CloudFormation stack {stack_name} in {region}",
             payload={"phase": f"cloudformation deploy {stack_name}", "stack": stack_name,
                      "region": region})

        create = [_aws(), "cloudformation", "create-stack",
                  "--stack-name", stack_name,
                  "--region", region,
                  "--capabilities", *caps,
                  "--output", "json",
                  *tmpl_args]
        create += _parameters_args(parameters)

        res = _run_streamed(create, f"cloudformation create-stack {stack_name}")
    finally:
        if tmpl_tmp:
            try:
                os.unlink(tmpl_tmp)
            except OSError:
                pass
    if not res.get("ok"):
        out = res.get("output", "")
        if "AlreadyExistsException" in out or "already exists" in out.lower():
            return {"ok": False, "error": (
                f"Stack {stack_name!r} already exists. Verify it with "
                f"cloudformation_status, then either delete it with "
                f"cloudformation_delete and redeploy, or use a distinct stack name."),
                "output": out}
        return res

    # Block until the stack finishes creating, then report status + outputs.
    wait = [_aws(), "cloudformation", "wait", "stack-create-complete",
            "--stack-name", stack_name, "--region", region]
    wres = _run_streamed(wait, f"cloudformation wait {stack_name}")

    describe = [_aws(), "cloudformation", "describe-stacks",
                "--stack-name", stack_name, "--region", region, "--output", "json"]
    dres = _run_streamed(describe, f"cloudformation describe {stack_name}")

    ok = wres.get("ok", False) and dres.get("ok", False)
    emit("aws_step",
         f"CloudFormation stack {stack_name} {'ready' if ok else 'did not reach CREATE_COMPLETE'}",
         severity="success" if ok else "danger",
         payload={"phase": f"cloudformation deploy {stack_name}", "stack": stack_name,
                  "region": region, "complete": ok})
    return {"ok": ok, "stack_name": stack_name, "region": region,
            "status_output": dres.get("output", ""),
            "hint": ("Onboarding trust stack deployed. Verify the Cortex Cloud instance "
                     "left PENDING with cortexmcp_cloud_get_instances.")}


def cloudformation_status(stack_name: str = "", region: str = "", **_: Any) -> dict:
    """Describe a CloudFormation stack (status, outputs). Read-only — runs
    immediately, no confirmation. Use to verify a deploy or check drift."""
    stack_name = (stack_name or "").strip()
    region = (region or "").strip()
    if not stack_name:
        return {"ok": False, "error": "stack_name is required"}
    if not region:
        return {"ok": False, "error": "region is required"}
    pre = _preflight()
    if not pre.get("ok"):
        return pre
    describe = [_aws(), "cloudformation", "describe-stacks",
                "--stack-name", stack_name, "--region", region, "--output", "json"]
    return _run_streamed(describe, f"cloudformation describe {stack_name}")


def cloudformation_delete(stack_name: str = "", region: str = "", **_: Any) -> dict:
    """Delete a CloudFormation stack and wait for it to be removed. Mutating:
    requires typed human confirmation of the stack name (like terraform destroy)."""
    stack_name = (stack_name or "").strip()
    region = (region or "").strip()
    if not stack_name:
        return {"ok": False, "error": "stack_name is required"}
    if not region:
        return {"ok": False, "error": "region is required"}
    pre = _preflight()
    if not pre.get("ok"):
        return pre
    refused = infra_gate.guard(stack_name, "destroy")
    if refused:
        return refused
    delete = [_aws(), "cloudformation", "delete-stack",
              "--stack-name", stack_name, "--region", region]
    res = _run_streamed(delete, f"cloudformation delete-stack {stack_name}")
    if not res.get("ok"):
        return res
    wait = [_aws(), "cloudformation", "wait", "stack-delete-complete",
            "--stack-name", stack_name, "--region", region]
    wres = _run_streamed(wait, f"cloudformation wait-delete {stack_name}")
    return {"ok": wres.get("ok", False), "stack_name": stack_name, "region": region}


# ── Skill registration ──────────────────────────────────────────────────────────
registry.skill(
    "cloudformation_deploy",
    "Deploy a CloudFormation stack into the current AWS account and wait for it to "
    "finish. This is the correct way to apply the trust template returned by Cortex "
    "Cloud onboarding: pass `template_url` = the `manual.CF` link from "
    "cortexmcp_cloud_onboard_aws_account, a `stack_name` (e.g. "
    "'cortex-cloud-onboarding-<account-id>'), and an explicit `region`. Do NOT wrap "
    "the CFT in Terraform. Mutating (creates IAM roles): on a confirm_required "
    "refusal, summarize exactly what the stack creates and ask the user to type the "
    "stack name to confirm, then retry. Credentials come from the environment only.",
    {"type": "object", "properties": {
        "stack_name": {"type": "string", "description": "CloudFormation stack name"},
        "template_url": {"type": "string",
                         "description": "S3/HTTPS URL of the template (e.g. manual.CF); "
                                        "non-S3 URLs are downloaded and deployed inline"},
        "template_body": {"type": "string",
                          "description": "Inline template YAML/JSON (alternative to template_url)"},
        "region": {"type": "string", "description": "AWS region, e.g. ap-southeast-1"},
        "parameters": {"type": "object",
                       "description": "Stack parameters as a {key: value} map"},
        "capabilities": {"type": "array", "items": {"type": "string"},
                         "description": "Override IAM capabilities (defaults cover named IAM)"}},
     "required": ["stack_name", "region"]})(cloudformation_deploy)

registry.skill(
    "cloudformation_status",
    "Describe a CloudFormation stack (status + outputs). Read-only — runs immediately. "
    "Use to verify an onboarding deploy or inspect an existing stack.",
    {"type": "object", "properties": {
        "stack_name": {"type": "string"},
        "region": {"type": "string", "description": "AWS region"}},
     "required": ["stack_name", "region"]})(cloudformation_status)

registry.skill(
    "cloudformation_delete",
    "Delete a CloudFormation stack and wait for removal. Mutating: on a "
    "confirm_required refusal, summarize what will be removed and ask the user to "
    "type the stack name to confirm, then retry. Use for onboarding teardown/cleanup.",
    {"type": "object", "properties": {
        "stack_name": {"type": "string"},
        "region": {"type": "string", "description": "AWS region"}},
     "required": ["stack_name", "region"]})(cloudformation_delete)
