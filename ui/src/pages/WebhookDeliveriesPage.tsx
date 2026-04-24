import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type DeliveryOut } from "../api/client";
import { getCurrentUser } from "../auth";

const STATUS_OPTIONS = [
  { value: "", label: "All" },
  { value: "pending", label: "pending" },
  { value: "delivered", label: "delivered" },
  { value: "failed", label: "failed" },
  { value: "exhausted", label: "exhausted" },
];

export default function WebhookDeliveriesPage() {
  const [status, setStatus] = useState("failed");
  const qc = useQueryClient();
  const user = getCurrentUser();
  const { data, isLoading, error } = useQuery({
    queryKey: ["deliveries", status],
    queryFn: () => api.listDeliveries({ status: status || undefined }),
    refetchInterval: 10_000,
  });

  const retry = useMutation({
    mutationFn: (id: string) => api.retryDelivery(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["deliveries"] }),
  });

  return (
    <>
      <h1>Webhook Deliveries</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Outbound POSTs from AWE to each caller's <code>callback_url</code>. An
        exhausted delivery has hit <code>max_attempts</code> and stopped
        retrying automatically — click Retry to re-queue it.
      </p>

      <div className="card">
        <div style={{ marginBottom: 16 }}>
          <label>
            Status filter:{" "}
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUS_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {isLoading && <p>Loading…</p>}
        {error && (
          <p style={{ color: "var(--color-danger)" }}>
            Failed to load deliveries (do you have the <code>awe-admin</code>{" "}
            role?).
          </p>
        )}
        {data && data.length === 0 && (
          <p style={{ color: "var(--color-text-muted)" }}>
            No deliveries match this filter.
          </p>
        )}
        {data && data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Event</th>
                <th>Request</th>
                <th>URL</th>
                <th>Attempt</th>
                <th>Status</th>
                <th>Last error</th>
                <th>Next retry</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.map((d) => (
                <DeliveryRow
                  key={d.id}
                  d={d}
                  canRetry={user.isAdmin}
                  onRetry={() => retry.mutate(d.id)}
                  pending={retry.isPending && retry.variables === d.id}
                />
              ))}
            </tbody>
          </table>
        )}
        {retry.error && (
          <p style={{ color: "var(--color-danger)", marginTop: 12 }}>
            Retry failed: {String(retry.error)}
          </p>
        )}
      </div>
    </>
  );
}

function DeliveryRow({
  d,
  canRetry,
  onRetry,
  pending,
}: {
  d: DeliveryOut;
  canRetry: boolean;
  onRetry: () => void;
  pending: boolean;
}) {
  return (
    <tr>
      <td style={{ fontSize: 12 }}>{d.event_type}</td>
      <td style={{ fontFamily: "monospace", fontSize: 12 }}>
        {d.request_id.slice(0, 8)}…
      </td>
      <td
        style={{
          fontFamily: "monospace",
          fontSize: 12,
          maxWidth: 220,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
        title={d.url}
      >
        {d.url}
      </td>
      <td>{d.attempt}</td>
      <td>
        <span className={`status-pill status-${d.status}`}>{d.status}</span>
        {d.last_status_code != null && (
          <span style={{ marginLeft: 6, color: "var(--color-text-muted)" }}>
            HTTP {d.last_status_code}
          </span>
        )}
      </td>
      <td
        style={{
          fontSize: 12,
          color: "var(--color-text-muted)",
          maxWidth: 280,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
        title={d.last_error ?? ""}
      >
        {d.last_error ?? "—"}
      </td>
      <td style={{ fontSize: 12 }}>
        {d.status === "delivered"
          ? "—"
          : new Date(d.next_attempt_at).toLocaleString()}
      </td>
      <td>
        {canRetry && d.status !== "delivered" && (
          <button
            className="icon-btn"
            onClick={onRetry}
            disabled={pending}
            title="Reset attempts and re-queue for immediate delivery"
          >
            {pending ? "Retrying…" : "Retry"}
          </button>
        )}
      </td>
    </tr>
  );
}
