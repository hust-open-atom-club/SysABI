PYTHON ?= python3
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: bootstrap init-layout generate-corpus import-corpus filter-corpus build-eligible run-smoke run-full analyze report test

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
	$(PYTHON) tools/prog2c_wrap.py --eligible-file eligible_programs/phase1.jsonl

run-smoke:
	$(PYTHON) orchestrator/scheduler.py --campaign smoke --limit 100

run-full:
	$(PYTHON) orchestrator/scheduler.py --campaign full --limit 1000

analyze:
	$(PYTHON) tools/render_summary.py

report:
	$(PYTHON) tools/reduce_case.py --fixture controlled_divergence

test:
	$(PYTHON) -m unittest discover -s tests -v
