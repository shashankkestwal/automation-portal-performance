#!/usr/bin/env bash
# Run the active SCENARIO with local Locust (headless). Same URL/password rules as `make test`.
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

SCENARIO="${SCENARIO:-mvp}"
USERS="${USERS:-10}"
SPAWN_RATE="${SPAWN_RATE:-2}"
DURATION="${DURATION:-10s}"
PORTAL_NAMESPACE="${PORTAL_NAMESPACE:-self-service-portal}"
AAP_NAMESPACE="${AAP_NAMESPACE:-ansible-automation-platform}"
PORTAL_ROUTE="${PORTAL_ROUTE:-sap}"
AAP_ROUTE="${AAP_ROUTE:-aap}"
AAP_ADMIN_SECRET="${AAP_ADMIN_SECRET:-${AAP_ROUTE}-admin-password}"
STATUS_CHECK_DELAY_SECONDS="${STATUS_CHECK_DELAY_SECONDS:-10}"
USE_SCM="${USE_SCM:-false}"
LOCUST_LOGLEVEL="${LOCUST_LOGLEVEL:-DEBUG}"
TMP_DIR="${TMP_DIR:-${REPO_ROOT}/.tmp}"

mkdir -p "${TMP_DIR}"

if ! command -v locust &>/dev/null; then
  echo "ERROR: locust not found. Install deps: pip install -r requirements.txt" >&2
  exit 1
fi

KUBECMD=()
if command -v oc &>/dev/null; then
  KUBECMD=(oc)
elif command -v kubectl &>/dev/null; then
  KUBECMD=(kubectl)
else
  echo "ERROR: neither oc nor kubectl is on PATH" >&2
  exit 1
fi

PORTAL_URL="https://$("${KUBECMD[@]}" -n "${PORTAL_NAMESPACE}" get route "${PORTAL_ROUTE}" -o jsonpath='{.spec.host}')"
AAP_URL="https://$("${KUBECMD[@]}" -n "${AAP_NAMESPACE}" get route "${AAP_ROUTE}" -o jsonpath='{.spec.host}')"

LOCUST_EXTRA=()
if [[ "${SCENARIO}" == "ee-builder" ]]; then
  AAP_PASSWORD="${AAP_PASSWORD:-redhat123}"
  if [[ -z "${AAP_PASSWORD}" ]]; then
    echo "ERROR: set AAP_PASSWORD in test.env (password for user-001..user-N)" >&2
    exit 1
  fi
  LOCUST_EXTRA+=(--status-check-delay-seconds "${STATUS_CHECK_DELAY_SECONDS}")
  case "${USE_SCM}" in
    true|True|TRUE|1|yes|Yes|YES)
      LOCUST_EXTRA+=(--use-scm)
      if [[ -z "${GITHUB_USER_OAUTH_TOKEN:-}" ]]; then
        echo "ERROR: USE_SCM=true requires GITHUB_USER_OAUTH_TOKEN in test.env (SCM publish + GitHub verify)" >&2
        exit 1
      fi
      ;;
  esac
  if [[ -n "${AAP_ACCESS_TOKEN:-}" ]]; then
    LOCUST_EXTRA+=(--aap-access-token "${AAP_ACCESS_TOKEN}")
  fi
  if [[ -n "${GITHUB_USER_OAUTH_TOKEN:-}" ]]; then
    LOCUST_EXTRA+=(--github-user-oauth-token "${GITHUB_USER_OAUTH_TOKEN}")
  fi
else
  AAP_PASSWORD="$("${KUBECMD[@]}" -n "${AAP_NAMESPACE}" get secret "${AAP_ADMIN_SECRET}" \
    -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)"
  if [[ -z "${AAP_PASSWORD}" ]]; then
    echo "ERROR: empty admin password (secret ${AAP_ADMIN_SECRET} in ${AAP_NAMESPACE})" >&2
    exit 1
  fi
fi

LOCUSTFILE="${REPO_ROOT}/test/${SCENARIO}.py"
if [[ ! -f "${LOCUSTFILE}" ]]; then
  echo "ERROR: scenario file not found: ${LOCUSTFILE}" >&2
  exit 1
fi

echo "Scenario:   ${SCENARIO}"
echo "Portal URL: ${PORTAL_URL}"
echo "AAP URL:    ${AAP_URL}"
echo "Users:      ${USERS}  spawn-rate: ${SPAWN_RATE}  duration: ${DURATION}"
if [[ "${SCENARIO}" == "ee-builder" ]]; then
  echo "USE_SCM:    ${USE_SCM}  (Locust extras: ${LOCUST_EXTRA[*]:-none})"
fi
echo "Running:    locust -f test/${SCENARIO}.py --headless ..."

date -u -Ins >"${TMP_DIR}/benchmark-before"

locust -f "${LOCUSTFILE}" \
  --host "${PORTAL_URL}" \
  --users "${USERS}" \
  --spawn-rate "${SPAWN_RATE}" \
  --run-time "${DURATION}" \
  --headless \
  --loglevel "${LOCUST_LOGLEVEL}" \
  --aap-url "${AAP_URL}" \
  --aap-password "${AAP_PASSWORD}" \
  "${LOCUST_EXTRA[@]}" \
  2>&1 | tee "${TMP_DIR}/load-test.log"

date -u -Ins >"${TMP_DIR}/benchmark-after"
echo "Test completed at $(date -u -Ins)"
echo "Logs: ${TMP_DIR}/load-test.log"
