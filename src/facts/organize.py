"""Organize the current corpus without collecting new sources or draining extraction."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

from .contracts import PROMPT_VERSION, TAXONOMY_VERSION, VERSION, Observation
from .core import MODEL, ROOT, dumps, ident, now
from .organize_fields import FACETS, adapt_item, measurement, parse_date, plain, pointer
from .organize_llm import Budget, enqueue, run_jobs, settings
from .organize_store import OrganizedStore, name_key
from .organize_tables import infer_mapping, mapping_input, normalize_table, raw_rows, repair_mapping, validate_mapping

ADAPTER_VERSION = "observations-v2"


def input_digest(item):
    return ident("input", item)


def support_input(store, item):
    # Keep the same JSON-pointer field paths as the immutable item. Operational
    # metadata/references are excluded from the model's semantic evidence.
    result = {k: v for k, v in item.items() if k in {"id", "kind", "title", "detail", "original_text", "dates", "entities", "measurements", "tags", "data", "published"}}
    context = item.get("collection_metadata", {}).get("extraction_context", {})
    if context.get("job_id"):
        row = store.db.execute("SELECT text_artifact FROM extraction_jobs WHERE id=?", (context["job_id"],)).fetchone()
        if row:
            text = store.read(row[0])
            original = plain(item.get("original_text"))
            offset = text.find(original) if original else -1
            result["support"] = text[max(0, offset-250):offset+len(original)+250] if offset >= 0 else ""
    return result


def grounded(input_value, path, expression=None):
    try:
        value = pointer(input_value, path)
    except (KeyError, IndexError, ValueError, TypeError):
        raise ValueError("invalid source pointer: "+path) from None
    if value is None or value == "":
        raise ValueError("empty supporting source field")
    if expression is not None:
        text = value if isinstance(value, str) else dumps(value)
        if name_key(expression) not in name_key(text):
            raise ValueError("expression absent from cited input field")
    return value


def locate_support(store, original, observation):
    context = original.get("collection_metadata", {}).get("extraction_context", {})
    if not observation.original_text or not context.get("job_id"):
        return
    job = store.db.execute("SELECT text_artifact FROM extraction_jobs WHERE id=?", (context["job_id"],)).fetchone()
    if not job:
        return
    text = store.read(job[0]); offset = text.find(observation.original_text)
    if offset >= 0 and text.find(observation.original_text, offset+1) < 0:
        for reference in observation.references:
            reference["locator"] = {**reference.get("locator", {}), "chunk_artifact": job[0],
                "chunk_char_start": offset, "chunk_char_end": offset+len(observation.original_text), "precision": "exact_chunk_span"}
    else:
        observation.warnings.append("supporting_text_not_uniquely_located")


def temporal_context(observation, definition):
    dates = [d for d in observation.dates if d.start and d.role not in {"publication", "report", "acquisition", "reporting_period", "unknown"}]
    if not dates or not definition.get("date_from"):
        return "unknown"
    lower, upper = definition["date_from"], definition.get("date_to", "9999-12-31")
    labels = {"before_period" if d.end < lower else "after_period" if d.start > upper else "in_period" for d in dates}
    return next(iter(labels)) if len(labels) == 1 else "mixed"


def apply_semantic(store, job, payload, parsed, artifact):
    supplied = {p["id"]: p for p in payload["inputs"]}
    if len(parsed.results) != len(supplied) or {r.input_id for r in parsed.results} != set(supplied):
        raise ValueError("model output must account for every input exactly once")
    for result in parsed.results:
        original_row = store.db.execute("SELECT payload FROM items WHERE id=?", (result.input_id,)).fetchone()
        original = json.loads(original_row[0]); base = adapt_item(original)
        source = supplied[result.input_id]
        records = []
        for ordinal, part in enumerate(result.parts):
            if not part.evidence_paths:
                raise ValueError("part lacks source evidence")
            for path in part.evidence_paths:
                grounded(source, path)
            if any(c not in payload["taxonomy"] for c in part.categories):
                raise ValueError("category outside supplied taxonomy")
            title = grounded(source, part.title_path)
            detail = grounded(source, part.detail_path)
            if not isinstance(title, str) or not isinstance(detail, str):
                raise ValueError("title/detail must select text fields")
            value = base.model_copy(deep=True)
            value.id = ident("observation", original["id"], ordinal)
            value.title, value.detail = title[:240], detail
            value.kind, value.subtype = part.kind, part.subtype
            value.categories = part.categories or base.categories
            value.field_lineage.update({"kind": part.evidence_paths, "subtype": part.evidence_paths,
                "categories": part.evidence_paths, "title": [part.title_path], "detail": [part.detail_path]})
            mentions = list(base.entities)
            for mention in part.entities:
                grounded(source, mention.input_path, mention.name)
                if mention.canonical_name:
                    grounded(source, mention.input_path, mention.canonical_name)
                for alias in mention.aliases:
                    grounded(source, mention.input_path, alias)
                forms = {name_key(n) for n in [mention.name, mention.canonical_name, *mention.aliases] if n}
                # Same-source co-mentions alone do not justify merging the two
                # generations of a family name.
                has_junior = any(re.search(r"\b(?:jr|junior)\b", n) for n in forms)
                has_senior_form = any("donald trump" in n and not re.search(r"\b(?:jr|junior)\b", n) for n in forms)
                if has_junior and has_senior_form:
                    raise ValueError("alias joins distinct family members")
                mentions = [m for m in mentions if name_key(m.name) not in forms]
                mentions.append(mention)
            value.entities = mentions
            for attribution in part.attribution:
                grounded(source, attribution.input_path, attribution.name)
            value.attribution = list({(a.name, a.role): a for a in [*base.attribution, *part.attribution]}.values())
            for d in part.dates:
                grounded(source, d.input_path, d.expression)
                normalized = parse_date(d.expression, d.role, d.input_path)
                value.dates = [old for old in value.dates if old.original_expression != d.expression]
                value.dates.append(normalized)
            for m in part.measurements:
                grounded(source, m.input_path, m.expression)
                normalized = measurement(m.expression, unit=m.unit, metric=m.metric, period=m.period, basis=m.basis, value_kind=m.value_kind, input_path=m.input_path)
                # Do not replace a richer, already supplied numeric observation
                # with an unparseable expression proposed by the model.
                if normalized.value is not None or normalized.low is not None:
                    value.measurements = [old for old in value.measurements if old.original_expression != m.expression]
                    value.measurements.append(normalized)
            locate_support(store, original, value)
            definition_row = store.db.execute("SELECT payload FROM topics WHERE id=?", (original["topic"],)).fetchone()
            value.temporal_context = temporal_context(value, json.loads(definition_row[0]) if definition_row else {})
            value.processing.update({"method": "structured_llm", "job_id": job["id"], "response_artifact": artifact,
                "input_digest": input_digest(original), "adapter_version": ADAPTER_VERSION,
                "prompt_version": PROMPT_VERSION, "taxonomy_version": TAXONOMY_VERSION,
                "model": json.loads(job["settings"])["model"]})
            records.append(value)
        version = ident("observation-version", ADAPTER_VERSION, input_digest(original), job["id"])
        store.replace_input("item:"+original["id"], records, version)
        store.coverage("item", original["id"], original["topic"], input_digest(original), "semantic", [r.id for r in records], {"job_id": job["id"], "adapter_version": ADAPTER_VERSION}, version)


def apply_table_mapping(store, job, payload, parsed, artifact):
    parsed = validate_mapping(repair_mapping(parsed, payload), payload)
    store.db.execute("INSERT OR REPLACE INTO organized_mappings VALUES (?,?,?,?,?)",
        (payload["dataset_id"], payload["input_digest"], job["topic"], dumps(parsed.model_dump()), artifact))


def table_link_index(store, topics):
    output = {}
    for row in store.db.execute("SELECT payload FROM organized_tables"):
        table = json.loads(row[0])
        if table["topic"] not in topics:
            continue
        mapping = {}
        for r in pq.read_table(store.root / table["row_coverage_artifact"]).to_pylist():
            mapping[str(r["source_row"])] = json.loads(r["series_ids"])
        output[table["id"]] = (table["version"], mapping)
    return output


def adapt_all(store, topics):
    links = table_link_index(store, topics)
    definitions = {r[0]: json.loads(r[1]) for r in store.db.execute("SELECT id,payload FROM topics")}
    counts = Counter()
    for row in store.db.execute("SELECT * FROM items ORDER BY topic,id").fetchall():
        if row["topic"] not in topics:
            continue
        original = json.loads(row["payload"]); digest = input_digest(original)
        prior = store.db.execute("SELECT * FROM organized_coverage WHERE input_kind='item' AND input_id=?", (row["id"],)).fetchone()
        data, context = original.get("data") or {}, original.get("collection_metadata", {}).get("extraction_context", {})
        did = data.get("dataset_id")
        table_version = links[did][0] if did in links else None
        if prior and prior["input_digest"] == digest and json.loads(prior["payload"]).get("adapter_version") == ADAPTER_VERSION and json.loads(prior["payload"]).get("table_version") == table_version:
            counts["cached"] += 1; continue
        observation = adapt_item(original)
        observation.processing.update({"input_digest": digest, "adapter_version": ADAPTER_VERSION})
        if did in links:
            source_row = context.get("row") or data.get("source_row", {}).get("classification_id")
            observation.series_ids = links[did][1].get(str(source_row), [])
            observation.processing["table_version"] = table_version
            if not observation.series_ids:
                observation.warnings.append("dataset_row_has_no_normalized_series")
        observation.temporal_context = temporal_context(observation, definitions.get(row["topic"], {}))
        locate_support(store, original, observation)
        version = ident("observation-version", ADAPTER_VERSION, digest, table_version)
        store.replace_input("item:"+row["id"], [observation], version)
        store.coverage("item", row["id"], row["topic"], digest, "deterministic", [observation.id], {"adapter_version": ADAPTER_VERSION, "table_version": table_version}, version)
        counts["adapted"] += 1
        if counts["adapted"] % 250 == 0:
            store.db.commit()
    store.db.commit()
    return dict(counts)


def prose_inputs(store, topics, pilot=False):
    buckets = defaultdict(list)
    for row in store.db.execute("SELECT payload FROM items ORDER BY topic,id"):
        p = json.loads(row[0]); origin = p.get("collection_metadata", {}).get("extraction_context", {}).get("type")
        if p["topic"] in topics and origin in {"extracted_chunk", "structured_packet_item"}:
            buckets[(p["topic"], origin)].append(p)
    if not pilot:
        return [p for key in sorted(buckets) for p in buckets[key]]
    # Round-robin across topic/import-origin buckets so a large origin cannot
    # consume the pilot and exclude the structured research packets.
    chosen = []
    for i in range(100):
        for key in sorted(buckets):
            if i < len(buckets[key]):
                chosen.append(buckets[key][i])
                if len(chosen) == 100:
                    return chosen
    return chosen


def enqueue_semantic(store, inputs, model):
    buckets = defaultdict(list)
    for original in inputs:
        prior = store.db.execute("SELECT state,input_digest,payload FROM organized_coverage WHERE input_kind='item' AND input_id=?", (original["id"],)).fetchone()
        if prior and prior["state"] == "semantic" and prior["input_digest"] == input_digest(original):
            # Settings changes must invalidate semantic cache independently of the adapter.
            job_id = json.loads(prior["payload"]).get("job_id")
            previous = store.db.execute("SELECT settings FROM organized_jobs WHERE id=?", (job_id,)).fetchone()
            if previous and json.loads(previous[0]) == settings("semantic", model):
                continue
        refs = original.get("references") or []
        source = refs[0].get("source_id") if refs else original["id"]
        buckets[(original["topic"], source)].append(support_input(store, original))
    ids = set()
    for (topic, source), values in sorted(buckets.items()):
        batch, length = [], 0
        for value in values:
            size = len(dumps(value))
            if batch and (len(batch) == 10 or length+size > 28000):
                ids.add(enqueue(store, topic, "semantic", {"taxonomy": FACETS.get(topic, {"other": []}), "source_id": source, "inputs": batch}, model))
                batch, length = [], 0
            batch.append(value); length += size
        if batch:
            ids.add(enqueue(store, topic, "semantic", {"taxonomy": FACETS.get(topic, {"other": []}), "source_id": source, "inputs": batch}, model))
    store.db.commit()
    return ids


def organize(root=ROOT, topic="all", *, dry_run=False, pilot=False, maximum=10, workers=2, model=MODEL, use_llm=True):
    if workers < 1 or workers > 2:
        raise ValueError("Organization supports one or two LLM requests at a time")
    store = OrganizedStore(root, readonly=dry_run)
    if dry_run:
        result = store.inventory(topic)
        result.update({"mode": "dry-run", "schema_version": VERSION,
            "cost_limit_usd": maximum, "model": model, "note": "No files, jobs or API requests created. Prices are estimates; original extraction queue is not processed."})
        return result
    topics = [r[0] for r in store.db.execute("SELECT id FROM topics ORDER BY id") if topic == "all" or r[0] == topic]
    if not topics:
        raise ValueError("Unknown topic")
    budget = Budget(maximum)
    run_id = ident("organize-run", now(), topics, pilot)
    run = {"id": run_id, "started": now(), "topics": topics, "pilot": pilot, "schema_version": VERSION,
           "model": model, "cost_limit_usd": maximum, "state": "running"}
    with store.locks(topics):
        backup = store.root / "storage/backups/catalog-before-organization.sqlite"
        if not backup.exists():
            backup.parent.mkdir(exist_ok=True)
            target = sqlite3.connect(backup)
            store.db.backup(target); target.close()
        store.db.execute("INSERT INTO organized_runs VALUES (?,?,?)", (run_id, topic, dumps(run))); store.db.commit()
        datasets = [json.loads(row[0]) for row in store.db.execute("SELECT payload FROM datasets ORDER BY id") if "us-federal-spending" in topics]
        table_work = []
        for manifest in datasets:
            rows = raw_rows(store, manifest)
            digest = ident("table-input", manifest["raw_artifact"], manifest["metadata"], manifest.get("parquet"))
            mapping = infer_mapping(manifest, rows)
            context = {**mapping_input(manifest, rows), "input_digest": digest}
            if mapping.orientation == "unresolved":
                # Revalidate saved output after parser repairs before paying for
                # another response. Successful mappings also get representation
                # repairs (e.g. explicit year-column orientation).
                from .contracts import TableMapping
                recovered = False
                for previous in store.db.execute("SELECT * FROM organized_jobs WHERE stage='table_mapping' AND response_artifact IS NOT NULL ORDER BY updated DESC").fetchall():
                    previous_payload = json.loads(store.read(previous["input_artifact"]))
                    if previous_payload.get("dataset_id") != manifest["id"] or previous_payload.get("input_digest") != digest:
                        continue
                    try:
                        response = json.loads(store.read(previous["response_artifact"]))
                        choice = response["raw"]["choices"][0]
                        if choice.get("finish_reason") == "length":
                            continue
                        parsed = TableMapping.model_validate_json(choice["message"]["content"])
                        apply_table_mapping(store, previous, context, parsed, previous["response_artifact"])
                        recovered = True
                        break
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
                if not recovered and use_llm:
                    enqueue(store, "us-federal-spending", "table_mapping", context, model)
            table_work.append((manifest, mapping, digest))
        store.db.commit()
        if use_llm:
            run["mapping_jobs"] = run_jobs(store, topics, "table_mapping", budget,
                lambda job, payload, parsed, artifact: apply_table_mapping(store, job, payload, parsed, artifact), workers)
        for i, (manifest, mapping, digest) in enumerate(table_work, 1):
            mapped = store.db.execute("SELECT payload,artifact FROM organized_mappings WHERE dataset_id=? AND digest=?", (manifest["id"], digest)).fetchone()
            artifact = None
            if mapped:
                from .contracts import TableMapping
                mapping, artifact = TableMapping.model_validate_json(mapped[0]), mapped[1]
            table = normalize_table(store, manifest, mapping, digest, artifact)
            print(f"organize tables {i}/{len(table_work)}: {manifest['name']}; {table['series_count']} series; {table['row_counts']}", flush=True)
        run["adaptation"] = adapt_all(store, topics)
        inputs = prose_inputs(store, topics, pilot)
        if use_llm:
            ids = enqueue_semantic(store, inputs, model)
            run["semantic_jobs"] = run_jobs(store, topics, "semantic", budget,
                lambda job, payload, parsed, artifact: apply_semantic(store, job, payload, parsed, artifact), workers, ids)
        from .organize_groups import build_groups
        run["grouping"] = build_groups(store, topics, inputs, budget, model, workers, use_llm)
        run.update({"finished": now(), "state": "budget_limited" if budget.stopped else "finished",
                    "estimated_api_cost_usd": budget.spent, "coverage": store.inventory(topic)})
        store.db.execute("UPDATE organized_runs SET payload=? WHERE id=?", (dumps(run), run_id)); store.db.commit()
        output = store.root / "storage/organized/runs"
        output.mkdir(parents=True, exist_ok=True)
        (output / (run_id+".json")).write_text(dumps(run)+"\n")
        return run
