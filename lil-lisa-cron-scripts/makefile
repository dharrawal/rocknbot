SHELL = /bin/bash

.EXPORT_ALL_VARIABLES:

setup-env:
	uv venv .venv
	uv sync --extra dev

VENV_PY = .venv/bin/python
VENV_BIN = .venv/bin

test:
	$(VENV_PY) -m unittest discover -s tests -v

# Cron: run-nightly is the ONLY cron target. Schedule this (or
# .venv/bin/python nightly_pipeline.py) and nothing else.
run-nightly:
	$(VENV_PY) nightly_pipeline.py

# Optional one-shot ops — never cron. historical_import_production.py and
# backfill_github_urls.py stay in this directory; they are not nightly.
run-historical-import:
	$(VENV_PY) historical_import_production.py

run-backfill-github-urls:
	$(VENV_PY) backfill_github_urls.py

update-env:
	uv sync --extra dev
