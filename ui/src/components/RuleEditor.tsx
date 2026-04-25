import type { RuleCreate } from "../api/client";

interface Props {
  rule: RuleCreate;
  onChange: (r: RuleCreate) => void;
  onRemove: () => void;
}

// Each rule_type has a different value shape; switching type resets the value
// fields to a sensible blank for that type.
const BLANK_VALUE: Record<RuleCreate["rule_type"], Record<string, unknown>> = {
  user: { user_id: "" },
  role: { role: "" },
  group: { group: "" },
  expression: { logic: {} },
  http: { url: "" },
};

export default function RuleEditor({ rule, onChange, onRemove }: Props) {
  function setType(type: RuleCreate["rule_type"]) {
    onChange({
      rule_type: type,
      rule_value: { ...BLANK_VALUE[type] },
      kind: rule.kind,
      required: rule.required,
    });
  }

  function setField(key: string, value: unknown) {
    onChange({ ...rule, rule_value: { ...rule.rule_value, [key]: value } });
  }

  const isObserver = rule.kind === "observer";

  return (
    <div className="rule-row">
      <div className="rule-type-col">
        <select
          value={rule.rule_type}
          onChange={(e) => setType(e.target.value as RuleCreate["rule_type"])}
        >
          <option value="user">user</option>
          <option value="role">role</option>
          <option value="group">group</option>
          <option value="expression">expression (JSONLogic)</option>
          <option value="http">http</option>
        </select>
      </div>

      <div className="rule-value-col">{renderValueEditor(rule, setField)}</div>

      <select
        value={rule.kind ?? "approver"}
        onChange={(e) =>
          onChange({
            ...rule,
            kind: e.target.value as "approver" | "observer",
            required: e.target.value === "observer" ? false : rule.required,
          })
        }
        title="Approver decisions count toward stage completion. Observers can comment but do not block."
      >
        <option value="approver">approver</option>
        <option value="observer">observer</option>
      </select>

      <label
        style={{
          display: "flex",
          alignItems: "center",
          gap: 4,
          fontSize: 12,
          opacity: isObserver ? 0.4 : 1,
        }}
        title="If checked, every user resolved by this rule must approve, overriding the stage's quorum mode."
      >
        <input
          type="checkbox"
          checked={!!rule.required}
          disabled={isObserver}
          onChange={(e) => onChange({ ...rule, required: e.target.checked })}
        />
        required
      </label>

      <button className="icon-btn danger" onClick={onRemove} title="Remove rule">
        ×
      </button>
    </div>
  );
}

function renderValueEditor(
  rule: RuleCreate,
  setField: (k: string, v: unknown) => void
) {
  switch (rule.rule_type) {
    case "user":
      return (
        <input
          value={String(rule.rule_value.user_id ?? "")}
          onChange={(e) => setField("user_id", e.target.value)}
          placeholder="Keycloak user id, e.g. u-alice"
        />
      );
    case "role":
      return (
        <div style={{ display: "flex", gap: 8, flex: 1 }}>
          <input
            style={{ flex: 2 }}
            value={String(rule.rule_value.role ?? "")}
            onChange={(e) => setField("role", e.target.value)}
            placeholder="Role name, e.g. PROGRAM_MANAGER"
          />
          <input
            style={{ flex: 1 }}
            value={String(rule.rule_value.client ?? "")}
            onChange={(e) =>
              setField("client", e.target.value || undefined)
            }
            placeholder="Client (optional; blank = realm role)"
            title="Leave blank for a realm role. Set to a clientId (e.g. registry-staff-portal) to resolve a client role."
          />
        </div>
      );
    case "group":
      return (
        <input
          value={String(rule.rule_value.group ?? "")}
          onChange={(e) => setField("group", e.target.value)}
          placeholder="Keycloak group path, e.g. /states/d1/officers"
        />
      );
    case "http":
      return (
        <input
          value={String(rule.rule_value.url ?? "")}
          onChange={(e) => setField("url", e.target.value)}
          placeholder="https://caller/resolve-approvers"
        />
      );
    case "expression":
      return (
        <textarea
          rows={3}
          value={JSON.stringify(rule.rule_value.logic ?? {}, null, 2)}
          onChange={(e) => {
            try {
              setField("logic", JSON.parse(e.target.value));
            } catch {
              // Let the invalid JSON sit in the field; caller sees it as red
              // on save via server-side validation.
            }
          }}
          placeholder='{"var": "district_head"}'
          style={{ fontFamily: "monospace", fontSize: 12 }}
        />
      );
  }
}
