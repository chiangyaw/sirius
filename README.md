# Sirius

A dark-mode, multi-agent, multi-skill **security demo platform**. Prompt agents on
the left; watch the **live event flow** on the right — every LLM call, tool call,
Prisma AIRS verdict, Terraform line, and scan finding streamed over a WebSocket so
you can *learn how it works* and *showcase the flow* in a demo.

Backend: **FastAPI** (Python). LLM: **Claude on GCP Vertex AI**. Frontend:
**React + Vite + TypeScript + Tailwind**.

## Features

1. **Prisma AIRS scenarios** — bank / telco / healthcare agents that will leak
   another customer's data to a prompt-injection attack **unless** the Prisma AIRS
   toggle is on, in which case the malicious prompt is blocked before the model.
2. **Purple Team scans** — safe local recon (fingerprint, endpoint/API discovery,
   exposure checks) always available; an **intrusive** Kali-on-AWS path that is
   double-gated (config flag + human authorization of the exact target).
3. **Cortex** — an API client + **MCP server** (incidents, alerts, endpoints,
   isolate, XQL), plus helpers to deploy the Cortex k8s connector (Helm) to EKS and
   run a detectable attack simulation.
4. **Terraform build & destroy** — in-app apply/plan/destroy that streams output as
   events, honors **temporary AWS credentials (AWS_SESSION_TOKEN)**, and ships
   `vuln-infra`, `eks`, and `kali-box` projects.

## Quick start

### Backend
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[all]'          # or: pip install -e .   (core only)
sirius onboard                   # interactive setup — writes config.yaml + .env
sirius serve                     # http://127.0.0.1:5173 (serves API + built UI)
```

### Onboarding wizard

`sirius onboard` (alias `sirius init`) is an interactive wizard — the easiest way
to get set up. It asks how you want to run Sirius and writes the answers to the
right place, honoring the two-tier config split:

- **Non-secret settings → `config.yaml`** (safe to commit; rendered with its
  documented inline comments intact).
- **Secrets / API tokens → `.env`** (git-ignored, written `chmod 600`, existing
  keys and comments preserved).

By design it **never asks for cloud credentials** — AWS/Azure/GCP sign-in stays
with `aws sso login` / `az login` / `gcloud auth application-default login` and
the in-app buttons; the wizard only records *which* account/tenant/project to
target. It **never echoes a secret** (masked entry, shows only "already set / not
set"), and only genuine API tokens with no interactive login (Anthropic, Prisma
AIRS, Cortex) are stored in `.env`.

It walks through five sections:

1. **Claude access** — pick the LLM backend:
   - `vertex` — Claude on GCP Vertex AI (credentials via ADC); prompts for GCP
     project + region.
   - `direct` — the Anthropic API; prompts for `ANTHROPIC_API_KEY` (→ `.env`).
   - `bedrock` — Amazon Bedrock; prompts for the Bedrock region (creds from your
     AWS chain).
   - `echo` — offline, no LLM (UI/event flow only, no real answers).

   Then the default + scenario model ids (with sensible per-provider defaults).
2. **Prisma AIRS** — enable scanning, profile name, API endpoint, and
   `PANW_AI_SEC_API_KEY` (→ `.env`).
3. **Cortex** — connect a live tenant (FQDN, Advanced vs Standard auth,
   `CORTEX_API_KEY` + `CORTEX_API_KEY_ID` → `.env`) or stay in mock mode, plus
   whether to enable the cortex-mcp bridge and where its interpreter lives.
4. **Backend cloud provider** — AWS (SSO start URL, region, account id, role),
   Azure (tenant/subscription/location), or GCP (project/region → `.env`) for
   infrastructure deploys.
5. **Safety gates** — require human authorization for destructive infra ops, and
   whether to allow the intrusive purple-team path (both default to safe).

It shows a review summary, then writes on confirmation. Re-run it any time to
change settings — it backs up the previous `config.yaml` to `config.yaml.bak`
first. Prefer manual setup? `cp .env.example .env` and edit `config.yaml` by hand
instead.

Vertex needs GCP Application Default Credentials:
`gcloud auth application-default login` and set `llm.vertex_project` in
`config.yaml` (or `GOOGLE_CLOUD_PROJECT` in `.env`). Without a working backend,
Sirius falls back to a no-network **Echo** backend so the UI/event flow still works.

#### Rotating the Vertex project

To switch the Vertex project without editing committed config, set an override
in `backend/.env` (git-ignored) and restart:

```bash
SIRIUS_VERTEX_PROJECT=prod-<new-id>   # wins over config.yaml
# SIRIUS_VERTEX_REGION=global          # only if the region changes too
```

Resolution order (first non-empty wins): `SIRIUS_VERTEX_PROJECT` →
`llm.vertex_project` (config.yaml) → `ANTHROPIC_VERTEX_PROJECT_ID` →
`GOOGLE_CLOUD_PROJECT`. After rotating, run `gcloud auth login` for the new
project. A stale project shows as `403 PERMISSION_DENIED / CONSUMER_INVALID`.

### Frontend
```bash
cd frontend
npm install
npm run dev                      # http://localhost:5173 (proxies /api + /ws to :8010)
# run the backend it proxies to, on :8010:
(cd ../backend && sirius serve --port 8010)
# or build into the backend to serve everything from :5173 (one process):
npm run build && (cd ../backend && sirius serve)
```

## Configuration

- **`backend/config.yaml`** — non-secret settings (LLM, AIRS, Cortex, purple team).
  Safe to commit.
- **`backend/.env`** — secrets only (git-ignored). See `.env.example`.

## Cortex MCP server

```bash
python -m sirius.cortex.mcp_server          # stdio MCP server
```
Register it in any MCP client (Claude Desktop/Code). Honors `cortex.mock_mode`.

> **Requires Python ≥ 3.10** (the `mcp` SDK does). The rest of Sirius runs on 3.9+.

**Cortex API key scopes.** Sirius auto-detects Advanced vs Standard auth. The key's
role (SBAC) must include the scopes you use:
- incidents + alerts + XQL → attack reports & queries
- **endpoints (Endpoint Administrator)** → required for isolate / scan / list.
  Without it those calls return an SBAC-blocked error (surfaced clearly in the UI).

Note: creating prevention **profiles/policies is not exposed** by the Cortex
public API — that stays a console action.

## Safety

The intrusive purple-team path only runs against targets a human has explicitly
authorized (`POST /api/purpleteam/authorize` with the confirmation phrase) **and**
only when `purpleteam.allow_intrusive: true`. The `vuln-infra` / `eks` projects are
**intentionally vulnerable** — deploy them only in an account you own and destroy
them when finished.
