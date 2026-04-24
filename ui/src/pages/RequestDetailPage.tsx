import { Link, useParams } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";
import { api } from "../api/client";

export default function RequestDetailPage() {
  const { requestId = "" } = useParams();

  const [requestQ, tasksQ, eventsQ, deliveriesQ] = useQueries({
    queries: [
      { queryKey: ["request", requestId], queryFn: () => api.getRequest(requestId) },
      {
        queryKey: ["request-tasks", requestId],
        queryFn: () => api.getRequestTasks(requestId),
      },
      {
        queryKey: ["request-events", requestId],
        queryFn: () => api.getRequestEvents(requestId),
      },
      {
        queryKey: ["request-deliveries", requestId],
        queryFn: () => api.listDeliveries({ request_id: requestId }),
      },
    ],
  });

  const request = requestQ.data;
  const tasks = tasksQ.data ?? [];
  const events = eventsQ.data ?? [];
  const deliveries = deliveriesQ.data ?? [];

  if (requestQ.isLoading) return <p>Loading…</p>;
  if (!request) {
    return (
      <>
        <h1>Request not found</h1>
        <Link to="/requests">← Back to requests</Link>
      </>
    );
  }

  // Group tasks by stage_order
  const tasksByStage = new Map<number, typeof tasks>();
  for (const t of tasks) {
    const arr = tasksByStage.get(t.stage_order) ?? [];
    arr.push(t);
    tasksByStage.set(t.stage_order, arr);
  }
  const stageOrders = Array.from(tasksByStage.keys()).sort((a, b) => a - b);

  return (
    <>
      <Link to="/requests">← Back to requests</Link>
      <h1 style={{ marginTop: 8 }}>
        {request.artifact_type}/{request.artifact_id}{" "}
        <span className={`status-pill status-${request.status}`}>
          {request.status}
        </span>
      </h1>
      <p style={{ color: "var(--color-text-muted)", fontFamily: "monospace", fontSize: 12 }}>
        request_id: {request.id} · policy: {request.policy_key} v
        {request.policy_version} · stage {request.current_stage_order}
      </p>

      <div className="card">
        <h2>Context</h2>
        <pre
          style={{
            background: "var(--color-light-grey)",
            padding: 12,
            borderRadius: "var(--radius-sm)",
            fontSize: 12,
            margin: 0,
            overflow: "auto",
          }}
        >
          {JSON.stringify(request.context, null, 2)}
        </pre>
      </div>

      <div className="card">
        <h2>Stages</h2>
        {stageOrders.length === 0 && (
          <p style={{ color: "var(--color-text-muted)" }}>
            No tasks yet. If the request is still <code>pending</code>, stage 1
            resolution may have failed — see the event timeline below.
          </p>
        )}
        {stageOrders.map((order) => (
          <div
            key={order}
            style={{
              borderLeft:
                order === request.current_stage_order
                  ? "3px solid var(--color-yellow)"
                  : "3px solid var(--color-border)",
              paddingLeft: 12,
              marginBottom: 16,
            }}
          >
            <strong>Stage {order}</strong>
            {order === request.current_stage_order && (
              <span
                style={{
                  marginLeft: 8,
                  fontSize: 11,
                  color: "var(--color-accent-hover)",
                  fontWeight: 500,
                }}
              >
                CURRENT
              </span>
            )}
            <table style={{ marginTop: 8 }}>
              <thead>
                <tr>
                  <th>Assignee</th>
                  <th>Status</th>
                  <th>Due</th>
                  <th>Claimed</th>
                  <th>Completed</th>
                </tr>
              </thead>
              <tbody>
                {tasksByStage.get(order)!.map((t) => (
                  <tr key={t.id}>
                    <td>{t.assignee}</td>
                    <td>
                      <span className={`status-pill status-${t.status}`}>
                        {t.status}
                      </span>
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {t.due_at ? new Date(t.due_at).toLocaleString() : "—"}
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {t.claimed_at
                        ? new Date(t.claimed_at).toLocaleString()
                        : "—"}
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {t.completed_at
                        ? new Date(t.completed_at).toLocaleString()
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>

      <div className="card">
        <h2>Event timeline</h2>
        {events.length === 0 && (
          <p style={{ color: "var(--color-text-muted)" }}>No events yet.</p>
        )}
        <ol style={{ paddingLeft: 20 }}>
          {events.map((e) => (
            <li key={e.id} style={{ marginBottom: 8 }}>
              <code>{e.event_type}</code>{" "}
              <span style={{ color: "var(--color-text-muted)", fontSize: 12 }}>
                {new Date(e.created_at).toLocaleString()}
              </span>
              {Object.keys(e.payload).length > 0 && (
                <pre
                  style={{
                    background: "var(--color-light-grey)",
                    padding: 8,
                    borderRadius: "var(--radius-sm)",
                    fontSize: 11,
                    margin: "4px 0 0",
                    overflow: "auto",
                  }}
                >
                  {JSON.stringify(e.payload, null, 2)}
                </pre>
              )}
            </li>
          ))}
        </ol>
      </div>

      <div className="card">
        <h2>Webhook deliveries</h2>
        {deliveries.length === 0 && (
          <p style={{ color: "var(--color-text-muted)" }}>
            No deliveries enqueued — either no <code>callback_url</code> was
            set on this request, or no deliverable event has fired yet.
          </p>
        )}
        {deliveries.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Event</th>
                <th>Attempt</th>
                <th>Status</th>
                <th>Last status code</th>
                <th>Next retry</th>
              </tr>
            </thead>
            <tbody>
              {deliveries.map((d) => (
                <tr key={d.id}>
                  <td style={{ fontSize: 12 }}>{d.event_type}</td>
                  <td>{d.attempt}</td>
                  <td>
                    <span className={`status-pill status-${d.status}`}>
                      {d.status}
                    </span>
                  </td>
                  <td>{d.last_status_code ?? "—"}</td>
                  <td style={{ fontSize: 12 }}>
                    {d.status === "delivered"
                      ? "—"
                      : new Date(d.next_attempt_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
