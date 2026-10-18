# Refresh the merged corpus and establish a recoverable development handoff

Approved for implementation by the user on 2026-10-17. Start from merged main;
deliver code, tests and operational evidence through a feature PR. This milestone
does not build the website, impose editorial gates, enable schedules or migrate
the collector to distributed workers.

## 1. Preserve and inventory the existing collection

- Create a consistent SQLite snapshot and record a manifest of retained storage
  and export files. Exclude live WAL/SHM, transient locks, credentials and prior
  backups from recursive backup discovery.
- Copy retained bytes into a portable content-addressed bundle, recording original
  relative paths, SHA-256 digests, sizes and the snapshot's record inventory.
- Verify every object and restore into a fresh isolated directory. Retain the
  originals, extraction backlog, topic definitions and provider task identities.
- Backups publish atomically and never declare success before manifest/object
  verification and SQLite integrity checks finish.

## 2. Refresh derived data without new inference

- Add an explicit cached-only organization option: deterministic table/field
  transformations plus replay of saved semantic/grouping responses. It must not
  submit provider requests, require provider credentials or discard valid semantic
  enrichment merely because new inference is disabled.
- Rebuild with observations-v6 and tables-v4. Compare original catalog tables to
  the pre-refresh snapshot; validate contracts, coverage and artifact references.
- Regenerate immutable topic exports. Run a second cached-only pass to establish
  repeatability and retain before/after counts and unresolved inputs as evidence.

## 3. Make the refreshed handoff portable and resumable

- Reuse the same bundle for filesystem and S3-compatible artifact delivery.
  Object keys contain content hashes. Existing objects must match their declared
  digest and size; conflicting content fails rather than overwriting evidence.
- Save a receipt only after all object bytes and the manifest have been verified.
  Resume by verifying/reusing existing objects; a failed delivery leaves the
  previous completed receipt usable. Keep endpoints/credentials in environment
  variables or owner-only ignored configuration; never in tracked manifests.
- Add restore/verify commands that can retrieve the entire snapshot from its
  remote manifest into an isolated directory, validate hashes and open SQLite.
- Use a dedicated facts development target, selecting the Railway workspace and
  resource identities explicitly. Do not use Events resources. Adopt any created
  resources into reviewed project-owned IaC and verify resulting provider state.

## 4. Verify the PostgreSQL development mirror

- Reuse the existing transactional full-catalog mirror. Add a durable import
  receipt with catalog digest/counts, synchronized kinds and artifact-manifest ID;
  record the local delivery receipt only after the remote transaction commits.
- Verify the development target, schema and connection without exposing secrets.
  Compare remote/local envelope counts and representative evidence/series links;
  repeat delivery and verify no logical duplicates or lost locally retained history.
- Exercise failure, interruption, empty/missing kinds and recovery using disposable
  local PostgreSQL databases. Run the same handoff from the restored bundle.
- Missing remote credentials/target identity do not block local implementation,
  restore drills or fixture tests. Report remote operations as pending rather than
  claiming cloud durability based on local tests.

## 5. Delivery and acceptance

- CLI help and README document refresh, backup, upload, verify, restore, sync and
  recovery, including storage ownership and the mirror's deletion semantics.
- Meaningful tests cover cached-only request prohibition and cache retention,
  object corruption/missing bytes, path traversal, upload retry, immutable manifests,
  interrupted backup publication, restore integrity and transaction receipts.
- Run the full relevant suite, compilation and whitespace checks; keep raw data,
  generated exports, model outputs and runtime reports ignored.
- Execute the real local refresh and restoration drill. Complete remote object
  and PostgreSQL delivery when the explicitly selected credentials are available.
- Publish a tracked sanitized handoff pointing to private detailed verification
  reports, commit/push the feature branch and open a reviewable PR.

After this milestone, resume a measured extraction batch and add a supervised
single-host runner. Those collection/scheduling stages remain separate so a
storage failure cannot trigger recrawling or repeated paid inference.
