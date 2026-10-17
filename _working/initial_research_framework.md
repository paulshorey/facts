# Initial research framework for facts.news

Status: implementation plan for review. No collection platform or website has been implemented.

## 1. Outcome and scope

Build a fast, agent-operated system that takes a topic and accumulates a large,
organized collection of individual examples, events, statistics, headlines,
quotes, documents, and datasets. Preserve source references when available and
retain the underlying retrieved material so it can be reused later.

The immediate output is a searchable collection of small records and portable
data exports. Later systems can turn these records into listicles, timelines,
fact lists, statistics pages, or other website content.

This phase covers sourcing, discovery, downloading, extraction, normalization,
deduplication, organization, and export. Fact-checking, source credibility
scoring, editorial review, argument analysis, article writing, and public website
design are outside this implementation. None is a prerequisite for saving an item.

Operating principles:

- Collect broadly, save early, and make partial results usable immediately.
- Produce itemized data instead of long research reports.
- Keep rich source material behind compact normalized records.
- Missing dates, incomplete citations, unavailable pages, and ambiguous fields
  must not cause otherwise useful leads to disappear.
- Use code for downloading, parsing, storage, and straightforward transformations;
  use LLMs for query expansion, item extraction, and flexible categorization.
- Preserve differing values and descriptions as separate source observations.
- Make every collection pass resumable and reusable by another agent.
- Measure useful collection throughput and cost rather than prose quality.

## 2. Initial technical choices

| Concern | Initial implementation | Reason |
| --- | --- | --- |
| Runtime | Python 3.12+, project-local environment, pinned dependencies | Fits source adapters, document processing, LLM calls, and tabular data tooling. |
| Contracts | Pydantic models with exported JSON Schemas | One format for CLI imports, agent output, database records, and exports. |
| Structured storage | PostgreSQL with ordinary columns, JSONB, and full-text search | Supports multiple workers, flexible item fields, filtering, and durable task state. |
| Captured files | Local content-addressed filesystem through a small storage interface | Start without provisioning a cloud bucket; allow an S3-compatible backend later. |
| Web discovery | Parallel Search API; connector tools for interactive agent research | Existing search access works. Use direct API calls for unattended workers. |
| Source retrieval | Direct HTTP and Trafilatura; Parallel Extract as a fallback | Cheap extraction first, broader retrieval when needed. |
| PDF extraction | Text extraction first; Docling for tables and difficult layouts | Avoid treating every PDF as a paid LLM task. OCR is an optional fallback. |
| Tables and datasets | CSV/JSON/XLSX readers, Parquet, and DuckDB | Retain and query large datasets without turning every cell into an LLM request. |
| Semantic extraction | A configurable structured-output LLM adapter | Pin the selected provider/model after a small extraction benchmark; keep provider details outside the data contract. |
| Long-running work | PostgreSQL task queue and a Python worker command | Supports restart and concurrency without adding another orchestration service. |
| Interchange | JSONL items, JSON manifests, CSV/Parquet tables, Markdown inventories | Easy handoff to other agents and future applications. |

Do not initially add Redis, a graph database, a vector database, a CMS, a separate
agent framework, or a web dashboard. SQL relations and text search are sufficient
for the first useful collection system.

Parallel's current documentation specifies `/v1/search` and `/v1/extract` for new
integrations. Implement against the current request schemas rather than copying
older v1beta examples. Extract's current fetch, excerpt, and full-content options
live under `advanced_settings`. [Search API](https://docs.parallel.ai/api-reference/search/search),
[Extract settings](https://docs.parallel.ai/extract/advanced-extract-settings)

## 3. Collection architecture

```text
Topic / seed URLs / supplied files
              |
              v
Query expansion + source discovery <---- follow-up queries / discovered links
              |
              v
Persistent source frontier
              |
              v
Capture source bytes / API responses / search excerpts
              |
       +------+----------------------+
       |                             |
       v                             v
Text / PDF / transcript path    Dataset / table path
       |                             |
Chunk + extract discrete items  Parse rows + retain schema and units
       |                             |
       +------------+----------------+
                    v
Normalize fields + exact dedupe + tag / group
                    |
                    v
PostgreSQL collection + private artifact storage
                    |
                    v
JSONL / JSON / CSV / Parquet / Markdown inventory
```

Write each successful stage as it completes. A research run does not need to
finish before its collected items can be listed or exported.

Two execution paths share the same storage contract:

1. **Worker-driven collection:** Python calls provider APIs, fetches pages,
   processes documents, and normalizes results without an open chat.
2. **Agent-assisted collection:** An agent uses its available web tools and
   imports a structured research packet through the CLI. It can add sources,
   excerpts, items, and follow-up queries to an existing topic.

Desktop connector tools are not assumed to be callable from a background Python
process. Each automated adapter needs its own supported API or CLI execution
path. Imported agent packets provide a bridge when only interactive tools exist.

## 4. Source adapters and rollout order

### Required adapters for the first usable version

| Adapter | Input | What to preserve |
| --- | --- | --- |
| Parallel Search | Topic objective and batches of related queries | Exact query, search context, result URL/title/date, excerpts, raw response, provider request ID. |
| Direct URL capture | Discovered or user-supplied URL | Requested/final URL, retrieval time, response status, MIME type, useful non-secret response metadata, body artifact. |
| Parallel Extract | URLs that need additional retrieval or extraction | Returned text, source URL, retrieval provenance, errors, raw response. |
| Local files | HTML, Markdown, text, PDF, CSV, JSON, JSONL, XLSX | Original file, file fingerprint, import metadata, parsed outputs. |
| Agent packet import | Versioned JSON/JSONL from an agent or external research tool | Supplied source references, original tool payload where supplied, individual items, next queries. |

Search excerpts are useful collection inputs. Save them immediately even if the
full page cannot be retrieved. Record `capture_level=search_excerpt` or
`provider_extract`, not `full_document`. A provider's full extracted text still
does not mean we have captured the original page bytes.

### Next adapters, after the core path works

- **last30days:** Import recent web/social results from the installed skill's
  JSON output. Support its versioned agent profile and, where more source detail
  is useful, its raw profile. Preserve the original output and record the skill
  revision. Do not rerun research merely to change export formats.
- **RSS/Atom and sitemaps:** Enumerate known publisher collections and newly
  published URLs efficiently.
- **Targeted site collections:** Search pages, archives, document indexes, and
  API pagination for a known source. Prefer a bounded adapter over a general
  browser agent repeatedly navigating the same site.
- **Public datasets:** Initially Treasury Fiscal Data, USAspending, CBO downloads,
  and SEC filing collections where relevant to a requested topic. Each adapter
  specifies identifiers, pagination, formats, and source-specific limits.
- **Scholarly metadata:** OpenAlex/Crossref searches for intellectual topics;
  save metadata and accessible documents as separate artifacts.
- **Video/transcripts:** Import available transcripts with timestamps. Keep
  transcription separate from ordinary text extraction.

Keep browser-based capture, large site crawls, paid extraction services, and
Parallel Task API research as optional adapters. They can fill specific gaps;
ordinary search-to-items collection should work without them.

`last30days` is installed at `~/.agents/skills/last30days`. Its adapter must follow
the installed runtime and execution instructions, select sources explicitly for
unattended work, and avoid interactive setup during a running campaign. Missing
source credentials disable that lane with a recorded reason; they do not block
the other adapters. Full source access has not been tested for this project.

Parallel Task API can later run larger source-enumeration jobs that return item
arrays and URLs. Import its results through the same packet contract; do not
require an essay/report or provider confidence score. Persist external job IDs
and retrieve the original job after interruption. [Task API](https://docs.parallel.ai/task-api/task-quickstart),
[Task groups](https://docs.parallel.ai/task-api/group-api)

## 5. How a topic becomes a broad search campaign

The input is a short collection specification, not an editorial brief:

```yaml
schema_version: topic.v1
slug: example-topic
title: Human-readable topic
request: Original user request, preserved verbatim
date_from: null
date_to: null
languages: [en]
seed_queries: []
seed_urls: []
entities: []
collection_tags: []
item_kinds: [fact, example, event, statistic, headline, quote, document, dataset]
profile: broad
```

Allow an agent to create this from a sentence. Most fields are optional. A broad
topic should be runnable without an interview or a manually completed taxonomy.

### First pass: expand breadth

Use one planning call to generate query families, then execute them in batches:

- Topic synonyms, alternate spellings, names, organizations, and common aliases.
- Subtopics and concrete mechanisms associated with the request.
- Time slices for historical or long-running topics.
- Geography or institution slices where useful.
- Collection-oriented terms such as examples, list, timeline, report, database,
  investigation, statistics, records, spreadsheet, and document index.
- Source-format searches for PDFs, tables, datasets, filings, and transcripts.
- Domain-focused searches when a source appears particularly productive.
- Native-language searches when requested or when a relevant source collection
  is clearly in another language. Preserve original text and language metadata.

Query families are discovery tools, not a fixed ontology. An agent may add new
queries and tags as the collection reveals unfamiliar subtopics.

Start with roughly 12–24 query variants grouped into a smaller number of search
requests. This is a tunable first batch, not a limit on the topic. Persist every
returned candidate and all unprocessed frontier entries.

### Second pass: follow productive leads

- Extract relevant outbound links, referenced reports, named datasets, and source
  documents from captured pages.
- Expand a productive subtopic, entity, date range, publisher archive, or dataset.
- Detect named but missing documents and queue searches for them.
- Fetch the next page or time window in a structured source collection.
- Deprioritize repeated query families with little new yield without deleting
  their earlier results or requiring all searches to finish first.

Keep `discovered`, `queued`, `captured`, and `processed` counts separate. Priority
determines what runs next; it must not silently discard the rest of the frontier.

### Subsequent passes

`research continue` drains pending work or expands selected families. An explicit
refresh rechecks selected URLs, feeds, and queries for newer material. Reuse
unchanged captures and extracted items.

Record the requested scope and operational limits with each run: search request
limit, capture limit, crawl depth, provider spend ceiling where supported, and
concurrency. Limits apply to that pass. Reaching a limit leaves a resumable
backlog rather than labeling the whole topic complete.

Coverage reporting is mechanical: searched query families, time windows,
providers, encountered URLs, pagination progress, source types, failures, and
pending work. No coverage score should claim that the entire web was exhausted.

## 6. Item contract

Use one compact common envelope with kind-specific data. Do not build a complex
claim/assessment/argument ontology before there is a useful collection.

### Common item fields

| Field | Purpose |
| --- | --- |
| `id`, `schema_version` | Stable identifier and import/export contract version. |
| `kind` | `fact`, `example`, `event`, `statistic`, `headline`, `quote`, `document`, `dataset`, or `resource`. |
| `title` | Short, skimmable label, typically a phrase or one sentence. |
| `detail` | Compact description, usually 1–3 sentences; optional when the original text is sufficient. |
| `original_text` | Original excerpt, headline, quote, or row text when available. |
| `language` | Language of the original content; translated text can be stored alongside it. |
| `dates` | Event, publication, and period dates with date role and precision. Unknown dates stay null. |
| `entities` | Mentioned names and optional external identifiers; unresolved mentions are allowed. |
| `tags` | Flexible thematic labels and source-supplied categories. |
| `measurements` | Typed numbers, ranges, units, denominators, periods, and original numeric expressions. |
| `data` | Kind-specific JSON fields, validated for that kind. |
| `references` | Zero or more source/capture/locator references, with URLs where available. |
| `collection_metadata` | Import/run/chunk IDs, capture level, extractor version, and technical warnings. |

The label `fact` describes a collected item format; it does not mean that the
system has fact-checked it. No truth score, credibility score, editorial approval,
or verified state is required by this schema.

One item represents one useful example, occurrence, statistic, quote, or resource.
A page listing 40 examples should normally produce many items, rather than one
page summary. A source can produce multiple kinds. A statistic and the event it
describes may be linked while remaining independently queryable.

### Kind-specific fields

- **Examples/events:** people and organizations mentioned, location, action or
  occurrence, date/period, and source-provided category labels.
- **Statistics:** metric name, numeric value or range, unit, scale, population or
  denominator, period, geography, and reported qualifiers such as estimated.
- **Quotes:** speaker, exact supplied text, date, context, and timestamp/page if
  available. Missing speaker information does not block collection.
- **Headlines:** original headline, publisher, publication date, URL, and optional
  snippet. Do not expand a headline into invented details.
- **Documents/resources:** title, author/organization, publication date,
  document identifiers, URL, description, and stored artifact references.
- **Datasets:** source and table identifiers, schema, row count, period/coverage,
  file artifacts, units, and import/pagination metadata.

Example normalized statistic, using entirely synthetic fixture data:

```json
{
  "schema_version": "item.v1",
  "id": "item_demo_001",
  "kind": "statistic",
  "title": "Example program reported $4.2 million in expenses",
  "detail": "Synthetic example for the data contract.",
  "original_text": "Program expenses: $4.2 million",
  "language": "en",
  "dates": [],
  "entities": [{"name": "Example program", "external_id": null}],
  "tags": ["program-expenses"],
  "measurements": [{
    "metric": "reported program expenses",
    "value": "4200000",
    "unit": "USD",
    "original_expression": "$4.2 million",
    "period": null,
    "qualifier": null
  }],
  "data": {},
  "references": [{
    "source_id": "source_demo_001",
    "capture_id": "capture_demo_001",
    "url": "https://example.org/demo-report",
    "locator": {"type": "page", "page": 3}
  }],
  "collection_metadata": {
    "run_id": "run_demo_001",
    "capture_level": "full_document",
    "extractor_version": "items-v1",
    "warnings": []
  }
}
```

Use decimal strings or database numeric types for financial/precise values.
Retain the original expression. Convert obvious scales such as millions with
deterministic code; leave ambiguous expressions in text. Keep incompatible
numbers as separate observations instead of reconciling them.

An item with no usable URL is still saved with its available import/tool/source
context and `references: []` if necessary. Track missing references as a filterable
collection attribute. Never invent a citation to satisfy a schema requirement.

## 7. Storage model

Keep the relational model small:

| Table | Contents |
| --- | --- |
| `topics` / `topic_versions` | Effective collection specification and its history. |
| `runs` | Collection pass, input versions, operational limits, progress, and aggregate usage. |
| `tasks` / `task_attempts` | Durable work queue, ownership, external job IDs, attempts, and failures. |
| `sources` / `topic_sources` | Logical source identity, source metadata, topic membership, and discovery context. |
| `captures` | Versioned retrieved material and references to stored artifacts. |
| `items` / `topic_items` | Normalized item observations, topic membership, topic-specific tags, and searchable fields. |
| `item_sources` | Many-to-many references between items, sources, captures, and locators. |
| `item_groups` / `item_group_members` | Non-destructive grouping of similar/repeated items. |
| `datasets` | Table/file manifests, schemas, row counts, coverage, and parsed artifact locations. |

`items` stores the common query fields as columns and kind-specific details as
JSONB. Topic membership is many-to-many: one collected item or source can be reused
for another topic without another download or duplicate normalization call.

PostgreSQL is authoritative for structured items and task state. The artifact
store is authoritative for captured file bytes. JSONL files are imports/exports,
not a second mutable database or a parallel task-status system.

### Captured artifacts

Store original files and provider payloads as soon as they arrive:

```text
storage/objects/sha256/<prefix>/<digest>
```

Each artifact records stored-byte digest, MIME type, size, encoding/compression,
creation time, and parent artifact where it is a transformation. Write through a
temporary file and atomic rename; commit database references only after the
artifact is present. A periodic maintenance command can identify orphaned files.

Derived artifacts include cleaned text, chunk manifests, extraction JSON, table
files, and optional OCR output. A changed parser creates a new derived artifact;
it does not overwrite the original file. An unchanged source can reuse existing
outputs for the same processing version.

Record direct HTTP capture, provider extraction, search excerpt, user file, and
agent-supplied text as distinct acquisition types. Never promote a short excerpt
to a full-document capture just because its URL is known.

Large tables remain CSV/Parquet artifacts with database manifests. Materialize
useful rows into item records through explicit mappings when appropriate. Do not
create millions of generic fact records merely because a dataset has millions
of cells. DuckDB can query Parquet directly. [DuckDB documentation](https://duckdb.org/docs/lts/guides/file_formats/query_parquet.html)

Backups must cover both PostgreSQL and artifacts. Include a small restore check
that confirms restored items can still open their referenced files.

## 8. Extraction and normalization

### Text and document path

1. Detect the input format and save the original capture.
2. Extract text, headings, lists, tables, links, and available metadata with code.
3. Segment by heading/list/table boundaries; split oversized sections by token
   budget. Use small overlap for prose and repeat table headers where needed.
4. Run a structured extraction prompt over each chunk. Ask for all discrete
   topic-relevant items in the chunk, rather than the top five or a summary.
5. Normalize fields, attach source locators, and save each successful batch.
6. Save extraction errors and pending chunks separately; do not discard the
   source or previously collected items when another chunk fails.

The prompt receives the requested topic and source chunk. It returns item arrays,
tags, discovered source links, and suggested follow-up queries. It does not
evaluate credibility or decide what deserves publication.

Preserve source wording such as alleged, estimated, approximately, or denied when
present. This is information preservation during extraction, not a fact-checking
workflow. Do not enrich extracted records with unsupported details from model
memory. A model-generated lead becomes a discovery query, not a collected fact.

Use bounded chunks and structured responses. Detect output truncation and
unprocessed chunks; split and retry only the affected chunk. Permit at most one
JSON-format repair before marking that attempt failed and retaining its raw
output. A schema-valid output alone does not prove that every item in a document
was extracted; report the processed input coverage separately.

Trafilatura supports text, metadata, links, and HTML tables. Docling provides
structured document conversion and table export. Preserve original files when
conversion is incomplete. [Trafilatura](https://trafilatura.readthedocs.io/en/latest/corefunctions.html),
[Docling](https://github.com/docling-project/docling)

### Dataset path

1. Download the source file or paginate the API, preserving the original responses.
2. Record column names, types, units, period fields, and source row identifiers.
3. Use explicit mappings for known sources. Use one small LLM-assisted schema
   mapping only when needed, then apply that mapping with code to the full table.
4. Stream large files, store Parquet partitions, and retain row-level references
   such as sheet/row or API record ID.
5. Generate item rows using configured row templates if the dataset represents
   individually useful examples or figures. Keep the original dataset available.

Do not calculate economic interpretations, decide which estimate is correct, or
produce new conclusions during this phase. Basic representation transformations,
such as parsing dates or scaling an explicitly stated number, are sufficient.

### Deduplication and grouping

- Normalize URL fragments, known tracking parameters, and redirects conservatively;
  preserve original URLs and meaningful query parameters such as record IDs.
- Reuse byte-identical captures and cached extraction results.
- Make repeated imports of the same source chunk/item idempotent.
- Use text similarity, matching entities/dates, and compatible numeric fields to
  propose related-item groups after extraction. Start with SQL/text techniques;
  embeddings can be added only if they materially improve grouping.
- Preserve each differing source observation. Similarity never authorizes
  overwriting a number, discarding wording, or choosing a winning version.
- Keep exact duplicate observations separate from optional display groups.
  Exports can request every observation or grouped results with member references.

An extraction identity includes capture ID, chunk locator, extractor version, and
a deterministic item fingerprint. The cache key includes input digest, topic
extraction context, schema version, prompt version, and model settings. Changing
relevant context creates new extraction output; reprocessing the same context
does not create another identical observation. Topic-specific extraction cannot
claim that one pass exhausted every possible use of a source.

## 9. Durable execution and throughput

Task kinds: `expand_queries`, `search`, `enumerate_collection`, `capture`,
`parse_document`, `parse_dataset`, `extract_items`, `normalize_items`, `group_items`,
and `export`.

Task states: `queued`, `running`, `succeeded`, `failed_retryable`, `failed_terminal`,
and `paused`. These describe execution only. There is no editorial review queue.

Each worker claims a task atomically using row locking and a lease. Track its
heartbeat and current attempt. A unique attempt token fences commits so an old
worker cannot overwrite results after its lease has been reclaimed. PostgreSQL
documents `SKIP LOCKED` as useful for multiple consumers of a queue-like table.
[PostgreSQL SELECT documentation](https://www.postgresql.org/docs/current/sql-select.html)

Use idempotent writes and at-least-once task execution; do not promise exactly-once
external API calls. Store provider job/request IDs. If a paid asynchronous launch
has an unknown outcome, reconcile that submission before launching another job.

Start with configurable concurrency of four search requests, eight HTTP fetches,
and two LLM extraction requests. Apply per-provider and per-domain limits on top.
CPU-heavy PDF/OCR work gets a separate small process pool. These are proposed
defaults to tune with measured throughput, not expected provider allowances.

Efficiency rules:

- Batch related search variants and independent URL extractions where supported.
- Reuse a captured source across topics and runs.
- Cache parse results separately from topic-dependent extraction results.
- Skip LLM extraction for dataset rows handled by explicit mappings.
- Request focused excerpts for discovery; request complete content when a page
  contains a substantial collection of useful items. Record what was retrieved.
- Use conditional HTTP requests and explicit freshness settings when refreshing.
- Poll external jobs from the ordinary worker, not repeated AI chat turns.
- Retry only transient failures with bounded exponential backoff and jitter;
  respect rate-limit headers. Record authentication failures without a retry loop.
- Bound crawl depth and URL patterns so source-following does not become an
  accidental whole-web crawl. Keep discovered candidates for later passes.
- Record input/output tokens, requests, estimated/actual costs where available,
  and unknown usage explicitly. Do not treat missing cost data as zero.

Operational commands should expose items collected so far, remaining tasks,
failed URLs, current concurrency, and per-stage throughput. No web dashboard is
needed for the first version.

Use fetched content as data, never as agent instructions. Isolate document
parsing and reject local/private network targets in automatic public-web fetching.
Keep credentials and sensitive request headers out of stored artifacts and logs.
These are collector implementation requirements, not editorial policies.

## 10. Proposed repository and CLI

The following paths and commands are implementation targets, not existing tools.

```text
pyproject.toml
src/facts/
  cli.py
  contracts/
  adapters/
    parallel_search.py
    parallel_extract.py
    http_capture.py
    files.py
    agent_packet.py
    last30days.py
  discovery/
  extraction/
  normalization/
  storage/
  queue/
  exports/
migrations/
prompts/
topics/
tests/fixtures/
docs/collection.md
storage/                 # ignored captured files
exports/                 # ignored generated collections
_working/initial_research_framework.md
```

```bash
# Define and collect a topic.
facts topic add --slug example-topic --request "Collect examples and figures about ..."
facts research plan example-topic --profile broad
facts research run example-topic --profile broad

# Run/recover ordinary background processing.
facts worker --concurrency-profile default
facts research status example-topic
facts research continue example-topic
facts research pause example-topic

# Add discoveries from another agent or existing research files.
facts import urls example-topic --file urls.txt
facts import files example-topic --path ./incoming/
facts import agent example-topic --file research-packet.json

# Inspect collected data and export portable records.
facts items list example-topic --kind statistic
facts items list example-topic --tag example-tag
facts sources list example-topic --status failed
facts export example-topic --format jsonl --output ./exports/example-topic/
facts export example-topic --format markdown --output ./exports/example-topic/
```

`research plan` saves the generated query/task plan for inspection but is not a
mandatory human approval gate. `research run` may generate a missing plan and
proceed within its configured limits. `continue` reuses the topic frontier and
completed artifacts; `refresh` is a separate explicit operation.

Agent packet imports use `research-packet.v1`: topic slug, optional original tool
payload references, discovered sources, items, and follow-up queries. Supplied
external IDs are namespaced; internal IDs are assigned by the collector. Repeated
imports are idempotent. Agents submit batches through the CLI rather than editing
the database directly or coordinating through shared mutable JSON files.

## 11. Export contract for future display systems

Export collected data without writing articles or deciding a public layout:

- `manifest.json`: topic version, run IDs, export time, schema versions, counts,
  and outstanding collection work.
- `items.jsonl`: normalized item records, topic tags, measurements, and references.
- `sources.jsonl`: source metadata and citation information.
- `groups.jsonl`: optional grouping relationships and member IDs.
- `datasets.jsonl`: table manifests and references to exported CSV/Parquet files.
- `inventory.md`: a simple grouped list/table for the human and other agents to
  inspect the shape of the collected data.

Allow filtering by item kind, tag, entity mention, date, source, and measurement.
Keep export sorting mechanical: date, title, collection order, or explicit source
order. Grouped and ungrouped exports must remain available.

Public-display consumers can later use `title`, `detail`, dates, measurements,
tags, and citations without reading private processing logs. Export data must
not include credentials or require raw provider responses. Raw-artifact bundles
are an explicit private export option.

Do not define article approval states, editorial rankings, headline rewriting,
reason-to-care scoring, or a frontend component model in this phase. Preserve
source-supplied reasons and descriptions as ordinary collected text/items.

## 12. Implementation milestones

Implement in this order; each milestone leaves usable collection functionality.

### Milestone 1 — Import, store, and export itemized data

1. Initialize the Python package, CLI, locked dependencies, and project instructions.
2. Define `topic.v1`, `item.v1`, `research-packet.v1`, source/capture records, and
   export contracts. Keep unknown/optional fields permissive where useful.
3. Add PostgreSQL migrations, local artifact storage, and topic/item/source imports.
4. Implement `items list`, `sources list`, JSONL export, and Markdown inventory.
5. Import one small fixture packet containing examples, statistics, headlines,
   missing citations, repeated items, and differing values.

Completion: another agent can contribute useful structured research immediately,
without waiting for autonomous discovery or a public website.

### Milestone 2 — Search and collect from ordinary web sources

1. Implement query expansion, Parallel Search, source frontier, and direct URL capture.
2. Add HTML/text extraction, chunk manifests, structured item extraction, and
   deterministic normalization.
3. Add Parallel Extract fallback and local PDF/file imports.
4. Add exact deduplication, source reuse, and follow-up URL/query discovery.
5. Connect the stages to the durable queue and CLI status commands.

Completion: one topic request produces a growing, exportable collection of
individual items from multiple pages, with sources and raw captures retained.

### Milestone 3 — Expand volume and source variety

1. Add CSV/JSON/XLSX dataset ingestion, Parquet storage, and row mappings.
2. Add the last30days JSON adapter without coupling the framework to its internal
   database, HTML publishing features, or report presentation rules.
3. Add RSS/sitemap discovery and one productive paginated public-data adapter.
4. Add optional similar-item grouping, refresh, concurrency tuning, and cost/yield metrics.
5. Enable another agent or worker process to contribute safely to the same topic.

Completion: document research, social/web discovery, and bulk figures feed the
same item collection and export formats.

### Milestone 4 — Run two bounded collection pilots and refine the contracts

- **Example/event-heavy topic:** a bounded portion of the requested political
  topic, chosen to exercise many named occurrences, headlines, dates, and sources.
- **Figure/dataset-heavy topic:** a defined fiscal period of US federal spending,
  collecting available categories/line items and separately reported estimates.

Record collected item kinds, source counts, dataset row counts, missing fields,
failed retrievals, remaining frontier, elapsed time, and provider usage. Inspect
whether records are granular enough to become short lists and figures later.
Adjust the schema only where actual collected material requires it.

These pilots test collection throughput, storage, and organization. They do not
need a finished narrative, a verified verdict, or a publication-ready article.

## 13. Technical acceptance checks

Use a small set of meaningful fixtures and a bounded live collection run:

- A page containing many examples yields separate item records, not just one summary.
- Multiple chunks can finish independently; interruption resumes pending work.
- Reimporting the same packet or retrying a completed task creates no duplicate observations.
- A changed source capture or extractor version retains its separate processing provenance.
- Two sources with differing amounts remain separately accessible.
- Null, zero, false, approximate values, ranges, dates, and currencies survive normalization.
- A failed full-page fetch leaves the search result/excerpt and retryable frontier intact.
- A large dataset uses streaming/table processing without one LLM call per row.
- Multiple workers cannot commit through an expired task lease.
- JSONL export/import preserves item data and source relationships.
- A restored database/artifact bundle can open the files referenced by its items.
- Partial exports disclose pending/failed collection work without blocking export.

Track new URLs per query family, new item observations per source, rows ingested,
cache hits, exact duplicates avoided, pending work, and cost per collected batch.
These are operational metrics, not accuracy ratings or claims of completeness.

## 14. Immediate next implementation target

Build Milestones 1 and 2 first. The first useful deliverable is a CLI that accepts
a topic, gathers pages, extracts many small records, retains their source material,
and exports a collection that can be inspected and extended by another agent.

Add dataset and last30days adapters once that core path works. Leave fact-checking,
editorial systems, article composition, and website presentation to their later plans.
