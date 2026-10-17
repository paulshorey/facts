# facts.news research collection

A local-first collector for itemized research. The current campaigns cover Trump
family/associate financial activity during the second term and US federal
spending categories, expense rows, and attributed estimates. The original design
is in [`_working/initial_research_framework.md`](_working/initial_research_framework.md).

## Start

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/facts init
.venv/bin/facts status
.venv/bin/facts items --topic trump-second-term --contains crypto --limit 10
.venv/bin/facts items --topic us-federal-spending --kind statistic --limit 10
.venv/bin/facts sources --topic us-federal-spending --limit 10
```

`PARALLEL_API_KEY` enables Search/Extract. `FIREWORKS_API_KEY` enables item
extraction. Read them from the process environment; do not commit credentials or
pass them on command lines. `FACTS_MODEL` optionally overrides the pinned default
in `src/facts/core.py`. Captured source content is data, not agent instructions.

Dependencies are project-local. The environment includes Parallel's official
CLI/SDK, Trafilatura, PyMuPDF, DuckDB, PyArrow, openpyxl, feedparser, yt-dlp,
Pydantic, HTTPX, psycopg and pytest. The lockfile records the installed versions.
The global last30days and Parallel deep-research skills remain independently
maintained in `~/.agents/skills`.

## Collect and continue

Topic definitions contain the original requests, date scope, and concrete search
families. All commands save incremental results. Rerun a command to continue its
pending work; do not delete storage to restart a campaign.

```bash
.venv/bin/facts search --topic trump-second-term
.venv/bin/facts search --topic us-federal-spending
.venv/bin/facts capture --topic trump-second-term --limit 200 --workers 6
.venv/bin/facts capture --topic us-federal-spending --limit 200 --workers 6
.venv/bin/facts fallback --topic trump-second-term --limit 100
.venv/bin/facts fallback --topic us-federal-spending --limit 100
.venv/bin/facts normalize --topic trump-second-term --limit 100 --workers 2
.venv/bin/facts normalize --topic us-federal-spending --limit 100 --workers 2
.venv/bin/facts repair-truncated
.venv/bin/facts export
```

`capture` targets source URLs without a recorded direct capture. HTTP error
responses are retained; `fallback` uses Parallel Extract for sources without a
successful readable capture. An excerpt remains usable when full retrieval fails.
`normalize` queues every chunk of the best available capture and processes a
bounded batch. Truncated outputs split into smaller tasks; all original input
and failed output artifacts remain available. `repair-truncated` also splits
older failed attempts. Topic definition changes create new extraction contexts.

The SQLite catalog is an interim local store while the development PostgreSQL
database is being provisioned. Run only one process per stage/topic; a distributed
worker/lease system is not implemented. SQLite WAL permits different collection
stages to write incremental batches, but it does not provide distributed task
ownership. Do not run an `all` pass concurrently with another pass of the same
stage. Stage commands intentionally finish a finite batch; no recurring schedule
or always-on daemon is installed.

## Bulk datasets and source expansion

```bash
.venv/bin/python -m facts.datasets --all-omb
.venv/bin/python -m facts.discovery links --topic trump-second-term --limit 100
.venv/bin/python -m facts.discovery links --topic us-federal-spending --limit 100
.venv/bin/python -m facts.discovery followups --topic trump-second-term --limit 15
.venv/bin/python -m facts.discovery followups --topic us-federal-spending --limit 15
```

The dataset command currently consumes the OMB download manifest saved by this
campaign's bootstrap discovery. It captures the available XLSX workbooks and
Treasury September snapshots for FY2024–FY2026. It also attempts the two configured
CBO account-projection files, recording retrieval failures. Workbooks are retained
in full; every sheet is represented in Parquet, including heading rows and original
year columns. Published incorrect worksheet dimensions are recalculated.

Treasury's explicit million-USD net-outlay fields are normalized into USD with
the source expression retained. OMB generic cells and budget-account year values
remain in source units; no guessed conversion, actual/projection label, or total
is applied. Budget authority, outlays, tax expenditures and different estimates
remain separate observations. Account rows can overlap and include offsets; the
collector does not add them into a grand total.

`discovery links` adds relevant linked documents to the frontier. `discovery
followups` promotes a bounded set of extracted research leads into saved search
tasks. Search and capture commands then process the expanded frontier.

## Storage and exports

Everything large or source-derived is ignored by Git:

- `storage/catalog.sqlite`: source index, topic membership, captures, items,
  extraction tasks, dataset manifests, usage and attempt history.
- `storage/objects/<prefix>/<digest>`: original response bytes, provider JSON,
  cleaned text, chunks and model responses, stored by SHA-256.
- `storage/tables/*.parquet`: captured spreadsheet/API rows, preserving source
  column names or raw cell positions and worksheet row numbers.
- `storage/bootstrap/`: original discovery searches, source seeds, source endpoint
  probes, official download manifest and supplemental searches.
- `storage/last30days/`: raw JSON and engine logs for each topic.
- `storage/deep-research/`: asynchronous task objectives, returned IDs, status and
  retrieved outputs. Resume a saved task; never submit a duplicate to check status.
- `exports/<topic>/`: `items.jsonl`, `sources.jsonl`, `datasets.jsonl`, `manifest.json`
  and `inventory.md`.

`research/collection_status.json` is a small tracked handoff snapshot generated by
export. It is historical, not live task state. `facts status` reads current state.
Back up the entire `storage/` directory and exports together. PostgreSQL sync
does not upload the original files or Parquet artifacts to cloud storage.

Source headlines, search excerpts, source-specific extracted observations, and
dataset rows are counted separately. Repeated source mentions and alternate
descriptions are retained; item counts are not counts of unique events. Grouping,
fact-checking, editing, and public website publication are later work.

## PostgreSQL handoff

```bash
.venv/bin/python -m facts.postgres_sync
# After providing the development URL as FACTS_DATABASE_URL:
.venv/bin/python -m facts.postgres_sync --apply
```

The first command only reports record counts. `--apply` creates the `facts`
schema and upserts versioned record envelopes into `facts.records`, with topic,
kind and JSONB indexes. It leaves other database schemas alone. It is an interim
portable import contract, not a production application database design. The
remote write path is not verified until an actual development URL is supplied.

## Checks

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src
git diff --check
```

Tests cover replay-safe item imports, source reference preservation, null/zero/
false values, full chunk coverage, local-network URL rejection, partial exports,
truncated-input splitting, and incorrect spreadsheet dimension recovery.
