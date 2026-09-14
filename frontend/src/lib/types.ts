export type Severity = "info" | "success" | "warn" | "danger";

export type EventType =
  | "user_prompt"
  | "agent_start"
  | "agent_thought"
  | "llm_request"
  | "llm_response"
  | "llm_delta"
  | "airs_scan"
  | "tool_call"
  | "tool_result"
  | "skill_invoked"
  | "terraform_step"
  | "aws_sso"
  | "scan_finding"
  | "blocked"
  | "error"
  | "agent_end"
  | "status";

export interface SiriusEvent {
  id: string;
  seq: number;
  ts: number;
  session_id: string;
  agent: string;
  type: EventType;
  title: string;
  severity: Severity;
  payload: Record<string, any>;
  parent_id: string | null;
}

export interface Scenario {
  id: string;
  label: string;
  domain: string;
  description: string;
  attack_prompt: string;
  benign_prompt: string;
}

export interface Bootstrap {
  agents: { name: string; skills: string[]; model: string }[];
  scenarios: Scenario[];
  config: {
    airs_enabled_default: boolean;
    airs_available: boolean;
    airs_profile: string;
    cortex_mock: boolean;
    allow_intrusive: boolean;
    infra_require_authorization: boolean;
    aws_configured: boolean;
    aws_profile: string;
    llm_model: string;
    llm_scenario_model: string;
    vertex_project: string | null;
    vertex_region: string;
  };
  ack_phrase: string;
}

export interface VertexProject {
  project_id: string;
  name?: string;
}

export interface VertexStatus {
  project: string | null;
  region: string;
  source: string;
  adc: boolean;
  gcloud: boolean;
  model?: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  text: string;
}

export interface HistoryEntry {
  id: string;
  ts: number;
  session_id: string;
  agent: string;
  mode: string | null;
  scenario: string | null;
  airs_enabled: boolean;
  status: "ok" | "error";
  prompt: string;
  reply: string;
  error: string | null;
}

export interface Conversation {
  session_id: string;
  title: string;
  mode: string;
  scenario: string | null;
  agent: string | null;
  airs_enabled: boolean;
  count: number;
  created_ts: number;
  updated_ts: number;
}

export interface AwsSettingsStatus {
  method: "keys" | "sso" | "none";
  access_key_last4: string | null;
  has_session_token: boolean;
  region: string;
  sso_profile: string | null;
  authenticated: boolean;
  account?: string | null;
  arn?: string | null;
}
