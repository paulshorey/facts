"""Typed ``research`` read model rebuilt from one consistent local catalog snapshot.

The lossless ``facts.records`` mirror stays the import contract. This module
derives query-ready relational tables from the same SQLite read transaction
(and the snapshot's retained Parquet value files) inside the import's
PostgreSQL transaction, so readers see either the previous or the new model.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

MODEL_VERSION = "research-v1"
NON_EVENT_ROLES = {"publication", "report", "acquisition", "reporting_period", "unknown", "article_date", "publication_date"}
NUMBER = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?")
# Children before parents for deletion; reversed for loading.
TABLES = ("series_values", "series", "datasets", "group_members", "groups", "observation_sources",
          "attributions", "observation_entities", "measurements", "observation_dates", "observations",
          "entity_aliases", "entities", "captures", "topic_sources", "sources", "topics")


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _date(value):
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _number(value):
    if value is None:
        return None
    text = str(value).strip()
    if not NUMBER.fullmatch(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _table(store, name):
    return store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _copy(connection, table, columns, rows, counts):
    from psycopg import sql
    statement = sql.SQL("COPY research.{} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns)))
    count = 0
    with connection.cursor() as cursor, cursor.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)
            count += 1
    counts[table] = count


def current_receipt(connection):
    row = connection.execute("SELECT receipt_id, model_version FROM research.refresh_state WHERE id='current'").fetchone()
    return tuple(row) if row else None


def refresh(store, connection, receipt_id):
    """Replace the read model with the active catalog. Caller holds the SQLite snapshot and PG transaction."""
    from psycopg.types.json import Jsonb

    if current_receipt(connection) == (receipt_id, MODEL_VERSION):
        return {"refreshed": False, "reason": "read model already matches this receipt"}
    # DELETE (not TRUNCATE) keeps concurrent readers on the previous MVCC snapshot.
    for table in TABLES:
        connection.execute(f"DELETE FROM research.{table}")
    connection.execute("DELETE FROM research.refresh_state")
    counts = {}
    db = store.db

    def topics():
        for row in db.execute("SELECT id,payload FROM topics ORDER BY id"):
            payload = _json(row["payload"])
            yield row["id"], payload.get("title"), Jsonb(payload)
    _copy(connection, "topics", ("id", "title", "payload"), topics(), counts)

    def sources():
        for row in db.execute("SELECT * FROM sources ORDER BY id"):
            yield (row["id"], row["url"], urlsplit(row["url"]).hostname, row["title"], row["published"],
                   _date(row["published"]), _timestamp(row["created"]))
    _copy(connection, "sources", ("id", "url", "host", "title", "published", "published_date", "created_at"), sources(), counts)
    _copy(connection, "topic_sources", ("topic", "source_id", "family", "search_id"),
          (tuple(r) for r in db.execute("SELECT topic,source,family,search_id FROM topic_sources ORDER BY 1,2,3")), counts)

    def captures():
        for row in db.execute("SELECT * FROM captures ORDER BY id"):
            yield (row["id"], row["source"], row["level"], row["state"], row["status"], _timestamp(row["acquired"]),
                   row["artifact"], row["text_artifact"], Jsonb(_json(row["metadata"])))
    _copy(connection, "captures", ("id", "source_id", "level", "state", "http_status", "acquired_at", "artifact",
                                   "text_artifact", "metadata"), captures(), counts)

    if not _table(store, "organized_records"):
        for table in TABLES:
            counts.setdefault(table, 0)
        return _finish(connection, receipt_id, counts)

    observations, versions = [], {}
    series = []
    for row in db.execute("SELECT payload FROM organized_records WHERE active=1 ORDER BY id"):
        payload = _json(row["payload"])
        versions[payload["id"]] = payload["processing"]["version"]
        (series if payload["record_type"] == "series" else observations).append(payload)

    # Only entities linked from active observations; retired versions leave entities behind locally.
    linked = {e.get("entity_id") for p in observations for e in p.get("entities", []) if e.get("entity_id")}
    entities = {}
    for row in db.execute("SELECT id,payload FROM organized_entities ORDER BY id"):
        if row["id"] in linked:
            entities[row["id"]] = _json(row["payload"])
    _copy(connection, "entities", ("id", "name", "entity_type", "aliases", "payload"),
          ((k, v["name"], v.get("entity_type", "unknown"), v.get("aliases", []), Jsonb(v)) for k, v in entities.items()), counts)
    _copy(connection, "entity_aliases", ("entity_id", "alias", "entity_type"),
          (tuple(r) for r in db.execute("SELECT entity_id,alias,entity_type FROM organized_aliases ORDER BY 1,2")
           if r["entity_id"] in entities), counts)

    def observation_rows():
        for p in observations:
            # Provenance, reporting-period and unknown-role dates are not the event window
            # (same exclusions as temporal classification and grouping); all dates stay in observation_dates.
            dates = [d for d in p.get("dates", []) if (d.get("start") or d.get("end")) and d.get("role") not in NON_EVENT_ROLES]
            starts = [_date(d.get("start")) for d in dates if _date(d.get("start"))]
            ends = [_date(d.get("end") or d.get("start")) for d in dates if _date(d.get("end") or d.get("start"))]
            yield (p["id"], p["processing"]["version"], p["topic"], p["record_type"], p["content_role"], p["kind"],
                   p["subtype"], p["title"], p.get("detail", ""), p.get("original_text", ""),
                   min(starts) if starts else None, max(ends) if ends else None, p.get("temporal_context"),
                   p.get("categories", []), p.get("original_tags", []), p.get("warnings", []), p.get("input_ids", []),
                   p["processing"].get("method"), Jsonb(p))
    _copy(connection, "observations", ("id", "version", "topic", "record_type", "content_role", "kind", "subtype", "title",
                                       "detail", "original_text", "event_start", "event_end", "temporal_context", "categories",
                                       "original_tags", "warnings", "input_ids", "method", "payload"), observation_rows(), counts)
    _copy(connection, "observation_dates", ("observation_id", "ordinal", "role", "original_expression", "start_text", "end_text",
                                            "start_date", "end_date", "precision", "calendar"),
          ((p["id"], i, d.get("role", "unknown"), d.get("original_expression", ""), d.get("start"), d.get("end"),
            _date(d.get("start")), _date(d.get("end")), d.get("precision", "unknown"), d.get("calendar", "unknown"))
           for p in observations for i, d in enumerate(p.get("dates", []))), counts)
    _copy(connection, "measurements", ("observation_id", "ordinal", "topic", "metric", "value", "low", "high", "value_text",
                                       "comparator", "unit", "original_unit", "scale", "period", "basis", "original_basis",
                                       "value_kind", "qualifier", "original_expression"),
          ((p["id"], i, p["topic"], m.get("metric", "unspecified"), _number(m.get("value")), _number(m.get("low")),
            _number(m.get("high")), m.get("value"), m.get("comparator", "unknown"), m.get("unit"), m.get("original_unit"),
            m.get("scale"), m.get("period"), m.get("basis"), m.get("original_basis"), m.get("value_kind", "unknown"),
            m.get("qualifier"), m.get("original_expression", ""))
           for p in observations for i, m in enumerate(p.get("measurements", []))), counts)
    _copy(connection, "observation_entities", ("observation_id", "ordinal", "entity_id", "name", "canonical_name",
                                               "entity_type", "role"),
          ((p["id"], i, e.get("entity_id"), e["name"], e.get("canonical_name"), e.get("entity_type", "unknown"),
            e.get("role", "mentioned")) for p in observations for i, e in enumerate(p.get("entities", []))), counts)
    _copy(connection, "attributions", ("observation_id", "ordinal", "name", "role"),
          ((p["id"], i, a["name"], a["role"]) for p in observations for i, a in enumerate(p.get("attribution", []))), counts)
    _copy(connection, "observation_sources", ("observation_id", "ordinal", "source_id", "capture_id", "url", "locator"),
          ((p["id"], i, r.get("source_id"), r.get("capture_id"), r.get("url"), Jsonb(r.get("locator") or {}))
           for p in observations for i, r in enumerate(p.get("references", []))), counts)

    groups = {}
    for row in db.execute("SELECT payload FROM organized_groups ORDER BY id"):
        payload = _json(row["payload"])
        groups[payload["id"]] = payload
    by_id = {p["id"]: p for p in observations}
    current = []
    for row in db.execute("SELECT group_id,record_id,relation FROM organized_memberships ORDER BY 1,2,3"):
        group = groups.get(row["group_id"])
        if group is None or row["record_id"] not in by_id:
            continue
        expected = (group.get("member_versions") or {}).get(row["record_id"])
        if expected and expected != versions.get(row["record_id"]):
            continue
        current.append((row["group_id"], row["record_id"], row["relation"]))
    members_of = {}
    for gid, rid, _ in current:
        members_of.setdefault(gid, set()).add(rid)

    def group_rows():
        # Stale groups (no current members) are omitted; counts describe current members only.
        for gid, g in groups.items():
            ids = members_of.get(gid)
            if not ids:
                continue
            urls = {r.get("url") for rid in ids for r in by_id[rid].get("references", []) if r.get("url")}
            yield (gid, g["topic"], g["group_kind"], g["title"], g.get("category"), g.get("anchor_id"), len(ids),
                   len(urls), g.get("version"), Jsonb(g))
    _copy(connection, "groups", ("id", "topic", "group_kind", "title", "category", "anchor_id", "observation_count",
                                 "source_url_count", "version", "payload"), group_rows(), counts)
    _copy(connection, "group_members", ("group_id", "observation_id", "relation"), iter(current), counts)

    tables = [_json(r["payload"]) | {"_state": r["state"], "_version": r["version"]}
              for r in db.execute("SELECT state,version,payload FROM organized_tables ORDER BY id")]
    _copy(connection, "datasets", ("id", "topic", "name", "state", "version", "orientation", "row_counts", "series_count",
                                   "value_count", "payload"),
          ((t["id"], t["topic"], t.get("name"), t["_state"], t["_version"], (t.get("mapping") or {}).get("orientation"),
            Jsonb(t.get("row_counts") or {}), t.get("series_count"), t.get("value_count"),
            Jsonb({k: v for k, v in t.items() if not k.startswith("_")})) for t in tables), counts)
    dataset_ids = {t["id"] for t in tables}
    series = [s for s in series if s["dataset_id"] in dataset_ids]

    def series_rows():
        for s in series:
            reference = (s.get("references") or [{}])[0]
            yield (s["id"], s["processing"]["version"], s["topic"], s["dataset_id"], s["title"], s.get("detail", ""), s["subtype"],
                   s["metric"], s.get("basis"), s.get("original_basis"), s.get("unit"), s.get("scale"), s.get("unit_evidence"),
                   s["row_role"], Jsonb(s.get("dimensions") or {}), [str(h) for h in s.get("hierarchy", [])],
                   s.get("categories", []), s.get("warnings", []), reference.get("source_id"), reference.get("capture_id"),
                   Jsonb(reference.get("locator")) if reference.get("locator") is not None else None, Jsonb(s))
    _copy(connection, "series", ("id", "version", "topic", "dataset_id", "title", "detail", "subtype", "metric", "basis",
                                 "original_basis", "unit", "scale", "unit_evidence", "row_role", "dimensions", "hierarchy",
                                 "categories", "warnings", "source_id", "capture_id", "locator", "payload"), series_rows(), counts)
    series_ids = {s["id"] for s in series}

    def values():
        import pyarrow.parquet as pq
        for t in tables:
            path = store.root / t["values_artifact"]
            # A referenced value file is retained evidence; absence is a broken snapshot, not an empty table.
            parquet = pq.ParquetFile(path)
            for batch in parquet.iter_batches(batch_size=20000):
                for v in batch.to_pylist():
                    if v["series_id"] not in series_ids:
                        continue
                    warnings = _json(v.get("warnings")) or []
                    yield (v["series_id"], v["period"], v["source_row"], v["source_column"], v["dataset_id"],
                           _date(v.get("period_start")), _date(v.get("period_end")), v.get("calendar"), v.get("period_role"),
                           v.get("value"), v.get("source_value"), v.get("unit"), v.get("scale"), v.get("basis"),
                           v.get("value_kind"), v.get("original_expression"), [str(w) for w in warnings])
    _copy(connection, "series_values", ("series_id", "period", "source_row", "source_column", "dataset_id", "period_start",
                                        "period_end", "calendar", "period_role", "value", "source_value", "unit", "scale",
                                        "basis", "value_kind", "original_expression", "warnings"), values(), counts)
    return _finish(connection, receipt_id, counts)


def _finish(connection, receipt_id, counts):
    from psycopg.types.json import Jsonb
    for table in TABLES:
        connection.execute(f"ANALYZE research.{table}")
    connection.execute("""INSERT INTO research.refresh_state VALUES ('current',%s,%s,%s,now())""",
                       (receipt_id, MODEL_VERSION, Jsonb(counts)))
    return {"refreshed": True, "model_version": MODEL_VERSION, "counts": counts}
