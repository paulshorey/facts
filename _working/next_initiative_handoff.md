# Next initiative handoff

Prepared October 29, 2026 for the next facts.news task.

## Recommended next outcome

Complete one small research cycle from the saved extraction queue through verified
storage delivery. This will prove how new content, reservations, normalization
and recovery work together in the merged framework.

Choose **A** if Facts provider access can be restored. If access still depends on
the owner, choose **B** and make the saved research easier to inspect immediately.
**C** is another useful offline engineering task. Do not spend the whole next task
repeating failed authentication attempts or building another infrastructure layer.

| Choice | Initiative | Concrete deliverable | Can start without provider access? |
| --- | --- | --- | --- |
| A — recommended operational path | Restore access, prove provider recovery, run one bounded extraction batch | A disposable restore report and a new verified research snapshot from at most five jobs | Only the local checks and preparation |
| B — recommended offline fallback | Produce useful itemized research packs from the saved corpus | Two local data packs and a coverage catalog that another agent can query and extend | Yes; no new model requests |
| C — focused offline engineering | Resolve a small set of saved financial-table mapping failures | Parser/mapping fixes, representative fixtures and improved row coverage | Yes; use deterministic code and cached outputs |

The next user should select the initiative. This handoff does not start a paid
batch, authorize an unattended runner, change topic scope or create another task.

## Starting state verified for this handoff

- Repository: `/Users/pshorey/git/facts`.
- GitHub `main`: `1d494fed87a97ed2830646a70f9082376c9dfdaf`.
- PR [#10](https://github.com/paulshorey/facts/pull/10) merged on October 26. Its
  integrated feature head was `f45f348c5b5cf9cb633332157fa97d6dcc6de0f6`.
- PR [#11](https://github.com/paulshorey/facts/pull/11) keeps ambiguous provider
  outcomes `running`, preserves reservations and stops dispatch. Explicit failed-job
  retry must not resubmit those uncertain requests.
- PR [#12](https://github.com/paulshorey/facts/pull/12) holds the collector writer
  lock from import catalog opening/verification through PostgreSQL commit and local
  receipt persistence. Keep this protection intact.
- There were no open application PRs when this handoff was prepared. Fetch again
  at task start; do not resume a merged feature branch.
- The integrated review records 131 passing tests with zero skips, including
  disposable PostgreSQL and process-exclusion regressions. That is the October 26
  review result, not a new full test run performed for this documentation task.

Read-only local counts were checked on October 29:

| Saved material | Count |
| --- | ---: |
| Original items | 12,106 |
| Sources | 1,598 |
| Captures | 2,745 |
| Dataset tables | 88 |
| Active observations | 10,571 |
| Active source entries | 1,578 |
| Active series definitions | 67,840 |
| Extraction jobs queued | 5,116 |
| Extraction jobs succeeded / split | 393 / 1 |
| Extraction jobs failed / running | 0 / 0 |
| Grouping batches queued | 90 |

Counts overlap and are not unique incidents or additive spending totals. Use
organized financial values: retained legacy Treasury item values have a known
million-fold scaling error that the organization layer corrects from saved rows.

Both topic files currently end on **October 16, 2026**. Do not silently extend
that cutoff: changing topic payloads changes extraction identities and can enqueue
a second pass over saved captures. A later recency campaign needs its own explicit
scope and incremental-work plan.

## Evidence and recovery references

Read `AGENTS.md`, `README.md`, the selected topic JSON, and these files:

- [Current corpus handoff](corpus_refresh_handoff.md): dated operational evidence.
- [Recovery follow-up plan](recovery_followup_plan.md): recovery sequence and gates.
- [Railway runbook](../.railway/README.md) and
  [resource identities](../.railway/project.json): exact Facts ownership/targets.
- [Normalization plan](content_normalization_plan.md) and
  [initial normalization report](../research/normalization_handoff.md): data model
  and historical limitations.

The last complete private local proof is under
`storage/recovery-proof/2026-10-25/`. `final-proof.json` identifies code revision
`70524e1`; it verified and restored 13,481 files and audited 12,477 artifact
references. Original table fingerprints and active contents matched the baseline.
This predates the PR #11/#12 fixes; repeat the relevant checks on current `main`
before a new operational cycle. The October 17 cloud upload/import is historical.

Retain the local bundle at `storage/handoffs/`. Its known snapshot is:

```text
2bb06f6635185c5ed105b8f861a7a44a38eb9f859b3b6f37e245b271cf4fcd3c
```

Last provider observations, **October 25; not refreshed for this handoff**:

- The exact Facts project denied the WebArts profile, documented fallback and
  Railway connector. S3 returned `SignatureDoesNotMatch`.
- The saved database connection still worked over TLS. Its current receipt then
  referenced the snapshot above. Database size was 1,000,445,631 bytes; actual
  volume capacity/usage/free space was not verified.
- A provider backup restore had not been exercised. The controlled local restore
  and database integration tests do not replace that drill.

## Choice A: prove one bounded research and delivery cycle

### 1. Establish access and a fresh baseline

Start on current `main`, inspect local changes/process ownership, then use an
isolated feature branch for any changes. Use `.venv/bin/python` and `.venv/bin/facts`.
Keep one writer per collector root/host and leave the source corpus intact.

Repeat the read-only checks and save dated reports in ignored storage:

```bash
.venv/bin/python -m facts.audit --current
.venv/bin/facts-handoff verify --snapshot 2bb06f6635185c5ed105b8f861a7a44a38eb9f859b3b6f37e245b271cf4fcd3c
.venv/bin/facts-handoff list
```

Read `~/.agents/README.md` and the Railway skills before provider work. Select the
intended profile explicitly and verify the exact project/workspace/environment
against `.railway/project.json`. Do not treat a failed account-wide identity call
as proof that a scoped token is invalid. If the exact project is still denied,
ask the owner which profile now owns Facts or to refresh the registered access.
Never request pasted tokens, reuse another project's secret or create a replacement
Facts project to bypass the missing access.

Once authorized access works, refresh the owner-only ignored local connections
through the project helper. Verify the configured bucket's manifest/object access,
the original database volume ID/attachment, current capacity and backup inventory.
SQL database size alone does not establish disk headroom. Record actual results;
do not promote an old receipt or successful HTTP response into recovery proof.

### 2. Exercise provider recovery on a disposable target

The normal volume-backup restore replaces a service's mounted volume. Never test
it in place on the real Facts dev service. Inspect the current provider API/docs
and establish a supported disposable workflow first.

A suitable drill can seed a separately identified disposable database with the
known catalog, take its provider backup, change a drill-owned witness row, restore
that backup, and verify the witness reverted. Compare catalog counts, receipt and
representative evidence/series links before/after. A small-fixture drill must be
labeled as such; it does not prove full-corpus recovery. Do not assume daily/weekly
volume schedules provide the archive needed for PITR-to-new-service recovery.

Record backup, target service and target volume IDs; completed deployment; SQL/TLS
checks; witness result; counts; and cleanup. Remove only drill-owned resources.
Avoid whole-project applies while temporary resources are outside the IaC ownership
definition. Verify the original attachment and clean plan after cleanup.

### 3. Run exactly one small extraction batch

Only after the access, capacity and provider-restore gates pass, verify current
Fireworks model pricing and configure `FACTS_EXTRACT_INPUT_RATE` and
`FACTS_EXTRACT_OUTPUT_RATE`. Rates are USD per million tokens. The code's budget
is a conservative reservation at those declared rates, not a provider billing cap.

Default proposal for the task: `trump-second-term`, at most five existing queued
jobs, one worker, maximum USD 1 in configured-rate reservations. The next user
can choose the spending topic instead. Keep the existing October 16 cutoff.

```bash
.venv/bin/facts normalize --topic trump-second-term --limit 5 --workers 1 --max-cost-usd 1 --existing-only
```

Do not add `--retry-failed`, run `all`, loop the command or reissue saved Parallel
deep-research tasks. A timeout/lost response or persistence error stops the cycle;
retain reservations and inspect uncertain `running` jobs manually. Save job IDs,
outcomes, usage/reservations and item-count deltas without recording credentials.

### 4. Normalize and deliver once

After successful extraction, run cached-only organization and regenerate organized
exports. Compare original-data deltas to the bounded batch and retain previous
versions. Create and verify a new backup, deliver it to the configured bucket and
retain its verified destination-specific receipt. Independently retrieve/verify
the new snapshot. Never use the old snapshot's receipt for newly collected data.

If a development mirror update is included, import from the restored new snapshot
with `--snapshot`, `--bundle` and the matching S3 `--artifact-receipt`. Keep the
importer lock through validation, remote commit and local receipt. A mirror retry
must not launch another extraction or crawl. Repeat only delivery/import to prove
receipt reuse and zero logical duplication.

### Acceptance

- Current-main local verification and an isolated provider restore are recorded.
- Exactly one bounded batch is accounted for, including failures/uncertain jobs.
- New items retain source/capture/evidence links; existing data remains available.
- The new snapshot restores independently; any mirror receipt identifies it.
- The handoff includes exact revisions, IDs/counts, costs/reservations and remaining
  limitations. No unattended schedule or website is introduced by this task.

## Choice B: make the saved research usable offline

Use existing organized queries/exports to produce two itemized data packs, without
new inference or provider access. Do not build another general query layer when
`facts items`, `facts series` and export utilities already supply it.

Suggested packs: Trump-family/associate financial activity grouped by saved
category/entity, and federal spending grouped by fiscal period/category/basis.
Write private outputs under `exports/` and small scripts/query definitions in Git.

Deliver JSONL/CSV plus a concise local index/preview. Preserve record/version IDs,
source URLs, capture/evidence pointers, original expressions, units, fiscal periods,
financial basis, source qualifications and field lineage. Separate source entries,
observations and series. Reuse saved titles/details; do not write editorial essays,
invent totals or turn supplied allegations into conclusions.

Include coverage/backlog counts, reproducible commands and several useful query
examples per topic. Verify representative rows trace to local evidence and repeat
generation is deterministic. This is data exploration/packaging, not a public site
or a new fact-checking/editorial system. No source-catalog rewrite, paid API call,
cloud import or scope extension is needed.

## Choice C: improve saved financial-table coverage offline

The last audit reports 14 tables with unresolved rows, totaling 139 unresolved rows
across 88 dataset tables. Inspect the current `organized_tables` payloads and row
coverage Parquet files before choosing work; these counts are a starting point.

Select two or three representative mapping failures. Trace them to retained
workbooks/API rows, repair deterministic parsing/mapping and add bounded fixtures.
Preserve account dimensions/hierarchy, original rows, units and period/basis
distinctions. Do not guess missing semantics or modify captured bytes/legacy items.

Run cached-only organization under the writer lock. Report exactly which rows
moved from unresolved to normalized/metadata, retain warnings for ambiguous rows,
verify scaling and series identity, regenerate exports and verify a local backup.
No new model requests or remote mirror import are part of this offline choice.

## Later initiative: a supervised single-host runner

Build this after the bounded research/delivery cycle is proven. It needs durable
local journal/evidence paths, finite run limits, current-rate reservations, process
supervision, status reporting and graceful shutdown. Treat extraction, normalization
and delivery as separate resumable phases. A storage/delivery failure must pause
the run without another crawl or paid submission. Uncertain provider outcomes
remain manual recovery. Do not build a distributed queue or high-frequency agent
scraping dispatcher as the next step.

## Ready-to-use next task prompt

> Read AGENTS.md and `_working/next_initiative_handoff.md` in
> `/Users/pshorey/git/facts`. Begin from current main, preserving private storage.
> Follow choice A: restore exact Facts access, verify live volume capacity, prove
> a disposable provider backup restore, then run one five-job, one-worker,
> USD-1-reservation extraction batch from the existing `trump-second-term` queue
> at verified current model rates. Keep the existing topic cutoff. Complete cached
> organization, export and a verified new snapshot/delivery; use only its receipt
> for any authorized dev mirror import. If owner access is unavailable, report the
> precise blocker and propose choice B; do not repeat authentication attempts or
> start paid collection. Keep operational evidence private and deliver any code
> changes through a reviewed feature PR. Do not build a website or unattended runner.

For an entirely offline next task, replace the choice-A sentence with:

> Follow choice B. Produce reproducible itemized data packs and a coverage catalog
> from the saved normalized corpus using existing query/export utilities. Use no
> paid requests, provider changes, source-catalog rewrites or scope extensions.
