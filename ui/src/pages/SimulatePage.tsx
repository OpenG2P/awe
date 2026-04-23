import { useState } from "react";
import { useParams } from "react-router-dom";
import { api, type SimulateResponse } from "../api/client";

export default function SimulatePage() {
  const { policyKey = "", version = "1" } = useParams();
  const [contextStr, setContextStr] = useState("{}");
  const [result, setResult] = useState<SimulateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function run() {
    setError(null);
    setPending(true);
    try {
      const context = JSON.parse(contextStr);
      const out = await api.simulate(policyKey, Number(version), context);
      setResult(out);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <h1>Simulate · {policyKey} v{version}</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Paste a sample request context to see which approvers each stage
        resolves to. No database writes occur.
      </p>

      <div className="card">
        <h2>Context JSON</h2>
        <p style={{ color: "var(--color-text-muted)", fontSize: 13 }}>
          The caller sends this payload to{" "}
          <code>POST /v1/awe/requests</code>. Only{" "}
          <code>expression</code> and <code>http</code> rules read from it —{" "}
          <code>user</code>, <code>role</code>, and <code>group</code> rules
          ignore context entirely. Field names are whatever your expressions /
          caller-side resolver expect (AWE has no fixed schema).
        </p>
        <textarea
          value={contextStr}
          onChange={(e) => setContextStr(e.target.value)}
          rows={8}
          style={{
            width: "100%",
            fontFamily: "monospace",
            padding: 12,
            border: "1px solid var(--color-border)",
            borderRadius: "var(--radius-sm)",
          }}
        />
        <div style={{ marginTop: 12 }}>
          <button className="btn-primary" onClick={run} disabled={pending}>
            {pending ? "Running…" : "Run simulation"}
          </button>
        </div>
        {error && (
          <p style={{ color: "var(--color-danger)", marginTop: 12 }}>{error}</p>
        )}
      </div>

      {result && (
        <div className="card">
          <h2>Resolved stages</h2>
          {result.stages.map((s) => (
            <div
              key={s.stage_order}
              style={{
                borderBottom: "1px solid var(--color-light-grey)",
                padding: "12px 0",
              }}
            >
              <strong>
                Stage {s.stage_order} · {s.name}
              </strong>{" "}
              <span style={{ color: "var(--color-text-muted)" }}>
                ({s.mode}
                {s.mode_value ? ` : ${s.mode_value}` : ""})
              </span>
              {s.skipped ? (
                <p style={{ color: "var(--color-text-muted)" }}>
                  Skipped ({s.skip_reason})
                </p>
              ) : (
                <ul>
                  {s.resolved_approvers.map((a) => (
                    <li key={a}>{a}</li>
                  ))}
                  {s.resolved_approvers.length === 0 && (
                    <li style={{ color: "var(--color-danger)" }}>
                      No approvers resolved — stage would block.
                    </li>
                  )}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  );
}
