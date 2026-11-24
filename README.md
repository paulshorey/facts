# facts.news research collection

A local-first collector for itemized research. The current campaigns cover Trump
family/associate financial activity during the second term and US federal
spending categories, expense rows, and attributed estimates. The original design
is in [`_working/initial_research_framework.md`](_working/initial_research_framework.md).
The latest verified refresh, cloud restore and development mirror results are in
[`_working/corpus_refresh_handoff.md`](_working/corpus_refresh_handoff.md).
The next-task recommendation, operational prerequisites and offline alternatives
are in [`_working/next_initiative_handoff.md`](_working/next_initiative_handoff.md).

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
Pydantic, HTTPX, psycopg, boto3 and pytest. The lockfile records the installed versions.
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
# Only after the recovery gates pass and current model token rates are configured:
.venv/bin/facts normalize --topic trump-second-term --limit 5 --workers 1 --max-cost-usd 1 --existing-only
.venv/bin/facts repair-truncated
.venv/bin/facts export
```

`capture` targets source URLs without a recorded direct capture. HTTP error
responses are retained; `fallback` uses Parallel Extract for sources without a
successful readable capture. An excerpt remains usable when full retrieval fails.
`normalize` requires one explicit topic, positive `--limit`, and explicit
`--max-cost-usd`. Configure current model rates in `FACTS_EXTRACT_INPUT_RATE` and
`FACTS_EXTRACT_OUTPUT_RATE` (USD per million tokens), or pass
`--input-usd-per-million` / `--output-usd-per-million`. There are no stale default
extraction prices. The ceiling is a conservative reservation using these declared
rates, not a provider billing guarantee. It covers up to three HTTP attempts,
input byte/token framing allowance and maximum output/reasoning allowance. A
configured rate/model mismatch can invalidate a dollar estimate; verify the quote
before a paid batch. Reservations are never refunded within a run.

`--existing-only` consumes the saved queue without generating new extraction jobs.
Otherwise the command queues chunks of the best available saved capture first.
Failed jobs are excluded unless `--retry-failed` is supplied. A cost reservation
and `running` job state commit before submission; interrupted uncertain jobs are
left `running` for manual evidence recovery, not automatically retried. Inspect
their `normalize_reservation` attempts and saved outputs before deciding whether
to retry. Transport, HTTP and response-parsing exceptions also preserve `running`
and stop new dispatch because an exception alone does not establish that no paid
request completed. Even `--retry-failed` excludes these uncertain jobs. Completed
responses with an invalid extraction schema remain explicit failed jobs. Storage failures stop dispatch; other already submitted bounded requests
may finish, but no replacement request is launched. No sync/crawl/batch loop is
started on failure. Truncated outputs split into smaller tasks; all original input
and failed output artifacts remain available. `repair-truncated` also splits
older failed attempts. Topic definition changes create new extraction contexts.

The SQLite catalog remains the local collection journal; the dedicated development
PostgreSQL database receives explicit verified mirror imports. Supported mutating
entrypoints and backup share one nonblocking, reentrant, collector-wide file lock
at `storage/locks/collector.lock`. Run one writer per collector root on one host;
different topics/stages no longer run concurrently with backup. Low-level direct
`Store` mutations remain the caller's responsibility. SQLite WAL and local locks
do not provide distributed ownership. Stage commands finish a finite batch; no recurring schedule
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

Treasury API amount fields are already USD; only the printed PDF scales them to
millions. The importer now retains API dollars without multiplying them. Legacy
`item.v1` Treasury observations from the initial collection contain a million-fold
scaling error and legacy dataset manifests have the wrong unit label. Organization
rebuilds those values from the saved raw rows, records `treasury-api-dollars-v1`,
and preserves the original records. Use organized values for downstream work.
OMB generic cells and budget-account year values
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
Use the portable recovery commands below to snapshot the catalog, retained
`storage/` artifacts, topic definitions and exports together. PostgreSQL sync
does not itself upload original files or Parquet; `facts-handoff` delivers those
objects and their manifest separately.

Source headlines, search excerpts, source-specific extracted observations, and
dataset rows are counted separately. Repeated source mentions and alternate
descriptions are retained; item counts are not counts of unique events. Grouping,
fact-checking, editing, and public website publication are later work. The
organization layer below supplies reversible grouping without editing the sources.

## Organize the existing corpus

The approved specification is
[`_working/content_normalization_plan.md`](_working/content_normalization_plan.md).
The initial normalization results and recovery notes are in
[`research/normalization_handoff.md`](research/normalization_handoff.md).
The latest cached refresh and storage verification is linked at the top of this README.
Organization reads the saved items/tables and leaves source records, raw captures,
and the extraction queue intact. It stores new versioned observations, entities,
group memberships, jobs, mappings and coverage in the same local catalog. Financial
values use decimal arithmetic; numeric table values are stored in typed Parquet.

```bash
# Truly read-only: no schema changes, jobs or provider calls.
.venv/bin/facts organize --topic all --dry-run

# 100 prose items stratified by topic/import origin, plus retained tables.
.venv/bin/facts organize --topic all --pilot --max-cost-usd 10

# Resume/process the current corpus. Unchanged successful inputs use caches.
.venv/bin/facts organize --topic all --incremental --max-cost-usd 10

# Deterministic adaptations/table mappings only, without provider calls.
.venv/bin/facts organize --topic all --no-llm

.venv/bin/facts items --topic trump-second-term --view organized --category world-liberty --limit 10
.venv/bin/facts items --topic trump-second-term --view grouped --contains Powerus --limit 3
.venv/bin/facts items --topic us-federal-spending --view organized --content-role table_observation --limit 5
.venv/bin/facts series --topic us-federal-spending --basis net_outlays --limit 10
.venv/bin/facts export --topic all --organized
```

`--entity`, `--category`, `--group`, `--kind`, `--basis`, `--content-role`,
`--date-from` and `--date-to` filter organized records. Date filters use source
event/reporting dates rather than publication dates. `series --period` matches
the exact retained period label; values also include period bounds when known.
Historical fiscal-year labels before 1977 retain their labels without silently
using the modern October–September calendar.
Explicit `FY` prefixes in table period labels also select the fiscal calendar,
independently of the workbook filename. Long-format series retain account/category
labels in their title and dimensions so different labeled rows remain separate
across reporting periods. Repeated headings retain distinct source columns rather
than overwriting a dimension. The `tables-v4` mapper invalidates older cached table
outputs; the next organization pass rebuilds derived versions from retained rows.

Code handles explicit field aliases, number scales, table transformations and
source locators. The LLM assigns semantic categories/types, extracts entity roles
and attribution, and suggests grouping among supplied candidates. It selects
existing source text rather than writing new articles. Unknowns and unresolved
layouts remain inspectable. Similar descriptions never replace source observations
or choose a preferred figure. Source leads, comments, captions, table observations
and substantive prose have separate `content_role` fields.

Observations also retain `source_fields` for supplied structured fields that do
not fit the shared model. Measurements keep `original_basis` beside the canonical
financial basis. Entity aliases require supplied alternative names; the original
names and their evidence remain accessible. Group definitions retain membership
versions and history; cached match decisions can be applied to new processing
versions when their semantic inputs are unchanged.

`FIREWORKS_API_KEY` enables structured inference. `FACTS_MODEL` or `--model`
selects the model. The default reuses GLM 5.3 Flash. Rates default to $0.15 input
and $0.50 generated tokens per million, estimated standard prices as of
2026-10-17; set `FACTS_ORGANIZE_INPUT_RATE` and `FACTS_ORGANIZE_OUTPUT_RATE` when
changing model/rates. Price estimates count all generated/reasoning tokens and
ignore caching discounts. A missing usage response is conservatively charged at
the reserved request ceiling. These estimates are not a provider billing report.

The $10 limit is per command invocation, including reserved in-flight capacity.
Work stops at the ceiling with unfinished jobs retained. There are at most two
provider requests in flight by default; `--workers 1` through `--workers 8`
provide bounded overrides (the full-corpus run used eight after measuring latency).
HTTP 401/403 stop submissions; Retry-After is persisted
and respected. Truncated multi-item batches split, successful jobs remain cached,
and invalid structured results get one validation retry. Interrupted submissions
retain their conservative cost reservation and may be retried; exactly-once
provider billing is not promised. Local per-topic file locks prevent overlapping
organization commands, including `all` versus a specific topic.

Partial valid batches keep their successful observations and create single-input
repair jobs only for omitted/malformed results. A terminal failure leaves a
`semantic_unresolved` record with the original fields and response pointers.
Ordinary reruns do not pay to retry unchanged terminal inputs; use
`organize --retry-failed` explicitly after inspecting/fixing the cause.

New private artifacts live in `storage/organized/` (requests/responses also use
the existing content-addressed objects). The first execution saves
`storage/backups/catalog-before-organization.sqlite`. Organized exports live in
`exports/<topic>/organized-v1/<snapshot>/`; `latest.json` identifies the current
immutable snapshot and `inventory.md` is a convenient preview. JSONL files include
observations, source entries, entities, groups, sources, references, dataset
manifests, series definitions and input coverage. Copied value/row-coverage
Parquet files use paths relative to the snapshot, while private original artifact
references remain collector-root-relative provenance pointers. Original source
bytes are not bundled. Manifests disclose unresolved mappings, queued extraction
work and estimated usage. They do not assert completeness or additive totals.

The normalization contracts are in `src/facts/contracts.py`; the observation JSON
schema is included in organized exports. Grouping and mapping relations are
versioned, and stale relations are ignored after their observations change.

## PostgreSQL handoff

```bash
.venv/bin/python -m facts.postgres_sync
# For writes, use the snapshot-linked command below with its S3 delivery receipt.
```

The command above reports local record counts and the kinds an apply would
reconcile; it does not connect to PostgreSQL. `--apply` creates the `facts` schema
and mirrors versioned record envelopes into `facts.records`, with topic, kind and
JSONB indexes. One complete local catalog is authoritative for the synchronized
kinds in that PostgreSQL target. It stages one consistent SQLite snapshot using
COPY, upserts current envelopes and deletes remote IDs absent locally in one
transaction. Present but empty kinds are cleared; optional organization kinds
missing from an older local schema and unknown remote kinds remain untouched.
Historical versions still present locally remain in the mirror. Mirror writers
serialize; ordinary readers can continue reading the previous committed snapshot.
Failure rolls back the entire apply, including its deletions. Other database
schemas are left alone. This is an interim portable import contract, not a
production application database design. Every successful import also commits a
content-addressed receipt in `facts.import_receipts`; `facts.mirror_state` points
to the receipt describing the current mirror. Identical logical content and
artifact-manifest identity reuse the same receipt. The local delivery receipt is
written only after the PostgreSQL commit. A lost local receipt is safe to recover
by repeating the import. The CLI holds the collector-wide writer lock from snapshot
validation through the PostgreSQL commit and local receipt write, so supported
writers cannot change the catalog between verification and import. A failed
receipt insert rolls back the mirror too.
Unchanged envelopes skip the upsert write, avoiding repeated JSONB/index/WAL work
on retries. `synced_records` counts staged envelopes; `written_records` counts
inserts/changed envelopes. Per-record `synced_at` tracks the last content import,
while receipts record completed deliveries.
Local receipts are scoped by the non-secret database host/port/name under
`storage/deliveries/postgres/<target-id>/`, so identical imports to different
databases cannot overwrite each other's local delivery records.
Bare `--apply` is rejected before opening the catalog. Operational imports require
`--snapshot`, `--bundle` and `--artifact-receipt`; the receipt must name the exact
configured S3 endpoint/bucket/prefix, not a filesystem mirror or another bucket.
The CLI also retrieves and hashes the configured replica's manifest before opening
PostgreSQL, so missing/inaccessible/corrupt replica metadata blocks an import.
The lower-level primitive rejects downgrading an already linked mirror to an
unlinked receipt. A receipt records a completed transfer; the manifest check does
not rehash all remote file objects. Reverify after any known damage
or access change, and check actual provider volume usage before a new full import.

The dedicated Facts development resources and secure connection setup are
documented in [.railway/README.md](.railway/README.md). Read-only infrastructure
verification, SQL/TLS connectivity and actual corpus delivery are distinct checks.

## Cached refresh and portable recovery

```bash
.venv/bin/facts-handoff backup
# Save the returned snapshot digest before starting the refresh.
.venv/bin/facts organize --cached-only
.venv/bin/python -m facts.audit --current --output /tmp/facts-audit.json
.venv/bin/facts export --organized
.venv/bin/facts-handoff backup
```

`--cached-only` keeps deterministic normalization and replays saved semantic,
mapping and grouping output. It prohibits provider submissions before credential
checks or job recovery, costs zero new inference, and leaves pending work visible.
It differs from `--no-llm`, which skips semantic/grouping cache replay. It cannot
be combined with `--retry-failed` or `--no-llm`. Original items, source captures,
extraction jobs, provider task IDs and prior record versions remain retained.

`facts.audit` checks SQLite integrity, observation/series contracts, current
normalizer versions, referenced sources/captures/files, Parquet value/series links
and dataset row dispositions. It fingerprints all original catalog tables and
active derived contents for before/after and repeatability comparisons. It checks
structure and lineage; its output does not assert factual accuracy or completeness.

Portable bundles default to `storage/handoffs/`. SQLite's backup API creates a
consistent catalog snapshot; files are copied into a deduplicated object pool.
The manifest records original relative paths, SHA-256, byte sizes and original
table fingerprints. Prior backup directories, locks, live SQLite companions,
hidden files and credentials are excluded. Run while collection/export stages are
idle: changing files fail verification instead of producing a completed manifest.
The manifest must include every catalog-referenced artifact, including historical
records, search and extraction inputs/outputs, attempt artifacts, and raw packet
origins stored by follow-up-only imports. Job IDs in follow-up origins are not file
references. The read-only audit uses this same artifact inventory. Missing retained
evidence fails audit, backup, and verification. An in-repository
bundle must live under `storage/handoffs`; otherwise use a separate external path
so excluding the backup cannot accidentally exclude original evidence directories.
The bundle includes raw captures, source packets, model outputs, provider task IDs,
derived Parquet/JSONL, topic definitions and exports. A blog export alone is not
a full research backup. Code/dependencies and credentials are restored separately.
New manifests use `handoff.v2` and declare their original-table inventory. Legacy
`handoff.v1` inventories are frozen independently of the current writer's table
list, so adding another preserved table cannot invalidate a published snapshot.

```bash
# Replace this placeholder with the digest printed by backup.
facts_snapshot='<snapshot-sha256>'
.venv/bin/facts-handoff verify --snapshot "$facts_snapshot"
.venv/bin/facts-handoff restore --snapshot "$facts_snapshot" \
  --destination /tmp/facts-restored

# .env.facts-dev is written by .railway/connect.py; values never appear in Git.
.venv/bin/python -m dotenv -f .env.facts-dev run --override -- \
  .venv/bin/facts-handoff deliver --snapshot "$facts_snapshot" --workers 8

# Fetch into an independent bundle, then restore into a new root.
.venv/bin/python -m dotenv -f .env.facts-dev run --override -- \
  .venv/bin/facts-handoff receive --snapshot "$facts_snapshot" \
  --bundle /tmp/facts-received --workers 8
.venv/bin/facts-handoff restore --snapshot "$facts_snapshot" \
  --bundle /tmp/facts-received --destination /tmp/facts-remote-restored
.venv/bin/python -m facts.audit --root /tmp/facts-remote-restored --current
```

Delivery/retrieval use one to thirty-two bounded threads (default eight). Existing objects are read and
verified, never blindly overwritten. New S3 objects use `If-None-Match: *`, then
are downloaded and hashed. The manifest is delivered last, and the local receipt
appears under `storage/handoffs/receipts/<snapshot>/<destination-hash>.json` only
after all objects and the remote manifest verify. Interrupted work can resume with
the same snapshot; incomplete retrieval has no completed `latest.json`. Restore
verifies all bytes before atomically publishing a new destination and refuses to
overwrite an existing root. `--remote-directory /other/disk/facts` provides the
same delivery protocol for an independent filesystem backup.

The S3 client requires `FACTS_S3_ENDPOINT`, `FACTS_S3_BUCKET`,
`FACTS_S3_ACCESS_KEY_ID`, `FACTS_S3_SECRET_ACCESS_KEY`; optional `FACTS_S3_REGION`
and `FACTS_S3_PREFIX` default to `auto` and `facts-v1`. Manifests contain no
credentials. Conditional creation is requested as a best-effort safeguard.
Railway makes no separate guarantee of atomic conditional writes, listing
freshness or read-after-write consistency. The same credential can overwrite or
delete content; bucket backups, object lock and versioning are not provided.
This is a verified replica, not an immutable store; keep the local bundle. See
[Railway's clarification](https://station.railway.com/questions/railway-buckets-consistency-and-create-d83ae29c).

Discover snapshot IDs if `latest.json` is lost:

```bash
.venv/bin/facts-handoff list
.venv/bin/python -m dotenv -f .env.facts-dev run --override -- .venv/bin/facts-handoff list --remote
```

Discovery validates manifest identity but does not claim a complete/fresh listing
or verify file objects. Select an explicit snapshot ID and run `verify`/`receive`.
Ordinary delivery/retrieval still fail on a damaged existing object. Explicit
repair uses a fully verified local bundle, quarantines damaged replica bytes and
their digest before replacement, replaces one key, then verifies it:

```bash
.venv/bin/python -m dotenv -f .env.facts-dev run --override -- \
  .venv/bin/facts-handoff repair --snapshot "$facts_snapshot" \
  --object-digest '<damaged-object-sha256>' --confirm-repair
# Omit --object-digest to repair the snapshot's manifest instead.
```

Repair is never an automatic response to a failed upload/download. Quarantine and
repair receipts remain under the local bundle. Run `deliver` again after repair
to record a fresh full verification. Transfers/repairs for one bundle share a
local writer lock; concurrent writers on different hosts/bundles are unsupported,
and this protocol does not claim provider compare-and-swap semantics.

Link a PostgreSQL import to the exact delivered artifact snapshot by importing
from a restored root and supplying its verified delivery receipt:

```bash
.venv/bin/python -m dotenv -f .env.facts-dev run --override -- \
  .venv/bin/python -m facts.postgres_sync --apply \
  --root /tmp/facts-remote-restored --bundle storage/handoffs \
  --snapshot "$facts_snapshot" \
  --artifact-receipt 'storage/handoffs/receipts/<snapshot>/<destination-hash>.json'
```

The command rejects a catalog whose bytes differ from the bundle snapshot.
Remote SQL receipts include the logical catalog digest, synchronized kinds,
per-kind counts and artifact-manifest ID. The current mirror may later advance;
the content-addressed artifact snapshot remains the recovery source for earlier receipts.
Database outages and failed uploads leave the original catalog, last successful
receipt and pending extraction work intact; retry delivery without recrawling or
resubmitting research/model jobs. PostgreSQL remains a development mirror of the
complete catalog, with its documented deletion semantics.

## Checks

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src
git diff --check
```

Tests cover replay-safe item imports, source reference preservation, null/zero/
false values, full chunk coverage, local-network URL rejection, partial exports,
truncated-input splitting, and incorrect spreadsheet dimension recovery.
PostgreSQL reconciliation tests start and stop an isolated temporary cluster over
a Unix socket when local `initdb`/`pg_ctl` are available for a non-root user.
Otherwise those integration tests skip explicitly. They never use
`FACTS_DATABASE_URL` or an existing PostgreSQL service.

Collector review safeguards: public capture pins the validated IP for every redirect,
retaining the original HTTP Host and TLS SNI and disabling environment proxy/netrc
settings. Individual Treasury failures are recorded while later datasets continue.
The OMB bootstrap manifest at `storage/bootstrap/isolated-web-preflight.omb-downloads.json`
is optional: supply a JSON list of `{"url": "https://.../workbook.xlsx"}` entries
for expanded OMB coverage. When absent, cataloged workbooks and CBO URLs still run,
and a missing-manifest attempt makes the coverage limitation explicit. Bounded
follow-up expansion advances past existing searches; packet follow-ups and remote
record envelopes retain pending leads.

Organization review safeguards: retryable/queued inference jobs refresh configured
token prices before submission; completed jobs retain historical settings. Explicit
acronym expansions reuse a unique supported entity ID. Exact occurrence grouping
includes entity identity, including replay of older grouping edges. Export snapshot
identity includes the exported entity and table payloads.
Conflicting expansions of the same acronym retain separate canonical entities;
later bare acronym mentions retain an ambiguous identity instead of selecting one
of the organizations. Existing source records and observation links are preserved.

### Observation bounds and linked-source limits

The observations-v6 adapter preserves full ISO date ranges, evaluates topic windows
with either bound absent, and handles negated amount comparisons before positive
substrings. A subsequent organization pass versions corrected observations while
retaining original items and cached model responses. No saved corpus is rewritten
by installing the code. Link discovery counts newly associated topic sources
against its limit even when the URL is already cataloged for another topic.

### FY2027 Budget Appendix pilot

The pilot collects the Congressional Budget Office Salaries and Expenses program
and financing schedule on printed page 19. It reads the saved full-volume PDF
and compares the complete schedule against the GovInfo Legislative Branch PDF.
The first collection fetches that chapter. Subsequent runs reuse the saved
captures. A disagreement or incomplete row stops the import before any item is
committed. The two PDF copies represent the same published schedule.

```bash
.venv/bin/python -m facts.appendix --collect
.venv/bin/python -m facts.appendix --format markdown
```

Each row and fiscal column yields one item with both PDF locators, the printed
row code, account hierarchy, actual/estimate label and original expression.
Dollar figures in the source are in millions. A dotted source cell retains its
expression and has no numeric measurement. Rows include balances, adjustments
and overlapping totals. The Markdown preview compares budget authority with
outlays and displays the full schedule. The default JSON includes row headings,
cell expressions, source IDs, capture IDs and PDF digests. A rerun reuses item
identities. Both commands accept `--root` for another collector checkout.

For the November 24 collection, the private pack under
`exports/us-federal-spending/fy2027-cbo-schedule/` includes the verified schedule
and a source comparison on printed page 20. Check account codes and year headers
against page geometry when using older model extractions from two-column PDFs.
