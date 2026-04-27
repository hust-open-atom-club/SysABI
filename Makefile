PYTHON ?= python3
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
WORKFLOW ?= baseline
TARGET ?=
CAMPAIGN ?= smoke
FIXTURE ?= controlled_divergence
LIMIT ?=
JOBS ?=
ELIGIBLE_FILE ?=
ASTERINAS_JOBS ?= 4
RUN_LIMIT ?= 100

# Unified commands
.PHONY: help init derive build run analyze report clean

help:
	@echo "SysABI - Unified Command Interface"
	@echo ""
	@echo "Usage: make <command> [VARIABLE=value ...]"
	@echo ""
	@echo "Commands:"
	@echo "  init     - Initialize layout for WORKFLOW (default: baseline)"
	@echo "  derive   - Derive corpus/programs for WORKFLOW"
	@echo "  build    - Build eligible programs for WORKFLOW"
	@echo "  run      - Run campaign for WORKFLOW and CAMPAIGN"
	@echo "  analyze  - Analyze campaign results for WORKFLOW"
	@echo "  report   - Generate divergence report for WORKFLOW"
	@echo "  clean    - Clean up processes and temporary files"
	@echo "  test     - Run test suite"
	@echo ""
	@echo "Variables:"
	@echo "  WORKFLOW=<workflow>     Target workflow (baseline, asterinas, tgoskits_starryos, ...)"
	@echo "  TARGET=<target>         Target OS (optional, inferred from WORKFLOW)"
	@echo "  CAMPAIGN=<campaign>     Campaign name (smoke, full, default: smoke)"
	@echo "  LIMIT=<n>               Max cases to process"
	@echo "  JOBS=<n>                Concurrent worker threads"
	@echo "  FIXTURE=<fixture>       Divergence fixture for report"
	@echo ""
	@echo "Examples:"
	@echo "  make run WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=50 JOBS=4"
	@echo "  make run WORKFLOW=tgoskits_starryos CAMPAIGN=smoke LIMIT=20 JOBS=4"
	@echo "  make run WORKFLOW=tgoskits_arceos_smoke CAMPAIGN=smoke LIMIT=10 JOBS=2"
	@echo ""


init:
	$(PYTHON) tools/init_layout.py --workflow $(WORKFLOW)

derive:
	$(MAKE) derive-workflow WORKFLOW=$(WORKFLOW)

build:
	$(MAKE) build-workflow WORKFLOW=$(WORKFLOW) $(if $(LIMIT),LIMIT=$(LIMIT),) $(if $(JOBS),JOBS=$(JOBS),)

run:
	$(MAKE) run-workflow WORKFLOW=$(WORKFLOW) CAMPAIGN=$(CAMPAIGN) $(if $(LIMIT),LIMIT=$(LIMIT),) $(if $(JOBS),JOBS=$(JOBS),)

analyze:
	$(MAKE) analyze-workflow WORKFLOW=$(WORKFLOW)

report:
	$(MAKE) report-workflow WORKFLOW=$(WORKFLOW) $(if $(FIXTURE),FIXTURE=$(FIXTURE),)

clean:
	$(PYTHON) tools/cleanup_repo_processes.py --repo-root "$(ROOT)"

# ---------------------------------------------------------------------------
# Low-level workflow targets (used by unified commands above)
# ---------------------------------------------------------------------------

.PHONY: bootstrap init-layout generate-corpus import-corpus filter-corpus build-workflow run-workflow analyze-workflow report-workflow derive-workflow preflight-workflow prepare-target

bootstrap:
	./tools/bootstrap_syzkaller.sh

init-layout:
	$(PYTHON) tools/init_layout.py

generate-corpus:
	$(PYTHON) tools/generate_corpus.py --count 1000 --output-dir corpus/input/generated

import-corpus:
	$(PYTHON) tools/import_syz.py --input-dir corpus/input/generated --source-type generated

filter-corpus:
	$(PYTHON) tools/filter_corpus.py

build-workflow:
	$(PYTHON) tools/prog2c_wrap.py --workflow $(WORKFLOW) $(if $(ELIGIBLE_FILE),--eligible-file $(ELIGIBLE_FILE),) $(if $(LIMIT),--limit $(LIMIT),) $(if $(JOBS),--jobs $(JOBS),)

run-workflow:
	$(MAKE) prepare-target WORKFLOW=$(WORKFLOW)
	$(PYTHON) orchestrator/scheduler.py --workflow $(WORKFLOW) --campaign $(CAMPAIGN) $(if $(LIMIT),--limit $(LIMIT),) $(if $(JOBS),--jobs $(JOBS),)

analyze-workflow:
	$(PYTHON) tools/render_summary.py --workflow $(WORKFLOW)

report-workflow:
	$(PYTHON) tools/reduce_case.py --workflow $(WORKFLOW) --fixture $(FIXTURE)

derive-workflow:
	@TARGET_NAME="$$( $(PYTHON) tools/workflow_path.py --workflow $(WORKFLOW) --key target )"; \
	SUPPORTS_PREFLIGHT="$$( $(PYTHON) tools/workflow_path.py --workflow $(WORKFLOW) --key capabilities.supports_preflight )"; \
	if [ "$$SUPPORTS_PREFLIGHT" = "true" ]; then \
		$(PYTHON) tools/build_scml_manifest.py; \
		$(PYTHON) tools/export_scml_targets.py --workflow $(WORKFLOW); \
		$(PYTHON) tools/generate_scml_candidates.py --workflow $(WORKFLOW); \
		$(PYTHON) tools/derive_scml_allowed_sequences.py --workflow $(WORKFLOW); \
	elif [ "$$TARGET_NAME" = "asterinas" ]; then \
		$(PYTHON) tools/init_layout.py --workflow $(WORKFLOW); \
		$(PYTHON) tools/derive_asterinas_corpus.py --workflow $(WORKFLOW); \
	else \
		echo "derive-workflow unsupported for target=$$TARGET_NAME workflow=$(WORKFLOW)" >&2; exit 1; \
	fi

preflight-workflow:
	@SUPPORTS_PREFLIGHT="$$( $(PYTHON) tools/workflow_path.py --workflow $(WORKFLOW) --key capabilities.supports_preflight )"; \
	if [ "$$SUPPORTS_PREFLIGHT" = "true" ]; then \
		ELIGIBLE_FILE="$$( $(PYTHON) tools/workflow_path.py --workflow $(WORKFLOW) --key preflight.source_eligible_file )"; \
		$(PYTHON) tools/prog2c_wrap.py --workflow $(WORKFLOW) --eligible-file "$$ELIGIBLE_FILE"; \
		$(PYTHON) tools/preflight_scml_gate.py --workflow $(WORKFLOW); \
	else \
		echo "preflight-workflow unsupported for workflow=$(WORKFLOW)" >&2; exit 1; \
	fi

prepare-target:
	@TARGET_MODE="$$( $(PYTHON) tools/workflow_path.py --workflow $(WORKFLOW) --key target_config.default_mode 2>/dev/null || true )"; \
	if [ -n "$$TARGET_MODE" ]; then \
		SYZABI_WORKFLOW=$(WORKFLOW) $(PYTHON) targets/entrypoint.py --mode "$$TARGET_MODE" --healthcheck; \
	else \
		SYZABI_WORKFLOW=$(WORKFLOW) $(PYTHON) targets/entrypoint.py --healthcheck; \
	fi


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

test:
	$(PYTHON) -m unittest discover -s tests -v
