PYTHON ?= python3
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
ASTERINAS_JOBS ?= 4
WORKFLOW ?= baseline
CAMPAIGN ?= smoke
FIXTURE ?= controlled_divergence
LIMIT ?=
JOBS ?=
ELIGIBLE_FILE ?=

.PHONY: bootstrap init-layout generate-corpus import-corpus filter-corpus build-eligible run-smoke run-full analyze report build-asterinas-scml-manifest derive-asterinas-scml preflight-asterinas-scml derive-asterinas prepare-asterinas-candidate build-asterinas run-asterinas-smoke run-asterinas-full analyze-asterinas report-asterinas run-workflow analyze-workflow report-workflow build-workflow derive-workflow preflight-workflow prepare-target test clean

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

build-eligible:
	@echo "warning: build-eligible is deprecated; use build-workflow WORKFLOW=baseline" >&2
	$(MAKE) build-workflow WORKFLOW=baseline

build-workflow:
	$(PYTHON) tools/prog2c_wrap.py --workflow $(WORKFLOW) $(if $(ELIGIBLE_FILE),--eligible-file $(ELIGIBLE_FILE),)

run-workflow:
	$(PYTHON) orchestrator/scheduler.py --workflow $(WORKFLOW) --campaign $(CAMPAIGN) $(if $(LIMIT),--limit $(LIMIT),) $(if $(JOBS),--jobs $(JOBS),)

analyze-workflow:
	$(PYTHON) tools/render_summary.py --workflow $(WORKFLOW)

report-workflow:
	$(PYTHON) tools/reduce_case.py --workflow $(WORKFLOW) --fixture $(FIXTURE)

run-smoke:
	$(MAKE) run-workflow WORKFLOW=baseline CAMPAIGN=smoke LIMIT=100

run-full:
	$(MAKE) run-workflow WORKFLOW=baseline CAMPAIGN=full LIMIT=1000

analyze:
	$(MAKE) analyze-workflow WORKFLOW=baseline

report:
	$(MAKE) report-workflow WORKFLOW=baseline FIXTURE=controlled_divergence

build-asterinas-scml-manifest:
	$(PYTHON) tools/build_scml_manifest.py

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
	@TARGET_NAME="$$( $(PYTHON) tools/workflow_path.py --workflow $(WORKFLOW) --key target )"; \
	if [ "$$TARGET_NAME" = "asterinas" ]; then \
		SYZABI_WORKFLOW=$(WORKFLOW) $(PYTHON) targets/asterinas/entrypoint.py --mode docker-qemu --healthcheck; \
	else \
		echo "prepare-target unsupported for target=$$TARGET_NAME workflow=$(WORKFLOW)" >&2; exit 1; \
	fi

derive-asterinas-scml:
	@echo "warning: derive-asterinas-scml is deprecated; use derive-workflow/preflight-workflow WORKFLOW=asterinas_scml" >&2
	$(MAKE) derive-workflow WORKFLOW=asterinas_scml
	$(MAKE) preflight-workflow WORKFLOW=asterinas_scml

preflight-asterinas-scml:
	@echo "warning: preflight-asterinas-scml is deprecated; use preflight-workflow WORKFLOW=asterinas_scml" >&2
	$(MAKE) preflight-workflow WORKFLOW=asterinas_scml

test:
	$(PYTHON) -m unittest discover -s tests -v

derive-asterinas:
	@echo "warning: derive-asterinas is deprecated; use derive-workflow WORKFLOW=asterinas" >&2
	$(MAKE) derive-workflow WORKFLOW=asterinas

prepare-asterinas-candidate:
	@echo "warning: prepare-asterinas-candidate is deprecated; use prepare-target WORKFLOW=asterinas" >&2
	$(MAKE) prepare-target WORKFLOW=asterinas

build-asterinas:
	@echo "warning: build-asterinas is deprecated; use build-workflow WORKFLOW=asterinas" >&2
	$(MAKE) build-workflow WORKFLOW=asterinas

run-asterinas-smoke:
	@echo "warning: run-asterinas-smoke is deprecated; use run-workflow WORKFLOW=asterinas CAMPAIGN=smoke" >&2
	$(MAKE) run-workflow WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=50 JOBS=$(ASTERINAS_JOBS)

run-asterinas-full:
	@echo "warning: run-asterinas-full is deprecated; use run-workflow WORKFLOW=asterinas CAMPAIGN=full" >&2
	$(MAKE) run-workflow WORKFLOW=asterinas CAMPAIGN=full LIMIT=200 JOBS=$(ASTERINAS_JOBS)

analyze-asterinas:
	@echo "warning: analyze-asterinas is deprecated; use analyze-workflow WORKFLOW=asterinas" >&2
	$(MAKE) analyze-workflow WORKFLOW=asterinas

report-asterinas:
	@echo "warning: report-asterinas is deprecated; use report-workflow WORKFLOW=asterinas" >&2
	$(MAKE) report-workflow WORKFLOW=asterinas FIXTURE=controlled_divergence

clean:
	$(PYTHON) tools/cleanup_repo_processes.py --repo-root "$(ROOT)"
