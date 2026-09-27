PYTHON ?= python3.11
.DEFAULT_GOAL := help

.PHONY: help setup run check check-browser

help:
	@printf '%s\n' \
	  'make setup         Create/reuse .venv and install requirements (Python >= 3.11)' \
	  '                   Override creation interpreter: make setup PYTHON=/path/to/python' \
	  'make run           Serve app at http://127.0.0.1:8000 with reload' \
	  'make check         Full checks, including subprocess and browser checks (requires Node.js)' \
	  'make check-browser Browser checks only (requires Node.js)'

setup:
	@if [ ! -e .venv ] && [ ! -L .venv ]; then \
	  "$(PYTHON)" -c 'import sys; sys.exit("Python >= 3.11 is required" if sys.version_info < (3, 11) else 0)' && \
	  "$(PYTHON)" -m venv .venv; \
	elif [ ! -x .venv/bin/python ]; then \
	  printf '%s\n' 'Existing .venv has no executable bin/python; it was left unchanged.' >&2; \
	  exit 1; \
	fi
	@.venv/bin/python -c 'import sys; sys.exit("Existing .venv requires Python >= 3.11; it was left unchanged" if sys.version_info < (3, 11) else 0)'
	.venv/bin/python -m pip install -r requirements.txt

run:
	.venv/bin/python -m uvicorn app.main:app --reload --port 8000

check:
	@command -v node >/dev/null 2>&1 || { printf '%s\n' 'Node.js is required; install node and add it to PATH.' >&2; exit 1; }
	.venv/bin/python checks/run_checks.py

check-browser:
	node checks/browser_check.js
