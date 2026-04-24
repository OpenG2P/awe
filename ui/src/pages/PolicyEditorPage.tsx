import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { getCurrentUser } from "../auth";

// Fixed width so all action buttons (Simulate / Activate / Deactivate)
// line up in the actions column even though their labels differ in length.
// Sized to fit "Deactivate" snugly at rest; pending labels may wrap briefly.
const actionBtnStyle: React.CSSProperties = { minWidth: 80 };

export default function PolicyEditorPage() {
  const { policyKey = "" } = useParams();
  const qc = useQueryClient();
  const user = getCurrentUser();

  const { data: versions, isLoading } = useQuery({
    queryKey: ["policy-versions", policyKey],
    queryFn: () => api.getVersions(policyKey),
  });

  const activate = useMutation({
    mutationFn: (version: number) => api.activate(policyKey, version),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["policy-versions", policyKey] });
      qc.invalidateQueries({ queryKey: ["policies"] });
    },
  });

  const deactivate = useMutation({
    mutationFn: (version: number) => api.deactivate(policyKey, version),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["policy-versions", policyKey] });
      qc.invalidateQueries({ queryKey: ["policies"] });
    },
  });

  function confirmAndDeactivate(version: number) {
    const ok = window.confirm(
      `Deactivate ${policyKey} v${version}?\n\n` +
        "Until another version is activated, POST /requests for this " +
        "policy_key will fail. In-flight requests are unaffected."
    );
    if (ok) deactivate.mutate(version);
  }

  return (
    <>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
        }}
      >
        <div>
          <h1>{policyKey}</h1>
          <Link to="/policies">← Back to policies</Link>
        </div>
        {user.isAdmin && (
          <Link
            to={`/policies/${encodeURIComponent(policyKey)}/versions/new`}
            className="btn-primary"
          >
            + New draft version
          </Link>
        )}
      </div>

      <div className="card" style={{ marginTop: 24 }}>
        <h2>Versions</h2>
        {isLoading && <p>Loading…</p>}
        {versions && (
          <table>
            <thead>
              <tr>
                <th>Version</th>
                <th>Status</th>
                <th>Created</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((v) => (
                <tr key={v.id}>
                  <td>v{v.version}</td>
                  <td>
                    <span className={`status-pill status-${v.status}`}>
                      {v.status}
                    </span>
                  </td>
                  <td>{new Date(v.created_at).toLocaleString()}</td>
                  <td style={{ textAlign: "right" }}>
                    <div
                      style={{
                        display: "flex",
                        gap: 8,
                        justifyContent: "flex-end",
                      }}
                    >
                      <Link
                        to={`/policies/${encodeURIComponent(
                          policyKey
                        )}/versions/${v.version}/simulate`}
                        className="icon-btn orange"
                        style={actionBtnStyle}
                      >
                        Simulate
                      </Link>
                      {v.status === "draft" && user.isAdmin && (
                        <>
                          <Link
                            to={`/policies/${encodeURIComponent(
                              policyKey
                            )}/versions/${v.version}/edit`}
                          >
                            Edit
                          </Link>
                          <button
                            className="icon-btn"
                            onClick={() => activate.mutate(v.version)}
                            disabled={activate.isPending}
                            style={actionBtnStyle}
                          >
                            {activate.isPending &&
                            activate.variables === v.version
                              ? "Activating…"
                              : "Activate"}
                          </button>
                        </>
                      )}
                      {v.status === "active" && user.isAdmin && (
                        <button
                          className="icon-btn danger"
                          onClick={() => confirmAndDeactivate(v.version)}
                          disabled={deactivate.isPending}
                          title="Archive this version without activating a replacement"
                          style={actionBtnStyle}
                        >
                          {deactivate.isPending &&
                          deactivate.variables === v.version
                            ? "Deactivating…"
                            : "Deactivate"}
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {activate.error && (
          <p style={{ color: "var(--color-danger)", marginTop: 12 }}>
            Activate failed: {String(activate.error)}
          </p>
        )}
        {deactivate.error && (
          <p style={{ color: "var(--color-danger)", marginTop: 12 }}>
            Deactivate failed: {String(deactivate.error)}
          </p>
        )}
      </div>
    </>
  );
}
