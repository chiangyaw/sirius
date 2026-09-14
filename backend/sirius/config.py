"""
Configuration for Sirius.

Two-tier split (same principle as Alfred):
  - Non-secret settings  -> config.yaml (safe to commit)
  - Secrets / tokens      -> .env (git-ignored), loaded into the environment

Nothing in Sirius reads config files directly; everything receives the typed
`SiriusConfig` object from `load_config()`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

_CONFIG_FILENAME = "config.yaml"


class LLMConfig(BaseModel):
    backend: str = "vertex"                       # only "vertex" for now (Claude on Vertex AI)
    model: str = "claude-sonnet-5"                # default/general Vertex Claude model id
    scenario_model: str = ""                       # Prisma AIRS scenario agents (falls back to model)
    vertex_project: str = ""                       # falls back to env at runtime
    vertex_region: str = "us-east5"                # falls back to env at runtime
    max_tokens: int = 4096
    max_steps: int = 20                            # tool-use loop ceiling per turn (multi-step provisioning)


class AIRSConfig(BaseModel):
    """Prisma AIRS (AI Runtime Security) inspection settings.

    The API token is NEVER stored here — it is read from PANW_AI_SEC_API_KEY.
    """
    enabled: bool = True
    profile_name: str = "sirius-security-profile"
    app_name: str = "sirius"
    api_endpoint: str = "https://service.api.aisecurity.paloaltonetworks.com"
    fail_open: bool = True


class CortexConfig(BaseModel):
    """Cortex XDR tenant settings. API key + key id come from the environment."""
    fqdn: str = ""                # e.g. https://api-tenant.xdr.us.paloaltonetworks.com
    advanced_auth: bool = True    # Advanced (nonce+timestamp+hash) vs Standard auth
    mock_mode: bool = True
    # Dir holding downloaded Cortex Cloud k8s security-profile pairs
    # (*.auth.json + *.values.yaml) used by cortex_deploy_k8s_konnector.
    # Resolved relative to the backend root if not absolute.
    konnector_profile_dir: str = "cortex-profiles"
    # Dir where downloaded agent installers (e.g. Windows MSI) are written by
    # cortex_download_distribution. Resolved relative to the backend root if not absolute.
    distribution_download_dir: str = "cortex-distributions"


class CortexMcpConfig(BaseModel):
    """External cortex-mcp server (github.com/chiangyaw/cortex-mcp) that Sirius
    consumes as an MCP *client* over stdio. Auth comes from the same CORTEX_*
    environment variables (passed through to the server subprocess)."""
    enabled: bool = True
    command: str = ""                                    # defaults to sys.executable
    args: list[str] = Field(default_factory=lambda: ["-m", "cortex_mcp"])


class PurpleTeamConfig(BaseModel):
    allow_intrusive: bool = False  # master gate; per-run confirmation still required


class BrowserConfig(BaseModel):
    """Headless-browser (Playwright) skill settings.

    Dual-use gate: `enabled` is the master switch and `allowed_hosts` is a
    per-navigation allow-list. A navigation to a host not on the list is refused
    (returns a refusal dict, never raises) — same convention as PurpleTeam.
    """
    enabled: bool = True
    headless: bool = True
    # Hostnames an agent may navigate to. Empty list = allow any host.
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    # Max concurrent per-session browser contexts kept alive at once.
    max_sessions: int = 4


class InfraConfig(BaseModel):
    """Least-privilege policy for destructive infrastructure ops (terraform
    apply/destroy, k8s mutations). Enforced by sirius.infra_gate."""
    # Master gate: when true, destructive ops require human authorization of the
    # exact resource via POST /api/infra/authorize before an agent may run them.
    require_authorization: bool = True
    # Optional override of the code-default per-agent capability bundles, keyed by
    # agent name (e.g. {"attacker": ["purpleteam_recon", ...]}). Empty = use defaults.
    agent_capabilities: dict[str, list[str]] = Field(default_factory=dict)
    # Per-agent terraform-project allow-list, keyed by agent name. "*" = all
    # projects. An agent not listed here defaults to ["*"].
    agent_projects: dict[str, list[str]] = Field(default_factory=dict)


class AWSConfig(BaseModel):
    """AWS SSO settings for the web-UI-driven `aws sso login` flow.

    All fields are NON-SECRET (safe to commit): they describe *which* SSO
    account/role to log into, not any credential. The actual SSO token is minted
    by the aws CLI into ~/.aws/sso/cache/; nothing secret is stored here.
    """
    profile: str = "sirius"        # ~/.aws/config profile name Sirius creates/uses
    sso_start_url: str = ""        # e.g. https://myorg.awsapps.com/start
    sso_region: str = ""           # region hosting IAM Identity Center
    account_id: str = ""           # 12-digit target account
    role_name: str = ""            # permission set / role to assume
    region: str = ""               # default region for the profile (falls back to sso_region)

    def configured(self) -> bool:
        """True when enough is set to drive `aws sso login`."""
        return bool(self.sso_start_url and self.account_id and self.role_name)


class AzureConfig(BaseModel):
    """Azure CLI settings for the web-UI-driven `az login --use-device-code` flow.

    All fields are NON-SECRET (safe to commit): they name WHICH tenant/subscription
    to target, not any credential. The az CLI mints and caches the token itself
    into ~/.azure/; nothing secret is stored here. Service-principal secrets (e.g.
    ARM_CLIENT_SECRET) live ONLY in .env / the environment.
    """
    tenant_id: str = ""            # optional --tenant for `az login`
    subscription_id: str = ""      # subscription to select after login
    location: str = ""             # default region for TF variables / demos

    def configured(self) -> bool:
        """True when a tenant or subscription is pinned (login works without either)."""
        return bool(self.tenant_id or self.subscription_id)


class SiriusConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    airs: AIRSConfig = Field(default_factory=AIRSConfig)
    cortex: CortexConfig = Field(default_factory=CortexConfig)
    cortex_mcp: CortexMcpConfig = Field(default_factory=CortexMcpConfig)
    purpleteam: PurpleTeamConfig = Field(default_factory=PurpleTeamConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    aws: AWSConfig = Field(default_factory=AWSConfig)
    azure: AzureConfig = Field(default_factory=AzureConfig)
    infra: InfraConfig = Field(default_factory=InfraConfig)

    # Resolved lazily from config + environment.
    def vertex_project(self) -> Optional[str]:
        # SIRIUS_VERTEX_PROJECT (.env) wins over config.yaml so the project can be
        # rotated without touching committed config — just edit .env and restart.
        return (
            os.environ.get("SIRIUS_VERTEX_PROJECT")
            or self.llm.vertex_project
            or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or None
        )

    def vertex_region(self) -> str:
        # SIRIUS_VERTEX_REGION (.env) wins over config.yaml (same rationale).
        return (
            os.environ.get("SIRIUS_VERTEX_REGION")
            or self.llm.vertex_region
            or os.environ.get("CLOUD_ML_REGION")
            or os.environ.get("ANTHROPIC_VERTEX_REGION")
            or "us-east5"
        )


def _default_config_path() -> Path:
    """config.yaml sits alongside the backend project root (parent of the package)."""
    return Path(__file__).resolve().parent.parent / _CONFIG_FILENAME


def load_config(config_path: Optional[Path] = None) -> SiriusConfig:
    """Load .env, then config.yaml, into a typed SiriusConfig.

    Search order for config.yaml:
      1. explicit config_path argument
      2. SIRIUS_CONFIG_PATH environment variable
      3. <backend root>/config.yaml
    Missing file -> all defaults (Sirius still boots).
    """
    _load_dotenv()

    if config_path is None:
        env_path = os.environ.get("SIRIUS_CONFIG_PATH")
        config_path = Path(env_path) if env_path else _default_config_path()

    raw: dict = {}
    if config_path and config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    cfg = SiriusConfig(**raw)
    # Normalise the AIRS endpoint (no trailing slash).
    cfg.airs.api_endpoint = cfg.airs.api_endpoint.rstrip("/")
    # FQDN may also come from the environment (CORTEX_FQDN) for convenience.
    cfg.cortex.fqdn = (cfg.cortex.fqdn or os.environ.get("CORTEX_FQDN", "")).rstrip("/")
    return cfg


def _load_dotenv() -> None:
    """Load the nearest .env into the environment (best-effort, never overrides)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # Look for .env next to config.yaml (backend root) and in cwd.
    for candidate in (_default_config_path().parent / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
