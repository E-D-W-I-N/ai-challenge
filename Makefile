PYTHON = python3.11
.DEFAULT_GOAL := help
.PHONY: help setup run check
help:
	@printf '%s\n' 'make setup — create/reuse .venv, install pinned dependencies' 'make run PROJECT=/path/to/project — interactive file assistant' 'make check — compact offline file-agent and HTTP checks'
setup:
	@if [ ! -e .venv ]; then "$(PYTHON)" -m venv .venv; fi
	.venv/bin/python -c 'import sys; assert sys.version_info >= (3, 11), "Python >=3.11 required"'
	.venv/bin/python -m pip install -r requirements.txt
run:
	.venv/bin/python -m file_agent --project "$(PROJECT)"
check:
	.venv/bin/python -m checks.core
