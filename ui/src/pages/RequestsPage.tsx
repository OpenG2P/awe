import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export default function RequestsPage() {
  const [statusFilter, setStatusFilter] = useState<string>("");
  const { data, isLoading } = useQuery({
    queryKey: ["requests", statusFilter],
    queryFn: () => api.listRequests({ status: statusFilter || undefined }),
  });

  return (
    <>
      <h1>Requests</h1>
      <div className="card">
        <div style={{ marginBottom: 16 }}>
          <label>
            Status filter:{" "}
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
            >
              <option value="">All</option>
              <option value="pending">pending</option>
              <option value="in_review">in_review</option>
              <option value="approved">approved</option>
              <option value="rejected">rejected</option>
              <option value="cancelled">cancelled</option>
            </select>
          </label>
        </div>
        {isLoading && <p>Loading…</p>}
        {data && (
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Artifact</th>
                <th>Policy</th>
                <th>Stage</th>
                <th>Status</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r) => (
                <tr key={r.id}>
                  <td style={{ fontFamily: "monospace", fontSize: 12 }}>
                    <Link to={`/requests/${r.id}`}>{r.id.slice(0, 8)}…</Link>
                  </td>
                  <td>
                    <Link to={`/requests/${r.id}`}>
                      {r.artifact_type}/{r.artifact_id}
                    </Link>
                  </td>
                  <td>
                    {r.policy_key} v{r.policy_version}
                  </td>
                  <td>{r.current_stage_order}</td>
                  <td>
                    <span className={`status-pill status-${r.status}`}>
                      {r.status}
                    </span>
                  </td>
                  <td>{new Date(r.created_at).toLocaleString()}</td>
                </tr>
              ))}
              {data.length === 0 && (
                <tr>
                  <td colSpan={6} style={{ color: "var(--color-text-muted)" }}>
                    No requests found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
