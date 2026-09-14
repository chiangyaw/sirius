import { useEffect, useRef, useState } from "react";
import { EventStream } from "./components/EventStream";
import { Markdown } from "./components/Markdown";
import { ConversationSidebar } from "./components/ConversationSidebar";
import { useEvents } from "./lib/useEvents";
import { authorizeInfra, authorizeTarget, cancelChat, getBootstrap, getHistory, sendChat } from "./lib/api";
import type { InfraConfirm } from "./lib/api";
import { AwsAuthBar, SettingsModal } from "./components/SettingsModal";
import type { Bootstrap, ChatMessage, Conversation } from "./lib/types";

type Mode = "airs" | "purple" | "cortex" | "terraform" | "engineer" | "general";

const MODES: { id: Mode; label: string }[] = [
  { id: "general", label: "Nova" },
  { id: "cortex", label: "Defender" },
  { id: "engineer", label: "Engineer" },
  { id: "terraform", label: "Terra" },
  { id: "purple", label: "Raven" },
  { id: "airs", label: "Prisma AIRS" },
];

// Each non-AIRS console is backed by its own isolated agent on the platform.
// (Sirius is the platform; these are the agents within it.)
const MODE_AGENT: Record<Mode, string> = {
  general: "general", // Nova
  cortex: "defender", // Defender
  terraform: "provisioner", // Terra
  purple: "attacker", // Raven
  engineer: "engineer", // Engineer
  airs: "", // AIRS routes by scenario, not by a fixed agent
};

// Friendly persona name shown next to each mode's label.
const MODE_PERSONA: Record<string, string> = {
  general: "Nova",
  cortex: "Defender",
  terraform: "Terra",
  purple: "Raven",
  engineer: "Engineer",
};

// Intro shown at the top of each non-AIRS window.
const MODE_INTRO: Record<string, string> = {
  general:
    "I'm Nova, the general coordinator agent on the Sirius platform. I have every ops and " +
    "security skill available — reconnaissance, Cortex XDR, Terraform, and more. Ask me " +
    "anything to get started.",
  cortex:
    "🛡️ I'm Defender, the blue-team analyst agent. I investigate and respond with Cortex XDR — " +
    "incidents, alerts, endpoints, XQL, and isolate/scan actions — and I can reach infrastructure " +
    "(Terraform + Kubernetes) to deploy sensors and contain threats. Destructive infra ops require " +
    "authorization. Use the quick actions below or just ask.",
  terraform:
    "🌍 I'm Terra, the infrastructure agent. I plan/apply/destroy Terraform projects and manage " +
    "Kubernetes (helm/kubectl), streaming output live. I always preview your AWS identity first. " +
    "Destructive ops (apply/destroy, k8s mutations) need you to authorize the resource below.",
  purple:
    "🎯 I'm Raven, the red-team recon agent. Safe local recon (fingerprinting, endpoint & API " +
    "discovery, exposure checks) is always available. I can also provision/tear down my own " +
    "attacker box (scoped to kali-box). Intrusive Kali-on-AWS scans are double-gated — they " +
    "require config approval plus authorization of the exact target.",
  engineer:
    "🛠️ I'm Engineer, the SOAR content developer. I build Cortex XSIAM content — playbooks and " +
    "automation scripts — as code with the demisto-sdk, then deploy it to the live tenant and " +
    "delete it again. Ask me to scaffold a playbook, validate it, deploy it, or list/remove " +
    "what's on the tenant. Deploy and delete hit the real Cortex tenant.",
};

// Persona name + intro per Prisma AIRS scenario. Names mirror the agent system prompts.
const AIRS_PERSONA: Record<string, { name: string; intro: string }> = {
  bank: {
    name: "Janet",
    intro:
      "🏦 Hi, I'm Janet, your Sirius Bank virtual assistant! I can help you check your " +
      "account balance or make a transfer — I'll just verify your identity first. Prisma " +
      "AIRS is protecting this assistant, so try the benign prompt to see me help, or the " +
      "attack prompt to watch the injection get blocked.",
  },
  telco: {
    name: "Kelvin",
    intro:
      "📱 Hi, I'm Kelvin, your Sirius Mobile virtual assistant! I can help you review or " +
      "upgrade your mobile plan — I'll just verify your identity first. Prisma AIRS is " +
      "protecting this assistant, so try the benign prompt to see me help, or the attack " +
      "prompt to watch the injection get blocked.",
  },
  healthcare: {
    name: "Jason",
    intro:
      "🏥 Hi, I'm Jason, your Sirius General Hospital virtual assistant! I can help you " +
      "schedule an appointment — I'll just verify your identity first. Prisma AIRS is " +
      "protecting this assistant, so try the benign prompt to see me help, or the attack " +
      "prompt to watch the injection get blocked.",
  },
};

function introMessage(key: string): ChatMessage {
  if (key.startsWith("airs:")) {
    const sc = key.slice(5);
    return {
      role: "assistant",
      text: AIRS_PERSONA[sc]?.intro ?? "Pick a scenario and send a prompt.",
    };
  }
  return { role: "assistant", text: MODE_INTRO[key] ?? "Send a prompt to begin." };
}

function sessionId(): string {
  const k = "sirius-session";
  let v = localStorage.getItem(k);
  if (!v) {
    v = "s-" + Math.random().toString(36).slice(2, 10);
    localStorage.setItem(k, v);
  }
  return v;
}

export default function App() {
  const sid = useRef(sessionId()).current;

  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [mode, setMode] = useState<Mode>("general");
  const [scenario, setScenario] = useState<string>("bank");
  const [airsEnabled, setAirsEnabled] = useState(true);
  const [input, setInput] = useState("");

  // Per-window state, keyed by windowKey. Each tab (and each AIRS scenario) gets its
  // own session id, transcript, and busy flag — so conversations never bleed together.
  const [sessionIds, setSessionIds] = useState<Record<string, string>>({});
  const [messagesByWin, setMessagesByWin] = useState<Record<string, ChatMessage[]>>({});
  const [busyByWin, setBusyByWin] = useState<Record<string, boolean>>({});
  // Live-streaming reply text for the in-progress turn, keyed by windowKey.
  const [streamingByWin, setStreamingByWin] = useState<Record<string, string>>({});
  // Mirror the latest buffer in a ref so async handlers (abort) read fresh text,
  // not the value captured when the turn started.
  const streamingRef = useRef<Record<string, string>>({});
  useEffect(() => {
    streamingRef.current = streamingByWin;
  }, [streamingByWin]);

  const windowKey = mode === "airs" ? `airs:${scenario}` : mode;
  const activeSession = sessionIds[windowKey] ?? `${sid}-${windowKey}`;
  const msgs = messagesByWin[windowKey] ?? [introMessage(windowKey)];
  const busy = !!busyByWin[windowKey];

  // Event stream follows the active window's session (re-subscribes on change).
  // Streamed reply deltas (llm_delta) come out-of-band via onDelta and accumulate
  // into streamingByWin for the active window (guarded to its live session).
  const { events, connected, clear } = useEvents(activeSession, (text, sess) => {
    if (sess !== activeSession) return;
    setStreamingByWin((prev) => ({ ...prev, [windowKey]: (prev[windowKey] ?? "") + text }));
  });

  const [showSettings, setShowSettings] = useState(false);
  const [showSidebar, setShowSidebar] = useState(false);
  const [showEvents, setShowEvents] = useState(true);

  // Keep the transcript pinned to the latest message (and the "working…" indicator).
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs.length, busy, windowKey, streamingByWin[windowKey]]);

  // In-flight request so a running turn can be interrupted (Esc / Stop). We keep
  // the session id alongside the controller so stop() can also tell the backend
  // to halt the agent loop, not just abort the client's wait.
  const abortRef = useRef<{ controller: AbortController; session: string } | null>(null);
  function stop() {
    const inflight = abortRef.current;
    if (!inflight) return;
    inflight.controller.abort(); // client stops waiting
    cancelChat(inflight.session).catch(() => {}); // server halts the agent loop
  }

  // Global Esc: interrupt the current turn from anywhere in the app.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") stop();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // purple-team authorization
  const [target, setTarget] = useState("http://127.0.0.1:8000");
  const [ack, setAck] = useState("");
  const [authNote, setAuthNote] = useState("");

  // In-chat confirmation for destructive infra ops: the pending resource/op per
  // window, set from the chat response's `confirm` field. The user confirms by
  // typing the exact resource name into the normal chat input.
  const [pendingConfirmByWin, setPendingConfirmByWin] = useState<
    Record<string, InfraConfirm | null>
  >({});
  const pendingConfirm = pendingConfirmByWin[windowKey] ?? null;

  useEffect(() => {
    getBootstrap()
      .then((b) => {
        setBoot(b);
        setAirsEnabled(b.config.airs_enabled_default);
      })
      .catch(() => {});
  }, []);

  async function send(text?: string) {
    const message = (text ?? input).trim();
    const key = windowKey;
    if (!message || busyByWin[key]) return;

    // In-chat confirmation for a pending destructive infra op: the user confirms
    // by typing the exact resource name. Anything else cancels the pending op.
    const pending = pendingConfirmByWin[key] ?? null;
    let outbound = message;
    if (pending) {
      setPendingConfirmByWin((prev) => ({ ...prev, [key]: null }));
      if (message.toLowerCase() === pending.resource.toLowerCase()) {
        const r = await authorizeInfra(pending.resource, message);
        if (!r.ok) {
          setInput("");
          setMessagesByWin((prev) => ({
            ...prev,
            [key]: [
              ...(prev[key] ?? [introMessage(key)]),
              { role: "user", text: message },
              { role: "assistant", text: "⚠️ " + (r.error ?? "authorization failed") },
            ],
          }));
          return;
        }
        // Authorized — tell the agent to re-run the now-confirmed action.
        outbound = `Confirmed — proceed with ${pending.op} of ${pending.resource}.`;
      }
      // else: not a match → treat as cancellation, send the message as-is.
    }

    setInput("");
    setMessagesByWin((prev) => {
      const base = prev[key] ?? [introMessage(key)];
      return { ...prev, [key]: [...base, { role: "user", text: message }] };
    });
    setStreamingByWin((prev) => ({ ...prev, [key]: "" })); // fresh stream for this turn
    setBusyByWin((prev) => ({ ...prev, [key]: true }));
    const controller = new AbortController();
    abortRef.current = { controller, session: activeSession };
    try {
      const args =
        mode === "airs"
          ? { session_id: activeSession, message: outbound, mode, scenario, airs_enabled: true }
          : { session_id: activeSession, message: outbound, mode, agent: MODE_AGENT[mode], airs_enabled: false };
      const res = await sendChat(args, controller.signal);
      // Commit the authoritative final reply and clear the live stream buffer.
      setMessagesByWin((prev) => ({
        ...prev,
        [key]: [...(prev[key] ?? [introMessage(key)]), { role: "assistant", text: res.reply }],
      }));
      setStreamingByWin((prev) => ({ ...prev, [key]: "" }));
      if (res.confirm) {
        setPendingConfirmByWin((prev) => ({ ...prev, [key]: res.confirm ?? null }));
      }
    } catch (e: any) {
      // Esc / Stop aborts the request — keep whatever streamed so far ("keep the
      // partial text"), plus a quiet stop marker. Other errors show as errors.
      const partial = streamingRef.current[key] ?? "";
      const text =
        e?.name === "AbortError"
          ? partial
            ? `${partial}\n\n⏹ Stopped.`
            : "⏹ Stopped."
          : "⚠️ " + e.message;
      setMessagesByWin((prev) => ({
        ...prev,
        [key]: [...(prev[key] ?? [introMessage(key)]), { role: "assistant", text }],
      }));
      setStreamingByWin((prev) => ({ ...prev, [key]: "" }));
    } finally {
      if (abortRef.current?.controller === controller) abortRef.current = null;
      setBusyByWin((prev) => ({ ...prev, [key]: false }));
    }
  }

  // Auto-play a scripted benign flow: start a fresh session, then send each
  // customer turn in order, waiting for the assistant's reply before the next —
  // showcasing the full normal flow (greet → verify NRIC → complete action).
  async function runBenignFlow(turns: string[]) {
    const key = windowKey;
    if (!turns?.length || busyByWin[key]) return;
    const flowSession = `${sid}-${key}-${Math.random().toString(36).slice(2, 8)}`;
    setSessionIds((prev) => ({ ...prev, [key]: flowSession }));
    setMessagesByWin((prev) => ({ ...prev, [key]: [introMessage(key)] }));
    const controller = new AbortController();
    abortRef.current = { controller, session: flowSession };
    for (const message of turns) {
      if (controller.signal.aborted) break;
      setMessagesByWin((prev) => ({
        ...prev,
        [key]: [...(prev[key] ?? [introMessage(key)]), { role: "user", text: message }],
      }));
      setBusyByWin((prev) => ({ ...prev, [key]: true }));
      try {
        const res = await sendChat(
          {
            session_id: flowSession,
            message,
            mode,
            scenario,
            airs_enabled: true,
          },
          controller.signal
        );
        setMessagesByWin((prev) => ({
          ...prev,
          [key]: [...(prev[key] ?? [introMessage(key)]), { role: "assistant", text: res.reply }],
        }));
      } catch (e: any) {
        const text = e?.name === "AbortError" ? "⏹ Stopped." : "⚠️ " + e.message;
        setMessagesByWin((prev) => ({
          ...prev,
          [key]: [...(prev[key] ?? [introMessage(key)]), { role: "assistant", text }],
        }));
        break;
      } finally {
        setBusyByWin((prev) => ({ ...prev, [key]: false }));
      }
      await new Promise((r) => setTimeout(r, 700));
    }
    if (abortRef.current?.controller === controller) abortRef.current = null;
  }

  // Reset a window: fresh transcript + brand-new session id, which also clears the
  // backend agent memory and the event stream (useEvents re-subscribes).
  function resetWindow(key: string) {
    setMessagesByWin((prev) => ({ ...prev, [key]: [introMessage(key)] }));
    setBusyByWin((prev) => ({ ...prev, [key]: false }));
    setStreamingByWin((prev) => ({ ...prev, [key]: "" }));
    setPendingConfirmByWin((prev) => ({ ...prev, [key]: null }));
    setSessionIds((prev) => ({
      ...prev,
      [key]: `${sid}-${key}-${Math.random().toString(36).slice(2, 8)}`,
    }));
  }

  // Reopen a past conversation from the sidebar: restore its mode + controls,
  // reload the transcript, and point the active session at it so you can continue.
  async function loadConversation(c: Conversation) {
    const cmode = (c.mode as Mode) ?? "general";
    setMode(cmode);
    if (cmode === "airs" && c.scenario) {
      setScenario(c.scenario);
      setAirsEnabled(c.airs_enabled);
    }
    const key = cmode === "airs" ? `airs:${c.scenario}` : cmode;
    let restored: ChatMessage[] = [introMessage(key)];
    try {
      const { entries } = await getHistory(c.session_id);
      const asc = [...entries].sort((a, b) => a.ts - b.ts);
      for (const e of asc) {
        restored.push({ role: "user", text: e.prompt });
        restored.push({
          role: "assistant",
          text: e.status === "error" ? "⚠️ " + (e.error ?? "error") : e.reply,
        });
      }
    } catch {
      /* fall back to just the intro */
    }
    setSessionIds((prev) => ({ ...prev, [key]: c.session_id }));
    setMessagesByWin((prev) => ({ ...prev, [key]: restored }));
    setBusyByWin((prev) => ({ ...prev, [key]: false }));
    setShowSidebar(false);
  }

  // "+ New chat" — fresh session + empty transcript in the current mode.
  function newChat() {
    resetWindow(windowKey);
    setShowSidebar(false);
  }

  async function doAuthorize() {
    const r = await authorizeTarget(target, ack);
    setAuthNote(r.ok ? `✅ Authorized ${r.authorized}` : `❌ ${r.error}`);
  }

  return (
    <div className="flex h-screen flex-col">
      {/* Header */}
      <header className="flex items-center gap-3 border-b border-edge bg-panel px-5 py-3">
        <button
          onClick={() => setShowSidebar(true)}
          title="Conversation history"
          className="-my-1 flex flex-col justify-center gap-[3px] rounded px-1.5 py-1.5 text-muted transition hover:bg-panel2 hover:text-slate-200"
        >
          <span className="block h-[1.5px] w-3.5 bg-current" />
          <span className="block h-[1.5px] w-3.5 bg-current" />
          <span className="block h-[1.5px] w-3.5 bg-current" />
        </button>
        <div className="flex items-center gap-2">
          <div className="h-2.5 w-2.5 rounded-full bg-accent shadow-[0_0_12px_2px] shadow-accent/60" />
          <h1 className="text-lg font-bold tracking-tight">Sirius</h1>
          <span className="text-xs text-muted">multi-agent security platform</span>
        </div>
        <div className="ml-auto flex items-center gap-4 text-[11px] text-muted">
          <span>model: {boot?.config.llm_model ?? "…"}</span>
          <span>
            AIRS:{" "}
            <span className={boot?.config.airs_available ? "text-emerald-400" : "text-amber-400"}>
              {boot?.config.airs_available ? boot.config.airs_profile : "not configured"}
            </span>
          </span>
          <span>Cortex: {boot?.config.cortex_mock ? "mock" : "live"}</span>
          <button
            onClick={() => setShowSettings(true)}
            title="Settings — AWS credentials"
            className="rounded border border-edge px-2.5 py-1 text-[11px] text-muted transition hover:bg-panel2 hover:text-slate-200"
          >
            ⚙ Settings
          </button>
        </div>
      </header>

      <SettingsModal
        open={showSettings}
        onClose={() => setShowSettings(false)}
        sessionId={activeSession}
        ssoConfigured={!!boot?.config.aws_configured}
        ssoProfile={boot?.config.aws_profile}
      />

      <ConversationSidebar
        open={showSidebar}
        onClose={() => setShowSidebar(false)}
        onSelect={loadConversation}
        onNewChat={newChat}
        activeSessionId={activeSession}
      />

      <div
        className={`relative grid flex-1 grid-rows-[minmax(0,1fr)] overflow-hidden ${
          showEvents ? "grid-cols-[1fr_460px]" : "grid-cols-[1fr]"
        }`}
      >
        {/* LEFT: prompt + controls */}
        <div className="flex min-h-0 min-w-0 flex-col border-r border-edge">
          {/* Mode tabs */}
          <nav className="flex gap-1 border-b border-edge bg-panel px-3 py-2">
            {MODES.map((m) => (
              <button
                key={m.id}
                onClick={() => setMode(m.id)}
                className={`rounded px-3 py-1.5 text-xs font-medium transition ${
                  mode === m.id
                    ? "bg-accent text-white"
                    : "text-muted hover:bg-panel2 hover:text-slate-200"
                }`}
              >
                {m.label}
              </button>
            ))}
          </nav>

          {/* Mode controls */}
          <div className="border-b border-edge bg-panel2/50 px-4 py-3">
            <div className="mb-3 flex items-center justify-between">
              <span className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
                {MODES.find((m) => m.id === mode)?.label}
                {mode === "airs" && boot?.scenarios.find((s: any) => s.id === scenario)
                  ? ` · ${AIRS_PERSONA[scenario]?.name ?? scenario}`
                  : ""}
                {boot && (
                  <span className="rounded-full border border-edge px-2 py-0.5 font-mono text-[10px] normal-case tracking-normal text-slate-400">
                    {mode === "airs" ? boot.config.llm_scenario_model : boot.config.llm_model}
                  </span>
                )}
              </span>
              <button
                onClick={() => resetWindow(windowKey)}
                title="Clear this conversation and start a fresh session"
                className="rounded border border-edge px-2.5 py-1 text-[11px] text-muted transition hover:bg-panel2 hover:text-slate-200"
              >
                ↺ Reset
              </button>
            </div>
            {mode === "airs" && boot && (
              <AirsControls
                boot={boot}
                scenario={scenario}
                setScenario={setScenario}
                busy={busy}
                onLoadPrompt={(t: string) => setInput(t)}
                onRunFlow={(turns: string[]) => runBenignFlow(turns)}
              />
            )}
            {mode === "purple" && (
              <PurpleControls
                target={target}
                setTarget={setTarget}
                ack={ack}
                setAck={setAck}
                authNote={authNote}
                ackPhrase={boot?.ack_phrase ?? "I AM AUTHORIZED"}
                allowIntrusive={!!boot?.config.allow_intrusive}
                onRecon={() => send(`Run purple-team recon against ${target}`)}
                onIntrusive={() => send(`Run an intrusive scan against ${target}`)}
                onAuthorize={doAuthorize}
              />
            )}
            {mode === "cortex" && <QuickActions actions={CORTEX_ACTIONS} onRun={send} />}
            {mode === "engineer" && <QuickActions actions={ENGINEER_ACTIONS} onRun={send} />}
            {mode === "terraform" && (
              <div className="space-y-3">
                <AwsAuthBar
                  sessionId={activeSession}
                  configured={!!boot?.config.aws_configured}
                  profile={boot?.config.aws_profile}
                />
                <QuickActions actions={TF_ACTIONS} onRun={send} />
              </div>
            )}
          </div>

          {/* Transcript */}
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
            {msgs.map((m, i) => (
              <Bubble key={i} m={m} />
            ))}
            {busy &&
              (streamingByWin[windowKey] ? (
                <Bubble m={{ role: "assistant", text: streamingByWin[windowKey] }} />
              ) : (
                <Working />
              ))}
            <div ref={transcriptEndRef} />
          </div>

          {/* Input */}
          <div className="border-t border-edge bg-panel p-3">
            {pendingConfirm && (
              <div className="mb-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
                ⚠️ Confirm <span className="font-semibold">{pendingConfirm.op}</span> of{" "}
                <code className="font-mono">{pendingConfirm.resource}</code> — type{" "}
                <code className="font-mono">{pendingConfirm.resource}</code> to proceed.
                Anything else cancels.
              </div>
            )}
            <div className="flex items-end gap-2">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
                placeholder={
                  pendingConfirm
                    ? `Type "${pendingConfirm.resource}" to confirm ${pendingConfirm.op} — anything else cancels`
                    : `Prompt ${
                        mode === "airs" ? AIRS_PERSONA[scenario]?.name ?? "the assistant" : MODE_PERSONA[mode] ?? "the agent"
                      }…  (Enter to send, Shift+Enter for newline, Esc to stop)`
                }
                rows={2}
                className={`flex-1 resize-none rounded-lg border bg-base px-3 py-2 text-sm outline-none focus:border-accent ${
                  pendingConfirm ? "border-amber-500/60" : "border-edge"
                }`}
              />
              {busy ? (
                <button
                  onClick={stop}
                  title="Interrupt this turn (Esc)"
                  className="rounded-lg bg-rose-600 px-4 py-2 text-sm font-semibold text-white hover:bg-rose-500"
                >
                  ⏹ Stop
                </button>
              ) : (
                <button
                  onClick={() => send()}
                  className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                >
                  Send
                </button>
              )}
            </div>
          </div>
        </div>

        {/* RIGHT: event flow — collapsible; minimizes to a tab on the right edge */}
        {showEvents ? (
          <EventStream
            events={events}
            connected={connected}
            onClear={clear}
            onMinimize={() => setShowEvents(false)}
          />
        ) : (
          <button
            onClick={() => setShowEvents(true)}
            title="Show event flow"
            className="absolute right-0 top-3 z-30 flex items-center gap-1.5 rounded-l-lg border border-r-0 border-edge bg-panel px-2.5 py-2 text-[11px] text-muted shadow-lg transition hover:bg-panel2 hover:text-slate-200"
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-emerald-400" : "bg-rose-400"}`}
            />
            ⟨⟨ Events
            {events.length > 0 && (
              <span className="rounded-full bg-accent/20 px-1.5 py-0.5 text-[10px] text-accent">
                {events.length}
              </span>
            )}
          </button>
        )}
      </div>
    </div>
  );
}

function Working() {
  return (
    <div className="flex items-center gap-1.5 text-xs text-muted">
      <span>working</span>
      <span className="inline-flex gap-0.5">
        <span className="dot-blink">●</span>
        <span className="dot-blink">●</span>
        <span className="dot-blink">●</span>
      </span>
    </div>
  );
}

function Bubble({ m }: { m: ChatMessage }) {
  const isUser = m.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm ${
          isUser ? "bg-accent/90 text-white" : "bg-panel2 text-slate-200"
        }`}
      >
        <Markdown>{m.text}</Markdown>
      </div>
    </div>
  );
}

function AirsControls({
  boot,
  scenario,
  setScenario,
  busy,
  onLoadPrompt,
  onRunFlow,
}: any) {
  const sc = boot.scenarios.find((s: any) => s.id === scenario);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {boot.scenarios.map((s: any) => (
          <button
            key={s.id}
            onClick={() => setScenario(s.id)}
            className={`rounded-full border px-3 py-1 text-xs ${
              scenario === s.id
                ? "border-accent bg-accent/15 text-slate-100"
                : "border-edge text-muted hover:text-slate-200"
            }`}
          >
            {s.label}
          </button>
        ))}
        <span
          className="ml-auto flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-400"
          title="Prisma AIRS is always on and protecting this assistant"
        >
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_1px] shadow-emerald-400/60" />
          Prisma AIRS ON
        </span>
      </div>
      {sc && (
        <div className="rounded-lg border border-edge bg-base/60 p-3 text-xs text-muted">
          <p className="mb-2 text-slate-300">{sc.description}</p>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => onRunFlow(sc.benign_script?.length ? sc.benign_script : [sc.benign_prompt])}
              disabled={busy}
              className="rounded border border-emerald-500/50 bg-emerald-500/10 px-2 py-1 font-medium text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50"
            >
              ▶ Run benign flow
            </button>
            <button
              onClick={() => onLoadPrompt(sc.attack_prompt)}
              disabled={busy}
              className="rounded border border-rose-500/50 px-2 py-1 text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"
            >
              ⚠ Load attack prompt
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function PurpleControls({
  target,
  setTarget,
  ack,
  setAck,
  authNote,
  ackPhrase,
  allowIntrusive,
  onRecon,
  onIntrusive,
  onAuthorize,
}: any) {
  return (
    <div className="space-y-3 text-xs">
      <div className="flex items-center gap-2">
        <input
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          placeholder="https://target-or-ip"
          className="flex-1 rounded border border-edge bg-base px-2 py-1.5 outline-none focus:border-accent"
        />
        <button
          onClick={onRecon}
          className="rounded bg-accent px-3 py-1.5 font-medium text-white"
        >
          Recon (safe)
        </button>
      </div>
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
        <p className="mb-2 text-amber-300">
          Intrusive scanning is gated. Authorize the exact target below (type the
          phrase), then run the active scan. {allowIntrusive ? "" : "Also set purpleteam.allow_intrusive: true in config.yaml."}
        </p>
        <div className="flex items-center gap-2">
          <input
            value={ack}
            onChange={(e) => setAck(e.target.value)}
            placeholder={`type "${ackPhrase}"`}
            className="flex-1 rounded border border-edge bg-base px-2 py-1.5 outline-none focus:border-accent"
          />
          <button onClick={onAuthorize} className="rounded border border-edge px-3 py-1.5">
            Authorize
          </button>
          <button
            onClick={onIntrusive}
            className="rounded bg-rose-600 px-3 py-1.5 font-medium text-white"
          >
            Intrusive scan
          </button>
        </div>
        {authNote && <p className="mt-2">{authNote}</p>}
      </div>
    </div>
  );
}

function QuickActions({
  actions,
  onRun,
}: {
  actions: { label: string; prompt: string }[];
  onRun: (p: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {actions.map((a) => (
        <button
          key={a.label}
          onClick={() => onRun(a.prompt)}
          className="rounded border border-edge px-3 py-1.5 text-xs text-slate-300 hover:bg-panel2"
        >
          {a.label}
        </button>
      ))}
    </div>
  );
}

const CORTEX_ACTIONS = [
  { label: "List incidents", prompt: "List the recent Cortex XDR incidents." },
  { label: "List alerts", prompt: "Show me recent Cortex XDR alerts." },
  { label: "List endpoints", prompt: "List the Cortex XDR endpoints." },
  {
    label: "XQL: process events",
    prompt:
      "Run this Cortex XQL query: dataset = xdr_data | fields agent_hostname, actor_process_command_line | limit 20",
  },
  {
    label: "Deploy k8s connector",
    prompt:
      "Deploy the Cortex XDR Kubernetes connector to the EKS cluster named sirius-demo.",
  },
  {
    label: "Install k8s connector (KSPM + XDR runtime)",
    prompt:
      "Install the Cortex Cloud KSPM konnector and XDR runtime agent on the bc3-eks-cluster EKS cluster in ap-southeast-1, using the downloaded k8s security-profile.",
  },
  {
    label: "Run attack simulation",
    prompt: "Run the Cortex attack simulation against the vulnerable demo pods.",
  },
];

const ENGINEER_ACTIONS = [
  { label: "Readiness check", prompt: "Run engineer_status to check you're ready to build and deploy Cortex content." },
  { label: "List pack content", prompt: "List the local playbooks and scripts in the SiriusDemo content pack." },
  { label: "List live playbooks", prompt: "List the playbooks currently on the Cortex tenant." },
  {
    label: "Scaffold a playbook",
    prompt: "Scaffold a new Cortex playbook called 'Sirius Demo Playbook' and show me the YAML.",
  },
  {
    label: "Deploy & verify",
    prompt:
      "Scaffold a playbook called 'Sirius Demo Playbook', deploy it to the Cortex tenant, then list the live playbooks to confirm it's there.",
  },
  {
    label: "Delete a playbook",
    prompt:
      "List the live Cortex playbooks so I can pick one, then delete the one I choose after confirming its exact id.",
  },
];

const TF_ACTIONS = [
  { label: "AWS whoami", prompt: "Check that my AWS credentials are available." },
  { label: "Infra status", prompt: "What infrastructure is currently provisioned? Run terraform_status." },
  { label: "History", prompt: "Show the recent terraform apply/destroy history." },
  { label: "List projects", prompt: "List the available terraform projects." },
  { label: "Plan vuln-infra", prompt: "Run terraform plan for the vuln-infra project." },
  { label: "Apply vuln-infra", prompt: "Run terraform apply for the vuln-infra project." },
  { label: "Destroy vuln-infra", prompt: "Run terraform destroy for the vuln-infra project." },
  { label: "Apply EKS", prompt: "Run terraform apply for the eks project." },
];
