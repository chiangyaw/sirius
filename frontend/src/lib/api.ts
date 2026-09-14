import type {
  AwsSettingsStatus,
  Bootstrap,
  Conversation,
  HistoryEntry,
  VertexProject,
  VertexStatus,
} from "./types";

export async function getBootstrap(): Promise<Bootstrap> {
  const r = await fetch("/api/bootstrap");
  if (!r.ok) throw new Error("bootstrap failed");
  return r.json();
}

export interface ChatArgs {
  session_id: string;
  message: string;
  agent?: string;
  scenario?: string;
  mode?: string;
  airs_enabled?: boolean;
  reset?: boolean;
}

export interface InfraConfirm {
  resource: string;
  op: string;
}

export async function sendChat(
  args: ChatArgs,
  signal?: AbortSignal
): Promise<{ reply: string; agent: string; confirm?: InfraConfirm | null }> {
  const r = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
    signal,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || `chat failed (${r.status})`);
  }
  return r.json();
}

export async function cancelChat(sessionId: string): Promise<{ ok?: boolean }> {
  const r = await fetch("/api/chat/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  return r.json().catch(() => ({}));
}

export async function authorizeTarget(
  target: string,
  acknowledgement: string
): Promise<{ ok?: boolean; error?: string; authorized?: string }> {
  const r = await fetch("/api/purpleteam/authorize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target, acknowledgement }),
  });
  return r.json();
}

export async function authorizeInfra(
  resource: string,
  acknowledgement: string
): Promise<{ ok?: boolean; error?: string; authorized?: string }> {
  const r = await fetch("/api/infra/authorize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resource, acknowledgement }),
  });
  return r.json();
}

export async function awsSsoLogin(
  sessionId: string
): Promise<{ ok?: boolean; started?: boolean; profile?: string; error?: string }> {
  const r = await fetch("/api/aws/sso/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  return r.json();
}

export async function getAwsStatus(): Promise<{
  authenticated?: boolean;
  account?: string;
  arn?: string;
  profile?: string;
  error?: string;
}> {
  const r = await fetch("/api/aws/status");
  return r.json();
}

export async function azureLogin(
  sessionId: string
): Promise<{ ok?: boolean; started?: boolean; tenant?: string | null; error?: string }> {
  const r = await fetch("/api/azure/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  return r.json();
}

export async function getAzureStatus(): Promise<{
  ok?: boolean;
  subscription_id?: string;
  subscription_name?: string;
  tenant_id?: string;
  user?: string;
  error?: string;
}> {
  const r = await fetch("/api/azure/status");
  return r.json();
}

export async function getVertexStatus(): Promise<VertexStatus> {
  const r = await fetch("/api/vertex/status");
  return r.json();
}

export async function listVertexProjects(): Promise<{
  ok?: boolean;
  error?: string;
  projects: VertexProject[];
}> {
  const r = await fetch("/api/vertex/projects");
  return r.json();
}

export async function setVertexProject(body: {
  project: string;
  region?: string;
  session_id?: string;
}): Promise<VertexStatus & { ok?: boolean; error?: string; model?: string }> {
  const r = await fetch("/api/vertex/project", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function getSettings(): Promise<AwsSettingsStatus> {
  const r = await fetch("/api/settings");
  return r.json();
}

export async function saveAwsKeys(body: {
  access_key_id: string;
  secret_access_key: string;
  session_token?: string;
  region?: string;
}): Promise<AwsSettingsStatus & { ok?: boolean; error?: string }> {
  const r = await fetch("/api/settings/aws/keys", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function clearAwsCreds(): Promise<AwsSettingsStatus & { ok?: boolean }> {
  const r = await fetch("/api/settings/aws/clear", { method: "POST" });
  return r.json();
}

export async function getHistory(sessionId?: string): Promise<{ entries: HistoryEntry[] }> {
  const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  const r = await fetch(`/api/history${q}`);
  if (!r.ok) throw new Error("history fetch failed");
  return r.json();
}

export async function clearHistory(): Promise<{ ok: boolean; cleared: number }> {
  const r = await fetch("/api/history/clear", { method: "POST" });
  return r.json();
}

export async function getConversations(): Promise<{ conversations: Conversation[] }> {
  const r = await fetch("/api/conversations");
  if (!r.ok) throw new Error("conversations fetch failed");
  return r.json();
}

export async function deleteConversation(
  sessionId: string
): Promise<{ ok: boolean; deleted: number }> {
  const r = await fetch("/api/conversations/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  return r.json();
}
