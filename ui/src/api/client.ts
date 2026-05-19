// Thin wrapper around the AWE HTTP API. The SPA and API share an origin
// (Istio routes `/v1/awe/*` to the backend Service, `/` to the UI Service),
// so relative URLs work without CORS. In dev, Vite's proxy forwards
// `/v1/awe` → :8000.
//
// Auth tokens come from the Keycloak-js client in ../auth.ts — either a real
// Keycloak session token in production, or a dev fallback JWT when the
// runtime config has an empty Keycloak URL.

import { getToken } from "../auth";

const BASE = "/v1/awe";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Normalize list endpoints — tasks API returns `{ items, total, … }`. */
export function ensureArray<T>(value: unknown): T[] {
  if (Array.isArray(value)) return value;
  if (
    value &&
    typeof value === "object" &&
    "items" in value &&
    Array.isArray((value as PagedTasksOut).items)
  ) {
    return (value as PagedTasksOut).items as T[];
  }
  return [];
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
  deactivate: (key: string, v: number) =>
    request<Policy>(
      `/policies/${encodeURIComponent(key)}/versions/${v}/deactivate`,
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
  getRequest: (id: string) =>
    request<ApprovalRequestOut>(`/requests/${encodeURIComponent(id)}`),
  getRequestEvents: (id: string) =>
    request<ApprovalEvent[]>(`/requests/${encodeURIComponent(id)}/events`),
  taskStats: (params: { status?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.status) qs.append("status", params.status);
    return request<TaskStatsOut>(`/tasks/stats?${qs}`);
  },
  listTasks: (params: Partial<TaskQuery> = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(
      ([k, v]) => v !== undefined && v !== null && qs.append(k, String(v))
    );
    return request<PagedTasksOut>(`/tasks?${qs}`);
  },
  getRequestTasks: async (id: string) => {
    const page = await request<PagedTasksOut>(
      `/tasks?assignee=*&request_id=${encodeURIComponent(id)}&page_size=100`
    );
    return ensureArray<TaskOut>(page);
  },
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
  listAudit: (
    params: {
      actor?: string;
      action?: string;
      resource_type?: string;
      resource_id?: string;
      since?: string;
      until?: string;
      limit?: number;
    } = {}
  ) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v && qs.append(k, String(v)));
    return request<AuditActionOut[]>(`/admin/audit?${qs}`);
  },
  reassignTask: (taskId: string, newAssignee: string, reason?: string) =>
    request<TaskOut>(`/tasks/${encodeURIComponent(taskId)}/reassign`, {
      method: "POST",
      body: JSON.stringify({ new_assignee: newAssignee, reason }),
    }),
  listDelegations: (userId?: string) => {
    const qs = new URLSearchParams();
    if (userId) qs.append("user_id", userId);
    return request<DelegationOut[]>(`/delegations?${qs}`);
  },
  createDelegation: (payload: DelegationCreate) =>
    request<DelegationOut>(`/delegations`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteDelegation: (id: string) =>
    request<void>(`/delegations/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
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
  forbid_self_approval?: boolean;
  forbid_repeat_approvers?: boolean;
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
  parallel_group?: number | null;
  on_breach?: "notify" | "auto_approve" | "auto_reject" | "escalate" | null;
  on_empty?: "skip" | "block";
  skip_if?: Record<string, unknown> | null;
  rules: Rule[];
  escalation_rules?: Rule[];
}

export interface Rule {
  id: string;
  rule_type: string;
  rule_value: Record<string, unknown>;
  kind?: "approver" | "observer";
  required?: boolean;
}

// ---------------------------------------------------------------------------
// Shapes accepted by POST /policies (no `id` on nested rows)
// ---------------------------------------------------------------------------
export interface PolicyCreate {
  policy_key: string;
  name: string;
  description?: string;
  artifact_type: string;
  forbid_self_approval?: boolean;
  forbid_repeat_approvers?: boolean;
  stages: StageCreate[];
}

export interface StageCreate {
  name: string;
  stage_order: number;
  mode: string;
  mode_value?: number | null;
  sla_hours?: number | null;
  on_empty: "skip" | "block";
  parallel_group?: number | null;
  on_breach?: "notify" | "auto_approve" | "auto_reject" | "escalate" | null;
  escalation_rules?: RuleCreate[];
  skip_if?: Record<string, unknown> | null;
  rules: RuleCreate[];
}

export interface RuleCreate {
  rule_type: "user" | "role" | "group" | "expression" | "http";
  rule_value: Record<string, unknown>;
  kind?: "approver" | "observer";
  required?: boolean;
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
  kind?: "approver" | "observer";
  delegated_from?: string | null;
  reassigned_from?: string | null;
  status: string;
  claimed_at?: string | null;
  completed_at?: string | null;
  due_at?: string | null;
  decision_id?: string | null;
  created_at: string;
  context?: Record<string, unknown> | null;
  artifact_type?: string | null;
  artifact_id?: string | null;
  policy_key?: string | null;
  search_text?: string | null;
}

export interface TaskStatsOut {
  total: number;
  change_request_count: number;
  intake_form_count: number;
}

export interface PagedTasksOut {
  items: TaskOut[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface TaskQuery {
  assignee: string;
  request_id: string;
  status: string;
  artifact_type: string;
  policy_key: string;
  search_text: string;
  page: number;
  page_size: number;
}

export interface DelegationOut {
  id: string;
  user_id: string;
  delegate_to: string;
  starts_at: string;
  ends_at: string;
  reason?: string | null;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DelegationCreate {
  user_id: string;
  delegate_to: string;
  starts_at: string;
  ends_at: string;
  reason?: string;
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

export interface AuditActionOut {
  id: string;
  occurred_at: string;
  actor: string;
  actor_email?: string | null;
  action: string;
  resource_type: string;
  resource_id: string;
  summary?: string | null;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
}
