# Research cycle: November 4–5, 2026

This supersedes the "next initiative" choice in
[next_initiative_handoff.md](next_initiative_handoff.md): choice A was executed
end-to-end, with full-queue extraction instead of a five-job batch. Private evidence lives
under `storage/recovery-proof/2026-11-04/` (logs, fingerprints, audits, receipts).

## Gates

| Gate | Result |
| --- | --- |
| Bucket access | `connect.py` refreshed credentials; `SignatureDoesNotMatch` resolved |
| Volume capacity | 2.9 GB of 50 GB before; 8.4 GB after the import |
| Provider restore | Disposable full-corpus copy: backup → mutate → restore; witness reverted, fingerprint matched; drill resources deleted; clean plan |
| Schema | Migrations 1–2 rehearsed on the drill copy, then applied to the real DB (mirror fingerprint unchanged) |

## Extraction (Fireworks GLM 5.3 Flash, $0.15 / $0.50 per M tokens)

- Pre-run snapshot: `80b0040ffbecf88cbb77571a9a7bfe0ecdeda9f2691cc65226e25a44d45aa518`.
- 8,634 responses; 20.1M prompt and 45.5M completion tokens; **≈ $25.78** at the
  configured rates (reservations $243.75; not a provider bill).
- 140,518 new items (152,624 total). Extraction queue: 0 queued. 7,371 succeeded,
  1,643 split, 6 failed (truncated chunks too short to split) and 1,415 deferred.
- Triage (`facts triage`) deferred 642 duplicate OGE 278e chunks (three copies of
  one disclosure), 18 other duplicates, 694 chunks of already-imported OMB workbooks
  and 61 empty captures. Deferral is reversible with `--restore`.
- Lessons now in code: 429/401/403 and connection failures requeue instead of
  becoming uncertain; 429 retries honor `Retry-After`; 12k output tokens and 600 s
  reads cut truncation splits. The account limit is 90k generated tokens/min; seven
  workers used about half of it without 429s.
- The Budget Appendix FY2027 (≈ 8k+ items) and the OGE disclosure transaction list
  dominate the new volume. Both are structured documents; deterministic parsers would
  be cheaper and more exact than LLM itemization for future refreshes.

## Organization, delivery and import

- `organize --cached-only`: 150,999 observations, 67,840 series and 1,668 source
  references. New extracted items are **deterministic** records (`kind` mostly
  `statement`, `semantic_mapping_unresolved` not yet applied). Semantic enrichment
  and LLM grouping are queued: 14,556 semantic and 14,733 grouping jobs.
- Audit passed (54,868 artifact references, 220,507 contracts, 907,302 values).
- Snapshot `f76f7de89980713de1eacceb048541b419965de7c9c1bf93a0b8c33441b2158f`:
  56,147 files and 6.3 GB. Delivered to S3 (55,020 objects verified; 42,471 new).
  Independently received, restored and audited: the active digest matched.
- Receipt-linked import into the real dev Postgres: receipt
  `192b4cb8d5b2320a97e4fe03542f8482f592c127269f8d47526053072986105e`,
  2,270,784 mirrored records, and the read model rebuilt (152,667 observations,
  197,393 measurements, 171,750 entity mentions, 907,302 series values). A repeat
  import wrote 0 records and reused the receipt.

## Semantic organization and second publication (November 5–6)

- `organize --incremental --max-cost-usd 150 --workers 6` (detached) finished on
  November 6 at 09:25 UTC for **≈ $56.16** (semantic $32.16; grouping ≈ $24).
  Coverage: 142,995 items semantic, 589 semantic-unresolved (validation rejections
  such as uncited expressions; their deterministic records remain), 9,040 deterministic.
  609 semantic jobs failed terminally. 14,733 grouping jobs left `queued` by the
  earlier cached-only pass are superseded contexts, not pending work.
- Result: 152,647 observations; 132,623 groups, 22,078 with several members. Trump topic
  categories led by disclosures (31k), trading (12.7k), regulatory actions, crypto
  and business deals.
- Audit passed (148,037 artifact references). Snapshot
  `9c04bbbf377d132611730b7bdf9cda9d6808bc0140c8ba9e87b546f13bf55359` (149,531 files,
  10.1 GB). An `EndpointConnectionError` interrupted delivery once (around a host
  restart); a resumed delivery verified all 148,212 objects. The independent
  receive/restore/audit matched active digest `07fdb97d…`.
- Linked import receipt `1afeaaeca9a44ccb5816361224f7b2a2ef179f11cc8dda6ffe62df75982f199f`:
  3,121,356 mirrored records (130,880 superseded removed), read model rebuilt
  (154,315 observations, 268,678 measurements, 232,246 entity mentions, 44,924
  entities, 350,340 group memberships, 907,302 series values). A repeat import wrote 0.
  Database 7.1 GB; volume 11.1 of 50 GB.

## Next

1. Replace LLM itemization of the Budget Appendix and OGE 278e transactions with
   deterministic table parsers.
2. Make the collector root configurable (`FACTS_ROOT`) and let the import gate accept
   any verified off-machine destination (S3 is optional off-site backup only).
3. Grouping runs one FTS query per observation (about 1.5 h per pass at this size);
   cache candidate searches by input digest before the corpus grows further.
4. Review the 589 semantic-unresolved items and 6 failed extraction chunks; decide
   whether a targeted `--retry-failed` is worthwhile.
