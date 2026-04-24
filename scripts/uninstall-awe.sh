#!/usr/bin/env bash
#
# uninstall-awe.sh
# ----------------
# Cleanly uninstall an OpenG2P Approval Workflow Engine Helm release and
# every resource it touched — including the PostgreSQL database + role
# that live inside the commons-postgresql instance (not owned by AWE's
# Helm release, so `helm uninstall` leaves them), and the K8s Secrets
# that keycloak-init creates with `helm.sh/resource-policy: keep` for
# the `awe-admin-portal` and `awe-admin-resolver` clients.
#
# What it does, in order:
#   1. helm uninstall <release>        (AWE backend + UI workloads, Services,
#                                        Istio VS, helm-owned secrets/configmaps)
#   2. Delete leftover Jobs + pods     (keycloak-init + postgres-init pin
#                                        themselves with hook-delete-policy:
#                                        before-hook-creation)
#   3. Delete keycloak-init Secrets    (`awe-admin-portal`, `awe-admin-resolver`
#                                        — annotated `resource-policy: keep`)
#   4. Sweep any other leftover        (labels: app.kubernetes.io/instance)
#      Secrets / ConfigMaps
#   5. Drop Postgres DB + role         (via `kubectl exec` into commons-postgresql)
#   6. Delete PVCs by label            (AWE has none today; kept for parity
#                                        with Registry and future-proofing)
#   7. Delete PVs still Released       (typically reclaimPolicy=Retain volumes)
#   8. Optional: delete Keycloak       (only with --delete-kc-clients — calls
#      clients                          kcadm.sh inside the commons-keycloak pod)
#
# Requires: kubectl (cluster admin), helm, bash 4+, jq.
#
# USAGE:
#   ./uninstall-awe.sh \
#       --namespace <ns> \
#       [--release <name>]             (default: awe)
#       [--postgres-release <name>]    (default: commons-postgresql)
#       [--postgres-namespace <ns>]    (default: same as --namespace)
#       [--keycloak-release <name>]    (default: commons-keycloak)
#       [--keycloak-namespace <ns>]    (default: same as --namespace)
#       [--keycloak-realm <name>]      (default: staff)
#       [--delete-kc-clients]          (also drop the two Keycloak clients)
#       [--keep-kc-secrets]            (leave awe-admin-portal / resolver Secrets)
#       [--keep-pvs]                   (delete PVCs but not PVs)
#       [--dry-run]                    (print actions, change nothing)
#       [--yes]                        (skip interactive confirmation)
#
# EXAMPLES:
#   # Dry run first — no changes made:
#   ./uninstall-awe.sh --namespace awe --dry-run
#
#   # For real, with confirmation prompt:
#   ./uninstall-awe.sh --namespace awe
#
#   # Full blast including Keycloak clients (non-interactive, CI):
#   ./uninstall-awe.sh --namespace awe --delete-kc-clients --yes

set -euo pipefail

# ---------- defaults ----------
RELEASE="awe"
NAMESPACE=""
POSTGRES_RELEASE="commons-postgresql"
POSTGRES_NAMESPACE=""
KEYCLOAK_RELEASE="commons-keycloak"
KEYCLOAK_NAMESPACE=""
KEYCLOAK_REALM="staff"
DELETE_KC_CLIENTS=false
KEEP_KC_SECRETS=false
KEEP_PVS=false
DRY_RUN=false
ASSUME_YES=false

# ---------- cli ----------
usage() { sed -n '2,50p' "$0"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release)              RELEASE="$2";              shift 2 ;;
    --namespace|-n)         NAMESPACE="$2";            shift 2 ;;
    --postgres-release)     POSTGRES_RELEASE="$2";     shift 2 ;;
    --postgres-namespace)   POSTGRES_NAMESPACE="$2";   shift 2 ;;
    --keycloak-release)     KEYCLOAK_RELEASE="$2";     shift 2 ;;
    --keycloak-namespace)   KEYCLOAK_NAMESPACE="$2";   shift 2 ;;
    --keycloak-realm)       KEYCLOAK_REALM="$2";       shift 2 ;;
    --delete-kc-clients)    DELETE_KC_CLIENTS=true;    shift ;;
    --keep-kc-secrets)      KEEP_KC_SECRETS=true;      shift ;;
    --keep-pvs)             KEEP_PVS=true;             shift ;;
    --dry-run)              DRY_RUN=true;              shift ;;
    --yes|-y)               ASSUME_YES=true;           shift ;;
    -h|--help)              usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done

[[ -z "$NAMESPACE" ]] && { echo "ERROR: --namespace is required"; exit 1; }
[[ -z "$POSTGRES_NAMESPACE" ]] && POSTGRES_NAMESPACE="$NAMESPACE"
[[ -z "$KEYCLOAK_NAMESPACE" ]] && KEYCLOAK_NAMESPACE="$NAMESPACE"

# ---------- derived: DB / user names (templated exactly like values.yaml) ----------
# values.yaml:
#   aweDB:     '{{ printf "%s" .Release.Name | replace "-" "_" }}'
#   aweDBUser: '{{ printf "%s_user" .Release.Name | replace "-" "_" }}'
RELEASE_UNDERSCORED="${RELEASE//-/_}"
AWE_DB="${RELEASE_UNDERSCORED}"
AWE_USER="${RELEASE_UNDERSCORED}_user"

# Keycloak client IDs provisioned by AWE's keycloak-init block.
KC_PORTAL_CLIENT="awe-admin-portal"
KC_RESOLVER_CLIENT="awe-admin-resolver"

# ---------- helpers ----------
_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
_green() { printf "\033[32m%s\033[0m\n" "$*"; }
_yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
_blue()  { printf "\033[34m%s\033[0m\n" "$*"; }

run() {
  # Print + execute, or just print if --dry-run. Never aborts on non-zero
  # exit — cleanup must be idempotent. Already-gone resources just produce
  # a notice and we move on.
  echo "  \$ $*"
  if [[ "$DRY_RUN" == false ]]; then
    eval "$@" || _yellow "  (command returned non-zero — continuing)"
  fi
}

kexec_psql() {
  # Run SQL as postgres superuser inside the commons-postgresql pod.
  local sql="$1"
  local cmd=(kubectl exec -n "$POSTGRES_NAMESPACE" "$PG_POD" -c postgresql -- \
             bash -c "PGPASSWORD=\"\$POSTGRES_PASSWORD\" psql -U postgres -v ON_ERROR_STOP=0 -c \"$sql\"")
  echo "  \$ psql -U postgres -c \"$sql\""
  if [[ "$DRY_RUN" == false ]]; then
    "${cmd[@]}" || _yellow "  (psql returned non-zero — continuing)"
  fi
}

kcadm() {
  # Run a kcadm.sh command inside the commons-keycloak pod, already
  # authenticated. Uses the admin password from the pod env.
  local args=("$@")
  local cmd=(kubectl exec -n "$KEYCLOAK_NAMESPACE" "$KC_POD" -- \
             bash -c "/opt/keycloak/bin/kcadm.sh config credentials \
                        --server http://localhost:8080 \
                        --realm master \
                        --user admin \
                        --password \"\$KEYCLOAK_ADMIN_PASSWORD\" >/dev/null && \
                      /opt/keycloak/bin/kcadm.sh $(printf '%q ' "${args[@]}")")
  echo "  \$ kcadm.sh ${args[*]}"
  if [[ "$DRY_RUN" == false ]]; then
    "${cmd[@]}" || _yellow "  (kcadm returned non-zero — continuing)"
  fi
}

# ---------- pre-flight ----------
_blue "==> Pre-flight checks"

command -v kubectl >/dev/null || { _red "kubectl not found"; exit 1; }
command -v helm    >/dev/null || { _red "helm not found";    exit 1; }
command -v jq      >/dev/null || { _red "jq not found";      exit 1; }

if kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
  NAMESPACE_EXISTS=true
  _green "  Namespace '$NAMESPACE' exists"
else
  NAMESPACE_EXISTS=false
  _yellow "  Namespace '$NAMESPACE' does not exist — namespace-scoped cleanup will be skipped"
fi

# Locate commons-postgresql pod. Bitnami's chart uses these labels.
PG_POD=""
if kubectl get ns "$POSTGRES_NAMESPACE" >/dev/null 2>&1; then
  PG_POD=$(kubectl get pod -n "$POSTGRES_NAMESPACE" \
    -l "app.kubernetes.io/instance=$POSTGRES_RELEASE,app.kubernetes.io/name=postgresql" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [[ -z "$PG_POD" ]] && kubectl get pod -n "$POSTGRES_NAMESPACE" "${POSTGRES_RELEASE}-0" >/dev/null 2>&1; then
    PG_POD="${POSTGRES_RELEASE}-0"
  fi
fi

if [[ -z "$PG_POD" ]]; then
  PG_POD_FOUND=false
  _yellow "  commons-postgresql pod not found — DB / role drop step will be skipped"
else
  PG_POD_FOUND=true
  _green "  Found Postgres pod: $PG_POD (namespace: $POSTGRES_NAMESPACE)"
fi

# Locate commons-keycloak pod (only needed if --delete-kc-clients).
KC_POD=""
if [[ "$DELETE_KC_CLIENTS" == true ]] && kubectl get ns "$KEYCLOAK_NAMESPACE" >/dev/null 2>&1; then
  KC_POD=$(kubectl get pod -n "$KEYCLOAK_NAMESPACE" \
    -l "app.kubernetes.io/instance=$KEYCLOAK_RELEASE,app.kubernetes.io/name=keycloak" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [[ -z "$KC_POD" ]] && kubectl get pod -n "$KEYCLOAK_NAMESPACE" "${KEYCLOAK_RELEASE}-0" >/dev/null 2>&1; then
    KC_POD="${KEYCLOAK_RELEASE}-0"
  fi
fi

if [[ "$DELETE_KC_CLIENTS" == true ]]; then
  if [[ -z "$KC_POD" ]]; then
    KC_POD_FOUND=false
    _yellow "  commons-keycloak pod not found — Keycloak client delete step will be skipped"
  else
    KC_POD_FOUND=true
    _green "  Found Keycloak pod: $KC_POD (namespace: $KEYCLOAK_NAMESPACE)"
  fi
fi

if helm -n "$NAMESPACE" status "$RELEASE" >/dev/null 2>&1; then
  _green "  Helm release '$RELEASE' found in namespace '$NAMESPACE'"
  HELM_RELEASE_EXISTS=true
else
  _yellow "  Helm release '$RELEASE' not found — will skip helm uninstall step"
  HELM_RELEASE_EXISTS=false
fi

# ---------- blast radius ----------
_blue "==> Resources to be deleted"

echo
echo "Helm release:       $RELEASE (namespace: $NAMESPACE)"
echo "Postgres database:  $AWE_DB"
echo "Postgres role:      $AWE_USER"
echo "Postgres pod:       ${PG_POD:-<not found — will skip DB drop>} ($POSTGRES_NAMESPACE)"
if [[ "$KEEP_KC_SECRETS" == false ]]; then
  echo "Keycloak secrets:   $KC_PORTAL_CLIENT, $KC_RESOLVER_CLIENT (namespace: $NAMESPACE)"
fi
if [[ "$DELETE_KC_CLIENTS" == true ]]; then
  echo "Keycloak clients:   $KC_PORTAL_CLIENT, $KC_RESOLVER_CLIENT (realm: $KEYCLOAK_REALM)"
fi
echo

if [[ "$NAMESPACE_EXISTS" == true ]]; then
  echo "Jobs (label app.kubernetes.io/instance=$RELEASE):"
  kubectl -n "$NAMESPACE" get job -l "app.kubernetes.io/instance=$RELEASE" \
    --no-headers 2>/dev/null | awk '{print "  - " $1}' || echo "  (none)"

  echo "Secrets (label app.kubernetes.io/instance=$RELEASE):"
  kubectl -n "$NAMESPACE" get secret -l "app.kubernetes.io/instance=$RELEASE" \
    --no-headers 2>/dev/null | awk '{print "  - " $1}' || echo "  (none)"

  echo "ConfigMaps (label app.kubernetes.io/instance=$RELEASE):"
  kubectl -n "$NAMESPACE" get configmap -l "app.kubernetes.io/instance=$RELEASE" \
    --no-headers 2>/dev/null | awk '{print "  - " $1}' || echo "  (none)"

  echo "PVCs (label app.kubernetes.io/instance=$RELEASE):"
  kubectl -n "$NAMESPACE" get pvc -l "app.kubernetes.io/instance=$RELEASE" \
    --no-headers 2>/dev/null | awk '{print "  - " $1}' || echo "  (none)"
else
  echo "(namespace '$NAMESPACE' does not exist — no namespace-scoped resources to preview)"
fi

if [[ "$KEEP_PVS" == false ]]; then
  echo "PVs (bound to above PVCs / labeled with release):"
  kubectl get pv -o json 2>/dev/null | \
    jq -r --arg ns "$NAMESPACE" --arg rel "$RELEASE" \
      '.items[] | select((.spec.claimRef.namespace==$ns) or (.metadata.labels["app.kubernetes.io/instance"]==$rel)) | "  - " + .metadata.name + " (" + .status.phase + ")"' \
    2>/dev/null | sort -u || true
fi
echo

# ---------- confirmation ----------
if [[ "$DRY_RUN" == true ]]; then
  _yellow "DRY-RUN: no changes will be made."
fi

if [[ "$ASSUME_YES" == false && "$DRY_RUN" == false ]]; then
  _red "This is destructive. Type the release name ('$RELEASE') to confirm:"
  read -r CONFIRM
  if [[ "$CONFIRM" != "$RELEASE" ]]; then
    _red "Confirmation did not match. Aborting."
    exit 1
  fi
fi

# ========== STEP 1: helm uninstall ==========
_blue "==> [1/8] Helm uninstall"
if [[ "$HELM_RELEASE_EXISTS" == true ]]; then
  run "helm uninstall '$RELEASE' -n '$NAMESPACE' --wait --timeout 5m || true"
else
  echo "  (skipped — release not present)"
fi

# ========== STEP 2: leftover Jobs ==========
# keycloak-init + postgres-init hook Jobs pin themselves with
# `hook-delete-policy: before-hook-creation` and are NOT cleaned up by
# `helm uninstall`. Purge them explicitly BEFORE dropping the DB so their
# Pods close Postgres connections cleanly.
_blue "==> [2/8] Delete leftover Jobs and Pods"
if [[ "$NAMESPACE_EXISTS" == true ]]; then
  run "kubectl -n '$NAMESPACE' delete job -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found --wait=true --timeout=2m"
  run "kubectl -n '$NAMESPACE' delete pod -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found --field-selector=status.phase!=Running"
else
  echo "  (skipped — namespace '$NAMESPACE' not present)"
fi

# ========== STEP 3: Keycloak-init client Secrets (resource-policy: keep) ==========
# keycloak-init/templates/client-secrets.yaml annotates these with
# `helm.sh/resource-policy: keep`, so `helm uninstall` leaves them alone.
# A re-install would then find a random 32-char value already in K8s and
# push it back into Keycloak — which is fine for upgrades, unwanted on a
# full teardown. Remove them unless --keep-kc-secrets.
_blue "==> [3/8] Delete keycloak-init client Secrets"
if [[ "$KEEP_KC_SECRETS" == true ]]; then
  _yellow "  (skipped — --keep-kc-secrets)"
elif [[ "$NAMESPACE_EXISTS" == true ]]; then
  run "kubectl -n '$NAMESPACE' delete secret '$KC_PORTAL_CLIENT' --ignore-not-found"
  run "kubectl -n '$NAMESPACE' delete secret '$KC_RESOLVER_CLIENT' --ignore-not-found"
else
  echo "  (skipped — namespace '$NAMESPACE' not present)"
fi

# ========== STEP 4: sweep leftover Secrets & ConfigMaps ==========
_blue "==> [4/8] Sweep leftover Secrets / ConfigMaps"
if [[ "$NAMESPACE_EXISTS" == true ]]; then
  run "kubectl -n '$NAMESPACE' delete secret    -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"
  run "kubectl -n '$NAMESPACE' delete configmap -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"
else
  echo "  (skipped — namespace '$NAMESPACE' not present)"
fi

# ========== STEP 5: drop Postgres DB + role ==========
_blue "==> [5/8] Drop Postgres database and role"
if [[ "$PG_POD_FOUND" == true ]]; then
  echo "  - Database: $AWE_DB"
  kexec_psql "REVOKE CONNECT ON DATABASE \\\"$AWE_DB\\\" FROM PUBLIC;"
  kexec_psql "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$AWE_DB' AND pid <> pg_backend_pid();"
  kexec_psql "DROP DATABASE IF EXISTS \\\"$AWE_DB\\\";"

  echo "  - Role: $AWE_USER"
  # Reassign/drop stray ownership outside the dropped DB.
  kexec_psql "REASSIGN OWNED BY \\\"$AWE_USER\\\" TO postgres;"
  kexec_psql "DROP OWNED BY \\\"$AWE_USER\\\";"
  kexec_psql "DROP ROLE IF EXISTS \\\"$AWE_USER\\\";"
else
  echo "  (skipped — commons-postgresql pod not reachable; if Postgres is already gone, DB is gone too)"
fi

# ========== STEP 6: PVCs ==========
_blue "==> [6/8] Delete PVCs"
if [[ "$NAMESPACE_EXISTS" == true ]]; then
  run "kubectl -n '$NAMESPACE' delete pvc -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"
else
  echo "  (skipped — namespace '$NAMESPACE' not present)"
fi

# ========== STEP 7: PVs ==========
_blue "==> [7/8] Delete PVs"
if [[ "$KEEP_PVS" == true ]]; then
  _yellow "  (skipped — --keep-pvs)"
else
  pv_list=$(kubectl get pv -o json 2>/dev/null | \
    jq -r --arg ns "$NAMESPACE" \
      '.items[] | select(.spec.claimRef.namespace==$ns) | select(.status.phase=="Released" or .status.phase=="Failed") | .metadata.name' \
    2>/dev/null || true)
  pv_labeled=$(kubectl get pv -l "app.kubernetes.io/instance=$RELEASE" \
                 -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
  pv_all=$(echo "$pv_list $pv_labeled" | tr ' ' '\n' | sort -u | tr '\n' ' ' | sed 's/^ *//;s/ *$//')

  if [[ -z "$pv_all" ]]; then
    echo "  (no PVs to delete)"
  else
    for pv in $pv_all; do
      run "kubectl delete pv '$pv' --ignore-not-found"
    done
  fi
fi

# ========== STEP 8: Keycloak clients (optional) ==========
_blue "==> [8/8] Delete Keycloak clients"
if [[ "$DELETE_KC_CLIENTS" == false ]]; then
  _yellow "  (skipped — pass --delete-kc-clients to also remove '$KC_PORTAL_CLIENT' and '$KC_RESOLVER_CLIENT' from realm '$KEYCLOAK_REALM')"
elif [[ "$KC_POD_FOUND" != true ]]; then
  echo "  (skipped — commons-keycloak pod not reachable)"
else
  for cid in "$KC_PORTAL_CLIENT" "$KC_RESOLVER_CLIENT"; do
    echo "  - Client: $cid (realm: $KEYCLOAK_REALM)"
    # Resolve clientId → uuid, then delete. Two kcadm calls; both tolerant of not-found.
    if [[ "$DRY_RUN" == false ]]; then
      UUID=$(kubectl exec -n "$KEYCLOAK_NAMESPACE" "$KC_POD" -- \
        bash -c "/opt/keycloak/bin/kcadm.sh config credentials \
                   --server http://localhost:8080 --realm master \
                   --user admin --password \"\$KEYCLOAK_ADMIN_PASSWORD\" >/dev/null && \
                 /opt/keycloak/bin/kcadm.sh get clients -r '$KEYCLOAK_REALM' \
                   -q clientId='$cid' --fields id --format csv --noquotes 2>/dev/null | head -n1" \
        2>/dev/null || true)
      if [[ -z "$UUID" ]]; then
        _yellow "    (client not found — already deleted or never existed)"
        continue
      fi
      kcadm delete "clients/$UUID" -r "$KEYCLOAK_REALM"
    else
      echo "    (dry-run — would resolve UUID and delete)"
    fi
  done
fi

echo
_green "==> Done."
if [[ "$DRY_RUN" == true ]]; then
  _yellow "    (dry-run — nothing was actually changed)"
fi
