# One command to set up, one to run the demo.
PY ?= python

setup:
	$(PY) -m pip install -e ".[dev]"

demo:
	brain demo

serve:
	brain build && brain serve

check:
	ruff check .
	ruff format --check .
	mypy
	pytest -q
	BRAIN_OFFLINE=1 $(PY) eval/run_eval.py --check

.PHONY: setup demo serve check
