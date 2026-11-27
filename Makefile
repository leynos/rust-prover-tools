MDLINT ?= markdownlint-cli2
# `make fmt` and `make check-fmt` call mdtablefix directly. `--git` selects the
# Markdown files Git tracks and `--include-untracked` adds the untracked files
# Git does not ignore, so a new document is formatted before it is staged.
# Both modes need mdtablefix 0.6.0 or later; CI pins the version at the
# install-mdtablefix step.
MDTABLEFIX ?= mdtablefix
MDTABLEFIX_SELECT = --git --include-untracked
MDTABLEFIX_RULES = --wrap --renumber --breaks --ellipsis --fences
NIXIE ?= nixie
export PATH := $(HOME)/.local/bin:$(HOME)/.bun/bin:$(PATH)
# Every uv call goes through the vendored helper, which cleans the environment,
# uses the global uv cache, runs offline first and goes online at most once. See
# https://github.com/leynos/shared-actions/tree/main/uv_gate for the contract.
# The one exception is typos-config-builder at a release tag, which the spelling
# baseline requires to be called directly.
UV_GATE ?= python3 scripts/uv_gate.py
UV ?= $(shell command -v uv 2>/dev/null || printf '%s/.local/bin/uv' "$$HOME")
# The helper finds uv on PATH, so a caller's UV=/path/to/uv is honoured by putting
# that uv's directory first on PATH for every recipe.
export PATH := $(if $(findstring /,$(UV)),$(patsubst %/,%,$(dir $(UV))):)$(PATH)
# A UV given on the command line or in the environment must be uv itself or an executable
# file named uv: the helper finds uv on PATH by that name and would otherwise use another.
ifneq ($(filter command line environment,$(origin UV)),)
ifneq ($(UV),uv)
ifeq ($(findstring /,$(UV)),)
$(error UV=$(UV) must be uv or a path to an executable file named uv)
endif
ifeq ($(shell test -f '$(UV)' -a -x '$(UV)' && printf y),)
$(error UV=$(UV) is not an executable file)
endif
ifneq ($(notdir $(UV)),uv)
$(error UV=$(UV) must be named uv)
endif
endif
endif
USER_CARGO := $(HOME)/.cargo/bin/cargo
USER_WHITAKER := $(HOME)/.local/bin/whitaker
USER_BIN_PATH := $(HOME)/.cargo/bin:$(HOME)/.local/bin:$(HOME)/.bun/bin
TOOLS = $(MDLINT)
VENV_TOOLS = pytest
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY := 1

# The CV-005 CodeScene contracts live in shared-actions and run from a full
# commit, so a fix is a pin bump. `.github/cv005.toml` holds this repository's
# only parameters.
CV005_CONTRACTS_REF ?= 88977798a5c3bae1549afb99642529488c665276
CV005_CONTRACTS = $(UV_GATE) tool --python 3.13 --from 'git+https://github.com/leynos/shared-actions@$(CV005_CONTRACTS_REF)\#subdirectory=packages/cv005-contracts' -- cv005-contracts

PYTEST_XDIST_WORKERS ?= auto
TYPOS_CONFIG_BUILDER_VERSION ?= v0.1.3
TYPOS_CONFIG_BUILDER = $(UV) tool run --python 3.14 --from \
	"git+https://github.com/leynos/typos-config-builder.git@$(TYPOS_CONFIG_BUILDER_VERSION)" \
	typos-config-builder
PYTHON_TARGETS ?= rust_prover_tools tests
PYLINT_PYTHON ?= pypy@3.12
PYLINT_VERSION ?= 4.0.9
PYLINT_TARGETS ?= $(PYTHON_TARGETS)
PYLINT = $(UV_GATE) tool --managed-python --python $(PYLINT_PYTHON) --from 'pylint==$(PYLINT_VERSION)' -- pylint


.PHONY: prepare help all clean build build-release lint lint-python interrogate fmt check-fmt \
        markdownlint nixie spelling test typecheck $(TOOLS) $(VENV_TOOLS) test-workflow-contracts \
        workflow-check actionlint kani-snapshot-smoke kani-snapshot-verify

prepare: ## Install the locked development environment, offline first
	$(UV_GATE) prepare --group dev

.DEFAULT_GOAL := all

test-workflow-contracts: ## Check the CV-005 CodeScene workflow contracts
	$(CV005_CONTRACTS) check --repository .

# The workflow files are executable policy, so they get both a syntax/semantic
# linter and the contract tests that pin guards, permissions, artefacts and
# budgets. `ACTIONLINT` is overridable for a host that installs it elsewhere.
ACTIONLINT ?= actionlint
WORKFLOW_CONTRACT_TESTS ?= \
	tests/test_kani_workflow_contract.py \
	tests/test_kani_workflow_build_contract.py \
	tests/test_kani_workflow_condition_contract.py \
	tests/test_kani_snapshot_producer_purity.py \
	tests/test_python_release_contract.py \
	tests/test_python_release_lifecycle.py \
	tests/test_workflow_contract.py \
	tests/test_workflow_makefile_contract.py

# `all` runs the full test suite, so the contract modules are not repeated in
# its prerequisite list.
actionlint: ## Lint GitHub Actions workflows
	$(call ensure_tool,$(ACTIONLINT))
	$(ACTIONLINT) .github/workflows/*.yml

workflow-check: build actionlint ## Lint workflows and check their contracts
	$(UV_GATE) run -- pytest -q $(WORKFLOW_CONTRACT_TESTS)

# Opt-in: dispatches a real, non-publishing snapshot run. It builds on a hosted
# runner and creates an attestation, so it is never part of `all`.
#
# The producer refuses a tag whose 12-hex prefix is not the upstream commit's,
# but only once a hosted run has started. The tag grammar validated by the
# recipe makes the fifth dash-separated field exactly that prefix, so the
# recipe compares it locally and the operator sees the mistake without a red
# resolve job.
KANI_SNAPSHOT_UPSTREAM_COMMIT ?=
KANI_SNAPSHOT_PUBLICATION_ID ?=
# Exported, never interpolated: each value is data for the recipe's shell,
# not source the shell parses.
export KANI_SNAPSHOT_UPSTREAM_COMMIT KANI_SNAPSHOT_PUBLICATION_ID

kani-snapshot-smoke: ## Dispatch a non-publishing snapshot run (opt-in)
	$(call ensure_tool,gh)
	@test -n "$$KANI_SNAPSHOT_UPSTREAM_COMMIT" || { \
	  printf 'Error: set KANI_SNAPSHOT_UPSTREAM_COMMIT to a full upstream commit\n' >&2; \
	  exit 1; \
	}
	@test -n "$$KANI_SNAPSHOT_PUBLICATION_ID" || { \
	  printf 'Error: set KANI_SNAPSHOT_PUBLICATION_ID to an unused snapshot tag\n' >&2; \
	  exit 1; \
	}
	@printf '%s\n' "$$KANI_SNAPSHOT_UPSTREAM_COMMIT" | grep -Eqx '[0-9a-f]{40}' || { \
	  printf 'Error: KANI_SNAPSHOT_UPSTREAM_COMMIT must be a full 40-hex commit\n' >&2; \
	  exit 1; \
	}
	@printf '%s\n' "$$KANI_SNAPSHOT_PUBLICATION_ID" | \
	  grep -Eqx 'kani-snapshot-[0-9]{4}-(0[1-9]|1[0-2])-[0-9a-f]{12}-r[1-9][0-9]*' || { \
	  printf 'Error: KANI_SNAPSHOT_PUBLICATION_ID must match kani-snapshot-YYYY-MM-<12 hex>-rN\n' >&2; \
	  exit 1; \
	}
	@test "$$(printf '%s\n' "$$KANI_SNAPSHOT_PUBLICATION_ID" | cut -d- -f5)" = \
	      "$$(printf '%s\n' "$$KANI_SNAPSHOT_UPSTREAM_COMMIT" | cut -c1-12)" || { \
	  printf 'Error: KANI_SNAPSHOT_PUBLICATION_ID must carry the upstream commit prefix (12 hex)\n' >&2; \
	  exit 1; \
	}
	gh workflow run kani-monthly.yml --ref main \
	  -f upstream_commit="$$KANI_SNAPSHOT_UPSTREAM_COMMIT" \
	  -f publication_id="$$KANI_SNAPSHOT_PUBLICATION_ID" \
	  -f publish=false

# Opt-in: verifies a produced release through the public consumer commands in a
# fresh virtual environment. It installs the pinned toolchain, runs setup and
# executes real proofs, so it is never part of `all`. The release directory is
# what `gh run download` fetched from a non-publishing run; nothing here
# publishes.
KANI_SNAPSHOT_ASSET_DIR ?=
KANI_SNAPSHOT_WHEEL ?=
KANI_SNAPSHOT_RUSTC_1_93 ?=
# Exported, never interpolated: each value is data for the recipe's shell.
export KANI_SNAPSHOT_ASSET_DIR KANI_SNAPSHOT_WHEEL KANI_SNAPSHOT_RUSTC_1_93

# The throwaway environment is created inside a private `mktemp -d` tree the
# recipe owns, so its cleanup can never name a path the caller chose. `build/`
# is gitignored, and the tree is removed on success and on failure.
KANI_SNAPSHOT_SMOKE_PARENT := $(CURDIR)/build

kani-snapshot-verify: ## Verify a produced snapshot through the consumer path
	$(call ensure_uv)
	@test -n "$$KANI_SNAPSHOT_ASSET_DIR" || { \
	  printf 'Error: set KANI_SNAPSHOT_ASSET_DIR to the downloaded release directory\n' >&2; \
	  exit 1; \
	}
	@test -d "$$KANI_SNAPSHOT_ASSET_DIR" || { \
	  printf 'Error: KANI_SNAPSHOT_ASSET_DIR is not a directory: %s\n' "$$KANI_SNAPSHOT_ASSET_DIR" >&2; \
	  exit 1; \
	}
	@if [ -z "$$KANI_SNAPSHOT_WHEEL" ]; then \
	  printf 'Error: set KANI_SNAPSHOT_WHEEL to a built wheel\n' >&2; \
	  exit 1; \
	fi
	@test -f "$$KANI_SNAPSHOT_WHEEL" || { \
	  printf 'Error: KANI_SNAPSHOT_WHEEL is not a file: %s\n' "$$KANI_SNAPSHOT_WHEEL" >&2; \
	  exit 1; \
	}
	@if [ -n "$$KANI_SNAPSHOT_RUSTC_1_93" ] && [ ! -x "$$KANI_SNAPSHOT_RUSTC_1_93" ]; then \
	  printf 'Error: KANI_SNAPSHOT_RUSTC_1_93 is not executable: %s\n' "$$KANI_SNAPSHOT_RUSTC_1_93" >&2; \
	  exit 1; \
	fi
	@mkdir -p "$(KANI_SNAPSHOT_SMOKE_PARENT)"
	@smoke_dir=$$(mktemp -d "$(KANI_SNAPSHOT_SMOKE_PARENT)/kani-snapshot-smoke.XXXXXX"); \
	trap 'rm -rf "$$smoke_dir"' EXIT INT TERM; \
	$(UV_GATE) run -- uv venv "$$smoke_dir/venv" --python 3.14 && \
	$(UV_GATE) run -- uv pip install --python "$$smoke_dir/venv/bin/python" \
	  "$$KANI_SNAPSHOT_WHEEL" && \
	KANI_SNAPSHOT_SMOKE=1 \
	  KANI_SNAPSHOT_SMOKE_ASSET_DIR="$$KANI_SNAPSHOT_ASSET_DIR" \
	  KANI_SNAPSHOT_SMOKE_PROVER_TOOLS="$$smoke_dir/venv/bin/prover-tools" \
	  KANI_SNAPSHOT_SMOKE_RUSTC_1_93="$$KANI_SNAPSHOT_RUSTC_1_93" \
	  $(UV_GATE) run -- pytest -q tests/integration/test_kani_snapshot_smoke.py

all: build check-fmt lint typecheck test spelling test-workflow-contracts actionlint

define ensure_uv
	@command -v $(UV) >/dev/null 2>&1 || { \
	  printf "Error: uv is required, but '%s' was not found or is not executable\n" "$(UV)" >&2; \
	  exit 1; \
	}
endef

.venv: pyproject.toml prepare
	$(call ensure_uv)

build: .venv prepare ## Build virtual-env and install deps

build-release: prepare ## Build artefacts (sdist & wheel)
	$(call ensure_uv)
	$(UV_GATE) run -- python -m build --sdist --wheel

clean: ## Remove build artefacts
	rm -rf build dist *.egg-info \
	  .mypy_cache .pytest_cache .coverage coverage.* \
	  lcov.info htmlcov .venv .uv-cache .uv-tools
	find . -type d -name '__pycache__' -print0 | xargs -0 -r rm -rf

define ensure_tool
	@command -v $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required, but not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

define ensure_tool_venv
	@$(UV_GATE) run -- which $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required in the virtualenv, but is not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

ifneq ($(strip $(TOOLS)),)
$(TOOLS): ## Verify required CLI tools
	$(call ensure_tool,$@)
endif


ifneq ($(strip $(VENV_TOOLS)),)
.PHONY: $(VENV_TOOLS)
$(VENV_TOOLS): build ## Verify required CLI tools in venv
	$(call ensure_tool_venv,$@)
endif


fmt: build ## Format sources
	$(UV_GATE) run -- ruff format $(PYTHON_TARGETS)
	$(UV_GATE) run -- ruff check --select I --fix $(PYTHON_TARGETS)

	$(MDTABLEFIX) --in-place $(MDTABLEFIX_SELECT) $(MDTABLEFIX_RULES)
	$(MDLINT) --fix "**/*.md"

check-fmt: build ## Verify formatting
	$(UV_GATE) run -- ruff format --check $(PYTHON_TARGETS)

	$(MDTABLEFIX) --check $(MDTABLEFIX_SELECT) $(MDTABLEFIX_RULES)

lint: lint-python ## Run linters

# Docstring presence for the package, its tests and the scripts.
# `scripts/uv_gate.py` is the shared-actions helper, vendored byte for byte, so
# it is not edited here; it ships with complete docstrings, so the gate covers it.
# Ruff checks docstring style for the package and its tests only (`PYTHON_TARGETS`).
INTERROGATE_VERSION ?= 1.7.0
INTERROGATE_TARGETS ?= rust_prover_tools tests scripts
INTERROGATE = $(UV_GATE) tool --from 'interrogate==$(INTERROGATE_VERSION)' -- interrogate
# Level 2 prints a MISSED row per undocumented definition, which the
# `interrogate` target fails on: the rounded percentage alone can pass a lone gap.
INTERROGATE_FLAGS ?= -vv --fail-under 100

# Interrogate rounds its percentage to one decimal, so `--fail-under 100` alone
# can pass a lone gap in a large repository; fail on any MISSED row as well.
interrogate: ## Enforce complete Python docstring coverage
	@report="$$($(INTERROGATE) $(INTERROGATE_FLAGS) $(INTERROGATE_TARGETS))"; \
	  status=$$?; \
	  printf '%s\n' "$$report"; \
	  test "$$status" -eq 0 || exit "$$status"; \
	  if printf '%s\n' "$$report" | grep -q '| *MISSED *|'; then \
	    printf '%s\n' 'interrogate: undocumented definitions remain; see the MISSED rows above' >&2; \
	    exit 1; \
	  fi

lint-python: build interrogate ## Run Python linters
	$(UV_GATE) run -- ruff check $(PYTHON_TARGETS)
	$(PYLINT) $(PYLINT_TARGETS)


typecheck: build ## Run typechecking
	$(UV_GATE) run -- ty --version
	$(UV_GATE) run -- ty check $(PYTHON_TARGETS)

markdownlint: spelling $(MDLINT) ## Lint Markdown files and enforce spelling
	env -u NO_COLOR $(MDLINT) '**/*.md'

spelling: ## Enforce en-GB-oxendict spelling and shared phrase corrections
	$(TYPOS_CONFIG_BUILDER) gate --repository .





nixie: ## Validate Mermaid diagrams
	$(call ensure_tool,$(NIXIE))
	$(NIXIE) --no-sandbox

test: build $(VENV_TOOLS) ## Run tests
	$(UV_GATE) run -- pytest -v -n $(PYTEST_XDIST_WORKERS)


help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":.*##"; printf "Available targets:\n"} {gsub(/^[[:space:]]+/, "", $$2); printf "  %-20s %s\n", $$1, $$2}'
