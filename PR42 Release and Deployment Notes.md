# Deployment Documentation: Tech Support Integration

## 1\. Overview

This adds two things to Rocknbot. First, a real-time escalation flow that lets Lil Lisa post unanswered questions to a tech support channel with one click. Second, a nightly pipeline that automatically turns resolved tech support conversations into verified answers the bots can use going forward, including merging new insight into an existing answer instead of always creating a duplicate. Nothing here requires a new service. It's an extension of `LilLisa_Server` and `lil-lisa`, plus a set of standalone scripts triggered on a schedule.

## 2\. New Environment Variables

### `LilLisa_Server/env/lillisa_server.env`

| Variable | Default | Purpose |
| :---- | :---- | :---- |
| `TECHSUPPORT_SYNC_INTERVAL_HOURS` | `24` | How often the nightly pipeline checks the tech support channel for new or updated threads. Expert-adjustable. |
| `TECHSUPPORT_SYNC_HOT_DAYS` | `30` | Nightly, `nightly_techsupport_sync.py` only refreshes known threads whose last reply activity is within this many days. Uses a cheap parent `latest_reply` lookup (`conversations.history`, not a full `conversations.replies` download). |
| `TECHSUPPORT_SYNC_CATCHUP_AGE_DAYS` | `90` | Age cap for the periodic catch-up: known threads quieter than the hot window but still within this many days are checked on catch-up runs. Threads older than this stay in state but are not polled. |
| `TECHSUPPORT_SYNC_CATCHUP_INTERVAL_DAYS` | `7` | How often the catch-up pass runs (stamped in `techsupport_sync_state.json` as `last_catchup_timestamp`). Independent of `TECHSUPPORT_SYNC_INTERVAL_HOURS`. |
| `TECHSUPPORT_SYNC_MAX_PARENT_LOOKUPS` | `200` | Cap on each of the nightly hot set and the catch-up set (hottest first). Excess waits for a later run. |
| `TECHSUPPORT_REEMBED_INTERVAL_DAYS` | `7` | How often the verified techsupport table gets a full contextual re-embed (late-chunking refresh). Expert-adjustable. |
| `VERIFIED_TECHSUPPORT_QA_FOLDERPATH` | `data/verified_techsupport/` | Where the verified techsupport markdown file lives locally, before it gets pushed to the GitHub repo (see Section 7). |
| `TECHSUPPORT_PIPELINE_TICK_MINUTES` | `60` | How often the API process checks whether the nightly pipeline is due (Section 5). Not the nightly schedule itself — real work is gated by `TECHSUPPORT_SYNC_INTERVAL_HOURS`, so this only needs to be more frequent than it. |

### `LilLisa_Server/cron/env/techsupport_sync.env` (dedicated file)

| Variable | Purpose |
| :---- | :---- |
| `SLACK_BOT_TOKEN` | Bot token used by the standalone nightly scripts to read the tech support channel. Same bot as `lil-lisa`'s. |
| `TECHSUPPORT_CHANNEL_ID` | The real tech support channel ID. **Must equal** `lil-lisa`'s `TECHSUPPORT_CHANNEL_ID_IDA` / `_IDDM` / `_IDO` (those three must all be the same ID; the bot raises at startup if they disagree). If you also copy the product-specific names into this file, the pipeline refuses to start when they differ from `TECHSUPPORT_CHANNEL_ID`. |
| `ADMIN_CHANNEL_ID` | Where pipeline error notifications get posted. |

This is a separate env file from `lil-lisa`'s on purpose, so the nightly scripts can run standalone (for example as their own cron job) without depending on `lil-lisa`'s directory or config existing.

### `LilLisa_Server/cron/env/github_push.env` (dedicated file, new)

| Variable | Purpose |
| :---- | :---- |
| `GITHUB_TOKEN` | Personal access token used to push the verified techsupport markdown file to its dedicated GitHub repo after every nightly update. See Section 7a for PAT scopes and how auth is passed (GIT_ASKPASS; token must not be in the URL). |
| `GITHUB_REPO_URL` | HTTPS URL of that repo with **no** embedded credentials (currently `sgodey8/rocknbot_techsupport_qa_pairs`, see Section 7). |

### `lil-lisa/app_envfiles/lil-lisa.env` (additions to the existing file)

| Variable | Purpose |
| :---- | :---- |
| `TECHSUPPORT_CHANNEL_ID_IDA` | Tech support channel for IDA. |
| `TECHSUPPORT_CHANNEL_ID_IDDM` | Tech support channel for IDDM. |
| `TECHSUPPORT_CHANNEL_ID_IDO` | Tech support channel for IDO (optional product). |

In production these must all point to the **same** real channel (one shared tech support channel, not one per product). The bot asserts that at startup. Cron watches only `TECHSUPPORT_CHANNEL_ID` in `techsupport_sync.env`; set that to the same ID.

**Important existing variable:** `MAX_LENGTH`: must be 3000 or less, since that's Slack's hard limit on message length. A backup check also exists in case this is ever misconfigured.

## 3\. New Files and Directories

- `LilLisa_Server/data/verified_techsupport/techsupport_qa_pairs.md`. The verified techsupport Q\&A source file (markdown). Auto-created and appended by the pipeline, and auto-pushed to GitHub (Section 7\) after every update.  
- `LilLisa_Server/cron/`. Nightly pipeline Python jobs (see Section 4), shipped in the API image and run by the API process itself — see Section 5. It has no `pyproject.toml` of its own: DSPy is in the server's dependencies and everything shares `LilLisa_Server/.venv`.  
- `LilLisa_Server/src/techsupport_cron.py`. Adapter that lets the API process run `nightly_pipeline.run_pipeline()`: resolves the cron package, serialises overlapping runs, and provides the optional periodic tick.  
- `LilLisa_Server/.dockerignore`. Keeps virtualenvs and generated state out of the build context (it was several GB before this existed).  
- `LilLisa_Server/cron/env/techsupport_sync.env` and `LilLisa_Server/cron/env/github_push.env`. See Section 2\. Both need to be gitignored (already confirmed).  
- State files, auto-created and gitignored, safe to delete if you want a clean slate since the pipeline will just re-detect everything as new on the next run:  
  - `LilLisa_Server/cron/techsupport_sync_state.json`  
  - `LilLisa_Server/cron/techsupport_reembed_state.json`  
  - `LilLisa_Server/cron/techsupport_review_state.json`  
  - `LilLisa_Server/scripts/techsupport_thread_tags.json`, written by the running API (`POST /tag_techsupport_thread/`). Cron only reads this file; see Section 4a.

## 4\. Scripts

| Script | When to run | Purpose |
| :---- | :---- | :---- |
| `nightly_pipeline.py` | **Nightly, automatic.** Run by the API process (Section 5); the **only** script that should ever be scheduled. | The main entry point. Orchestrates everything below, including the GitHub push and the live server's index reload. |
| `nightly_techsupport_sync.py` | Nightly, via `nightly_pipeline.py` | Detects new or updated threads in the tech support channel. New parents come from `conversations.history` since last run. Known threads are not all polled: nightly hot window + periodic 90-day catch-up, both capped (see the `TECHSUPPORT_SYNC_*` knobs in Section 2). |
| `techsupport_classifier.py` | Nightly, via `nightly_pipeline.py` | Classifies whether a thread is useful and conclusive (DSPy-based |
| `techsupport_qa_ingest.py` | Nightly, via `nightly_pipeline.py` | Extracts a summary from a resolved thread and adds it to the markdown file plus LanceDB, or merges it into an existing entry (see Section 4a). |
| `techsupport_contextual_reembed.py` | Nightly, via `nightly_pipeline.py` (on its own interval) | Periodically re-embeds the whole verified table together (late-chunking), on the interval from Section 2\. |
| `techsupport_review_sync.py` | Nightly, via `nightly_pipeline.py` | Picks up manual edits an expert might make directly to the markdown file and syncs them into LanceDB. Fully optional, not a gate. |
| `techsupport_rollback.py` | Manual (ops) | `list_available_versions()` and `rollback_to_version(n)`, see Section 8\. |
| `github_sync.py` | Nightly, via `nightly_pipeline.py` (or standalone retry) | Pushes the current markdown file to the dedicated GitHub repo. Skips the push if the file hasn't actually changed. Called automatically by `nightly_pipeline.py`, but can also be run standalone if a push needs to be retried manually. |
| `github_anchor.py` | Library / helper | Generates GitHub-accurate anchor slugs from entry titles, so answers can link directly to the right section of the file on GitHub. |
| `historical_import_production.py` | **One-shot.** Never cron. Optional: `make run-historical-import`. | One-time bulk import of `data/historical_import/production_1year.txt` into the same verified-techsupport store the nightly pipeline maintains. Resumable; not part of the default schedule. |
| `backfill_github_urls.py` | **One-shot.** Never cron. Optional: `make run-backfill-github-urls`. | One-time metadata patch: adds `github_url` on existing LanceDB rows that predate the GitHub-anchor feature. New nightly inserts already get this; do not schedule this script. |

`nightly_pipeline.py` is the only script that runs on a schedule. Leave `historical_import_production.py` and `backfill_github_urls.py` in `LilLisa_Server/cron/` (do not move them into a one-shot folder); they are invoked by hand only.

### 4a. Merge/Enrich: avoiding duplicate entries

If Lil Lisa answers a question by citing an existing verified techsupport entry, and the user escalates anyway, any genuinely new insight added in that escalation gets merged into the existing entry instead of creating a near-duplicate one. Here's how that works:

1. When an answer's top source is an existing techsupport entry, that connection is noted in the response.  
2. If the user escalates, this gets recorded server-side through a new endpoint, `/tag_techsupport_thread/` on `LilLisa_Server`, following the same pattern as `/record_endorsement/`, and tracked in `techsupport_thread_tags.json`.  
3. When the nightly pipeline later processes that escalation thread, if it's tagged this way, it uses a lighter classification bar. It just needs to be useful, not necessarily fully conclusive on its own, since the point isn't to independently resolve a new question but to add supplementary insight to something already resolved.  
4. If it passes, the existing entry's content gets updated through an LLM merge call, but its title never changes. That's what keeps the entry's GitHub link stable even as its content grows over time.

### 4b. Embedding space until weekly reembed (ops)

Nightly `add` / `replace` / `enrich` embed each new row with Voyage `input_type="query"` (the same helper retrieval uses). `techsupport_contextual_reembed.py` (default every `TECHSUPPORT_REEMBED_INTERVAL_DAYS` days) re-embeds the **whole** `techsupport_qa_pairs.md` with contextual `input_type="document"`.

Until that weekly job runs, **new rows live in a different vector space** than rows last written by reembed. They are still searchable against user queries (query-to-query), but they may rank oddly next to older document-space rows. If a brand-new verified answer seems missing or weak in Slack, wait for the next reembed (or run `techsupport_contextual_reembed.py` once). Matching insert embeddings to the weekly job is follow-on work (`pr42-enhancements.3`).

## 5\. Nightly Pipeline Scheduling

**There is no crontab.** The API process schedules and runs the nightly pipeline itself. Nothing to install on a host, no second container, no external scheduler — deploying the API image is the whole setup.

The three API dockerfiles `COPY cron /app/cron`, and `LilLisa_Server/pyproject.toml` includes DSPy. `src/techsupport_cron.py` puts that package on `sys.path` and runs the same `run_pipeline()` in-process, and `main.lifespan` starts the tick at boot.

Because `cron/` lives inside the server tree, `paths.py` resolves the server root as its own parent — `/app/cron` → `/app` in the image, and the checkout path on a dev box. No env var is needed for either.

**Build context.** Unchanged (`LilLisa_Server/`), but a new `LilLisa_Server/.dockerignore` is now required: it keeps `.venv`, `speedict/`, and generated state out of the build, which took the context from ~7.3 GB down to ~179 MB (almost all of the remainder is `lancedb/`, deliberately kept because `dockerfile_lancedb` copies it).

### 5a. How the schedule works

The tick fires every `TECHSUPPORT_PIPELINE_TICK_MINUTES` (default 60), starting one interval after boot so it never competes with startup index rebuilds.

That tick is **not** the nightly schedule. Each run calls `is_channel_check_due()`, which does the real gating against `TECHSUPPORT_SYNC_INTERVAL_HOURS` (default 24) using `last_run_timestamp` in `techsupport_sync_state.json`. So an hourly tick still produces one real run per day, ticks that are not due no-op cheaply, and a container restart self-heals on the next tick instead of missing the window. To change the actual cadence, adjust `TECHSUPPORT_SYNC_INTERVAL_HOURS`, not the tick.

If the cron package is somehow missing from the image, the server logs an error at startup and keeps serving; only the nightly work is skipped.

### 5b. Forcing a run by hand

`POST /run_nightly_pipeline/` runs the pipeline immediately, for ops work and post-deploy verification. It uses the same `encrypted_key` JWT as the other admin routes:

```
curl -X POST "https://your-api/run_nightly_pipeline/?encrypted_key=$ENCRYPTED_KEY"
```

`ENCRYPTED_KEY` is the same JWT `lil-lisa` sends: `jwt.encode({"some": "payload"}, AUTHENTICATION_KEY, algorithm="HS256")`.

It returns immediately and runs as a FastAPI background task; progress and the run summary go to the server log. Runs are serialised — if the tick is already running the pipeline, the call is ignored rather than queued, and vice versa. Returns 503 if the image was built without the cron package.

For a one-off run outside the API (debugging, historical imports), the scripts are still directly invocable:

```
cd LilLisa_Server/cron
../.venv/bin/python nightly_pipeline.py
```

Avoid doing that while the API is up — the tick and a manual process are separate OS processes, and the in-process lock cannot see across them.

### 5c. Required volume

The pipeline's state files (`techsupport_sync_state.json`, `techsupport_reembed_state.json`, `techsupport_review_state.json`) are written next to the scripts, i.e. `/app/cron/` in the image. Mount that directory on persistent storage. Without it, every container restart loses the dedup state and the next run re-processes every thread as new. The same applies to `LANCEDB_FOLDERPATH`, `VERIFIED_TECHSUPPORT_QA_FOLDERPATH`, and `LilLisa_Server/scripts/` (where the API writes `techsupport_thread_tags.json` and the pipeline reads it).

If you have pipeline state JSON under the old `LilLisa_Server/scripts/` or `lil-lisa-cron-scripts/` location, copy it into `LilLisa_Server/cron/` before first run. Leave `techsupport_thread_tags.json` where the API writes it (`LilLisa_Server/scripts/`).

**Secrets.** `env/techsupport_sync.env` and `env/github_push.env` live under the cron package (`/app/cron/env/`) and are gitignored, so supply them the same way `passwords/` is supplied in your deployment. Without them the tick logs a clear `Missing required env var(s)` error each time and does nothing else.

Since the pipeline runs in the same process as the API, `LIL_LISA_SERVER_URL` keeps its `http://127.0.0.1:8000` default — the index reload call is to itself.

## 6\. Slack App Configuration

No new scopes are needed beyond what's already configured. The existing bot token and scopes (`app_mentions:read`, `chat:write`, `im:write`, `channels:history`, `channels:read`, `im:history`) cover everything needed for the escalation flow and the nightly scripts.

The bot doesn't have `channels:join`. If it's ever removed from the admin channel, or the admin channel changes someone will need to manually invite it back.

For the real production tech support channel, the bot needs to actually be a member of that channel for the nightly scripts to read its history, or `conversations.history` and `conversations.replies` will fail with `not_in_channel`. 

## 7\. Private GitHub Repo

This is now live and actively syncing. The verified techsupport markdown file gets pushed automatically after every nightly update to a dedicated private repo, `sgodey8/rocknbot_techsupport_qa_pairs`. Every answer that draws on this content includes a working link directly to the relevant section of the file, using GitHub's heading-anchor links, so users can click through and read the full context.

This repo currently lives under my personal GitHub account, matching the existing golden QA pairs repo (`drawal1/rocknbot_qa_pairs`), not a company-owned org account. This can be moved if needed.

### 7a. `github_push.env` setup (devops)

`github_sync.py` (called from `nightly_pipeline.py`) clones that private repo over HTTPS and pushes `techsupport_qa_pairs.md` if it changed. **Same two variables as before** — there is no new env var and no SSH deploy key.

1. Copy the example and fill it in (this file is gitignored):

```
cd LilLisa_Server/cron/env
cp github_push.env.example github_push.env
```

2. Set:

| Variable | What to put |
| :---- | :---- |
| `GITHUB_TOKEN` | A GitHub PAT that can push to the repo in `GITHUB_REPO_URL`. Fine-grained: **Contents: Read and write** on that repo. Classic: `repo` scope for a private repo. |
| `GITHUB_REPO_URL` | Plain HTTPS clone URL only, e.g. `https://github.com/org/rocknbot_techsupport_qa_pairs.git`. |

3. **Do not put the token in the URL.** These are wrong and will be rejected (or would leak the PAT into `.git/config` / git error logs):

```
# BAD
GITHUB_REPO_URL=https://ghp_....@github.com/org/repo.git
GITHUB_REPO_URL=git@github.com:org/repo.git
```

The script injects credentials via `GIT_ASKPASS` (a short helper that prints `$GITHUB_TOKEN` when Git asks for a username/password). GitHub accepts the PAT as that password. The clone URL logged at INFO is the clean `https://github.com/...` URL with no secret.

4. **The token must live in `github_push.env`.** `github_sync.py` reads that file only. Exporting `GITHUB_TOKEN` in crontab or the process environment is **not** enough if the file is missing or the keys are empty.

5. Cron does not need extra Git config. The host needs `/bin/sh` (normal Linux runner). A user-level `credential.helper` will not persist this PAT: that clone passes `credential.helper=` so the token is not written to `~/.git-credentials`.

6. Smoke-check after deploy:

```
cd LilLisa_Server/cron
../.venv/bin/python github_sync.py
```

Unchanged markdown prints `{'pushed': False, 'reason': 'unchanged'}`. A real change prints `pushed: True` and a commit message. Logs should show `Cloning https://github.com/...` **without** a token in the URL.

**If push fails:** GitHub 401 / `Authentication failed` almost always means the PAT is expired, lacks Contents write, or `GITHUB_REPO_URL` points at the wrong repo. Rotate the token in `github_push.env` only (not in a clone URL). You should not need to change cron or Git's global config.

## 8\. Rollback Procedure

The verified techsupport LanceDB table (`TECHSUPPORT_QA_PAIRS`) keeps full version history. Every re-embed or bulk write creates a new version instead of overwriting in place. If something goes wrong, like a bad re-embed or corrupted data, you can check what's available:

cd LilLisa_Server/cron

../.venv/bin/python \-c "from techsupport_rollback import list_available_versions; list_available_versions()"

Then roll back to a specific version:

../.venv/bin/python \-c "from techsupport_rollback import rollback_to_version; rollback_to_version(N)"

This is non-destructive. Rolling back creates a new version rather than deleting anything, so you can always move forward or backward again afterward.

Now that the GitHub repo in Section 7 is live, the markdown file itself also has real version history through normal git commits, so both the database content and the source file can be inspected or reverted independently if needed.

### 8a. Restore markdown + review_state with LanceDB

`techsupport_rollback.py` only restores the LanceDB table (`TECHSUPPORT_QA_PAIRS`). It does **not** auto-restore files from Lance versions.

The verified techsupport markdown, `techsupport_review_state.json`, and LanceDB must be restored **together** from the same point in time. Rolling the table back without the matching files will drift: review_state `node_ids` will not match table rows, and the markdown source will not match what retrieval serves.

After `rollback_to_version(N)`, the script prints a loud reminder listing the other files. Restore them yourself:

1. LanceDB `TECHSUPPORT_QA_PAIRS` — this script (`rollback_to_version(N)`).
2. `techsupport_qa_pairs.md` — under `VERIFIED_TECHSUPPORT_QA_FOLDERPATH` (typically `LilLisa_Server/data/verified_techsupport/techsupport_qa_pairs.md`). Use git history in the Section 7 repo (or a backup) from the same moment as LanceDB version N.
3. `LilLisa_Server/cron/techsupport_review_state.json` — gitignored; restore from a backup/copy taken at that same moment. There is no Lance version of this file.
