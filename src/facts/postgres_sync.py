"""Optional structured-record sync. Local artifacts remain local until uploaded."""
import argparse
import contextlib
import json
import os

from .core import now, ident

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS facts;
CREATE TABLE IF NOT EXISTS facts.records (
  kind text NOT NULL,
  id text NOT NULL,
  topic text,
  payload jsonb NOT NULL,
  synced_at timestamptz NOT NULL,
  PRIMARY KEY(kind,id)
);
CREATE INDEX IF NOT EXISTS records_topic_kind_idx ON facts.records(topic,kind);
CREATE INDEX IF NOT EXISTS records_payload_gin_idx ON facts.records USING gin(payload);
"""

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


def sync_records(store, connection):
    """Mirror the kinds present in one catalog; all remote changes commit together."""
    from psycopg.types.json import Jsonb

    count = 0
    synced_at = now()
    with connection.transaction():
        # Serialize initialization too: concurrent CREATE IF NOT EXISTS does
        # not itself protect first-time schema creation or lock upgrades.
        connection.execute('SELECT pg_advisory_xact_lock(%s,%s)', SYNC_LOCK)
        connection.execute(SCHEMA)
        # Serialize mirror writers before taking the local snapshot. Ordinary
        # PostgreSQL readers still see the previous committed mirror during COPY.
        connection.execute('LOCK TABLE facts.records IN SHARE ROW EXCLUSIVE MODE')
        with catalog_snapshot(store):
            kinds = synchronized_kinds(store)
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
                        count += 1
            connection.execute('ANALYZE facts_sync_stage')
        connection.execute('''INSERT INTO facts.records (kind,id,topic,payload,synced_at)
            SELECT kind,id,topic,payload,synced_at FROM facts_sync_stage
            ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,
                topic=excluded.topic,synced_at=excluded.synced_at''')
        deleted = connection.execute('''DELETE FROM facts.records AS existing
            WHERE existing.kind = ANY(%s::text[]) AND NOT EXISTS (
                SELECT 1 FROM facts_sync_stage AS staged
                WHERE staged.kind=existing.kind AND staged.id=existing.id)''', (kinds,)).rowcount
    return {'synced_records': count, 'deleted_records': deleted,
            'synchronized_kinds': kinds, 'artifacts': 'local; not uploaded'}


def dry_run(store):
    with catalog_snapshot(store):
        counts = dict.fromkeys(synchronized_kinds(store), 0)
        for kind, _, _, _ in records(store):
            counts[kind] += 1
    return {'mode': 'dry-run', 'records': counts, 'synchronized_kinds': list(counts),
            'apply_behavior': 'Mirror present kinds, including deletion of remote IDs missing locally; other kinds remain untouched.',
            'artifacts': 'local; not uploaded'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');args=p.parse_args()
    from .organize_store import OrganizedStore
    # Reading counts should not initialize tables or alter collection state.
    s=OrganizedStore(readonly=True)
    try:
        if not args.apply:
            print(json.dumps(dry_run(s),indent=2));return
        import psycopg
        url=os.environ.get('FACTS_DATABASE_URL')
        if not url:raise SystemExit('Set FACTS_DATABASE_URL in the environment; do not pass credentials as command arguments.')
        with psycopg.connect(url) as conn:
            result=sync_records(s,conn)
        print(json.dumps(result))
    except Exception as error:
        # Driver errors can include connection details; report only the error class.
        raise SystemExit(f'PostgreSQL sync failed: {type(error).__name__}; inspect private diagnostics without printing connection credentials.')
    finally:
        s.db.close()


if __name__=='__main__':main()
