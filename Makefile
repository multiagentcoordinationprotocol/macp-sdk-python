.PHONY: help setup lint fmt typecheck test test-integration test-conformance test-all coverage build sync-fixtures verify-fixtures dev-link-protos

SPEC_CONFORMANCE_DIR := ../multiagentcoordinationprotocol/schemas/conformance

help:  ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make <target>\n\nTargets:\n"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup:  ## Install SDK + dev + docs extras in editable mode.
	pip install -e ".[dev,docs]"

lint:  ## Run ruff check on src/ + tests/ + examples/.
	ruff check src/ tests/ examples/

fmt:  ## Apply ruff format across src/ + tests/ + examples/.
	ruff format src/ tests/ examples/

typecheck:  ## Run mypy against src/macp_sdk/.
	mypy src/macp_sdk/

test:  ## Run unit tests.
	pytest tests/unit/ -v

test-integration:  ## Run integration tests (requires a running MACP runtime).
	pytest tests/integration/ -v -m integration

test-conformance:  ## Replay the canonical conformance fixtures.
	pytest tests/conformance/ -v -m conformance

test-all: lint typecheck test test-integration test-conformance  ## Run the full green-bar matrix.

coverage:  ## Unit tests with HTML + terminal coverage report.
	pytest --cov=macp_sdk --cov-report=html --cov-report=term tests/unit/

build:  ## Build sdist + wheel into dist/.
	python -m build

## Sync conformance fixtures from canonical source
sync-fixtures:  ## Copy conformance fixtures from the spec repo into tests/conformance/.
	@if [ ! -d "$(SPEC_CONFORMANCE_DIR)" ]; then \
		echo ""; \
		echo "  sync-fixtures: spec repo not found at $(SPEC_CONFORMANCE_DIR)"; \
		echo ""; \
		echo "  Clone it alongside this repo:"; \
		echo "    git clone https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol $(dir $(abspath $(lastword $(MAKEFILE_LIST))))../multiagentcoordinationprotocol"; \
		echo ""; \
		echo "  or override SPEC_CONFORMANCE_DIR=/path/to/schemas/conformance"; \
		echo ""; \
		exit 1; \
	fi
	@for f in $(SPEC_CONFORMANCE_DIR)/*.json; do \
		cp "$$f" tests/conformance/; \
		echo "  Copied $$(basename $$f)"; \
	done
	@echo "Done. Run 'git diff tests/conformance/' to review changes."

verify-fixtures:  ## Fail if local fixtures drifted from canonical (CI drift gate).
	@if [ ! -d "$(SPEC_CONFORMANCE_DIR)" ]; then \
		echo "  verify-fixtures: spec repo not found at $(SPEC_CONFORMANCE_DIR)"; \
		exit 1; \
	fi
	@drift=0; \
	for f in $(SPEC_CONFORMANCE_DIR)/*.json; do \
		b=$$(basename "$$f"); \
		if ! diff -q "$$f" "tests/conformance/$$b" >/dev/null 2>&1; then \
			echo "  DRIFT: tests/conformance/$$b differs from canonical"; drift=1; \
		fi; \
	done; \
	if [ $$drift -ne 0 ]; then \
		echo "Conformance fixtures drifted from canonical. Run 'make sync-fixtures' and commit."; \
		exit 1; \
	fi; \
	echo "All conformance fixtures match the canonical source."

## Install local proto package for development (test proto changes before publishing)
dev-link-protos:  ## Install ../multiagentcoordinationprotocol/packages/proto-python in editable mode.
	pip install -e ../multiagentcoordinationprotocol/packages/proto-python
	@echo "Installed local macp-proto. Changes to proto-python/src/macp/ are reflected immediately."
