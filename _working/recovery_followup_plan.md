# Recovery proof and safeguards after PR #8

2026-10-25, based on merged main 2237937. Keep the collector idle until the saved
corpus and snapshot pass the stricter inventory. Do not start an unattended runner.

1. Run the merged read-only audit and verify snapshot 2bb06f66…; compare original
   tables and active records to the prior baseline. Update the tracked handoff
   with the new artifact-reference count and explicit dates/code revisions.
2. Freeze legacy manifest inventory rules and record inventory explicitly in new
   manifests. Add manifest discovery and explicit, quarantined repair from a
   verified local bundle. Describe S3 as a verified replica, not immutable storage.
3. Require snapshot-linked CLI imports and the configured S3 destination's receipt;
   reject accidental downgrade of an already linked mirror. Keep tests isolated.
4. Add a collector-wide advisory lock shared by backup and all supported mutating
   entrypoints. Add an explicit extraction budget with conservative per-request
   reservations persisted before submission. Unknown interrupted submissions stay
   pending manual recovery; storage failure cannot launch another paid batch.
5. Verify exact Railway project/volume identity and current usage before imports.
   Run a provider backup restore into a disposable target only when the provider
   exposes a safe restore-to-new-target path and authorized access works. Never
   restore a volume backup in place on the real dev service.
6. Only after the provider restore passes, run an explicitly bounded extraction
   batch for one topic/host, with configured current model rates and a dollar
   ceiling. Back up again before any subsequent mirror import.
7. Test the recovery paths, publish a feature PR and record exactly which checks
   were executed locally/cloud-side and which remain blocked.

Current discovery: the saved corpus is present. Merged-code audit and snapshot
verification pass, with 12,477 artifact references. Both the WebArts profile and
Railway connector deny the exact Facts project; the local database credential
still permits TLS/read-only SQL. Provider restore and paid extraction therefore
remain gated while local implementation/verification proceeds.
