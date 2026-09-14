"""
Sirius web backend — FastAPI.

  • WebSocket /ws/events?session_id=...  → live event stream (the right panel)
  • POST /api/chat                       → run an agent turn (events stream via WS)
  • GET  /api/bootstrap                  → agents, scenarios, config for the UI
  • POST /api/purpleteam/authorize       → human authorizes an intrusive target
  • GET  /health

Importing this module registers every feature's skills (scenarios, terraform,
purple team, cortex) into the shared registry.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sirius import agent as agent_mod
from sirius import audit as audit_mod
from sirius import aws_sso
from sirius import settings as settings_mod
from sirius import vertex as vertex_mod
from sirius.agent import Agent, run_turn
from sirius.airs import PrismaAIRS
from sirius.config import load_config
from sirius.events import bus
from sirius.llm import get_backend

# Import feature packages for their side effect: registering skills.
from sirius import scenarios  # noqa: E402,F401
from sirius import terraform_runner  # noqa: E402,F401
from sirius import azure_cli  # noqa: E402,F401  (registers azure_* skills)
from sirius import aws_cli  # noqa: E402,F401  (registers aws_cli passthrough skill)
from sirius import cloudformation_runner  # noqa: E402,F401  (registers cloudformation_* skills)
from sirius import purpleteam  # noqa: E402,F401
from sirius import cortex  # noqa: E402,F401
from sirius import k8s_skills  # noqa: E402,F401  (registers k8s_* native skills)
from sirius import web_skills  # noqa: E402,F401  (registers web_fetch skill)
from sirius import browser_skills  # noqa: E402,F401  (registers browser_* Playwright skills)
from sirius import reports as reports_mod  # noqa: E402  (registers generate_report skill)
from sirius import engineer  # noqa: E402,F401  (registers engineer_* content-engineering skills)
from sirius import infra_gate
from sirius import cortex_mcp_client  # noqa: E402  (Sirius as MCP client for cortex-mcp)

# Discover + register the external cortex-mcp tools as cortexmcp_* skills (over the
# real MCP protocol). Empty if the server is disabled/unavailable — Defender then
# falls back to the native cortex_* skills.
_CORTEXMCP_SKILLS = cortex_mcp_client.init()

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("sirius.web")

# ── App state ──────────────────────────────────────────────────────────────────
_cfg = load_config()
_llm = get_backend(_cfg)
_airs = PrismaAIRS(_cfg.airs)


def rebuild_llm() -> str:
    """Rebuild the LLM backend from current config + environment, with no restart.

    Used after a live Vertex project switch: `vertex.set_project()` writes the new
    SIRIUS_VERTEX_PROJECT into os.environ, then this reloads config and rebuilds
    `_llm`. `chat()` reads the module-global `_llm` at call time, so the reassignment
    takes effect on the next turn immediately. Platform-agent `.model` ids are also
    refreshed (they track config, though a project switch alone doesn't change them).
    Returns the resulting backend model id (e.g. "echo" if Vertex still can't init).
    """
    global _cfg, _llm
    _cfg = load_config()
    _llm = get_backend(_cfg)
    for _a in _PLATFORM_AGENTS:
        _a.model = _cfg.llm.model
    _log.info("LLM backend rebuilt (model=%s project=%s region=%s)",
              _llm.model, _cfg.vertex_project(), _cfg.vertex_region())
    return _llm.model

# ── Platform agents ──────────────────────────────────────────────────────────
# Sirius is the *platform*; these are distinct agents *within* it. Each has its
# own persona/profile and an isolated skill set (an agent can only call the tools
# in its own skill_names — enforced in agent.run_turn). Their friendly names are
# in the personas below; rename freely.

# Shared reminder for any agent that can install the Cortex k8s connector.
_KONNECTOR_NOTE = (
    "To install the Cortex agent / k8s connector onto a Kubernetes (EKS) cluster, "
    "always use cortex_deploy_k8s_konnector: it installs from the downloaded "
    "security-profile pair (auth.json + values.yaml) in cortex-profiles/ via Helm "
    "and needs NO Cortex distribution API. Do not use the Cortex distribution API "
    "for k8s installs — it is not required and may be unavailable; if it errors, "
    "fall straight back to cortex_deploy_k8s_konnector."
)

# Reminder for building the Windows/macOS/Linux endpoint agent installer.
_ENDPOINT_INSTALLER_NOTE = (
    "To get a Cortex agent installer to put on a target machine (e.g. a Windows MSI), "
    "use cortex_download_distribution: it creates the installation package and "
    "downloads it in one call. Pass name + platform + agent_version to create a new "
    "package (list versions with cortex_get_distribution_versions; package_type is "
    "x64/x86 for a Windows MSI), or pass an existing distribution_id to just download. "
    "Creating a package with the same name each time makes a duplicate — reuse the "
    "distribution_id to re-download. This is the endpoint-agent path; it is separate "
    "from the Kubernetes konnector above."
)

_CORTEX_SKILLS = [
    "cortex_test_connection", "cortex_get_incidents", "cortex_get_alerts",
    "cortex_get_endpoints", "cortex_k8s_incidents", "cortex_k8s_attack_report",
    "cortex_isolate_endpoint", "cortex_unisolate_endpoint", "cortex_scan_endpoint",
    "cortex_xql_query", "cortex_generate_k8s_deployment",
    # k8s connector install: profile-based konnector path ONLY. The legacy
    # cortex_deploy_k8s_connector + cortex_create_distribution are deliberately
    # omitted — for k8s installs they depend on the distribution API and led the
    # agent into a dead end instead of the working konnector path.
    "cortex_deploy_k8s_konnector",
    "cortex_get_distribution_versions",
    # Endpoint installers (e.g. Windows MSI): create+poll+download in one call via
    # the verified distributions/create endpoint. This is the ENDPOINT-agent path,
    # separate from the k8s konnector above.
    "cortex_download_distribution",
    "cortex_attack_simulation",
]
# CLOUD bundle = the native, event-emitting terraform_runner skills (aws + IaC).
# (The sirius-cloud MCP duplicates these; the native ones stream events + record
# inventory, so we use them and leave the MCP server for external clients.)
_CLOUD_SKILLS = [
    "aws_whoami", "aws_cli", "terraform_list_projects", "terraform_plan",
    "terraform_apply", "terraform_destroy",
    "terraform_status", "terraform_state_list", "terraform_history",
    "terraform_write_project", "terraform_remove_project",
    # CloudFormation: apply a CFT straight into AWS (e.g. the Cortex onboarding
    # trust template) without going through Terraform.
    "cloudformation_deploy", "cloudformation_status", "cloudformation_delete",
]
# AZURE bundle = native, event-emitting `az` skills (device-code login + auth,
# subscription selection, and a gated general `az` passthrough for deploys).
# Terraform against Azure (azurerm) already works through the terraform_* skills
# once `az` is authenticated — this bundle provides that auth plus direct az reach.
_AZURE_SKILLS = [
    "azure_login", "azure_whoami", "azure_account_list",
    "azure_set_subscription", "azure_cli",
]
# K8S bundle = native wrappers around the sirius-k8s runner (helm/kubectl/…).
_K8S_SKILLS = [
    "k8s_update_kubeconfig", "k8s_helm", "k8s_kubectl",
    "k8s_apply_manifest", "k8s_helm_install",
]
_PURPLE_SKILLS = ["purpleteam_recon", "purpleteam_intrusive_scan"]
# WEB bundle = curl-equivalent URL fetch (raw manifests, API responses, docs).
# Granted to every agent so any persona can pull a URL into its turn.
_WEB_SKILLS = ["web_fetch"]
# BROWSER bundle = headless Chromium (Playwright) driving: navigate/click/type/
# snapshot/screenshot/close. Gated by browser.enabled + browser.allowed_hosts.
_BROWSER_SKILLS = [
    "browser_navigate", "browser_click", "browser_type",
    "browser_snapshot", "browser_screenshot", "browser_close",
]
# REPORT bundle = generate a downloadable Markdown findings report. Granted to
# every platform agent so any persona can write up its findings for download.
_REPORT_SKILLS = ["generate_report"]
# ENGINEER bundle = build/deploy/delete Cortex XSIAM content (playbooks + scripts)
# as code with demisto-sdk. This is the SOAR content-developer capability.
_ENGINEER_SKILLS = [
    "engineer_status", "engineer_list_content", "engineer_scaffold_playbook",
    "engineer_write_content", "engineer_validate", "engineer_deploy",
    "engineer_list_playbooks", "engineer_delete_playbook", "engineer_delete_script",
]

_INFRA_NOTE = (
    "Actions that change REAL infrastructure — terraform apply and destroy, and "
    "helm/kubectl mutations — need human confirmation; authoring/removing local "
    "project files and running init/plan do NOT. When one of those actions is "
    "refused with confirm_required, DON'T retry: summarize in plain language exactly "
    "what will change, then ask the user to type the exact resource name to confirm. "
    "The user types it in the normal chat box and the UI records the authorization; "
    "you then simply run the action again. Never try to bypass the gate or "
    "self-confirm — only the human typing the exact name authorizes it."
)

# Shared truthfulness guardrail. Terra (provisioner) already carries an inline
# version of this for terraform; Nova and Defender need it too — without it they
# have fabricated success and even invented command output (e.g. a made-up
# `describe-stacks` result reporting CREATE_COMPLETE for a stack that was never
# created). Applies to every mutating/verifying tool, not just terraform.
_TRUTHFUL_NOTE = (
    "REPORT ONLY WHAT THE TOOLS ACTUALLY RETURN. Never say an action succeeded — and "
    "never state that infrastructure was created, changed, deleted, or onboarded — "
    "unless a tool call you made in THIS turn returned ok:true; quote the real values "
    "it returned. If a tool returns ok:false, refused, needs_auth, or confirm_required, "
    "tell the user plainly it did NOT succeed and show the actual error. NEVER invent or "
    "paste command/API output, JSON, resource IDs, ARNs, stack IDs, statuses (e.g. "
    "CREATE_COMPLETE), timestamps, or resource counts: every value you show the user must "
    "come verbatim from a real tool result. If you did not actually run a tool, say so — "
    "do not describe what it 'would' return as if it had run. To confirm something exists, "
    "call a read-only tool (aws_cli describe-/list-/get-, cloudformation_status, "
    "cortexmcp_cloud_get_instances) and report only its actual output."
)

# Shared reminder for any agent that can reach Azure via the `az` CLI.
_AZURE_NOTE = (
    "For Azure work (onboarding, demos, deploys): sign in with azure_login "
    "(device-code — the link and code stream to the event panel), check identity "
    "with azure_whoami, and pick a subscription with azure_set_subscription / "
    "azure_account_list. Deploy either with azure_cli (pass args as a list WITHOUT "
    "the leading 'az', e.g. [\"group\",\"create\",\"-n\",\"demo-rg\",\"-l\",\"southeastasia\"]) "
    "or with Terraform — the azurerm provider uses your az session automatically. "
    "Read-only az commands (show/list/get) run immediately; mutating ones "
    "(create/delete/deploy…) are gated exactly like terraform apply — on a "
    "confirm_required refusal, summarize the change and ask the user to type the "
    "resource-group name (or 'azure') to confirm, then retry."
)

# Shared reminder for any agent that can reach AWS directly via the `aws` CLI.
_AWS_CLI_NOTE = (
    "For direct AWS work OUTSIDE terraform state, use aws_cli (pass args as a list "
    "WITHOUT the leading 'aws', and always include an explicit --region, e.g. "
    "[\"ec2\",\"describe-vpcs\",\"--region\",\"us-west-2\"]). Read-only operations "
    "(describe-/list-/get-) run immediately — use them to VERIFY what actually exists "
    "before acting. This is how you inspect or clean up resources that are NOT in any "
    "terraform state (e.g. orphaned when a project's state was lost): confirm they exist "
    "with describe/list, then delete them in dependency order. Mutating operations "
    "(delete-/create-/terminate-/detach-…) are gated: on a confirm_required refusal, "
    "summarize exactly what will change and ask the user to type 'aws' to confirm — that "
    "one confirmation is sticky for the session, so a batch of deletes won't re-prompt. "
    "Never accept pasted credentials; they come from the environment only."
)

# Shared reminder: a NEW build must start from CLEAN terraform state. A directory
# that was applied before may carry state bound to a DIFFERENT AWS account, which
# corrupts plan/apply with phantom or cross-account resources.
_NEW_BUILD_NOTE = (
    "NEW BUILDS START WITH CLEAN STATE. When the user asks you to build or deploy NEW "
    "infrastructure, do NOT author into or re-apply an existing project directory that "
    "may already hold terraform state from a prior apply — that state can belong to a "
    "DIFFERENT AWS account and will corrupt plan/apply with phantom or cross-account "
    "resources. First run terraform_list_projects (and terraform_state_list if unsure "
    "whether a same-named project has state); if a project with the name you'd use "
    "already exists, create the new one under a DISTINCT name — add a short meaningful "
    "suffix (e.g. purpose or a few random hex chars) — so it begins with empty state. "
    "Reuse an existing project directory ONLY when the user explicitly wants to modify, "
    "update, inspect, or destroy that SAME existing deployment. Reusing tested .tf "
    "CONTENT as a starting point is good; it is stale STATE you must never inherit."
)

# Defender drives Cortex through the external cortex-mcp when it's available;
# otherwise it falls back to the native cortex_* skills.
_DEFENDER_CORTEX = _CORTEXMCP_SKILLS or _CORTEX_SKILLS

# Cortex Cloud *account onboarding* (AWS/Azure account → Cortex Cloud) lives ONLY on
# the external cortex-mcp. Surface those specific tools to any agent that should be
# able to onboard an account — even one whose main Cortex bundle is the native
# cortex_* set (Nova), which has no onboarding of its own. Empty if cortex-mcp is
# unavailable (then onboarding isn't possible without provisioning CORTEXCLOUD_* for
# the Terraform provider).
_CORTEX_ONBOARD_SKILLS = [
    s for s in _CORTEXMCP_SKILLS if s in (
        "cortexmcp_cloud_onboard_aws_account",
        "cortexmcp_cloud_onboard_azure_account",
        "cortexmcp_cloud_get_instances",
        "cortexmcp_cloud_create_instance_template",
        "cortexmcp_cloud_get_outposts",
        "cortexmcp_cloud_create_outpost_template",
    )
]

# Onboarding a cloud ACCOUNT *into* Cortex Cloud must go through cortex-mcp, NOT the
# cortexcloud Terraform provider (which needs separate CORTEXCLOUD_* secrets that are
# not provisioned here and always fail with a provider-config error). Only meaningful
# when those MCP tools are present.
_CLOUD_ONBOARD_NOTE = (
    "To onboard an AWS account into Cortex Cloud, follow this EXACT two-step flow — do "
    "not improvise a Terraform script for any part of it:\n"
    "1. CREATE THE ONBOARDING via cortex-mcp: call cortexmcp_cloud_onboard_aws_account "
    "(for a single account use scope='ACCOUNT' and scan_mode='MANAGED'; ALWAYS pass "
    "regions — the AWS onboarding API rejects the request without at least one region, "
    "e.g. regions=['ap-southeast-1'] — and pass additional_capabilities as the user asks, "
    "e.g. {\"agentless_disk_scanning\": true, "
    "\"registry_scanning\": true, \"serverless_scanning\": true, \"xsiam_analytics\": true, "
    "\"data_security_posture_management\": true}). This authenticates with the live Cortex "
    "tenant, creates a PENDING instance, and returns a CloudFormation trust template link "
    "at manual.CF (with manual.TF as an alternative, and automated.link as a one-click "
    "console URL for a human). The account is BOUND when you "
    "deploy that template into it, so no account id is sent in this call.\n"
    "2. APPLY THE TEMPLATE with CloudFormation (NOT Terraform): call cloudformation_deploy "
    "with template_url = the manual.CF link, a stack_name like "
    "'cortex-cloud-onboarding-<account-id>', and an explicit region (use aws_whoami to "
    "confirm the target account/region first). It creates the stack, waits for "
    "CREATE_COMPLETE, and returns the status — this is a real IAM-creating change, so it "
    "is gated: on a confirm_required refusal, summarize what the stack creates and ask the "
    "user to type the stack name to confirm, then retry. Report the stack as created ONLY "
    "if cloudformation_deploy returns ok:true; if it returns ok:false, refused, or an "
    "error, show that error verbatim and STOP — do NOT claim the stack exists, and do NOT "
    "fabricate a describe-stacks/CREATE_COMPLETE result.\n"
    "Then VERIFY with real read-only calls before reporting success: cloudformation_status "
    "for the stack's actual StackStatus, and cortexmcp_cloud_get_instances (the instance "
    "should move off PENDING). Quote what those tools return; never invent their output. "
    "Do NOT author a 'cortex-aws-onboarding' Terraform project, do NOT use an "
    "aws_cloudformation_stack Terraform resource, and do NOT use the cortexcloud Terraform "
    "provider — that path needs separate CORTEXCLOUD_* credentials that are NOT provisioned "
    "here and always fails with a provider-configuration error. Cortex-mcp + "
    "cloudformation_deploy is the only supported path. (For Azure, the equivalent is "
    "cortexmcp_cloud_onboard_azure_account, whose manual.ARM/TF template is deployed with "
    "azure_cli.)"
    if _CORTEX_ONBOARD_SKILLS else ""
)

# Code-default per-agent capability grants (config can override via infra.agent_capabilities).
_DEFAULT_GRANTS: dict[str, list[str]] = {
    "general": _CORTEX_SKILLS + _CORTEX_ONBOARD_SKILLS + _CLOUD_SKILLS + _AZURE_SKILLS + _K8S_SKILLS + _PURPLE_SKILLS + _WEB_SKILLS + _BROWSER_SKILLS + _REPORT_SKILLS,
    "defender": _DEFENDER_CORTEX + _CLOUD_SKILLS + _AZURE_SKILLS + _K8S_SKILLS + _WEB_SKILLS + _REPORT_SKILLS,
    "provisioner": _CLOUD_SKILLS + _AZURE_SKILLS + _K8S_SKILLS + _WEB_SKILLS + _REPORT_SKILLS,
    "attacker": _CLOUD_SKILLS + _AZURE_SKILLS + _PURPLE_SKILLS + _WEB_SKILLS + _BROWSER_SKILLS + _REPORT_SKILLS,
    # Engineer — SOAR content developer: build/deploy/delete Cortex content, plus
    # read-only Cortex context (native cortex_* reads) to inform what it builds.
    "engineer": (_ENGINEER_SKILLS
                 + ["cortex_test_connection", "cortex_get_incidents",
                    "cortex_get_alerts", "cortex_get_endpoints"]
                 + _WEB_SKILLS + _REPORT_SKILLS),
}


def _grant(name: str) -> list[str]:
    """Resolve an agent's skill_names: config override if present, else code default."""
    override = _cfg.infra.agent_capabilities.get(name)
    return list(override) if override else list(_DEFAULT_GRANTS.get(name, []))


# Defender — blue-team analyst: Cortex XDR + infra reach for response (the "cortex" console).
_DEFENDER_CORTEX_NOTE = (
    "Your Cortex tools are provided by the external cortex-mcp server (the "
    "cortexmcp_* tools) — Sirius drives your Cortex tenant over the Model Context "
    "Protocol. Use them to list incidents/alerts/endpoints, run XQL, take response "
    "actions (isolate/unisolate/scan), build agent installers (download_distribution), "
    "and reach the whole Cortex API via cortexmcp_cortex_api_call when a typed tool "
    "doesn't exist."
    if _CORTEXMCP_SKILLS else
    "You investigate and respond using Cortex XDR: list incidents, alerts and "
    "endpoints, run XQL queries, and take response actions (isolate/unisolate/scan). "
    + _KONNECTOR_NOTE + " " + _ENDPOINT_INSTALLER_NOTE
)
DEFENDER_AGENT = Agent(
    name="defender",
    system=(
        "You are Defender, the blue-team security analyst agent on the Sirius "
        "platform. " + _DEFENDER_CORTEX_NOTE + " You can also reach infrastructure — "
        "Terraform (cloud) and Kubernetes (helm/kubectl) — to deploy sensors and "
        "contain threats. Explain findings clearly and recommend concrete "
        "remediation. " + _CLOUD_ONBOARD_NOTE + " " + _AZURE_NOTE + " " + _INFRA_NOTE
        + " " + _TRUTHFUL_NOTE
    ),
    skill_names=_grant("defender"),
)

# Terra — infrastructure engineer: Terraform (cloud) + Kubernetes (the "terraform" console).
PROVISIONER_AGENT = Agent(
    name="provisioner",
    system=(
        "You are Terra, the infrastructure engineer agent on the Sirius platform. You "
        "DESIGN, create, deploy, and tear down AWS infrastructure with Terraform, and "
        "manage Kubernetes (helm/kubectl), streaming output live. You can apply remote "
        "manifests straight from a URL — kubectl fetches URLs itself, so "
        "`k8s_kubectl(\"apply -f https://.../all-in-one.yaml\")` works directly (it is a "
        "gated mutation, so confirm as usual). Use web_fetch first only when you need to "
        "read or transform the file's contents before applying.\n\n"
        "Only actions that change REAL infrastructure — terraform apply and destroy — "
        "need confirmation, and it happens IN CHAT: when the tool is refused with "
        "confirm_required, summarize the impact and ask the user to type the exact "
        "project name to confirm; they type it in the normal chat box, the UI records "
        "the authorization, and you run the same tool again. Apply is confirmed ONCE "
        "PER SESSION: after the user confirms a project's apply, you may re-apply that "
        "SAME project again in this session WITHOUT another confirmation — this is what "
        "lets you fix a failed apply and retry on your own. Destroy always gets its own "
        "confirm. Writing project files and running init/plan need NO confirmation — do "
        "them proactively.\n\n"
        "Follow this lifecycle:\n"
        "1. CREATE — When the user describes infrastructure they want, design it and "
        "author the Terraform files yourself (provider.tf, main.tf, variables.tf, "
        "outputs.tf as appropriate) and call terraform_write_project right away — no "
        "confirmation needed to write local files. NEVER put secrets or credentials in "
        "any file or variable default — region comes from a variable; credentials come "
        "only from the environment.\n"
        "2. PLAN — immediately run terraform_plan (it inits) to preview, then explain "
        "in plain, non-technical language exactly what resources will be created and "
        "any cost/security implications. No confirmation needed to plan.\n"
        "3. APPLY — call terraform_apply DIRECTLY. Do NOT ask the user to type anything "
        "first: the confirmation prompt is armed by the tool's own confirm_required "
        "refusal, so it must be your FIRST apply call that produces it. ONLY after "
        "terraform_apply returns confirm_required do you relay it — summarize the impact "
        "and ask the user to type the exact project name. They type it once in the chat "
        "box, the UI records the authorization, and you call terraform_apply again. "
        "Asking for the typed name based on the plan alone (before the tool refuses) "
        "makes the user type it twice — never do that. This is the ONLY confirmation in "
        "the build flow.\n"
        "3b. SELF-HEAL ON FAILURE — if terraform_apply fails, READ the returned "
        "terraform_error and output to find the cause. If it is a fixable CONFIGURATION "
        "problem — e.g. an unsupported Kubernetes/EKS version, an invalid or unavailable "
        "instance type, an unavailable availability zone, or a name/CIDR that collides "
        "with resources another terraform project already created — correct the "
        "offending .tf with terraform_write_project and call terraform_apply again. You "
        "do NOT need a new confirmation to re-apply the SAME project in this session. "
        "Briefly explain each fix you make and why. Make at most 3 repair attempts; if it "
        "still fails, or the error is about credentials, permissions, or quota rather "
        "than config, STOP and report it plainly — do not loop.\n"
        "4. BEFORE DESTROY — summarize in plain language every resource that will be "
        "destroyed (use terraform_state_list / terraform_plan), then call "
        "terraform_destroy DIRECTLY (same rule: let the tool's confirm_required arm the "
        "prompt — don't pre-ask for the typed name), then retry after the user confirms.\n"
        "5. AFTER DESTROY — offer to remove the local project files with "
        "terraform_remove_project (no confirmation needed — local files only).\n\n"
        "REPORT ONLY WHAT THE TOOLS RETURN. Never announce that infrastructure was "
        "created, changed, or destroyed unless the tool call you just made in THIS turn "
        "returned ok:true. terraform_apply returns ok, applied, resource_count, and "
        "outputs — quote those actual values, and if it returned needs_auth / refused / "
        "ok:false, tell the user plainly it did NOT succeed and show the error. After a "
        "successful apply or destroy, call terraform_state_list (or terraform_status) to "
        "confirm the real resource count before you summarize. Never fabricate resource "
        "counts, outputs, endpoints, or a success message.\n\n"
        "Always confirm the cloud identity before touching infrastructure — aws_whoami "
        "for AWS, azure_whoami for Azure — and stop on auth errors; never accept pasted "
        "credentials on the command line. " + _NEW_BUILD_NOTE + " " + _AWS_CLI_NOTE + " " + _AZURE_NOTE + " " + _INFRA_NOTE
    ),
    skill_names=_grant("provisioner"),
)

# Raven — red-team recon operator + provisions its own attacker box (the "Attacker" console).
ATTACKER_AGENT = Agent(
    name="attacker",
    system=(
        "You are Raven, the red-team recon agent on the Sirius platform. Safe local "
        "recon (fingerprinting, endpoint/API discovery, exposure checks) is always "
        "available. You can provision and tear down your own attacker infrastructure "
        "via Terraform (you are scoped to the kali-box project only). Intrusive "
        "Kali-on-AWS scans are double-gated: the human must authorize the exact target "
        "first. If an action is refused, tell the user exactly how to authorize it — "
        "never try to bypass the gate. " + _AZURE_NOTE
    ),
    skill_names=_grant("attacker"),
)

# Nova — general coordinator with every ops/security skill (the "general" console).
GENERAL_AGENT = Agent(
    name="general",
    system=(
        "You are Nova, the general coordinator agent on the Sirius platform. You "
        "have every ops and security skill available — purple-team recon, Terraform "
        "(cloud), Kubernetes (helm/kubectl), and Cortex XDR. Use your tools when "
        "helpful and explain what you find clearly. You also have full Azure reach "
        "via the az CLI. For intrusive scans, the human must authorize the target "
        "first. " + _KONNECTOR_NOTE + " " + _CLOUD_ONBOARD_NOTE + " " + _NEW_BUILD_NOTE + " " + _AWS_CLI_NOTE + " " + _AZURE_NOTE + " " + _INFRA_NOTE
        + " " + _TRUTHFUL_NOTE
    ),
    skill_names=_grant("general"),
)

# Engineer — SOAR content developer: authors, deploys, and deletes Cortex XSIAM
# content (playbooks + automation scripts) as code with demisto-sdk (the "engineer"
# console).
_ENGINEER_NOTE = (
    "You BUILD, DEPLOY, and DELETE Cortex XSIAM content (playbooks and automation "
    "scripts) as code with the demisto-sdk. Typical flow:\n"
    "1. Check readiness with engineer_status (demisto-sdk present, STANDARD "
    "credentials set, tenant reachable). If it reports not ready, tell the user "
    "exactly what is missing and STOP — do not pretend a deploy happened.\n"
    "2. AUTHOR — for a brand-new playbook call engineer_scaffold_playbook(name, "
    "description) to write a minimal valid skeleton into the SiriusDemo pack, then "
    "extend it; or write full YAML yourself and save it with engineer_write_content "
    "(kind='playbook' or 'script'). engineer_list_content shows what is in the pack.\n"
    "3. VALIDATE with engineer_validate before shipping (defaults to the whole pack).\n"
    "4. DEPLOY with engineer_deploy(target) — this uploads to the LIVE tenant. Pass a "
    "single file to ship just that item, or empty for the whole pack.\n"
    "5. MANAGE — engineer_list_playbooks finds the exact id of anything on the tenant; "
    "engineer_delete_playbook / engineer_delete_script remove it. Deletion is "
    "permanent on the live tenant, so confirm the id with engineer_list_playbooks "
    "first and tell the user what you are about to delete.\n"
    "Deploy and delete hit the real Cortex tenant — REPORT ONLY WHAT THE TOOLS "
    "RETURN. Never claim a playbook was deployed or deleted unless the tool call you "
    "made in THIS turn returned ok:true; if it returns ok:false show the actual error "
    "and say plainly it did not succeed. Do not fabricate ids, paths, or SDK output."
)
ENGINEER_AGENT = Agent(
    name="engineer",
    system=(
        "You are Engineer, the SOAR content-developer agent on the Sirius platform. "
        + _ENGINEER_NOTE + " You can also read live Cortex context (incidents, alerts, "
        "endpoints) to inform the content you build. Keep the user in the loop and "
        "explain each step."
    ),
    skill_names=_grant("engineer"),
)

# Platform agents keyed by the name the frontend routes to.
_PLATFORM_AGENTS = [GENERAL_AGENT, DEFENDER_AGENT, PROVISIONER_AGENT, ATTACKER_AGENT,
                    ENGINEER_AGENT]


# ── Multi-agent model routing ────────────────────────────────────────────────
# The platform agents run the default model; the Prisma AIRS scenario agents run
# the lighter scenario_model. Both configurable in config.yaml (llm.model /
# llm.scenario_model).
for _a in _PLATFORM_AGENTS:
    _a.model = _cfg.llm.model
_SCENARIO_MODEL = _cfg.llm.scenario_model or _cfg.llm.model
for _s in scenarios.SCENARIOS.values():
    _s.agent.model = _SCENARIO_MODEL


def _agents() -> dict[str, Agent]:
    agents = {a.name: a for a in _PLATFORM_AGENTS}
    for s in scenarios.SCENARIOS.values():
        agents[s.agent.name] = s.agent
    return agents


# session_id -> {"messages": [...], "agent": name}
_sessions: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus.bind_loop(asyncio.get_running_loop())
    _log.info("Sirius up. LLM=%s  AIRS=%s  Cortex mock=%s",
              _llm.model, _airs.available, _cfg.cortex.mock_mode)
    yield


app = FastAPI(title="Sirius", version="0.1.0", lifespan=lifespan)


# ── Models ─────────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    session_id: str
    message: str
    agent: Optional[str] = None        # agent name; or use `scenario`
    scenario: Optional[str] = None     # scenario id -> selects its agent
    mode: Optional[str] = None         # UI mode (general/cortex/airs/…) for history
    airs_enabled: bool = True
    reset: bool = False


class DeleteConversationRequest(BaseModel):
    session_id: str


class AuthorizeRequest(BaseModel):
    target: str
    acknowledgement: str               # must contain the confirmation phrase


class AuthorizeInfraRequest(BaseModel):
    resource: str                      # terraform project name, or "k8s"
    acknowledgement: str               # must contain the confirmation phrase


class AwsLoginRequest(BaseModel):
    session_id: str                    # so the login URL streams to the right UI


class AzureLoginRequest(BaseModel):
    session_id: str                    # so the device-code link streams to the right UI


class AwsKeysRequest(BaseModel):
    access_key_id: str
    secret_access_key: str
    session_token: str = ""
    region: str = ""


class VertexProjectRequest(BaseModel):
    project: str                       # target GCP project id
    region: str = ""                   # optional Vertex region override
    session_id: str = ""               # so the switch event streams to the right UI


ACK_PHRASE = "I AM AUTHORIZED"


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"ok": True, "llm": _llm.model, "airs_available": _airs.available}


@app.get("/api/bootstrap")
async def bootstrap():
    return {
        "agents": [
            {"name": a.name, "skills": a.skill_names, "model": a.model or _llm.model}
            for a in _agents().values()
        ],
        "scenarios": scenarios.list_scenarios(),
        "config": {
            "airs_enabled_default": _cfg.airs.enabled,
            "airs_available": _airs.available,
            "airs_profile": _cfg.airs.profile_name,
            "cortex_mock": _cfg.cortex.mock_mode,
            "cortex_mcp_tools": len(_CORTEXMCP_SKILLS),
            "allow_intrusive": _cfg.purpleteam.allow_intrusive,
            "infra_require_authorization": _cfg.infra.require_authorization,
            "aws_configured": _cfg.aws.configured(),
            "aws_profile": _cfg.aws.profile,
            "llm_model": _cfg.llm.model,
            "llm_scenario_model": _SCENARIO_MODEL,
            "vertex_project": _cfg.vertex_project(),
            "vertex_region": _cfg.vertex_region(),
        },
        "ack_phrase": ACK_PHRASE,
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    agents = _agents()
    if req.scenario:
        sc = scenarios.get_scenario(req.scenario)
        if not sc:
            return JSONResponse({"error": f"unknown scenario {req.scenario}"}, 400)
        agent = sc.agent
    else:
        agent = agents.get(req.agent or GENERAL_AGENT.name, GENERAL_AGENT)

    sess = _sessions.get(req.session_id)
    if sess is None:
        # First touch this run — seed context from the audit log so a conversation
        # reopened from the History sidebar (or after a restart) keeps its memory.
        sess = {"messages": _rehydrate_messages(req.session_id), "agent": agent.name}
        _sessions[req.session_id] = sess
    if req.reset or sess["agent"] != agent.name:
        sess["messages"] = []
        sess["agent"] = agent.name

    try:
        reply = await run_turn(
            agent=agent,
            llm=_llm,
            airs=_airs,
            session_id=req.session_id,
            user_message=req.message,
            messages=sess["messages"],
            airs_enabled=req.airs_enabled,
            max_tokens=_cfg.llm.max_tokens,
            max_steps=_cfg.llm.max_steps,
            # Stream token-by-token for platform agents; AIRS scenarios keep
            # scan-then-reveal (streaming would leak text before the response scan).
            stream=(not req.airs_enabled),
        )
    except Exception as e:  # record the failed turn, then preserve existing behavior
        audit_mod.record(
            session_id=req.session_id, agent=agent.name, mode=req.mode,
            scenario=req.scenario, airs_enabled=req.airs_enabled,
            prompt=req.message, reply="", status="error", error=str(e),
        )
        raise
    # A turn stopped by the user is recorded as "cancelled" (not "ok"), so it is
    # skipped when rehydrating session memory and doesn't poison later turns.
    stopped = agent_mod.consume_cancelled(req.session_id)
    audit_mod.record(
        session_id=req.session_id, agent=agent.name, mode=req.mode,
        scenario=req.scenario, airs_enabled=req.airs_enabled,
        prompt=req.message, reply=reply, status="cancelled" if stopped else "ok",
    )
    # If a destructive infra op was refused this turn pending human confirmation,
    # hand the resource/op to the UI so it can prompt for the name in-chat.
    confirm = infra_gate.take_pending(req.session_id)
    return {"reply": reply, "agent": agent.name, "confirm": confirm}


class CancelRequest(BaseModel):
    session_id: str


@app.post("/api/chat/cancel")
async def cancel_chat(req: CancelRequest):
    """Interrupt the in-flight turn for this session (the UI's Esc / Stop button).

    The agent loop can't kill a blocking LLM/tool call mid-flight, so this flags
    the turn to stop at its next checkpoint (before the next LLM step, or right
    after the current tool finishes)."""
    agent_mod.request_cancel(req.session_id)
    return {"ok": True}


def _rehydrate_messages(session_id: str) -> list[dict]:
    """Rebuild agent message history from the audit log — successful turns only,
    as plain user/assistant text pairs (tool-call detail isn't needed to continue)."""
    messages: list[dict] = []
    for e in audit_mod.read_session(session_id):
        if e.get("status") != "ok":
            continue
        reply = e.get("reply") or ""
        if not reply:
            continue  # skip empty replies — Anthropic rejects empty content blocks
        messages.append({"role": "user", "content": e.get("prompt") or ""})
        messages.append({"role": "assistant", "content": reply})
    return messages


@app.get("/api/history")
async def get_history(limit: Optional[int] = None, session_id: Optional[str] = None):
    """Prompt/response audit trail, newest first. Optionally filtered to one session."""
    return {"entries": audit_mod.read_all(limit, session_id)}


@app.post("/api/history/clear")
async def clear_history():
    return {"ok": True, "cleared": audit_mod.clear()}


@app.get("/api/conversations")
async def get_conversations():
    """Audit turns grouped into conversations (for the history sidebar), newest first."""
    return {"conversations": audit_mod.list_conversations()}


@app.post("/api/conversations/delete")
async def delete_conversation(req: DeleteConversationRequest):
    return {"ok": True, "deleted": audit_mod.delete_session(req.session_id)}


@app.get("/api/reports")
async def list_reports():
    """List generated findings reports, newest first (for a UI list if wanted)."""
    d = reports_mod._reports_dir()
    items = []
    for p in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        items.append({
            "filename": p.name,
            "download_url": f"/api/reports/{p.name}",
            "bytes": st.st_size,
            "modified_ts": st.st_mtime,
        })
    return {"reports": items}


@app.get("/api/reports/{filename}")
async def download_report(filename: str):
    """Serve a generated report as a download (Content-Disposition: attachment).

    Path-traversal guarded: only *.md files that actually resolve inside the
    reports directory are served."""
    d = reports_mod._reports_dir()
    safe = Path(filename).name  # strip any directory components
    path = (d / safe).resolve()
    if path.suffix != ".md" or not str(path).startswith(str(d.resolve())) or not path.is_file():
        return JSONResponse({"error": "report not found"}, 404)
    return FileResponse(str(path), media_type="text/markdown", filename=safe)


@app.post("/api/purpleteam/authorize")
async def authorize(req: AuthorizeRequest):
    if ACK_PHRASE not in req.acknowledgement.upper():
        return JSONResponse(
            {"error": f"acknowledgement must contain the phrase '{ACK_PHRASE}'"}, 400)
    if not _cfg.purpleteam.allow_intrusive:
        return JSONResponse(
            {"error": "purpleteam.allow_intrusive is false in config.yaml"}, 403)
    purpleteam.authorize_target(req.target)
    return {"ok": True, "authorized": req.target}


@app.post("/api/infra/authorize")
async def authorize_infra(req: AuthorizeInfraRequest):
    """Human authorizes a destructive infra op on a resource (terraform project or
    'k8s'). Confirmed in-chat by typing the exact resource name: the acknowledgement
    must match the resource. One-shot — consumed when the next gated op runs."""
    if req.acknowledgement.strip().lower() != (req.resource or "").strip().lower():
        return JSONResponse(
            {"error": f"to confirm, type the exact resource name '{req.resource}'"}, 400)
    infra_gate.authorize_infra(req.resource)
    return {"ok": True, "authorized": req.resource}


@app.post("/api/aws/sso/login")
async def aws_login(req: AwsLoginRequest):
    """Kick off `aws sso login` for the Terraform agent. The verification URL is
    streamed to the event stream; AWS_PROFILE is set in the running process so
    later terraform runs inherit it with no restart."""
    if not _cfg.aws.configured():
        return JSONResponse(
            {"error": "AWS SSO not configured — set aws.sso_start_url / account_id / "
                      "role_name in config.yaml"}, 400)
    # SSO and pasted keys are mutually exclusive — drop keys so the profile wins.
    settings_mod.clear_aws_keys_for_sso()
    res = aws_sso.begin_login(req.session_id, _cfg.aws)
    if not res.get("ok"):
        return JSONResponse(res, 409)
    return res


@app.get("/api/aws/status")
async def aws_status():
    return aws_sso.status(_cfg.aws.profile if _cfg.aws.configured() else None)


@app.post("/api/azure/login")
async def azure_login_endpoint(req: AzureLoginRequest):
    """Kick off `az login --use-device-code`. The verification URL + code stream to
    the event stream; the az CLI caches the token so later az/terraform runs inherit
    the session with no restart."""
    res = azure_cli.begin_login(req.session_id, _cfg.azure)
    if not res.get("ok"):
        return JSONResponse(res, 409)
    return res


@app.get("/api/azure/status")
async def azure_status():
    return azure_cli.azure_whoami()


@app.get("/api/vertex/status")
async def vertex_status():
    """Resolved Vertex project/region + ADC presence + the live backend model."""
    return {**vertex_mod.status(), "model": _llm.model}


@app.get("/api/vertex/projects")
async def vertex_projects():
    """Best-effort `gcloud projects list` for the switcher (may be empty → free-text)."""
    return vertex_mod.list_projects()


@app.post("/api/vertex/project")
async def vertex_set_project(req: VertexProjectRequest):
    """Switch the active Vertex project live: persist to .env + env, then rebuild the
    LLM backend so the next turn uses it — no restart. GCP credentials (ADC) are
    reused; if the new project needs a different identity, run
    `gcloud auth application-default login` and it will be picked up."""
    res = vertex_mod.set_project(req.project, req.region, req.session_id or None)
    if not res.get("ok"):
        return JSONResponse(res, 400)
    model = rebuild_llm()
    return {**res, "model": model, **vertex_mod.status()}


@app.get("/api/settings")
async def get_settings():
    """Masked AWS credential status (never returns secret values)."""
    # aws_status() shells out to `aws sts get-caller-identity`; run it off the event
    # loop so a slow/hanging AWS CLI call never blocks other requests or the WS stream.
    return await asyncio.to_thread(settings_mod.aws_status)


@app.post("/api/settings/aws/keys")
async def save_aws_keys(req: AwsKeysRequest):
    # save_aws_keys() ends with a blocking `aws sts get-caller-identity` (via
    # aws_status); offload to a thread so "Saving…" doesn't hang the event loop.
    res = await asyncio.to_thread(
        settings_mod.save_aws_keys,
        req.access_key_id, req.secret_access_key, req.session_token, req.region)
    if not res.get("ok"):
        return JSONResponse(res, 400)
    return res


@app.post("/api/settings/aws/clear")
async def clear_aws_creds():
    return await asyncio.to_thread(settings_mod.clear_aws)


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    await ws.accept()
    session_id = ws.query_params.get("session_id") or uuid.uuid4().hex
    q = await bus.subscribe(session_id, replay=True)
    try:
        await ws.send_json({"type": "connected", "session_id": session_id})
        while True:
            ev = await q.get()
            await ws.send_json(ev.model_dump())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(session_id, q)


# ── Serve the built frontend, if present ────────────────────────────────────────
_DIST = Path(__file__).resolve().parent.parent / "web_dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(str(_DIST / "index.html"))
else:
    @app.get("/")
    async def index_placeholder():
        return JSONResponse({
            "app": "Sirius",
            "note": "Frontend not built yet. Run the Vite dev server (npm run dev) "
                    "or build it into backend/sirius/web_dist.",
        })
