"""Incremental derived storage; the original collector tables are never rewritten."""
from __future__ import annotations

import contextlib
import fcntl
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from .contracts import VERSION, Observation, SeriesDefinition
from .core import ROOT, Store, dumps, ident, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS organized_records (
 id TEXT NOT NULL, version TEXT NOT NULL, topic TEXT NOT NULL, record_type TEXT NOT NULL,
 input_key TEXT NOT NULL, content_role TEXT NOT NULL, kind TEXT NOT NULL,
 subtype TEXT NOT NULL, title TEXT NOT NULL, event_start TEXT, basis TEXT,
 active INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL,
 PRIMARY KEY(id,version));
CREATE INDEX IF NOT EXISTS organized_topic ON organized_records(topic,active,record_type,content_role);
CREATE INDEX IF NOT EXISTS organized_input ON organized_records(input_key,active);
CREATE TABLE IF NOT EXISTS organized_entities (id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organized_aliases (
 entity_id TEXT NOT NULL, alias TEXT NOT NULL, entity_type TEXT NOT NULL,
 PRIMARY KEY(entity_id,alias));
CREATE INDEX IF NOT EXISTS organized_alias ON organized_aliases(alias,entity_type);
CREATE TABLE IF NOT EXISTS organized_groups (
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, group_kind TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organized_memberships (
 group_id TEXT NOT NULL,record_id TEXT NOT NULL,relation TEXT NOT NULL,
 PRIMARY KEY(group_id,record_id,relation));
CREATE TABLE IF NOT EXISTS organized_relations (
 record_id TEXT NOT NULL,version TEXT NOT NULL,target_id TEXT NOT NULL,
 target_version TEXT NOT NULL,relation TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(record_id,version,target_id,target_version,relation));
CREATE TABLE IF NOT EXISTS organized_links (
 record_id TEXT NOT NULL,version TEXT NOT NULL,target_kind TEXT NOT NULL,target_id TEXT NOT NULL,
 PRIMARY KEY(record_id,version,target_kind,target_id));
CREATE TABLE IF NOT EXISTS organized_coverage (
 input_kind TEXT NOT NULL,input_id TEXT NOT NULL,topic TEXT NOT NULL,version TEXT NOT NULL,
 input_digest TEXT NOT NULL,state TEXT NOT NULL,record_ids TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(input_kind,input_id));
CREATE TABLE IF NOT EXISTS organized_jobs (
 id TEXT PRIMARY KEY,topic TEXT NOT NULL,stage TEXT NOT NULL,input_artifact TEXT NOT NULL,
 settings TEXT NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 response_artifact TEXT,error TEXT,usage TEXT,cost REAL NOT NULL DEFAULT 0,
 reserved REAL NOT NULL DEFAULT 0,ready_at REAL NOT NULL DEFAULT 0,updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS organized_pending ON organized_jobs(stage,state,ready_at);
CREATE TABLE IF NOT EXISTS organized_runs (id TEXT PRIMARY KEY,topic TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organized_tables (
 id TEXT PRIMARY KEY,topic TEXT NOT NULL,input_digest TEXT NOT NULL,
 version TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organized_mappings (
 dataset_id TEXT NOT NULL,digest TEXT NOT NULL,topic TEXT NOT NULL,
 payload TEXT NOT NULL,artifact TEXT NOT NULL,PRIMARY KEY(dataset_id,digest));
CREATE VIRTUAL TABLE IF NOT EXISTS organized_search USING fts5(
 record_id UNINDEXED,version UNINDEXED,topic UNINDEXED,title,body);
CREATE TABLE IF NOT EXISTS organized_search_keys (id INTEGER PRIMARY KEY,record_id TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS organized_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


def name_key(name):
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", name).casefold()).strip()


class OrganizedStore:
    def __init__(self, root=ROOT, readonly=False):
        self.root = Path(root)
        self.readonly = readonly
        if readonly:
            self.original = None
            self.db = sqlite3.connect(f"file:{self.root / 'storage/catalog.sqlite'}?mode=ro", uri=True)
            self.db.row_factory = sqlite3.Row
        else:
            self.original = Store(root)
            self.db = self.original.db
            self.db.executescript(SCHEMA)
            if not self.db.execute("SELECT 1 FROM organized_meta WHERE key='search-rowid-v2'").fetchone():
                self.db.execute("DELETE FROM organized_search")
                self.db.execute("INSERT OR IGNORE INTO organized_search_keys(record_id) SELECT DISTINCT id FROM organized_records WHERE active=1")
                self.db.execute("INSERT INTO organized_search(rowid,record_id,version,topic,title,body) SELECT k.id,r.id,r.version,r.topic,r.title,COALESCE(json_extract(r.payload,'$.detail'),'')||' '||COALESCE(json_extract(r.payload,'$.original_text'),'')||' '||COALESCE(json_extract(r.payload,'$.entities'),'')||' '||COALESCE(json_extract(r.payload,'$.categories'),'') FROM organized_records r JOIN organized_search_keys k ON k.record_id=r.id WHERE r.active=1")
                self.db.execute("INSERT INTO organized_meta VALUES ('search-rowid-v2','1')")
            self.db.commit()

    def exists(self):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE name='organized_records'").fetchone() is not None

    def put(self, value):
        return self.original.put(value)

    def read(self, path):
        return (self.root / path).read_text(errors="replace")

    @contextlib.contextmanager
    def locks(self, topics):
        handles = []
        try:
            for topic in sorted(topics):
                folder = self.root / "storage/locks"
                folder.mkdir(exist_ok=True)
                handle = (folder / (ident("organize-lock", topic) + ".lock")).open("a")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    raise RuntimeError(f"Another organizing process owns topic {topic}") from None
                handles.append(handle)
            yield
        finally:
            for handle in reversed(handles):
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()

    def entity(self, mention, input_ids=()):
        canonical = mention.get("canonical_name") or mention["name"]
        key = name_key(canonical)
        kind = mention.get("entity_type") or "unknown"
        # Honor explicit same-name alias mappings, never fuzzy-match people or companies.
        found = self.db.execute("SELECT DISTINCT entity_id FROM organized_aliases WHERE alias=? AND entity_type=? ORDER BY entity_id", (key, kind)).fetchall()
        eid = found[0][0] if len(found) == 1 and not mention.get("canonical_name") else ident("entity", key, kind)
        aliases = set([mention["name"], canonical, *mention.get("aliases", [])])
        previous = self.db.execute("SELECT payload FROM organized_entities WHERE id=?", (eid,)).fetchone()
        payload = json.loads(previous[0]) if previous else {"schema_version": VERSION, "id": eid, "name": canonical, "entity_type": kind}
        payload["aliases"] = sorted(set(payload.get("aliases", [])).union(aliases))
        if mention.get("aliases") or mention.get("canonical_name"):
            evidence = {"input_ids": list(input_ids), "input_path": mention.get("input_path"), "canonical_name": canonical, "aliases": sorted(aliases)}
            existing = payload.setdefault("alias_evidence", [])
            if evidence not in existing:
                existing.append(evidence)
        self.db.execute("INSERT INTO organized_entities VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (eid, dumps(payload)))
        for alias in aliases:
            self.db.execute("INSERT OR IGNORE INTO organized_aliases VALUES (?,?,?)", (eid, name_key(alias), kind))
        return eid

    def save_record(self, value, version, input_key):
        value = value.model_dump() if hasattr(value, "model_dump") else dict(value)
        for mention in value.get("entities", []):
            mention["entity_id"] = self.entity(mention, value.get("input_ids", []))
        value.setdefault("schema_version", VERSION)
        value.setdefault("processing", {})["version"] = version
        contract = SeriesDefinition if value["record_type"] == "series" else Observation
        value = contract.model_validate(value).model_dump()
        self.db.execute("UPDATE organized_records SET active=0 WHERE id=? AND version!=?", (value["id"], version))
        dates = [d.get("start") for d in value.get("dates", []) if d.get("start") and d.get("role") not in {"publication", "acquisition"}]
        bases = [m.get("basis") for m in value.get("measurements", []) if m.get("basis")]
        self.db.execute("INSERT INTO organized_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id,version) DO UPDATE SET active=1,payload=excluded.payload,title=excluded.title,event_start=excluded.event_start,basis=excluded.basis,kind=excluded.kind,subtype=excluded.subtype,content_role=excluded.content_role",
            (value["id"], version, value["topic"], value["record_type"], input_key,
             value.get("content_role", "dataset"), value.get("kind", "series"), value.get("subtype", "unspecified"),
             value["title"], min(dates) if dates else None, value.get("basis") or (bases[0] if bases else None), 1, dumps(value)))
        self.db.execute("DELETE FROM organized_links WHERE record_id=? AND version=?", (value["id"], version))
        for target_kind, targets in [("entity", [e["entity_id"] for e in value.get("entities", [])]), ("category", value.get("categories", []))]:
            self.db.executemany("INSERT OR IGNORE INTO organized_links VALUES (?,?,?,?)", [(value["id"], version, target_kind, target) for target in targets])
        self.db.execute("INSERT OR IGNORE INTO organized_search_keys(record_id) VALUES (?)", (value["id"],))
        search_id = self.db.execute("SELECT id FROM organized_search_keys WHERE record_id=?", (value["id"],)).fetchone()[0]
        self.db.execute("DELETE FROM organized_search WHERE rowid=?", (search_id,))
        text = " ".join([value.get("detail", ""), value.get("original_text", ""), *[e["name"] for e in value.get("entities", [])], *value.get("categories", [])])
        self.db.execute("INSERT INTO organized_search(rowid,record_id,version,topic,title,body) VALUES (?,?,?,?,?,?)", (search_id, value["id"], version, value["topic"], value["title"], text))
        return value

    def coverage(self, kind, iid, topic, digest, state, record_ids, payload=None, version=VERSION):
        self.db.execute("INSERT INTO organized_coverage VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(input_kind,input_id) DO UPDATE SET version=excluded.version,input_digest=excluded.input_digest,state=excluded.state,record_ids=excluded.record_ids,payload=excluded.payload",
            (kind, iid, topic, version, digest, state, dumps(record_ids), dumps(payload or {})))

    def replace_input(self, input_key, records, version):
        # Retire old split parts too, so a later one-part result cannot leave old atoms active.
        self.db.execute("UPDATE organized_records SET active=0 WHERE input_key=?", (input_key,))
        for value in records:
            self.save_record(value, version, input_key)
        active_ids = {r["id"] if isinstance(r, dict) else r.id for r in records}
        for row in self.db.execute("SELECT DISTINCT id FROM organized_records WHERE input_key=?", (input_key,)).fetchall():
            if row[0] not in active_ids:
                self.db.execute("DELETE FROM organized_search WHERE rowid IN (SELECT id FROM organized_search_keys WHERE record_id=?)", (row[0],))

    def query(self, topic="all", view="organized", limit=20, contains=None, entity=None,
              category=None, group=None, kind=None, basis=None, content_role=None,
              date_from=None, date_to=None):
        if not self.exists():
            return []
        if view == "grouped":
            filtered_ids = None
            if any([contains, entity, category, kind, basis, content_role, date_from, date_to]):
                filtered_ids = {r["id"] for r in self.query(topic, "organized", 0, contains, entity, category, group, kind, basis, content_role, date_from, date_to)}
            rows = self.db.execute("SELECT payload FROM organized_groups" + (" WHERE topic=?" if topic != "all" else "") + " ORDER BY id", (() if topic == "all" else (topic,))).fetchall()
            output = []
            for row in rows:
                p = json.loads(row[0])
                if group and p["id"] != group:
                    continue
                members = self.db.execute("SELECT r.payload FROM organized_memberships m JOIN organized_records r ON r.id=m.record_id AND r.active=1 WHERE m.group_id=? ORDER BY r.id", (p["id"],)).fetchall()
                p["members"] = [json.loads(m[0]) for m in members]
                if filtered_ids is not None and not filtered_ids.intersection(m["id"] for m in p["members"]):
                    continue
                if not p["members"]:
                    continue
                output.append(p)
                if limit and len(output) >= limit:
                    break
            return output
        clauses, params = ["r.active=1"], []
        for column, value in [("topic", None if topic == "all" else topic), ("kind", kind), ("content_role", content_role)]:
            if value:
                clauses.append(f"r.{column}=?"); params.append(value)
        if basis:
            clauses.append("(r.basis=? OR EXISTS(SELECT 1 FROM json_each(r.payload,'$.measurements') m WHERE json_extract(m.value,'$.basis')=?))")
            params += [basis, basis]
        if category:
            clauses.append("EXISTS(SELECT 1 FROM organized_links l WHERE l.record_id=r.id AND l.version=r.version AND l.target_kind='category' AND l.target_id=?)"); params.append(category)
        if entity:
            clauses.append("EXISTS(SELECT 1 FROM organized_links l JOIN organized_entities e ON e.id=l.target_id WHERE l.record_id=r.id AND l.version=r.version AND l.target_kind='entity' AND (e.id=? OR lower(json_extract(e.payload,'$.name')) LIKE ?))")
            params += [entity, "%"+entity.casefold()+"%"]
        if group:
            clauses.append("EXISTS(SELECT 1 FROM organized_memberships m WHERE m.record_id=r.id AND m.group_id=?)"); params.append(group)
        if contains:
            tokens = re.findall(r"\w+", contains)
            if tokens:
                clauses.append("r.id IN (SELECT record_id FROM organized_search WHERE organized_search MATCH ?)")
                params.append(" AND ".join('"'+t+'"' for t in tokens))
        if date_from or date_to:
            date_conditions = ["json_extract(d.value,'$.role') NOT IN ('publication','acquisition','report','article_date','publication_date')"]
            if date_from:
                date_conditions.append("json_extract(d.value,'$.end')>=?"); params.append(date_from)
            if date_to:
                date_conditions.append("json_extract(d.value,'$.start')<=?"); params.append(date_to)
            clauses.append("EXISTS(SELECT 1 FROM json_each(r.payload,'$.dates') d WHERE "+" AND ".join(date_conditions)+")")
        sql = "SELECT r.payload FROM organized_records r WHERE " + " AND ".join(clauses) + " ORDER BY r.id"
        if limit:
            sql += " LIMIT ?"; params.append(limit)
        return [json.loads(r[0]) for r in self.db.execute(sql, params)]

    def inventory(self, topic="all"):
        args = () if topic == "all" else (topic,)
        where = "" if topic == "all" else " WHERE topic=?"
        result = {"input_items": self.db.execute("SELECT count(*) FROM items"+where, args).fetchone()[0],
                  "datasets": self.db.execute("SELECT count(*) FROM datasets"+where, args).fetchone()[0]}
        result["input_origins"] = dict(self.db.execute("SELECT COALESCE(json_extract(payload,'$.collection_metadata.extraction_context.type'),'unknown'),count(*) FROM items"+where+" GROUP BY 1", args))
        result["pending_extractions"] = self.db.execute("SELECT count(*) FROM extraction_jobs"+(" WHERE topic=? AND" if topic != "all" else " WHERE")+" state='queued'", args).fetchone()[0]
        if self.exists():
            result["records"] = dict(self.db.execute("SELECT record_type,count(*) FROM organized_records WHERE active=1"+(" AND topic=?" if topic != "all" else "")+" GROUP BY record_type", args))
            result["coverage"] = dict(self.db.execute("SELECT input_kind||':'||state,count(*) FROM organized_coverage"+where+" GROUP BY 1", args))
            result["jobs"] = dict(self.db.execute("SELECT stage||':'||state,count(*) FROM organized_jobs"+where+" GROUP BY 1", args))
            result["estimated_api_cost_usd"] = self.db.execute("SELECT COALESCE(sum(cost),0) FROM organized_jobs"+where, args).fetchone()[0]
            result["dataset_row_disposition"] = {}
            for row in self.db.execute("SELECT payload FROM organized_tables"+where, args):
                for k, v in json.loads(row[0]).get("row_counts", {}).items():
                    result["dataset_row_disposition"][k] = result["dataset_row_disposition"].get(k, 0)+v
        return result
