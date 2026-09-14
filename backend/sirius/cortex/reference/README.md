# Cortex documentation reference

Local mirror of the public Cortex Documentation Portal
(`https://cortex-docs.paloaltonetworks.com`), kept here because the official
GitBook **MCP server is blocked by enterprise MCP policy**. WebFetch is *not*
blocked, so on-demand access still works — these files are the map.

## Files
- `cortex-docs-index.md` — the site's `llms.txt`: every doc page as a
  `[title](url.md): description` line (~21k entries). This is a **grep target**,
  not a read-whole file.
- `cortex-docs-full.md` — the site's `llms-full.txt`: a single-file body dump of
  the core/home content for fully-offline lookup.

## How to use it when generating reports
1. `grep -i "<topic>" cortex-docs-index.md` to find the relevant page(s).
   e.g. `grep -iE "xql|compliance|api-overview|issue" cortex-docs-index.md`
2. Every page has a Markdown variant: append `.md` to the URL (the index links
   already do this). Fetch it with WebFetch to get authoritative content.
   Verified: `.../xql-command-reference-guide/readme.md` → 200.
3. Cite the page title/URL in the report so claims are traceable.

## Product areas covered
XSIAM · XDR · XDR Agent · Cortex Cloud (runtime / posture / k8s / appsec) ·
AgentiX · Data Security · XSOAR · Xpanse · API references (XDR/XSIAM/XSOAR/Cloud)
· XQL schema & command reference · compliance · data-model schema.

## Refreshing
Docs change; re-pull periodically:
```bash
curl -sSL https://cortex-docs.paloaltonetworks.com/llms.txt      -o cortex-docs-index.md
curl -sSL https://cortex-docs.paloaltonetworks.com/llms-full.txt -o cortex-docs-full.md
```
Last pulled: 2026-08-11.
