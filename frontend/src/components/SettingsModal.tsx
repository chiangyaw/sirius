import { useEffect, useState } from "react";
import {
  awsSsoLogin,
  azureLogin,
  clearAwsCreds,
  clearHistory,
  getAwsStatus,
  getAzureStatus,
  getSettings,
  getVertexStatus,
  listVertexProjects,
  saveAwsKeys,
  setVertexProject,
} from "../lib/api";
import type { AwsSettingsStatus, VertexProject, VertexStatus } from "../lib/types";

const INPUT =
  "w-full rounded border border-edge bg-base px-2 py-1.5 text-sm outline-none focus:border-accent";

// SSO login bar — reused both inside the Settings modal and on the Terraform tab.
export function AwsAuthBar({
  sessionId,
  configured,
  profile,
  onChange,
}: {
  sessionId: string;
  configured: boolean;
  profile?: string;
  onChange?: () => void;
}) {
  const [status, setStatus] = useState<{ authenticated?: boolean; account?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  async function refresh() {
    try {
      setStatus(await getAwsStatus());
    } catch {
      /* ignore */
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function login() {
    setBusy(true);
    setNote("Starting login — open the AWS sign-in link in the event stream →");
    const r = await awsSsoLogin(sessionId);
    if (!r.ok) {
      setBusy(false);
      setNote(`❌ ${r.error}`);
      return;
    }
    // Poll identity until the browser login completes (or give up after ~2 min).
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      const st = await getAwsStatus();
      setStatus(st);
      if (st.authenticated || tries > 40) {
        clearInterval(timer);
        setBusy(false);
        if (st.authenticated) {
          setNote(`✅ Authenticated (account ${st.account})`);
          onChange?.();
        }
      }
    }, 3000);
  }

  return (
    <div className="rounded border border-edge bg-panel/40 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-muted">AWS:</span>
        <span
          className={`text-[11px] font-medium ${
            status?.authenticated ? "text-emerald-400" : "text-amber-400"
          }`}
        >
          {status?.authenticated ? `signed in · ${status.account}` : "not signed in"}
        </span>
        <button
          onClick={login}
          disabled={!configured || busy}
          title={
            configured
              ? `Run aws sso login (profile ${profile ?? "sirius"})`
              : "Set aws.sso_start_url / account_id / role_name in config.yaml"
          }
          className="ml-auto rounded border border-edge px-3 py-1 text-[11px] text-slate-300 transition hover:bg-panel2 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? "Signing in…" : status?.authenticated ? "Re-authenticate AWS" : "Authenticate AWS"}
        </button>
      </div>
      {!configured && (
        <p className="mt-1 text-[11px] text-amber-400/80">
          Set <code className="font-mono">aws.sso_start_url</code>,{" "}
          <code className="font-mono">account_id</code>, and{" "}
          <code className="font-mono">role_name</code> in config.yaml to enable SSO login.
        </p>
      )}
      {note && <p className="mt-1 text-[11px] text-muted">{note}</p>}
    </div>
  );
}

// Azure device-code login bar — independent of the AWS auth method.
export function AzureAuthBar({ sessionId }: { sessionId: string }) {
  const [status, setStatus] = useState<{
    ok?: boolean;
    subscription_name?: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  async function refresh() {
    try {
      setStatus(await getAzureStatus());
    } catch {
      /* ignore */
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function login() {
    setBusy(true);
    setNote("Starting login — open the Azure device-code link in the event stream →");
    const r = await azureLogin(sessionId);
    if (!r.ok) {
      setBusy(false);
      setNote(`❌ ${r.error}`);
      return;
    }
    // Poll identity until the device-code login completes (or give up after ~2 min).
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      const st = await getAzureStatus();
      setStatus(st);
      if (st.ok || tries > 40) {
        clearInterval(timer);
        setBusy(false);
        if (st.ok) setNote(`✅ Signed in (subscription ${st.subscription_name})`);
      }
    }, 3000);
  }

  return (
    <div className="rounded border border-edge bg-panel/40 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-muted">Azure:</span>
        <span
          className={`text-[11px] font-medium ${
            status?.ok ? "text-emerald-400" : "text-amber-400"
          }`}
        >
          {status?.ok ? `signed in · ${status.subscription_name}` : "not signed in"}
        </span>
        <button
          onClick={login}
          disabled={busy}
          title="Run az login --use-device-code"
          className="ml-auto rounded border border-edge px-3 py-1 text-[11px] text-slate-300 transition hover:bg-panel2 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? "Signing in…" : status?.ok ? "Re-authenticate Azure" : "Authenticate Azure"}
        </button>
      </div>
      {note && <p className="mt-1 text-[11px] text-muted">{note}</p>}
    </div>
  );
}

// Vertex (GCP) project switcher — the rotating-sandbox-project control.
export function VertexBar({ sessionId }: { sessionId: string }) {
  const [status, setStatus] = useState<VertexStatus | null>(null);
  const [projects, setProjects] = useState<VertexProject[]>([]);
  const [project, setProject] = useState("");
  const [region, setRegion] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  async function refresh() {
    try {
      const st = await getVertexStatus();
      setStatus(st);
      setProject(st.project ?? "");
      setRegion(st.region ?? "");
    } catch {
      /* ignore */
    }
  }

  useEffect(() => {
    refresh();
    // gcloud projects list is best-effort — a Vertex-only identity may lack the
    // permission, in which case the field stays free-text with no suggestions.
    listVertexProjects()
      .then((r) => setProjects(r.projects || []))
      .catch(() => setProjects([]));
  }, []);

  async function switchProject() {
    const target = project.trim();
    if (!target) {
      setNote("❌ Enter a GCP project id.");
      return;
    }
    setBusy(true);
    setNote(`Switching to ${target}…`);
    try {
      const r = await setVertexProject({ project: target, region: region.trim(), session_id: sessionId });
      if (r.ok === false) {
        setNote(`❌ ${r.error}`);
      } else {
        setStatus(r);
        setNote(
          r.model === "echo"
            ? "⚠️ Switched, but Vertex couldn't init (backend fell back to Echo) — check ADC / the project id."
            : `✅ Switched to ${r.project} · ${r.region} (model ${r.model}). Applies on the next turn — no restart.`
        );
      }
    } catch (e: any) {
      setNote(`❌ ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded border border-edge bg-panel/40 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-muted">Project:</span>
        <span className="text-[11px] font-medium text-slate-200">
          {status?.project ?? "unset"}
          {status?.region ? ` · ${status.region}` : ""}
        </span>
        <span
          className={`ml-auto text-[11px] font-medium ${
            status?.adc ? "text-emerald-400" : "text-amber-400"
          }`}
          title={
            status?.adc
              ? "Application Default Credentials present"
              : "No ADC — run: gcloud auth application-default login"
          }
        >
          {status?.adc ? "ADC ✓" : "no ADC"}
        </span>
      </div>

      <div className="mt-2 flex flex-wrap items-end gap-2">
        <label className="min-w-[10rem] flex-1 block">
          <span className="mb-1 block text-[11px] text-muted">GCP project id</span>
          <input
            className={INPUT}
            list="vertex-projects"
            value={project}
            onChange={(e) => setProject(e.target.value)}
            placeholder="prod-xxxxxx"
            autoComplete="off"
            spellCheck={false}
          />
          <datalist id="vertex-projects">
            {projects.map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.name && p.name !== p.project_id ? p.name : ""}
              </option>
            ))}
          </datalist>
        </label>
        <label className="w-32 block">
          <span className="mb-1 block text-[11px] text-muted">Region</span>
          <input
            className={INPUT}
            value={region}
            onChange={(e) => setRegion(e.target.value)}
            placeholder="us-east5"
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <button
          onClick={switchProject}
          disabled={busy}
          className="rounded bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
        >
          {busy ? "Switching…" : "Switch"}
        </button>
      </div>

      {status?.source && (
        <p className="mt-1 text-[11px] text-muted/70">source: {status.source}</p>
      )}
      {note && <p className="mt-1 text-[11px] text-muted">{note}</p>}
    </div>
  );
}

export function SettingsModal({
  open,
  onClose,
  sessionId,
  ssoConfigured,
  ssoProfile,
}: {
  open: boolean;
  onClose: () => void;
  sessionId: string;
  ssoConfigured: boolean;
  ssoProfile?: string;
}) {
  const [st, setSt] = useState<AwsSettingsStatus | null>(null);
  const [tab, setTab] = useState<"keys" | "sso">("keys");
  const [keyId, setKeyId] = useState("");
  const [secret, setSecret] = useState("");
  const [token, setToken] = useState("");
  const [region, setRegion] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [histNote, setHistNote] = useState("");
  const [confirmHist, setConfirmHist] = useState(false);

  async function clearAllHistory() {
    setBusy(true);
    try {
      const r = await clearHistory();
      setHistNote(`✅ Cleared ${r.cleared} history ${r.cleared === 1 ? "entry" : "entries"}.`);
    } catch (e: any) {
      setHistNote(`❌ ${e.message}`);
    } finally {
      setBusy(false);
      setConfirmHist(false);
    }
  }

  async function refresh() {
    try {
      const s = await getSettings();
      setSt(s);
      if (s.method === "sso") setTab("sso");
      setRegion(s.region || "");
    } catch {
      /* ignore */
    }
  }

  // Load status + reset transient inputs whenever the modal opens.
  useEffect(() => {
    if (!open) return;
    setNote("");
    setKeyId("");
    setSecret("");
    setToken("");
    setHistNote("");
    setConfirmHist(false);
    refresh();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open) return null;

  async function save() {
    if (!keyId.trim() || !secret.trim()) {
      setNote("❌ Access key ID and secret are both required.");
      return;
    }
    setBusy(true);
    setNote("Saving…");
    try {
      const r = await saveAwsKeys({
        access_key_id: keyId,
        secret_access_key: secret,
        session_token: token,
        region,
      });
      if (r.ok === false) {
        setNote(`❌ ${r.error}`);
        return;
      }
      setKeyId("");
      setSecret("");
      setToken("");
      setNote("✅ Saved. Terraform will use these immediately (no restart).");
      refresh();
    } catch (e: any) {
      setNote(`❌ ${e?.message || "save failed"}`);
    } finally {
      setBusy(false);
    }
  }

  async function clearAll() {
    setBusy(true);
    try {
      await clearAwsCreds();
      setNote("Cleared AWS credentials.");
      refresh();
    } catch (e: any) {
      setNote(`❌ ${e?.message || "clear failed"}`);
    } finally {
      setBusy(false);
    }
  }

  const methodLabel =
    st?.method === "keys" ? "access keys" : st?.method === "sso" ? "SSO" : "none";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="w-[460px] max-w-[92vw] rounded-lg border border-edge bg-panel p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center">
          <h2 className="text-sm font-semibold tracking-wide">Settings</h2>
          <button
            onClick={onClose}
            className="ml-auto rounded px-2 py-0.5 text-muted transition hover:bg-panel2 hover:text-slate-200"
          >
            ✕
          </button>
        </div>

        {/* Current status */}
        <div className="mb-4 rounded-lg border border-edge bg-base/60 p-3 text-xs">
          <div className="flex items-center gap-2">
            <span className="text-muted">Current:</span>
            <span className="font-medium">{methodLabel}</span>
            <span
              className={`ml-auto font-medium ${
                st?.authenticated ? "text-emerald-400" : "text-amber-400"
              }`}
            >
              {st?.authenticated ? `signed in · ${st.account}` : "not signed in"}
            </span>
          </div>
        </div>

        {/* Method tabs */}
        <div className="mb-3 flex gap-1">
          {(["keys", "sso"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`rounded px-3 py-1.5 text-xs font-medium transition ${
                tab === t ? "bg-accent text-white" : "text-muted hover:bg-panel2 hover:text-slate-200"
              }`}
            >
              {t === "keys" ? "Access keys" : "SSO login"}
            </button>
          ))}
        </div>

        {tab === "keys" ? (
          <div className="space-y-2 text-xs">
            <label className="block">
              <span className="mb-1 block text-muted">AWS_ACCESS_KEY_ID</span>
              <input
                className={INPUT}
                value={keyId}
                onChange={(e) => setKeyId(e.target.value)}
                placeholder={
                  st?.access_key_last4 ? `•••••••• ${st.access_key_last4} (enter to replace)` : "AKIA…"
                }
                autoComplete="off"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-muted">AWS_SECRET_ACCESS_KEY</span>
              <input
                className={INPUT}
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder="••••••••"
                autoComplete="off"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-muted">
                AWS_SESSION_TOKEN <span className="text-muted/60">(optional, for temporary creds)</span>
              </span>
              <input
                className={INPUT}
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={st?.has_session_token ? "•••••••• (set)" : "optional"}
                autoComplete="off"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-muted">AWS_REGION</span>
              <input
                className={INPUT}
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                placeholder="ap-southeast-1"
                autoComplete="off"
              />
            </label>
            <div className="flex items-center gap-2 pt-1">
              <button
                onClick={save}
                disabled={busy}
                className="rounded bg-accent px-3 py-1.5 font-medium text-white disabled:opacity-40"
              >
                {busy ? "Saving…" : "Save keys"}
              </button>
              <button
                onClick={clearAll}
                disabled={busy}
                className="rounded border border-edge px-3 py-1.5 text-slate-300 hover:bg-panel2 disabled:opacity-40"
              >
                Clear
              </button>
            </div>
            <p className="pt-1 text-[11px] text-muted/70">
              Stored in .env (owner-only) and applied live. Best for long-lived IAM keys;
              temporary keys expire and must be re-entered — use SSO for those.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            <AwsAuthBar
              sessionId={sessionId}
              configured={ssoConfigured}
              profile={ssoProfile}
              onChange={refresh}
            />
            <p className="text-[11px] text-muted/70">
              Opens <code className="font-mono">aws sso login</code>; the sign-in link appears in
              the event stream. Role creds refresh automatically until the SSO token expires.
            </p>
          </div>
        )}

        {note && <p className="mt-3 text-[11px] text-muted">{note}</p>}

        {/* Vertex (GCP) project — switch the rotating sandbox project, no restart */}
        <div className="mt-5 border-t border-edge pt-4">
          <div className="mb-2 flex items-center gap-2">
            <span className="text-xs font-semibold tracking-wide">Vertex AI (GCP)</span>
            <span className="text-[11px] text-muted">switch the Claude-on-Vertex project</span>
          </div>
          <VertexBar sessionId={sessionId} />
          <p className="mt-1 text-[11px] text-muted/70">
            Persists <code className="font-mono">SIRIUS_VERTEX_PROJECT</code> to .env and rebuilds the
            LLM backend live. Reuses your GCP credentials (ADC); if the new project needs a different
            identity, run <code className="font-mono">gcloud auth application-default login</code>.
          </p>
        </div>

        {/* Azure sign-in (device code) — independent of the AWS method above */}
        <div className="mt-5 border-t border-edge pt-4">
          <div className="mb-2 flex items-center gap-2">
            <span className="text-xs font-semibold tracking-wide">Azure</span>
            <span className="text-[11px] text-muted">device-code sign-in for Azure deploys</span>
          </div>
          <AzureAuthBar sessionId={sessionId} />
          <p className="mt-1 text-[11px] text-muted/70">
            Opens <code className="font-mono">az login --use-device-code</code>; the sign-in link
            appears in the event stream. The token is cached by the az CLI and reused with no restart.
          </p>
        </div>

        {/* Prompt history / audit trail */}
        <div className="mt-5 border-t border-edge pt-4">
          <div className="mb-2 flex items-center gap-2">
            <span className="text-xs font-semibold tracking-wide">Prompt history</span>
            <span className="text-[11px] text-muted">audit trail of every prompt &amp; reply</span>
          </div>
          <div className="flex items-center gap-2">
            {confirmHist ? (
              <>
                <span className="text-[11px] text-amber-400">
                  Permanently delete all history?
                </span>
                <button
                  onClick={clearAllHistory}
                  disabled={busy}
                  className="ml-auto rounded bg-rose-600 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
                >
                  {busy ? "Clearing…" : "Yes, clear all"}
                </button>
                <button
                  onClick={() => setConfirmHist(false)}
                  disabled={busy}
                  className="rounded border border-edge px-3 py-1.5 text-xs text-slate-300 hover:bg-panel2 disabled:opacity-40"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                onClick={() => setConfirmHist(true)}
                className="rounded border border-rose-500/50 px-3 py-1.5 text-xs text-rose-300 hover:bg-rose-500/10"
              >
                Clear all history
              </button>
            )}
          </div>
          {histNote && <p className="mt-2 text-[11px] text-muted">{histNote}</p>}
        </div>
      </div>
    </div>
  );
}
