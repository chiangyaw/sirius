import { useState } from "react";
import type { SiriusEvent, Severity } from "../lib/types";

const SEV_STYLES: Record<Severity, string> = {
  info: "border-l-sky-500/70 bg-sky-500/5",
  success: "border-l-emerald-500/70 bg-emerald-500/5",
  warn: "border-l-amber-500/70 bg-amber-500/5",
  danger: "border-l-rose-500/80 bg-rose-500/10",
};

const TYPE_LABEL: Record<string, string> = {
  user_prompt: "PROMPT",
  agent_start: "AGENT",
  agent_end: "AGENT",
  llm_request: "LLM →",
  llm_response: "LLM ←",
  airs_scan: "AIRS",
  tool_call: "TOOL →",
  tool_result: "TOOL ←",
  skill_invoked: "SKILL",
  terraform_step: "TF",
  engineer_step: "SDK",
  aws_sso: "AWS SSO",
  azure_login: "AZURE",
  azure_step: "AZ",
  scan_finding: "SCAN",
  blocked: "BLOCKED",
  error: "ERROR",
  status: "STATUS",
};

const DOT: Record<Severity, string> = {
  info: "bg-sky-400",
  success: "bg-emerald-400",
  warn: "bg-amber-400",
  danger: "bg-rose-400",
};

// Collapse the full text to a one-line preview; the full text shows on expand.
const SUMMARY_LEN = 140;
function summarize(text: string): string {
  const oneLine = text.replace(/\s+/g, " ").trim();
  return oneLine.length > SUMMARY_LEN ? oneLine.slice(0, SUMMARY_LEN) + "…" : oneLine;
}

export function EventCard({ ev }: { ev: SiriusEvent }) {
  const [open, setOpen] = useState(false);
  const hasPayload = ev.payload && Object.keys(ev.payload).length > 0;
  const time = new Date(ev.ts * 1000).toLocaleTimeString();

  // Prefer a text payload (llm_response / user_prompt) as the full detail; else the title.
  const fullText = typeof ev.payload?.text === "string" ? ev.payload.text : ev.title;
  const loginUrl = typeof ev.payload?.url === "string" ? ev.payload.url : null;
  const userCode = typeof ev.payload?.user_code === "string" ? ev.payload.user_code : null;
  const summary = summarize(fullText);
  const truncated = summary !== fullText.replace(/\s+/g, " ").trim();
  const expandable = hasPayload || truncated;

  return (
    <div
      className={`event-in border-l-2 ${SEV_STYLES[ev.severity]} rounded-r px-3 py-2 text-sm`}
    >
      <div
        className={`flex items-start gap-2 ${expandable ? "cursor-pointer" : ""}`}
        onClick={() => expandable && setOpen((o) => !o)}
      >
        <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${DOT[ev.severity]}`} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-semibold tracking-wider text-muted">
              {TYPE_LABEL[ev.type] || ev.type.toUpperCase()}
            </span>
            <span className="text-[10px] text-muted/60">{ev.agent}</span>
            <span className="ml-auto text-[10px] text-muted/50">{time}</span>
          </div>
          <div className="break-words text-[13px] leading-snug text-slate-200">
            {open ? (
              <span className="whitespace-pre-wrap">{fullText}</span>
            ) : (
              summary
            )}
          </div>
          {loginUrl && (
            <a
              href={loginUrl}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="mt-1 inline-block break-all rounded bg-amber-500/15 px-2 py-1 text-[12px] font-medium text-amber-300 underline decoration-dotted hover:bg-amber-500/25"
            >
              Sign in to AWS ↗{userCode ? ` (code ${userCode})` : ""}
            </a>
          )}
        </div>
        {expandable && (
          <span className="mt-0.5 text-muted/60">{open ? "▾" : "▸"}</span>
        )}
      </div>
      {open && hasPayload && (
        <pre className="mt-2 max-h-72 overflow-auto rounded bg-black/40 p-2 text-[11px] leading-relaxed text-slate-300">
          {JSON.stringify(ev.payload, null, 2)}
        </pre>
      )}
    </div>
  );
}
