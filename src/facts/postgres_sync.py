"""Optional structured-record sync. Local artifacts remain local until uploaded."""
import argparse
import json
import os

from .core import Store, now

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


def records(store):
    for table in ['topics','searches','sources','captures','items','datasets','extraction_jobs','attempts']:
        for row in store.db.execute(f'SELECT * FROM {table}'):
            value=dict(row)
            for key in ['payload','metadata']:
                if key in value:
                    value[key]=json.loads(value[key])
            yield table, row['id'], value.get('topic') or (row['id'] if table=='topics' else None), value
    for row in store.db.execute('SELECT * FROM topic_sources'):
        value=dict(row)
        from .core import ident
        yield 'topic_sources',ident('membership',value),row['topic'],value
    # Organization is optional and may not have run on a fresh collector.
    tables = ['organized_records','organized_entities','organized_aliases','organized_groups',
              'organized_memberships','organized_relations','organized_links','organized_coverage',
              'organized_jobs','organized_runs','organized_tables','organized_mappings']
    for table in tables:
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


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');args=p.parse_args()
    from .organize_store import OrganizedStore
    # Reading counts should not initialize tables or alter collection state.
    s=Store() if args.apply else OrganizedStore(readonly=True)
    if not args.apply:
        counts={}
        for kind,_,_,_ in records(s):counts[kind]=counts.get(kind,0)+1
        print(json.dumps({'mode':'dry-run','records':counts,'artifacts':'local; not uploaded'},indent=2));return
    import psycopg
    from psycopg.types.json import Jsonb
    url=os.environ.get('FACTS_DATABASE_URL')
    if not url:raise SystemExit('Set FACTS_DATABASE_URL in the environment; do not pass credentials as command arguments.')
    # Driver errors can include connection details; report only the error class.
    try:
        with psycopg.connect(url) as conn:
            conn.execute(SCHEMA)
            count=0
            with conn.cursor() as cursor:
                for kind,rid,topic,payload in records(s):
                    cursor.execute('INSERT INTO facts.records VALUES (%s,%s,%s,%s,%s) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,topic=excluded.topic,synced_at=excluded.synced_at',(kind,rid,topic,Jsonb(payload),now()))
                    count+=1
            print(json.dumps({'synced_records':count,'artifacts':'local; not uploaded'}))
    except Exception as error:
        raise SystemExit(f'PostgreSQL sync failed: {type(error).__name__}; inspect private diagnostics without printing connection credentials.')


if __name__=='__main__':main()
