import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
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

function useDebouncedValue(value: string, delayMs = 300) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

function KeycloakLoadError({ label }: { label: string }) {
  return (
    <span style={{ fontSize: 11, color: "var(--color-danger, #c00)" }}>
      Could not load {label} from Keycloak
    </span>
  );
}

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

  function patchRuleValue(patch: Record<string, unknown>) {
    onChange({ ...rule, rule_value: { ...rule.rule_value, ...patch } });
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

      <div className="rule-value-col">
        {renderValueEditor(rule, setField, patchRuleValue)}
      </div>

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

function UserSelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (userId: string) => void;
}) {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebouncedValue(search);

  const { data: users, isLoading, isError } = useQuery({
    queryKey: ["keycloak-users", debouncedSearch],
    queryFn: () => api.listKeycloakUsers(debouncedSearch || undefined),
  });

  const options = users ?? [];
  const hasSavedValue =
    !!value && !options.some((u) => u.user_id === value);

  function labelFor(user: (typeof options)[number]) {
    const parts = [user.user_id];
    if (user.name) parts.push(user.name);
    if (user.email) parts.push(user.email);
    return parts.join(" — ");
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
      <input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Search Keycloak users…"
        style={{ fontSize: 12 }}
      />
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={isLoading}
        style={{ flex: 1 }}
      >
        <option value="">
          {isLoading ? "Loading users…" : "— Select user —"}
        </option>
        {hasSavedValue && (
          <option value={value}>{value} (saved)</option>
        )}
        {options.map((u) => (
          <option key={u.user_id} value={u.user_id}>
            {labelFor(u)}
          </option>
        ))}
      </select>
      {isError && <KeycloakLoadError label="users" />}
    </div>
  );
}

function RoleSelect({
  role,
  client,
  onChange,
}: {
  role: string;
  client: string;
  onChange: (patch: Record<string, unknown>) => void;
}) {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebouncedValue(search);

  const { data: clients, isLoading: clientsLoading, isError: clientsError } =
    useQuery({
      queryKey: ["keycloak-clients"],
      queryFn: () => api.listKeycloakClients(),
    });

  const {
    data: roles,
    isLoading: rolesLoading,
    isError: rolesError,
  } = useQuery({
    queryKey: ["keycloak-roles", client || "realm", debouncedSearch],
    queryFn: () =>
      api.listKeycloakRoles(client || undefined, debouncedSearch || undefined),
  });

  const roleOptions = roles ?? [];
  const hasSavedRole =
    !!role && !roleOptions.some((r) => r.name === role);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
      <div style={{ display: "flex", gap: 8 }}>
        <select
          value={client}
          onChange={(e) =>
            onChange({
              client: e.target.value || undefined,
              role: "",
            })
          }
          disabled={clientsLoading}
          style={{ flex: 1 }}
          title="Leave as realm role, or pick a client for client roles."
        >
          <option value="">
            {clientsLoading ? "Loading…" : "Realm role"}
          </option>
          {(clients ?? []).map((c) => (
            <option key={c.client_id} value={c.client_id}>
              {c.client_id}
              {c.name !== c.client_id ? ` — ${c.name}` : ""}
            </option>
          ))}
        </select>
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search roles…"
          style={{ flex: 1, fontSize: 12 }}
        />
      </div>
      <select
        value={role}
        onChange={(e) => onChange({ role: e.target.value })}
        disabled={rolesLoading}
        style={{ flex: 1 }}
      >
        <option value="">
          {rolesLoading ? "Loading roles…" : "— Select role —"}
        </option>
        {hasSavedRole && (
          <option value={role}>{role} (saved)</option>
        )}
        {roleOptions.map((r) => (
          <option key={r.name} value={r.name}>
            {r.description ? `${r.name} — ${r.description}` : r.name}
          </option>
        ))}
      </select>
      {clientsError && <KeycloakLoadError label="clients" />}
      {rolesError && <KeycloakLoadError label="roles" />}
    </div>
  );
}

function GroupSelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (groupPath: string) => void;
}) {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebouncedValue(search);

  const { data: groups, isLoading, isError } = useQuery({
    queryKey: ["keycloak-groups", debouncedSearch],
    queryFn: () => api.listKeycloakGroups(debouncedSearch || undefined),
  });

  const options = groups ?? [];
  const hasSavedValue =
    !!value && !options.some((g) => g.path === value);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
      <input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Search Keycloak groups…"
        style={{ fontSize: 12 }}
      />
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={isLoading}
        style={{ flex: 1 }}
      >
        <option value="">
          {isLoading ? "Loading groups…" : "— Select group —"}
        </option>
        {hasSavedValue && (
          <option value={value}>{value} (saved)</option>
        )}
        {options.map((g) => (
          <option key={g.path} value={g.path}>
            {g.path}
            {g.name && g.name !== g.path ? ` — ${g.name}` : ""}
          </option>
        ))}
      </select>
      {isError && <KeycloakLoadError label="groups" />}
    </div>
  );
}

function renderValueEditor(
  rule: RuleCreate,
  setField: (k: string, v: unknown) => void,
  patchRuleValue: (patch: Record<string, unknown>) => void
) {
  switch (rule.rule_type) {
    case "user":
      return (
        <UserSelect
          value={String(rule.rule_value.user_id ?? "")}
          onChange={(userId) => setField("user_id", userId)}
        />
      );
    case "role":
      return (
        <RoleSelect
          role={String(rule.rule_value.role ?? "")}
          client={String(rule.rule_value.client ?? "")}
          onChange={patchRuleValue}
        />
      );
    case "group":
      return (
        <GroupSelect
          value={String(rule.rule_value.group ?? "")}
          onChange={(groupPath) => setField("group", groupPath)}
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
