"""
sirius-cloud MCP server — cloud authentication + Terraform over stdio.

Tools (today): AWS SSO login, AWS whoami, and terraform list/plan/apply/destroy.
Designed to grow: Azure (`az login`) and GCP (`gcloud auth`) authentication tools
slot in alongside aws_sso_login, and their binaries get added to
`_runner.ALLOWED_BINARIES`.

Runs over stdio:  python -m sirius.cloud.main

Register in an MCP client, e.g.:
  {"mcpServers": {"sirius-cloud": {"command": "backend/.venv/bin/python",
    "args": ["-m", "sirius.cloud.main"]}}}

SECURITY: credentials come from the process environment / the aws CLI's own SSO
cache only. This server never accepts pasted keys, never writes secrets, and never
echoes them — the only file it writes is the NON-SECRET ~/.aws/config profile.
terraform apply/destroy run with -auto-approve; expose only to trusted clients.
"""

from __future__ import annotations

import configparser
import json
import os
import re
from pathlib import Path

from sirius.cloud._runner import run

# backend/sirius/cloud/mcp_server.py -> parents: [0]=cloud [1]=sirius [2]=backend
# [3]=repo root, which holds terraform/<project>/.
TERRAFORM_ROOT = Path(__file__).resolve().parents[3] / "terraform"

# Guard so a bad/injected project name can't escape the terraform/ directory.
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")


# ── AWS helpers (standalone; no app/event-bus deps) ─────────────────────────────
def _aws_config_path() -> Path:
    override = os.environ.get("AWS_CONFIG_FILE")
    return Path(override).expanduser() if override else Path.home() / ".aws" / "config"


def _ensure_aws_profile(profile: str, start_url: str, region: str,
                        account_id: str, role_name: str) -> None:
    """Create/update the NON-SECRET SSO profile in ~/.aws/config (surgical)."""
    path = _aws_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)
    section = "default" if profile == "default" else f"profile {profile}"
    if not parser.has_section(section):
        parser.add_section(section)
    parser[section]["sso_start_url"] = start_url
    parser[section]["sso_region"] = region
    parser[section]["sso_account_id"] = account_id
    parser[section]["sso_role_name"] = role_name
    parser[section]["region"] = region
    parser[section]["output"] = "json"
    with open(path, "w", encoding="utf-8") as f:
        parser.write(f)


def _whoami(profile: str = "") -> dict:
    """sts get-caller-identity, without printing any secret."""
    env = os.environ.copy()
    if profile:
        env["AWS_PROFILE"] = profile
    r = run(["aws", "sts", "get-caller-identity", "--output", "json"], timeout=30, env=env)
    if not r["ok"]:
        return {"authenticated": False, "error": r["output"][:300]}
    try:
        ident = json.loads(r["output"] or "{}")
    except json.JSONDecodeError:
        return {"authenticated": False, "error": r["output"][:300]}
    return {"authenticated": True, "account": ident.get("Account"),
            "arn": ident.get("Arn"), "profile": env.get("AWS_PROFILE")}


def _azure_whoami() -> dict:
    """az account show, without printing any secret."""
    r = run(["az", "account", "show", "-o", "json"], timeout=30)
    if not r["ok"]:
        return {"authenticated": False, "error": r["output"][:300]}
    try:
        acct = json.loads(r["output"] or "{}")
    except json.JSONDecodeError:
        return {"authenticated": False, "error": r["output"][:300]}
    return {"authenticated": True, "subscription_id": acct.get("id"),
            "subscription_name": acct.get("name"), "tenant_id": acct.get("tenantId"),
            "user": (acct.get("user") or {}).get("name")}


# ── Terraform helpers ───────────────────────────────────────────────────────────
def _project_dir(name: str) -> Path | None:
    if not _SAFE_NAME.match(name or ""):
        return None
    d = (TERRAFORM_ROOT / name).resolve()
    if not str(d).startswith(str(TERRAFORM_ROOT.resolve())):
        return None
    return d


def _tf_preflight() -> dict:
    who = _whoami(os.environ.get("AWS_PROFILE", ""))
    if not who.get("authenticated"):
        return {"ok": False,
                "error": "no valid AWS session — run aws_sso_login first", "detail": who}
    return {"ok": True, "identity": who}


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise SystemExit("mcp not installed. Run: pip install 'sirius[mcp]'") from e

    mcp = FastMCP("sirius-cloud")

    # ── AWS authentication ──────────────────────────────────────────────────────
    @mcp.tool()
    def aws_sso_login(profile: str = "sirius", start_url: str = "", sso_region: str = "",
                      account_id: str = "", role_name: str = "") -> dict:
        """Authenticate to AWS via IAM Identity Center (SSO).

        If start_url + account_id + role_name are provided, the named profile is
        created/updated in ~/.aws/config first (non-secret). Then `aws sso login`
        runs and your browser opens to complete sign-in. AWS_PROFILE is set for
        this server process so the terraform_* tools inherit the session; short-
        lived role creds refresh automatically from ~/.aws/sso/cache/ until the
        SSO token expires (then just call this again — no server restart)."""
        if start_url and account_id and role_name:
            _ensure_aws_profile(profile, start_url, sso_region, account_id, role_name)
        os.environ["AWS_PROFILE"] = profile
        login = run(["aws", "sso", "login", "--profile", profile], timeout=300)
        return {"ok": login["ok"], "profile": profile,
                "login_output": login["output"],
                "identity": _whoami(profile) if login["ok"] else {}}

    @mcp.tool()
    def aws_whoami(profile: str = "") -> dict:
        """Show the current AWS identity (sts get-caller-identity). No secrets printed."""
        return _whoami(profile or os.environ.get("AWS_PROFILE", ""))

    # ── Azure authentication + CLI ───────────────────────────────────────────────
    @mcp.tool()
    def azure_login(tenant: str = "", subscription: str = "") -> dict:
        """Authenticate to Azure via device code (`az login --use-device-code`).

        The output contains the verification URL + code to open in a browser; the
        call blocks until sign-in completes. The az CLI caches the token in
        ~/.azure/ so later az/terraform runs inherit the session. Optionally pins a
        tenant and selects a subscription afterwards. No secret is written by us."""
        cmd = ["az", "login", "--use-device-code"]
        if tenant:
            cmd += ["--tenant", tenant]
        login = run(cmd, timeout=300)
        if login["ok"] and subscription:
            run(["az", "account", "set", "--subscription", subscription], timeout=30)
        return {"ok": login["ok"], "login_output": login["output"],
                "identity": _azure_whoami() if login["ok"] else {}}

    @mcp.tool()
    def azure_whoami() -> dict:
        """Show the current Azure identity + subscription (az account show). No secrets."""
        return _azure_whoami()

    @mcp.tool()
    def azure_cli(args: list[str]) -> dict:
        """Run an `az` command. `args` is the argument list WITHOUT the leading 'az'
        (e.g. ["group", "list"] or ["group", "create", "-n", "rg", "-l", "eastus"]).
        Credentials come from the az session only; runs with shell=False. Mutating
        commands are NOT gated here — expose this server only to trusted clients."""
        if not args:
            return {"ok": False, "error": "empty az command"}
        if args[0] == "az":
            args = args[1:]
        if args and args[0] in ("login", "logout"):
            return {"ok": False, "error": "use the azure_login tool for sign-in/out"}
        return run(["az", *args], timeout=600)

    # ── Terraform ───────────────────────────────────────────────────────────────
    @mcp.tool()
    def terraform_list_projects() -> dict:
        """List terraform projects under the repo's terraform/ directory."""
        if not TERRAFORM_ROOT.exists():
            return {"projects": []}
        return {"projects": sorted(p.name for p in TERRAFORM_ROOT.iterdir()
                                   if p.is_dir() and any(p.glob("*.tf")))}

    @mcp.tool()
    def terraform_plan(project: str) -> dict:
        """terraform init + plan for terraform/<project>/ (AWS session required)."""
        d = _project_dir(project)
        if d is None:
            return {"ok": False, "error": f"invalid project name: {project!r}"}
        if not d.exists():
            return {"ok": False, "error": f"no such project: {project}"}
        pre = _tf_preflight()
        if not pre["ok"]:
            return pre
        init = run(["terraform", f"-chdir={d}", "init", "-input=false"], timeout=600)
        plan = run(["terraform", f"-chdir={d}", "plan", "-input=false", "-no-color"],
                   timeout=600)
        return {"ok": plan["ok"], "init_output": init["output"], "plan_output": plan["output"]}

    @mcp.tool()
    def terraform_apply(project: str) -> dict:
        """terraform init + apply (-auto-approve) for terraform/<project>/."""
        d = _project_dir(project)
        if d is None:
            return {"ok": False, "error": f"invalid project name: {project!r}"}
        if not d.exists():
            return {"ok": False, "error": f"no such project: {project}"}
        pre = _tf_preflight()
        if not pre["ok"]:
            return pre
        init = run(["terraform", f"-chdir={d}", "init", "-input=false"], timeout=600)
        apply = run(["terraform", f"-chdir={d}", "apply", "-auto-approve",
                     "-input=false", "-no-color"], timeout=1800)
        out = {"ok": apply["ok"], "init_output": init["output"], "apply_output": apply["output"]}
        if apply["ok"]:
            res = run(["terraform", f"-chdir={d}", "output", "-json"], timeout=60)
            try:
                out["outputs"] = json.loads(res["output"] or "{}")
            except json.JSONDecodeError:
                out["outputs"] = {}
        return out

    @mcp.tool()
    def terraform_destroy(project: str) -> dict:
        """terraform destroy (-auto-approve) for terraform/<project>/."""
        d = _project_dir(project)
        if d is None:
            return {"ok": False, "error": f"invalid project name: {project!r}"}
        if not d.exists():
            return {"ok": False, "error": f"no such project: {project}"}
        pre = _tf_preflight()
        if not pre["ok"]:
            return pre
        init = run(["terraform", f"-chdir={d}", "init", "-input=false"], timeout=600)
        destroy = run(["terraform", f"-chdir={d}", "destroy", "-auto-approve",
                       "-input=false", "-no-color"], timeout=1800)
        return {"ok": destroy["ok"], "init_output": init["output"],
                "destroy_output": destroy["output"]}

    return mcp
