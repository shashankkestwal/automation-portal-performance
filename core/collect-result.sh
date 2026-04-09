set -o nounset
set -o errexit
set -o pipefail

echo -e "\n === Collecting test results and metrics ===\n"

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Always anchor paths at repo root, regardless of current working directory
REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || (cd "${SCRIPT_DIR}/.." && pwd))"
cd "${REPO_ROOT}"

ARTIFACT_DIR=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${ARTIFACT_DIR:-${REPO_ROOT}/.artifacts}")
mkdir -p "${ARTIFACT_DIR}"

export TMP_DIR

TMP_DIR=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${TMP_DIR:-${REPO_ROOT}/.tmp}")
mkdir -p "${TMP_DIR}"

export LOCUST_NAMESPACE

LOCUST_NAMESPACE="${LOCUST_NAMESPACE:-locust-operator}"
SCENARIO="${SCENARIO:-mvp}"

cli="oc"

# Logs
gather_pod_logs() {
    log_dir=$1
    pods=$2
    namespace=$3
    mkdir -p "$log_dir"
    echo -e "\nCollecting logs from pods in '$namespace' namespace:"
    for pod in $pods; do
        echo "$pod"
        containers=$($cli -n "$namespace" get pod "$pod" -o json | jq -r '.spec.containers[].name')
        if $cli -n "$namespace" get pod "$pod" -o json | jq -e '.spec.initContainers? // empty' >/dev/null; then
            init_containers=$($cli -n "$namespace" get pod "$pod" -o json | jq -r '.spec.initContainers[].name // empty')
        else
            init_containers=""
        fi
        all_containers="$containers $init_containers"
        for container in $all_containers; do
            logfile_prefix="$log_dir/${pod##*/}.$container"
            echo -e " -> $logfile_prefix.log"
            $cli -n "$namespace" logs "$pod" -c "$container" --tail=-1 >&"$logfile_prefix.log" || true
            echo -e " -> $logfile_prefix.previous.log"
            $cli -n "$namespace" logs "$pod" -c "$container" --tail=-1 --previous=true >&"$logfile_prefix.previous.log" || true
        done
    done
}

# Collect Locust pod logs
pods="$(oc -n "$LOCUST_NAMESPACE" get pods -o json | jq -r '.items[] | select(.metadata.name | contains("locust-operator")).metadata.name')"
pods="$pods $(oc -n "$LOCUST_NAMESPACE" get pods -o json | jq -r '.items[] | select(.metadata.name | contains("test-worker")).metadata.name')"
pods="$pods $(oc -n "$LOCUST_NAMESPACE" get pods -o json | jq -r '.items[] | select(.metadata.name | contains("test-master")).metadata.name')"
# gather_pod_logs "${ARTIFACT_DIR}/locust-logs" "$pods" "$LOCUST_NAMESPACE"

monitoring_collection_data=$ARTIFACT_DIR/benchmark.json
monitoring_collection_log=$ARTIFACT_DIR/monitoring-collection.log
monitoring_collection_dir=$ARTIFACT_DIR/monitoring-collection-raw-data-dir
mkdir -p "$monitoring_collection_dir"

try_gather_file() {
    if [ -f "$1" ]; then
        cp -vf "$1" "${2:-$ARTIFACT_DIR}"
    else
        echo "WARNING: Tried to gather $1 but the file was not found!"
    fi
}

try_gather_dir() {
    if [ -d "$1" ]; then
        cp -rvf "$1" "${2:-$ARTIFACT_DIR}"
    else
        echo "WARNING: Tried to gather $1 but the directory was not found!"
    fi
}

try_gather_file "${TMP_DIR}/benchmark-before"
try_gather_file "${TMP_DIR}/benchmark-after"
try_gather_file "${TMP_DIR}/benchmark-scenario"
try_gather_file "${TMP_DIR}/locust-k8s-operator.values.yaml"
try_gather_file "${TMP_DIR}/locust-test.yaml"
try_gather_file load-test.log

# Metrics
PYTHON_VENV_DIR="${REPO_ROOT}/.venv"

echo "$(date -u -Ins) Setting up tool to collect monitoring data"
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

echo "$(date -u -Ins) Collecting monitoring data"
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
    echo "$(date -u -Ins) Collecting metrics from $1"
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

echo "$(date -u -Ins) Collecting Test phase metrics"
status_data.py \
    --status-data-file "$monitoring_collection_data" \
    --end \
    --set \
    measurements.timings.benchmark.started="$benchmark_started" \
    measurements.timings.benchmark.ended="$benchmark_ended" \
    measurements.timings.benchmark.duration="$benchmark_duration" \
    name="Automation Portal performance test ( ${SCENARIO} )" \
    metadata.scenario.name="$SCENARIO" \
    -d >"$monitoring_collection_log" 2>&1

# Collect cluster-level metrics if config exists
if [ -f "config/cluster_read_config.test.yaml" ]; then
    envsubst <config/cluster_read_config.test.yaml >"${metrics_config_dir}/cluster_read_config.test.yaml"
    collect_additional_metrics "${metrics_config_dir}/cluster_read_config.test.yaml"
fi

# Scenario specific metrics
echo "$(date -u -Ins) Collecting Scenario specific metrics"
if [ -f "test/${SCENARIO}.metrics.yaml" ]; then
    envsubst <"test/${SCENARIO}.metrics.yaml" >"${metrics_config_dir}/${SCENARIO}.metrics.yaml"
    collect_additional_metrics "${metrics_config_dir}/${SCENARIO}.metrics.yaml"
fi

set +u
deactivate
set -u

echo "$(date -u -Ins) Collecting error reports"
# Error report
find "$ARTIFACT_DIR" -name load-test.log -print0 | sort -V | while IFS= read -r file; do
    if grep "Error report" "$file" >/dev/null; then
        tail -n +"$(grep -n "Error report" "$file" | head -n 1 | cut -d ":" -f 1)" "$file"
    else
        echo 'No errors found!'
    fi
done >"$ARTIFACT_DIR/error-report.txt"

echo -e "\n === Results collection complete ===\n"

