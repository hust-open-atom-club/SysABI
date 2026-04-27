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
	@echo "Deprecated aliases (still work, but will be removed):"
	@echo "  run-smoke, run-full, run-asterinas-smoke, run-asterinas-full"
	@echo "  run-tgoskits-starryos-smoke, run-tgoskits-starryos-scale"
	@echo "  run-tgoskits-arceos-smoke, build-eligible, derive-asterinas-scml"

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
# Deprecated aliases
# ---------------------------------------------------------------------------

.PHONY: run-smoke run-full run-asterinas-smoke run-asterinas-full analyze-asterinas report-asterinas build-asterinas derive-asterinas prepare-asterinas-candidate derive-asterinas-scml preflight-asterinas-scml build-eligible run-pipeline

run-smoke:
	@echo "warning: run-smoke is deprecated; use 'make run WORKFLOW=baseline CAMPAIGN=smoke LIMIT=100'" >&2
	$(MAKE) run WORKFLOW=baseline CAMPAIGN=smoke LIMIT=100

run-full:
	@echo "warning: run-full is deprecated; use 'make run WORKFLOW=baseline CAMPAIGN=full LIMIT=1000'" >&2
	$(MAKE) run WORKFLOW=baseline CAMPAIGN=full LIMIT=1000

run-pipeline:
	@echo "warning: run-pipeline is deprecated; use the individual init/derive/build/run commands" >&2
	$(PYTHON) tools/init_layout.py --workflow baseline
	$(PYTHON) tools/init_layout.py --workflow asterinas
	$(MAKE) filter-corpus
	$(MAKE) derive-workflow WORKFLOW=asterinas
	$(MAKE) build-workflow WORKFLOW=asterinas LIMIT=$(RUN_LIMIT) JOBS=$(ASTERINAS_JOBS)
	$(MAKE) run-workflow WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=$(RUN_LIMIT) JOBS=$(ASTERINAS_JOBS)

run-asterinas-smoke:
	@echo "warning: run-asterinas-smoke is deprecated; use 'make run WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=50 JOBS=$(ASTERINAS_JOBS)'" >&2
	$(MAKE) run WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=50 JOBS=$(ASTERINAS_JOBS)

run-asterinas-full:
	@echo "warning: run-asterinas-full is deprecated; use 'make run WORKFLOW=asterinas CAMPAIGN=full LIMIT=200 JOBS=$(ASTERINAS_JOBS)'" >&2
	$(MAKE) run WORKFLOW=asterinas CAMPAIGN=full LIMIT=200 JOBS=$(ASTERINAS_JOBS)

analyze-asterinas:
	@echo "warning: analyze-asterinas is deprecated; use 'make analyze WORKFLOW=asterinas'" >&2
	$(MAKE) analyze WORKFLOW=asterinas

report-asterinas:
	@echo "warning: report-asterinas is deprecated; use 'make report WORKFLOW=asterinas'" >&2
	$(MAKE) report WORKFLOW=asterinas

build-asterinas:
	@echo "warning: build-asterinas is deprecated; use 'make build WORKFLOW=asterinas'" >&2
	$(MAKE) prepare-target WORKFLOW=asterinas
	$(MAKE) build-workflow WORKFLOW=asterinas

derive-asterinas:
	@echo "warning: derive-asterinas is deprecated; use 'make derive WORKFLOW=asterinas'" >&2
	$(MAKE) derive WORKFLOW=asterinas

prepare-asterinas-candidate:
	@echo "warning: prepare-asterinas-candidate is deprecated; use 'make prepare-target WORKFLOW=asterinas'" >&2
	$(MAKE) prepare-target WORKFLOW=asterinas

derive-asterinas-scml:
	@echo "warning: derive-asterinas-scml is deprecated; use 'make derive WORKFLOW=asterinas_scml'" >&2
	$(MAKE) derive-workflow WORKFLOW=asterinas_scml
	$(MAKE) preflight-workflow WORKFLOW=asterinas_scml

preflight-asterinas-scml:
	@echo "warning: preflight-asterinas-scml is deprecated; use 'make preflight-workflow WORKFLOW=asterinas_scml'" >&2
	$(MAKE) preflight-workflow WORKFLOW=asterinas_scml

build-eligible:
	@echo "warning: build-eligible is deprecated; use 'make build WORKFLOW=baseline'" >&2
	$(MAKE) build WORKFLOW=baseline

# ---------------------------------------------------------------------------
# TGOSKits legacy aliases
# ---------------------------------------------------------------------------

.PHONY: preflight-tgoskits-starryos run-tgoskits-starryos-smoke run-tgoskits-starryos-scale preflight-tgoskits-arceos run-tgoskits-arceos-smoke

preflight-tgoskits-starryos:
	@echo "warning: preflight-tgoskits-starryos is deprecated; use 'make prepare-target WORKFLOW=tgoskits_starryos'" >&2
	$(PYTHON) tools/tgoskits_launch.py --workflow tgoskits_starryos preflight

run-tgoskits-starryos-smoke:
	@echo "warning: run-tgoskits-starryos-smoke is deprecated; use 'make run WORKFLOW=tgoskits_starryos CAMPAIGN=smoke'" >&2
	$(PYTHON) tools/tgoskits_launch.py --workflow tgoskits_starryos campaign --campaign smoke $(if $(ELIGIBLE_FILE),--eligible-file $(ELIGIBLE_FILE),) $(if $(LIMIT),--limit $(LIMIT),) --jobs $(or $(JOBS),1)

run-tgoskits-starryos-scale:
	@echo "warning: run-tgoskits-starryos-scale is deprecated; use 'make run WORKFLOW=tgoskits_starryos_scale CAMPAIGN=full'" >&2
	$(PYTHON) tools/tgoskits_launch.py --workflow tgoskits_starryos_scale campaign --campaign full $(if $(ELIGIBLE_FILE),--eligible-file $(ELIGIBLE_FILE),) --limit $(or $(LIMIT),200) --jobs $(or $(JOBS),8)

preflight-tgoskits-arceos:
	@echo "warning: preflight-tgoskits-arceos is deprecated; use 'make prepare-target WORKFLOW=tgoskits_arceos_smoke'" >&2
	$(PYTHON) tools/tgoskits_launch.py --workflow tgoskits_arceos_smoke preflight

run-tgoskits-arceos-smoke:
	@echo "warning: run-tgoskits-arceos-smoke is deprecated; use 'make run WORKFLOW=tgoskits_arceos_smoke CAMPAIGN=smoke'" >&2
	$(PYTHON) tools/tgoskits_launch.py --workflow tgoskits_arceos_smoke campaign --campaign smoke $(if $(ELIGIBLE_FILE),--eligible-file $(ELIGIBLE_FILE),) --limit $(or $(LIMIT),1) --jobs $(or $(JOBS),1)

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

test:
	$(PYTHON) -m unittest discover -s tests -v
