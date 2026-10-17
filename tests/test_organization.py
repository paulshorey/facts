import json
from decimal import Decimal

import pyarrow.parquet as pq
import pytest

from facts.contracts import GroupBatch, Observation, SemanticBatch, TableMapping, ValueColumn
from facts.core import Store, dumps
from facts.datasets import dataset
from facts.organize import apply_semantic, input_digest, organize
from facts.organize_fields import adapt_item, measurement, parse_date
from facts.organize_groups import compatible
from facts.organize_llm import Budget, enqueue, run_jobs
from facts.organize_store import OrganizedStore
from facts.organize_tables import infer_mapping, mapping_input, normalize_table, raw_rows, validate_mapping


def topic(store, slug="topic"):
    store.db.execute("INSERT OR IGNORE INTO topics VALUES (?,?)", (slug, dumps({"slug": slug, "date_from": "2025-01-20", "date_to": "2026-10-16"})))
    store.db.commit()


def item(store, title="Company announces a drone contract", detail="Acme announced a drone contract on May 2, 2025.", **extra):
    sid = store.source("topic", "https://example.com/report", "Report")
    cid = store.capture(sid, "direct_http", b"source", detail)
    iid = store.item("topic", sid, cid, {"title": title, "detail": detail, "kind": "event", **extra}, {"type": "extracted_chunk"})
    store.db.commit()
    return json.loads(store.db.execute("SELECT payload FROM items WHERE id=?", (iid,)).fetchone()[0])


def part():
    return {"kind": "event", "subtype": "contract", "categories": ["other"], "evidence_paths": ["/detail"],
            "entities": [{"name": "Acme", "entity_type": "organization", "role": "participant", "input_path": "/detail"}],
            "attribution": [], "dates": [{"expression": "May 2, 2025", "role": "event", "input_path": "/detail"}],
            "measurements": [], "title_path": "/title", "detail_path": "/detail"}


def test_explicit_scales_and_existing_base_values():
    assert measurement("$2.5 billion", unit="USD").value == "2500000000.0"
    assert Decimal(measurement("$2 billion", value="2000000000", unit="USD").value) == Decimal("2000000000")
    assert Decimal(measurement("1.8", value="1.8", unit="trillion dollars").value) == Decimal("1800000000000")
    assert measurement("0", unit="USD").value == "0"
    assert measurement("-12.50", unit="USD").value == "-12.50"
    assert measurement("null", unit="USD").value is None
    assert measurement("hundreds of millions of dollars").value is None
    assert measurement("about $3 million").comparator == "approximate"
    r = measurement("$10–20 million")
    assert Decimal(r.low) == 10000000 and Decimal(r.high) == 20000000 and r.value is None


def test_dates_keep_precision_fiscal_calendar_and_unknown_expressions():
    assert parse_date("2025-02").end == "2025-02-28"
    assert parse_date("FY2025").start == "2024-10-01"
    assert parse_date("FY1962").start is None
    assert parse_date("March 2025").precision == "month"
    assert parse_date("2025-02-31").start is None
    assert parse_date("2025–2034").calendar == "unknown"
    assert parse_date("6/30/26").start is None


def test_legacy_treasury_rebuilt_from_raw_dollars_without_mutating_input(tmp_path):
    store = Store(tmp_path); topic(store)
    original = item(store, kind="statistic", measurements=[{"value": "7009973667049300000", "unit": "USD", "original_unit": "millions of USD"}],
        data={"dataset_id": "d", "source_row": {"current_fytd_net_outly_amt": "7009973667049.30", "record_fiscal_year": "2025"}})
    original["collection_metadata"]["extraction_context"]["type"] = "dataset_row"
    saved = dumps(original)
    observed = adapt_item(original)
    assert observed.measurements[0].value == "7009973667049.30"
    assert observed.processing["corrections"] == ["treasury-api-dollars-v1"]
    assert dumps(original) == saved


def test_treasury_table_values_and_hierarchy(tmp_path):
    s = Store(tmp_path); topic(s, "us-federal-spending")
    sid = s.source("us-federal-spending", "https://example.com/mts")
    rows = [{"record_date": "2025-09-30", "record_fiscal_year": "2025", "classification_id": "root", "parent_id": "null", "classification_desc": "Net Outlays", "current_fytd_net_outly_amt": "null"},
            {"record_date": "2025-09-30", "record_fiscal_year": "2025", "classification_id": "total", "parent_id": "root", "classification_desc": "Total Outlays", "current_fytd_net_outly_amt": "7009973667049.30"}]
    raw = s.put({"data": rows}); did = dataset(s, sid, "mts_table_5-2025-09-30", rows, {}, raw)
    o = OrganizedStore(tmp_path)
    manifest = json.loads(o.db.execute("SELECT payload FROM datasets WHERE id=?", (did,)).fetchone()[0])
    mapping = infer_mapping(manifest, rows)
    result = normalize_table(o, manifest, mapping, "input-digest")
    values = pq.read_table(tmp_path / result["values_artifact"]).to_pylist()
    assert values[0]["value"] == Decimal("7009973667049.30")
    series = json.loads(o.db.execute("SELECT payload FROM organized_records WHERE record_type='series'").fetchone()[0])
    assert series["hierarchy"] == ["Net Outlays"]
    assert result["row_counts"] == {"metadata": 1, "normalized": 1}


def test_accounts_with_same_title_different_dimensions_do_not_merge(tmp_path):
    s = Store(tmp_path); topic(s, "us-federal-spending")
    sid = s.source("us-federal-spending", "https://example.com/outlays.xlsx")
    rows = [{"source_row": str(i+2), "Agency Name": "Agency", "Account Name": "Other", "Account Code": code, "2025": amount} for i, (code, amount) in enumerate([("001", "0"), ("002", "-100")])]
    did = dataset(s, sid, "outlays_fy2027.xlsx:Sheet", rows, {"headers": ["Agency Name", "Account Name", "Account Code", "2025"]}, s.put(b"raw", binary=True))
    o = OrganizedStore(tmp_path); manifest = json.loads(o.db.execute("SELECT payload FROM datasets WHERE id=?", (did,)).fetchone()[0])
    result = normalize_table(o, manifest, infer_mapping(manifest, rows), "digest")
    values = pq.read_table(tmp_path / result["values_artifact"]).to_pylist()
    assert len({v["series_id"] for v in values}) == 2
    assert values[0]["source_value"] == 0 and values[1]["source_value"] == -100
    assert all(v["value"] is None and v["unit"] is None for v in values)
    before = o.db.execute("SELECT count(*) FROM organized_records").fetchone()[0]
    normalize_table(o, manifest, infer_mapping(manifest, rows), "digest")
    assert o.db.execute("SELECT count(*) FROM organized_records").fetchone()[0] == before


def test_mapping_rejects_invented_columns_or_unsupported_unit_evidence():
    context = {"columns": ["source_row", "column_1", "column_2"], "rows": [{"source_row": "1", "column_1": "in millions of dollars"}]}
    with pytest.raises(ValueError):
        validate_mapping(TableMapping(orientation="wide", value_columns=[ValueColumn(column="invented", metric="amount")]), context)
    with pytest.raises(ValueError):
        validate_mapping(TableMapping(orientation="wide", value_columns=[ValueColumn(column="column_2", metric="amount", unit="USD", scale="1000000", unit_evidence="in billions of dollars")]), context)
    valid = TableMapping(orientation="wide", value_columns=[ValueColumn(column="column_2", metric="amount", unit="USD", scale="1000000", unit_evidence="in millions of dollars")])
    assert validate_mapping(valid, context) == valid


def test_semantic_results_require_complete_inputs_and_grounded_fields(tmp_path):
    s = Store(tmp_path); topic(s); original = item(s)
    o = OrganizedStore(tmp_path)
    job = {"id": "job", "settings": dumps({"model": "test"})}
    payload = {"taxonomy": {"other": []}, "inputs": [original]}
    valid = SemanticBatch.model_validate({"results": [{"input_id": original["id"], "parts": [part()]}]})
    apply_semantic(o, job, payload, valid, "response-artifact"); o.db.commit()
    record = o.query("topic")[0]
    assert record["dates"][0]["start"] == "2025-05-02"
    assert record["entities"][0]["entity_id"]
    with pytest.raises(ValueError, match="every input"):
        apply_semantic(o, job, payload, SemanticBatch(results=[]), "artifact")
    invalid = part(); invalid["entities"][0]["name"] = "Invented Person"
    with pytest.raises(ValueError, match="absent"):
        apply_semantic(o, job, payload, SemanticBatch.model_validate({"results": [{"input_id": original["id"], "parts": [invalid]}]}), "artifact")


def test_same_entity_different_occurrences_are_incompatible():
    base = {"title": "Acme received a drone contract", "entities": [{"name": "Acme"}], "dates": [{"role": "event", "start": "2025-05-02", "end": "2025-05-02"}]}
    other = {**base, "title": "Acme received another drone contract", "dates": [{"role": "event", "start": "2025-09-02", "end": "2025-09-02"}]}
    assert not compatible(base, other)
    assert not compatible(base, {**base, "title": "Acme announced a drone investment"})


def test_dry_run_does_not_create_derived_schema_or_change_database(tmp_path):
    s = Store(tmp_path); topic(s); item(s); s.db.close()
    path = tmp_path / "storage/catalog.sqlite"
    before = path.read_bytes()
    report = organize(tmp_path, dry_run=True)
    assert report["input_items"] == 1
    assert path.read_bytes() == before
    assert not OrganizedStore(tmp_path, readonly=True).exists()


def test_budget_and_semantic_cache_make_reruns_free(tmp_path, monkeypatch):
    s = Store(tmp_path); topic(s); original = item(s)
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-placeholder")
    calls = []
    def fake_request(body):
        calls.append(body)
        payload = json.loads(body["messages"][-1]["content"])
        result = {"results": [{"input_id": p["id"], "parts": [part()]} for p in payload["inputs"]]}
        return {"status": 200, "raw": {"choices": [{"finish_reason": "stop", "message": {"content": dumps(result)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 100}}, "retry_after": None}
    monkeypatch.setattr("facts.organize_llm.request", fake_request)
    a = organize(tmp_path, maximum=1, model="test")
    count = len(calls)
    assert count == 1 and a["coverage"]["coverage"]["item:semantic"] == 1
    b = organize(tmp_path, maximum=1, model="test")
    assert len(calls) == count and b["estimated_api_cost_usd"] == 0
    o = OrganizedStore(tmp_path)
    jid = enqueue(o, "topic", "grouping", {"entries": []}, "test"); o.db.commit()
    report = run_jobs(o, ["topic"], "grouping", Budget(0), lambda *a: None)
    assert report["state"] == "budget_limited" and len(calls) == count
    assert o.db.execute("SELECT state FROM organized_jobs WHERE id=?", (jid,)).fetchone()[0] == "queued"


def test_export_keeps_originals_and_portable_series(tmp_path):
    from facts.organize_export import export_organized
    s = Store(tmp_path); topic(s); original = item(s)
    organize(tmp_path, use_llm=False)
    result = export_organized(tmp_path)
    folder = tmp_path / result[0]["path"]
    observed = json.loads((folder / "observations.jsonl").read_text().splitlines()[0])
    assert observed["input_ids"] == [original["id"]]
    assert (folder / "manifest.json").exists()
    assert (folder / "references.jsonl").exists()
    assert s.db.execute("SELECT payload FROM items WHERE id=?", (original["id"],)).fetchone()[0] == dumps(original)


def test_active_versions_replace_search_and_retire_old_split_parts(tmp_path):
    o = OrganizedStore(tmp_path); topic(o.original)
    a = Observation(id="a", topic="topic", title="Old title", input_ids=["input"])
    b = Observation(id="b", topic="topic", title="Second atom", input_ids=["input"])
    o.replace_input("item:input", [a, b], "v1")
    updated = a.model_copy(update={"title": "New searchable title"})
    o.replace_input("item:input", [updated], "v2"); o.db.commit()
    assert len(o.query("topic", contains="searchable")) == 1
    assert o.query("topic", contains="Old") == []
    assert o.query("topic", contains="Second") == []
    assert o.db.execute("SELECT count(*) FROM organized_records").fetchone()[0] == 3
    assert o.db.execute("SELECT count(*) FROM organized_search").fetchone()[0] == 1


def test_topic_locks_cover_all_against_individual_topic(tmp_path):
    a, b = OrganizedStore(tmp_path), OrganizedStore(tmp_path)
    with a.locks(["one", "two"]):
        with pytest.raises(RuntimeError, match="owns topic"):
            with b.locks(["two"]):
                pass
    with b.locks(["two"]):
        pass


def test_split_jobs_resume_from_parent_without_reissuing_parent(tmp_path, monkeypatch):
    from facts.organize_llm import body_for, reservation
    o = OrganizedStore(tmp_path); topic(o.original)
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-placeholder")
    jid = enqueue(o, "topic", "grouping", {"entries": [{"id": "a"}, {"id": "b"}]}, "test"); o.db.commit()
    job = dict(o.db.execute("SELECT * FROM organized_jobs WHERE id=?", (jid,)).fetchone())
    reserved = reservation(body_for(job, {"entries": [{"id": "a"}, {"id": "b"}]}), json.loads(job["settings"]))
    calls, applied = [], []
    def fake_request(body):
        payload = json.loads(body["messages"][-1]["content"]); calls.append(payload)
        if len(payload["entries"]) == 2:
            return {"status": 200, "raw": {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}, "retry_after": None}
        result = {"results": [{"input_id": payload["entries"][0]["id"], "candidate_id": None, "relation": "no_match"}]}
        return {"status": 200, "raw": {"choices": [{"finish_reason": "stop", "message": {"content": dumps(result)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 10}}, "retry_after": None}
    monkeypatch.setattr("facts.organize_llm.request", fake_request)
    apply = lambda j, p, r, a: applied.extend(x.input_id for x in r.results)
    run_jobs(o, ["topic"], "grouping", Budget(reserved+0.0000001), apply, workers=1, allowed_ids={jid})
    assert len(calls) == 1
    assert o.db.execute("SELECT state FROM organized_jobs WHERE id=?", (jid,)).fetchone()[0] == "split"
    run_jobs(o, ["topic"], "grouping", Budget(1), apply, workers=1, allowed_ids={jid})
    assert sorted(applied) == ["a", "b"] and len(calls) == 3


def test_auth_failure_stops_later_stages(tmp_path, monkeypatch):
    o = OrganizedStore(tmp_path); topic(o.original)
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-placeholder")
    enqueue(o, "topic", "grouping", {"entries": []}, "test"); o.db.commit()
    calls = []
    def fake(body):
        calls.append(body); return {"status": 401, "raw": {"error": "unauthorized"}, "retry_after": None}
    monkeypatch.setattr("facts.organize_llm.request", fake)
    budget = Budget(1)
    first = run_jobs(o, ["topic"], "grouping", budget, lambda *a: None)
    second = run_jobs(o, ["topic"], "semantic", budget, lambda *a: None)
    assert first["state"] == second["state"] == "authentication_failed"
    assert len(calls) == 1
