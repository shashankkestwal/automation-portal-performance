# automation-portal-performance

Performance test framework for the **Self-Service Portal** integrated with **Ansible Automation Platform (AAP)**, using **Locust** (via the Locust Kubernetes Operator) and a small set of helper scripts to collect artifacts/metrics.

## What this repo does

- Installs the **Locust Operator** into a cluster namespace.
- Runs **LocustTest** CRs that execute scenarios from `test/<scenario>.py` (default: `test/mvp.py`).
- Optionally scales portal and database workloads before a run (`core/scale_portal.sh`).
- Collects run artifacts (logs, config snapshots, metrics outputs) into a local artifacts directory.

## Prerequisites

### Local tools

- `kubectl` **or** `oc` (OpenShift)
- `helm`
- `python3`
- `envsubst`
- `base64`

### Cluster access

- Access to the cluster where the portal and AAP are deployed
- Permissions to:
  - create resources in the Locust operator namespace (`LOCUST_NAMESPACE`)
  - read Routes and Secrets in the portal and AAP namespaces
  - patch Deployments and StatefulSets in the portal namespace (when using `core/scale_portal.sh`)

## Configuration

All configuration is driven by environment variables. The simplest approach is to copy `test.env.example` to `test.env` in the repo root (gitignored; auto-loaded by the `Makefile` and `core/scale_portal.sh`).

`make test` snapshots your `test.env` to the temp dir (`$(TMP_DIR)/test.env`) so every run is reproducible.

### Shared variables (all scenarios)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SCENARIO` | `mvp` | Locust script: `test/$(SCENARIO).py` |
| `PORTAL_NAMESPACE` | `self-service-portal` | Namespace for portal Route and workloads |
| `AAP_NAMESPACE` | `ansible-automation-platform` | Namespace for AAP Route and admin secret |
| `LOCUST_NAMESPACE` | `locust-operator` | Where LocustTest CRs run |
| `PORTAL_ROUTE` | `sap` | Route name in `PORTAL_NAMESPACE` |
| `AAP_ROUTE` | `aap` | Route name in `AAP_NAMESPACE` |
| `AAP_ADMIN_SECRET` | `$(AAP_ROUTE)-admin-password` | Secret used by **mvp** for admin password |
| `WORKERS` | `5` | Locust worker pod count |
| `USERS` | `10` | Simulated concurrent users |
| `SPAWN_RATE` | `2` | Users spawned per second |
| `DURATION` | `10s` | Test run time |
| `LOCUST_EXTRA_CMD` | `--debug=true` | Extra Locust master args |

URLs (`PORTAL_URL`, `AAP_URL`) are discovered from OpenShift Routes when you run `make test`; you do not set them in `test.env`.

## Scenarios

### MVP (`test/mvp.py`)

Baseline portal journey: OAuth as **admin**, catalog browsing, scaffolder task creation, job template run, and related API calls.

**Concurrency model:** Every Locust user logs in as the same AAP account — `admin`. There is no per-user assignment; all concurrent virtual users share one identity. That models a single power-user driving the portal under load, not many distinct portal users.

**Credentials:** `make test` reads the AAP **admin** password from the cluster secret (`AAP_ADMIN_SECRET` in `AAP_NAMESPACE`). You do not need `AAP_PASSWORD` in `test.env` for mvp.

**Locust CLI (via operator):** `--aap-url`, `--aap-password` only. No ee-builder-specific flags.

Example:

```bash
make test SCENARIO=mvp USERS=25 WORKERS=3 SPAWN_RATE=2 DURATION=10m
```

### EE builder (`test/ee-builder.py`)

Execution-environment builder flow: OAuth, catalog (EE definitions, templates, collections, git repos), scaffolder EE create, and post-create checks (task status, catalog entity, history).

**Concurrency model:** Each Locust user gets a **distinct** AAP account: `user-001`, `user-002`, … up to `user-$(USERS zero-padded to 3 digits)`. Usernames are assigned at test start on the master and distributed to workers; **admin is never used**. All seeded users are expected to share the same password (`AAP_PASSWORD` in `test.env`).

You must provision `user-001` … `user-00N` on AAP before running with `USERS=N`.

**Credentials:** `make test` does **not** read the admin secret for ee-builder. It uses `AAP_PASSWORD` from `test.env` (Makefile default `redhat123` if unset).

**Task flow:**

1. **Non-SCM** EE create always runs (`scaffolder_create` with `use_scm=false`).
2. If `USE_SCM=true`, an additional **SCM** create runs and GitHub repo verification is performed (org `test-rhaap-portal` in the test script).

### MVP vs EE builder (summary)

| | **mvp** | **ee-builder** |
|---|---------|----------------|
| AAP login | `admin` (every user) | `user-001` … `user-N` (one per Locust user) |
| Password source | Cluster secret `AAP_ADMIN_SECRET` | `AAP_PASSWORD` in `test.env` |
| Distinct users under load | No (shared admin) | Yes (unique usernames) |
| Extra `test.env` / Locust flags | None | See below |

## EE builder: extra parameters

Set these in `test.env` when `SCENARIO=ee-builder`. The Makefile maps them into the LocustTest CR (`config/locust-test-template.yaml`).

| Variable | Default | Locust flag / behavior |
|----------|---------|------------------------|
| `AAP_PASSWORD` | `redhat123` (if unset at run time) | `--aap-password` — shared password for all `user-NNN` accounts |
| `STATUS_CHECK_DELAY_SECONDS` | `10` | `--status-check-delay-seconds` — wait after EE create before task/catalog/history/GitHub checks |
| `USE_SCM` | `false` | When `true` / `True` / `1` / `yes`, adds `--use-scm` (SCM create + GitHub verify after non-SCM) |
| `AAP_ACCESS_TOKEN` | (empty) | `--aap-access-token` on **worker** only, if set — optional scaffolder `secrets.aapToken` |
| `GITHUB_USER_OAUTH_TOKEN` | (empty) | `--github-user-oauth-token` on master/worker, if set — required for SCM publish (`secrets.USER_OAUTH_TOKEN`) |

Optional Locust flags (not wired through `test.env` today; defaults in `test/ee-builder.py`):

- `--ee-template-name` — EE template (default `ansible-execution-environment-builder-start-from-scratch`)

Example `test.env` fragment:

```bash
export SCENARIO=ee-builder
export USERS=10
export AAP_PASSWORD=redhat123
export STATUS_CHECK_DELAY_SECONDS=10
export USE_SCM=true
export GITHUB_USER_OAUTH_TOKEN=ghp_xxxxxxxx   # required when USE_SCM=true
# export AAP_ACCESS_TOKEN=...                 # optional
```

Local headless run (preferred):

```bash
make test-local
```

Equivalent to the operator run for the current `SCENARIO` and `test.env` (see `core/run-locust-local.sh`).

## Scaling the portal (`core/scale_portal.sh`)

Before a performance run you can resize the portal Deployment and PostgreSQL StatefulSet in `PORTAL_NAMESPACE`. The script sources `test.env` if present, then applies only the knobs you set (unset variables are left unchanged).

```bash
./core/scale_portal.sh
```

Preview changes without applying:

```bash
DRY_RUN=1 ./core/scale_portal.sh
```

### Namespace and deployment

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORTAL_NAMESPACE` | `self-service-portal` | Target namespace (same as load test) |
| `PORTAL_DEPLOYMENT_NAME` | `redhat-rhaap-portal` | Portal Deployment to scale |
| `AUTODISCOVER_DB` | `1` | If `DB_NAME` unset, pick first postgres-like StatefulSet in the namespace |
| `DRY_RUN` | `0` | `1` = print commands only |

### Portal application (Deployment)

Set any combination; the script runs `oc scale` / `oc set resources` as needed.

| Variable | Example | Purpose |
|----------|---------|---------|
| `PORTAL_REPLICAS` | `4` | Replica count |
| `PORTAL_CPU_REQUEST` | `1` | CPU request |
| `PORTAL_MEM_REQUEST` | `4Gi` | Memory request |
| `PORTAL_CPU_LIMIT` | `1500m` | CPU limit |
| `PORTAL_MEM_LIMIT` | `5Gi` | Memory limit |

### Database (StatefulSet)

| Variable | Default / notes | Purpose |
|----------|-----------------|---------|
| `DB_NAME` | auto if `AUTODISCOVER_DB=1` | Postgres StatefulSet name (e.g. `redhat-postgresql`) |
| `DB_REPLICAS` | | Replica count |
| `DB_CPU_REQUEST` / `DB_MEM_REQUEST` | | Requests |
| `DB_CPU_LIMIT` / `DB_MEM_LIMIT` | | Limits |
| `DB_MAX_CONNECTIONS` | | Sets DB env (see below) and restarts the StatefulSet |
| `DB_MAX_CONNECTIONS_ENV` | `POSTGRESQL_MAX_CONNECTIONS` | Env var name for max connections |
| `DB_MAX_CONNECTIONS_CONTAINER` | | `-c` container name when patching env |

If no portal or DB variables are set, the script exits successfully with a message and makes no changes.

Example `test.env` for scaling (run `./core/scale_portal.sh` before `make test`):

```bash
export PORTAL_NAMESPACE=portal-ns
export PORTAL_DEPLOYMENT_NAME=redhat-rhaap-portal
export PORTAL_REPLICAS=4
export PORTAL_CPU_REQUEST=1
export PORTAL_MEM_REQUEST=4Gi
export PORTAL_CPU_LIMIT=1500m
export PORTAL_MEM_LIMIT=5Gi

export DB_NAME=redhat-postgresql
export AUTODISCOVER_DB=0
export DB_REPLICAS=1
export DB_CPU_REQUEST=250m
export DB_MEM_REQUEST=256Mi
export DB_CPU_LIMIT=250m
export DB_MEM_LIMIT=1Gi
```

## Common commands

### Help

```bash
make help
```

### Install Locust Operator

```bash
make deploy-locust
```

### Run a test (cluster / Locust operator)

```bash
make test
```

Override parameters:

```bash
make test SCENARIO=ee-builder USERS=10 WORKERS=5 SPAWN_RATE=2 DURATION=5m
```

Notes:

- Workers are adjusted if `WORKERS > USERS` (workers must not exceed users).
- Portal and AAP URLs are discovered from cluster Routes.
- **mvp:** AAP admin password comes from `AAP_ADMIN_SECRET`.
- **ee-builder:** `AAP_PASSWORD` must be set in `test.env` (password for `user-001` … `user-N`).

### Run a test locally (`make test-local`)

Runs the same scenario with **Locust on your machine** (headless), without deploying a LocustTest CR. Uses `test.env`, discovers portal/AAP URLs from Routes, and applies the same credential rules as `make test`.

Prerequisites:

```bash
pip install -r requirements.txt
```

```bash
make test-local
make test-local SCENARIO=ee-builder USERS=2 SPAWN_RATE=1 DURATION=5m
```

Optional: `LOCUST_LOGLEVEL` (default `DEBUG`). Logs go to `$(TMP_DIR)/load-test.log`; `benchmark-before` / `benchmark-after` timestamps are written under `$(TMP_DIR)` for `make collect-results`.

You can also run the script directly: `./core/run-locust-local.sh`.

### Collect results

```bash
make collect-results
```

This runs `core/collect-result.sh` and gathers:

- `benchmark-before` / `benchmark-after` timestamps
- the applied `locust-test.yaml`
- `test.env` snapshot (if present)
- `load-test.log` (streamed Locust master logs captured during the run)
- `locust-master.log` (best-effort read of the master pod logs at collection time)
- metrics output generated by the framework (CSV + aggregated JSON in the artifacts directory)

### Clean up

Remove test resources for the current scenario:

```bash
make clean
```

Remove local temp/artifacts only:

```bash
make clean-local
```

Remove both cluster resources and local temp:

```bash
make clean-all
```

## Adding a new scenario

- Create `test/<name>.py`
- Run via `make test SCENARIO=<name>`
- If the scenario needs extra Locust flags, extend the Makefile pattern used for `EE_BUILDER_LOCUST_CMD` and `config/locust-test-template.yaml`

## Artifacts layout (what “collect-results” produces)

Artifacts are written to a timestamped directory under the repo (your exact folder name depends on how you run the framework).

Typical files:

- `benchmark.json`: aggregated measurements and per-group Locust metrics
- `locust-master.log`: Locust master summary + error report
- `load-test.log`: streamed master logs during `make test`
- `monitoring-collection.log` and `monitoring-collection-raw-data-dir/*.csv`: raw time-series exports used to build `benchmark.json`
- `locust-test.yaml`: the LocustTest CR applied for the run
- `test.env`: configuration snapshot used for the run
