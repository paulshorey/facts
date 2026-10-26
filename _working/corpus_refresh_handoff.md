# Corpus refresh and development storage handoff

Original drill date: 2026-10-17. Implementation plan:
[`corpus_refresh_handoff_plan.md`](corpus_refresh_handoff_plan.md).
Code is delivered through [Facts PR #8](https://github.com/paulshorey/facts/pull/8).

## Current recovery proof: October 25

Read-only verification was repeated on the actual saved corpus using merged main
`22379379d2b5d9af5d49dac81457a8584931f7bc`, including PR #9's complete inventory.
The collector was idle. `facts.audit --current` passed with **12,477 artifact
references**, 79,989 active contracts, 907,302 table values and 35,508 dataset rows.
All original table fingerprints and the active-record digest still match the
October 17 baseline. No provider/model requests for new content were submitted.

`facts-handoff verify` passed for snapshot
`2bb06f6635185c5ed105b8f861a7a44a38eb9f859b3b6f37e245b271cf4fcd3c`,
including all 13,481 files and the stricter historical/follow-up artifact inventory.
The local bundle remains in place. This is current proof of the saved corpus and
local snapshot; the October 17 cloud transfer/import below is historical. A fresh
isolated local restore under the follow-up code also restored all 13,481 files
and passed the same 12,477-reference audit; its original tables and active digest
match the current corpus. The temporary drill directory was cleaned up.

Current read-only PostgreSQL checks passed with TLS. The current mirror still
references the same linked snapshot and receipt. Database size is 1,000,445,631
bytes; `facts.records`, including its indexes, occupies 992,419,840 bytes, of which
221,233,152 bytes is indexes. These SQL sizes are **not** volume-used/free-space
measurements. No import was run during this follow-up.

The exact Facts project denies the explicitly selected WebArts profile, documented
fallback profile and connected Railway control-plane client. The saved S3
credential returns `SignatureDoesNotMatch`. Current volume inventory/capacity,
provider backup schedules, remote object integrity and provider restore have
therefore **not** been reverified. Restore access first; do not infer current
cloud recovery proof from the old successful drill or SQL connectivity.

The provider backup restore and bounded paid extraction remain gated. The normal
volume-backup restore changes the source mount; it must not be exercised in place
on the real dev database. Establish a supported disposable restore target first.

Private current reports: `storage/recovery-proof/2026-10-25/merged-audit.json`,
`merged-snapshot-verify.json`, `comparison.json` and `database-readonly.json`.
`final-proof.json` records the exact follow-up code revision; `final-audit.json`
and `final-restored-audit.json` retain the final corpus/restore checks.
Implementation follow-up: [`recovery_followup_plan.md`](recovery_followup_plan.md).

## Corpus state

The merged corpus was rebuilt with observations-v6 and tables-v4 using saved
responses. No research tasks, collection requests or model requests were submitted.
All ten original table fingerprints match the baseline, including extraction jobs
and the original attempt journal. The estimated historical API cost remains
$1.12282025; this refresh added zero API cost.

| Quantity | Before | After |
| --- | ---: | ---: |
| Original items | 12,106 | 12,106 |
| Sources | 1,598 | 1,598 |
| Captures | 2,745 | 2,745 |
| Dataset tables | 88 | 88 |
| Active observations | 10,571 | 10,571 |
| Source entries | 1,578 | 1,578 |
| Series definitions | 67,718 | 67,840 |
| Inputs with saved semantic enrichment | 3,047 | 3,047 |
| Queued original extractions | 5,116 | 5,116 |

A second cached-only pass produced identical active record content and inventory.
The structural audit validated 79,989 active contracts, 907,302 Parquet values,
series/value linkage, all 35,508 dataset rows, and referenced source/capture/file
identities. Row disposition remains 33,474 normalized, 1,895 metadata and 139
unresolved. Fourteen tables have unresolved rows; 19 original inputs retain a
semantic-unresolved outcome. These states remain inspectable.

Refreshed grouping contexts leave 90 grouping batches queued without inference.
Exact reusable cached results are applied; incompatible/changed contexts remain
pending. Prior grouping history is retained. Current occurrence-group count is
3,086, with 19 comparison groups and 31 subject groups. None of these counts
establish unique incidents, unique accounts or additive spending totals.

## October 17 artifact handoff (historical)

The pre-refresh bundle restored 12,993 files into an isolated directory. The
post-refresh snapshot is:

```text
2bb06f6635185c5ed105b8f861a7a44a38eb9f859b3b6f37e245b271cf4fcd3c
```

It maps 13,481 original relative file paths to 12,558 unique content objects,
representing 2,681,089,227 logical bytes. It includes a consistent SQLite catalog,
raw evidence, provider task identity files, saved model outputs, normalized tables,
topic definitions and exports. Credentials, transient locks and prior backup
directories are excluded. Verification checks hashes, SQLite integrity and the
inclusion of artifacts referenced by retained historical catalog rows.

Actual acceptance checks passed:

- The then-current bucket smoke test observed conflicting conditional writes being
  rejected. This was an observation, not a Railway-level atomicity guarantee.
- An incomplete real upload was interrupted before manifest publication.
- The resumed upload reused 3,660 verified objects and uploaded 8,898 remaining
  objects, then verified and published the manifest.
- The entire snapshot was retrieved into `/tmp/facts-dev-received`, independently
  verified, and restored into `/tmp/facts-dev-restored`.
- The restored catalog passed the contract, reference and table-link audit.

Local source/bundle state remains in `storage/` and `storage/handoffs/`. Detailed
private reports are under `storage/organized/handoff/`, including `before.json`,
`after-first.json`, `after-second.json`, `refresh-validation.json`, `exports.json`,
`post-refresh.json`, `upload-interruption.json`, `remote-delivery.json`,
`remote-retrieval.json`, `remote-restore.json` and `restored-audit.json`.
The completed per-destination artifact receipt is under
`storage/handoffs/receipts/<snapshot>/`. Temporary restore directories are drill
outputs; the local bundle and remote manifest are the durable handoff references.

## October 17 development infrastructure record

The [Facts Railway project](https://railway.com/project/f463753e-8db6-4d2b-b8d1-127e9ab5d59d)
has a development environment, a PostgreSQL 18.6 service with a persistent data
volume, and the `facts-evidence` bucket in `iad`. Non-secret resource identities
and project-owned IaC are in `.railway/`. No Events resources are used.

Live verification passed for project/workspace/environment identity, the exact
database volume attachment, active successful database deployment, clean IaC
plan, bucket identity/region, PostgreSQL TLS/read-only SQL, and daily/weekly
database backup schedules. The `.railway/connect.py` helper writes only owner-only,
ignored project-local connections after verifying these identities; no credentials
are recorded here or in Git. See [the infrastructure runbook](../.railway/README.md).

Provider-managed volume-backup restoration has not been exercised. Railway does
not guarantee conditional-write atomicity or offer object lock, versioning,
create-only/no-delete credential scopes, or bucket backups. Conditional headers
and hash checks detect some errors; they do not make the bucket immutable.
Keep the independent local bundle. See the [Railway staff clarification](https://station.railway.com/questions/railway-buckets-consistency-and-create-d83ae29c).

## October 17 PostgreSQL acceptance (historical)

The exact remotely restored snapshot was imported into the dedicated development
database twice. Both transactions committed 802,981 synchronized envelopes with
zero deletions. These include source rows, retained versions, links, groups,
memberships, coverage, jobs and run metadata; they are not 802,981 independent
facts. The second delivery wrote zero changed envelopes and reused the first
receipt. The database contains one import receipt, and its current-mirror pointer
references that receipt.

```text
Import receipt:
1314273df8de0f0026248e3ec1c0bbf19107e647e8d9d483f52118901f827b1c

Logical catalog digest:
cc874f26b043cac22ae8e40545a5648cf566c254865ceb1e7c0b983560eb2be5
```

Read-only SQL confirmed all per-kind counts, the receipt's catalog digest and
artifact-manifest identity, and exact representative source/capture/item/dataset/
table/observation/series payloads against the restored catalog. TLS was enabled.
Detailed reports are `postgres-import-1.json`, `postgres-import-2.json`,
`postgres-verification.json` and `postgres-repeat-verification.json` under
`storage/organized/handoff/`. The post-commit receipt was also retained under the
collector's `storage/deliveries/postgres/<target-id>/` after the restore drill.

The reusable importer keeps schema creation, staging, reconciliation and receipt
creation in one transaction. It skips unchanged upsert writes, preserves unrelated
database kinds/schemas and records local delivery only after commit. Destructive
failure/rollback/disconnect tests used disposable local PostgreSQL databases.

## Validation and next work

The October 17 implementation suite passed 100 tests; PR #9 increased it to 105.
The October 25 recovery follow-up passes 127 tests, including real isolated PostgreSQL rollback,
disconnect/retry, receipt atomicity and lost-local-receipt recovery. Tests also
cover path traversal, unsafe bundle placement, missing retained evidence, object
corruption, interrupted delivery, restore integrity and cached-only request
prohibition/cache retention. TypeScript checks, Python compilation, dependency
checks and Git whitespace checks pass.

The follow-up also covers frozen legacy inventories/new explicit inventories,
snapshot discovery, explicit quarantined object/manifest repair, exact S3 receipt
identity, linked-mirror downgrade rejection, cross-process writer exclusion,
persisted extraction reservations and storage-failure dispatch stop. Those repair
and failure cases are controlled fixtures, not fresh operations on the Railway
bucket. Provider restore and the priced extraction batch remain unperformed.

The next collection milestone is a bounded batch from the existing extraction
backlog, followed by a supervised single-host runner with delivery retries.
Hosted journal persistence and any recurring schedules require that separate
implementation. A website, editorial workflow and distributed queue are outside
this handoff.
