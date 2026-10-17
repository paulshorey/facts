"""Bulk financial data capture. Preserve rows and units; do not reconcile totals."""
from __future__ import annotations

import io
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq

from .core import Store, dumps, ident, now, retrieve

TOPIC = "us-federal-spending"


def dataset(store, sid, name, rows, metadata, raw_artifact):
    did = ident("dataset", sid, name, raw_artifact)
    destination = store.storage / "tables" / f"{did}.parquet"
    destination.parent.mkdir(exist_ok=True)
    # Text columns preserve heterogeneous spreadsheet cells and opaque codes.
    if rows:
        keys = list(dict.fromkeys(k for row in rows for k in row))
        table = pa.table({k: [None if r.get(k) is None else str(r[k]) for r in rows] for k in keys})
        pq.write_table(table, destination, compression="zstd")
    manifest = {"id": did, "name": name, "row_count": len(rows), "metadata": metadata,
                "raw_artifact": raw_artifact, "parquet": str(destination.relative_to(store.root)) if rows else None,
                "created_at": now(), "source_id": sid}
    store.db.execute("INSERT INTO datasets VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,artifact=excluded.artifact", (did, TOPIC, sid, raw_artifact, dumps(manifest)))
    store.db.commit()
    return did


def treasury(store):
    base = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/mts/"
    for table in ("mts_table_5", "mts_table_9"):
        for period in ("2024-09-30", "2025-09-30", "2026-09-30"):
            params = {"filter": f"record_date:eq:{period}", "page[size]": 10000, "sort": "record_date"}
            url = str(httpx.URL(base+table, params=params))
            sid = store.source(TOPIC, url, f"Treasury {table}: {period}", family="treasury-dataset")
            if store.db.execute("SELECT 1 FROM datasets WHERE source=?", (sid,)).fetchone():
                continue
            try:
                with httpx.Client(timeout=60) as client:
                    response = client.get(url)
                    response.raise_for_status()
                    payload = response.json()
                raw = store.put(payload)
                rows = payload["data"]
                if int(payload.get("meta", {}).get("total-pages", 1)) > 1:
                    raise ValueError("Treasury snapshot exceeds requested page size; pagination required")
                cid = store.capture(sid, "dataset_api", payload, "", {"period": period, "table": table})
                did = dataset(store, sid, table+"-"+period, rows, {"period": period, "api_metadata": payload.get("meta"), "units": "USD", "unit_mapping": "treasury-api-dollars-v1", "hierarchy_preserved": True}, raw)
                for row in rows:
                    expression = row.get("current_fytd_net_outly_amt")
                    if expression in (None, "null", ""):
                        continue
                    try:
                        amount = str(Decimal(expression.replace(",", "")))
                    except InvalidOperation:
                        continue
                    label = row.get("classification_desc") or row.get("classification_id")
                    store.item(TOPIC, sid, cid, {"kind": "statistic", "title": f"FY{period[:4]} {label}",
                        "detail": f"Treasury fiscal-year-to-date net outlays: {expression} USD. This row may be a subtotal or offset; source hierarchy is retained.",
                        "dates": [{"role": "period_end", "value": period}], "tags": ["treasury", table, "outlays"],
                        "measurements": [{"metric": "fiscal_year_to_date_net_outlays", "value": amount, "unit": "USD", "original_expression": expression, "original_unit": "USD", "period": period}],
                        "data": {"dataset_id": did, "source_row": row}},
                        {"type": "dataset_row", "classification_id": row.get("classification_id"), "period": period})
                store.db.commit()
                print(f"Treasury {table} {period}: {len(rows)} rows", flush=True)
            except Exception as error:
                store.attempt("dataset", url, "failed", metadata={"error_class": type(error).__name__, "table": table, "period": period})
                store.db.commit()
                print(f"Treasury snapshot failed {table} {period}: {type(error).__name__}", flush=True)


def workbook(store, url, title=None):
    sid = store.source(TOPIC, url, title or Path(url).name, family="official-workbook")
    existing = store.db.execute("SELECT * FROM captures WHERE source=? AND level='workbook' AND state='succeeded' LIMIT 1", (sid,)).fetchone()
    if existing:
        body = (store.root / existing['artifact']).read_bytes()
        metadata, status = json.loads(existing['metadata']), 200
    else:
        body, _, metadata, status = retrieve(url)
    if status != 200:
        store.capture(sid, "workbook", body, "", metadata, status, "failed")
        store.db.commit()
        print(f"Workbook unavailable: {status} {url}", flush=True)
        return
    raw_artifact = store.put(body, binary=True)
    cid = store.capture(sid, "workbook", body, "", metadata)
    book = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
    for sheet in book.worksheets:
        # Some published sheets declare A1:A1 despite containing many rows.
        sheet.reset_dimensions()
        iterator = iter(sheet.values)
        first = next(iterator, ())
        # Account-level public database has a real column header on its first row.
        tabular = "Agency Name" in first and "Account Name" in first
        values = list(iterator) if tabular else [first, *iterator]
        width = max((len(row) for row in values), default=len(first))
        headers = [str(v) if v is not None else f"column_{i+1}" for i, v in enumerate(first)] if tabular else [f"column_{i+1}" for i in range(width)]
        if len(set(headers)) != len(headers):
            headers = [f"{h}_{i+1}" for i, h in enumerate(headers)]
        rows = [{"source_row": i+(2 if tabular else 1), **{headers[j]: value for j, value in enumerate(row) if j < len(headers)}} for i, row in enumerate(values) if any(v is not None for v in row)]
        did = dataset(store, sid, Path(url).name+":"+sheet.title, rows,
                      {"sheet": sheet.title, "headers": headers, "layout": "header_table" if tabular else "raw_cells", "source_units": "preserved in original workbook; no inferred conversions", "data_only": True, "parser_version": "workbook-v2"}, raw_artifact)
        if tabular:
            year_columns = [str(year) for year in range(2024, 2032) if str(year) in headers]
            for row in rows:
                # One item per account/category row; all selected years stay in data.
                yearly = {year: row.get(year) for year in year_columns}
                if not any(v not in (0, None, "0") for v in yearly.values()):
                    continue
                store.item(TOPIC, sid, cid, {"kind": "statistic", "title": f"{row.get('Agency Name')}: {row.get('Account Name')}",
                    "detail": f"Budget account row from {Path(url).name}; fiscal-year values and classification are preserved in the original source units.",
                    "entities": [row.get("Agency Name")], "tags": ["omb", "budget-account"] + (["outlays"] if 'outlays_' in url else ["budget-authority"] if 'budauth_' in url else []),
                    "data": {"dataset_id": did, "sheet": sheet.title, "year_values_source_units": yearly,
                             "classification": {k: row.get(k) for k in headers if not k.isdigit()}}},
                    {"type": "spreadsheet_row", "sheet": sheet.title, "row": row["source_row"]})
        store.db.commit()
        print(f"Workbook {Path(url).name} / {sheet.title}: {len(rows)} rows", flush=True)
    book.close()


def run(store, all_omb=False):
    treasury(store)
    manifest = store.root / "storage/bootstrap/isolated-web-preflight.omb-downloads.json"
    if manifest.exists():
        urls = [r["url"] for r in json.loads(manifest.read_text()) if r["url"].endswith(".xlsx")]
    else:
        urls = [r[0] for r in store.db.execute("SELECT s.url FROM sources s JOIN topic_sources t ON t.source=s.id WHERE t.topic=? AND s.url LIKE '%.xlsx' ORDER BY s.url", (TOPIC,))]
        store.attempt("dataset_manifest", "omb", "missing", metadata={"path": str(manifest.relative_to(store.root)), "catalog_workbooks": len(urls)})
        store.db.commit()
        print("OMB bootstrap manifest missing; using cataloged workbook URLs. Supply the manifest to expand OMB coverage.", flush=True)
    priority = ["outlays_fy2027.xlsx", "objclass_detail_fy2027.xlsx", "hist03z1_fy2027.xlsx", "hist03z2_fy2027.xlsx", "hist04z1_fy2027.xlsx", "budauth_fy2027.xlsx"]
    urls.sort(key=lambda u: priority.index(Path(u).name) if Path(u).name in priority else 99)
    if not all_omb:
        urls = [u for u in urls if Path(u).name in priority]
    urls += ["https://www.cbo.gov/system/files/2026-06/51142-2026-06-Spending-Projections.xlsx", "https://www.cbo.gov/system/files/2026-02/51142-2026-02-Spending-Projections.xlsx"]
    for url in urls:
        try:
            workbook(store, url)
        except Exception as error:
            store.attempt("dataset", url, "failed", metadata={"error": f"{type(error).__name__}: {str(error)[:300]}"})
            store.db.commit()
            print(f"Dataset failed {url}: {type(error).__name__}", flush=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-omb", action="store_true")
    args = parser.parse_args()
    store = Store()
    store.load_topics()
    run(store, args.all_omb)
