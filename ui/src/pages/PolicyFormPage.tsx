import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type Policy,
  type PolicyCreate,
  type RuleCreate,
  type StageCreate,
} from "../api/client";
import StageEditor from "../components/StageEditor";
import "./PolicyFormPage.css";

function blankStage(order: number): StageCreate {
  return {
    name: "",
    stage_order: order,
    mode: "all",
    mode_value: null,
    sla_hours: null,
    on_empty: "block",
    rules: [],
  };
}

function policyToStages(p: Policy): StageCreate[] {
  return p.stages.map((s) => ({
    name: s.name,
    stage_order: s.stage_order,
    mode: s.mode,
    mode_value: s.mode_value ?? null,
    sla_hours: s.sla_hours ?? null,
    on_empty: s.on_empty ?? "block",
    parallel_group: s.parallel_group ?? null,
    on_breach: s.on_breach ?? null,
    skip_if: s.skip_if ?? null,
    escalation_rules: (s.escalation_rules ?? []).map(
      (r): RuleCreate => ({
        rule_type: r.rule_type as RuleCreate["rule_type"],
        rule_value: r.rule_value,
        kind: r.kind ?? "approver",
        required: !!r.required,
      })
    ),
    rules: s.rules.map(
      (r): RuleCreate => ({
        rule_type: r.rule_type as RuleCreate["rule_type"],
        rule_value: r.rule_value,
        kind: r.kind ?? "approver",
        required: !!r.required,
      })
    ),
  }));
}

interface Props {
  mode: "create" | "add-version" | "edit-draft";
}

export default function PolicyFormPage({ mode }: Props) {
  const navigate = useNavigate();
  const { policyKey: existingKey, version: versionParam } = useParams();
  const editingVersion = versionParam ? Number(versionParam) : undefined;

  const [policyKey, setPolicyKey] = useState(existingKey ?? "");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [artifactType, setArtifactType] = useState("");
  const [forbidSelf, setForbidSelf] = useState(false);
  const [forbidRepeat, setForbidRepeat] = useState(false);
  const [stages, setStages] = useState<StageCreate[]>([blankStage(1)]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(mode !== "create");

  // Prefill: edit-draft loads exactly the version being edited; add-version
  // seeds from the newest existing version so the author can tweak-and-save.
  useEffect(() => {
    if (mode === "create" || !existingKey) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        let seed: Policy;
        if (mode === "edit-draft" && editingVersion) {
          seed = await api.getVersion(existingKey, editingVersion);
        } else {
          const versions = await api.getVersions(existingKey);
          if (versions.length === 0) {
            setLoading(false);
            return;
          }
          seed = await api.getVersion(existingKey, versions[0].version);
        }
        if (cancelled) return;
        setName(seed.name);
        setDescription(seed.description ?? "");
        setArtifactType(seed.artifact_type);
        setForbidSelf(!!seed.forbid_self_approval);
        setForbidRepeat(!!seed.forbid_repeat_approvers);
        setStages(policyToStages(seed));
      } catch (e) {
        if (!cancelled) {
          setError(
            e instanceof ApiError
              ? `Could not load policy (${e.status})`
              : String(e)
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mode, existingKey, editingVersion]);

  function updateStage(idx: number, next: StageCreate) {
    setStages((prev) => prev.map((s, i) => (i === idx ? next : s)));
  }

  function addStage() {
    setStages((prev) => [...prev, blankStage(prev.length + 1)]);
  }

  function removeStage(idx: number) {
    setStages((prev) =>
      prev
        .filter((_, i) => i !== idx)
        .map((s, i) => ({ ...s, stage_order: i + 1 }))
    );
  }

  function moveStage(idx: number, dir: -1 | 1) {
    const target = idx + dir;
    if (target < 0 || target >= stages.length) return;
    setStages((prev) => {
      const next = [...prev];
      [next[idx], next[target]] = [next[target], next[idx]];
      return next.map((s, i) => ({ ...s, stage_order: i + 1 }));
    });
  }

  async function save() {
    setError(null);

    if (!policyKey.trim() || !name.trim() || !artifactType.trim()) {
      setError("policy_key, name, and artifact_type are required");
      return;
    }
    if (stages.some((s) => !s.name.trim())) {
      setError("Every stage needs a name");
      return;
    }
    if (
      stages.some(
        (s) =>
          (s.mode === "any-n" ||
            s.mode === "quorum" ||
            s.mode === "percentage") &&
          !s.mode_value
      )
    ) {
      setError("any-n / quorum / percentage modes need a mode_value");
      return;
    }
    if (stages.some((s) => s.rules.length === 0)) {
      setError("Every stage needs at least one approver rule");
      return;
    }

    const payload: PolicyCreate = {
      policy_key: policyKey.trim(),
      name: name.trim(),
      description: description.trim() || undefined,
      artifact_type: artifactType.trim(),
      forbid_self_approval: forbidSelf,
      forbid_repeat_approvers: forbidRepeat,
      stages,
    };

    setSaving(true);
    try {
      if (mode === "edit-draft" && editingVersion) {
        await api.updateDraft(policyKey, editingVersion, payload);
      } else if (mode === "add-version") {
        await api.addVersion(policyKey, payload);
      } else {
        await api.createPolicy(payload);
      }
      navigate(`/policies/${encodeURIComponent(payload.policy_key)}`);
    } catch (e) {
      if (e instanceof ApiError) {
        setError(`API error ${e.status}: ${e.message}`);
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setSaving(false);
    }
  }

  const headerLabel = {
    create: "New policy",
    "add-version": `New version of ${existingKey}`,
    "edit-draft": `Edit ${existingKey} v${editingVersion}`,
  }[mode];

  const subtitle = {
    create:
      "Creates version 1 as a draft. Activate it once stages and approvers are ready.",
    "add-version":
      "Creates a new draft version, pre-filled from the latest existing version. Activating it later will archive the currently-active version.",
    "edit-draft":
      "Edits this draft in place. Only drafts are editable — activated versions are immutable; use “New draft version” to propose changes to an active policy.",
  }[mode];

  const saveLabel = {
    create: "Create policy",
    "add-version": "Create draft version",
    "edit-draft": "Save changes",
  }[mode];

  if (loading) {
    return <p>Loading policy…</p>;
  }

  return (
    <>
      <h1>{headerLabel}</h1>
      <p style={{ color: "var(--color-text-muted)" }}>{subtitle}</p>

      <div className="card">
        <h2>Metadata</h2>
        <div className="form-row">
          <label>
            Policy key
            <input
              value={policyKey}
              onChange={(e) => setPolicyKey(e.target.value)}
              placeholder="registry.change_request.v1"
              disabled={mode !== "create"}
            />
          </label>
          <label>
            Name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Registry CR approval"
            />
          </label>
        </div>
        <div className="form-row">
          <label>
            Artifact type
            <input
              value={artifactType}
              onChange={(e) => setArtifactType(e.target.value)}
              placeholder="registry.change_request"
            />
          </label>
        </div>
        <label>
          Description
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            placeholder="Optional — what does this policy govern?"
          />
        </label>
        <div className="form-row checkbox-row" style={{ marginTop: 8 }}>
          <label
            className="checkbox-label"
            title="The user who initiated the request is filtered out of every stage's approver list."
          >
            <input
              type="checkbox"
              checked={forbidSelf}
              onChange={(e) => setForbidSelf(e.target.checked)}
            />
            Forbid self-approval
          </label>
          <label
            className="checkbox-label"
            title="Anyone who approved an earlier stage is filtered out of later stages."
          >
            <input
              type="checkbox"
              checked={forbidRepeat}
              onChange={(e) => setForbidRepeat(e.target.checked)}
            />
            Forbid repeat approvers across stages
          </label>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h2>Stages</h2>
          <button className="btn-secondary" onClick={addStage}>
            + Add stage
          </button>
        </div>
        {stages.map((stage, idx) => (
          <StageEditor
            key={idx}
            stage={stage}
            canMoveUp={idx > 0}
            canMoveDown={idx < stages.length - 1}
            canRemove={stages.length > 1}
            onChange={(next) => updateStage(idx, next)}
            onRemove={() => removeStage(idx)}
            onMoveUp={() => moveStage(idx, -1)}
            onMoveDown={() => moveStage(idx, +1)}
          />
        ))}
      </div>

      {error && (
        <div
          className="card"
          style={{ borderLeft: "4px solid var(--color-danger)" }}
        >
          <strong style={{ color: "var(--color-danger)" }}>Error:</strong>{" "}
          <span>{error}</span>
        </div>
      )}

      <div style={{ display: "flex", gap: 12 }}>
        <button className="btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : saveLabel}
        </button>
        <button className="btn-secondary" onClick={() => navigate(-1)}>
          Cancel
        </button>
      </div>
    </>
  );
}
