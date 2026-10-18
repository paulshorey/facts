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
    store=Store(tmp_path);add_topic(store);store.db.close()
    monkeypatch.setenv('FACTS_DATABASE_URL',postgres.info.dsn)
    monkeypatch.setattr('sys.argv',['facts.postgres_sync','--apply','--root',str(tmp_path)])
    original=handoff.journal
    def disk_failure(*args): raise OSError('simulated local interruption after commit')
    monkeypatch.setattr(handoff,'journal',disk_failure)
    with pytest.raises(SystemExit,match='OSError'): postgres_sync.main()
    assert postgres.execute('SELECT count(*) FROM facts.import_receipts').fetchone()[0]==1
    assert not (tmp_path/'storage/deliveries').exists()
    monkeypatch.setattr(handoff,'journal',original)
    postgres_sync.main()
    assert postgres.execute('SELECT count(*) FROM facts.import_receipts').fetchone()[0]==1
    assert len(list((tmp_path/'storage/deliveries/postgres').rglob('*.json')))==1


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
