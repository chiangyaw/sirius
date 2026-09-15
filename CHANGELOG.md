# Changelog

All notable changes to Sirius are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Onboarding wizard** — `sirius onboard` (alias `sirius init`), an interactive
  setup flow that writes non-secret settings to `config.yaml` and secrets to
  `.env` (chmod 600). Covers the Claude backend, Prisma AIRS, Cortex XDR + the
  cortex-mcp bridge, the backend cloud provider (AWS / Azure / GCP), and the
  safety gates. Never asks for cloud credentials and never echoes a secret.
- **LLM backends** — `direct` (Anthropic API via `ANTHROPIC_API_KEY`) and
  `bedrock` (Amazon Bedrock) join `vertex`; `llm.backend` selects the provider
  (`vertex | direct | bedrock | echo`), with graceful Echo fallback if a backend
  can't initialize.

## [0.1.0] - 2026-09-14

Initial release of Sirius — a multi-agent, multi-skill security demo platform:
a FastAPI backend and React/Vite/TypeScript frontend with a live WebSocket
event stream as the demo centerpiece.

### Added
- **Agent + skill framework** — native Anthropic tool-use loop where every step
  emits an event; feature modules register tools in a shared skill registry.
- **Prisma AIRS scenarios** — bank / telco / healthcare agents demonstrating
  prompt-injection data-leak protection, gated by the AIRS scan toggle.
- **Purple Team** — safe local recon plus a double-gated intrusive Kali-on-AWS
  path (config flag + explicit human authorization of the exact target).
- **Cortex XDR integration** — API client and MCP server (incidents, alerts,
  endpoints, isolate, XQL) with helpers for the k8s connector and attack sims.
- **Terraform build & destroy** — in-app apply/plan/destroy that streams output
  as events and honors temporary AWS credentials (`AWS_SESSION_TOKEN`).
- **Live event stream** — typed events over `/ws/events`, surfaced in the UI.

[Unreleased]: https://github.com/chiangyaw/sirius/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/chiangyaw/sirius/releases/tag/v0.1.0
