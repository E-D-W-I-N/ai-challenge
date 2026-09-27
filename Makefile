PYTHON ?= python3.11

.DEFAULT_GOAL := help
.PHONY: help setup run check check-browser

help:
	@printf '%s\n' \
	  'make setup         Create/reuse .venv and install requirements (Python >= 3.11)' \
	  'make run           Start app at http://127.0.0.1:8000' \
	  'make check         Run all checks once, including subprocess and browser checks' \
	  'make check-browser Run browser checks with Node.js' \
	  'Creation override: make setup PYTHON=/path/to/python (default: python3.11)' \
	  'Existing .venv is reused; .env is never created or overwritten.'

setup:
	@if [ -e .venv ] || [ -L .venv ]; then \
	  test -x .venv/bin/python || { echo 'Existing .venv has no executable Python; repair it explicitly.' >&2; exit 1; }; \
	else \
	  "$(PYTHON)" -c 'import sys; sys.exit("Python >= 3.11 required" if sys.version_info < (3, 11) else 0)' || exit $$?; \
	  "$(PYTHON)" -m venv .venv || exit $$?; \
	fi
	@.venv/bin/python -c 'import sys; sys.exit("Python >= 3.11 required in .venv" if sys.version_info < (3, 11) else 0)'
	.venv/bin/python -m pip install -r requirements.txt

run:
	.venv/bin/python -m uvicorn app.main:app --reload --port 8000

check:
	@command -v node >/dev/null 2>&1 || { echo 'Node.js is required for make check.' >&2; exit 1; }
	.venv/bin/python checks/run_checks.py

check-browser:
	@command -v node >/dev/null 2>&1 || { echo 'Node.js is required for make check-browser.' >&2; exit 1; }
	node checks/browser_check.js
