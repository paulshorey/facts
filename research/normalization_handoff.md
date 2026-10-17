# Organized research handoff

Completed 2026-10-17 against the existing collection (research cutoff 2026-10-16).
This pass organized saved content; it did not drain the collection/extraction
frontier or launch another deep-research campaign.

The approved plan is `_working/content_normalization_plan.md`, committed directly
to main as `00072d8`. Implementation is on `codex/content-normalization`, stacked
on collector PR #1 through https://github.com/paulshorey/facts/pull/2.

## Results

| Collection | Original item inputs | Organized observations | Headline/source entries | Structured series |
| --- | ---: | ---: | ---: | ---: |
| trump-second-term | 2,543 | 1,788 | 785 | 0 |
| us-federal-spending | 9,563 | 8,783 | 793 | 67,718 |

All 12,106 original items have coverage records. The organized layer has 43
additional observation parts after splitting compound inputs. Counts are source
observations, not counts of unique events or additive expense totals.

The semantic pass covered 3,054 prose observations and 12 extracted headline
entries. 3,036 prose observations received LLM metadata; 18 prose observations
and one headline retain a deterministic fallback with `semantic_unresolved`.
Their original content, references, model responses and retry context remain
available. No fact-checking or editorial approval was required to save them.

The 88 retained tables produced 907,302 value records in Parquet: 906,127 have a
typed decimal source value and 132,136 have a unit-normalized value. Other values
retain their original units/expressions, including 1,175 source markers that are
not silently converted to zero. Many account/object-class tables do not state
units in their retained headers; these have null conversion/unit fields rather
than guessed dollar scales. Constant-dollar values remain distinguishable.

Every retained table row has a disposition: 33,474 transformed rows, 1,895
metadata rows, and 139 unresolved rows across 14 tables. Unresolved rows and their
source coordinates are retained in the original and row-coverage Parquet files.

There are 2,859 occurrence/observation groups, 31 subject collections and 19
comparison groups. Groups retain their members and differing figures; they are
not assertions that each group is a unique incident. Membership versions, related
links and prior group definitions remain available in the catalog.

## Open the data

Human previews:

- `exports/trump-second-term/organized-v1/inventory.md`
- `exports/us-federal-spending/organized-v1/inventory.md`

Each topic's `organized-v1/latest.json` identifies its immutable snapshot. A
snapshot includes `observations.jsonl`, `source_entries.jsonl`, `entities.jsonl`,
`groups.jsonl`, `series.jsonl`, `sources.jsonl`, `references.jsonl`,
`datasets.jsonl`, `coverage.jsonl`, JSON Schemas and copied value/row-coverage
Parquet files. The inventory previews 200 substantive observations; JSONL contains
the complete organized collection.

Portable value and row-coverage paths are relative to the snapshot. Original/raw
capture pointers are collector-root-relative private provenance; original bytes
are not bundled in the portable export. References include acquisition type,
capture state, source title, available publication date and source locators.

```bash
.venv/bin/facts organize --topic all --dry-run
.venv/bin/facts items --topic trump-second-term --view grouped --contains Powerus --limit 3
.venv/bin/facts items --topic us-federal-spending --view organized --category defense --limit 10
.venv/bin/facts series --topic us-federal-spending --basis net_outlays --limit 10
.venv/bin/facts export --topic all --organized
```

`source_fields` preserves supplied structured fields outside the common model.
Financial bases are canonicalized while `original_basis` remains available.
Dates retain source expressions, precision and fiscal/calendar distinctions.
LLM JSON pointers are relative to the selected input object in the saved job
input artifact; find that object by the observation's original input ID.

## Correction and validation

The initial Treasury importer incorrectly multiplied API dollar values by one
million. The importer is corrected for future collection, and 1,973 legacy
observations are rebuilt in the organized layer with `treasury-api-dollars-v1`.
Original `item.v1` records and legacy dataset unit labels remain unchanged for
lineage; use organized measurements/series for downstream work.

The live FY2025 total-outlays regression is `7009973667049.30` USD, read from the
saved API data rather than summed from overlapping accounts.

Validation completed:

- 35 automated tests, Python compilation and `git diff --check` passed.
- All 79,867 active records validate against the observation/series contracts.
- Every declared observation field/entity/date/measurement/attribution pointer
  resolves against its saved input; no invalid lineage pointers were found.
- All nine original catalog tables remain identical to the pre-organization
  backup, including all items, captures, datasets and extraction job states.
- All 8,819 referenced original objects exist and match their SHA-256 filenames
  (412,900,082 bytes checked).
- The final adapter/provenance pass reused saved model outputs and made zero API
  requests, at zero additional inference cost.
- All 176 exported table/row-coverage Parquet files resolve within their portable
  snapshots. A restored SQLite snapshot passes `PRAGMA integrity_check` and
  contains all 79,867 active records:
  `storage/backups/catalog-organized-20261017T165632Z.sqlite`.

Recorded API cost estimates total approximately $1.12 across pilots, repairs and
the full pass, including conservative reservations for interrupted requests.
This is a token-based estimate, not a provider billing statement. Default
concurrency is two; the full pass used eight after measuring provider latency,
under the same $10 per-invocation reservation ceiling.

## Continue safely

```bash
# Changed/new saved inputs reuse the same pipeline and caches.
.venv/bin/facts organize --topic all --incremental --max-cost-usd 10

# Explicitly retry terminal semantic inputs after inspecting/fixing their cause.
.venv/bin/facts organize --topic trump-second-term --retry-failed --max-cost-usd 10

# Record-envelope counts only; no remote writes.
.venv/bin/python -m facts.postgres_sync
```

Job counts include historical failed attempts/versions. The input coverage and
row-disposition records describe the current organized output. No organizing
jobs remain queued/running. The original 5,116 extraction chunks remain pending
by design, and no continuously running collector or recurring automation exists.

PostgreSQL sync includes normalized record versions, jobs, entities, aliases,
groups/history and table manifests. Remote application is unverified because no
development `FACTS_DATABASE_URL` has been supplied. Sync does not upload original
or Parquet files. Back up the full `storage/` directory and exports together.

Private reports are under `storage/organized/`: `source-preservation-check.json`,
`original-artifact-check.json`, `contract-and-treasury-check.json`,
`final-lineage-check.json`, `export-report.json`, `postgres-sync-dry-run.json`,
`portable-export-and-backup-check.json`,
and the versioned run records in `runs/`.
