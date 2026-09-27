PYTHON ?= python3.11

.DEFAULT_GOAL := help
.PHONY: help setup run check check-browser

help:
	@printf '%s\n' \
	  'make setup         Create/reuse .venv (Python >= 3.11) and install requirements' \
	  'make run           Run the app at http://127.0.0.1:8000 with reload' \
	  'make check         Run all offline checks (requires Node.js)' \
	  'make check-browser Run browser checks with Node.js' \
	  'Override creation interpreter: make setup PYTHON=/path/to/python'

setup:
	@if [ ! -e .venv ]; then \
	  "$(PYTHON)" -c 'import sys; sys.exit("Python >= 3.11 is required" if sys.version_info < (3, 11) else 0)' && \
	  "$(PYTHON)" -m venv .venv; \
	fi
	@.venv/bin/python -c 'import sys; sys.exit("Python >= 3.11 is required in .venv" if sys.version_info < (3, 11) else 0)'
	.venv/bin/python -m pip install -r requirements.txt

run:
	.venv/bin/python -m uvicorn app.main:app --reload --port 8000

check:
	@command -v node >/dev/null 2>&1 || { printf '%s\n' 'Node.js is required for make check' >&2; exit 1; }
	.venv/bin/python checks/run_checks.py

check-browser:
	node checks/browser_check.js
