.PHONY: help setup lint fmt typecheck test test-integration test-conformance test-all coverage build sync-fixtures verify-fixtures dev-link-protos

SPEC_CONFORMANCE_DIR := ../multiagentcoordinationprotocol/schemas/conformance

# <canonical-subpath>:<local-dir> pairs checked by sync-fixtures/verify-fixtures.
# "." = the flat top level of SPEC_CONFORMANCE_DIR.
FIXTURE_DIR_PAIRS := .:tests/conformance cmt-hash:tests/vectors/cmt-hash

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

test:  ## Run unit tests with the coverage gate (fail_under from pyproject).
	pytest tests/unit/ -v --cov

test-integration:  ## Run integration tests (requires a running MACP runtime).
	pytest tests/integration/ -v -m integration

test-conformance:  ## Replay the canonical conformance fixtures.
	pytest tests/conformance/ -v -m conformance

test-all: lint typecheck test test-integration test-conformance  ## Run the full green-bar matrix.

coverage:  ## Unit tests with HTML + terminal coverage report.
	pytest tests/unit/ --cov --cov-report=html --cov-report=term

build:  ## Build sdist + wheel into dist/.
	python -m build

## Sync conformance fixtures from canonical source
sync-fixtures:  ## Copy conformance fixtures from the spec repo into tests/conformance/ and tests/vectors/cmt-hash/.
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
	@for pair in $(FIXTURE_DIR_PAIRS); do \
		sub="$${pair%%:*}"; \
		canon="$(SPEC_CONFORMANCE_DIR)"; \
		[ "$$sub" = "." ] || canon="$$canon/$$sub"; \
		if [ ! -d "$$canon" ]; then \
			echo "  MISSING: canonical directory $$canon does not exist (FIXTURE_DIR_PAIRS entry '$$pair')"; \
			exit 1; \
		fi; \
	done
	@for pair in $(FIXTURE_DIR_PAIRS); do \
		sub="$${pair%%:*}"; dest="$${pair#*:}"; \
		canon="$(SPEC_CONFORMANCE_DIR)"; \
		[ "$$sub" = "." ] || canon="$$canon/$$sub"; \
		mkdir -p "$$dest"; \
		for f in "$$canon"/*.json; do \
			[ -e "$$f" ] || continue; \
			cp "$$f" "$$dest"/ || exit 1; \
			echo "  Copied $$dest/$$(basename "$$f")"; \
		done; \
	done
	@echo "Done. Run 'git diff tests/conformance/ tests/vectors/cmt-hash/' to review changes."
	@echo "Note: sync-fixtures copies but never deletes -- a local file flagged EXTRA by 'make verify-fixtures' must be removed by hand."

verify-fixtures:  ## Fail if local fixtures drifted from canonical (CI drift gate).
	@if [ ! -d "$(SPEC_CONFORMANCE_DIR)" ]; then \
		echo "  verify-fixtures: spec repo not found at $(SPEC_CONFORMANCE_DIR)"; \
		exit 1; \
	fi
	@drift=0; missing=0; \
	for pair in $(FIXTURE_DIR_PAIRS); do \
		sub="$${pair%%:*}"; dest="$${pair#*:}"; \
		canon="$(SPEC_CONFORMANCE_DIR)"; \
		[ "$$sub" = "." ] || canon="$$canon/$$sub"; \
		if [ ! -d "$$canon" ]; then \
			echo "  MISSING: canonical directory $$canon does not exist (FIXTURE_DIR_PAIRS entry '$$pair')"; \
			drift=1; missing=1; \
			continue; \
		fi; \
		pair_drift=0; \
		for f in "$$canon"/*.json; do \
			[ -e "$$f" ] || continue; \
			b=$$(basename "$$f"); \
			if ! diff -q "$$f" "$$dest/$$b" >/dev/null 2>&1; then \
				echo "  DRIFT: $$dest/$$b differs from (or is missing vs) canonical"; \
				drift=1; pair_drift=1; \
			fi; \
		done; \
		for f in "$$dest"/*.json; do \
			[ -e "$$f" ] || continue; \
			b=$$(basename "$$f"); \
			if [ ! -f "$$canon/$$b" ]; then \
				echo "  EXTRA: $$dest/$$b has no canonical source"; \
				drift=1; pair_drift=1; \
			fi; \
		done; \
		if [ $$pair_drift -eq 0 ]; then \
			echo "  OK: $$dest matches $$canon"; \
		fi; \
	done; \
	if [ $$drift -ne 0 ]; then \
		echo "Conformance fixtures drifted from canonical. Run 'make sync-fixtures' and commit."; \
		if [ $$missing -ne 0 ]; then \
			echo "Note: sync-fixtures can't fix MISSING -- fix FIXTURE_DIR_PAIRS/checkout, and delete the orphaned dir if you drop it."; \
		fi; \
		exit 1; \
	fi; \
	echo "All conformance fixtures match the canonical source."

## Install local proto package for development (test proto changes before publishing)
dev-link-protos:  ## Install ../multiagentcoordinationprotocol/packages/proto-python in editable mode.
	pip install -e ../multiagentcoordinationprotocol/packages/proto-python
	@echo "Installed local macp-proto. Changes to proto-python/src/macp/ are reflected immediately."
