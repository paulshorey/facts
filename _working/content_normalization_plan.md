# Organize the collected research into reusable items, groups and data series

Approved implementation plan, 2026-10-17. Publish this document directly to main;
implement on `codex/content-normalization`, stacked on `codex/research-collection`
with a PR targeting that collector branch. Leave the collector PR open.

## 1. Approach and current corpus

Build a second normalization layer over the existing collection. Use the existing
Fireworks LLM API for semantic organization, and Python/DuckDB for table
transformations and numeric conversions. Keep each original observation
accessible beneath its normalized representation.

The first pass processes the current corpus. Later extraction results enter the
same pipeline incrementally.

| Existing content | Count | Processing approach |
| --- | ---: | --- |
| Prose observations from extracted chunks and research packets | 3,054 | Standardize fields, classify with an LLM, resolve names and group related observations |
| Table-derived item observations | 7,474 | Rebuild normalized representations from the underlying dataset rows |
| Headline/source entries | 1,578 | Identify headlines, source leads, comments and navigation material; retain discovery context |
| Retained dataset tables | 88 tables / 35,508 rows | Interpret headers, units and hierarchy, then transform with code |

These counts overlap: table-derived observations came from retained tables.

First correction: fix the Treasury importer's unit conversion. Saved API values
are already dollar amounts, but the importer multiplies them by one million.
For example, the saved FY2025 total is `7,009,973,667,049.30`; Treasury's published
report shows the rounded equivalent as `7,009,974` million dollars. Regenerate
corrected derived values from saved responses and record the correction
explicitly. Original captures remain intact.
[Treasury September 2025 report, Table 9](https://fiscaldata.treasury.gov/static-data/published-reports/mts/MonthlyTreasuryStatement_202509.pdf)

This phase produces organized data and inspection exports. Fact-checking,
editorial selection and public website design remain later work.

## 2. Normalized data model

Introduce versioned Pydantic contracts with a common core and specialized fields.
Preserve existing `item.v1` records as inputs.

| Record | Contents |
| --- | --- |
| Observation | One source-supplied event, statement, example, figure or quotation; original item IDs, source text, descriptive title and normalized fields |
| Entity | Stable ID, name, type and aliases for people, organizations, agencies, programs, assets and places |
| Group | A case/subject collection, a particular occurrence, or a comparison set; member observations remain individually accessible |
| Series | A dataset metric with classification, units, periods, release/version and links to typed values |
| Source reference | URL, capture/artifact IDs, acquisition type and available chunk, sheet, row, cell or page locator |

Each observation supports:

- Classification: record type and subtype. Recover distinctions currently
  collapsed into `fact`, such as investment, contract, allegation, disclosure,
  denial and regulatory action.
- Dates: separate event date, publication date, reporting period and acquisition
  date; retain original expressions and precision.
- Entities and attribution: distinguish participants, quoted speakers,
  publishers, bylines and the person or organization credited with an estimate.
- Measurements: decimal value or range, original expression, currency/unit,
  scale, period, metric and supplied qualifiers. Distinguish amounts, changes,
  percentages, valuations and cumulative totals.
- Organization: canonical topic categories, original tags, entity links, group
  membership and temporal context.
- Lineage: original record IDs, field input paths, evidence locators,
  normalization version and processing metadata. Missing information is explicit.

Use a small shared classification vocabulary plus topic-specific facets:

- Trump collection: subject/case, participants, activity type, business or asset,
  jurisdiction and date.
- Spending collection: function/subfunction, agency/bureau/account, program,
  object class, fiscal period, mandatory/discretionary classification and
  financial basis.

Keep discovery lanes such as "latest" or "economists" separate from subject
categories. Preserve supplied tags when they do not map cleanly.

Groups organize rather than flatten material. A broad World Liberty collection
can contain separate investments, transactions and regulatory events. Multiple
descriptions of one occurrence can share a group while retaining different
amounts and wording.

## 3. Processing pipeline

### Deterministic preparation

Build adapters for extraction outputs, research packets, source entries and
datasets:

- Read semantic fields buried in `data`, including packet dates, amounts,
  attribution and original item types.
- Standardize existing date and measurement key variants.
- Parse explicit numbers, scales and units using decimal arithmetic.
- Classify blank titles, comments, link lists, captions and source leads as
  separate content roles. Retain them outside the substantive-item view.
- Recover precise evidence locations where saved material permits it. Keep
  coarse locations when exact spans cannot be recovered.

### LLM semantic normalization

Reuse `accounts/fireworks/models/glm-5p3-flash` through the existing Fireworks
integration. Require JSON Schema responses and validate locally with Pydantic.
Fireworks supports this through `response_format: json_schema`.
[Structured-output documentation](https://docs.fireworks.ai/structured-responses/structured-response-formatting)

Process small batches of observations from the same source, capped at ten items
and a bounded input size. Supply existing observations and relevant saved excerpts.

The model assigns types, subtypes and canonical category IDs; identifies entity
mentions and roles; structures ambiguous dates and measurement expressions;
splits compound observations into separately useful items; and selects
related-group candidates from IDs supplied by the application.

Every returned item references its input IDs. Newly structured fields point to
supplied text or fields; the model must not fill gaps from general knowledge.

Cache results using input digests, schema/prompt versions, taxonomy version and
model settings. Save requests, responses, usage and failures. Retry transient
failures; split truncated batches; leave unresolved records available with
processing warnings.

### Dataset normalization

Normalize directly from saved API responses and workbooks:

- Explicit mappings for Treasury MTS, OMB account tables, object-class data,
  historical tables and Treasury tax-expenditure tables.
- Recover worksheet structure from originals where needed, including merged
  headers, formatting, formulas and footnotes.
- For unfamiliar layouts, ask the LLM for a declarative mapping from headers and
  representative rows, then validate and apply that mapping with code.
- Produce typed long-format Parquet values: series ID, period, source value,
  normalized value, unit, financial basis and source locator.
- Preserve every available historical/projected period, negative offsets,
  zeros, totals and parent-child classifications.
- Leave unknown units and ambiguous columns unresolved rather than guessing.

Do not send every row through the LLM or create a prose item for every numeric cell.

### Entity resolution and grouping

Use exact identifiers, explicit aliases and full-text candidate retrieval before
asking the LLM about ambiguous matches:

- Retain distinctions such as Donald Trump versus Donald Trump Jr., and a company
  versus its token.
- Occurrence matching uses participants, action and date context. Sharing a
  prominent person or organization is insufficient.
- Budget series identities include complete classification, metric, period
  basis and source release. Repeated labels such as "Other" or "Operations and
  Support" remain distinct.
- Differing values remain separate observations in a comparison group.
- Ambiguous matches stay separate with reversible related-item links.

Group labels are short descriptions. Group outputs expose members and measurement
variants without generating an article.

## 4. Storage, interfaces and execution

Extend SQLite with normalized records, entity aliases, groups/memberships and
normalization job/run state. Index common filter fields and add SQLite FTS5.
Keep bulk series values in Parquet, queried with DuckDB.

Derived records are versioned. Corrections identify affected inputs and replacement
derivations; originals remain available.

```bash
facts organize --topic all --dry-run
facts organize --topic all --pilot
facts organize --topic all --incremental --max-cost-usd 10
facts items --topic trump-second-term --view grouped
facts items --topic us-federal-spending --view organized
facts export --topic all --organized
```

Extend queries with entity, category, group, date, financial-basis and content-role
filters. Organized exports contain observations, entities, groups, source
references, series manifests, Parquet values and a readable inventory. Manifests
report input coverage, versions, unresolved mappings, costs and pending collection.

Extend the PostgreSQL record-envelope sync to include new types. Local processing
works immediately; remote writes still require explicit sync and
`FACTS_DATABASE_URL`.

Defaults: one organizing process per topic, two concurrent LLM requests,
checkpointed completion, and a $10 initial full-pass API ceiling including
reserved in-flight capacity. Unchanged inputs reuse caches; changed inputs create
new versions. Leave the existing extraction backlog untouched in this pass.

## 5. Delivery and acceptance

1. Inventory coverage, implement contracts/adapters and repair Treasury derivations.
2. Pilot 100 prose observations across both topics/import origins and examples of
   every distinct table layout; inspect inventories and tune structural mappings.
3. Normalize existing observations and all retained tables, resolve entities,
   build groups and export results.
4. Document commands, versions, ambiguities and incremental continuation.

Validate real-corpus fixtures covering:

- Treasury API dollars are not multiplied again; PDF million-dollar expressions
  still convert correctly.
- Zero, null, negative values, ranges and approximate expressions survive.
- Annual amounts, multiyear totals, changes, percentages, outlays, obligations
  and budget authority remain distinguishable.
- Identically named accounts in different hierarchies remain distinct.
- One occurrence's repeated descriptions can group; separate occurrences sharing
  an entity remain separate.
- Every normalized record/value has input/source lineage; unavailable precise
  locators are identified.
- Unchanged reruns make no additional LLM calls or duplicate records; interruption
  resumes unfinished jobs.
- Every input item and retained row is accounted for as content, metadata, a
  source lead or an unresolved input.
- Exports remain usable while some mappings/jobs are unfinished.

Deliver a searchable, versioned collection of small research items and structured
figures, with grouped and original views for later editorial/website work.
