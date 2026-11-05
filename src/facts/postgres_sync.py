"""Optional structured-record sync. Local artifacts remain local until uploaded."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path

from .core import ROOT, dumps, now, ident
from .runtime import writer_lock

from .postgres_schema import MIRROR_SCHEMA as SCHEMA, migrate
from . import postgres_read_model

BASE_TABLES = ('topics', 'searches', 'sources', 'captures', 'items', 'datasets', 'extraction_jobs', 'attempts')
ORGANIZED_TABLES = ('organized_records', 'organized_entities', 'organized_aliases', 'organized_groups', 'organized_group_history',
                    'organized_memberships', 'organized_relations', 'organized_links', 'organized_coverage',
                    'organized_jobs', 'organized_runs', 'organized_tables', 'organized_mappings')
SYNC_LOCK = (int.from_bytes(b'fact'), int.from_bytes(b'sync'))


def synchronized_kinds(store):
    present = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [*BASE_TABLES, 'followups', 'topic_sources', *[table for table in ORGANIZED_TABLES if table in present]]


@contextlib.contextmanager
def catalog_snapshot(store):
    if store.db.in_transaction:
        raise ValueError('Commit local changes before syncing the catalog')
    store.db.execute('BEGIN')
    try:
        yield
    finally:
        store.db.rollback()


def records(store):
    for table in BASE_TABLES:
        for row in store.db.execute(f'SELECT * FROM {table}'):
            value=dict(row)
            for key in ['payload','metadata']:
                if key in value:
                    value[key]=json.loads(value[key])
            yield table, row['id'], value.get('topic') or (row['id'] if table=='topics' else None), value
    for row in store.db.execute('SELECT * FROM followups'):
        value = dict(row)
        yield 'followups', ident('followup', value['topic'], value['query']), row['topic'], value
    for row in store.db.execute('SELECT * FROM topic_sources'):
        value=dict(row)
        yield 'topic_sources',ident('membership',value),row['topic'],value
    # Organization is optional and may not have run on a fresh collector.
    for table in ORGANIZED_TABLES:
        if not store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        keys = [r['name'] for r in sorted(store.db.execute(f'PRAGMA table_info({table})'), key=lambda r:r['pk']) if r['pk']]
        for row in store.db.execute(f'SELECT * FROM {table}'):
            value=dict(row)
            for key in ['payload','settings','usage','record_ids']:
                if isinstance(value.get(key),str):
                    value[key]=json.loads(value[key])
            # Composite IDs preserve prior record versions and relation history.
            rid = value['id'] if keys==['id'] else ident(table, *[value[k] for k in keys])
            yield table,rid,value.get('topic'),value


def sync_records(store, connection, *, artifact_manifest=None):
    """Mirror the kinds present in one catalog; all remote changes commit together."""
    from psycopg.types.json import Jsonb

    count = 0
    synced_at = now()
    with connection.transaction():
        # Serialize initialization too: concurrent CREATE IF NOT EXISTS does
        # not itself protect first-time schema creation or lock upgrades.
        connection.execute('SELECT pg_advisory_xact_lock(%s,%s)', SYNC_LOCK)
        migrations = migrate(connection, SYNC_LOCK)
        previous = connection.execute('''SELECT r.artifact_manifest FROM facts.mirror_state s
            JOIN facts.import_receipts r ON r.id=s.receipt_id WHERE s.id='current' ''').fetchone()
        if artifact_manifest is None and previous and previous[0] is not None:
            raise ValueError('A linked mirror cannot be downgraded to an unlinked import')
        # Serialize mirror writers before taking the local snapshot. Ordinary
        # PostgreSQL readers still see the previous committed mirror during COPY.
        connection.execute('LOCK TABLE facts.records IN SHARE ROW EXCLUSIVE MODE')
        with catalog_snapshot(store):
            kinds = synchronized_kinds(store)
            counts = dict.fromkeys(kinds, 0)
            leaves = []
            connection.execute('''CREATE TEMP TABLE facts_sync_stage (
                kind text NOT NULL, id text NOT NULL, topic text,
                payload jsonb NOT NULL, synced_at timestamptz NOT NULL,
                PRIMARY KEY(kind,id)) ON COMMIT DROP''')
            with connection.cursor() as cursor:
                with cursor.copy('COPY facts_sync_stage (kind,id,topic,payload,synced_at) FROM STDIN') as copy:
                    for kind, rid, topic, payload in records(store):
                        if kind not in kinds:
                            raise ValueError('Record kind is outside the synchronized catalog')
                        copy.write_row((kind, rid, topic, Jsonb(payload), synced_at))
                        counts[kind] += 1
                        leaves.append(hashlib.sha256(dumps([kind, rid, topic, payload]).encode()).hexdigest())
                        count += 1
            connection.execute('ANALYZE facts_sync_stage')
            # Hash logical contents, independent of enumeration order and delivery time.
            digest = hashlib.sha256(dumps([sorted(kinds), sorted(leaves)]).encode()).hexdigest()
            receipt_id = hashlib.sha256(dumps([digest, artifact_manifest]).encode()).hexdigest()
            connection.execute('''INSERT INTO facts.import_receipts
                (id,catalog_digest,artifact_manifest,counts,synchronized_kinds,committed_at)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING''',
                (receipt_id,digest,artifact_manifest,Jsonb(counts),Jsonb(kinds),synced_at))
            # The typed read model comes from this same SQLite snapshot and commits atomically.
            read_model = postgres_read_model.refresh(store, connection, receipt_id)
        written = connection.execute('''INSERT INTO facts.records AS existing (kind,id,topic,payload,synced_at)
            SELECT kind,id,topic,payload,synced_at FROM facts_sync_stage
            ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,
                topic=excluded.topic,synced_at=excluded.synced_at
            WHERE existing.payload IS DISTINCT FROM excluded.payload
               OR existing.topic IS DISTINCT FROM excluded.topic''').rowcount
        deleted = connection.execute('''DELETE FROM facts.records AS existing
            WHERE existing.kind = ANY(%s::text[]) AND NOT EXISTS (
                SELECT 1 FROM facts_sync_stage AS staged
                WHERE staged.kind=existing.kind AND staged.id=existing.id)''', (kinds,)).rowcount
        connection.execute('''INSERT INTO facts.mirror_state VALUES ('current',%s)
            ON CONFLICT(id) DO UPDATE SET receipt_id=excluded.receipt_id''', (receipt_id,))
    return {'synced_records': count, 'written_records': written, 'deleted_records': deleted,
            'synchronized_kinds': kinds, 'counts': counts, 'receipt_id': receipt_id,
            'catalog_digest': digest, 'artifact_manifest': artifact_manifest,
            'migrations_applied': migrations, 'read_model': read_model,
            'artifacts': 'manifest linked; verify its delivery receipt' if artifact_manifest else 'local; not uploaded'}


def dry_run(store):
    with catalog_snapshot(store):
        counts = dict.fromkeys(synchronized_kinds(store), 0)
        for kind, _, _, _ in records(store):
            counts[kind] += 1
    return {'mode': 'dry-run', 'records': counts, 'synchronized_kinds': list(counts),
            'apply_behavior': 'Mirror present kinds, including deletion of remote IDs missing locally; other kinds remain untouched.',
            'artifacts': 'local; not uploaded'}


def validate_delivery(root, bundle, snapshot, receipt_path, destination):
    from .handoff import load_manifest, verify
    from .artifact_store import digest_file
    if not destination.startswith('s3:'):
        raise ValueError('Operational database imports require the configured S3 replica')
    manifest=load_manifest(bundle,snapshot);verify(bundle,snapshot)
    catalog=next(f for f in manifest['files'] if f['path']=='storage/catalog.sqlite')
    if digest_file(Path(root)/'storage/catalog.sqlite') != (catalog['sha256'],catalog['size']):
        raise ValueError('Sync a restored snapshot root to link its artifact manifest')
    # The read model also loads the root's organized Parquet values; they must be the snapshot's bytes.
    for entry in manifest['files']:
        if entry['path'].startswith('storage/organized/tables/'):
            path=Path(root)/entry['path']
            if not path.is_file() or digest_file(path) != (entry['sha256'],entry['size']):
                raise ValueError('Restored root organized table files differ from the snapshot manifest')
    receipt=json.loads(Path(receipt_path).read_text())
    if (receipt.get('snapshot') != snapshot or not receipt.get('verified_at')
            or receipt.get('destination') != destination
            or type(receipt.get('verified_objects')) is not int
            or receipt['verified_objects'] != len({f['sha256'] for f in manifest['files']})):
        raise ValueError('Artifact delivery receipt does not match this snapshot and S3 destination')


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true')
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--bundle',type=Path)
    p.add_argument('--snapshot')
    p.add_argument('--artifact-receipt',type=Path)
    p.add_argument('--migrate',action='store_true',help='Apply pending PostgreSQL schema migrations only')
    p.add_argument('--migration-status',action='store_true',help='Report applied/pending migrations without writing')
    args=p.parse_args()
    if args.migrate or args.migration_status:
        if args.apply:
            p.error('--migrate/--migration-status cannot be combined with --apply')
        return _run_migrations(apply=args.migrate)
    if args.apply and not (args.snapshot and args.bundle and args.artifact_receipt):
        p.error('--apply requires --snapshot, --bundle and the configured S3 --artifact-receipt')
    # Keep the snapshot check, mirror commit and local receipt under one root owner.
    # Read-only inspection must not create a writer lock or any storage directories.
    with writer_lock(args.root) if args.apply else contextlib.nullcontext():
        return _run_import(args)


def _run_migrations(apply):
    from .postgres_schema import compatible, status
    url=os.environ.get('FACTS_DATABASE_URL')
    if not url:raise SystemExit('Set FACTS_DATABASE_URL in the environment; do not pass credentials as command arguments.')
    try:
        import psycopg
        with psycopg.connect(url) as conn:
            applied=[]
            if apply:
                with conn.transaction():
                    applied=migrate(conn,SYNC_LOCK)
            else:
                conn.read_only=True
            rows=status(conn)
            print(json.dumps({'applied_now':applied,'compatible':compatible(rows),
                              'pending':[r['version'] for r in rows if not r['applied']],'migrations':rows}))
    except ValueError:
        raise
    except Exception as error:
        raise SystemExit(f'PostgreSQL migration failed: {type(error).__name__}; inspect private diagnostics without printing connection credentials.')


def _run_import(args):
    from .organize_store import OrganizedStore
    # Reading counts should not initialize tables or alter collection state.
    s=OrganizedStore(args.root,readonly=True)
    try:
        if not args.apply:
            print(json.dumps(dry_run(s),indent=2));return
        if args.snapshot:
            from .artifact_store import DirectoryStore, S3Store, digest_file, verify_object
            from .handoff import manifest_key
            replica=S3Store()
            validate_delivery(args.root,args.bundle,args.snapshot,args.artifact_receipt,replica.identity)
            key=manifest_key(args.snapshot)
            digest,size=digest_file(DirectoryStore(args.bundle).path(key))
            verify_object(replica,key,digest,size)
        elif args.artifact_receipt or args.bundle:
            raise ValueError('Artifact options require --snapshot')
        import psycopg
        url=os.environ.get('FACTS_DATABASE_URL')
        if not url:raise SystemExit('Set FACTS_DATABASE_URL in the environment; do not pass credentials as command arguments.')
        with psycopg.connect(url) as conn:
            result=sync_records(s,conn,artifact_manifest=args.snapshot)
            target={'host':conn.info.host,'port':conn.info.port,'database':conn.info.dbname}
        from .handoff import journal
        target_id=hashlib.sha256(dumps(target).encode()).hexdigest()[:16]
        result.update({'database_target':target,'delivered_at':now()})
        journal(args.root/'storage/deliveries/postgres'/target_id/f'{result["receipt_id"]}.json', result)
        print(json.dumps(result))
    except Exception as error:
        # Driver errors can include connection details; report only the error class.
        raise SystemExit(f'PostgreSQL sync failed: {type(error).__name__}; inspect private diagnostics without printing connection credentials.')
    finally:
        s.db.close()


if __name__=='__main__':main()
