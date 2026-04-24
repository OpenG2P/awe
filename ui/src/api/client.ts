// Thin wrapper around the AWE HTTP API. In production the SPA is served
// from the same origin as the API (under /v1/awe/admin), so relative URLs
// work without CORS. In dev, Vite's proxy forwards /v1/awe → :8000.

const BASE = "/v1/awe";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// ---------------------------------------------------------------------------
// Bearer token
// ---------------------------------------------------------------------------
// Policy/admin endpoints require a Keycloak bearer with the `awe-admin` role.
// Strategy:
//   1. If `localStorage.awe_token` is set, use it (wire a real login flow
//      into this later — plug in @react-keycloak/web or similar).
//   2. Otherwise fall back to a built-in dev token. The backend accepts
//      unsigned tokens when `keycloak.issuer` is empty (dev mode). This
//      makes `npm run dev` work end-to-end without any manual setup.
// The dev token is `sub=dev-admin`, realm role `awe-admin`, no signature.
// Never reachable in production — the Helm chart sets a non-empty issuer
// which forces JWKS verification.
const DEV_TOKEN =
  "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0." +
  btoa(
    JSON.stringify({
      sub: "dev-admin",
      email: "dev-admin@local",
      realm_access: { roles: ["awe-admin"] },
    })
  ).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_") +
  ".";

export function getToken(): string {
  return localStorage.getItem("awe_token") || DEV_TOKEN;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
      ...(init.headers || {}),
    },
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new ApiError(resp.status, body);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
}

export const api = {
  listPolicies: () => request<Policy[]>("/policies"),
  createPolicy: (payload: PolicyCreate) =>
    request<Policy>("/policies", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  addVersion: (key: string, payload: PolicyCreate) =>
    request<Policy>(`/policies/${encodeURIComponent(key)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  updateDraft: (key: string, version: number, payload: PolicyCreate) =>
    request<Policy>(
      `/policies/${encodeURIComponent(key)}/versions/${version}`,
      { method: "PATCH", body: JSON.stringify(payload) }
    ),
  getVersions: (key: string) =>
    request<PolicyVersion[]>(`/policies/${encodeURIComponent(key)}/versions`),
  getVersion: (key: string, v: number) =>
    request<Policy>(`/policies/${encodeURIComponent(key)}/versions/${v}`),
  activate: (key: string, v: number) =>
    request<Policy>(
      `/policies/${encodeURIComponent(key)}/versions/${v}/activate`,
      { method: "POST" }
    ),
  simulate: (key: string, v: number, context: Record<string, unknown>) =>
    request<SimulateResponse>(
      `/policies/${encodeURIComponent(key)}/versions/${v}/simulate`,
      { method: "POST", body: JSON.stringify({ context }) }
    ),
  listRequests: (params: Partial<RequestQuery> = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v && qs.append(k, String(v)));
    return request<ApprovalRequestOut[]>(`/requests?${qs}`);
  },
  getRequest: (id: string) => request<ApprovalRequestOut>(`/requests/${id}`),
  getRequestEvents: (id: string) =>
    request<ApprovalEvent[]>(`/requests/${id}/events`),
  getRequestTasks: (id: string) =>
    request<TaskOut[]>(
      `/tasks?assignee=*&request_id=${encodeURIComponent(id)}`
    ),
  listDeliveries: (params: { status?: string; request_id?: string } = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v && qs.append(k, String(v)));
    return request<DeliveryOut[]>(`/admin/deliveries?${qs}`);
  },
  retryDelivery: (id: string) =>
    request<DeliveryOut>(
      `/admin/deliveries/${encodeURIComponent(id)}/retry`,
      { method: "POST" }
    ),
};

// ---------------------------------------------------------------------------
// Shape types (mirror Pydantic schemas in src/awe/schemas)
// ---------------------------------------------------------------------------
export interface Policy {
  id: string;
  policy_key: string;
  version: number;
  name: string;
  description?: string | null;
  status: "draft" | "active" | "archived";
  artifact_type: string;
  created_at: string;
  updated_at: string;
  stages: Stage[];
}

export interface PolicyVersion {
  id: string;
  policy_key: string;
  version: number;
  status: string;
  created_at: string;
}

export interface Stage {
  id: string;
  stage_order: number;
  name: string;
  mode: string;
  mode_value?: number | null;
  sla_hours?: number | null;
  rules: Rule[];
}

export interface Rule {
  id: string;
  rule_type: string;
  rule_value: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Shapes accepted by POST /policies (no `id` on nested rows)
// ---------------------------------------------------------------------------
export interface PolicyCreate {
  policy_key: string;
  name: string;
  description?: string;
  artifact_type: string;
  stages: StageCreate[];
}

export interface StageCreate {
  name: string;
  stage_order: number;
  mode: string;
  mode_value?: number | null;
  sla_hours?: number | null;
  on_empty: "skip" | "block";
  rules: RuleCreate[];
}

export interface RuleCreate {
  rule_type: "user" | "role" | "group" | "expression" | "http";
  rule_value: Record<string, unknown>;
}

export interface SimulateResponse {
  policy_id: string;
  policy_version: number;
  stages: {
    stage_order: number;
    name: string;
    mode: string;
    mode_value?: number | null;
    resolved_approvers: string[];
    skipped: boolean;
    skip_reason?: string | null;
  }[];
}

export interface ApprovalRequestOut {
  id: string;
  policy_id: string;
  policy_key: string;
  policy_version: number;
  artifact_type: string;
  artifact_id: string;
  source_service: string;
  requester?: string | null;
  context: Record<string, unknown>;
  status: string;
  current_stage_order: number;
  callback_url?: string | null;
  completed_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApprovalEvent {
  id: string;
  request_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface RequestQuery {
  artifact_type: string;
  artifact_id: string;
  status: string;
  limit: number;
}

export interface TaskOut {
  id: string;
  request_id: string;
  stage_id: string;
  stage_order: number;
  assignee: string;
  status: string;
  claimed_at?: string | null;
  completed_at?: string | null;
  due_at?: string | null;
  decision_id?: string | null;
  created_at: string;
}

export interface DeliveryOut {
  id: string;
  event_id: string;
  request_id: string;
  event_type: string;
  url: string;
  status: "pending" | "delivered" | "failed" | "exhausted";
  attempt: number;
  next_attempt_at: string;
  last_attempt_at?: string | null;
  last_status_code?: number | null;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
}
