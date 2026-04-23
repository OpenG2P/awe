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
            <option value="quorum">quorum — alias for any-n</option>
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
    </div>
  );
}
