import { useEffect, useMemo, useState } from "react";
import { deleteConversation, getConversations } from "../lib/api";
import type { Conversation } from "../lib/types";

const MODE_LABEL: Record<string, string> = {
  general: "General",
  cortex: "Cortex",
  airs: "Prisma AIRS",
  terraform: "Terraform",
  purple: "Attacker",
};

// Slide-in conversation history drawer (ChatGPT/Claude style). Lists past
// conversations grouped by recency; selecting one reopens it in the main window.
export function ConversationSidebar({
  open,
  onClose,
  onSelect,
  onNewChat,
  activeSessionId,
}: {
  open: boolean;
  onClose: () => void;
  onSelect: (c: Conversation) => void;
  onNewChat: () => void;
  activeSessionId: string;
}) {
  const [convos, setConvos] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [err, setErr] = useState("");

  async function refresh() {
    setErr("");
    setLoading(true);
    try {
      const r = await getConversations();
      setConvos(r.conversations);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!open) return;
    refresh();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return convos;
    return convos.filter(
      (c) =>
        c.title.toLowerCase().includes(q) ||
        MODE_LABEL[c.mode]?.toLowerCase().includes(q) ||
        (c.scenario ?? "").toLowerCase().includes(q)
    );
  }, [convos, query]);

  // Group by recency buckets for the classic sidebar layout.
  const groups = useMemo(() => groupByRecency(filtered), [filtered]);

  async function onDelete(e: React.MouseEvent, c: Conversation) {
    e.stopPropagation();
    await deleteConversation(c.session_id);
    refresh();
  }

  return (
    <>
      {/* Backdrop */}
      <div
        className={`fixed inset-0 z-40 bg-black/50 transition-opacity ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
        onClick={onClose}
      />
      {/* Drawer */}
      <aside
        className={`fixed left-0 top-0 z-50 flex h-full w-[340px] max-w-[85vw] flex-col border-r border-edge bg-panel shadow-2xl transition-transform duration-200 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-center gap-2 border-b border-edge px-4 py-3">
          <h2 className="text-sm font-semibold tracking-wide">Conversations</h2>
          <span className="text-[11px] text-muted">{convos.length}</span>
          <button
            onClick={onClose}
            className="ml-auto rounded px-2 py-0.5 text-muted transition hover:bg-panel2 hover:text-slate-200"
            title="Close"
          >
            ✕
          </button>
        </div>

        <div className="space-y-2 border-b border-edge px-3 py-3">
          <button
            onClick={onNewChat}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-3 py-2 text-sm font-semibold text-white transition hover:opacity-90"
          >
            + New chat
          </button>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search conversations…"
            className="w-full rounded border border-edge bg-base px-2 py-1.5 text-xs outline-none focus:border-accent"
          />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {loading && <p className="p-3 text-sm text-muted">Loading…</p>}
          {err && <p className="p-3 text-sm text-rose-400">⚠️ {err}</p>}
          {!loading && !err && filtered.length === 0 && (
            <p className="mt-10 px-3 text-center text-sm text-muted">
              {convos.length === 0
                ? "No conversations yet. Send a prompt to start one."
                : "No conversations match your search."}
            </p>
          )}
          {groups.map(({ label, items }) => (
            <div key={label} className="mb-3">
              <p className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-muted">
                {label}
              </p>
              {items.map((c) => (
                <button
                  key={c.session_id}
                  onClick={() => onSelect(c)}
                  className={`group flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left transition ${
                    c.session_id === activeSessionId
                      ? "bg-accent/15"
                      : "hover:bg-panel2"
                  }`}
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-slate-200">{c.title}</p>
                    <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-muted">
                      <span className="rounded-full border border-edge px-1.5 py-0.5">
                        {MODE_LABEL[c.mode] ?? c.mode}
                        {c.scenario ? ` · ${c.scenario}` : ""}
                      </span>
                      <span>{relativeTime(c.updated_ts)}</span>
                      <span>· {c.count} msg</span>
                    </div>
                  </div>
                  <span
                    onClick={(e) => onDelete(e, c)}
                    title="Delete conversation"
                    className="rounded px-1.5 py-1 text-muted opacity-0 transition hover:bg-rose-500/15 hover:text-rose-400 group-hover:opacity-100"
                  >
                    🗑
                  </span>
                </button>
              ))}
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}

// ── Recency grouping + relative-time helpers ────────────────────────────────
function startOfToday(): number {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function groupByRecency(convos: Conversation[]): { label: string; items: Conversation[] }[] {
  const today = startOfToday();
  const day = 86_400_000;
  const buckets: Record<string, Conversation[]> = {
    Today: [],
    Yesterday: [],
    "Previous 7 days": [],
    Older: [],
  };
  for (const c of convos) {
    if (c.updated_ts >= today) buckets["Today"].push(c);
    else if (c.updated_ts >= today - day) buckets["Yesterday"].push(c);
    else if (c.updated_ts >= today - 7 * day) buckets["Previous 7 days"].push(c);
    else buckets["Older"].push(c);
  }
  return Object.entries(buckets)
    .filter(([, items]) => items.length > 0)
    .map(([label, items]) => ({ label, items }));
}

function relativeTime(ts: number): string {
  const diff = Date.now() - ts;
  const min = Math.floor(diff / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const days = Math.floor(hr / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(ts).toLocaleDateString();
}
