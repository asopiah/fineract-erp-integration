.DEFAULT_GOAL := help
SHELL := /bin/bash

FINERACT_TAG   := 1.15.0
FINERACT_DIR   := upstream/fineract
FINERACT_IMAGE := fineract-gl-sync:$(FINERACT_TAG)
PATCH          := $(CURDIR)/fineract-customization/gl-account-external-id.patch
GRADLE         := cd $(FINERACT_DIR) && ./gradlew

GL_UNIT_TESTS := --tests 'org.apache.fineract.accounting.glaccount.*'
GL_IT_TESTS   := --tests 'org.apache.fineract.integrationtests.accounting.GLAccountExternalIdIntegrationTest'

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-22s %s\n", $$1, $$2}'

# --- Fineract (Java, JDK 21) -------------------------------------------------------------------------------------

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
fineract-image: fineract-patch ## Build the customized Fineract image (fineract-gl-sync) from source
	$(GRADLE) :fineract-provider:jibDockerBuild -x test -Djib.to.image=$(FINERACT_IMAGE)

.PHONY: test-java
test-java: fineract-patch ## Java unit tests for the customization
	$(GRADLE) :fineract-core:test $(GL_UNIT_TESTS) :fineract-accounting:test $(GL_UNIT_TESTS)

.PHONY: test-java-it
test-java-it: fineract-patch ## Java integration tests against the running stack
	cd $(FINERACT_DIR) && BACKEND_PROTOCOL=http BACKEND_HOST=localhost BACKEND_PORT=$${FINERACT_HOST_PORT:-58080} \
		./gradlew :integration-tests:test $(GL_IT_TESTS) -PcargoDisabled --max-workers=2

# --- Stack -------------------------------------------------------------------------------------------------------

.PHONY: up
up: ## Start the whole stack and wait until it is healthy
	docker compose up -d --build --wait

.PHONY: down
down: ## Stop the stack and delete its data
	docker compose down -v
