"""Map saved tables once, then transform all their values with deterministic code."""
from __future__ import annotations

import json
import re
import tempfile
import zipfile
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree

import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import VERSION, TableMapping, ValueColumn
from .core import dumps, ident
from .organize_fields import categories, decimal_text, parse_date, plain, unit_scale

MAPPER_VERSION = "tables-v2"
YEAR = re.compile(r"^(?:FY\s*)?((?:17|18|19|20|21)\d{2})(?:\s*[-–]\s*((?:17|18|19|20|21)\d{2}))?(?:\s*(?:estimate[ds]?|actual[ s]*|projection|enacted|proposed))?(?:\s*\d/)?$", re.I)
VALUE_SCHEMA = pa.schema([
    ("series_id", pa.string()), ("dataset_id", pa.string()), ("period", pa.string()),
    ("period_start", pa.string()), ("period_end", pa.string()), ("calendar", pa.string()),
    ("period_role", pa.string()), ("source_row", pa.string()), ("source_column", pa.string()),
    ("original_expression", pa.string()), ("source_value", pa.decimal128(38, 10)),
    ("value", pa.decimal128(38, 10)), ("unit", pa.string()), ("scale", pa.string()),
    ("basis", pa.string()), ("value_kind", pa.string()), ("warnings", pa.string()),
])


def raw_rows(store, manifest):
    if not manifest.get("parquet"):
        return []
    return pq.read_table(store.root / manifest["parquet"]).to_pylist()


def workbook_structure(store, manifest):
    """Read XML metadata, not a second lossy rendering of cached spreadsheet values."""
    path = store.root / manifest["raw_artifact"]
    if not zipfile.is_zipfile(path):
        return {}
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    with zipfile.ZipFile(path) as book:
        workbook = ElementTree.fromstring(book.read("xl/workbook.xml"))
        sheet = next((s for s in workbook.findall("m:sheets/m:sheet", ns) if s.attrib.get("name") == manifest["metadata"].get("sheet")), None)
        if sheet is None:
            return {}
        relationships = ElementTree.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        rid = sheet.attrib.get("{"+ns["r"]+"}id")
        target = next((r.attrib["Target"] for r in relationships if r.attrib.get("Id") == rid), None)
        if not target:
            return {}
        target = target.lstrip("/") if target.startswith("/") else "xl/"+target
        xml = ElementTree.fromstring(book.read(target))
        merges = [v.attrib["ref"] for v in xml.findall("m:mergeCells/m:mergeCell", ns)]
        formulas = [{"cell": c.attrib["r"], "formula": c.find("m:f", ns).text,
                     "cached_value": c.findtext("m:v", default=None, namespaces=ns)}
                    for c in xml.findall("m:sheetData/m:row/m:c", ns) if c.find("m:f", ns) is not None]
        return {"merged_ranges": merges, "formula_count": len(formulas), "formula_samples": formulas[:30],
                "formula_source": "original workbook XML", "cached_values_preserved": True}


def period(text, fiscal=False):
    text = plain(text)
    match = YEAR.fullmatch(text)
    if not match:
        return None
    expression = match[1] + ("-"+match[2] if match[2] else "")
    value = parse_date(("FY" if fiscal else "")+expression, "reporting_period")
    return {"label": text, "start": value.start, "end": value.end, "calendar": value.calendar,
            "role": "estimate" if re.search(r"estimat|project|propos", text, re.I) else "actual" if "actual" in text.casefold() else "unknown"}


def basis_for(text):
    t = text.casefold()
    for key, terms in [("budget_authority", ["budget authority", "budauth_"]),
                       ("tax_expenditures", ["tax expenditure", "tax-expenditure"]),
                       ("obligations", ["obligation", "objclass"]),
                       ("outlays", ["outlay"]), ("receipts", ["receipt"]),
                       ("balance", ["balance", "unexpended"])]:
        if any(term in t for term in terms):
            return key
    return None


def table_units(rows):
    # A global unit requires an explicit statement near the table heading.
    statements = [plain(v) for row in rows[:8] for k, v in row.items() if k != "source_row" and v is not None
                  if re.search(r"(?:in\s+)?(?:millions?|billions?|thousands?)\s+of\s+(?:u\.?s\.?\s*)?dollars|percent(?:age)?\s+of\s+(?:gdp|g.n.p)|\[\$\s*(?:millions?|billions?)\]", plain(v), re.I)]
    pairs = {(unit_scale(s)[0], unit_scale(s)[1]) for s in statements}
    if len(pairs) == 1:
        unit, scale = pairs.pop()
        return unit, scale, statements[0]
    return None, None, None


def infer_mapping(manifest, rows):
    name = manifest["name"]
    if not rows:
        return TableMapping(orientation="metadata", notes=["empty sheet"])
    if "classification_id" in rows[0]:
        fields = [k for k in rows[0] if k.endswith("_amt")]
        return TableMapping(orientation="treasury", label_columns=["classification_desc"],
            dimension_columns=[k for k in rows[0] if k not in fields],
            value_columns=[ValueColumn(column=k, metric=k, unit="USD", scale="1", unit_evidence="Treasury API native dollar values; PDF display uses millions", basis="receipts" if "rcpt_outly" in k else "net_outlays" if "net_outly" in k else "gross_outlays" if "gross_outly" in k else "applicable_receipts") for k in fields],
            notes=["treasury-api-dollars-v1: no million-fold multiplication"])
    unit, scale, evidence = table_units(rows)
    headers = manifest.get("metadata", {}).get("headers", [])
    if "Agency Name" in headers and "Account Name" in headers:
        years = [k for k in headers if YEAR.fullmatch(k) or k == "TQ"]
        return TableMapping(orientation="wide", label_columns=["Agency Name", "Bureau Name", "Account Name"],
            dimension_columns=[k for k in headers if k not in years],
            value_columns=[ValueColumn(column=k, metric=basis_for(name) or "reported_amount", period="FY"+k if k != "TQ" else "TQ", unit=unit, scale=scale, unit_evidence=evidence, basis=basis_for(name)) for k in years],
            notes=[] if unit else ["source units absent from captured table headers; values remain in source units"])
    # Public object-class table: retain its explicit BY/CY/PY roles without inventing FY labels.
    first = rows[0]
    header_by_value = {plain(v): k for k, v in first.items() if k != "source_row" and v is not None}
    if "BY Year" in header_by_value and "BY Amount" in header_by_value:
        amount_keys = [header_by_value[k] for k in ["PY Amount", "CY Amount", "BY Amount"]]
        return TableMapping(orientation="wide", header_rows=[first["source_row"]],
            label_columns=[header_by_value[k] for k in ["Agency Title", "Bureau Title", "Account Title", "OB Class"] if k in header_by_value],
            dimension_columns=[k for k in first if k not in amount_keys and k != "source_row"],
            value_columns=[ValueColumn(column=header_by_value[k], metric="obligations", period=k.split()[0], period_role=k.split()[0], unit=unit, scale=scale, unit_evidence=evidence, basis="obligations") for k in ["PY Amount", "CY Amount", "BY Amount"]],
            notes=["BY/CY/PY labels retained; fiscal-year interpretation requires an explicit source mapping"])
    # Wide tables have a row containing several year/period labels.
    for row in rows[:18]:
        year_cols = [k for k, v in row.items() if k != "source_row" and (YEAR.fullmatch(plain(v)) or plain(v) == "TQ")]
        if len(year_cols) >= 2:
            keys = [k for k in row if k != "source_row"]
            first_index = min(keys.index(k) for k in year_cols)
            labels = keys[:first_index]
            if not labels:
                continue
            return TableMapping(orientation="wide", header_rows=[r["source_row"] for r in rows if int(r["source_row"]) <= int(row["source_row"])],
                label_columns=labels, dimension_columns=labels,
                value_columns=[ValueColumn(column=k, metric=basis_for(name+" "+plain(rows[0].get("column_1"))) or "reported_amount", period=plain(row[k]), unit=unit, scale=scale, unit_evidence=evidence, basis=basis_for(name+" "+plain(rows[0].get("column_1")))) for k in year_cols])
    content = " ".join(plain(v) for r in rows for k, v in r.items() if k != "source_row")
    if len(rows) < 30 and not any(decimal_text(v) is not None for r in rows for k, v in r.items() if k != "source_row"):
        return TableMapping(orientation="metadata", notes=[content[:3000]])
    return TableMapping(orientation="unresolved", notes=["requires a validated table mapping"])


def mapping_input(manifest, rows):
    # A bounded representative sample, including footnotes and section boundaries.
    sample = [*rows[:12], *rows[max(12, len(rows)//2):max(12, len(rows)//2)+3], *rows[-6:]]
    return {"dataset_id": manifest["id"], "name": manifest["name"], "rows": sample,
            "columns": list(rows[0]) if rows else [], "row_count": len(rows)}


def repair_mapping(mapping, context):
    """Representation repairs backed by the supplied sample, not model inference."""
    mapping = mapping.model_copy(deep=True)
    numeric_year_columns = Counter()
    for row in context["rows"]:
        for key, value in row.items():
            if key != "source_row" and YEAR.fullmatch(plain(value)):
                numeric_year_columns[key] += 1
    value_columns = {v.column for v in mapping.value_columns}
    candidates = [(count, key) for key, count in numeric_year_columns.items() if count >= 3 and key not in value_columns]
    if mapping.orientation == "wide" and not mapping.period_column and candidates and all(v.period is None for v in mapping.value_columns):
        mapping.orientation = "long"
        mapping.period_column = max(candidates)[1]
        mapping.dimension_columns = [k for k in mapping.dimension_columns if k != mapping.period_column]
        mapping.notes.append("Code recovered the explicit source year column; model's wide orientation repaired")
    elif mapping.orientation == "long" and not mapping.period_column and all(v.period is not None for v in mapping.value_columns):
        mapping.orientation = "wide"
        mapping.notes.append("Fixed-period value columns use wide orientation")
    text = " ".join(plain(v) for row in context["rows"] for v in row.values())
    normalized_text = " ".join(text.split())
    for value in mapping.value_columns:
        if value.unit is not None and value.unit_evidence:
            evidence = " ".join(value.unit_evidence.split())
            if evidence in normalized_text:
                unit, scale = unit_scale(value.unit_evidence)
                if value.unit == unit and value.scale is None:
                    value.scale = scale
                    mapping.notes.append(f"{value.column}: explicit unit scale supplied by deterministic parser")
                if value.unit != unit or value.scale != scale:
                    mapping.notes.append(f"{value.column}: proposed unit conversion rejected; original values retained in source units")
                    value.unit = value.scale = value.unit_evidence = None
            else:
                mapping.notes.append(f"{value.column}: proposed unit evidence was not a literal source passage; original values retained in source units")
                value.unit = value.scale = value.unit_evidence = None
        elif value.unit is not None:
            mapping.notes.append(f"{value.column}: missing unit evidence; original values retained in source units")
            value.unit = value.scale = value.unit_evidence = None
    return mapping


def validate_mapping(mapping, context):
    columns = set(context["columns"])
    used = set(mapping.label_columns + mapping.dimension_columns + [v.column for v in mapping.value_columns])
    if mapping.period_column:
        used.add(mapping.period_column)
    if not used.issubset(columns):
        raise ValueError("mapping names columns absent from source")
    sample_rows = {str(r.get("source_row")) for r in context["rows"]}
    if not set(mapping.header_rows).issubset(sample_rows):
        raise ValueError("header row absent from supplied sample")
    if mapping.orientation in {"treasury"}:
        raise ValueError("Treasury mappings are supplied by the deterministic adapter")
    text = " ".join(" ".join(plain(v).split()) for row in context["rows"] for v in row.values())
    for value in mapping.value_columns:
        if value.scale is not None and value.scale not in {"1", "1000", "1000000", "1000000000", "1000000000000"}:
            raise ValueError("unsupported unit scale")
        if value.unit is not None:
            if not value.unit_evidence or " ".join(value.unit_evidence.split()) not in text:
                raise ValueError("unit mapping lacks literal source evidence")
            unit, scale = unit_scale(value.unit_evidence)
            if value.unit != unit or value.scale != scale:
                raise ValueError("unit conversion disagrees with source evidence")
    if mapping.orientation in {"wide", "long"} and not mapping.value_columns:
        raise ValueError("numeric mapping has no value columns")
    if mapping.orientation == "long" and not mapping.period_column:
        raise ValueError("long table lacks a period column")
    return mapping


def as_decimal(text):
    if text is None:
        return None
    try:
        value = Decimal(text)
        if value.adjusted() >= 28 or value.as_tuple().exponent < -10:
            return None
        return value
    except InvalidOperation:
        return None


def normalize_table(store, manifest, mapping, digest, mapping_artifact=None):
    rows = raw_rows(store, manifest)
    did = manifest["id"]
    version = ident("table-version", MAPPER_VERSION, digest, mapping.model_dump())
    old = store.db.execute("SELECT version,payload FROM organized_tables WHERE id=?", (did,)).fetchone()
    if old and old["version"] == version:
        return json.loads(old["payload"])
    destination = store.root / "storage/organized/tables"
    destination.mkdir(parents=True, exist_ok=True)
    final = destination / (version+".parquet")
    coverage_path = destination / (version+"-rows.parquet")
    handle = tempfile.NamedTemporaryFile(dir=destination, suffix=".parquet", delete=False)
    handle.close()
    writer = pq.ParquetWriter(handle.name, VALUE_SCHEMA, compression="zstd")
    counts, row_dispositions, buffer = Counter(), [], []
    series = {}
    source = store.db.execute("SELECT url,title FROM sources WHERE id=?", (manifest["source_id"],)).fetchone()
    caps = store.db.execute("SELECT id FROM captures WHERE source=? AND artifact=? ORDER BY id LIMIT 1", (manifest["source_id"], manifest["raw_artifact"])).fetchone()
    input_key = "dataset:"+did
    source_rows = {r.get("classification_id"): r for r in rows if r.get("classification_id")}
    header_names = {}
    if mapping.header_rows:
        for r in rows:
            if str(r.get("source_row")) in mapping.header_rows:
                for k, v in r.items():
                    if k != "source_row" and v is not None:
                        header_names[k] = plain(v)
    section = []
    store.db.execute("UPDATE organized_records SET active=0 WHERE input_key=?", (input_key,))
    try:
        for index, row in enumerate(rows):
            row_id = str(row.get("source_row") or row.get("classification_id") or index)
            refs = {"source_id": manifest["source_id"], "url": source["url"] if source else None,
                    "capture_id": caps[0] if caps else None, "artifact": manifest["raw_artifact"],
                    "locator": {"dataset_id": did, "sheet": manifest.get("metadata", {}).get("sheet"), "row": row_id}}
            label_parts = [plain(row.get(k)) for k in mapping.label_columns if plain(row.get(k))]
            label = ": ".join(label_parts)
            hierarchy = list(section)
            if mapping.orientation == "treasury":
                ancestors, parent, seen = [], row.get("parent_id"), set()
                while parent in source_rows and parent not in seen:
                    seen.add(parent)
                    ancestors.insert(0, plain(source_rows[parent].get("classification_desc")))
                    parent = source_rows[parent].get("parent_id")
                hierarchy = ancestors
            values, linked = [], []
            state = "unresolved"
            if mapping.orientation == "metadata" or row_id in mapping.header_rows:
                state = "metadata"
            elif mapping.orientation not in {"unresolved", "metadata"}:
                for spec in mapping.value_columns:
                    original = row.get(spec.column)
                    n = decimal_text(original)
                    marker = plain(original) in {"*", "**", "(*)", "(**)", "...", "--", "n.a.", "N/A"}
                    if n is None and not marker:
                        continue
                    p = period(spec.period, fiscal="fy202" in manifest["name"].casefold() or mapping.orientation == "treasury") if spec.period else None
                    if mapping.orientation == "long":
                        p = period(row.get(mapping.period_column), fiscal="fy202" in manifest["name"].casefold())
                    if mapping.orientation == "treasury":
                        fy = int(row["record_fiscal_year"])
                        date_label = row["record_date"]
                        if spec.column.startswith("prior_fytd"):
                            prior = parse_date(date_label).start
                            if prior:
                                year, month, day = map(int, prior.split("-"))
                                import calendar
                                date_label = f"{year-1}-{month:02d}-{min(day,calendar.monthrange(year-1,month)[1]):02d}"
                            fy -= 1
                        start = date_label[:8]+"01" if spec.column.startswith("current_month") else f"{fy-1}-10-01"
                        p = {"label": ("month " if spec.column.startswith("current_month") else "FYTD ")+date_label, "start": start, "end": date_label, "calendar": "fiscal", "role": "reported"}
                    if p is None:
                        # BY/CY/PY and unknown period codes remain explicitly labeled.
                        p = {"label": spec.period or "unknown", "start": None, "end": None, "calendar": "unknown", "role": spec.period_role}
                    if spec.period_role != "unknown":
                        p["role"] = spec.period_role
                    dims = {header_names.get(k, k): row.get(k) for k in mapping.dimension_columns if k != mapping.period_column}
                    key = [did, spec.metric, dims, hierarchy]
                    if mapping.orientation != "long":
                        key.append(row_id)
                    sid = ident("series", *key)
                    if sid not in series:
                        title = label or spec.metric
                        if mapping.orientation == "long":
                            title = spec.metric
                        series[sid] = {"schema_version": VERSION, "id": sid, "topic": "us-federal-spending", "record_type": "series", "content_role": "dataset", "kind": "series", "subtype": mapping.orientation,
                            "title": title, "detail": f"{manifest['name']}: {spec.metric}", "input_ids": [did],
                            "dataset_id": did, "metric": spec.metric, "basis": spec.basis,
                            "unit": spec.unit, "scale": spec.scale, "unit_evidence": spec.unit_evidence,
                            "dimensions": dims, "hierarchy": hierarchy, "row_role": "total" if re.search(r"\btotal\b", title, re.I) else "detail",
                            "categories": categories("us-federal-spending", title+" "+" ".join(hierarchy)),
                            "references": [refs], "values_artifact": str(final.relative_to(store.root)),
                            "dates": [], "warnings": [] if spec.unit else ["unknown_source_units"],
                            "processing": {"method": "dataset_mapping", "mapper_version": MAPPER_VERSION, "mapping_artifact": mapping_artifact}}
                    normalized = format(Decimal(n)*Decimal(spec.scale), "f") if n is not None and spec.unit and spec.scale else None
                    warnings = [] if normalized is not None else ["unknown_unit_conversion"]
                    if marker:
                        warnings.append("unparsed_numeric_marker")
                    source_decimal, normalized_decimal = as_decimal(n), as_decimal(normalized)
                    if source_decimal is None or (normalized is not None and normalized_decimal is None):
                        warnings.append("decimal_outside_parquet_precision")
                    values.append({"series_id": sid, "dataset_id": did, "period": p["label"], "period_start": p["start"], "period_end": p["end"], "calendar": p["calendar"], "period_role": p["role"], "source_row": row_id, "source_column": spec.column,
                        "original_expression": plain(original), "source_value": source_decimal, "value": normalized_decimal,
                        "unit": spec.unit, "scale": spec.scale, "basis": spec.basis, "value_kind": spec.value_kind, "warnings": dumps(warnings)})
                    linked.append(sid)
                if values:
                    state = "normalized"
                elif label and all(row.get(v.column) in (None, "", "null") for v in mapping.value_columns):
                    # Preserve section labels as hierarchy and keep note text in row provenance.
                    state = "metadata"
                    if len(label) < 240 and not re.match(r"\d+/|note|source|memorandum", label, re.I):
                        if label.endswith(":"):
                            section = [label.rstrip(":")]
                        elif re.match(r"\d{3}\s", label):
                            section = [*section[:1], label]
                elif all(v in (None, "", "null") for k, v in row.items() if k != "source_row"):
                    state = "metadata"
            counts[state] += 1
            row_dispositions.append({"source_row": row_id, "state": state, "series_ids": dumps(sorted(set(linked))), "original_row_artifact": manifest["parquet"]})
            buffer.extend(values)
            if len(buffer) >= 5000:
                writer.write_table(pa.Table.from_pylist(buffer, schema=VALUE_SCHEMA)); buffer.clear()
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer, schema=VALUE_SCHEMA))
        writer.close()
        Path(handle.name).replace(final)
        # Coverage is independently inspectable at the original row granularity.
        coverage_tmp = coverage_path.with_suffix(".tmp")
        coverage_table = pa.Table.from_pylist(row_dispositions) if row_dispositions else pa.table({"source_row": pa.array([], type=pa.string()), "state": pa.array([], type=pa.string()), "series_ids": pa.array([], type=pa.string()), "original_row_artifact": pa.array([], type=pa.string())})
        pq.write_table(coverage_table, coverage_tmp, compression="zstd"); coverage_tmp.replace(coverage_path)
        for value in series.values():
            store.save_record(value, version, input_key)
        structure = workbook_structure(store, manifest)
        result = {"id": did, "topic": "us-federal-spending", "name": manifest["name"], "version": version,
                  "source_id": manifest["source_id"], "mapping": mapping.model_dump(), "mapping_artifact": mapping_artifact,
                  "raw_artifact": manifest["raw_artifact"], "original_parquet": manifest.get("parquet"),
                  "values_artifact": str(final.relative_to(store.root)), "row_coverage_artifact": str(coverage_path.relative_to(store.root)),
                  "row_counts": dict(counts), "series_count": len(series), "value_count": pq.ParquetFile(final).metadata.num_rows,
                  "workbook_structure": structure, "notes": mapping.notes}
        state = "unresolved" if counts["unresolved"] else "succeeded"
        store.db.execute("INSERT INTO organized_tables VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET input_digest=excluded.input_digest,version=excluded.version,state=excluded.state,payload=excluded.payload", (did, "us-federal-spending", digest, version, state, dumps(result)))
        store.coverage("dataset", did, "us-federal-spending", digest, state, sorted(series), {"row_counts": dict(counts), "row_coverage_artifact": result["row_coverage_artifact"]}, version=version)
        store.db.commit()
        return result
    except BaseException:
        writer.close()
        Path(handle.name).unlink(missing_ok=True)
        store.db.rollback()
        raise
