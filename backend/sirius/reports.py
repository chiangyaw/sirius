"""
Findings report skill — turn a run's findings into a downloadable Markdown file.

`generate_report` assembles a clean Markdown document from a title, an optional
executive summary, an optional list of *structured* findings (each with a
severity, description, and recommendation), and an optional freeform Markdown
body. The file is written under the reports directory and the skill returns a
`download_url` (served by GET /api/reports/{filename}). The agent is expected to
relay that URL back to the user as a Markdown link, so the report is one click
away in the chat UI — no need to hunt through a directory.

Reports dir: $SIRIUS_REPORTS_DIR if set, else <backend root>/reports.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sirius.events import current_agent, current_session, emit
from sirius.skills import registry

# Severity ordering for sorting findings most-severe first, plus a badge emoji.
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SEVERITY_BADGE = {
    "critical": "🔴 Critical",
    "high": "🟠 High",
    "medium": "🟡 Medium",
    "low": "🔵 Low",
    "info": "⚪ Info",
}


def _reports_dir() -> Path:
    """Resolve (and create) the reports directory."""
    override = os.environ.get("SIRIUS_REPORTS_DIR")
    d = Path(override) if override else Path(__file__).resolve().parents[1] / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(text: str, fallback: str = "report") -> str:
    """A filesystem-safe slug from a title (lowercase, dash-separated)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or fallback)[:60]


def _norm_severity(value: Any) -> str:
    s = str(value or "info").strip().lower()
    return s if s in _SEVERITY_RANK else "info"


def _render_finding(idx: int, f: dict) -> str:
    """One finding → a Markdown section."""
    sev = _norm_severity(f.get("severity"))
    title = str(f.get("title") or f.get("name") or f"Finding {idx}").strip()
    lines = [f"### {idx}. {title}", "", f"**Severity:** {_SEVERITY_BADGE[sev]}  "]
    if f.get("affected"):
        lines.append(f"**Affected:** {f['affected']}  ")
    if f.get("description"):
        lines += ["", str(f["description"]).strip()]
    if f.get("recommendation"):
        lines += ["", f"**Recommendation:** {str(f['recommendation']).strip()}"]
    return "\n".join(lines)


def _build_markdown(
    title: str,
    summary: str,
    findings: list[dict],
    body: str,
    session_id: Optional[str],
    agent: str,
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts: list[str] = [f"# {title.strip() or 'Findings Report'}", ""]

    meta = [f"*Generated {ts}*", f"*Agent: {agent}*"]
    if session_id:
        meta.append(f"*Session: `{session_id}`*")
    parts += ["  \n".join(meta), ""]

    if summary.strip():
        parts += ["## Summary", "", summary.strip(), ""]

    if findings:
        ordered = sorted(
            findings, key=lambda f: _SEVERITY_RANK[_norm_severity(f.get("severity"))]
        )
        # Severity tally line — a quick at-a-glance count.
        tally: dict[str, int] = {}
        for f in ordered:
            tally[_norm_severity(f.get("severity"))] = (
                tally.get(_norm_severity(f.get("severity")), 0) + 1
            )
        counts = " · ".join(
            f"{_SEVERITY_BADGE[s]}: {tally[s]}"
            for s in _SEVERITY_RANK
            if s in tally
        )
        parts += [f"## Findings ({len(ordered)})", "", counts, ""]
        for i, f in enumerate(ordered, start=1):
            parts += [_render_finding(i, f), "", "---", ""]

    if body.strip():
        parts += [body.strip(), ""]

    return "\n".join(parts).rstrip() + "\n"


def generate_report(
    title: str,
    summary: str = "",
    findings: Optional[list] = None,
    body: str = "",
    filename: str = "",
    **_: Any,
) -> dict:
    """Write a Markdown findings report and return a download link.

    Returns {ok, filename, download_url, findings, bytes}. On any error the dict
    carries ok:false and an error message (never raises into the agent loop).
    """
    try:
        title = title or "Findings Report"
        findings = [f for f in (findings or []) if isinstance(f, dict)]
        session_id = current_session.get()
        agent = current_agent.get()

        md = _build_markdown(title, summary or "", findings, body or "", session_id, agent)

        stem = _slug(filename or title)
        name = f"{stem}-{uuid.uuid4().hex[:8]}.md"
        path = _reports_dir() / name
        path.write_text(md, encoding="utf-8")

        download_url = f"/api/reports/{name}"
        emit(
            "status",
            f"📄 Report generated: {name}",
            severity="success",
            payload={"filename": name, "download_url": download_url,
                     "findings": len(findings), "title": title},
        )
        return {
            "ok": True,
            "filename": name,
            "download_url": download_url,
            "findings": len(findings),
            "bytes": len(md.encode("utf-8")),
            "note": ("Report saved. Present the download_url to the user as a Markdown "
                     f"link, e.g. [Download {name}]({download_url})."),
        }
    except Exception as e:  # noqa: BLE001 — surface to the agent, don't crash the turn
        emit("error", f"Report generation failed: {e}", severity="danger",
             payload={"error": str(e)})
        return {"ok": False, "error": str(e)}


# ── Registration ─────────────────────────────────────────────────────────────
registry.skill(
    "generate_report",
    "Generate a downloadable Markdown findings report and return a download_url. "
    "Use this when the user wants a written report of findings from a demo, test, "
    "scan, or investigation. Pass a `title`; optionally a `summary` (executive "
    "summary), a list of structured `findings`, and/or a freeform Markdown `body` "
    "for anything else. ALWAYS relay the returned download_url back to the user as a "
    "Markdown link (e.g. `[Download report](<download_url>)`) so they can download it "
    "directly from the chat.",
    {"type": "object",
     "properties": {
         "title": {"type": "string", "description": "Report title / heading."},
         "summary": {"type": "string",
                     "description": "Optional executive summary (Markdown)."},
         "findings": {
             "type": "array",
             "description": "Optional structured findings, rendered most-severe first.",
             "items": {
                 "type": "object",
                 "properties": {
                     "title": {"type": "string", "description": "Short finding title."},
                     "severity": {"type": "string",
                                  "enum": ["critical", "high", "medium", "low", "info"],
                                  "description": "Finding severity."},
                     "affected": {"type": "string",
                                  "description": "Affected asset/host/endpoint (optional)."},
                     "description": {"type": "string",
                                     "description": "What was found (Markdown)."},
                     "recommendation": {"type": "string",
                                        "description": "Recommended remediation (optional)."},
                 },
                 "required": ["title", "severity"],
             },
         },
         "body": {"type": "string",
                  "description": "Optional freeform Markdown appended after the findings "
                                 "(tables, methodology, appendices, etc.)."},
         "filename": {"type": "string",
                      "description": "Optional base filename (slugified); defaults to the title."},
     },
     "required": ["title"]},
)(generate_report)
