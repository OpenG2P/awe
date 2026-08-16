import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type Rule, type Stage } from "../api/client";

// Read-only view of one policy version: metadata, stages, and the rules that
// resolve approvers. Purely a GET — no mutations here, so it is safe for
// AWE_VIEWER. Editing still lives in PolicyFormPage (drafts only).

function describeMode(stage: Stage): string {
  switch (stage.mode) {
    case "all":
      return "All approvers must approve";
    case "any-n":
    case "quorum":
      return `Any ${stage.mode_value ?? 1} approval${
        (stage.mode_value ?? 1) === 1 ? "" : "s"
      } complete the stage`;
    case "percentage":
      return `${stage.mode_value ?? 100}% of approvers must approve`;
    default:
      return stage.mode;
  }
}

function describeOnBreach(stage: Stage): string {
  switch (stage.on_breach) {
    case "auto_approve":
      return "Auto-approve the stage";
    case "auto_reject":
      return "Auto-reject the request";
    case "escalate":
      return "Escalate — add the approvers below";
    case "notify":
    default:
      return "Notify only (Caller decides)";
  }
}

// Render a rule's payload the way a policy author would read it, rather than
// dumping raw JSON. Falls back to JSON for anything unrecognised.
function describeRule(rule: Rule): string {
  const v = rule.rule_value || {};
  switch (rule.rule_type) {
    case "user":
      return String(v.user_id ?? "—");
    case "role":
      return v.client
        ? `${String(v.role ?? "—")}  (client: ${String(v.client)})`
        : `${String(v.role ?? "—")}  (realm role)`;
    case "group":
      return String(v.group ?? "—");
    case "http":
      return String(v.url ?? "—");
    case "expression":
      return JSON.stringify(v.logic ?? {});
    default:
      return JSON.stringify(v);
  }
}

function RuleTable({ rules }: { rules: Rule[] }) {
  return (
    <table style={{ marginTop: 8 }}>
      <thead>
        <tr>
          <th style={{ width: 140 }}>Type</th>
          <th>Value</th>
          <th style={{ width: 120 }}>Kind</th>
          <th style={{ width: 100 }}>Required</th>
        </tr>
      </thead>
      <tbody>
        {rules.map((r) => (
          <tr key={r.id}>
            <td>
              <code>{r.rule_type}</code>
            </td>
            <td style={{ fontFamily: "monospace", fontSize: 13 }}>
              {describeRule(r)}
            </td>
            <td>{r.kind === "observer" ? "observer" : "approver"}</td>
            <td>{r.required ? "yes" : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ minWidth: 180 }}>
      <div
        style={{
          fontSize: 12,
          fontWeight: 500,
          color: "var(--color-text-muted)",
          marginBottom: 4,
        }}
      >
        {label}
      </div>
      <div>{value}</div>
    </div>
  );
}

export default function PolicyVersionPage() {
  const { policyKey = "", version = "1" } = useParams();
  const versionNum = Number(version);

  const { data: policy, isLoading, error } = useQuery({
    queryKey: ["policy-version", policyKey, versionNum],
    queryFn: () => api.getVersion(policyKey, versionNum),
  });

  // Stages sharing a parallel_group run concurrently; a null group means the
  // stage is its own group (strictly sequential). Group them for display so
  // the concurrency is visible at a glance.
  const groups: Stage[][] = [];
  if (policy) {
    const ordered = [...policy.stages].sort(
      (a, b) => a.stage_order - b.stage_order
    );
    for (const stage of ordered) {
      const prev = groups[groups.length - 1];
      const sameGroup =
        prev &&
        stage.parallel_group != null &&
        prev[0].parallel_group === stage.parallel_group;
      if (sameGroup) prev.push(stage);
      else groups.push([stage]);
    }
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
          <h1>
            {policyKey} · v{version}
          </h1>
          <Link to={`/policies/${encodeURIComponent(policyKey)}`}>
            ← Back to versions
          </Link>
        </div>
        <Link
          to={`/policies/${encodeURIComponent(
            policyKey
          )}/versions/${version}/simulate`}
          className="icon-btn orange"
        >
          Simulate
        </Link>
      </div>

      {isLoading && <p style={{ marginTop: 24 }}>Loading…</p>}
      {error && (
        <p style={{ marginTop: 24, color: "var(--color-danger)" }}>
          Failed to load policy version.
        </p>
      )}

      {policy && (
        <>
          <div className="card" style={{ marginTop: 24 }}>
            <h2>Overview</h2>
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 24,
                marginTop: 12,
              }}
            >
              <Field label="Name" value={policy.name} />
              <Field
                label="Status"
                value={
                  <span className={`status-pill status-${policy.status}`}>
                    {policy.status}
                  </span>
                }
              />
              <Field
                label="Artifact type"
                value={<code>{policy.artifact_type}</code>}
              />
              <Field
                label="Self-approval"
                value={policy.forbid_self_approval ? "Forbidden" : "Allowed"}
              />
              <Field
                label="Repeat approvers"
                value={
                  policy.forbid_repeat_approvers ? "Forbidden" : "Allowed"
                }
              />
              <Field
                label="Created"
                value={new Date(policy.created_at).toLocaleString()}
              />
            </div>
            {policy.description && (
              <p style={{ marginTop: 16, color: "var(--color-text-muted)" }}>
                {policy.description}
              </p>
            )}
          </div>

          <div className="card" style={{ marginTop: 24 }}>
            <h2>Stages</h2>
            {groups.length === 0 && (
              <p style={{ color: "var(--color-text-muted)" }}>
                This policy has no stages — requests are approved immediately.
              </p>
            )}

            {groups.map((group, gi) => (
              <div key={gi} style={{ marginTop: gi === 0 ? 12 : 20 }}>
                {group.length > 1 && (
                  <div
                    style={{
                      fontSize: 12,
                      fontWeight: 500,
                      color: "var(--color-text-muted)",
                      marginBottom: 8,
                    }}
                  >
                    Parallel group {group[0].parallel_group} — these{" "}
                    {group.length} stages run concurrently and must all approve
                    before the flow advances.
                  </div>
                )}

                {group.map((stage) => {
                  const approverRules = stage.rules.filter(
                    (r) => r.kind !== "observer"
                  );
                  const observerRules = stage.rules.filter(
                    (r) => r.kind === "observer"
                  );
                  return (
                    <div
                      key={stage.id}
                      style={{
                        border: "1px solid var(--color-border)",
                        borderRadius: "var(--radius-sm)",
                        padding: 16,
                        marginBottom: 12,
                        background: "var(--color-light-grey)",
                      }}
                    >
                      <h3
                        style={{
                          margin: 0,
                          fontSize: 15,
                          color: "var(--color-black)",
                        }}
                      >
                        Stage {stage.stage_order} · {stage.name}
                      </h3>

                      <div
                        style={{
                          display: "flex",
                          flexWrap: "wrap",
                          gap: 20,
                          marginTop: 12,
                        }}
                      >
                        <Field label="Mode" value={describeMode(stage)} />
                        <Field
                          label="SLA"
                          value={
                            stage.sla_hours ? `${stage.sla_hours} h` : "None"
                          }
                        />
                        {stage.sla_hours ? (
                          <Field
                            label="On SLA breach"
                            value={describeOnBreach(stage)}
                          />
                        ) : null}
                        <Field
                          label="If no approvers resolve"
                          value={
                            stage.on_empty === "skip"
                              ? "Skip the stage"
                              : "Reject the request"
                          }
                        />
                      </div>

                      {stage.skip_if && (
                        <div style={{ marginTop: 12 }}>
                          <div
                            style={{
                              fontSize: 12,
                              fontWeight: 500,
                              color: "var(--color-text-muted)",
                              marginBottom: 4,
                            }}
                          >
                            Skip condition (JSONLogic over request context)
                          </div>
                          <pre
                            style={{
                              margin: 0,
                              fontSize: 12,
                              overflowX: "auto",
                            }}
                          >
                            {JSON.stringify(stage.skip_if, null, 2)}
                          </pre>
                        </div>
                      )}

                      <div style={{ marginTop: 12 }}>
                        <div
                          style={{
                            fontSize: 12,
                            fontWeight: 500,
                            color: "var(--color-text-muted)",
                          }}
                        >
                          Approver rules
                        </div>
                        {approverRules.length > 0 ? (
                          <RuleTable rules={approverRules} />
                        ) : (
                          <p
                            style={{
                              color: "var(--color-text-muted)",
                              fontSize: 13,
                            }}
                          >
                            None.
                          </p>
                        )}
                      </div>

                      {observerRules.length > 0 && (
                        <div style={{ marginTop: 12 }}>
                          <div
                            style={{
                              fontSize: 12,
                              fontWeight: 500,
                              color: "var(--color-text-muted)",
                            }}
                          >
                            Observers (notified, do not gate completion)
                          </div>
                          <RuleTable rules={observerRules} />
                        </div>
                      )}

                      {stage.on_breach === "escalate" &&
                        stage.escalation_rules &&
                        stage.escalation_rules.length > 0 && (
                          <div style={{ marginTop: 12 }}>
                            <div
                              style={{
                                fontSize: 12,
                                fontWeight: 500,
                                color: "var(--color-text-muted)",
                              }}
                            >
                              Escalation rules (added on SLA breach)
                            </div>
                            <RuleTable rules={stage.escalation_rules} />
                          </div>
                        )}
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}
