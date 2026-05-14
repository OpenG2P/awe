import type { RuleCreate, StageCreate } from "../api/client";
import RuleEditor from "./RuleEditor";

interface Props {
  stage: StageCreate;
  canMoveUp: boolean;
  canMoveDown: boolean;
  canRemove: boolean;
  onChange: (stage: StageCreate) => void;
  onRemove: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
}

export default function StageEditor({
  stage,
  canMoveUp,
  canMoveDown,
  canRemove,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
}: Props) {
  function patch(partial: Partial<StageCreate>) {
    onChange({ ...stage, ...partial });
  }

  function addRule() {
    const blank: RuleCreate = { rule_type: "user", rule_value: { user_id: "" } };
    patch({ rules: [...stage.rules, blank] });
  }

  function updateRule(idx: number, rule: RuleCreate) {
    patch({ rules: stage.rules.map((r, i) => (i === idx ? rule : r)) });
  }

  function removeRule(idx: number) {
    patch({ rules: stage.rules.filter((_, i) => i !== idx) });
  }

  const needsModeValue =
    stage.mode === "any-n" || stage.mode === "quorum" || stage.mode === "percentage";

  return (
    <div className="stage-block">
      <div className="stage-header">
        <h3>Stage {stage.stage_order}</h3>
        <div className="stage-actions">
          <button
            className="icon-btn"
            onClick={onMoveUp}
            disabled={!canMoveUp}
            title="Move up"
          >
            ↑
          </button>
          <button
            className="icon-btn"
            onClick={onMoveDown}
            disabled={!canMoveDown}
            title="Move down"
          >
            ↓
          </button>
          <button
            className="icon-btn danger"
            onClick={onRemove}
            disabled={!canRemove}
            title="Remove stage"
          >
            Remove
          </button>
        </div>
      </div>

      <div className="form-row">
        <label>
          Name
          <input
            value={stage.name}
            onChange={(e) => patch({ name: e.target.value })}
            placeholder="District officers"
          />
        </label>
        <label>
          Mode
          <select
            value={stage.mode}
            onChange={(e) => patch({ mode: e.target.value })}
          >
            <option value="all">all — every approver must approve</option>
            <option value="any-n">any-n — first N approvals suffice</option>
            <option value="percentage">percentage — ceil(P% of approvers)</option>
          </select>
        </label>
        <label style={{ maxWidth: 140 }}>
          {stage.mode === "percentage" ? "Percent" : "N"}
          <input
            type="number"
            min={1}
            value={stage.mode_value ?? ""}
            onChange={(e) =>
              patch({
                mode_value: e.target.value ? Number(e.target.value) : null,
              })
            }
            disabled={!needsModeValue}
            placeholder={needsModeValue ? (stage.mode === "percentage" ? "e.g. 60" : "e.g. 1") : "—"}
          />
        </label>
      </div>

      <div className="form-row">
        <label style={{ maxWidth: 160 }}>
          SLA (hours, optional)
          <input
            type="number"
            min={1}
            value={stage.sla_hours ?? ""}
            onChange={(e) =>
              patch({
                sla_hours: e.target.value ? Number(e.target.value) : null,
              })
            }
          />
        </label>
        <label style={{ maxWidth: 200 }}>
          If no approvers resolve
          <select
            value={stage.on_empty}
            onChange={(e) =>
              patch({ on_empty: e.target.value as "skip" | "block" })
            }
          >
            <option value="block">block — reject the request</option>
            <option value="skip">skip — advance to next stage</option>
          </select>
        </label>
        <label
          style={{ maxWidth: 160 }}
          title="Stages sharing this number run in parallel and must all approve before the group advances. Leave blank for sequential."
        >
          Parallel group
          <input
            type="number"
            min={1}
            value={stage.parallel_group ?? ""}
            onChange={(e) =>
              patch({
                parallel_group: e.target.value
                  ? Number(e.target.value)
                  : null,
              })
            }
            placeholder="(sequential)"
          />
        </label>
      </div>

      <div className="form-row">
        <label
          style={{ maxWidth: 220 }}
          title="What to do when every open task in this stage has crossed its SLA."
        >
          On SLA breach
          <select
            value={stage.on_breach ?? ""}
            onChange={(e) =>
              patch({
                on_breach: e.target.value
                  ? (e.target.value as
                      | "notify"
                      | "auto_approve"
                      | "auto_reject"
                      | "escalate")
                  : null,
              })
            }
            disabled={!stage.sla_hours}
          >
            <option value="">notify (default)</option>
            <option value="auto_approve">auto-approve the stage</option>
            <option value="auto_reject">auto-reject the request</option>
            <option value="escalate">escalate (use rules below)</option>
          </select>
        </label>
        <label
          style={{ flex: 1 }}
          title='Optional JSONLogic that, when truthy against the request context, skips this stage. e.g. {"<=":[{"var":"amount"},10000]}'
        >
          Skip if (JSONLogic, optional)
          <input
            type="text"
            value={
              stage.skip_if ? JSON.stringify(stage.skip_if) : ""
            }
            onChange={(e) => {
              const v = e.target.value.trim();
              if (!v) {
                patch({ skip_if: null });
                return;
              }
              try {
                patch({ skip_if: JSON.parse(v) });
              } catch {
                /* leave it; saved JSON validates server-side */
              }
            }}
            placeholder='e.g. {"<=":[{"var":"amount"},10000]}'
            style={{ fontFamily: "monospace", fontSize: 12 }}
          />
        </label>
      </div>

      <div>
        <div className="card-header" style={{ marginBottom: 8 }}>
          <label style={{ margin: 0 }}>Approver rules</label>
          <button className="icon-btn" onClick={addRule}>
            + Add rule
          </button>
        </div>
        {stage.rules.length === 0 && (
          <p style={{ color: "var(--color-text-muted)", fontSize: 13 }}>
            No rules yet — click “Add rule” to pick approvers by user / role /
            group / JSONLogic / HTTP.
          </p>
        )}
        <div className="rules-list">
          {stage.rules.map((rule, idx) => (
            <RuleEditor
              key={idx}
              rule={rule}
              onChange={(r) => updateRule(idx, r)}
              onRemove={() => removeRule(idx)}
            />
          ))}
        </div>
      </div>

      {stage.on_breach === "escalate" && (
        <div style={{ marginTop: 12 }}>
          <div className="card-header" style={{ marginBottom: 8 }}>
            <label style={{ margin: 0 }}>Escalation rules</label>
            <button
              className="icon-btn"
              onClick={() =>
                patch({
                  escalation_rules: [
                    ...(stage.escalation_rules ?? []),
                    { rule_type: "user", rule_value: { user_id: "" } },
                  ],
                })
              }
            >
              + Add rule
            </button>
          </div>
          <p style={{ color: "var(--color-text-muted)", fontSize: 12 }}>
            Resolved when SLA breaches; resulting users are added as fresh
            approvers on this stage.
          </p>
          <div className="rules-list">
            {(stage.escalation_rules ?? []).map((rule, idx) => (
              <RuleEditor
                key={`esc-${idx}`}
                rule={rule}
                onChange={(r) =>
                  patch({
                    escalation_rules: (stage.escalation_rules ?? []).map((er, i) =>
                      i === idx ? r : er
                    ),
                  })
                }
                onRemove={() =>
                  patch({
                    escalation_rules: (stage.escalation_rules ?? []).filter(
                      (_, i) => i !== idx
                    ),
                  })
                }
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
