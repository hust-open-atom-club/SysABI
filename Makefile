PYTHON ?= python3
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
ASTERINAS_JOBS ?= 4

.PHONY: bootstrap init-layout generate-corpus import-corpus filter-corpus build-eligible run-smoke run-full analyze report build-asterinas-scml-manifest derive-asterinas-scml preflight-asterinas-scml derive-asterinas prepare-asterinas-candidate build-asterinas run-asterinas-smoke run-asterinas-full analyze-asterinas report-asterinas test clean

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

run-smoke:
	$(PYTHON) orchestrator/scheduler.py --campaign smoke --limit 100

run-full:
	$(PYTHON) orchestrator/scheduler.py --campaign full --limit 1000

analyze:
	$(PYTHON) tools/render_summary.py

report:
	$(PYTHON) tools/reduce_case.py --fixture controlled_divergence

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
	$(PYTHON) tools/prog2c_wrap.py --workflow asterinas

run-asterinas-smoke:
	$(PYTHON) orchestrator/scheduler.py --workflow asterinas --campaign smoke --limit 50 --jobs $(ASTERINAS_JOBS)

run-asterinas-full:
	$(PYTHON) orchestrator/scheduler.py --workflow asterinas --campaign full --limit 200 --jobs $(ASTERINAS_JOBS)

analyze-asterinas:
	$(PYTHON) tools/render_summary.py --workflow asterinas

report-asterinas:
	$(PYTHON) tools/reduce_case.py --workflow asterinas --fixture controlled_divergence

clean:
	$(PYTHON) tools/cleanup_repo_processes.py --repo-root "$(ROOT)" \
		--remove artifacts/runs/asterinas \
		--remove artifacts/runs/asterinas_scml \
		--remove artifacts/sandboxes/asterinas \
		--remove artifacts/preflight/asterinas_scml \
		--remove artifacts/asterinas/build \
		--remove artifacts/asterinas/build-probe \
		--remove artifacts/asterinas/host-target \
		--remove artifacts/asterinas/initramfs-packages \
		--remove build/asterinas/testcases \
		--remove build/asterinas_scml/testcases \
		--remove artifacts/generated/asterinas_scml \
		--remove eligible_programs/asterinas.jsonl \
		--remove eligible_programs/asterinas_scml.targets.jsonl \
		--remove eligible_programs/asterinas_scml.generated.jsonl \
		--remove eligible_programs/asterinas_scml.jsonl \
		--remove eligible_programs/asterinas_scml.static.jsonl \
		--remove reports/asterinas \
		--remove reports/asterinas_scml
