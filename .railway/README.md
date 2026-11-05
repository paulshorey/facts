# Facts development storage

The dedicated [Facts project](https://railway.com/project/f463753e-8db6-4d2b-b8d1-127e9ab5d59d)
owns one development PostgreSQL service, its data volume, and the `facts-evidence`
Railway Bucket. Non-secret immutable resource identities are in `project.json`.
There is no application deployment, production environment, website or collector
schedule. Events resources and credentials are unrelated.

The database uses Railway's official PostgreSQL 18 TLS template in
`us-east4-eqdc4a`. Its template-managed mount is `/var/lib/postgresql/data`.
The bucket is in `iad`; preserve its region and identity. Objects use the
`facts-v1/objects/<prefix>/<sha256>` and `facts-v1/manifests/<sha256>.json` keys.
This client requests conditional creation and verifies bytes. Railway gives no
separate atomicity/consistency guarantee for these requests. The bucket credential
can overwrite/delete content, and object lock, versioning and bucket backups are
unsupported. Retain independent local snapshots and manifests. The October 17
conditional-write smoke test established observed behavior, not a provider guarantee.

On Paul’s Mac, use the workspace token `RAILWAY_API_TOKEN` exported from
`~/.secrets.sh`. Clean noninteractive shells must receive it explicitly;
`~/.shortcuts.sh` defines shell helpers, not Railway credentials. Unset
`RAILWAY_TOKEN` for workspace operations and require a nonempty API token.

```bash
npm --prefix .railway ci --ignore-scripts
npm --prefix .railway run check
(
  unset RAILWAY_TOKEN
  test -n "${RAILWAY_API_TOKEN:-}" || exit 1
  export RAILWAY_PROJECT_ID=f463753e-8db6-4d2b-b8d1-127e9ab5d59d
  export RAILWAY_ENVIRONMENT_ID=bd4378a5-dd88-44c3-957f-57877e8ca905
  # Verifies live volume identity/attachment before creating a pinned plan.
  .venv/bin/python .railway/preflight.py --out storage/railway/reviewed-plan.json
  # Review that exact plan; stop on replacement/deletion or extra resources.
  railway config apply --plan storage/railway/reviewed-plan.json --yes
  .venv/bin/python .railway/verify.py
  .venv/bin/python .railway/connect.py
)
```

`connect.py` verifies project/workspace/environment, database service, live volume
attachment and bucket identity/region before saving credentials to owner-only,
ignored `.env.facts-dev`. It reads credentials without displaying them or resetting
them. It does not edit global profiles. Load this file through python-dotenv rather
than passing secrets as command arguments or printing Railway variables. The sync
client uses only `FACTS_DATABASE_URL`; it does not fall back to `DATABASE_URL`.

`verify.py` fetches no credentials. It checks a clean IaC plan, active successful
database deployment, exact live data volume attachment, bucket identity and daily/
weekly backup schedules. Database SQL/TLS and artifact read/write tests are separate
acceptance checks. Do not interpret a successful plan as evidence of data delivery.

SDK 3.15.0 uses the inherited `_` executable for its CLI version check. The Python
wrappers set `_` to the resolved Railway executable in their subprocess environment
so it does not mistakenly check Python's version. This does not change the global
toolchain or shell configuration.

The template initially creates the database/volume. A second reviewed apply may be
needed for the public TCP connection/reference settings. Adopt the template-created
`postgres-volume` in the definition before subsequent applies. The template owns
generated credentials and the attachment; this definition manages the volume's
region/size/alerts. Never apply an older incomplete whole-project definition.
The imported volume declaration is preservation, not an attachment. Do not add
`volumeMounts` to a managed Postgres helper. The live preflight is essential:
TypeScript does not independently bind the product-managed attachment to its ID.
Never casually apply this graph while disposable restore resources are present.

Backup schedules are configured through Railway's native PostgreSQL commands:

```bash
railway postgres pitr schedule set --daily --weekly \
  --project f463753e-8db6-4d2b-b8d1-127e9ab5d59d \
  --environment bd4378a5-dd88-44c3-957f-57877e8ca905 \
  --service 40a20cf2-77ee-4f76-a146-637c3dd51282 --json
```

These schedules are checked but a provider volume-backup restore has not been
exercised. The portable corpus restore drill is a separate tested recovery path.
Keep SQLite WAL and its dispatch state on one host; this milestone does not make
SQLite a shared queue. Future hosted collectors need a persistent local journal
volume or a deliberately implemented transactional queue.

## Current recovery gate

Passed on November 4–5, 2026 (private evidence under
`storage/recovery-proof/2026-11-04/`):

- `connect.py` refreshed the bucket credentials; the earlier `SignatureDoesNotMatch`
  is gone and the remote manifest listing works.
- The live volume reported 2,924 MB used of 50,000 MB; daily and weekly volume
  backups existed. PITR (WAL archiving) is **not** enabled.
- Provider restore drill on a disposable service: a `pg_dump -n facts` of the
  real database was restored into a fresh template Postgres (identical content
  fingerprint), a witness row was written, an on-demand backup was taken, the
  witness was changed and a row added, and `pitr backup restore` was run. The
  restore staged a new volume and redeployed; the witness reverted and the record
  fingerprint matched the source. The drill service and its volumes were then
  deleted. `verify.py` reported a clean plan and the original attachment intact.
- Migrations 1–2 were rehearsed on the drill copy, then applied to the real
  database; the existing mirror fingerprint was unchanged.

Notes from the drill: `railway add` with only `RAILWAY_PROJECT_ID`/`RAILWAY_ENVIRONMENT_ID`
reported "Project not found" yet created a service; prefer the `templateDeployV2`
GraphQL mutation with the `postgres` template and an explicit name, and list
services afterwards. Deleting a manual backup with the workspace token returned
`UNAUTHORIZED`; deleting its volume removes it. Repeat the drill after provider
or template changes; it proves volume-backup restore, not PITR.

For a provider restore drill, first verify project/environment/service/volume IDs,
current capacity and backup inventory with restored control-plane access. Ordinary
volume-backup restore stages a replacement volume on the source service; do not
run `pitr backup restore` against the real Facts dev service. Establish a supported
disposable target. A disposable database loaded with a known snapshot can exercise
its own provider backup/restore without changing the original. If testing a PITR
restore-to-new-service path, first establish that a usable archive/base backup
exists; the daily/weekly volume schedules do not establish PITR coverage.

Record source backup ID, disposable target service/volume IDs, completed deployment,
TLS/SQL counts and import receipt/evidence links. Remove only drill-owned resources
after verification and check the original attachment/clean plan again. A failed
or merely scheduled restore is not a successful recovery drill. See the current
[handoff](../_working/corpus_refresh_handoff.md) for private report locations.

References: [Railway Buckets](https://docs.railway.com/storage-buckets),
[conditional-write clarification](https://station.railway.com/questions/railway-buckets-consistency-and-create-d83ae29c),
[managed database ownership](https://docs.railway.com/infrastructure-as-code/reference),
[volume backups](https://docs.railway.com/volumes/backups),
[PITR](https://docs.railway.com/volumes/point-in-time-recovery).
