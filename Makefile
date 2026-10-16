PYTHON = python3.11
.DEFAULT_GOAL := help
.PHONY: help setup check
help:
	@printf '%s\n' 'make setup — create/reuse .venv, install pinned dependencies' 'make check — offline Git and HTTP checks, no live inference' 'Review: .venv/bin/python -m review --help'
setup:
	@if [ ! -e .venv ]; then "$(PYTHON)" -m venv .venv; fi
	.venv/bin/python -c 'import sys; assert sys.version_info >= (3, 11), "Python >=3.11 required"'
	.venv/bin/python -m pip install -r requirements.txt
check:
	.venv/bin/python -m checks.core
