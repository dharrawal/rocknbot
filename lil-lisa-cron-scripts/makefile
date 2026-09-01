SHELL = /bin/bash

.EXPORT_ALL_VARIABLES:

setup-env:
	uv venv .venv
	uv sync --extra dev

VENV_PY = .venv/bin/python
VENV_BIN = .venv/bin

test:
	$(VENV_PY) -m unittest discover -s tests -v

run-nightly:
	$(VENV_PY) nightly_pipeline.py

update-env:
	uv sync --extra dev
