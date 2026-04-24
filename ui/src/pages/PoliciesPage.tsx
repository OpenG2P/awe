import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export default function PoliciesPage() {
  const navigate = useNavigate();
  const { data, isLoading, error } = useQuery({
    queryKey: ["policies"],
    queryFn: api.listPolicies,
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
          <h1>Policies</h1>
          <p style={{ color: "var(--color-text-muted)" }}>
            Versioned approval blueprints. Edit creates a new draft; activating
            a draft archives the previously active version.
          </p>
        </div>
        <button className="btn-primary" onClick={() => navigate("/policies/new")}>
          + New policy
        </button>
      </div>
      <div className="card">
        {isLoading && <p>Loading…</p>}
        {error && <p>Failed to load policies.</p>}
        {data && data.length === 0 && <p>No policies yet.</p>}
        {data && data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Policy key</th>
                <th>Name</th>
                <th>Artifact type</th>
                <th>Latest version</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.map((p) => (
                <tr key={p.id}>
                  <td>{p.policy_key}</td>
                  <td>{p.name}</td>
                  <td>{p.artifact_type}</td>
                  <td>v{p.version}</td>
                  <td>
                    <span className={`status-pill status-${p.status}`}>
                      {p.status}
                    </span>
                  </td>
                  <td>
                    <Link
                      to={`/policies/${encodeURIComponent(p.policy_key)}`}
                      className="icon-btn success"
                    >
                      Open
                    </Link>
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
