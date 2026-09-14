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
3. **Cortex XDR** — an API client + **MCP server** (incidents, alerts, endpoints,
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
cp .env.example .env             # fill in tokens (AIRS, Cortex, AWS, GCP)
sirius serve                     # http://127.0.0.1:5173 (serves API + built UI)
```
Vertex needs GCP Application Default Credentials:
`gcloud auth application-default login` and set `llm.vertex_project` in
`config.yaml` (or `GOOGLE_CLOUD_PROJECT` in `.env`). Without it, Sirius falls back
to a no-network **Echo** backend so the UI/event flow still works.

#### Rotating the Vertex project

The sandbox project rotates roughly every 5 weeks. To switch without editing
committed config, set an override in `backend/.env` (git-ignored) and restart:

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

Note: creating prevention **profiles/policies is not exposed** by the Cortex XDR
public API — that stays a console action.

## Safety

The intrusive purple-team path only runs against targets a human has explicitly
authorized (`POST /api/purpleteam/authorize` with the confirmation phrase) **and**
only when `purpleteam.allow_intrusive: true`. The `vuln-infra` / `eks` projects are
**intentionally vulnerable** — deploy them only in an account you own and destroy
them when finished.
