import { useEffect, useRef } from "react";
import type { SiriusEvent } from "../lib/types";
import { EventCard } from "./EventCard";

export function EventStream({
  events,
  connected,
  onClear,
  onMinimize,
}: {
  events: SiriusEvent[];
  connected: boolean;
  onClear: () => void;
  onMinimize?: () => void;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  return (
    <div className="flex h-full flex-col bg-panel">
      <div className="flex items-center gap-2 border-b border-edge px-4 py-3">
        <h2 className="text-sm font-semibold tracking-wide text-slate-200">
          Event Flow
        </h2>
        <span
          className={`flex items-center gap-1 text-[10px] ${
            connected ? "text-emerald-400" : "text-rose-400"
          }`}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              connected ? "bg-emerald-400" : "bg-rose-400"
            }`}
          />
          {connected ? "live" : "offline"}
        </span>
        <span className="ml-auto text-[10px] text-muted">{events.length} events</span>
        <button
          onClick={onClear}
          className="rounded border border-edge px-2 py-0.5 text-[10px] text-muted hover:bg-panel2"
        >
          clear
        </button>
        {onMinimize && (
          <button
            onClick={onMinimize}
            title="Minimize event flow"
            className="rounded border border-edge px-2 py-0.5 text-[10px] text-muted hover:bg-panel2 hover:text-slate-200"
          >
            ⟩⟩
          </button>
        )}
      </div>
      <div className="flex-1 space-y-1.5 overflow-y-auto p-3">
        {events.length === 0 && (
          <div className="mt-10 text-center text-sm text-muted">
            Events will appear here as the agent works.
            <br />
            Prompt, scan, tool calls, AIRS verdicts — all live.
          </div>
        )}
        {events.map((ev) => (
          <EventCard key={ev.id + ev.seq} ev={ev} />
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}
