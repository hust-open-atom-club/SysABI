PYTHON ?= python3
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
ASTERINAS_JOBS ?= 4
WORKFLOW ?= baseline
CAMPAIGN ?= smoke
FIXTURE ?= controlled_divergence
LIMIT ?=
JOBS ?=
ELIGIBLE_FILE ?=

.PHONY: bootstrap init-layout generate-corpus import-corpus filter-corpus build-eligible run-smoke run-full analyze report build-asterinas-scml-manifest derive-asterinas-scml preflight-asterinas-scml derive-asterinas prepare-asterinas-candidate build-asterinas run-asterinas-smoke run-asterinas-full analyze-asterinas report-asterinas run-workflow analyze-workflow report-workflow build-workflow test clean

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
	$(PYTHON) tools/prog2c_wrap.py --eligible-file eligible_programs/baseline.jsonl

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

derive-asterinas-scml:
	$(PYTHON) tools/build_scml_manifest.py
	$(PYTHON) tools/export_scml_targets.py --workflow asterinas_scml
	$(PYTHON) tools/generate_scml_candidates.py --workflow asterinas_scml
	$(PYTHON) tools/derive_scml_allowed_sequences.py --workflow asterinas_scml
	$(PYTHON) tools/prog2c_wrap.py --workflow asterinas_scml --eligible-file eligible_programs/asterinas_scml.static.jsonl
	$(PYTHON) tools/preflight_scml_gate.py --workflow asterinas_scml

preflight-asterinas-scml:
	$(PYTHON) tools/prog2c_wrap.py --workflow asterinas_scml --eligible-file eligible_programs/asterinas_scml.static.jsonl
	$(PYTHON) tools/preflight_scml_gate.py --workflow asterinas_scml

test:
	$(PYTHON) -m unittest discover -s tests -v

derive-asterinas:
	$(PYTHON) tools/init_layout.py --workflow asterinas
	$(PYTHON) tools/derive_asterinas_corpus.py --workflow asterinas

prepare-asterinas-candidate:
	SYZABI_WORKFLOW=asterinas $(PYTHON) tools/run_asterinas.py --mode docker-qemu --healthcheck

build-asterinas:
	$(MAKE) build-workflow WORKFLOW=asterinas

run-asterinas-smoke:
	$(MAKE) run-workflow WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=50 JOBS=$(ASTERINAS_JOBS)

run-asterinas-full:
	$(MAKE) run-workflow WORKFLOW=asterinas CAMPAIGN=full LIMIT=200 JOBS=$(ASTERINAS_JOBS)

analyze-asterinas:
	$(MAKE) analyze-workflow WORKFLOW=asterinas

report-asterinas:
	$(MAKE) report-workflow WORKFLOW=asterinas FIXTURE=controlled_divergence

clean:
	$(PYTHON) tools/cleanup_repo_processes.py --repo-root "$(ROOT)" \
		--remove artifacts/runs/asterinas \
		--remove artifacts/runs/asterinas_scml \
		--remove artifacts/runs/targets/linux/baseline \
		--remove artifacts/runs/targets/asterinas/asterinas \
		--remove artifacts/runs/targets/asterinas/asterinas_scml \
		--remove artifacts/sandboxes/asterinas \
		--remove artifacts/preflight/asterinas_scml \
		--remove artifacts/asterinas/build \
		--remove artifacts/asterinas/build-probe \
		--remove artifacts/asterinas/host-target \
		--remove artifacts/asterinas/initramfs-packages \
		--remove artifacts/targets/asterinas/initramfs-packages \
		--remove build/asterinas/testcases \
		--remove build/asterinas_scml/testcases \
		--remove build/targets/linux/baseline/testcases \
		--remove build/targets/asterinas/asterinas/testcases \
		--remove build/targets/asterinas/asterinas_scml/testcases \
		--remove artifacts/generated/asterinas_scml \
		--remove eligible_programs/asterinas.jsonl \
		--remove eligible_programs/asterinas_scml.targets.jsonl \
		--remove eligible_programs/asterinas_scml.generated.jsonl \
		--remove eligible_programs/asterinas_scml.jsonl \
		--remove eligible_programs/asterinas_scml.static.jsonl \
		--remove reports/asterinas \
		--remove reports/asterinas_scml \
		--remove reports/targets/linux/baseline \
		--remove reports/targets/asterinas/asterinas \
		--remove reports/targets/asterinas/asterinas_scml
