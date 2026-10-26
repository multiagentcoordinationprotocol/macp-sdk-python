.PHONY: help setup lint fmt typecheck test test-integration test-integration-docker test-conformance test-parity test-all coverage build sync-fixtures verify-fixtures sync-parity verify-parity lint-fixtures dev-link-protos

SPEC_CONFORMANCE_DIR := ../multiagentcoordinationprotocol/schemas/conformance
SPEC_PARITY_DIR := ../multiagentcoordinationprotocol/schemas/parity

# <canonical-subpath>:<local-dir> pairs checked by sync-fixtures/verify-fixtures.
# "." = the flat top level of SPEC_CONFORMANCE_DIR.
FIXTURE_DIR_PAIRS := .:tests/conformance cmt-hash:tests/vectors/cmt-hash receipt:tests/vectors/receipt
# Single spec example files vendored as test fixtures: <file>:<local-path> pairs,
# <file> relative to SPEC_EXAMPLES_DIR. Derived from SPEC_CONFORMANCE_DIR so that
# overriding it (as CI does) also moves this.
SPEC_EXAMPLES_DIR ?= $(SPEC_CONFORMANCE_DIR)/../../examples/discovery
FIXTURE_FILE_PAIRS := receipt_keys.json:tests/fixtures/receipt_keys.json

help:  ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make <target>\n\nTargets:\n"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup:  ## Install SDK + dev + docs extras in editable mode.
	pip install -e ".[dev,docs]"

lint:  ## Run ruff check + ruff format --check on src/ + tests/ + examples/ (mirrors checks.yml's lint job).
	ruff check src/ tests/ examples/
	ruff format --check src/ tests/ examples/

fmt:  ## Apply ruff format across src/ + tests/ + examples/ (fixes a `make lint` formatting failure).
	ruff format src/ tests/ examples/

typecheck:  ## Run mypy against src/macp_sdk/.
	mypy src/macp_sdk/

test:  ## Run unit tests with the coverage gate (fail_under from pyproject).
	pytest tests/unit/ -v --cov

test-integration:  ## Run integration tests (requires a running MACP runtime).
	pytest tests/integration/ -v -m integration

RUNTIME_IMAGE := ghcr.io/multiagentcoordinationprotocol/macp-runtime:0.9.0

test-integration-docker:  ## Run integration tests against the CI-pinned runtime image (needs docker).
	docker rm -f macp-sdk-it macp-sdk-it-ro >/dev/null 2>&1 || true
	docker run -d --name macp-sdk-it -p 50061:50051 -e MACP_ALLOW_INSECURE=1 \
		-e MACP_MEMORY_ONLY=1 -e MACP_BIND_ADDR=0.0.0.0:50051 $(RUNTIME_IMAGE)
	docker run -d --name macp-sdk-it-ro -p 50062:50051 -e MACP_ALLOW_INSECURE=1 \
		-e MACP_MEMORY_ONLY=1 -e MACP_BIND_ADDR=0.0.0.0:50051 -e MACP_POLICIES_DIR=/policies \
		-v "$(CURDIR)/tests/integration/fixtures/policies:/policies:ro" $(RUNTIME_IMAGE)
	for port in 50061 50062; do \
		for i in $$(seq 1 30); do nc -z localhost $$port && break; sleep 1; done; \
		nc -z localhost $$port || { echo "runtime on :$$port never became ready"; exit 1; }; \
	done
	MACP_RUNTIME_TARGET=127.0.0.1:50061 MACP_TEST_READ_ONLY_REGISTRY=127.0.0.1:50062 \
		pytest tests/integration/ -m integration -p no:randomly --strict-markers --no-cov -v -rs; \
		status=$$?; docker rm -f macp-sdk-it macp-sdk-it-ro >/dev/null; exit $$status

test-conformance:  ## Replay the canonical conformance fixtures.
	pytest tests/conformance/ -v -m conformance

test-parity:  ## Run the cross-SDK parity contract test (tests/parity/).
	pytest tests/parity/ -v

test-all: lint typecheck test test-integration test-conformance test-parity lint-fixtures  ## Run the full green-bar matrix.

coverage:  ## Unit tests with HTML + terminal coverage report.
	pytest tests/unit/ --cov --cov-report=html --cov-report=term

# dist/ is wiped before building because `twine check dist/*` globs
# everything in the directory -- without the wipe, a stale artifact left
# over from a previous (or failed) build would get re-validated alongside
# the new one.
build:  ## Build sdist + wheel into dist/, then validate with twine check (mirrors ci.yml's build job).
	rm -rf dist/
	python -m build
	@command -v twine >/dev/null 2>&1 || { echo "twine not found -- run 'make setup' (or pip install -e \".[dev]\")"; exit 1; }
	twine check dist/*

## Sync conformance fixtures from canonical source
sync-fixtures:  ## Copy conformance fixtures from the spec repo into tests/conformance/, tests/vectors/{cmt-hash,receipt}/ and tests/fixtures/.
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
	@for pair in $(FIXTURE_FILE_PAIRS); do \
		src="$(SPEC_EXAMPLES_DIR)/$${pair%%:*}"; dest="$${pair#*:}"; \
		if [ ! -f "$$src" ]; then \
			echo "  MISSING: canonical file $$src does not exist (FIXTURE_FILE_PAIRS entry '$$pair')"; \
			exit 1; \
		fi; \
		mkdir -p "$$(dirname "$$dest")"; \
		cp "$$src" "$$dest" || exit 1; \
		echo "  Copied $$dest"; \
	done
	@echo "Done. Run 'git diff tests/conformance/ tests/vectors/ tests/fixtures/' to review changes."
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
	for pair in $(FIXTURE_FILE_PAIRS); do \
		src="$(SPEC_EXAMPLES_DIR)/$${pair%%:*}"; dest="$${pair#*:}"; \
		if [ ! -f "$$src" ]; then \
			echo "  MISSING: canonical file $$src does not exist (FIXTURE_FILE_PAIRS entry '$$pair')"; \
			drift=1; missing=1; \
		elif ! cmp -s "$$src" "$$dest"; then \
			echo "  DRIFT: $$dest differs from (or is missing vs) $$src"; \
			drift=1; \
		else \
			echo "  OK: $$dest matches $$src"; \
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

## Sync the vendored parity contract from canonical source
sync-parity:  ## Copy contract.json from the spec repo into tests/parity/.
	@if [ ! -f "$(SPEC_PARITY_DIR)/contract.json" ]; then \
		echo ""; \
		echo "  sync-parity: canonical contract.json not found at $(SPEC_PARITY_DIR)/contract.json"; \
		echo ""; \
		echo "  Clone the spec repo alongside this repo:"; \
		echo "    git clone https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol $(dir $(abspath $(lastword $(MAKEFILE_LIST))))../multiagentcoordinationprotocol"; \
		echo ""; \
		echo "  or override SPEC_PARITY_DIR=/path/to/schemas/parity"; \
		echo ""; \
		exit 1; \
	fi
	cp "$(SPEC_PARITY_DIR)/contract.json" tests/parity/contract.json
	@echo "Copied tests/parity/contract.json. Run 'git diff tests/parity/contract.json' to review changes."

verify-parity:  ## Fail if the vendored parity contract drifted from canonical (CI drift gate).
	@if [ ! -f "$(SPEC_PARITY_DIR)/contract.json" ]; then \
		echo "  verify-parity: canonical contract.json not found at $(SPEC_PARITY_DIR)/contract.json"; \
		exit 1; \
	fi
	@if diff -q "$(SPEC_PARITY_DIR)/contract.json" tests/parity/contract.json >/dev/null 2>&1; then \
		echo "  OK: tests/parity/contract.json matches $(SPEC_PARITY_DIR)/contract.json"; \
	else \
		echo "  DRIFT: tests/parity/contract.json differs from (or is missing vs) canonical"; \
		echo "Vendored parity contract drifted from canonical. Run 'make sync-parity' and commit."; \
		exit 1; \
	fi

lint-fixtures:  ## Lint canonical conformance fixtures for internal consistency (spec repo's lint_fixtures.py; mirrors conformance-fixtures.yml).
	@if [ ! -d "$(SPEC_CONFORMANCE_DIR)" ]; then \
		echo "  lint-fixtures: spec repo not found at $(SPEC_CONFORMANCE_DIR)"; \
		exit 1; \
	fi
	@if [ ! -f "$(SPEC_CONFORMANCE_DIR)/lint_fixtures.py" ]; then \
		echo "  lint-fixtures: lint_fixtures.py not found at $(SPEC_CONFORMANCE_DIR)/lint_fixtures.py"; \
		exit 1; \
	fi
	python3 $(SPEC_CONFORMANCE_DIR)/lint_fixtures.py

## Install local proto package for development (test proto changes before publishing)
dev-link-protos:  ## Install ../multiagentcoordinationprotocol/packages/proto-python in editable mode.
	pip install -e ../multiagentcoordinationprotocol/packages/proto-python
	@echo "Installed local macp-proto. Changes to proto-python/src/macp/ are reflected immediately."
