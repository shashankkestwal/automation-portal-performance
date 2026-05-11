# Override test environment variables
ifneq (,$(wildcard test.env))
	include test.env
endif


# Scenario to run — corresponds to test/<SCENARIO>.py
export SCENARIO ?= mvp

# Namespace configuration (URLs and secrets are auto-discovered from these)
export PORTAL_NAMESPACE ?= self-service-portal
export AAP_NAMESPACE ?= ansible-automation-platform

# Route names within each namespace
export PORTAL_ROUTE ?= sap
export AAP_ROUTE ?= aap

# Secret containing AAP admin password (data key: password)
AAP_ADMIN_SECRET ?= $(AAP_ROUTE)-admin-password

# Optional: AAP access token for Locust mvp (scaffolder secrets.aapToken); set in test.env
AAP_ACCESS_TOKEN ?=
export AAP_ACCESS_TOKEN

# GitHub PAT for ee-builder SCM publish (secrets.USER_OAUTH_TOKEN); set in test.env when SCENARIO=ee-builder
GITHUB_USER_OAUTH_TOKEN ?=
export GITHUB_USER_OAUTH_TOKEN

# Number of locust worker pods (primary scaling knob)
export WORKERS ?= 5

# Locust load parameters (sensible defaults, override as needed)
export USERS ?= 10
export SPAWN_RATE ?= 2
export DURATION ?= 10s
# Passed to Locust as --scaffolder-task-status-delay-seconds (see config/locust-test-template.yaml)
export SCAFFOLDER_TASK_STATUS_DELAY_SECONDS ?= 10
export LOCUST_EXTRA_CMD ?= "--debug=true"

# Locust operator
export LOCUST_NAMESPACE ?= locust-operator
LOCUST_OPERATOR_REPO = locust-k8s-operator
LOCUST_OPERATOR = locust-operator

# Local directory to store temporary files
export TMP_DIR ?= $(shell python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' .tmp)

.DEFAULT_GOAL := help

##	=== Setup ===

## Create operator namespace
.PHONY: namespace
namespace:
	@kubectl create namespace $(LOCUST_NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	@echo "Enabling Prometheus monitoring for namespace $(LOCUST_NAMESPACE)..."

## Create temp directory
$(TMP_DIR):
	mkdir -p $(TMP_DIR)

##	=== Locust Operator ===

## Deploy and install locust operator helm chart
.PHONY: deploy-locust
deploy-locust: namespace $(TMP_DIR)
	helm repo add $(LOCUST_OPERATOR_REPO) https://abdelrhmanhamouda.github.io/locust-k8s-operator/ --force-update --namespace $(LOCUST_NAMESPACE)
	@if ! helm list --namespace $(LOCUST_NAMESPACE) | grep -q "$(LOCUST_OPERATOR)"; then \
		envsubst < ./config/locust-k8s-operator.values.yaml > $(TMP_DIR)/locust-k8s-operator.values.yaml; \
		helm install $(LOCUST_OPERATOR) locust-k8s-operator/locust-k8s-operator --namespace $(LOCUST_NAMESPACE) --values $(TMP_DIR)/locust-k8s-operator.values.yaml; \
	else \
		echo "Helm release \"$(LOCUST_OPERATOR)\" already exists"; \
	fi
	kubectl wait --timeout=180s --namespace $(LOCUST_NAMESPACE) --for=condition=ready $$(kubectl get --namespace $(LOCUST_NAMESPACE) pod -l app.kubernetes.io/name=locust-k8s-operator -o name)

## Uninstall locust operator helm chart
.PHONY: undeploy-locust
undeploy-locust: clean
	@kubectl delete crd locusttests.locust.io --wait --ignore-not-found=true
	@kubectl delete namespace $(LOCUST_NAMESPACE) --wait --ignore-not-found=true
	@kubectl delete clusterrolebinding $(LOCUST_NAMESPACE)-locust-k8s-operator --wait --ignore-not-found=true
	@kubectl delete clusterrole $(LOCUST_NAMESPACE)-locust-k8s-operator --wait --ignore-not-found=true
	@helm repo remove $(LOCUST_OPERATOR_REPO) || true

##	=== Testing ===

## Run the locust test via operator
## Usage: make test WORKERS=5
## Run `make test SCENARIO=...` to run a specific scenario
.PHONY: test
test: $(TMP_DIR)
ifneq ($(shell test $(USERS) -gt $(WORKERS) && echo 1 || echo 0),0)
	@echo "Users ($(USERS)) distributed across Workers ($(WORKERS))"
else
	$(eval WORKERS := $(USERS))
	@echo "Adjusted WORKERS to $(USERS) (workers must not exceed users)"
endif
	@if [ -f test.env ]; then cp -f test.env $(TMP_DIR)/test.env && echo "Snapshotted test.env -> $(TMP_DIR)/test.env"; fi
	@PORTAL_URL="https://$$(oc -n $(PORTAL_NAMESPACE) get route $(PORTAL_ROUTE) -o jsonpath='{.spec.host}')"; \
	AAP_URL="https://$$(oc -n $(AAP_NAMESPACE) get route $(AAP_ROUTE) -o jsonpath='{.spec.host}')"; \
	AAP_PASSWORD="$$(oc -n $(AAP_NAMESPACE) get secret $(AAP_ADMIN_SECRET) -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)"; \
	[ -n "$$AAP_PASSWORD" ] || { echo "ERROR: empty admin password (secret $(AAP_ADMIN_SECRET) in $(AAP_NAMESPACE))" >&2; exit 1; }; \
	export AAP_ACCESS_TOKEN_FLAG="$${AAP_ACCESS_TOKEN:+--aap-access-token $$AAP_ACCESS_TOKEN}"; \
	export GITHUB_USER_OAUTH_TOKEN_FLAG="$${GITHUB_USER_OAUTH_TOKEN:+--github-user-oauth-token $$GITHUB_USER_OAUTH_TOKEN}"; \
	export PORTAL_URL AAP_URL AAP_PASSWORD; \
	echo "Portal URL: $$PORTAL_URL"; \
	echo "AAP URL:    $$AAP_URL"; \
	envsubst '$$PORTAL_URL $$AAP_URL $$AAP_PASSWORD' < test/$(SCENARIO).py > $(TMP_DIR)/$(SCENARIO).py; \
	envsubst < config/locust-test-template.yaml | tee $(TMP_DIR)/locust-test.yaml | kubectl apply --namespace $(LOCUST_NAMESPACE) -f -
	kubectl create --namespace $(LOCUST_NAMESPACE) configmap locust.$(SCENARIO) --from-file $(TMP_DIR)/$(SCENARIO).py --dry-run=client -o yaml | kubectl apply --namespace $(LOCUST_NAMESPACE) -f -

	@echo "Waiting for Locust operator to create the service..."
	@timeout=$$(python3 -c "from datetime import datetime, timedelta;t_add=int('60'); print(int((datetime.now() + timedelta(seconds=t_add)).timestamp()))"); while [ -z "$$(kubectl get --namespace $(LOCUST_NAMESPACE) svc $(SCENARIO)-test-master -o name 2>/dev/null)" ]; do if [ "$$(date "+%s")" -gt "$$timeout" ]; then echo "ERROR: Timeout waiting for service $(SCENARIO)-test-master"; exit 1; else echo "Waiting for service..."; sleep 2s; fi; done
	@echo "Labeling the Locust service for Prometheus discovery..."
	kubectl label --namespace $(LOCUST_NAMESPACE) svc $(SCENARIO)-test-master app=locust-test scenario=$(SCENARIO) --overwrite
	envsubst < config/locust-metrics-service-template.yaml | kubectl apply --namespace $(LOCUST_NAMESPACE) -f -
	timeout=$$(python3 -c "from datetime import datetime, timedelta;t_add=int('680'); print(int((datetime.now() + timedelta(seconds=t_add)).timestamp()))"); while [ -z "$$(kubectl get --namespace $(LOCUST_NAMESPACE) pod -l performance-test-pod-name=$(SCENARIO)-test-master -o name)" ]; do if [ "$$(date "+%s")" -gt "$$timeout" ]; then echo "ERROR: Timeout waiting for locust master pod to start"; exit 1; else echo "Waiting for locust master pod to start..."; sleep 5s; fi; done
	date -u -Ins>$(TMP_DIR)/benchmark-before
	kubectl wait --namespace $(LOCUST_NAMESPACE) --for=condition=Ready=true $$(kubectl get --namespace $(LOCUST_NAMESPACE) pod -l performance-test-pod-name=$(SCENARIO)-test-master -o name) --timeout=60s
	@echo "Getting locust master log:"
	kubectl logs --namespace $(LOCUST_NAMESPACE) -f -l performance-test-pod-name=$(SCENARIO)-test-master | tee $(TMP_DIR)/load-test.log
	date -u -Ins>$(TMP_DIR)/benchmark-after
	@echo "Test completed at $$(date -u -Ins)"

.PHONY: collect-results
collect-results:
	@echo "Collecting results..."
	./core/collect-result.sh


## Remove test resources from cluster
## Run `make clean SCENARIO=...` to clean a specific scenario
.PHONY: clean
clean:
	kubectl delete --namespace $(LOCUST_NAMESPACE) cm locust.$(SCENARIO) --ignore-not-found --wait
	kubectl delete --namespace $(LOCUST_NAMESPACE) locusttests.locust.io $(SCENARIO).test --ignore-not-found --wait || true
	kubectl delete --namespace $(LOCUST_NAMESPACE) servicemonitor $(SCENARIO)-test-metrics --ignore-not-found --wait
	kubectl delete --namespace $(LOCUST_NAMESPACE) svc $(SCENARIO)-test-metrics --ignore-not-found --wait
## Remove local artifacts
.PHONY: clean-local
clean-local:
	rm -rf results/ __pycache__/ *.log $(TMP_DIR)

## Remove everything
.PHONY: clean-all
clean-all: clean clean-local

##	=== Help ===

## Print help message for all Makefile targets
## Run `make` or `make help` to see the help
.PHONY: help
help:
	@printf "Usage:\n  make <target>\n\n";
	@awk '{ \
			if ($$0 ~ /^.PHONY: [a-zA-Z\-_0-9]+$$/) { \
				helpCommand = substr($$0, index($$0, ":") + 2); \
				if (helpMessage) { \
					printf "\033[36m%-20s\033[0m %s\n", \
						helpCommand, helpMessage; \
					helpMessage = ""; \
				} \
			} else if ($$0 ~ /^[a-zA-Z\-_0-9.]+:/) { \
				helpCommand = substr($$0, 0, index($$0, ":")); \
				if (helpMessage) { \
					printf "\033[36m%-20s\033[0m %s\n", \
						helpCommand, helpMessage; \
					helpMessage = ""; \
				} \
			} else if ($$0 ~ /^##/) { \
				if (helpMessage) { \
					helpMessage = helpMessage"\n                     "substr($$0, 3); \
				} else { \
					helpMessage = substr($$0, 3); \
				} \
			} else { \
				if (helpMessage) { \
					print "\n                     "helpMessage"\n" \
				} \
				helpMessage = ""; \
			} \
		}' \
		$(MAKEFILE_LIST)
