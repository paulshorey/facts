"""Use only an isolated temporary PostgreSQL cluster, never configured databases."""
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from facts import postgres_sync
from facts.contracts import Observation
from facts.core import Store, dumps
from facts.organize_groups import build_groups
from facts.organize_llm import Budget
from facts.organize_store import OrganizedStore


@pytest.fixture(scope="session")
def postgres_cluster():
    initdb = shutil.which("initdb")
    pg_ctl = str(Path(initdb).with_name("pg_ctl")) if initdb else None
    if not initdb or not Path(pg_ctl).exists() or getattr(os, "geteuid", lambda: -1)() == 0:
        pytest.skip("Local PostgreSQL initdb/pg_ctl and a non-root user are required")
    # A short socket path avoids the macOS Unix-socket path-length limit.
    with tempfile.TemporaryDirectory(prefix="facts-pg-", dir="/tmp") as directory:
        data = Path(directory) / "data"
        subprocess.run([initdb, "-D", str(data), "-U", "facts_test", "--auth=trust", "--encoding=UTF8", "--no-locale"], check=True, capture_output=True)
        options = "-F -k " + shlex.quote(directory) + " -h '' -p 5432 -c max_connections=10 -c shared_buffers=12MB"
        try:
            subprocess.run([pg_ctl, "-D", str(data), "-l", str(Path(directory) / "server.log"), "-o", options, "-w", "-t", "15", "start"], check=True, capture_output=True)
            yield {"host": directory, "port": 5432, "user": "facts_test"}
        finally:
            if (data / "postmaster.pid").exists():
                subprocess.run([pg_ctl, "-D", str(data), "-m", "fast", "-w", "-t", "15", "stop"], check=True, capture_output=True)


@pytest.fixture
def postgres(postgres_cluster):
    name = "facts_case_" + uuid4().hex
    with psycopg.connect(**postgres_cluster, dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(**postgres_cluster, dbname=name, autocommit=True) as connection:
                yield connection
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def add_topic(store):
    store.db.execute("INSERT INTO topics VALUES ('topic',?)", (dumps({"title": "before"}),))
    store.db.commit()


def group_observations(store):
    build_groups(store, ["topic"], [], Budget(0), "unused", 1, False)


def test_sync_reconciles_regrouping_empty_kinds_and_retains_history(tmp_path, postgres):
    store = OrganizedStore(tmp_path); add_topic(store)
    for iid, entity in [("a", "Acme"), ("b", "Other Corp")]:
        store.save_record(Observation(id=iid, topic="topic", title="Contract awarded", input_ids=[iid],
                                     entities=[{"name": entity, "entity_type": "organization"}]), "v1", "item:"+iid)
    group_observations(store)
    first = postgres_sync.sync_records(store, postgres)
    old_groups = {r[0] for r in postgres.execute("SELECT id FROM facts.records WHERE kind='organized_groups'")}
    assert len(old_groups) == 2 and first["deleted_records"] == 0
    postgres.execute("INSERT INTO facts.records VALUES ('future_kind','untouched',NULL,'{}',now())")
    postgres.execute("CREATE SCHEMA unrelated; CREATE TABLE unrelated.keep (value text); INSERT INTO unrelated.keep VALUES ('retained')")
    store.save_record(Observation(id="b", topic="topic", title="Contract awarded", input_ids=["b"],
                                 entities=[{"name": "Acme", "entity_type": "organization"}]), "v2", "item:b")
    group_observations(store)
    second = postgres_sync.sync_records(store, postgres)
    new_groups = {r[0] for r in postgres.execute("SELECT id FROM facts.records WHERE kind='organized_groups'")}
    assert len(new_groups) == 1 and new_groups < old_groups
    assert second["deleted_records"] >= 2  # Obsolete group and its membership.
    expected = {(kind, rid): payload for kind, rid, _, payload in postgres_sync.records(store)}
    actual = {(kind, rid): payload for kind, rid, payload in postgres.execute("SELECT kind,id,payload FROM facts.records WHERE kind=ANY(%s::text[])", (second["synchronized_kinds"],))}
    assert actual == expected
    assert postgres.execute("SELECT count(*) FROM facts.records WHERE kind='organized_group_history'").fetchone()[0] >= 3
    assert postgres.execute("SELECT count(*) FROM facts.records WHERE kind='organized_records'").fetchone()[0] == 3
    store.db.execute("DELETE FROM organized_groups"); store.db.execute("DELETE FROM organized_memberships"); store.db.commit()
    third = postgres_sync.sync_records(store, postgres)
    assert third["deleted_records"] == 3
    assert postgres.execute("SELECT count(*) FROM facts.records WHERE kind IN ('organized_groups','organized_memberships')").fetchone()[0] == 0
    assert postgres.execute("SELECT id FROM facts.records WHERE kind='future_kind'").fetchone()[0] == "untouched"
    assert postgres.execute("SELECT value FROM unrelated.keep").fetchone()[0] == "retained"


@pytest.mark.parametrize("phase", ["copy", "delete"])
def test_sync_failure_rolls_back_the_whole_remote_mirror(tmp_path, postgres, monkeypatch, phase):
    store = OrganizedStore(tmp_path); add_topic(store)
    postgres_sync.sync_records(store, postgres)
    postgres.execute("INSERT INTO facts.records VALUES ('organized_groups','obsolete','topic','{}',now())")
    before = postgres.execute("SELECT * FROM facts.records ORDER BY kind,id").fetchall()
    store.db.execute("UPDATE topics SET payload=?", (dumps({"title": "after"}),)); store.db.commit()
    if phase == "copy":
        original = postgres_sync.records
        def fail_during_copy(store):
            yield next(original(store))
            raise RuntimeError("Interrupted local enumeration")
        monkeypatch.setattr(postgres_sync, "records", fail_during_copy)
        error = RuntimeError
    else:
        postgres.execute("""CREATE FUNCTION reject_prune() RETURNS trigger LANGUAGE plpgsql AS
            $$ BEGIN RAISE EXCEPTION 'prune blocked'; END $$;
            CREATE TRIGGER reject_prune BEFORE DELETE ON facts.records
            FOR EACH ROW EXECUTE FUNCTION reject_prune()""")
        error = psycopg.errors.RaiseException
    with pytest.raises(error):
        postgres_sync.sync_records(store, postgres)
    assert postgres.execute("SELECT * FROM facts.records ORDER BY kind,id").fetchall() == before
    assert postgres.execute("SELECT to_regclass('pg_temp.facts_sync_stage')").fetchone()[0] is None
    assert not store.db.in_transaction


def test_sync_does_not_delete_optional_kinds_missing_from_local_schema(tmp_path, postgres):
    store = Store(tmp_path); add_topic(store)
    postgres.execute(postgres_sync.SCHEMA)
    postgres.execute("INSERT INTO facts.records VALUES ('organized_groups','existing','topic','{}',now())")
    report = postgres_sync.sync_records(store, postgres)
    assert "organized_groups" not in report["synchronized_kinds"]
    assert report["deleted_records"] == 0
    assert postgres.execute("SELECT id FROM facts.records WHERE kind='organized_groups'").fetchone()[0] == "existing"
    organized = OrganizedStore(tmp_path)
    report = postgres_sync.sync_records(organized, postgres)
    assert "organized_groups" in report["synchronized_kinds"] and report["deleted_records"] == 1


def test_sync_reads_one_sqlite_snapshot_during_concurrent_collection(tmp_path, postgres, monkeypatch):
    store = Store(tmp_path); add_topic(store)
    store.db.execute("INSERT INTO followups VALUES ('topic','before','packet')"); store.db.commit()
    original = postgres_sync.records
    def records_with_concurrent_edit(store):
        for index, value in enumerate(original(store)):
            if index == 0:
                writer = sqlite3.connect(tmp_path / "storage/catalog.sqlite")
                with writer:
                    writer.execute("DELETE FROM followups")
                    writer.execute("INSERT INTO followups VALUES ('topic','after','packet')")
                writer.close()
            yield value
    monkeypatch.setattr(postgres_sync, "records", records_with_concurrent_edit)
    postgres_sync.sync_records(store, postgres)
    assert postgres.execute("SELECT payload->>'query' FROM facts.records WHERE kind='followups'").fetchone()[0] == "before"
    monkeypatch.setattr(postgres_sync, "records", original)
    postgres_sync.sync_records(store, postgres)
    assert postgres.execute("SELECT payload->>'query' FROM facts.records WHERE kind='followups'").fetchone()[0] == "after"


def test_sync_serializes_writers_while_readers_keep_previous_snapshot(tmp_path, postgres, monkeypatch):
    store = Store(tmp_path); add_topic(store)
    postgres_sync.sync_records(store, postgres)
    store.db.execute("UPDATE topics SET payload=?", (dumps({"title": "after"}),)); store.db.commit()
    original = postgres_sync.records
    calls = []
    def observe_during_copy(store):
        with psycopg.connect(postgres.info.dsn, autocommit=True) as reader:
            assert reader.execute("SELECT payload->'payload'->>'title' FROM facts.records WHERE kind='topics'").fetchone()[0] == "before"
            reader.execute("SET lock_timeout='50ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                postgres_sync.sync_records(store, reader)
            calls.append("reader saw old data; competing sync blocked")
        yield from original(store)
    monkeypatch.setattr(postgres_sync, "records", observe_during_copy)
    postgres_sync.sync_records(store, postgres)
    assert calls == ["reader saw old data; competing sync blocked"]
    assert postgres.execute("SELECT payload->'payload'->>'title' FROM facts.records WHERE kind='topics'").fetchone()[0] == "after"


def test_sync_dry_run_reports_empty_scope_without_writes(tmp_path):
    store = Store(tmp_path); add_topic(store); store.db.close()
    reader = OrganizedStore(tmp_path, readonly=True)
    path = tmp_path / "storage/catalog.sqlite"
    before = path.read_bytes()
    report = postgres_sync.dry_run(reader)
    assert report["records"]["topics"] == 1 and report["records"]["followups"] == 0
    assert "followups" in report["synchronized_kinds"] and "organized_groups" not in report["synchronized_kinds"]
    assert path.read_bytes() == before and not reader.db.in_transaction


def test_import_receipt_is_atomic_repeatable_and_tracks_current_mirror(tmp_path,postgres,monkeypatch):
    store=Store(tmp_path);add_topic(store)
    first=postgres_sync.sync_records(store,postgres,artifact_manifest='fixture-manifest')
    second=postgres_sync.sync_records(store,postgres,artifact_manifest='fixture-manifest')
    assert first['receipt_id']==second['receipt_id']
    assert first['written_records']==1 and second['written_records']==0
    with pytest.raises(ValueError,match='downgraded'):
        postgres_sync.sync_records(store,postgres)
    assert postgres.execute('SELECT count(*) FROM facts.import_receipts').fetchone()[0]==1
    assert postgres.execute('SELECT receipt_id FROM facts.mirror_state').fetchone()[0]==first['receipt_id']
    saved=postgres.execute('SELECT counts FROM facts.import_receipts').fetchone()[0]
    assert saved==first['counts']
    store.db.execute("UPDATE topics SET payload='{}'");store.db.commit()
    postgres.execute("""CREATE FUNCTION reject_receipt() RETURNS trigger LANGUAGE plpgsql AS
        $$ BEGIN RAISE EXCEPTION 'receipt blocked'; END $$;
        CREATE TRIGGER reject_receipt BEFORE INSERT ON facts.import_receipts
        FOR EACH ROW EXECUTE FUNCTION reject_receipt()""")
    with pytest.raises(psycopg.errors.RaiseException):
        postgres_sync.sync_records(store,postgres,artifact_manifest='fixture-manifest')
    assert postgres.execute("SELECT payload->'payload'->>'title' FROM facts.records WHERE kind='topics'").fetchone()[0]=='before'
    assert postgres.execute('SELECT receipt_id FROM facts.mirror_state').fetchone()[0]==first['receipt_id']
    assert postgres.execute('SELECT count(*) FROM facts.import_receipts').fetchone()[0]==1


def test_lost_local_receipt_retries_without_duplicate_remote_receipt(tmp_path,postgres,monkeypatch):
    from facts import handoff
    root=tmp_path/'restored';bundle=tmp_path/'bundle'
    store=Store(tmp_path/'source');add_topic(store);store.db.close()
    snapshot=handoff.backup(tmp_path/'source',bundle)['snapshot']
    handoff.restore(bundle,snapshot,root)
    connection={'FACTS_S3_ENDPOINT':'https://fixture.invalid','FACTS_S3_BUCKET':'evidence',
        'FACTS_S3_ACCESS_KEY_ID':'fixture','FACTS_S3_SECRET_ACCESS_KEY':'fixture'}
    for key,value in connection.items(): monkeypatch.setenv(key,value)
    receipt=tmp_path/'receipt.json'
    handoff.journal(receipt,{'snapshot':snapshot,'destination':'s3:fixture.invalid/evidence/facts-v1',
        'verified_objects':len({f['sha256'] for f in handoff.load_manifest(bundle,snapshot)['files']}),'verified_at':'2026-10-25T00:00:00Z'})
    import io
    def remote_manifest(self,key):
        assert key==handoff.manifest_key(snapshot)
        return io.BytesIO((bundle/key).read_bytes())
    monkeypatch.setattr('facts.artifact_store.S3Store.open',remote_manifest)
    monkeypatch.setenv('FACTS_DATABASE_URL',postgres.info.dsn)
    monkeypatch.setattr('sys.argv',['facts.postgres_sync','--apply','--root',str(root),
        '--snapshot',snapshot,'--bundle',str(bundle),'--artifact-receipt',str(receipt)])
    original=handoff.journal
    def disk_failure(*args): raise OSError('simulated local interruption after commit')
    monkeypatch.setattr(handoff,'journal',disk_failure)
    with pytest.raises(SystemExit,match='OSError'): postgres_sync.main()
    assert postgres.execute('SELECT count(*) FROM facts.import_receipts').fetchone()[0]==1
    assert not (root/'storage/deliveries').exists()
    monkeypatch.setattr(handoff,'journal',original)
    postgres_sync.main()
    assert postgres.execute('SELECT count(*) FROM facts.import_receipts').fetchone()[0]==1
    assert len(list((root/'storage/deliveries/postgres').rglob('*.json')))==1


def test_database_disconnect_keeps_previous_mirror_and_can_retry(tmp_path,postgres,monkeypatch):
    store=Store(tmp_path);add_topic(store)
    first=postgres_sync.sync_records(store,postgres)
    store.db.execute("UPDATE topics SET payload='{}'");store.db.commit()
    original=postgres_sync.records
    with psycopg.connect(postgres.info.dsn,autocommit=True) as writer:
        def interrupted(s):
            for index,row in enumerate(original(s)):
                if index==0:
                    postgres.execute('SELECT pg_terminate_backend(%s)',(writer.info.backend_pid,))
                yield row
        monkeypatch.setattr(postgres_sync,'records',interrupted)
        with pytest.raises(psycopg.OperationalError): postgres_sync.sync_records(store,writer)
    assert not store.db.in_transaction
    assert postgres.execute('SELECT receipt_id FROM facts.mirror_state').fetchone()[0]==first['receipt_id']
    assert postgres.execute("SELECT payload->'payload'->>'title' FROM facts.records WHERE kind='topics'").fetchone()[0]=='before'
    monkeypatch.setattr(postgres_sync,'records',original)
    second=postgres_sync.sync_records(store,postgres)
    assert second['receipt_id']!=first['receipt_id']


@pytest.mark.parametrize('phase', ['manifest', 'receipt'])
def test_linked_cli_import_excludes_catalog_writers_until_local_receipt(tmp_path,postgres,monkeypatch,phase):
    from facts import handoff
    import io
    import sys
    root=tmp_path/'restored';bundle=tmp_path/'bundle'
    store=Store(tmp_path/'source');add_topic(store);store.db.close()
    snapshot=handoff.backup(tmp_path/'source',bundle)['snapshot']
    handoff.restore(bundle,snapshot,root)
    for key,value in {'FACTS_S3_ENDPOINT':'https://fixture.invalid','FACTS_S3_BUCKET':'evidence',
        'FACTS_S3_ACCESS_KEY_ID':'fixture','FACTS_S3_SECRET_ACCESS_KEY':'fixture'}.items():
        monkeypatch.setenv(key,value)
    receipt=tmp_path/'receipt.json'
    handoff.journal(receipt,{'snapshot':snapshot,'destination':'s3:fixture.invalid/evidence/facts-v1',
        'verified_objects':len({f['sha256'] for f in handoff.load_manifest(bundle,snapshot)['files']}),
        'verified_at':'2026-10-26T00:00:00Z'})
    writer='''import sqlite3,sys
from pathlib import Path
from facts.runtime import writer_lock
with writer_lock(sys.argv[1]):
    with sqlite3.connect(Path(sys.argv[1])/'storage/catalog.sqlite') as db:
        db.execute("UPDATE topics SET payload='{}'")
'''
    attempts=[]
    def competing_writer():
        result=subprocess.run([sys.executable,'-c',writer,str(root)],capture_output=True,text=True)
        attempts.append(result)
    def remote_manifest(self,key):
        assert key==handoff.manifest_key(snapshot)
        if phase=='manifest': competing_writer()
        return io.BytesIO((bundle/key).read_bytes())
    monkeypatch.setattr('facts.artifact_store.S3Store.open',remote_manifest)
    original=handoff.journal
    def local_receipt(path,value):
        if phase=='receipt': competing_writer()
        original(path,value)
    monkeypatch.setattr(handoff,'journal',local_receipt)
    monkeypatch.setenv('FACTS_DATABASE_URL',postgres.info.dsn)
    monkeypatch.setattr('sys.argv',['facts.postgres_sync','--apply','--root',str(root),
        '--snapshot',snapshot,'--bundle',str(bundle),'--artifact-receipt',str(receipt)])
    postgres_sync.main()
    assert len(attempts)==1
    assert attempts[0].returncode!=0 and 'Another collector/backup writer' in attempts[0].stderr
    assert postgres.execute("SELECT payload->'payload'->>'title' FROM facts.records WHERE kind='topics'").fetchone()[0]=='before'
    assert postgres.execute('SELECT artifact_manifest FROM facts.import_receipts').fetchone()[0]==snapshot
    assert len(list((root/'storage/deliveries/postgres').rglob('*.json')))==1
    # The importer releases ownership after the receipt, allowing the next writer.
    result=subprocess.run([sys.executable,'-c',writer,str(root)],capture_output=True,text=True)
    assert result.returncode==0


def test_migrations_are_recorded_checksummed_and_reject_drift(postgres):
    from facts import postgres_schema
    with postgres.transaction():
        assert postgres_schema.migrate(postgres, postgres_sync.SYNC_LOCK) == [1, 2]
    with postgres.transaction():
        assert postgres_schema.migrate(postgres, postgres_sync.SYNC_LOCK) == []
    assert all(m["applied"] and m["checksum_matches"] for m in postgres_schema.status(postgres))
    postgres.execute("UPDATE facts.schema_migrations SET checksum='changed' WHERE version=2")
    with pytest.raises(ValueError, match="differs"):
        with postgres.transaction():
            postgres_schema.migrate(postgres, postgres_sync.SYNC_LOCK)
    postgres.execute("UPDATE facts.schema_migrations SET checksum=%s WHERE version=2", (postgres_schema.checksum(postgres_schema.RESEARCH_SCHEMA),))
    postgres.execute("INSERT INTO facts.schema_migrations (version,name,checksum) VALUES (99,'future','x')")
    with pytest.raises(ValueError, match="newer"):
        with postgres.transaction():
            postgres_schema.migrate(postgres, postgres_sync.SYNC_LOCK)


def test_migration_adopts_an_existing_unversioned_mirror(postgres):
    postgres.execute(postgres_sync.SCHEMA)
    postgres.execute("INSERT INTO facts.records VALUES ('topics','keep','keep','{}',now())")
    from facts import postgres_schema
    with postgres.transaction():
        assert postgres_schema.migrate(postgres, postgres_sync.SYNC_LOCK) == [1, 2]
    assert postgres.execute("SELECT id FROM facts.records").fetchone()[0] == "keep"


def _series_fixture(store, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from decimal import Decimal
    from facts.contracts import SeriesDefinition
    values = tmp_path / "storage/organized/tables/table-version_x.parquet"
    values.parent.mkdir(parents=True, exist_ok=True)
    decimal = pa.decimal128(38, 10)
    rows = [{"series_id": sid, "dataset_id": "dataset_x", "period": period, "period_start": start, "period_end": end,
             "calendar": "fiscal", "period_role": "unknown", "source_row": "8", "source_column": column,
             "original_expression": raw, "source_value": Decimal(raw), "value": Decimal(raw) * 1000000,
             "unit": "USD", "scale": "1000000", "basis": "net_outlays", "value_kind": "level", "warnings": "[]"}
            for sid, period, start, end, column, raw in [("series_a", "2025", "2024-10-01", "2025-09-30", "c4", "17570"),
                                                         ("series_a", "2026", "2025-10-01", "2026-09-30", "c5", "18270"),
                                                         ("series_stale", "2026", None, None, "c6", "1")]]
    schema = pa.schema([(k, decimal if k in ("source_value", "value") else pa.string()) for k in rows[0]])
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), values)
    table = {"id": "dataset_x", "topic": "topic", "name": "Book.xlsx:Sheet", "mapping": {"orientation": "wide"},
             "row_counts": {"normalized": 1}, "series_count": 1, "value_count": 2,
             "values_artifact": "storage/organized/tables/table-version_x.parquet"}
    store.db.execute("INSERT INTO organized_tables VALUES ('dataset_x','topic','digest','table-version_x','succeeded',?)", (dumps(table),))
    store.save_record(SeriesDefinition(id="series_a", topic="topic", subtype="wide", title="Defense", detail="Book.xlsx:Sheet",
        input_ids=["dataset_x"], dataset_id="dataset_x", metric="outlays", basis="net_outlays", unit="USD", scale="1000000",
        unit_evidence="(in millions)", dimensions={"account": "Defense"}, hierarchy=[], row_role="detail", categories=["defense"],
        references=[{"source_id": "source_x", "capture_id": "capture_x", "locator": {"row": "8"}}],
        values_artifact=table["values_artifact"], processing={"method": "dataset_mapping"}), "table-version_x", "dataset:x")
    store.db.commit()


def test_sync_builds_typed_research_read_model_atomically(tmp_path, postgres):
    store = OrganizedStore(tmp_path); add_topic(store)
    observation = Observation(id="obs_1", topic="topic", title="Fund received $2 billion", detail="Reported stake.",
        input_ids=["item_1"], categories=["investment"], dates=[{"role": "event", "original_expression": "May 2025",
        "start": "2025-05-01", "end": "2025-05-31", "precision": "month"}, {"role": "publication", "original_expression": "2026",
        "start": "2026-01-01"}, {"role": "reporting_period", "original_expression": "FY2026", "start": "2025-10-01",
        "end": "2026-09-30"}, {"role": "unknown", "original_expression": "2019", "start": "2019-01-01"}],
        measurements=[{"metric": "investment", "value": "2000000000", "original_expression": "$2 billion", "unit": "USD",
        "scale": "1000000000"}, {"metric": "share", "value": "about half", "original_expression": "about half"}],
        entities=[{"name": "MGX", "entity_type": "organization", "role": "investor"}],
        attribution=[{"name": "Reuters", "role": "reported_by"}],
        references=[{"source_id": "source_x", "capture_id": "capture_x", "url": "https://example.com/a", "locator": {"chunk": 0}}])
    # An inactive earlier version must not appear in the read model.
    store.save_record(Observation(id="obs_1", topic="topic", title="Old version", input_ids=["item_1"]), "v0", "item:1")
    store.save_record(observation, "v1", "item:1")
    store.db.commit()
    group_observations(store)
    _series_fixture(store, tmp_path)
    first = postgres_sync.sync_records(store, postgres)
    assert first["migrations_applied"] == [1, 2] and first["read_model"]["refreshed"]
    counts = first["read_model"]["counts"]
    assert counts["observations"] == 1 and counts["measurements"] == 2 and counts["series_values"] == 2
    row = postgres.execute("SELECT version,title,event_start,event_end,categories FROM research.observations").fetchone()
    assert row[0] == "v1" and row[1] == "Fund received $2 billion" and str(row[2]) == "2025-05-01" and str(row[3]) == "2025-05-31"
    assert row[4] == ["investment"]
    assert postgres.execute("SELECT count(*) FROM research.observation_dates").fetchone()[0] == 4
    measures = postgres.execute("SELECT value,value_text,unit FROM research.measurements ORDER BY ordinal").fetchall()
    assert measures[0][0] == 2000000000 and measures[1][0] is None and measures[1][1] == "about half"
    assert postgres.execute("SELECT name,role FROM research.observation_entities").fetchone() == ("MGX", "investor")
    assert postgres.execute("SELECT count(*) FROM research.observations WHERE search @@ plainto_tsquery('english','fund billion')").fetchone()[0] == 1
    assert postgres.execute("SELECT count(*) FROM research.group_members").fetchone()[0] >= 1
    points = postgres.execute("SELECT period,value,source_value,dataset_name FROM research.series_points ORDER BY period").fetchall()
    assert [(p[0], int(p[1]), int(p[2]), p[3]) for p in points] == [("2025", 17570000000, 17570, "Book.xlsx:Sheet"),
                                                                    ("2026", 18270000000, 18270, "Book.xlsx:Sheet")]
    state = postgres.execute("SELECT receipt_id,model_version FROM research.refresh_state").fetchone()
    assert state == (first["receipt_id"], "research-v1")
    # Unlinked receipts do not cover Parquet bytes, so every unlinked import rebuilds.
    second = postgres_sync.sync_records(store, postgres)
    assert second["receipt_id"] == first["receipt_id"] and second["read_model"]["refreshed"]
    linked = postgres_sync.sync_records(store, postgres, artifact_manifest="manifest-1")
    again = postgres_sync.sync_records(store, postgres, artifact_manifest="manifest-1")
    assert linked["read_model"]["refreshed"] and not again["read_model"]["refreshed"]
    store.db.execute("DELETE FROM organized_records WHERE id='obs_1'"); store.db.commit()
    third = postgres_sync.sync_records(store, postgres, artifact_manifest="manifest-2")
    assert third["read_model"]["refreshed"] and third["read_model"]["counts"]["observations"] == 0
    assert postgres.execute("SELECT count(*) FROM research.measurements").fetchone()[0] == 0
    assert postgres.execute("SELECT count(*) FROM research.series_values").fetchone()[0] == 2


def test_read_model_failure_rolls_back_mirror_and_previous_model(tmp_path, postgres):
    store = OrganizedStore(tmp_path); add_topic(store)
    _series_fixture(store, tmp_path)
    first = postgres_sync.sync_records(store, postgres)
    before = postgres.execute("SELECT * FROM facts.records ORDER BY kind,id").fetchall()
    (tmp_path / "storage/organized/tables/table-version_x.parquet").unlink()
    store.db.execute("UPDATE topics SET payload=?", (dumps({"title": "after"}),)); store.db.commit()
    with pytest.raises(FileNotFoundError):
        postgres_sync.sync_records(store, postgres)
    assert postgres.execute("SELECT * FROM facts.records ORDER BY kind,id").fetchall() == before
    assert postgres.execute("SELECT receipt_id FROM research.refresh_state").fetchone()[0] == first["receipt_id"]
    assert postgres.execute("SELECT count(*) FROM research.series_values").fetchone()[0] == 2


def test_migration_status_reports_newer_database_as_incompatible(postgres):
    from facts import postgres_schema
    with postgres.transaction():
        postgres_schema.migrate(postgres, postgres_sync.SYNC_LOCK)
    assert postgres_schema.compatible(postgres_schema.status(postgres))
    postgres.execute("INSERT INTO facts.schema_migrations (version,name,checksum) VALUES (99,'future','x')")
    rows = postgres_schema.status(postgres)
    assert rows[-1] == {'version': 99, 'name': None, 'applied': True, 'checksum_matches': None, 'unknown': True}
    assert not postgres_schema.compatible(rows)


def test_linked_import_rejects_altered_restored_parquet(tmp_path):
    from facts import handoff
    source = tmp_path/'source'; bundle = tmp_path/'bundle'; root = tmp_path/'restored'
    store = OrganizedStore(source); add_topic(store); _series_fixture(store, source); store.db.close()
    snapshot = handoff.backup(source, bundle)['snapshot']
    handoff.restore(bundle, snapshot, root)
    receipt = tmp_path/'receipt.json'
    handoff.journal(receipt, {'snapshot': snapshot, 'destination': 's3:fixture.invalid/evidence/facts-v1',
        'verified_objects': len({f['sha256'] for f in handoff.load_manifest(bundle, snapshot)['files']}),
        'verified_at': '2026-11-05T00:00:00Z'})
    destination = 's3:fixture.invalid/evidence/facts-v1'
    postgres_sync.validate_delivery(root, bundle, snapshot, receipt, destination)
    values = root/'storage/organized/tables/table-version_x.parquet'
    values.write_bytes(values.read_bytes() + b'\0')
    with pytest.raises(ValueError, match='organized table files differ'):
        postgres_sync.validate_delivery(root, bundle, snapshot, receipt, destination)


def test_read_model_omits_retired_entities_and_stale_groups(tmp_path, postgres):
    store = OrganizedStore(tmp_path); add_topic(store)
    for iid in ("a", "b"):
        store.save_record(Observation(id=iid, topic="topic", title="Contract awarded", input_ids=[iid],
            entities=[{"name": "Acme", "entity_type": "organization"}],
            references=[{"source_id": "s", "url": f"https://example.com/{iid}", "locator": {}}]), "v1", "item:"+iid)
    store.db.commit(); group_observations(store)
    # Re-version "b" with another entity after grouping; its group membership is now stale.
    store.save_record(Observation(id="b", topic="topic", title="Contract awarded", input_ids=["b"],
        entities=[{"name": "Other Corp", "entity_type": "organization"}]), "v2", "item:b")
    store.save_record(Observation(id="a", topic="topic", title="Contract awarded", input_ids=["a"],
        entities=[{"name": "Other Corp", "entity_type": "organization"}]), "v2", "item:a")
    store.db.commit()
    assert store.db.execute("SELECT count(*) FROM organized_groups").fetchone()[0] >= 1
    report = postgres_sync.sync_records(store, postgres)
    names = {r[0] for r in postgres.execute("SELECT name FROM research.entities")}
    assert names == {"Other Corp"}
    assert report["read_model"]["counts"]["groups"] == 0 and report["read_model"]["counts"]["group_members"] == 0


def test_unlinked_retry_revalidates_parquet_even_with_an_unchanged_catalog(tmp_path, postgres):
    store = OrganizedStore(tmp_path); add_topic(store)
    _series_fixture(store, tmp_path)
    first = postgres_sync.sync_records(store, postgres)
    (tmp_path / "storage/organized/tables/table-version_x.parquet").unlink()
    with pytest.raises(FileNotFoundError):
        postgres_sync.sync_records(store, postgres)
    assert postgres.execute("SELECT receipt_id FROM research.refresh_state").fetchone()[0] == first["receipt_id"]
