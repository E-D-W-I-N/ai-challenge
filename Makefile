PYTHON ?= python3.11

.DEFAULT_GOAL := help
.PHONY: help setup run check check-browser

help:
	@printf '%s\n' \
	  'make setup         Create/reuse .venv and install requirements (Python >= 3.11)' \
	  '                   Override creation interpreter: make setup PYTHON=/path/to/python' \
	  'make run           Run app at http://127.0.0.1:8000 (after setup)' \
	  'make check         Run all offline checks, including subprocess and browser checks' \
	  'make check-browser Run browser checks only (requires Node.js)'

setup:
	@set -eu; \
	if [ ! -e .venv ] && [ ! -L .venv ]; then \
	  "$(PYTHON)" -c 'import sys; sys.exit("Python >= 3.11 is required") if sys.version_info < (3, 11) else None'; \
	  "$(PYTHON)" -m venv .venv; \
	fi; \
	if [ ! -x .venv/bin/python ]; then \
	  printf '%s\n' 'Existing .venv has no executable bin/python; repair it explicitly.' >&2; \
	  exit 1; \
	fi; \
	.venv/bin/python -c 'import sys; sys.exit("Python >= 3.11 is required in .venv") if sys.version_info < (3, 11) else None'; \
	.venv/bin/python -m pip install -r requirements.txt

run:
	.venv/bin/python -m uvicorn app.main:app --reload --port 8000

check:
	@command -v node >/dev/null 2>&1 || { printf '%s\n' 'Node.js is required for make check.' >&2; exit 1; }
	.venv/bin/python checks/run_checks.py

check-browser:
	node checks/browser_check.js
