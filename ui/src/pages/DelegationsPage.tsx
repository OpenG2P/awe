import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { getCurrentUser } from "../auth";

function toLocal(iso: string): string {
  return new Date(iso).toLocaleString();
}

function nowLocal(): string {
  // 16-char "YYYY-MM-DDTHH:MM" suitable for <input type="datetime-local">.
  const d = new Date();
  const tz = d.getTimezoneOffset();
  const local = new Date(d.getTime() - tz * 60_000);
  return local.toISOString().slice(0, 16);
}

function plusDaysLocal(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  const tz = d.getTimezoneOffset();
  const local = new Date(d.getTime() - tz * 60_000);
  return local.toISOString().slice(0, 16);
}

export default function DelegationsPage() {
  const user = getCurrentUser();
  const qc = useQueryClient();
  const { data: delegations, isLoading } = useQuery({
    queryKey: ["delegations"],
    queryFn: () => api.listDelegations(),
  });

  const [userId, setUserId] = useState("");
  const [delegateTo, setDelegateTo] = useState("");
  const [startsAt, setStartsAt] = useState(nowLocal());
  const [endsAt, setEndsAt] = useState(plusDaysLocal(7));
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      api.createDelegation({
        user_id: userId.trim(),
        delegate_to: delegateTo.trim(),
        starts_at: new Date(startsAt).toISOString(),
        ends_at: new Date(endsAt).toISOString(),
        reason: reason.trim() || undefined,
      }),
    onSuccess: () => {
      setUserId("");
      setDelegateTo("");
      setReason("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["delegations"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteDelegation(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["delegations"] }),
  });

  function isActive(d: { starts_at: string; ends_at: string }) {
    const now = Date.now();
    return new Date(d.starts_at).getTime() <= now && new Date(d.ends_at).getTime() > now;
  }

  return (
    <>
      <h1>Delegations</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        While a delegation is active, any task that would be assigned to{" "}
        <code>user</code> is created for <code>delegate</code> instead. Existing
        tasks are not retroactively reassigned.
      </p>

      {user.isAdmin && (
        <div className="card">
          <h2>New delegation</h2>
          <div className="form-row">
            <label>
              User
              <input
                value={userId}
                onChange={(e) => setUserId(e.target.value)}
                placeholder="Keycloak user id, e.g. u-alice"
              />
            </label>
            <label>
              Delegate to
              <input
                value={delegateTo}
                onChange={(e) => setDelegateTo(e.target.value)}
                placeholder="e.g. u-bob"
              />
            </label>
          </div>
          <div className="form-row">
            <label>
              Starts at
              <input
                type="datetime-local"
                value={startsAt}
                onChange={(e) => setStartsAt(e.target.value)}
              />
            </label>
            <label>
              Ends at
              <input
                type="datetime-local"
                value={endsAt}
                onChange={(e) => setEndsAt(e.target.value)}
              />
            </label>
          </div>
          <label>
            Reason (optional)
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. on annual leave"
            />
          </label>
          <div style={{ marginTop: 12 }}>
            <button
              className="btn-primary"
              disabled={create.isPending || !userId.trim() || !delegateTo.trim()}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "Saving…" : "Create delegation"}
            </button>
            {error && (
              <span style={{ color: "var(--color-danger)", marginLeft: 12 }}>
                {error}
              </span>
            )}
          </div>
        </div>
      )}

      <div className="card">
        <h2>Active &amp; upcoming</h2>
        {isLoading && <p>Loading…</p>}
        {delegations && delegations.length === 0 && (
          <p style={{ color: "var(--color-text-muted)" }}>
            No delegations on file.
          </p>
        )}
        {delegations && delegations.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>User</th>
                <th>Delegate</th>
                <th>Window</th>
                <th>Reason</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {delegations.map((d) => (
                <tr key={d.id}>
                  <td>{d.user_id}</td>
                  <td>{d.delegate_to}</td>
                  <td style={{ fontSize: 12 }}>
                    {toLocal(d.starts_at)} → {toLocal(d.ends_at)}
                  </td>
                  <td>{d.reason || "—"}</td>
                  <td>
                    <span
                      className={`status-pill ${
                        isActive(d) ? "status-active" : "status-archived"
                      }`}
                    >
                      {isActive(d) ? "active" : "inactive"}
                    </span>
                  </td>
                  <td>
                    {user.isAdmin && (
                      <button
                        className="icon-btn danger"
                        onClick={() => {
                          if (
                            window.confirm(
                              `Delete delegation ${d.user_id} → ${d.delegate_to}?`
                            )
                          ) {
                            remove.mutate(d.id);
                          }
                        }}
                      >
                        Delete
                      </button>
                    )}
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
