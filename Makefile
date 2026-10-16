PYTHON = python3.11
PROJECT = .
.DEFAULT_GOAL := help
.PHONY: help setup run git-mcp check
help:
	@printf '%s\n' 'make setup — create/reuse .venv, install pinned dependencies' 'make run PROJECT=/path/to/repo — console assistant' 'make git-mcp PROJECT=/path/to/repo — independent read-only Git MCP service' 'make check — small offline behavioral checks; no LLM calls'
setup:
	@if [ ! -e .venv ]; then "$(PYTHON)" -m venv .venv; fi
	.venv/bin/python -c 'import sys; assert sys.version_info >= (3, 11), "Python >=3.11 required"'
	.venv/bin/python -m pip install -r requirements.txt
run:
	.venv/bin/python -m assistant --project "$(PROJECT)"
git-mcp:
	.venv/bin/python -m services.git --repo "$(PROJECT)"
check:
	.venv/bin/python -m checks.core
