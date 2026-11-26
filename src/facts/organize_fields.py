"""Deterministic adapters. Unknown source semantics remain unknown."""
from __future__ import annotations

import calendar
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from .contracts import Attribution, DateValue, EntityMention, Measurement, Observation
from .core import ident

FACETS = {
    "trump-second-term": {
        "crypto": ["crypto", "bitcoin", "stablecoin", "memecoin", "token"],
        "world-liberty": ["world liberty", "world-liberty", "usd1"],
        "trading": ["trading", "trades", "tariff", "futures", "options"],
        "business-deals": ["investment", "real estate", "hotel", "merger", "deal"],
        "access-payments": ["lobbying", "donor", "dinner", "sponsor", "fundrais"],
        "contracts": ["contract", "procurement", "pentagon", "powerus"],
        "regulatory-actions": ["regulator", "enforcement", "sec", "pardon", "lawsuit", "settlement"],
        "disclosures": ["disclosure", "financial interest", "ownership", "ethics"],
        "appointments": ["appointment", "appointed", "nominee", "adviser"],
        "foreign-projects": ["foreign", "qatar", "saudi", "uae", "serbia", "albania", "oman"],
        "other": [],
    },
    "us-federal-spending": {
        "social-security": ["social security"], "medicare": ["medicare"],
        "medicaid": ["medicaid"], "health": ["health"],
        "defense": ["defense", "defence", "military", "pentagon"],
        "interest": ["interest", "servicing the debt"],
        "income-security": ["income security", "snap", "unemployment", "nutrition"],
        "veterans": ["veteran"], "education": ["education", "student"],
        "transport": ["transport", "highway", "airport", "infrastructure"],
        "energy-environment": ["energy", "environment", "climate", "natural resources"],
        "immigration": ["immigration", "detention", "border", "deportation"],
        "foreign-aid": ["foreign aid", "foreign assistance", "international affairs"],
        "agriculture": ["agriculture", "farm", "crop"],
        "justice-government": ["justice", "prison", "court", "general government"],
        "tax-expenditures": ["tax expenditure", "tax credit", "revenue loss"],
        "doge": ["doge", "government efficiency"],
        "waste-fraud": ["fraud", "improper payment", "waste"],
        "taxpayer-costs": ["taxpayer", "golf", "air force one"],
        "other": [],
    },
}


def plain(value):
    return str(value).strip() if value is not None else ""


def decimal_text(value):
    if value is None or isinstance(value, bool):
        return None
    text = plain(value).replace(",", "").replace("−", "-")
    if text.casefold() in {"", "null", "none", "na", "n/a", "...", "--", "*", "(**)"}:
        return None
    if re.fullmatch(r"\([\d.]+\)", text):
        text = "-" + text[1:-1]
    try:
        n = Decimal(text)
        return format(n, "f") if n.is_finite() else None
    except InvalidOperation:
        return None


def unit_scale(text):
    """Only explicit units/scales. Percentage points are not percentages."""
    t = plain(text).casefold()
    if not t:
        return None, None
    if "percentage point" in t:
        return "percentage_points", "1"
    if "percent" in t or "%" in t:
        return ("percent_of_gdp" if "gdp" in t else "percent"), "1"
    if re.search(r"\busd\b|\bu\.?s\.?\s*dollars?\b|\bdollars?\b|\$", t):
        scale = next((v for k, v in [("trillion", "1000000000000"), ("billion", "1000000000"), ("million", "1000000"), ("thousand", "1000")] if k in t), "1")
        # Constant dollars require the price base; never convert them to nominal USD.
        return ("USD_constant" if "constant" in t or "chained" in t else "USD"), scale
    for name in ["years", "months", "days", "employees", "FTEs"]:
        if t == name.casefold() or re.search(r"\("+name.casefold()+r"\)", t):
            return name, "1"
    return text, "1"


def financial_basis(text):
    value = re.sub(r"[_-]+", " ", plain(text).casefold())
    for basis, terms in [
        ("net_outlays", ["net outlay"]), ("gross_outlays", ["gross outlay"]),
        ("applicable_receipts", ["applicable receipt"]), ("budget_authority", ["budget authority"]),
        ("tax_expenditures", ["tax expenditure", "revenue loss"]),
        ("outlays", ["outlay"]), ("obligations", ["obligation"]),
        ("receipts", ["receipt"]), ("balance", ["balance"]),
        ("income", ["income"]), ("revenue", ["revenue"]),
        ("valuation", ["valuation", "asset value", "holdings value"]),
        ("investment", ["investment"]), ("savings", ["saving"]),
        ("deficit", ["deficit"]), ("debt", ["debt"]), ("cost", ["cost"]),
    ]:
        if any(re.search(r"\b"+re.escape(term), value) for term in terms):
            return basis
    return None


def measurement(expression, *, unit=None, value=None, metric="unspecified", period=None,
                basis=None, value_kind="unknown", qualifier=None, input_path=""):
    original = plain(expression)
    combined = original + " " + plain(unit)
    normalized_unit, scale = unit_scale(combined if re.search(r"\$|dollars?|USD|percent|%", combined, re.I) else unit)
    # A unit such as "USD" alongside "$4 billion" still needs the explicit scale.
    if normalized_unit in {"USD", "USD_constant"}:
        _, scale = unit_scale(combined)
    approx = bool(re.search(r"about|around|approximately|nearly|roughly|estimated|~", original, re.I))
    comparator = "approximate" if approx else "equal"
    # Negation must win before the shorter positive comparison it contains.
    if re.search(r"\b(?:no|not) more than\b|\bnot over\b|<=|≤", original, re.I):
        comparator = "at_most"
    elif re.search(r"\b(?:no|not) less than\b|\bnot under\b|>=|≥", original, re.I):
        comparator = "at_least"
    elif re.search(r"at most|up to", original, re.I):
        comparator = "at_most"
    elif re.search(r"at least|minimum", original, re.I):
        comparator = "at_least"
    elif re.search(r"\bless than\b|\bunder\b|<", original, re.I):
        comparator = "less_than"
    elif re.search(r"\bmore than\b|\bover\b|>", original, re.I):
        comparator = "greater_than"
    parsed = decimal_text(value)
    low = high = None
    numbers = re.findall(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?", original)
    is_range = bool(re.search(r"\d\s*(?:-|–|—|to)\s*(?:\$\s*)?\d", original))
    if is_range and len(numbers) == 2:
        # The separator isn't a negative sign (e.g. 10-20).
        low, high = decimal_text(numbers[0]), decimal_text(numbers[1].lstrip("-"))
        comparator = "range"
        parsed = None
    elif parsed is None and len(numbers) == 1 and original:
        parsed = decimal_text(numbers[0])
        if parsed and re.fullmatch(r"\([\d,.]+\)", original):
            parsed = "-" + parsed
    # Supplied numeric values may already be in base USD; compare with expression.
    if scale and scale != "1":
        expr_value = decimal_text(numbers[0]) if len(numbers) == 1 else None
        if parsed is not None:
            if expr_value is not None and Decimal(parsed) == Decimal(expr_value) * Decimal(scale):
                pass
            else:
                parsed = format(Decimal(parsed) * Decimal(scale), "f")
        low = format(Decimal(low) * Decimal(scale), "f") if low is not None else None
        high = format(Decimal(high) * Decimal(scale), "f") if high is not None else None
    if not original and value is not None:
        original = plain(value)
    return Measurement(metric=metric or "unspecified", value=parsed, low=low, high=high,
                       comparator=comparator if parsed is not None or low is not None else "unknown",
                       original_expression=original, original_unit=unit, unit=normalized_unit,
                       scale=scale, period=plain(period) or None, basis=financial_basis(basis), original_basis=basis,
                       value_kind=value_kind, qualifier=plain(qualifier) or None, input_path=input_path)


def parse_date(expression, role="unknown", input_path=""):
    original = plain(expression)
    result = DateValue(role=role, original_expression=original, input_path=input_path)
    text = original.strip()
    fiscal = bool(re.match(r"(?:FY|fiscal year)\s*\d{4}", text, re.I))
    if fiscal and re.fullmatch(r"(?:FY|fiscal year)\s*\d{4}", text, re.I):
        year = int(re.search(r"\d{4}", text)[0])
        if year < 1977:
            # Historical federal fiscal calendars changed. Retain the year label
            # rather than silently assigning the modern October–September span.
            return result.model_copy(update={"precision": "year", "calendar": "fiscal"})
        return result.model_copy(update={"start": f"{year-1}-10-01", "end": f"{year}-09-30", "precision": "year", "calendar": "fiscal"})
    # Year ranges do not imply a fiscal calendar without an explicit marker.
    years = re.fullmatch(r"(?:FY)?\s*(\d{4})\s*[-–—]\s*(\d{4})", text, re.I)
    if years:
        a, b = map(int, years.groups())
        if a <= b:
            if fiscal and a < 1977:
                return result.model_copy(update={"precision": "range", "calendar": "fiscal"})
            return result.model_copy(update={"start": f"{a-1 if fiscal else a}-{'10-01' if fiscal else '01-01'}", "end": f"{b}-{'09-30' if fiscal else '12-31'}", "precision": "range", "calendar": "fiscal" if fiscal else "unknown"})
    iso_range = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:to|[-–—])\s*(\d{4}-\d{2}-\d{2})", text, re.I)
    if iso_range:
        try:
            start, end = (date.fromisoformat(bound).isoformat() for bound in iso_range.groups())
            if start <= end:
                return result.model_copy(update={"start": start, "end": end, "precision": "range", "calendar": "calendar"})
        except ValueError:
            pass
        return result
    iso = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", text)
    if iso:
        y, m, d = (int(x) if x else None for x in iso.groups())
        try:
            start = date(y, m or 1, d or 1).isoformat()
            end = date(y, m or 12, d or (calendar.monthrange(y, m)[1] if m else 31)).isoformat()
            return result.model_copy(update={"start": start, "end": end, "precision": "day" if d else "month" if m else "year", "calendar": "calendar" if m else "unknown"})
        except ValueError:
            return result
    from datetime import datetime
    for pattern in ["%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%B %Y", "%b %Y"]:
        try:
            dt = datetime.strptime(text, pattern)
            return parse_date(dt.strftime("%Y-%m" if "%d" not in pattern else "%Y-%m-%d"), role, input_path).model_copy(update={"original_expression": original})
        except ValueError:
            pass
    return result


def categories(topic, text):
    text = text.casefold()
    found = [key for key, words in FACETS.get(topic, {}).items() if words and any(re.search(r"(?<!\w)" + re.escape(w), text) for w in words)]
    return found or ["other"]


def pointer(value, path):
    if path == "":
        return value
    if not path.startswith("/"):
        raise ValueError("Expected JSON pointer")
    for key in path[1:].split("/"):
        key = key.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def adapt_item(item):
    data = item.get("data") or {}
    origin = item.get("collection_metadata", {}).get("extraction_context", {})
    original_kind = item.get("collection_metadata", {}).get("source_item_kind") or item["kind"]
    role = "substantive"
    title = plain(item.get("title"))
    refs = item.get("references") or []
    url = refs[0].get("url", "") if refs else ""
    if item["kind"] == "headline":
        role = "source_lead"
        if not title or re.fullmatch(r"\[?(?:removed|deleted)\]?", title, re.I):
            role = "navigation"
        elif "/comment/" in url or ("ycombinator.com/item" in url and origin.get("type") == "agent_packet"):
            role = "comment"
    elif origin.get("type") in {"dataset_row", "spreadsheet_row", "pdf_schedule_cell"}:
        role = "table_observation"
    if "photo caption" in plain(item.get("detail")).casefold():
        role = "caption"
    kind = item["kind"] if item["kind"] in {"event", "statistic", "quote", "document", "example", "resource"} else "statement"
    result = Observation(id=ident("observation", item["id"], 0), topic=item["topic"],
                         record_type="source_reference" if role in {"source_lead", "navigation", "comment"} else "observation",
                         content_role=role, kind=kind, subtype=original_kind,
                         title=title or "Untitled source entry", detail=plain(item.get("detail")),
                         original_text=plain(item.get("original_text")), input_ids=[item["id"]],
                         references=refs, original_tags=[str(t) for t in item.get("tags", []) if t is not None],
                         field_lineage={"title": ["/title"] if "title" in item else [], "detail": ["/detail"] if "detail" in item else [], "subtype": ["/collection_metadata/source_item_kind"] if item.get("collection_metadata", {}).get("source_item_kind") else ["/kind"]},
                         processing={"origin": origin, "method": "deterministic"},
                         source_fields={k: item[k] for k in ["data", "dates", "measurements"] if k in item} | {"original_kind": original_kind})
    for i, entity in enumerate(item.get("entities") or data.get("entities") or []):
        name = entity.get("name") if isinstance(entity, dict) else entity
        if name:
            result.entities.append(EntityMention(name=str(name), input_path=f"/entities/{i}" if item.get("entities") else f"/data/entities/{i}"))
    for i, obj in enumerate(item.get("dates") or []):
        if not isinstance(obj, dict):
            continue
        expression = next((obj.get(k) for k in ["original_expression", "expression", "date", "iso", "normalized_date", "normalized", "period", "value", "text", "point", "year"] if obj.get(k) is not None), "")
        if not expression and obj.get("start") and obj.get("end"):
            expression = str(obj["start"]) + "–" + str(obj["end"])
        if not expression:
            expression = json.dumps(obj, ensure_ascii=False)
        result.dates.append(parse_date(expression, obj.get("role") or obj.get("context") or "unknown", f"/dates/{i}"))
    if data.get("date"):
        result.dates.append(parse_date(data["date"], data.get("date_type") or "event", "/data/date"))
    if item.get("published"):
        result.dates.append(parse_date(item["published"], "publication", "/published"))
    if origin.get("type") == "primary_statistic" and data.get("period"):
        period = parse_date(data["period"], "observation_period", "/data/period")
        if not any(d.role == period.role and d.original_expression == period.original_expression for d in result.dates):
            result.dates.append(period)
    if data.get("fiscal_period"):
        result.dates.append(parse_date(data["fiscal_period"], "reporting_period", "/data/fiscal_period"))
    for i, m in enumerate(item.get("measurements") or []):
        if not isinstance(m, dict):
            continue
        expression = m.get("original_expression")
        v = next((m[k] for k in ["value", "quantity", "normalized_value", "decimal_string"] if m.get(k) is not None), None)
        result.measurements.append(measurement(expression if expression is not None else v,
            unit=m.get("unit"), value=v, metric=m.get("metric") or m.get("name") or item["title"],
            period=m.get("period"), basis=m.get("basis") or data.get("basis"),
            qualifier=m.get("qualifier"), input_path=f"/measurements/{i}"))
    if origin.get("type") == "failed_chunk_recovery":
        for measure in result.measurements:
            if measure.original_unit == "USD per month":
                measure.unit = measure.original_unit
        row = data.get("source_row") or {}
        # Early Part B premium rates apply to the entire printed month range.
        # The source's calendar-year heading starts below these rows.
        months = re.fullmatch(r"([A-Za-z]+ [0-9]{4})–([A-Za-z]+ [0-9]{4})", plain(row.get("period")))
        if months and origin.get("table") == "III.C2":
            first, last = (parse_date(bound) for bound in months.groups())
            if first.start and last.end and first.start <= last.end:
                result.dates.append(DateValue(original_expression=row["period"], role="reporting_period",
                    start=first.start, end=last.end, precision="range", calendar="calendar",
                    input_path="/data/source_row/period"))
                result.processing["origin"] = {**origin, "period_basis": "explicit month range"}
                for ref in result.references:
                    locator = ref.get("locator", {})
                    if locator.get("table") == "III.C2" and locator.get("period") == row["period"]:
                        ref["locator"] = {**locator, "period_basis": "explicit month range"}
    for i, m in enumerate(data.get("amounts") or []):
        if isinstance(m, dict):
            expression = next((m[k] for k in ["original_expression", "original_text", "text", "amount", "value"] if m.get(k) is not None), "")
            result.measurements.append(measurement(expression, unit=m.get("unit") or m.get("currency"), value=m.get("value"), metric=m.get("description") or m.get("label") or "reported amount", period=m.get("period"), qualifier=m.get("qualifier"), input_path=f"/data/amounts/{i}"))
        elif isinstance(m, (str, int, float)) and not isinstance(m, bool):
            result.measurements.append(measurement(m, metric="reported amount", input_path=f"/data/amounts/{i}"))
    if data.get("attributed_author"):
        result.attribution.append(Attribution(name=str(data["attributed_author"]), role="reported_by", input_path="/data/attributed_author"))
    result.categories = categories(item["topic"], " ".join([title, result.detail, *result.original_tags]))
    result.warnings = [] if refs else ["missing_source_reference"]
    # Legacy Treasury measurements were multiplied by 1M. Rebuild from the retained raw field.
    if origin.get("type") == "dataset_row" and data.get("source_row", {}).get("current_fytd_net_outly_amt") is not None:
        row = data["source_row"]
        result.measurements = [measurement(row["current_fytd_net_outly_amt"], unit="USD", metric="fiscal_year_to_date_net_outlays", period=row.get("record_fiscal_year"), basis="net_outlays", input_path="/data/source_row/current_fytd_net_outly_amt")]
        result.detail = "Treasury fiscal-year-to-date net outlays; amount rebuilt from the original API dollar value."
        result.processing["corrections"] = ["treasury-api-dollars-v1"]
        result.warnings.append("legacy_input_has_million_fold_scaling_error")
    return result
