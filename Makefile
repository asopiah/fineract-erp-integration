.DEFAULT_GOAL := help
SHELL := /bin/bash

FINERACT_TAG   := 1.15.0
FINERACT_DIR   := upstream/fineract
FINERACT_IMAGE := fineract-gl-sync:$(FINERACT_TAG)
PATCH          := $(CURDIR)/fineract-customization/gl-account-external-id.patch
GRADLE         := cd $(FINERACT_DIR) && ./gradlew

GL_UNIT_TESTS := --tests 'org.apache.fineract.accounting.glaccount.*'
GL_IT_TESTS   := --tests 'org.apache.fineract.integrationtests.accounting.GLAccountExternalIdIntegrationTest'
# Upstream's own GL integration tests: they never send an external id, so they pin the behaviour from before the patch
GL_UPSTREAM_IT_TESTS := --tests 'org.apache.fineract.integrationtests.accounting.GLAccountIntegrationTest'
TEST_PROFILE  := -f docker-compose.yml -f docker/fineract-test-profile.yml

VENV   := $(CURDIR)/.venv
BIN    := $(VENV)/bin
PYTHON_SERVICES := mock-erp integration-service

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_%-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-22s %s\n", $$1, $$2}'

# --- Stack ---------------------------------------------------------------------------------------------------------

.PHONY: up
up: ## Start and configure the whole stack (Fineract is built only if its image is missing), then wait until ready
	docker compose build mock-erp integration-service
	docker compose up -d --wait postgres fineract mock-erp integration-service
	docker compose up bootstrap --exit-code-from bootstrap

.PHONY: down
down: ## Stop the stack and delete its data
	docker compose down -v

# --- Fineract (Java, JDK 21) ---------------------------------------------------------------------------------------

$(FINERACT_DIR):
	git clone --depth 1 --branch $(FINERACT_TAG) https://github.com/apache/fineract.git $(FINERACT_DIR)

.PHONY: fineract-patch
fineract-patch: $(FINERACT_DIR) ## Apply the GL external id patch to the upstream checkout (idempotent)
	@if git -C $(FINERACT_DIR) apply --reverse --check $(PATCH) 2>/dev/null; then \
		echo "patch already applied"; \
	else \
		git -C $(FINERACT_DIR) apply $(PATCH) && echo "patch applied"; \
	fi

.PHONY: fineract-image
fineract-image: fineract-patch ## Build the customized Fineract image with the host JDK (fast path)
	$(GRADLE) :fineract-provider:jibDockerBuild -x test -Djib.to.image=$(FINERACT_IMAGE)

.PHONY: test-java
test-java: fineract-patch ## Java unit tests for the customization (always re-run, never cached)
	$(GRADLE) :fineract-core:test --rerun $(GL_UNIT_TESTS) :fineract-accounting:test --rerun $(GL_UNIT_TESTS)

.PHONY: test-java-it
test-java-it: fineract-patch ## Java integration tests against the running stack
	cd $(FINERACT_DIR) && BACKEND_PROTOCOL=http BACKEND_HOST=localhost BACKEND_PORT=$${FINERACT_HOST_PORT:-58080} \
		./gradlew :integration-tests:test --rerun $(GL_IT_TESTS) -PcargoDisabled --max-workers=2

# Proves the patch broke nothing that worked before: upstream's GL tests run next to ours. Upstream's tests need
# Fineract's "test" Spring profile (see docker/fineract-test-profile.yml), so Fineract is restarted with it for the run
# and always restarted without it afterwards, even when tests fail; the target still exits with the tests' status.
.PHONY: test-java-regression
test-java-regression: fineract-patch ## Upstream's GL integration tests + ours against Fineract in its "test" profile
	docker compose $(TEST_PROFILE) up -d --wait fineract
	cd $(FINERACT_DIR) && BACKEND_PROTOCOL=http BACKEND_HOST=localhost BACKEND_PORT=$${FINERACT_HOST_PORT:-58080} \
		./gradlew :integration-tests:test --rerun $(GL_UPSTREAM_IT_TESTS) $(GL_IT_TESTS) -PcargoDisabled --max-workers=2; \
		status=$$?; cd $(CURDIR) && docker compose up -d --wait fineract; exit $$status

# --- Python ------------------------------------------------------------------------------------------------------

$(BIN)/python:
	python3.12 -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e './mock-erp[dev]' -e './integration-service[dev]'

.PHONY: python-setup
python-setup: $(BIN)/python ## Create the virtualenv with both services and their dev tools

.PHONY: lint-python
lint-python: python-setup ## Ruff (lint + format check) and mypy --strict for both services
	@for service in $(PYTHON_SERVICES); do \
		echo "== $$service"; \
		(cd $$service && $(BIN)/ruff check . && $(BIN)/ruff format --check . && $(BIN)/mypy) || exit 1; \
	done

.PHONY: test-python
test-python: lint-python ## Lint, type-check, then unit and component tests (needs PostgreSQL: started here)
	docker compose up -d --wait postgres
	@for service in $(PYTHON_SERVICES); do \
		echo "== $$service"; \
		(cd $$service && $(BIN)/pytest) || exit 1; \
	done

.PHONY: test-stack
test-stack: python-setup ## Every simulation as an automated test against the running stack (make up first)
	cd integration-service && $(BIN)/pytest -m stack -v

.PHONY: simulate
simulate: python-setup ## Run every simulation and write their evidence to SIMULATIONS.md
	cd integration-service && $(BIN)/python -m simulations --output $(CURDIR)/SIMULATIONS.md

simulate-%: python-setup ## Run one simulation, e.g. make simulate-duplicates (list: make simulations)
	cd integration-service && $(BIN)/python -m simulations $*

.PHONY: simulations
simulations: python-setup ## List the simulation names
	@cd integration-service && $(BIN)/python -c "from simulations.scenarios import SCENARIOS; print('\n'.join(SCENARIOS))"

.PHONY: test
test: test-python test-java ## All tests that do not need the stack (Python and Java)
