import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type AuditActionOut } from "../api/client";

const ACTIONS = [
  { value: "", label: "All actions" },
  { value: "policy.create", label: "policy.create" },
  { value: "policy.update", label: "policy.update" },
  { value: "policy.add_version", label: "policy.add_version" },
  { value: "policy.activate", label: "policy.activate" },
  { value: "policy.deactivate", label: "policy.deactivate" },
  { value: "request.cancel", label: "request.cancel" },
  { value: "delivery.retry", label: "delivery.retry" },
];

const RESOURCE_TYPES = [
  { value: "", label: "All resources" },
  { value: "policy", label: "policy" },
  { value: "request", label: "request" },
  { value: "delivery", label: "delivery" },
];

export default function AuditLogPage() {
  const [action, setAction] = useState("");
  const [resourceType, setResourceType] = useState("");
  const [actor, setActor] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const { data, isLoading, error } = useQuery({
    queryKey: ["audit", action, resourceType, actor],
    queryFn: () =>
      api.listAudit({
        action: action || undefined,
        resource_type: resourceType || undefined,
        actor: actor || undefined,
        limit: 200,
      }),
  });

  function toggle(id: string) {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  }

  return (
    <>
      <h1>Audit Log</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Every admin / ops action — policy CRUD, request cancellation, delivery
        retry — is recorded here. Append-only, actor-attributed, with
        before/after snapshots where applicable.
      </p>

      <div className="card">
        <div style={{ display: "flex", gap: 16, marginBottom: 16, flexWrap: "wrap" }}>
          <label>
            Action:{" "}
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              {ACTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Resource:{" "}
            <select
              value={resourceType}
              onChange={(e) => setResourceType(e.target.value)}
            >
              {RESOURCE_TYPES.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Actor:{" "}
            <input
              value={actor}
              onChange={(e) => setActor(e.target.value)}
              placeholder="sub / user id"
              style={{ width: 180 }}
            />
          </label>
        </div>

        {isLoading && <p>Loading…</p>}
        {error && <p style={{ color: "var(--color-danger)" }}>Failed to load audit log.</p>}
        {data && data.length === 0 && (
          <p style={{ color: "var(--color-text-muted)" }}>No entries match.</p>
        )}
        {data && data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Resource</th>
                <th>Summary</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.map((row) => (
                <AuditRow
                  key={row.id}
                  row={row}
                  open={!!expanded[row.id]}
                  onToggle={() => toggle(row.id)}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function AuditRow({
  row,
  open,
  onToggle,
}: {
  row: AuditActionOut;
  open: boolean;
  onToggle: () => void;
}) {
  const hasDiff = row.before || row.after || row.metadata;

  return (
    <>
      <tr>
        <td style={{ fontSize: 12, whiteSpace: "nowrap" }}>
          {new Date(row.occurred_at).toLocaleString()}
        </td>
        <td style={{ fontSize: 13 }}>
          <div>{row.actor_email || row.actor}</div>
          {row.actor_email && (
            <div style={{ fontSize: 11, color: "var(--color-text-muted)" }}>
              {row.actor}
            </div>
          )}
        </td>
        <td>
          <code style={{ fontSize: 12 }}>{row.action}</code>
        </td>
        <td style={{ fontSize: 12 }}>
          {row.resource_type}/{row.resource_id}
        </td>
        <td>{row.summary || "—"}</td>
        <td>
          {hasDiff && (
            <button className="icon-btn" onClick={onToggle}>
              {open ? "Hide" : "Show"} diff
            </button>
          )}
        </td>
      </tr>
      {open && hasDiff && (
        <tr>
          <td
            colSpan={6}
            style={{ background: "var(--color-light-grey)", padding: 16 }}
          >
            <DiffView
              before={row.before ?? null}
              after={row.after ?? null}
              metadata={row.metadata ?? null}
            />
          </td>
        </tr>
      )}
    </>
  );
}

function DiffView({
  before,
  after,
  metadata,
}: {
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  metadata: Record<string, unknown> | null;
}) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      <section>
        <strong style={{ fontSize: 12 }}>Before</strong>
        <pre style={diffPre}>{before ? JSON.stringify(before, null, 2) : "—"}</pre>
      </section>
      <section>
        <strong style={{ fontSize: 12 }}>After</strong>
        <pre style={diffPre}>{after ? JSON.stringify(after, null, 2) : "—"}</pre>
      </section>
      {metadata && (
        <section style={{ gridColumn: "1 / -1" }}>
          <strong style={{ fontSize: 12 }}>Metadata</strong>
          <pre style={diffPre}>{JSON.stringify(metadata, null, 2)}</pre>
        </section>
      )}
    </div>
  );
}

const diffPre: React.CSSProperties = {
  background: "var(--color-white)",
  border: "1px solid var(--color-border)",
  borderRadius: "var(--radius-sm)",
  padding: 10,
  fontSize: 11,
  margin: "4px 0 0",
  overflow: "auto",
  maxHeight: 320,
};
