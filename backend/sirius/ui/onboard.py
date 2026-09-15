"""
Interactive onboarding wizard for Sirius (`sirius onboard`).

Walks a new user through the choices Sirius needs to run and writes them to the
right place, honouring the two-tier split:
  - non-secret settings  -> config.yaml   (safe to commit)
  - secrets / API tokens -> .env          (git-ignored, chmod 600)

It never prints a secret back, never puts a secret in config.yaml, and never
asks for cloud *credentials* (AWS/Azure/GCP sign-in stays with `aws sso login`
/ `az login` / `gcloud auth` and the in-app buttons). Only genuine API tokens
that have no interactive login (Anthropic, Prisma AIRS, Cortex) go into .env.

Re-runnable: existing values are shown as defaults; pressing Enter keeps them.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _backend_root() -> Path:
    """The backend project root (parent of the `sirius` package) — holds config.yaml/.env."""
    return Path(__file__).resolve().parent.parent.parent


def _config_path() -> Path:
    return _backend_root() / "config.yaml"


def _env_path() -> Path:
    return _backend_root() / ".env"


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_GREEN = "\033[32m"
C_RESET = "\033[0m"


def _hdr(title: str) -> None:
    print(f"\n{C_BOLD}{C_CYAN}== {title} =={C_RESET}")


def _note(msg: str) -> None:
    print(f"{C_DIM}{msg}{C_RESET}")


def _ask(label: str, default: str = "") -> str:
    """Free-text prompt. Enter keeps the shown default."""
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{label}{suffix}: ").strip()
    except EOFError:
        raw = ""
    return raw or default


def _ask_secret(label: str, already_set: bool) -> Optional[str]:
    """Secret prompt via getpass (not echoed). Enter keeps the existing value.

    Returns the new secret, or None to leave whatever is already in .env alone.
    """
    import getpass

    state = f"{C_GREEN}already set{C_RESET}" if already_set else f"{C_YELLOW}not set{C_RESET}"
    hint = "Enter to keep" if already_set else "Enter to skip"
    try:
        val = getpass.getpass(f"{label} ({state}, {hint}): ").strip()
    except EOFError:
        val = ""
    return val or None


def _ask_bool(label: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    raw = _ask(f"{label} ({d})").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "true", "1")


def _ask_choice(label: str, options: list[tuple[str, str]], default_key: str) -> str:
    """Numbered single-select. `options` is [(key, description)]. Returns the key."""
    print(f"\n{C_BOLD}{label}{C_RESET}")
    keys = [k for k, _ in options]
    default_idx = keys.index(default_key) if default_key in keys else 0
    for i, (key, desc) in enumerate(options, 1):
        marker = f"{C_GREEN}(default){C_RESET}" if (i - 1) == default_idx else ""
        print(f"  {i}) {C_BOLD}{key}{C_RESET} — {desc} {marker}")
    while True:
        raw = _ask("Choose", str(default_idx + 1)).strip()
        if raw in keys:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return keys[int(raw) - 1]
        print(f"  {C_YELLOW}Enter 1-{len(options)} or the name.{C_RESET}")


# ---------------------------------------------------------------------------
# .env merge (preserve existing keys/comments; chmod 600)
# ---------------------------------------------------------------------------


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        values[key.strip()] = val.strip()
    return values


def _merge_env(path: Path, updates: dict[str, str]) -> None:
    """Update/insert KEY=VALUE for each item in `updates`, preserving the rest."""
    if not updates:
        return
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)

    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.partition("=")[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Added by `sirius onboard`")
        for key, val in remaining.items():
            out.append(f"{key}={val}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# config.yaml rendering (keeps the documented, commented structure)
# ---------------------------------------------------------------------------


def _yn(b: bool) -> str:
    return "true" if b else "false"


def _q(s: str) -> str:
    """YAML-safe double-quoted scalar for possibly-empty/url-ish strings."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_config(v: dict) -> str:
    hosts = ", ".join(_q(h) for h in v["browser_allowed_hosts"])
    return f"""\
# Sirius non-secret settings. Secrets live in .env (see .env.example).
# This file is safe to commit. Generated/updated by `sirius onboard` — you can
# also edit it by hand; re-run the wizard any time to change these values.

llm:
  backend: {v["llm_backend"]}                    # vertex | direct | bedrock | echo
  model: {v["llm_model"]}             # default/general agent model id (provider-specific)
  scenario_model: {v["llm_scenario_model"]}    # Prisma AIRS scenario agents (bank/telco/healthcare)
  vertex_project: {v["vertex_project"]}   # Vertex only: GCP project id for Claude on Vertex AI.
                                     # Override without editing this file via
                                     # SIRIUS_VERTEX_PROJECT in .env, then restart.
  vertex_region: {v["vertex_region"]}              # Vertex only: region (e.g. global, us-east5).
  bedrock_region: {_q(v["bedrock_region"])}             # Bedrock only: falls back to AWS_REGION / us-east-1.
  max_tokens: {v["max_tokens"]}                  # per-response output ceiling (headroom for large tool calls)

airs:
  enabled: {_yn(v["airs_enabled"])}                      # master default; the UI toggle overrides per-request
  profile_name: {v["airs_profile"]}    # AIRS security profile name (from your tenant)
  app_name: sirius
  api_endpoint: {v["airs_endpoint"]}
  fail_open: true                    # scan errors allow traffic through (demo-friendly)

cortex:
  fqdn: {_q(v["cortex_fqdn"])}                           # e.g. https://api-yourtenant.xdr.us.paloaltonetworks.com
  advanced_auth: {_yn(v["cortex_advanced_auth"])}                # Advanced (nonce+timestamp+hash) vs Standard auth
  mock_mode: {_yn(v["cortex_mock_mode"])}                    # live once cortex.fqdn / CORTEX_FQDN is set (else auto-mock)
  konnector_profile_dir: cortex-profiles  # holds downloaded k8s security-profile pairs (*.auth.json + *.values.yaml)
  distribution_download_dir: cortex-distributions  # where cortex_download_distribution writes agent installers

cortex_mcp:
  # Sirius consumes the external cortex-mcp server (github.com/chiangyaw/cortex-mcp)
  # as an MCP client over stdio. Point `command` at that clone's venv python, or
  # leave blank to use the current interpreter.
  enabled: {_yn(v["cortex_mcp_enabled"])}
  command: {_q(v["cortex_mcp_command"])}
  args: ["-m", "cortex_mcp"]

purpleteam:
  allow_intrusive: {_yn(v["purpleteam_intrusive"])}         # master gate; per-run confirmation still required

browser:
  # Headless-browser (Playwright) skill for agents: navigate/click/type/screenshot.
  enabled: {_yn(v["browser_enabled"])}                     # master gate; disable to refuse all browser skills
  headless: true                     # set false to watch the browser during a demo
  allowed_hosts: [{hosts}]  # navigations off this list are refused ([] = any host)
  max_sessions: 4                    # max concurrent per-session browser contexts

infra:
  # Least-privilege policy for destructive infra ops (terraform apply/destroy,
  # k8s mutations), enforced by sirius.infra_gate.
  require_authorization: {_yn(v["infra_require_auth"])}        # destructive ops need human authorize via /api/infra/authorize
  agent_capabilities: {{}}
  agent_projects:
    attacker: ["kali-box", "aws"]    # Raven: own attacker box + aws_cli SSM for the authorized attack-range demo

aws:
  # Web-UI-driven `aws sso login` for Terraform. All NON-SECRET (safe to commit):
  # they name which SSO account/role to log into, not any credential.
  profile: {v["aws_profile"]}                    # ~/.aws/config profile Sirius creates/uses
  sso_start_url: {_q(v["aws_sso_start_url"])}                  # e.g. https://myorg.awsapps.com/start
  sso_region: {_q(v["aws_sso_region"])}                     # region hosting IAM Identity Center
  account_id: {_q(v["aws_account_id"])}                     # 12-digit target account id
  role_name: {_q(v["aws_role_name"])}                      # permission set / role, e.g. AdministratorAccess
  region: {_q(v["aws_region"])}                         # default region for the profile (falls back to sso_region)

azure:
  # Web-UI-driven `az login --use-device-code`. All NON-SECRET (safe to commit).
  # Service-principal secrets (ARM_CLIENT_SECRET etc.) live ONLY in .env.
  tenant_id: {_q(v["azure_tenant_id"])}                      # optional --tenant for login
  subscription_id: {_q(v["azure_subscription_id"])}                # subscription to select after login
  location: {_q(v["azure_location"])}                       # default region for TF variables / demos
"""


# ---------------------------------------------------------------------------
# Wizard sections
# ---------------------------------------------------------------------------

# Sensible per-provider model defaults so users aren't guessing ids.
_MODEL_HINTS = {
    "vertex": "claude-sonnet-5",
    "direct": "claude-sonnet-5",
    "bedrock": "anthropic.claude-sonnet-4-20250514-v1:0",
    "echo": "claude-sonnet-5",
}


def _section_llm(cfg: dict, env: dict, updates: dict) -> None:
    _hdr("Claude access — how should Sirius talk to Claude?")
    backend = _ask_choice(
        "LLM backend",
        [
            ("vertex", "Claude on GCP Vertex AI (ADC via `gcloud auth application-default login`)"),
            ("direct", "Direct Anthropic API (ANTHROPIC_API_KEY from console.anthropic.com)"),
            ("bedrock", "Amazon Bedrock (uses your AWS credential chain / SSO)"),
            ("echo", "No LLM — offline Echo backend (UI/event flow only, no real answers)"),
        ],
        default_key=cfg.get("llm", {}).get("backend", "vertex"),
    )
    updates["llm_backend"] = backend

    default_model = cfg.get("llm", {}).get("model") or _MODEL_HINTS.get(backend, "claude-sonnet-5")
    if backend == "echo":
        updates["llm_model"] = default_model
        updates["llm_scenario_model"] = cfg.get("llm", {}).get("scenario_model") or default_model
        _note("Echo needs no credentials — skipping model/key questions.")
    else:
        updates["llm_model"] = _ask("Default model id", default_model)
        updates["llm_scenario_model"] = _ask(
            "Scenario model id (AIRS demos)",
            cfg.get("llm", {}).get("scenario_model") or updates["llm_model"],
        )

    # Provider-specific bits.
    updates["vertex_project"] = cfg.get("llm", {}).get("vertex_project") or "your-gcp-project"
    updates["vertex_region"] = cfg.get("llm", {}).get("vertex_region") or "global"
    updates["bedrock_region"] = cfg.get("llm", {}).get("bedrock_region", "")

    if backend == "vertex":
        updates["vertex_project"] = _ask("GCP project id for Vertex", updates["vertex_project"])
        updates["vertex_region"] = _ask("Vertex region", updates["vertex_region"])
        _note("Vertex uses Application Default Credentials: run "
              "`gcloud auth application-default login` before starting Sirius.")
    elif backend == "direct":
        secret = _ask_secret("ANTHROPIC_API_KEY", already_set=bool(env.get("ANTHROPIC_API_KEY")))
        if secret:
            updates["_env"]["ANTHROPIC_API_KEY"] = secret
    elif backend == "bedrock":
        updates["bedrock_region"] = _ask(
            "Bedrock region", updates["bedrock_region"] or os.environ.get("AWS_REGION", "us-east-1")
        )
        _note("Bedrock uses your AWS credentials (env / profile / SSO). "
              "Configure them in the Cloud section or via `aws sso login`.")

    updates["max_tokens"] = str(cfg.get("llm", {}).get("max_tokens", 16384))


def _section_airs(cfg: dict, env: dict, updates: dict) -> None:
    _hdr("Prisma AIRS — AI Runtime Security scanning")
    enabled = _ask_bool("Enable AIRS scanning by default?",
                        cfg.get("airs", {}).get("enabled", True))
    updates["airs_enabled"] = enabled
    updates["airs_profile"] = cfg.get("airs", {}).get("profile_name", "your-airs-profile")
    updates["airs_endpoint"] = cfg.get("airs", {}).get(
        "api_endpoint", "https://service.api.aisecurity.paloaltonetworks.com")
    if enabled:
        updates["airs_profile"] = _ask("AIRS profile name", updates["airs_profile"])
        updates["airs_endpoint"] = _ask("AIRS API endpoint", updates["airs_endpoint"])
        secret = _ask_secret("PANW_AI_SEC_API_KEY", already_set=bool(env.get("PANW_AI_SEC_API_KEY")))
        if secret:
            updates["_env"]["PANW_AI_SEC_API_KEY"] = secret


def _section_cortex(cfg: dict, env: dict, updates: dict) -> None:
    _hdr("Cortex XDR platform")
    cur = cfg.get("cortex", {})
    live = _ask_bool("Connect to a live Cortex tenant? (No = mock mode for demos)",
                     not cur.get("mock_mode", True))
    updates["cortex_mock_mode"] = not live
    updates["cortex_fqdn"] = cur.get("fqdn", "")
    updates["cortex_advanced_auth"] = cur.get("advanced_auth", True)
    if live:
        updates["cortex_fqdn"] = _ask(
            "Cortex API FQDN (e.g. https://api-tenant.xdr.us.paloaltonetworks.com)",
            updates["cortex_fqdn"])
        updates["cortex_advanced_auth"] = _ask_bool(
            "Use Advanced auth? (No = Standard)", updates["cortex_advanced_auth"])
        key = _ask_secret("CORTEX_API_KEY", already_set=bool(env.get("CORTEX_API_KEY")))
        if key:
            updates["_env"]["CORTEX_API_KEY"] = key
        kid = _ask_secret("CORTEX_API_KEY_ID", already_set=bool(env.get("CORTEX_API_KEY_ID")))
        if kid:
            updates["_env"]["CORTEX_API_KEY_ID"] = kid

    # cortex-mcp bridge
    mcp = cfg.get("cortex_mcp", {})
    updates["cortex_mcp_enabled"] = _ask_bool(
        "Enable the cortex-mcp bridge? (external cortex-mcp server over stdio)",
        mcp.get("enabled", True))
    updates["cortex_mcp_command"] = mcp.get("command", "")
    if updates["cortex_mcp_enabled"]:
        updates["cortex_mcp_command"] = _ask(
            "Path to the cortex-mcp venv python (blank = use current interpreter)",
            updates["cortex_mcp_command"])


def _section_cloud(cfg: dict, env: dict, updates: dict) -> None:
    _hdr("Backend cloud provider — where infrastructure gets deployed")
    provider = _ask_choice(
        "Primary cloud provider for Terraform / demos",
        [
            ("aws", "Amazon Web Services (Terraform vuln-infra / eks / kali-box, SSM)"),
            ("azure", "Microsoft Azure (`az login` device-code flow)"),
            ("gcp", "Google Cloud (project/region; TF projects are AWS-focused today)"),
            ("none", "None / decide later"),
        ],
        default_key="aws",
    )

    # AWS (also used by the Bedrock backend for creds).
    aws = cfg.get("aws", {})
    updates["aws_profile"] = aws.get("profile", "sirius")
    updates["aws_sso_start_url"] = aws.get("sso_start_url", "")
    updates["aws_sso_region"] = aws.get("sso_region", "")
    updates["aws_account_id"] = aws.get("account_id", "")
    updates["aws_role_name"] = aws.get("role_name", "")
    updates["aws_region"] = aws.get("region", "")

    az = cfg.get("azure", {})
    updates["azure_tenant_id"] = az.get("tenant_id", "")
    updates["azure_subscription_id"] = az.get("subscription_id", "")
    updates["azure_location"] = az.get("location", "")

    if provider == "aws":
        _note("AWS credentials come from `aws sso login` / your environment — "
              "the wizard never asks for access keys. These just name the target.")
        updates["aws_profile"] = _ask("AWS profile name", updates["aws_profile"])
        updates["aws_sso_start_url"] = _ask("SSO start URL", updates["aws_sso_start_url"])
        updates["aws_sso_region"] = _ask("SSO region (Identity Center)", updates["aws_sso_region"])
        updates["aws_account_id"] = _ask("Target 12-digit account id", updates["aws_account_id"])
        updates["aws_role_name"] = _ask("Role / permission set", updates["aws_role_name"])
        updates["aws_region"] = _ask("Default region", updates["aws_region"] or updates["aws_sso_region"])
    elif provider == "azure":
        _note("Azure sign-in uses `az login --use-device-code`; SP secrets (if any) "
              "go in .env, not here.")
        updates["azure_tenant_id"] = _ask("Azure tenant id", updates["azure_tenant_id"])
        updates["azure_subscription_id"] = _ask("Subscription id", updates["azure_subscription_id"])
        updates["azure_location"] = _ask("Default location (e.g. southeastasia)",
                                         updates["azure_location"])
    elif provider == "gcp":
        proj = _ask("GCP project id", updates.get("vertex_project", "your-gcp-project"))
        region = _ask("GCP region", updates.get("vertex_region", "global"))
        # Persist to .env so gcloud/TF pick them up; keep config.yaml provider-agnostic.
        updates["_env"]["GOOGLE_CLOUD_PROJECT"] = proj
        updates["_env"]["CLOUD_ML_REGION"] = region
        _note("Saved GOOGLE_CLOUD_PROJECT / CLOUD_ML_REGION to .env. "
              "Sirius' Terraform projects target AWS today; GCP deploys are BYO.")


def _section_safety(cfg: dict, env: dict, updates: dict) -> None:
    _hdr("Safety gates")
    updates["infra_require_auth"] = _ask_bool(
        "Require human authorization for destructive infra ops? (strongly recommended)",
        cfg.get("infra", {}).get("require_authorization", True))
    print(f"{C_YELLOW}The intrusive purple-team path can run real attacks against a target "
          f"you authorize.{C_RESET}")
    updates["purpleteam_intrusive"] = _ask_bool(
        "Allow the intrusive purple-team path? (per-run human confirmation still required)",
        cfg.get("purpleteam", {}).get("allow_intrusive", False))
    # Carry browser defaults through unchanged.
    br = cfg.get("browser", {})
    updates["browser_enabled"] = br.get("enabled", True)
    updates["browser_allowed_hosts"] = br.get("allowed_hosts", ["localhost", "127.0.0.1"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> int:
    if not (os.isatty(0) and os.isatty(1)):
        print("sirius onboard is interactive — run it in a terminal (TTY).")
        return 1

    cfg_path = _config_path()
    env_path = _env_path()

    print(f"{C_BOLD}Sirius onboarding{C_RESET}")
    _note(f"Writing config -> {cfg_path}")
    _note(f"Writing secrets -> {env_path} (chmod 600, git-ignored)")
    _note("Press Enter to accept the [default] shown for any question.\n")

    existing_cfg: dict = {}
    if cfg_path.exists():
        try:
            existing_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            existing_cfg = {}
    env = _read_env(env_path)

    updates: dict = {"_env": {}}
    _section_llm(existing_cfg, env, updates)
    _section_airs(existing_cfg, env, updates)
    _section_cortex(existing_cfg, env, updates)
    _section_cloud(existing_cfg, env, updates)
    _section_safety(existing_cfg, env, updates)

    env_updates = updates.pop("_env")

    # Summary before writing.
    _hdr("Review")
    print(f"  LLM backend      : {updates['llm_backend']}  (model {updates['llm_model']})")
    print(f"  AIRS enabled     : {updates['airs_enabled']}")
    print(f"  Cortex mock mode : {updates['cortex_mock_mode']}"
          + (f"  fqdn {updates['cortex_fqdn']}" if updates['cortex_fqdn'] else ""))
    print(f"  Purple intrusive : {updates['purpleteam_intrusive']}")
    secret_keys = ", ".join(sorted(env_updates)) or "(none changed)"
    print(f"  Secrets to .env  : {secret_keys}")

    if not _ask_bool("\nWrite these settings now?", True):
        print("Aborted — nothing written.")
        return 1

    # Back up an existing config.yaml before overwriting.
    if cfg_path.exists():
        backup = cfg_path.with_suffix(".yaml.bak")
        shutil.copy2(cfg_path, backup)
        _note(f"Backed up previous config to {backup}")

    cfg_path.write_text(_render_config(updates), encoding="utf-8")
    _merge_env(env_path, env_updates)

    print(f"\n{C_GREEN}Done.{C_RESET} Next steps:")
    if updates["llm_backend"] == "vertex":
        print("  • gcloud auth application-default login")
    elif updates["llm_backend"] == "bedrock":
        print("  • aws sso login   (or export AWS credentials)")
    print("  • cd backend && sirius serve      # http://127.0.0.1:5173")
    return 0
