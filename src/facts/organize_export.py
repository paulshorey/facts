"""Portable versioned organization exports and read-only series inspection."""
from __future__ import annotations

import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from .contracts import VERSION, Observation, SeriesDefinition, EntityDefinition, GroupDefinition
from .core import ROOT, dumps, ident, now
from .organize_store import OrganizedStore

EXPORT_VERSION = "organized-export.v1"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+"\n")


def export_organized(root=ROOT, topic="all"):
    store = OrganizedStore(root, readonly=True)
    store.db.execute("BEGIN")
    if not store.exists():
        raise ValueError("Run facts organize before exporting organized data")
    topics = [r[0] for r in store.db.execute("SELECT id FROM topics ORDER BY id") if topic == "all" or r[0] == topic]
    results = []
    for selected in topics:
        inventory = store.inventory(selected)
        records = store.query(selected, limit=0)
        source_index = {r["id"]:dict(r) for r in store.db.execute("SELECT * FROM sources")}
        capture_index = {r["id"]:dict(r) for r in store.db.execute("SELECT id,level,state,artifact,text_artifact,acquired FROM captures")}
        for record in records:
            for reference in record.get("references", []):
                source = source_index.get(reference.get("source_id"), {})
                capture = capture_index.get(reference.get("capture_id"), {})
                reference.update({"source_title":source.get("title"),"published":source.get("published"),
                    "acquisition_type":capture.get("level"),"capture_state":capture.get("state"),
                    "acquired_at":capture.get("acquired"),"capture_artifact":capture.get("artifact"),
                    "capture_text_artifact":capture.get("text_artifact")})
        groups = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM organized_groups WHERE topic=? ORDER BY id", (selected,))]
        tables = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM organized_tables WHERE topic=? ORDER BY id", (selected,))]
        entities = [json.loads(r[0]) for r in store.db.execute("SELECT DISTINCT e.payload FROM organized_entities e JOIN organized_links l ON l.target_id=e.id AND l.target_kind='entity' JOIN organized_records r ON r.id=l.record_id AND r.version=l.version AND r.active=1 WHERE r.topic=? ORDER BY e.id", (selected,))]
        snapshot = ident("snapshot", EXPORT_VERSION, selected, [(r["id"], r["processing"]["version"]) for r in records], groups, inventory, source_index, capture_index, entities, tables)
        parent = store.root / "exports" / selected / "organized-v1"
        parent.mkdir(parents=True, exist_ok=True)
        final = parent / snapshot
        if not final.exists():
            with tempfile.TemporaryDirectory(dir=parent, prefix=".export-") as scratch:
                folder = Path(scratch)
                (folder / "tables").mkdir()
                (folder / "schemas").mkdir()
                for name, contract in [("observation", Observation), ("series", SeriesDefinition), ("entity", EntityDefinition), ("group", GroupDefinition)]:
                    write_json(folder / "schemas" / (name+".schema.json"), contract.model_json_schema())
                artifacts = set()
                for table in tables:
                    for field in ["values_artifact", "row_coverage_artifact"]:
                        path = store.root / table[field]
                        artifacts.add(table[field])
                        shutil.copy2(path, folder / "tables" / path.name)
                        table[field] = "tables/"+path.name
                for filename, kind in [("observations.jsonl", "observation"), ("source_entries.jsonl", "source_reference"), ("series.jsonl", "series")]:
                    with (folder / filename).open("w") as output:
                        for record in records:
                            if record["record_type"] == kind:
                                value = dict(record)
                                if kind == "series":
                                    value["values_artifact"] = "tables/"+Path(value["values_artifact"]).name
                                output.write(dumps(value)+"\n")
                with (folder / "groups.jsonl").open("w") as output:
                    for group in groups:
                        output.write(dumps(group)+"\n")
                with (folder / "entities.jsonl").open("w") as output:
                    for entity in entities:
                        output.write(dumps(entity)+"\n")
                sources = [dict(r) for r in store.db.execute("SELECT DISTINCT s.* FROM sources s JOIN topic_sources t ON t.source=s.id WHERE t.topic=? ORDER BY s.id", (selected,))]
                (folder / "sources.jsonl").write_text("".join(dumps(r)+"\n" for r in sources))
                references = {}
                for record in records:
                    for reference in record.get("references", []):
                        key = ident("reference", reference)
                        references[key] = {"id": key, **reference}
                (folder / "references.jsonl").write_text("".join(dumps(r)+"\n" for r in references.values()))
                (folder / "datasets.jsonl").write_text("".join(dumps(r)+"\n" for r in tables))
                with (folder / "coverage.jsonl").open("w") as output:
                    for row in store.db.execute("SELECT * FROM organized_coverage WHERE topic=? ORDER BY input_kind,input_id", (selected,)):
                        value = dict(row)
                        value["record_ids"] = json.loads(value["record_ids"])
                        value["payload"] = json.loads(value["payload"])
                        output.write(dumps(value)+"\n")
                roles = Counter(r.get("content_role") for r in records)
                counts = Counter(c for r in records for c in r.get("categories", []))
                manifest = {"schema_version": VERSION, "exporter_version": EXPORT_VERSION, "snapshot": snapshot, "topic": selected, "exported_at": now(),
                    "counts": inventory, "content_roles": dict(roles), "categories": dict(counts),
                    "table_values": sum(t["value_count"] for t in tables),
                    "source_reference_count": len(references), "artifact_paths": "values_artifact and row_coverage_artifact are export-relative; original/raw/capture artifact pointers are private collector-root-relative provenance",
                    "raw_artifacts": "retained privately in the collector; not included in this portable export",
                    "costs": "token-based estimates; unknown usage charged at reserved upper bound",
                    "corrections": [{"id": "treasury-api-dollars-v1", "description": "Organized Treasury values use the saved API dollars. Legacy item.v1 measurements and dataset unit labels can contain a million-fold scaling error; originals retained."}],
                    "count_semantics": "Observations are not unique incidents. Rows/values can overlap and are not additive totals."}
                write_json(folder / "manifest.json", manifest)
                lines = [f"# Organized collection: {selected}", "", f"Snapshot: `{snapshot}`", "", "## Coverage", "", "```json", json.dumps(inventory, indent=2), "```", "", "## Categories", "", *[f"- {k}: {v}" for k, v in counts.most_common()], "", "## Observation preview", "", "First 200 substantive observations in stable ID order; all records are in JSONL.", ""]
                observations = [r for r in records if r["record_type"] == "observation" and r.get("content_role") == "substantive"]
                for record in observations[:200]:
                    lines += [f"### {record['title']}", "", record.get("detail", ""), "", f"ID: `{record['id']}`; subtype: `{record['subtype']}`; categories: {', '.join(record['categories'])}", ""]
                    for reference in record.get("references", []):
                        if reference.get("url"):
                            lines += [f"- [Source]({reference['url']})"]
                    lines.append("")
                lines += ["## Dataset transformations", "", "| Dataset | Rows | Series | Values |", "| --- | --- | ---: | ---: |"]
                for table in tables:
                    lines.append(f"| {table['name']} | {table['row_counts']} | {table['series_count']} | {table['value_count']} |")
                (folder / "inventory.md").write_text("\n".join(lines)+"\n")
                folder.rename(final)
        latest = parent / "latest.json"
        temporary = parent / ".latest.tmp"
        write_json(temporary, {"schema_version": VERSION, "snapshot": snapshot, "path": snapshot, "manifest": snapshot+"/manifest.json", "inventory": snapshot+"/inventory.md"})
        temporary.replace(latest)
        # Convenient human entry point; immutable full snapshot remains above.
        shutil.copyfile(final / "inventory.md", parent / "inventory.md")
        results.append({"topic": selected, "path": str(final.relative_to(store.root)), "snapshot": snapshot, "counts": inventory})
    store.db.close()
    return results


def query_series(root=ROOT, topic="all", *, period=None, basis=None, limit=20):
    import duckdb
    store = OrganizedStore(root, readonly=True)
    if not store.exists():
        return []
    args = () if topic == "all" else (topic,)
    sql = "SELECT payload FROM organized_tables"+(" WHERE topic=?" if topic != "all" else "")
    files = [str(store.root / json.loads(r[0])["values_artifact"]) for r in store.db.execute(sql, args)]
    store.db.close()
    if not files:
        return []
    clauses, params = [], [files]
    if period:
        clauses.append("period=?"); params.append(period)
    if basis:
        clauses.append("basis=?"); params.append(basis)
    query = "SELECT * FROM read_parquet(?,union_by_name=true)"+(" WHERE "+" AND ".join(clauses) if clauses else "")+" ORDER BY series_id,period,source_row LIMIT ?"
    params.append(limit)
    with duckdb.connect() as db:
        result = db.execute(query, params)
        return [dict(zip([c[0] for c in result.description], row)) for row in result.fetchall()]
