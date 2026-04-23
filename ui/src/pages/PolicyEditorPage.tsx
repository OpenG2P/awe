import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

export default function PolicyEditorPage() {
  const { policyKey = "" } = useParams();
  const qc = useQueryClient();

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
        <Link
          to={`/policies/${encodeURIComponent(policyKey)}/versions/new`}
          className="btn-primary"
          style={{ textDecoration: "none" }}
        >
          + New draft version
        </Link>
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
                      >
                        Simulate
                      </Link>
                      {v.status === "draft" && (
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
                          >
                            {activate.isPending &&
                            activate.variables === v.version
                              ? "Activating…"
                              : "Activate"}
                          </button>
                        </>
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
      </div>
    </>
  );
}
