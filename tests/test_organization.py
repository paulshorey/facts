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
from facts.organize_tables import infer_mapping, mapping_input, normalize_table, period, raw_rows, validate_mapping


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
    from facts.audit import audit
    assert audit(tmp_path,current=True)['table_values_checked']==1
    result['value_count']=2
    o.db.execute('UPDATE organized_tables SET payload=? WHERE id=?',(dumps(result),did));o.db.commit()
    with pytest.raises(ValueError,match='value/series linkage'):
        audit(tmp_path,current=True)


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


def test_long_series_keep_label_only_accounts_separate_across_periods(tmp_path):
    s = Store(tmp_path); topic(s, "us-federal-spending")
    sid = s.source("us-federal-spending", "https://example.com/budget.xlsx")
    rows = [{"source_row": str(i+1), "account": account, "agency": "Shared agency", "year": year, "amount": amount}
            for i, (account, year, amount) in enumerate([("Account A", "FY 2025", "10"), ("Account A", "FY 2026", "20"), ("Account B", "FY 2025", "30"), ("Account B", "FY 2026", "40")])]
    did = dataset(s, sid, "budget.xlsx:Sheet", rows, {}, s.put(b"raw", binary=True))
    o = OrganizedStore(tmp_path)
    manifest = json.loads(o.db.execute("SELECT payload FROM datasets WHERE id=?", (did,)).fetchone()[0])
    mapping = TableMapping(orientation="long", label_columns=["account"], dimension_columns=["agency"], period_column="year",
                           value_columns=[ValueColumn(column="amount", metric="outlays", unit="USD", scale="1")])
    result = normalize_table(o, manifest, mapping, "digest")
    series = {r["dimensions"]["account"]: r for r in o.query("us-federal-spending", kind="series", limit=0)}
    assert result["series_count"] == 2 and set(series) == {"Account A", "Account B"}
    values = pq.read_table(tmp_path / result["values_artifact"]).to_pylist()
    for account, expected in [("Account A", [Decimal("10"), Decimal("20")]), ("Account B", [Decimal("30"), Decimal("40")])]:
        assert series[account]["title"] == account
        assert series[account]["dimensions"]["agency"] == "Shared agency"
        assert [v["value"] for v in values if v["series_id"] == series[account]["id"]] == expected
    assert values[0]["period"] == "FY 2025" and values[0]["period_start"] == "2024-10-01"
    assert values[0]["period_end"] == "2025-09-30" and values[0]["calendar"] == "fiscal"


def test_long_labels_with_duplicate_headings_do_not_overwrite_dimensions(tmp_path):
    s = Store(tmp_path); topic(s, "us-federal-spending")
    sid = s.source("us-federal-spending", "https://example.com/budget.xlsx")
    rows = [{"source_row": "1", "column_1": "Name", "column_2": "Name", "column_3": "Year", "column_4": "Amount"},
            {"source_row": "2", "column_1": "Agency A", "column_2": "Shared account", "column_3": "2025", "column_4": "10"},
            {"source_row": "3", "column_1": "Agency B", "column_2": "Shared account", "column_3": "2025", "column_4": "20"}]
    did = dataset(s, sid, "budget.xlsx:Sheet", rows, {}, s.put(b"raw", binary=True))
    o = OrganizedStore(tmp_path)
    manifest = json.loads(o.db.execute("SELECT payload FROM datasets WHERE id=?", (did,)).fetchone()[0])
    mapping = TableMapping(orientation="long", header_rows=["1"], label_columns=["column_2"], dimension_columns=["column_1"],
                           period_column="column_3", value_columns=[ValueColumn(column="column_4", metric="outlays", unit="USD", scale="1")])
    result = normalize_table(o, manifest, mapping, "digest")
    assert result["series_count"] == 2
    series = o.query("us-federal-spending", kind="series", limit=0)
    assert {tuple(sorted(r["dimensions"].values())) for r in series} == {("Agency A", "Shared account"), ("Agency B", "Shared account")}
    values = pq.read_table(tmp_path / result["values_artifact"]).to_pylist()
    assert len({v["series_id"] for v in values}) == 2


@pytest.mark.parametrize("label,start,end,calendar", [
    ("FY2025", "2024-10-01", "2025-09-30", "fiscal"),
    ("fy 2025", "2024-10-01", "2025-09-30", "fiscal"),
    ("FY 2025–2026 estimate", "2024-10-01", "2026-09-30", "fiscal"),
    ("FY 1962", None, None, "fiscal"),
    ("2025", "2025-01-01", "2025-12-31", "unknown"),
])
def test_table_period_honors_source_fiscal_prefix_without_filename_hint(label, start, end, calendar):
    parsed = period(label)
    assert (parsed["start"], parsed["end"], parsed["calendar"]) == (start, end, calendar)
    assert parsed["label"] == label
    assert period("2025", fiscal=True)["start"] == "2024-10-01"


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
    with pytest.raises(ValueError, match="resolved"):
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
    assert observed["references"][0]["acquisition_type"] == "direct_http"
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


def test_cost_limits_reject_nonfinite_or_negative_values():
    for value in [float("nan"), float("inf"), -1]:
        with pytest.raises(ValueError):
            Budget(value)


def test_explicit_entity_alias_reuses_canonical_id_and_preserves_junior(tmp_path):
    o = OrganizedStore(tmp_path)
    full = {"name": "Congressional Budget Office", "entity_type": "organization", "aliases": ["CBO"], "input_path": "/detail"}
    eid = o.entity(full, ["input"])
    assert o.entity({"name": "CBO", "entity_type": "organization"}) == eid
    senior = o.entity({"name": "Donald Trump", "entity_type": "person"})
    junior = o.entity({"name": "Donald Trump Jr.", "entity_type": "person"})
    assert senior != junior


def test_source_path_repairs_use_supplied_fields_only():
    from facts.organize import repair_source_path, grounded
    source = {"data": {"source_urls": ["https://example.com"], "entities": ["Donald Trump"]}, "detail": "Trump made a statement."}
    assert repair_source_path(source, "/source_urls/0") == "/data/source_urls/0"
    assert repair_source_path(source, "/detail", ["Donald Trump"]) == "/data/entities/0"
    with pytest.raises(ValueError):
        grounded(source, "/detail", "$TRUMP")


def test_partial_response_saves_valid_items_and_identifies_omissions(tmp_path):
    from facts.organize import apply_semantic_partial
    s = Store(tmp_path); topic(s)
    a = item(s)
    b = item(s, title="Another contract", detail="Acme announced another contract on May 2, 2025.")
    o = OrganizedStore(tmp_path)
    parsed = SemanticBatch.model_validate({"results": [{"input_id": a["id"], "parts": [part()]}]})
    report = apply_semantic_partial(o, {"id": "job", "settings": dumps({"model": "test"})}, {"inputs": [a, b], "taxonomy": {"other": []}}, parsed, "artifact")
    assert report["unprocessed"] == [b["id"]]
    assert len(o.query("topic")) == 1


def test_raw_amount_strings_and_zeroes_remain_available(tmp_path):
    s = Store(tmp_path); topic(s)
    original = item(s, data={"amounts": ["more than $1.4 billion", {"amount": 0, "unit": "USD"}], "custom_field": False})
    observed = adapt_item(original)
    assert Decimal(observed.measurements[0].value) == Decimal("1400000000")
    assert observed.measurements[1].value == "0"
    assert observed.source_fields["data"]["custom_field"] is False


def test_cached_partial_results_replay_after_adapter_upgrade(tmp_path, monkeypatch):
    s = Store(tmp_path); topic(s); a = item(s)
    b = item(s, title="Unrelated Acme lawsuit", detail="Acme announced another contract on May 2, 2025.")
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-placeholder")
    calls = []
    def fake_request(body):
        payload = json.loads(body["messages"][-1]["content"]); calls.append(payload)
        source = payload["inputs"][0]
        result = {"results": [{"input_id": source["id"], "parts": [part()]}]}
        return {"status": 200, "raw": {"choices": [{"finish_reason": "stop", "message": {"content": dumps(result)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 100}}, "retry_after": None}
    monkeypatch.setattr("facts.organize_llm.request", fake_request)
    first = organize(tmp_path, maximum=1, model="test")
    assert len(calls) == 2 and first["coverage"]["coverage"]["item:semantic"] == 2
    monkeypatch.setattr("facts.organize.ADAPTER_VERSION", "test-upgraded-adapter")
    monkeypatch.delenv("FIREWORKS_API_KEY")
    def forbidden(*a, **kw):
        raise AssertionError("Cached-only mode contacted a provider")
    monkeypatch.setattr("facts.organize_llm.request", forbidden)
    second = organize(tmp_path, maximum=1, model="test", cached_only=True)
    assert len(calls) == 2 and second["coverage"]["coverage"]["item:semantic"] == 2
    assert second["estimated_api_cost_usd"] == 0 and second["cost_limit_usd"] == 0


def test_cached_only_does_not_recover_running_or_submit_pending_jobs(tmp_path, monkeypatch):
    s = Store(tmp_path); topic(s); item(s)
    o = OrganizedStore(tmp_path)
    jid = enqueue(o, "topic", "semantic", {"inputs": []}, "test")
    o.db.execute("UPDATE organized_jobs SET state='running' WHERE id=?", (jid,)); o.db.commit()
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    report = organize(tmp_path, model="test", cached_only=True)
    assert report["semantic_jobs"]["state"] == "cached_only"
    assert o.db.execute("SELECT state FROM organized_jobs WHERE id=?", (jid,)).fetchone()[0] == "running"
    with pytest.raises(ValueError): organize(tmp_path,cached_only=True,retry_failed=True)
    with pytest.raises(ValueError): organize(tmp_path,cached_only=True,use_llm=False)


def test_terminal_semantic_failure_is_cached_until_explicit_retry(tmp_path, monkeypatch):
    s = Store(tmp_path); topic(s); item(s)
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-placeholder")
    calls = []
    def fake_request(body):
        payload = json.loads(body["messages"][-1]["content"]); calls.append(payload)
        proposal = part()
        if len(calls) <= 2:
            proposal["entities"][0]["name"] = "Invented Person"
        result = {"results": [{"input_id": payload["inputs"][0]["id"], "parts": [proposal]}]}
        return {"status": 200, "raw": {"choices": [{"finish_reason": "stop", "message": {"content": dumps(result)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 100}}, "retry_after": None}
    monkeypatch.setattr("facts.organize_llm.request", fake_request)
    first = organize(tmp_path, maximum=1, model="test")
    assert len(calls) == 2 and first["coverage"]["coverage"]["item:semantic_unresolved"] == 1
    organize(tmp_path, maximum=1, model="test")
    assert len(calls) == 2
    last = organize(tmp_path, maximum=1, model="test", retry_failed=True)
    assert len(calls) == 3 and last["coverage"]["coverage"]["item:semantic"] == 1


def test_financial_basis_normalization_preserves_original_description():
    value = measurement("$1 million", basis="net-outlays")
    assert value.basis == "net_outlays" and value.original_basis == "net-outlays"
    assert measurement("$1 million", basis="estimated").basis is None


def test_group_cache_identity_ignores_bookkeeping_versions(tmp_path):
    o = OrganizedStore(tmp_path)
    a = {"entries":[{"id":"one","version":"v1","title":"Example","candidates":[{"id":"two","version":"v1","title":"Related"}]}]}
    b = json.loads(dumps(a));b["entries"][0]["version"]="v2";b["entries"][0]["candidates"][0]["version"]="v2"
    first = enqueue(o,"topic","grouping",a,"test")
    second = enqueue(o,"topic","grouping",b,"test")
    assert first == second
    payload=json.loads(o.read(o.db.execute("SELECT input_artifact FROM organized_jobs WHERE id=?",(second,)).fetchone()[0]))
    assert payload == b


def test_incompatible_group_match_remains_a_related_link(tmp_path):
    from facts.organize_groups import apply_decisions
    o=OrganizedStore(tmp_path)
    a={"id":"a","version":"v1","title":"Acme drone contract","entities":[{"name":"Acme"}],"dates":[{"role":"event","start":"2025-01-01","end":"2025-01-01"}]}
    b={"id":"b","version":"v1","title":"Acme drone contract","entities":[{"name":"Acme"}],"dates":[{"role":"event","start":"2025-12-01","end":"2025-12-01"}]}
    a['candidates']=[b]
    decisions=GroupBatch.model_validate({'results':[{'input_id':'a','candidate_id':'b','relation':'same_occurrence'}]})
    apply_decisions(o,{'id':'job'},{'entries':[a]},decisions,'artifact')
    assert o.db.execute('SELECT relation FROM organized_relations').fetchone()[0]=='related_subject'


def test_queued_prices_refresh_but_completed_history_does_not(tmp_path, monkeypatch):
    o = OrganizedStore(tmp_path)
    payload = {"entries": []}
    monkeypatch.setenv("FACTS_ORGANIZE_INPUT_RATE", "0.15")
    jid = enqueue(o, "topic", "grouping", payload, "test")
    monkeypatch.setenv("FACTS_ORGANIZE_INPUT_RATE", "150")
    assert enqueue(o, "topic", "grouping", payload, "test") == jid
    row = o.db.execute("SELECT * FROM organized_jobs WHERE id=?", (jid,)).fetchone()
    assert json.loads(row["settings"])["input_usd_per_million"] == 150
    o.db.execute("UPDATE organized_jobs SET state='succeeded' WHERE id=?", (jid,))
    monkeypatch.setenv("FACTS_ORGANIZE_INPUT_RATE", "300")
    enqueue(o, "topic", "grouping", payload, "test")
    assert json.loads(o.db.execute("SELECT settings FROM organized_jobs WHERE id=?", (jid,)).fetchone()[0])["input_usd_per_million"] == 150


def test_acronym_first_preserves_entity_and_existing_links(tmp_path):
    o = OrganizedStore(tmp_path)
    eid = o.entity({"name": "CBO", "entity_type": "organization"})
    assert o.entity({"name": "CBO", "canonical_name": "Congressional Budget Office", "entity_type": "organization", "aliases": ["CBO"], "input_path": "/detail"}, ["later"]) == eid
    assert o.entity({"name": "Congressional Budget Office", "entity_type": "organization"}) == eid
    entity = json.loads(o.db.execute("SELECT payload FROM organized_entities WHERE id=?", (eid,)).fetchone()[0])
    assert entity["name"] == "Congressional Budget Office"
    assert entity["alias_evidence"][0]["input_ids"] == ["later"]


@pytest.mark.parametrize("provisional_first", [False, True])
def test_conflicting_acronym_expansions_keep_distinct_canonical_entities(tmp_path, provisional_first):
    o = OrganizedStore(tmp_path); topic(o.original)
    if provisional_first:
        provisional = o.entity({"name": "ABC", "entity_type": "organization"})
    first = {"name": "ABC", "canonical_name": "Alpha Beta Corp", "entity_type": "organization", "aliases": ["ABC"]}
    second = {**first, "canonical_name": "American Broadcasting Company"}
    saved = o.save_record(Observation(id="first", topic="topic", title="First report", input_ids=["first"], entities=[first]), "v1", "item:first")
    first_id = saved["entities"][0]["entity_id"]
    second_id = o.entity(second, ["second"])
    if provisional_first:
        assert first_id == provisional
    assert first_id != second_id
    assert json.loads(o.db.execute("SELECT payload FROM organized_entities WHERE id=?", (first_id,)).fetchone()[0])["name"] == first["canonical_name"]
    assert o.entity(first) == first_id and o.entity(second) == second_id
    assert {r[0] for r in o.db.execute("SELECT entity_id FROM organized_aliases WHERE alias='abc'")} == {first_id, second_id}
    ambiguous = o.entity({"name": "ABC", "entity_type": "organization"})
    assert ambiguous not in {first_id, second_id}
    assert o.entity({"name": "ABC", "entity_type": "organization"}) == ambiguous
    assert o.query("topic", entity=first_id)[0]["id"] == "first"
    assert o.query("topic", entity=second_id) == []


def test_entity_alias_change_creates_new_immutable_export(tmp_path):
    from facts.organize_export import export_organized
    o = OrganizedStore(tmp_path); topic(o.original)
    record = Observation(id="a", topic="topic", title="A report", input_ids=["a"], entities=[{"name": "CBO", "entity_type": "organization"}])
    o.save_record(record, "v1", "item:a"); o.db.commit()
    first = export_organized(tmp_path)[0]
    o.entity({"name": "CBO", "entity_type": "organization", "aliases": ["Budget office"], "input_path": "/detail"}, ["other-topic-input"]); o.db.commit()
    second = export_organized(tmp_path)[0]
    assert first["snapshot"] != second["snapshot"]
    old = (tmp_path / first["path"] / "entities.jsonl").read_text()
    new = (tmp_path / second["path"] / "entities.jsonl").read_text()
    assert "Budget office" not in old and "Budget office" in new


def test_exact_groups_do_not_combine_different_entities_or_reuse_bad_edges(tmp_path):
    from facts.organize_groups import build_groups
    o = OrganizedStore(tmp_path); topic(o.original)
    for iid, entity in [("a", "Acme"), ("b", "Other Corp"), ("c", "Acme")]:
        record = Observation(id=iid, topic="topic", title="Contract awarded", input_ids=[iid], detail="A contract was awarded.", entities=[{"name": entity, "entity_type": "organization"}])
        o.save_record(record, "v1", "item:"+iid)
    o.db.execute("INSERT INTO organized_relations VALUES (?,?,?,?,?,?)", ("b", "v1", "a", "v1", "same_occurrence", dumps({"method": "identical_description"})))
    result = build_groups(o, ["topic"], [], Budget(0), "test", 1, False)
    assert result["occurrence_groups"] == 2
    groups = [json.loads(r[0])["member_ids"] for r in o.db.execute("SELECT payload FROM organized_groups WHERE group_kind='occurrence'")]
    assert sorted(groups) == [["a", "c"], ["b"]]


def test_interrupted_job_refreshes_prices_before_recovered_submission(tmp_path, monkeypatch):
    o = OrganizedStore(tmp_path)
    monkeypatch.setenv("FIREWORKS_API_KEY", "offline-placeholder")
    monkeypatch.setenv("FACTS_ORGANIZE_INPUT_RATE", "0.15")
    jid = enqueue(o, "topic", "grouping", {"entries": []}, "test")
    o.db.execute("UPDATE organized_jobs SET state='running',attempts=1,reserved=0.001 WHERE id=?", (jid,)); o.db.commit()
    monkeypatch.setenv("FACTS_ORGANIZE_INPUT_RATE", "1000")
    monkeypatch.setattr("facts.organize_llm.request", lambda body: pytest.fail("Recovered job must not submit beyond current budget"))
    result = run_jobs(o, ["topic"], "grouping", Budget(0.01), lambda *args: None)
    row = o.db.execute("SELECT * FROM organized_jobs WHERE id=?", (jid,)).fetchone()
    assert result["state"] == "budget_limited" and row["state"] == "failed_retryable"
    assert json.loads(row["settings"])["input_usd_per_million"] == 1000
    assert row["cost"] == 0.001 and row["reserved"] == 0

@pytest.mark.parametrize("expression, comparator", [
    ("no more than $5 million", "at_most"),
    ("not more than $5 million", "at_most"),
    ("not less than $5 million", "at_least"),
    ("no less than $5 million", "at_least"),
    ("not over $5 million", "at_most"),
    ("not under $5 million", "at_least"),
    ("<= $5 million", "at_most"), (">= $5 million", "at_least"),
    ("at most $5 million", "at_most"), ("at least $5 million", "at_least"),
    ("less than $5 million", "less_than"), ("more than $5 million", "greater_than"),
])
def test_amount_bounds_keep_negation_and_inclusion(expression, comparator):
    result = measurement(expression)
    assert result.comparator == comparator
    assert Decimal(result.value) == 5000000
    assert result.original_expression == expression


def test_supplied_iso_date_range_survives_adapter_and_filtering(tmp_path):
    from facts.organize import temporal_context
    store = Store(tmp_path); topic(store)
    original = item(store, dates=[{"role": "event", "start": "2025-01-01", "end": "2025-03-31"}])
    saved = dumps(original)
    observed = adapt_item(original)
    d = observed.dates[0]
    assert (d.start, d.end, d.precision, d.calendar) == ("2025-01-01", "2025-03-31", "range", "calendar")
    assert d.input_path == "/dates/0" and d.original_expression == "2025-01-01–2025-03-31"
    assert temporal_context(observed, {"date_from": None, "date_to": "2024-12-31"}) == "after_period"
    assert dumps(original) == saved
    for expression in ["2025-02-31–2025-03-31", "2025-03-31–2025-01-01"]:
        assert parse_date(expression).start is None


@pytest.mark.parametrize("lower, upper, expression, expected", [
    (None, "2026-10-16", "2026-10-17", "after_period"),
    (None, "2026-10-16", "2025-01-01", "in_period"),
    ("2025-01-20", None, "2025-01-19", "before_period"),
    ("2025-01-20", None, "2026-10-17", "in_period"),
    (None, None, "2025-01-01", "unknown"),
])
def test_topic_windows_allow_one_unbounded_side(lower, upper, expression, expected):
    from facts.organize import temporal_context
    observed = Observation(id="one", topic="topic", title="Event", input_ids=["input"], dates=[parse_date(expression, "event")])
    assert temporal_context(observed, {"date_from": lower, "date_to": upper}) == expected


def test_observation_adapter_upgrade_recomputes_then_caches(tmp_path, monkeypatch):
    from facts.organize import adapt_all
    store = Store(tmp_path); topic(store)
    item(store, measurements=[{"value": "5", "unit": "USD", "original_expression": "no more than $5"}])
    organized = OrganizedStore(tmp_path)
    with monkeypatch.context() as old:
        old.setattr("facts.organize.ADAPTER_VERSION", "observations-v5")
        assert adapt_all(organized, ["topic"])["adapted"] == 1
    assert adapt_all(organized, ["topic"])["adapted"] == 1
    assert adapt_all(organized, ["topic"])["cached"] == 1
    assert organized.db.execute("SELECT count(*) FROM organized_records").fetchone()[0] == 2
    assert organized.db.execute("SELECT count(*) FROM organized_records WHERE active=1").fetchone()[0] == 1


def test_source_comparison_follows_item_into_views_and_export(tmp_path):
    from facts.organize_export import export_organized

    raw = Store(tmp_path)
    topic(raw)
    old = item(raw, title="Legacy employment estimate")
    organize(tmp_path, "topic", use_llm=False)
    first = export_organized(tmp_path, "topic")[0]
    ref = old["references"][0]
    detail = "The neighboring account reports the cited figures.\r\n- See the cited employment table."
    note_id = raw.item("topic", ref["source_id"], ref["capture_id"],
                       {"kind": "document", "title": "Account check", "detail": detail},
                       {"type": "source_comparison", "compared_item_id": old["id"]})
    raw.db.commit()
    organized = OrganizedStore(tmp_path, readonly=True)
    result, = organized.query("topic", contains="Legacy employment")
    check, = result["processing"]["source_checks"]
    assert check["item_id"] == note_id and check["compared_item_id"] == old["id"]
    assert check["detail"] == detail
    assert "input_has_source_comparison" in result["warnings"]
    grouped = organized.query("topic", view="grouped", contains="Legacy employment")
    assert any(check in m["processing"].get("source_checks", []) for g in grouped for m in g["members"])
    second = export_organized(tmp_path, "topic")[0]
    assert first["snapshot"] != second["snapshot"]
    lines = (tmp_path / second["path"] / "observations.jsonl").read_text().splitlines()
    assert any(check in json.loads(line)["processing"].get("source_checks", []) for line in lines)
    comparison_line = f"Source comparison `{note_id}` — The neighboring account reports the cited figures. - See the cited employment table."
    assert comparison_line in (tmp_path / second["path"] / "inventory.md").read_text().splitlines()
    assert json.loads(raw.db.execute("SELECT payload FROM items WHERE id=?", (old["id"],)).fetchone()[0]) == old
    from facts.core import export
    export(raw, "topic")
    items = [json.loads(line) for line in (tmp_path / "exports/topic/items.jsonl").read_text().splitlines()]
    target, = (i for i in items if i["id"] == old["id"])
    assert target["source_checks"] == [check]
    assert f"  {comparison_line}" in (tmp_path / "exports/topic/inventory.md").read_text().splitlines()
    organized.db.close()
    raw.db.close()


def test_primary_statistic_observation_period_is_queryable_after_replay(tmp_path, monkeypatch):
    from facts.organize import adapt_all
    store = Store(tmp_path)
    topic(store)
    sid = store.source("topic", "https://example.com/release")
    cid = store.capture(sid, "direct_http", b"release", "Survey results")
    store.item("topic", sid, cid, {"kind": "statistic", "title": "Survey", "published": "2025-09-25",
                                  "data": {"period": "2023–2024"}}, {"type": "primary_statistic"})
    store.db.commit()
    organized = OrganizedStore(tmp_path)
    with monkeypatch.context() as old:
        old.setattr("facts.organize.adapter_version", lambda _: "observations-v6")
        adapt_all(organized, ["topic"])
    assert adapt_all(organized, ["topic"]) == {"adapted": 1}
    result, = organized.query("topic", date_from="2024-01-01", date_to="2024-12-31")
    assert result["temporal_context"] == "before_period"
    assert [(d["role"], d["start"], d["end"]) for d in result["dates"]] == [
        ("publication", "2025-09-25", "2025-09-25"), ("observation_period", "2023-01-01", "2024-12-31")]
    assert not organized.query("topic", date_from="2025-01-01")
    assert adapt_all(organized, ["topic"]) == {"cached": 1}


def test_recovered_premium_preserves_rate_and_month_range(tmp_path):
    store = Store(tmp_path)
    topic(store)
    period = "July 1966–March 1968"
    original = item(store, data={"source_row": {"period": period, "period_basis": "Calendar year"}},
                    measurements=[{"value": "3", "unit": "USD per month", "period": period}])
    origin = {"type": "failed_chunk_recovery", "table": "III.C2", "period_basis": "Calendar year", "period": period}
    original["references"][0]["locator"] = origin
    original["collection_metadata"]["extraction_context"] = origin
    saved = dumps(original)
    result = adapt_item(original)
    assert dumps(original) == saved
    assert result.processing["origin"]["period_basis"] == result.references[0]["locator"]["period_basis"] == "explicit month range"
    assert result.source_fields["data"]["source_row"]["period_basis"] == "Calendar year"
    assert (result.dates[0].start, result.dates[0].end) == ("1966-07-01", "1968-03-31")
    assert (result.measurements[0].value, result.measurements[0].unit) == ("3", "USD per month")
