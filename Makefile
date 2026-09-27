PYTHON ?= python3.11

.PHONY: help setup run check check-browser
.DEFAULT_GOAL := help

help:
	@printf '%s\n' 'make setup         Create/reuse .venv and install requirements (Python >=3.11)' '                   Override: make setup PYTHON=/path/to/python' 'make run           Start http://127.0.0.1:8000 with reload' 'make check         Run all offline checks (requires Node.js)' 'make check-browser Run browser checks under Node.js'

setup:
	@if [ ! -e .venv ] && [ ! -L .venv ]; then \
		"$(PYTHON)" -c 'import sys; sys.exit("Python >=3.11 is required for setup" if sys.version_info < (3, 11) else 0)' && \
		"$(PYTHON)" -m venv .venv; \
	fi
	@.venv/bin/python -c 'import sys; sys.exit("Existing .venv requires Python >=3.11; choose a suitable environment" if sys.version_info < (3, 11) else 0)'
	.venv/bin/python -m pip install -r requirements.txt

run:
	.venv/bin/python -m uvicorn app.main:app --reload --port 8000

check:
	@command -v node >/dev/null 2>&1 || { printf '%s\n' 'Node.js is required: install node and retry make check' >&2; exit 1; }
	.venv/bin/python checks/run_checks.py

check-browser:
	@command -v node >/dev/null 2>&1 || { printf '%s\n' 'Node.js is required: install node and retry make check-browser' >&2; exit 1; }
	node checks/browser_check.js
