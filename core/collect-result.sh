set -o nounset
set -o errexit
set -o pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Always anchor paths at repo root, regardless of current working directory
REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || (cd "${SCRIPT_DIR}/.." && pwd))"
cd "${REPO_ROOT}"

# Default: one directory per run under .artifacts/, named with local date-time and AM/PM.
# Override: set ARTIFACT_DIR to a full path to write results there instead.
if [[ -n "${ARTIFACT_DIR:-}" ]]; then
	ARTIFACT_DIR=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${ARTIFACT_DIR}")
else
	ARTIFACT_STAMP="$(python3 -c 'from datetime import datetime; print(datetime.now().strftime("%Y-%m-%d_%I-%M-%S-%p"))')"
	ARTIFACT_DIR=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${REPO_ROOT}/.artifacts/${ARTIFACT_STAMP}")
fi
mkdir -p "${ARTIFACT_DIR}"
APP_FRAMEWORK_LOG="${ARTIFACT_DIR}/app-framework.log"
: >"${APP_FRAMEWORK_LOG}"

fw_echo() {
	local line
	line="$(date -u -Ins) $*"
	printf '%s\n' "$line" | tee -a "${APP_FRAMEWORK_LOG}"
}

fw_warn() {
	local line
	line="$(date -u -Ins) WARNING: $*"
	printf '%s\n' "$line" | tee -a "${APP_FRAMEWORK_LOG}" >&2
}

{
	echo ""
	echo " === Collecting test results and metrics ==="
	echo ""
} | tee -a "${APP_FRAMEWORK_LOG}"

fw_echo "Writing results to: ${ARTIFACT_DIR}"

export TMP_DIR

TMP_DIR=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${TMP_DIR:-${REPO_ROOT}/.tmp}")
mkdir -p "${TMP_DIR}"

SCENARIO="${SCENARIO:-mvp}"
export SCENARIO

cli="oc"

monitoring_collection_data=$ARTIFACT_DIR/benchmark.json
monitoring_collection_log=$ARTIFACT_DIR/monitoring-collection.log
monitoring_collection_dir=$ARTIFACT_DIR/monitoring-collection-raw-data-dir
mkdir -p "$monitoring_collection_dir"

try_gather_file() {
    if [ -f "$1" ]; then
        cp -vf "$1" "${2:-$ARTIFACT_DIR}"
    else
        fw_warn "Tried to gather $1 but the file was not found!"
    fi
}

try_gather_dir() {
    if [ -d "$1" ]; then
        cp -rvf "$1" "${2:-$ARTIFACT_DIR}"
    else
        fw_warn "Tried to gather $1 but the directory was not found!"
    fi
}

try_gather_file "${TMP_DIR}/benchmark-before"
try_gather_file "${TMP_DIR}/benchmark-after"
try_gather_file "${TMP_DIR}/benchmark-scenario"
try_gather_file "${TMP_DIR}/locust-k8s-operator.values.yaml"
try_gather_file "${TMP_DIR}/locust-test.yaml"
if [[ -f "${TMP_DIR}/test.env" ]]; then
	try_gather_file "${TMP_DIR}/test.env"
fi
# Metrics
PYTHON_VENV_DIR="${REPO_ROOT}/.venv"

fw_echo "Setting up tool to collect monitoring data"
python3 -m venv $PYTHON_VENV_DIR
set +u
# shellcheck disable=SC1090,SC1091
source $PYTHON_VENV_DIR/bin/activate
set -u
python3 -m pip install --quiet -U pip
python3 -m pip install --quiet -e "git+https://github.com/redhat-performance/opl.git#egg=opl-rhcloud-perf-team-core&subdirectory=core"
set +u
deactivate
set -u

fw_echo "Collecting monitoring data"
set +u
# shellcheck disable=SC1090,SC1091
source $PYTHON_VENV_DIR/bin/activate
set -u

timestamp_diff() {
    python3 -c "from datetime import datetime; st=datetime.fromisoformat('$1'.replace(',', '.')); et=datetime.fromisoformat('$2'.replace(',', '.')); print(f'{(et-st).total_seconds():.9f}')"
}

metrics_config_dir="${ARTIFACT_DIR}/metrics-config"
mkdir -p "$metrics_config_dir"

collect_additional_metrics() {
    fw_echo "Collecting metrics from $1"
    status_data.py \
        --status-data-file "$monitoring_collection_data" \
        --additional "$1" \
        --monitoring-start "$mstart" \
        --monitoring-end "$mend" \
        --monitoring-raw-data-dir "$monitoring_collection_dir" \
        --prometheus-host "https://$mhost" \
        --prometheus-port 443 \
        --prometheus-token "$($cli whoami -t)" \
        -d >>"$monitoring_collection_log" 2>&1
}

# Test phase
if [[ ! -f "${ARTIFACT_DIR}/benchmark-before" ]]; then
    echo "ERROR: missing ${ARTIFACT_DIR}/benchmark-before (run test first)" >&2
    exit 1
fi
if [[ ! -f "${ARTIFACT_DIR}/benchmark-after" ]]; then
    echo "ERROR: missing ${ARTIFACT_DIR}/benchmark-after (run test first)" >&2
    exit 1
fi

start_ts="$(cat "${ARTIFACT_DIR}/benchmark-before")"
end_ts="$(cat "${ARTIFACT_DIR}/benchmark-after")"
mstart=$(python3 -c "from datetime import datetime, timezone; ts='$start_ts'.replace(',', '.'); dt=datetime.fromisoformat(ts).astimezone(timezone.utc).replace(microsecond=0); print(dt.isoformat().replace('+00:00','Z'));")
mend=$(python3 -c "from datetime import datetime, timezone; ts='$end_ts'.replace(',', '.'); dt=datetime.fromisoformat(ts).astimezone(timezone.utc).replace(microsecond=0); print(dt.isoformat().replace('+00:00','Z'));")

mhost=$(kubectl -n openshift-monitoring get route -l app.kubernetes.io/name=thanos-query -o json | jq --raw-output '.items[0].spec.host')

# Get scenario version from mvp.py or the specified scenario
if [ -f "test/${SCENARIO}.py" ]; then
    mversion=$(python3 -c "import re, pathlib; p=pathlib.Path('test')/'${SCENARIO}.py'; s=p.read_text(encoding='utf-8') if p.exists() else ''; m=re.search(r'^__version__\s*=\s*\"([^\"]+)\"', s, re.M); print(m.group(1) if m else 'unknown')")
else
    mversion="unknown"
fi

benchmark_started_raw="$(cat "${ARTIFACT_DIR}/benchmark-before")"
benchmark_ended_raw="$(cat "${ARTIFACT_DIR}/benchmark-after")"
benchmark_started="$(python3 -c "from datetime import datetime, timezone; ts='$benchmark_started_raw'.replace(',', '.'); dt=datetime.fromisoformat(ts).astimezone(timezone.utc); print(dt.isoformat());")"
benchmark_ended="$(python3 -c "from datetime import datetime, timezone; ts='$benchmark_ended_raw'.replace(',', '.'); dt=datetime.fromisoformat(ts).astimezone(timezone.utc); print(dt.isoformat());")"
benchmark_duration="$(timestamp_diff "$benchmark_started" "$benchmark_ended")"

fw_echo "Collecting Test phase metrics"
status_data.py \
    --status-data-file "$monitoring_collection_data" \
    --end \
    --set \
    measurements.timings.benchmark.started="$benchmark_started" \
    measurements.timings.benchmark.ended="$benchmark_ended" \
    measurements.timings.benchmark.duration="$benchmark_duration" \
    name="Automation Portal performance test (${SCENARIO})" \
    metadata.scenario.name="$SCENARIO" \
    -d >"$monitoring_collection_log" 2>&1

# # Collect cluster-level metrics if config exists
# if [ -f "config/cluster_read_config.test.yaml" ]; then
#     envsubst <config/cluster_read_config.test.yaml >"${metrics_config_dir}/cluster_read_config.test.yaml"
#     collect_additional_metrics "${metrics_config_dir}/cluster_read_config.test.yaml"
# fi

# Scenario specific metrics
fw_echo "Collecting Scenario specific metrics"
if [ -f "config/prometheus/${SCENARIO}.scenario.yaml" ]; then
    collect_additional_metrics "config/prometheus/${SCENARIO}.scenario.yaml"
else
    fw_echo "Skipping scenario metrics: config/prometheus/${SCENARIO}.scenario.yaml not found"
fi
# Cluster level metrics
if [ -f "config/prometheus/cluster_read_metrics.yaml" ]; then
    collect_additional_metrics "config/prometheus/cluster_read_metrics.yaml"
else
    fw_echo "Skipping cluster level metrics: config/prometheus/cluster_read_metrics.yaml not found"
fi

set +u
deactivate
set -u

# echo "$(date -u -Ins) Collecting error reports"
# # Error report
# find "$ARTIFACT_DIR" -name load-test.log -print0 | sort -V | while IFS= read -r file; do
#     if grep "Error report" "$file" >/dev/null; then
#         tail -n +"$(grep -n "Error report" "$file" | head -n 1 | cut -d ":" -f 1)" "$file"
#     else
#         echo 'No errors found!'
#     fi
# done >"$ARTIFACT_DIR/error-report.txt"

{
	echo ""
	echo " === Results collection complete ==="
	echo ""
} | tee -a "${APP_FRAMEWORK_LOG}"

