set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if [[ -f "${REPO_ROOT}/test.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/test.env"
  set +a
fi

NAMESPACE="${PORTAL_NAMESPACE:-${NAMESPACE:-${SELF_SERVICE_PORTAL_NAMESPACE:-self-service-portal}}}"
PORTAL_DEPLOYMENT_NAME="${PORTAL_DEPLOYMENT_NAME:-redhat-rhaap-portal}"
DRY_RUN="${DRY_RUN:-0}"
AUTODISCOVER_DB="${AUTODISCOVER_DB:-1}"

KUBECMD=()
if command -v oc &>/dev/null; then
  KUBECMD=(oc)
elif command -v kubectl &>/dev/null; then
  KUBECMD=(kubectl)
else
  echo "ERROR: neither oc nor kubectl is on PATH" >&2
  exit 1
fi

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[DRY_RUN] $*"
  else
    "$@"
  fi
}

wants_portal_changes() {
  [[ -n "${PORTAL_REPLICAS:-}" \
    || -n "${PORTAL_CPU_REQUEST:-}" || -n "${PORTAL_MEM_REQUEST:-}" \
    || -n "${PORTAL_CPU_LIMIT:-}"   || -n "${PORTAL_MEM_LIMIT:-}" ]]
}

wants_db_changes() {
  [[ -n "${DB_REPLICAS:-}" \
    || -n "${DB_CPU_REQUEST:-}" || -n "${DB_MEM_REQUEST:-}" \
    || -n "${DB_CPU_LIMIT:-}"   || -n "${DB_MEM_LIMIT:-}" \
    || -n "${DB_NAME:-}" \
    || -n "${DB_MAX_CONNECTIONS:-}" ]]
}

discover_postgres_workload() {
  local first
  first="$("${KUBECMD[@]}" get statefulset -n "$NAMESPACE" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -Ei 'postgres|psql' | head -1 || true)"
  if [[ -n "$first" ]]; then
    echo "$first"
    return 0
  fi
  echo "ERROR: could not auto-detect a postgres StatefulSet in namespace ${NAMESPACE}. Set DB_NAME, or set AUTODISCOVER_DB=0" >&2
  return 1
}

build_resource_args() {
  local pfx="$1" ref="" val="" req_str="" lim_str=""
  ref="${pfx}_CPU_REQUEST"
  val="${!ref:-}"
  [[ -n "$val" ]] && req_str="cpu=$val"
  ref="${pfx}_MEM_REQUEST"
  val="${!ref:-}"
  [[ -n "$val" ]] && req_str="${req_str:+$req_str,}memory=$val"
  ref="${pfx}_CPU_LIMIT"
  val="${!ref:-}"
  [[ -n "$val" ]] && lim_str="cpu=$val"
  ref="${pfx}_MEM_LIMIT"
  val="${!ref:-}"
  [[ -n "$val" ]] && lim_str="${lim_str:+$lim_str,}memory=$val"
  REPLY_REQ="$req_str"
  REPLY_LIM="$lim_str"
}

scale_replicas() {
  local kind="$1" name="$2" replicas_var="$3"
  local n="${!replicas_var:-}"
  [[ -z "$n" ]] && return 0
  if [[ ! "$n" =~ ^[0-9]+$ ]]; then
    echo "ERROR: ${replicas_var} must be a non-negative integer, got: $n" >&2
    exit 1
  fi
  run "${KUBECMD[@]}" -n "$NAMESPACE" scale "$kind" "$name" --replicas="$n"
}

apply_resources() {
  local kind="$1" name="$2" prefix="$3"
  local cflag=() ref cname
  ref="${prefix}_SET_RESOURCES_CONTAINERS"
  cname="${!ref:-}"
  if [[ -n "$cname" ]]; then
    cflag=(-c "$cname")
  fi
  build_resource_args "$prefix"
  local r="$REPLY_REQ" l="$REPLY_LIM" args=()
  if [[ -z "$r" && -z "$l" ]]; then
    return 0
  fi
  [[ -n "$r" ]] && args+=(--requests="$r")
  [[ -n "$l" ]] && args+=(--limits="$l")
  if ((${#cflag[@]})); then
    run "${KUBECMD[@]}" -n "$NAMESPACE" set resources "$kind" "$name" "${cflag[@]}" "${args[@]}"
  else
    run "${KUBECMD[@]}" -n "$NAMESPACE" set resources "$kind" "$name" "${args[@]}"
  fi
}

main() {
  if ! "${KUBECMD[@]}" get namespace "$NAMESPACE" &>/dev/null; then
    echo "ERROR: namespace not found: $NAMESPACE" >&2
    exit 1
  fi

  if ! wants_portal_changes && ! wants_db_changes; then
    echo "Nothing to do: set at least one of PORTAL_REPLICAS, PORTAL_CPU_*, PORTAL_MEM_*, or DB_REPLICAS, DB_CPU_*, DB_MEM_*, DB_NAME, DB_MAX_CONNECTIONS (in test.env or the environment)." >&2
    exit 0
  fi

  echo "Namespace: $NAMESPACE"

  # --- Portal (application) ---
  if wants_portal_changes; then
    echo "App deploy: $PORTAL_DEPLOYMENT_NAME"
    if ! "${KUBECMD[@]}" get deployment "$PORTAL_DEPLOYMENT_NAME" -n "$NAMESPACE" &>/dev/null; then
      echo "ERROR: deployment/${PORTAL_DEPLOYMENT_NAME} not found" >&2
      exit 1
    fi
    if [[ -n "${PORTAL_REPLICAS:-}" ]]; then
      scale_replicas deployment "$PORTAL_DEPLOYMENT_NAME" PORTAL_REPLICAS
    fi
    apply_resources deployment "$PORTAL_DEPLOYMENT_NAME" PORTAL
    if [[ "$DRY_RUN" == "0" ]]; then
      run "${KUBECMD[@]}" -n "$NAMESPACE" rollout status "deployment/${PORTAL_DEPLOYMENT_NAME}" --timeout=5m || true
    fi
  fi

  # --- Database (always StatefulSet) ---
  if wants_db_changes; then
    local db_name=""
    local db_kind="statefulset"
    db_name="${DB_NAME:-}"
    if [[ -z "$db_name" && "$AUTODISCOVER_DB" == "1" ]]; then
      db_name=$(discover_postgres_workload) || exit 1
    elif [[ -n "$db_name" ]]; then
      :
    else
      echo "ERROR: set DB_NAME, or use AUTODISCOVER_DB=1 (default) to auto-detect a postgres StatefulSet in the namespace" >&2
      exit 1
    fi

    echo "DB workload: ${db_kind}/${db_name}"
    if ! "${KUBECMD[@]}" get "$db_kind" "$db_name" -n "$NAMESPACE" &>/dev/null; then
      echo "ERROR: ${db_kind}/${db_name} not found" >&2
      exit 1
    fi

    if [[ -n "${DB_REPLICAS:-}" ]]; then
      scale_replicas "$db_kind" "$db_name" DB_REPLICAS
    fi
    apply_resources "$db_kind" "$db_name" DB

    # Optional: raise PostgreSQL max_connections (image-specific env; default matches RHEL/SCL postgres).
    if [[ -n "${DB_MAX_CONNECTIONS:-}" ]]; then
      if [[ ! "${DB_MAX_CONNECTIONS}" =~ ^[0-9]+$ ]]; then
        echo "ERROR: DB_MAX_CONNECTIONS must be a non-negative integer, got: ${DB_MAX_CONNECTIONS}" >&2
        exit 1
      fi
      local env_name="${DB_MAX_CONNECTIONS_ENV:-POSTGRESQL_MAX_CONNECTIONS}"
      local cflag=()
      if [[ -n "${DB_MAX_CONNECTIONS_CONTAINER:-}" ]]; then
        cflag=(-c "${DB_MAX_CONNECTIONS_CONTAINER}")
      fi
      echo "DB max connections: setting ${env_name}=${DB_MAX_CONNECTIONS} on ${db_kind}/${db_name}"
      run "${KUBECMD[@]}" -n "$NAMESPACE" set env "${db_kind}/${db_name}" "${cflag[@]}" "${env_name}=${DB_MAX_CONNECTIONS}"
      if [[ "$DRY_RUN" == "0" ]]; then
        run "${KUBECMD[@]}" -n "$NAMESPACE" rollout restart "${db_kind}/${db_name}" || true
      fi
    fi

    if [[ "$DRY_RUN" == "0" ]]; then
      run "${KUBECMD[@]}" -n "$NAMESPACE" rollout status "$db_kind/${db_name}" --timeout=5m || true
    fi
  fi

  echo "Done."
}

main "$@"
