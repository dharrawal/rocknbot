# Deployment Documentation: Tech Support Integration

## 1\. Overview

This adds two things to Rocknbot. First, a real-time escalation flow that lets Lil Lisa post unanswered questions to a tech support channel with one click. Second, a nightly pipeline that automatically turns resolved tech support conversations into verified answers the bots can use going forward, including merging new insight into an existing answer instead of always creating a duplicate. Nothing here requires a new service, a scheduler, or host access. It's an extension of `LilLisa_Server` and `lil-lisa`: the pipeline ships inside the API image and the API process runs it on its own schedule (Section 5).

The same nightly run also watches the product channels themselves. When Lil Lisa answers a question in the IDA / IDDM / IDO channel and a member of that product's expert user group replies in the thread to fix or extend the answer, that reply is picked up and either rewrites the verified entry the answer came from or becomes a new one. There is no command to learn and no prefix to type: being in the product's expert user group is the whole signal. See Section 4c.

## 2\. New Environment Variables

### `LilLisa_Server/env/lillisa_server.env`

| Variable | Default | Purpose |
| :---- | :---- | :---- |
| `TECHSUPPORT_SYNC_INTERVAL_HOURS` | `24` | How often the nightly pipeline checks the tech support channel for new or updated threads. Expert-adjustable. |
| `TECHSUPPORT_SYNC_HOT_DAYS` | `30` | Nightly, `nightly_techsupport_sync.py` only refreshes known threads whose last reply activity is within this many days. Uses a cheap parent `latest_reply` lookup (`conversations.history`, not a full `conversations.replies` download). |
| `TECHSUPPORT_SYNC_CATCHUP_AGE_DAYS` | `90` | Age cap for the periodic catch-up: known threads quieter than the hot window but still within this many days are checked on catch-up runs. Threads older than this stay in state but are not polled. |
| `TECHSUPPORT_SYNC_CATCHUP_INTERVAL_DAYS` | `7` | How often the catch-up pass runs (stamped in `techsupport_sync_state.json` as `last_catchup_timestamp`). Independent of `TECHSUPPORT_SYNC_INTERVAL_HOURS`. |
| `TECHSUPPORT_SYNC_MAX_PARENT_LOOKUPS` | `200` | Cap on each of the nightly hot set and the catch-up set (hottest first). Excess waits for a later run. |
| `TECHSUPPORT_SCAN_PRODUCT_CHANNELS` | `true` | Master switch for the nightly product-channel scan for expert corrections (Section 4c). Set it to `false` to turn the whole pass off without unsetting the channel IDs. |
| `PRODUCT_SCAN_MAX_THREADS_PER_RUN` | `50` | Per-product cap on how many changed threads one run classifies. The excess is remembered in that channel's state (`pending_thread_ids`) and picked up next run, hottest first, so a busy day cannot become an unbounded LLM bill. |
| `PRODUCT_SCAN_INITIAL_LOOKBACK_DAYS` | `30` | How far back the *first* scan of a product channel looks. A channel with no state would otherwise be synced from timestamp zero, i.e. its entire history, and every old thread carrying an expert reply would be ingested. Later runs simply resume where the previous one stopped, so this only ever applies once per channel. Product channels only; the tech support channel is unaffected. |
| `TECHSUPPORT_REEMBED_INTERVAL_DAYS` | `7` | How often the verified techsupport table gets a full contextual re-embed (late-chunking refresh). Expert-adjustable. |
| `VERIFIED_TECHSUPPORT_QA_FOLDERPATH` | `data/verified_techsupport/` | Where the verified techsupport markdown file lives locally, before it gets pushed to the GitHub repo (see Section 7). |
| `TECHSUPPORT_PIPELINE_TICK_MINUTES` | `60` | How often the API process checks whether the nightly pipeline is due (Section 5). Not the nightly schedule itself — real work is gated by `TECHSUPPORT_SYNC_INTERVAL_HOURS`, so this only needs to be more frequent than it. |

### `LilLisa_Server/cron/env/techsupport_sync.env` (dedicated file)

| Variable | Purpose |
| :---- | :---- |
| `SLACK_BOT_TOKEN` | Bot token the nightly pipeline uses to read the tech support channel. Same bot as `lil-lisa`'s. |
| `TECHSUPPORT_CHANNEL_ID` | The real tech support channel ID. **Must equal** `lil-lisa`'s `TECHSUPPORT_CHANNEL_ID_IDA` / `_IDDM` / `_IDO` (those three must all be the same ID; the bot raises at startup if they disagree). If you also copy the product-specific names into this file, the pipeline refuses to start when they differ from `TECHSUPPORT_CHANNEL_ID`. |
| `ADMIN_CHANNEL_ID` | Where pipeline error notifications get posted. |
| `PRODUCT_CHANNEL_ID_IDA` / `_IDDM` / `_IDO` | The product channels Lil Lisa answers questions in, scanned nightly for expert corrections (Section 4c). Same IDs as `lil-lisa`'s `CHANNEL_ID_*`. All three are optional: a missing one simply disables that product's scan. The bot has to be a member of each one (Section 6). |
| `LIL_LISA_SLACK_USERID` | The bot's own Slack user ID, used to tag its turns as `(bot)` when a thread is formatted for the classifier and correction prompts. Optional: bot messages are still recognised by their `bot_id` without it. |
| `EXPERT_GROUP_ID_IDA` / `_IDDM` / `_IDO` | Slack **user group** IDs (e.g. `S0123ABCD`, from `usergroups.list` — not the `@handle`) whose members count as experts for that product. Same values as `lil-lisa`'s. `_IDA` and `_IDDM` are **required**: the resolver raises `ValueError` naming the missing variable, so the product-channel pass reports an error for that product instead of scanning it. `_IDO` is optional. Needs the `usergroups:read` scope (Section 6). |
| `EXPERT_GROUP_CACHE_SECONDS` | How long expert group membership is cached before Slack is asked again. Default `300`. |

Expert group membership is resolved by `LilLisa_Server/cron/expert_group.py`, the cron-side twin of `lil-lisa/src/expert_group.py` (the two packages deploy independently and cannot import each other, so the contract is duplicated — keep them aligned). Both cache membership for `EXPERT_GROUP_CACHE_SECONDS` and retry once on Slack `ratelimited`. On a failed lookup they keep serving the last cached membership for that product, with a warning; if nothing was ever cached they raise `ExpertLookupError` so the failure is loud. A Slack user group is now the only source of expert identity: there is no single-ID setting and no silent degradation to "nobody is an expert".

This is a separate env file from `lil-lisa`'s on purpose, so the pipeline never depends on `lil-lisa`'s directory or config existing — `LilLisa_Server` and `lil-lisa` deploy independently.

### `LilLisa_Server/cron/env/github_push.env` (dedicated file, new)

| Variable | Purpose |
| :---- | :---- |
| `GITHUB_TOKEN` | Personal access token used to push the verified techsupport markdown file to its dedicated GitHub repo after every nightly update. See Section 7a for PAT scopes and how auth is passed (GIT_ASKPASS; token must not be in the URL). |
| `GITHUB_REPO_URL` | HTTPS URL of that repo with **no** embedded credentials (currently `sgodey8/rocknbot_techsupport_qa_pairs`, see Section 7). |
| `QA_PAIRS_GITHUB_TOKEN` | Optional. Only needed when the golden QA pairs repo (`QA_PAIRS_GITHUB_REPO_URL` in `lillisa_server.env`) takes a different PAT than `GITHUB_TOKEN` above. See Section 7b. |

### `lil-lisa/app_envfiles/lil-lisa.env` (additions to the existing file)

| Variable | Purpose |
| :---- | :---- |
| `TECHSUPPORT_CHANNEL_ID_IDA` | Tech support channel for IDA. |
| `TECHSUPPORT_CHANNEL_ID_IDDM` | Tech support channel for IDDM. |
| `TECHSUPPORT_CHANNEL_ID_IDO` | Tech support channel for IDO (optional product). |
| `EXPERT_GROUP_ID_IDA` / `_IDDM` / `_IDO` | Slack **user group** IDs (e.g. `S0123ABCD`, from `usergroups.list` — not the `@handle`) whose members count as experts for that product. Every member gets the expert `👍` → golden QA pair and `#answer` behaviour, not just one person. `_IDA` and `_IDDM` are **required**: the bot refuses to start without them, the same as a missing `SLACK_BOT_TOKEN`. `_IDO` is optional; with no group nobody is an IDO expert. Needs the `usergroups:read` scope (Section 6). |
| `EXPERT_GROUP_CACHE_SECONDS` | How long expert group membership is cached before Slack is asked again. Default `300`. |

**Removed variables:** `EXPERT_USER_ID_IDA` / `_IDDM` / `_IDO` are gone. Expert identity comes only from the Slack user group, so set `EXPERT_GROUP_ID_IDA` and `_IDDM` before deploying: the bot raises at startup without them. The expert who gets DMed after a golden QA pair is added is now the expert who actually reacted, not a configured ID.

In production these must all point to the **same** real channel (one shared tech support channel, not one per product). The bot asserts that at startup. The pipeline watches only `TECHSUPPORT_CHANNEL_ID` in `techsupport_sync.env`; set that to the same ID.

**Important existing variable:** `MAX_LENGTH`: must be 3000 or less, since that's Slack's hard limit on message length. A backup check also exists in case this is ever misconfigured.

### 2a. How these files reach the container

The tables above describe files in the repo. Whether those files are *inside* the image depends on which dockerfile built it, and the three differ on purpose:

| Image | `env/` + `passwords/` | Built by |
| :---- | :---- | :---- |
| `dockerfile_local`, `dockerfile_cloud` | `COPY env /app/env` and `COPY passwords /app/passwords` — baked in | `make build-local` / `make build-cloud`, from your working copy |
| `dockerfile_prod` | **Neither is copied** — supply at runtime | `.github/workflows/ci.yaml`, published as `radiantone/rocknbot-server:staging` |

`dockerfile_prod` omits them deliberately: CI builds it from a clean checkout where both are gitignored, and it gets pushed to a registry, so it must contain no secrets. That means a prod deployment has to supply the configuration itself, either way round:

- **Mount the directories** at `/app/env` and `/app/passwords`. This is the closest match to the tables above and to how local/cloud behave.
- **Inject environment variables.** Every config loader in `src/` and `cron/` reads its `.env` file and then overlays `os.environ`, so a real env var beats a missing file or an empty placeholder in it. This covers `lillisa_server.env`, `cron/env/techsupport_sync.env`, and `cron/env/github_push.env` alike.

The one thing injection alone cannot replace is the key files. `LLM_API_KEY_FILEPATH`, `OPENAI_API_KEY_FILEPATH`, and `VOYAGE_API_KEY_FILEPATH` name *paths*, and `main.lifespan` reads each file at startup — a missing one raises `FileNotFoundError` and the container never serves. So even under injection, the key files must exist at whatever paths those variables point to.

This fails fast and loudly at boot rather than degrading, so a misconfigured prod container is obvious immediately. It is not a nightly-pipeline problem: because startup demands strictly more configuration than the pipeline does, a container that is serving traffic has everything the pipeline needs.

**Do not hand-build the prod image on a dev box.** `COPY cron /app/cron` picks up whatever is in `cron/env/` in the build context, and `.dockerignore` does not exclude it (local and cloud rely on those files being present). Building `dockerfile_prod` from a working copy that has real credentials there would bake `SLACK_BOT_TOKEN` and `GITHUB_TOKEN` into an image that is otherwise secret-free. CI is unaffected — it clones fresh, and both files are gitignored.

## 3\. New Files and Directories

- `LilLisa_Server/data/verified_techsupport/techsupport_qa_pairs.md`. The verified techsupport Q\&A source file (markdown). Auto-created and appended by the pipeline, and auto-pushed to GitHub (Section 7\) after every update.  
- `LilLisa_Server/cron/`. Nightly pipeline Python jobs (see Section 4), shipped in the API image and run by the API process itself — see Section 5. It has no `pyproject.toml` of its own: DSPy is in the server's dependencies and everything shares `LilLisa_Server/.venv`.  
- `LilLisa_Server/src/techsupport_cron.py`. Adapter that lets the API process run `nightly_pipeline.run_pipeline()`: resolves the cron package, serialises overlapping runs, and provides the periodic tick that `main.lifespan` starts.  
- `LilLisa_Server/.dockerignore`. Keeps virtualenvs and generated state out of the build context (it was several GB before this existed).  
- `LilLisa_Server/cron/env/techsupport_sync.env` and `LilLisa_Server/cron/env/github_push.env`. See Section 2\. Both need to be gitignored (already confirmed).  
- State files, auto-created and gitignored, safe to delete if you want a clean slate since the pipeline will just re-detect everything as new on the next run:  
  - `LilLisa_Server/cron/techsupport_sync_state.json`  
  - `LilLisa_Server/cron/techsupport_reembed_state.json`  
  - `LilLisa_Server/cron/techsupport_review_state.json`  
  - `LilLisa_Server/scripts/techsupport_thread_tags.json`, written by the API request handler (`POST /tag_techsupport_thread/`). The pipeline only reads it; see Section 4a.  
  - `LilLisa_Server/scripts/techsupport_answer_tags.json`, written by the API at answer time (`POST /invoke/`): `{Slack thread ts: title of the verified techsupport entry that answer cited}`. The pipeline only reads it, to tell an expert correcting a cited entry from a brand-new question; see Section 4c.

`techsupport_sync_state.json` is now keyed by channel, since the pipeline watches the tech support channel and the product channels:

```
{"version": 2,
 "channels": {"<channel id>": {"last_run_timestamp": ...,
                               "last_catchup_timestamp": ...,
                               "threads": {...},
                               "pending_thread_ids": [...]}}}
```

This is the only shape the pipeline accepts. A missing file is created fresh on the next run. A file whose top-level `version` is not `2`, including an early build's flat file with `last_run_timestamp` / `last_catchup_timestamp` / `threads` at the top level, is rejected: the run stops with a `RuntimeError` naming the path and telling you to delete the file. Delete it and re-run; the next run rebuilds it, and for the tech support channel that means the first sync starts from the beginning of the channel again. Nothing in production has the old shape, so this should never fire outside a dev box. `pending_thread_ids` holds the product-channel threads deferred by `PRODUCT_SCAN_MAX_THREADS_PER_RUN` (Section 4c).

## 4\. Scripts

| Script | When to run | Purpose |
| :---- | :---- | :---- |
| `nightly_pipeline.py` | **Nightly, automatic.** Run by the API process (Section 5); the **only** script that should ever be scheduled. | The main entry point. Orchestrates everything below, including the GitHub push and the live server's index reload. |
| `nightly_techsupport_sync.py` | Nightly, via `nightly_pipeline.py` | Detects new or updated threads in the tech support channel. New parents come from `conversations.history` since last run. Known threads are not all polled: nightly hot window + periodic 90-day catch-up, both capped (see the `TECHSUPPORT_SYNC_*` knobs in Section 2). |
| `techsupport_classifier.py` | Nightly, via `nightly_pipeline.py` | Classifies whether a thread is useful and conclusive (DSPy-based). |
| `techsupport_qa_ingest.py` | Nightly, via `nightly_pipeline.py` | Extracts a summary from a resolved thread and adds it to the markdown file plus LanceDB, or merges it into an existing entry (see Section 4a). |
| `techsupport_contextual_reembed.py` | Nightly, via `nightly_pipeline.py` (on its own interval) | Periodically re-embeds the whole verified table together (late-chunking), on the interval from Section 2\. |
| `techsupport_review_sync.py` | Nightly, via `nightly_pipeline.py` | Picks up manual edits an expert might make directly to the markdown file and syncs them into LanceDB. Fully optional, not a gate. |
| `techsupport_rollback.py` | Manual (ops) | `list_available_versions()` and `rollback_to_version(n)`, see Section 8\. |
| `github_sync.py` | Nightly, via `nightly_pipeline.py` (or standalone retry) | Pushes the current markdown file to the dedicated GitHub repo. Skips the push if the file hasn't actually changed. Called automatically by `nightly_pipeline.py`, but can also be run standalone if a push needs to be retried manually. |
| `expert_group.py` | Library / helper | Resolves which Slack users count as experts for a product from `EXPERT_GROUP_ID_*` (Section 2), raising rather than guessing when a required group is unset or unreadable. Used by the product-channel scan. Cron-side twin of `lil-lisa/src/expert_group.py`. |
| `github_anchor.py` | Library / helper | Generates GitHub-accurate anchor slugs from entry titles, so answers can link directly to the right section of the file on GitHub. |
| `historical_import_production.py` | **One-shot, manual.** Never scheduled. Optional: `make run-historical-import`. | One-time bulk import of `data/historical_import/production_1year.txt` into the same verified-techsupport store the nightly pipeline maintains. Resumable; not part of the default schedule. |
| `backfill_github_urls.py` | **One-shot, manual.** Never scheduled. Optional: `make run-backfill-github-urls`. | One-time metadata patch: adds `github_url` on existing LanceDB rows that predate the GitHub-anchor feature. New nightly inserts already get this; do not schedule this script. |

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

### 4c. Product-channel scan for expert corrections

`nightly_pipeline.run_product_channel_pass()` runs after the tech support loop and before the GitHub push, once per configured product channel (`PRODUCT_CHANNEL_ID_IDA` / `_IDDM` / `_IDO`). It uses the same thread-detection machinery as the tech support channel, with its own slice of `techsupport_sync_state.json` and its own `TECHSUPPORT_SYNC_INTERVAL_HOURS` gate per channel, so the channels are independent of one another.

**Additive, not a change to the tech support loop.** This pass is a separate scan of separate channels. The tech support channel's own detection, classification and ingest behave exactly as they did before, and turning the pass off with `TECHSUPPORT_SCAN_PRODUCT_CHANNELS=false` leaves the nightly run as it was.

**What the first scan covers.** A product channel with no state yet is seeded to `now - PRODUCT_SCAN_INITIAL_LOOKBACK_DAYS` (default 30) before its first `sync()` call, so that run only sees threads created inside that window rather than the channel's whole history. The seed is written to that channel's slice of `techsupport_sync_state.json` and logged as an INFO line naming the lookback. It happens once: from the second run on, the channel resumes from where the previous run stopped. Widen `PRODUCT_SCAN_INITIAL_LOOKBACK_DAYS` before the first run if you want more backlog picked up, and remember the per-run cap still applies, so a wide window drains over several runs rather than in one.

**Hot and catch-up windows apply per channel.** `TECHSUPPORT_SYNC_HOT_DAYS` (30), `TECHSUPPORT_SYNC_CATCHUP_AGE_DAYS` (90) and `TECHSUPPORT_SYNC_CATCHUP_INTERVAL_DAYS` (7) govern each product channel exactly as they govern the tech support channel: known threads quieter than the hot window are only re-checked on catch-up runs, and threads quieter than the catch-up cap stay in state but are no longer polled. These are per channel, so one busy channel does not shorten another's windows.

**The expert-reply gate comes first.** A thread is only looked at further if some message *other than the thread parent* was posted by a member of that product's expert user group. An expert opening a thread with a question of their own is not a correction. No expert reply means no LLM call at all, counted as `skipped_no_expert_reply`, which is what keeps ordinary product-channel traffic from costing anything.

**The expert-insight gate comes second.** An expert having replied is not by itself a reason to rewrite the knowledge base. One `HasExpertInsight` call reads the role-tagged thread and answers whether any `(expert)` message corrects, confirms, or adds technical insight to the `(bot)` answer or to the topic: a correction, a confirmation that the fix worked, an extra fix, a caveat. An expert's own questions and follow-up questions never count, and neither does small talk. A no is counted as `skipped_no_expert_insight` and nothing else happens: no classification, no ingest, no state flag. Because nothing is recorded, the thread comes back for reconsideration the moment `sync()` sees new reply activity on it, so the correction an expert posts after their question is picked up on the following run.

**Role tags.** A thread that passes the gate is rendered for the prompts with the speaker's role attached: `Lil Lisa (bot)`, `Jane (expert)`, and `Lil Lisa (bot, relaying a user's question)` for the repost the escalate button makes. The bot's contentless `Processing...` placeholders are dropped, and the classifier, summarize and correction prompts all state that `(bot)` content is AI-generated and unverified, and that a later `(expert)` message supersedes it.

**Routing.** Whether the answer in the thread cited a verified entry is what decides. That is recorded at answer time in `techsupport_answer_tags.json` (Section 3), keyed by the Slack thread ts:

1. **Cited entry, expert replied: supersede.** `correct_verified_entry()` rewrites that entry so anything the expert contradicts is removed or replaced, and everything else is kept. Only the "useful" bar applies here, not conclusiveness, the same lighter bar the escalation merge in 4a uses: the topic is already resolved, this thread only has to be worth folding in.
2. **Cited entry no longer in the markdown.** Falls back to a normal add, re-classified against the full useful *and* conclusive bar rather than inheriting the lighter one.
3. **No cited entry.** A plain new Q\&A: full useful and conclusive bar, then `add_verified_qa_pair()`.
4. **Thread already ingested, new expert activity since.** The same operation is repeated: corrected again if last time was a correction (a corrected entry keeps the thread ts of the thread that originally created it, so a replace could not find it), otherwise replaced.

**Supersede versus append merge.** These are deliberately different operations. The escalation path in 4a preserves the existing content and appends the new insight. A correction is the opposite: the point is that the contradicted content must *not* survive. Both keep the entry's title verbatim, which is what keeps its GitHub anchor link stable.

**Provenance.** A supersede appends a record to that entry in `techsupport_review_state.json` under `corrections`: `source_channel_id`, `source_thread_ts`, and `superseded_at`. The escalation enrich path appends a matching record under `enrichments` (same fields, stamped `enriched_at`), so each entry carries the history of every thread that changed it. An entry that was never corrected keeps exactly the shape it had before, so old state files are read unchanged.

**Volume and reporting.** Each product classifies at most `PRODUCT_SCAN_MAX_THREADS_PER_RUN` threads per run, hottest first; the rest are written to that channel's `pending_thread_ids` *before* any work starts, so a deferred thread is still picked up next run even though the sync already moved `last_seen_reply_ts` forward. Results appear under `product_channels` in the run summary, with three new counts, `corrected`, `skipped_no_expert_reply` and `skipped_no_expert_insight`. Per-thread errors ride along in the same admin alert as the tech support loop, prefixed with the product. A failure of the whole pass is alerted and does not fail the rest of the run.

**Prerequisites:** `PRODUCT_CHANNEL_ID_*` set (Section 2), the bot a member of those channels, and the `usergroups:read` scope (Section 6). If no experts can be resolved for a product, that product is skipped with a warning rather than scanned.

## 5\. Nightly Pipeline Scheduling

**There is no crontab.** The API process schedules and runs the nightly pipeline itself. Nothing to install on a host, no second container, no external scheduler — deploying the API image is the whole setup.

The three API dockerfiles `COPY cron /app/cron`, and `LilLisa_Server/pyproject.toml` includes DSPy. `src/techsupport_cron.py` puts that package on `sys.path` and runs the same `run_pipeline()` in-process, and `main.lifespan` starts the tick at boot.

Because `cron/` lives inside the server tree, `paths.py` resolves the server root as its own parent — `/app/cron` → `/app` in the image, and the checkout path on a dev box. No env var is needed for either.

**Build context.** Unchanged (`LilLisa_Server/`), but a new `LilLisa_Server/.dockerignore` is now required: it keeps `.venv`, `speedict/`, and generated state out of the build, which took the context from ~7.3 GB down to ~179 MB (almost all of the remainder is `lancedb/`, deliberately kept because `dockerfile_lancedb` copies it). What each image does and does not carry out of that context differs — see Section 2a before building or deploying `dockerfile_prod`.

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

The pipeline's state files (`techsupport_sync_state.json`, `techsupport_reembed_state.json`, `techsupport_review_state.json`) are written next to the scripts, i.e. `/app/cron/` in the image. Mount that directory on persistent storage. Without it, every container restart loses the dedup state and the next run re-processes every thread as new. The same applies to `LANCEDB_FOLDERPATH`, `VERIFIED_TECHSUPPORT_QA_FOLDERPATH`, and `LilLisa_Server/scripts/`, where the API writes both tag files, `techsupport_thread_tags.json` and `techsupport_answer_tags.json`, and the pipeline reads them. Losing the answer-tags file is not data loss, but a correction posted in a thread whose answer tag is gone is filed as a new entry instead of rewriting the entry the answer actually came from (Section 4c).

If you have pipeline state JSON under the old `LilLisa_Server/scripts/` or `lil-lisa-cron-scripts/` location, copy it into `LilLisa_Server/cron/` before first run. Leave both tag files where the API writes them (`LilLisa_Server/scripts/`).

**Secrets.** `env/techsupport_sync.env` and `env/github_push.env` live under the cron package (`/app/cron/env/`) and are gitignored, so supply them the same way `passwords/` is supplied in your deployment — mounted, or injected as environment variables (see Section 2a). Without them the tick logs a clear `Missing required env var(s)` error each time and does nothing else.

Since the pipeline runs in the same process as the API, `LIL_LISA_SERVER_URL` keeps its `http://127.0.0.1:8000` default — the index reload call is to itself.

### 5d. Forcing the product-channel scan by hand

`POST /run_product_channel_scan/` runs only the product-channel expert-correction pass (Section 4c): the IDA/IDDM/IDO channels are scanned, and whatever changed is pushed to GitHub and reloaded into the running index. The tech support loop, the review sync and the contextual re-embed are not run. Same `encrypted_key` JWT as the other admin routes:

```
curl -X POST "https://your-api/run_product_channel_scan/?encrypted_key=$ENCRYPTED_KEY"
```

`force` defaults to `true`, which is what an operator triggering a scan by hand normally wants: it bypasses the per-channel `TECHSUPPORT_SYNC_INTERVAL_HOURS` gate, so every configured channel is scanned now instead of being skipped because it was already checked today. Pass `force=false` to respect that gate, i.e. to behave exactly like the nightly run:

```
curl -X POST "https://your-api/run_product_channel_scan/?encrypted_key=$ENCRYPTED_KEY&force=false"
```

Nothing else is bypassed. `TECHSUPPORT_SCAN_PRODUCT_CHANNELS=false` still turns the pass off, `PRODUCT_SCAN_MAX_THREADS_PER_RUN` still caps each product, and threads already ingested are still deduped.

Like `/run_nightly_pipeline/`, it returns immediately and runs as a FastAPI background task, with progress and the run summary in the server log, and returns 503 if the image was built without the cron package. Both endpoints and the tick share one lock: a scan arriving during a nightly run is ignored rather than queued, and so is a nightly run arriving during a scan.

## 6\. Slack App Configuration

One new scope is needed: **`usergroups:read`**. Everything else in the escalation flow and the nightly scripts is covered by the existing bot token and scopes (`app_mentions:read`, `chat:write`, `im:write`, `channels:history`, `channels:read`, `im:history`).

`usergroups:read` lets the bot call `usergroups.users.list` to resolve the per-product expert user groups (`EXPERT_GROUP_ID_IDA` / `_IDDM` / `_IDO`, Section 2). Add the scope and reinstall the app **before** deploying: without it every expert lookup fails loudly (`ExpertLookupError` out of the Slack handlers, an errored product in the nightly pass) instead of quietly treating everyone as a non-expert.

### 6a. Expert user groups: what to set up

| Question | Answer |
| :---- | :---- |
| Does the bot need a new permission? | **Yes, one.** Add the bot token scope `usergroups:read` under OAuth & Permissions in the Slack app settings, then reinstall the app to the workspace so the bot token picks it up. It is a read-only scope. No other scope changes are needed. |
| Does the bot need to be a member of the expert groups? | **No.** User groups are workspace-level objects, and a bot token with `usergroups:read` can read any group's member list without belonging to it. User groups only hold human members, so there is nothing to add the bot to. |
| What does the bot do with the group? | Exactly one read-only call: `usergroups.users.list` for the configured group ID, cached for `EXPERT_GROUP_CACHE_SECONDS`. It never posts to the group, never mentions it, and never changes its membership. The group is only a lookup table for "is this user an expert". |
| What value goes in `EXPERT_GROUP_ID_*`? | The group's **ID**, which starts with `S` (for example `S0123ABCD`), **not** its `@handle`. Find it on the group's page under People & user groups in Slack, or from the `usergroups.list` API method. A handle in this variable fails the lookup. |
| Which Slack plan? | User groups are a paid-plan feature. They are not available on the free tier. |

The bot doesn't have `channels:join`. If it's ever removed from the admin channel, or the admin channel changes someone will need to manually invite it back.

For the real production tech support channel, the bot needs to actually be a member of that channel for the nightly scripts to read its history, or `conversations.history` and `conversations.replies` will fail with `not_in_channel`. The same holds for the product channels in `PRODUCT_CHANNEL_ID_*`, which the expert-correction scan reads with those same two calls. The bot answers questions in them already, so it is already a member; there is nothing to do unless it gets removed.

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

4. **Either the file or the process environment works.** `github_sync.load_env()` reads `github_push.env` and then overlays `os.environ`, so a container/k8s secret wins over an empty placeholder in the file. Supplying both is fine; supplying neither raises `Missing required env var(s) [...] - expected in <path> or the process environment`.

5. No extra Git config is needed. The image needs `/bin/sh`, which the `python:3.11-slim` base provides. A user-level `credential.helper` will not persist this PAT: that clone passes `credential.helper=` so the token is not written to `~/.git-credentials`.

6. Smoke-check after deploy. Inside the running container (no virtualenv there — dependencies are installed system-wide with `uv pip install --system`):

```
cd /app/cron
python github_sync.py
```

From a dev checkout instead:

```
cd LilLisa_Server/cron
../.venv/bin/python github_sync.py
```

Unchanged markdown prints `{'pushed': False, 'reason': 'unchanged'}`. A real change prints `pushed: True` and a commit message. Logs should show `Cloning https://github.com/...` **without** a token in the URL.

**Fixed in this release:** that clone now also passes `allow_unsafe_options=True`. GitPython classes `--config` as an unsafe clone option and raised `UnsafeOptionError` without it, which failed every nightly push. No configuration change is involved.

**If push fails:** GitHub 401 / `Authentication failed` almost always means the PAT is expired, lacks Contents write, or `GITHUB_REPO_URL` points at the wrong repo. Rotate the token in `github_push.env` (or the process environment), not in a clone URL. You should not need to change Git's global config.

### 7b. Expert thumbs-up pairs now push to the golden QA repo

Previously, a `POST /add_expert_qa_pair/` (the expert thumbs-up in a product channel) only wrote the pair into the `{PRODUCT}_QA_PAIRS` LanceDB table. `/update_golden_qa_pairs/` **drops that table** and rebuilds it from the markdown in `QA_PAIRS_GITHUB_REPO_URL`, so every expert pair added since the last rebuild was silently lost. `src/golden_qa_sync.py` now appends each verified pair to `{product}_qa_pairs.md` in that repo and pushes it, so the rebuild stays a pure function of the repo.

| Item | Detail |
| :---- | :---- |
| Repo | `QA_PAIRS_GITHUB_REPO_URL` from `env/lillisa_server.env` (the golden QA pairs repo). Plain HTTPS clone URL only — a URL with an embedded token, or an SSH/scp-style URL, is rejected. |
| Token | `QA_PAIRS_GITHUB_TOKEN` if set, otherwise the existing `GITHUB_TOKEN`. Both are read from `cron/env/github_push.env` overlaid by the process environment, same as `github_sync.load_env()`. No new secret is needed if the existing PAT can also push to the golden QA repo — set `QA_PAIRS_GITHUB_TOKEN` only when it cannot. |
| Auth mechanism | Same as section 7a: `GIT_ASKPASS` prints the PAT, `credential.helper=` on the clone so nothing is persisted to disk, and the clone is shallow (`depth 1`). |
| Commit | `Add expert-verified QA pair (IDDM) - <date>`, authored as `LilLisa Expert QA <noreply@radiantlogic.com>`. |

**The push cannot fail the request.** The LanceDB insert happens first and always stands; the push runs after it in a worker thread and never raises. The endpoint's JSON response now carries a `"pushed": true|false` field, and a failed push is logged as `Expert QA pair stored in LanceDB but NOT pushed to the golden QA repo ...` with the underlying error. `pushed: false` means the pair answers queries now but will disappear at the next golden QA rebuild — fix the token/URL and re-add it, or use the backfill script below.

**Backfill (pairs verified before this change).** Those pairs are recoverable from the server log, which records each one as `Expert QA Verification: {json}`:

```
cd LilLisa_Server
PYTHONPATH=. .venv/bin/python scripts/backfill_expert_qa_pairs.py --log-file /path/to/server.log --dry-run
PYTHONPATH=. .venv/bin/python scripts/backfill_expert_qa_pairs.py --log-file /path/to/server.log
```

It dedupes repeated log lines, skips pairs already present in the repo file (so re-running after a partial failure is safe), and prints a JSON summary (`found` / `skipped_existing` / `pushed` / `failed`). `--dry-run` changes nothing; `--product IDDM` (repeatable) limits it to one product. In the container use `python` instead of `.venv/bin/python`.

## 8\. Rollback Procedure

The verified techsupport LanceDB table (`TECHSUPPORT_QA_PAIRS`) keeps full version history. Every re-embed or bulk write creates a new version instead of overwriting in place. If something goes wrong, like a bad re-embed or corrupted data, you can check what's available.

Run this in the deployed container, since that is where the LanceDB volume is mounted (`python`, not `../.venv/bin/python` — the image installs dependencies system-wide):

cd /app/cron

python \-c "from techsupport_rollback import list_available_versions; list_available_versions()"

Then roll back to a specific version:

python \-c "from techsupport_rollback import rollback_to_version; rollback_to_version(N)"

On a dev checkout the same commands work with `cd LilLisa_Server/cron` and `../.venv/bin/python`.

This is non-destructive. Rolling back creates a new version rather than deleting anything, so you can always move forward or backward again afterward.

Now that the GitHub repo in Section 7 is live, the markdown file itself also has real version history through normal git commits, so both the database content and the source file can be inspected or reverted independently if needed.

### 8a. Restore markdown + review_state with LanceDB

`techsupport_rollback.py` only restores the LanceDB table (`TECHSUPPORT_QA_PAIRS`). It does **not** auto-restore files from Lance versions.

The verified techsupport markdown, `techsupport_review_state.json`, and LanceDB must be restored **together** from the same point in time. Rolling the table back without the matching files will drift: review_state `node_ids` will not match table rows, and the markdown source will not match what retrieval serves.

After `rollback_to_version(N)`, the script prints a loud reminder listing the other files. Restore them yourself:

1. LanceDB `TECHSUPPORT_QA_PAIRS` — this script (`rollback_to_version(N)`).
2. `techsupport_qa_pairs.md` — under `VERIFIED_TECHSUPPORT_QA_FOLDERPATH` (typically `LilLisa_Server/data/verified_techsupport/techsupport_qa_pairs.md`). Use git history in the Section 7 repo (or a backup) from the same moment as LanceDB version N.
3. `LilLisa_Server/cron/techsupport_review_state.json` — gitignored; restore from a backup/copy taken at that same moment. There is no Lance version of this file.
