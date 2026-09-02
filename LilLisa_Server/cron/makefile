SHELL = /bin/bash

.EXPORT_ALL_VARIABLES:

# These jobs share the LilLisa_Server venv one level up: their only extra
# runtime dep (DSPy) is in the server's pyproject, and the API process runs the
# same pipeline in-process via src/techsupport_cron.py.
setup-env:
	cd .. && $(MAKE) setup-env

VENV_PY = ../.venv/bin/python
VENV_BIN = ../.venv/bin

test:
	$(VENV_PY) -m unittest discover -s tests -v

# Manual run only. Nothing schedules this: the API process runs the same
# pipeline on its own tick (src/techsupport_cron.py), so use POST
# /run_nightly_pipeline/ to force a run against a live server. Running this
# while the API is up means two processes and one state file.
run-nightly:
	$(VENV_PY) nightly_pipeline.py

# One-shot ops. historical_import_production.py and backfill_github_urls.py
# stay in this directory; they are not nightly and are never scheduled.
run-historical-import:
	$(VENV_PY) historical_import_production.py

run-backfill-github-urls:
	$(VENV_PY) backfill_github_urls.py

update-env:
	cd .. && $(MAKE) update-env
