"""Collect account-coded Legislative Branch schedules from the FY2027 Budget Appendix."""
import argparse
from collections import defaultdict
from contextlib import closing, nullcontext
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sqlite3

import pymupdf

from .core import ROOT, Store, dumps, retrieve
from .runtime import writer_lock


TOPIC = "us-federal-spending"
VOLUME = "https://www.whitehouse.gov/wp-content/uploads/2026/04/appendix_fy2027.pdf"
CHAPTER = "https://www.govinfo.gov/content/pkg/BUDGET-2027-APP/pdf/BUDGET-2027-APP-2-3.pdf"
ACCOUNT = "008–0100–0–1–801"
SCHEDULE = "Program and Financing (in millions of dollars)"
COLUMNS = ["2025 actual", "2026 est.", "2027 est."]
AGENCY = "Congressional Budget Office"
# The bounding box and row inventory were checked against printed page 19.
TABLE = pymupdf.Rect(310, 335, 578, 696)
ROW_CODES = "0001 1100 1930 3000 3010 3011 3020 3041 3050 3100 3200 4000 4010 4011 4020 4033 4052 4070 4080 4180 4190".split()


@dataclass(frozen=True)
class Panel:
    agency: str
    account: str
    code: str
    printed_page: int
    x_offset: int
    object_bounds: tuple[int, int]
    employment_bounds: tuple[int, int]
    object_rows: str


ARCHITECT = "Architect of the Capitol"
OBJECTS = "Object Classification (in millions of dollars)"
EMPLOYMENT = "Employment Summary"
# Each crop ends before the next schedule or decorative page furniture.
PANELS = (
    Panel(AGENCY, "Salaries and Expenses", ACCOUNT, 20, 0, (151, 316), (324, 369),
          "11.1 11.3 11.5 11.9 12.1 23.3 25.1 25.2 25.7 31.0 99.0 99.9"),
    Panel(ARCHITECT, "Capital Construction and Operations", "001–0100–0–1–801", 20, 276,
          (335, 479), (483, 532), "11.1 11.5 11.9 12.1 23.2 25.1 25.4 26.0 31.0 99.9"),
    Panel(ARCHITECT, "Capitol Building", "001–0105–0–1–801", 21, 0, (376, 522), (527, 573),
          "11.1 11.3 11.5 11.9 12.1 25.1 25.4 26.0 32.0 99.9"),
    Panel(ARCHITECT, "Capitol Grounds", "001–0108–0–1–801", 21, 276, (322, 476), (480, 526),
          "11.1 11.5 11.9 12.1 23.2 25.1 25.4 26.0 31.0 32.0 99.9"),
)


def parse_table(page, box, offset, schedule, account, codes):
    # Align both printed panels to the left panel's coordinate system before
    # associating any values. PDF text stream order can reverse the three years.
    lines = defaultdict(list)
    for block in page.get_text("dict", clip=box)["blocks"]:
        for line in block.get("lines", []):
            spans = line["spans"]
            x, y = spans[0]["origin"]
            lines[round(y, 1)].append((round(x - offset, 1), "".join(s["text"] for s in spans)))
    ordered = [sorted(parts) for _, parts in sorted(lines.items())]
    headers = [[t for _, t in line] for line in ordered[:2]]
    if headers != [[schedule], [f"Identification code {account}", *COLUMNS]]:
        raise ValueError(f"Unexpected schedule, account or fiscal columns for {account}: {schedule}")
    bands = ((205, 237), (238, 270), (271, 301))
    if any(not left <= x < right for (x, _), (left, right) in zip(ordered[1][1:], bands, strict=True)):
        raise ValueError("Fiscal column heading outside its expected position")

    rows = []
    headings = []
    for parts in ordered[2:]:
        if len(parts) == 1 and parts[0][1].endswith(":"):
            x, title = parts[0]
            headings = [(indent, h) for indent, h in headings if indent < x]
            headings.append((x, title))
            continue
        if len(parts) != 5:
            raise ValueError(f"Incomplete schedule row: {parts}")
        (code_x, code), (x, raw_label), *cells = parts
        if abs(code_x - 36) > 1 or not 50 <= x < 205:
            raise ValueError(f"Unexpected row code or label position for {code}")
        # Removing the dot leaders leaves the printed row description intact.
        label = re.sub(r"\s*\.{2,}\s*$", "", raw_label)
        # Bold section headings apply to totals printed at the same indentation.
        if not headings and schedule != EMPLOYMENT:
            raise ValueError("Missing row section")
        path = [h for indent, h in headings if indent < x or indent == headings[0][0]]
        values = []
        for (start, raw), column, (left, right) in zip(cells, COLUMNS, bands, strict=True):
            if not left <= start < right or not re.fullmatch(r"-?\d+(?:,\d{3})*|\.{2,}", raw):
                raise ValueError(f"Unexpected cell in {code}, {column}: {raw}")
            values.append({"column": column, "expression": raw,
                           "value": None if raw.startswith(".") else int(raw.replace(",", ""))})
        rows.append({"code": code, "label": label, "source_label": raw_label,
                     "headings": path, "cells": values})
    if [r["code"] for r in rows] != codes:
        raise ValueError("Row inventory differs from the reviewed schedule")
    return rows


def parse_schedule(pdf, page_number):
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        page = document[page_number - 1]
        if not page.search_for("CONGRESSIONAL BUDGET OFFICE") or not page.search_for("SALARIES AND EXPENSES"):
            raise ValueError("Missing account title")
        rows = parse_table(page, TABLE, 276, SCHEDULE, ACCOUNT, ROW_CODES)
    # Preserve the original public page-19 JSON and item identities.
    for row in rows:
        for cell in row["cells"]:
            cell["millions_usd"] = cell.pop("value")
    return rows


def account_tables(pdf, page_offset):
    tables = []
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        for panel in PANELS:
            page = document[panel.printed_page + page_offset - 1]
            for schedule, bounds, codes, unit in (
                (OBJECTS, panel.object_bounds, panel.object_rows.split(), "millions of dollars"),
                (EMPLOYMENT, panel.employment_bounds, ["1001"], "FTEs"),
            ):
                box = pymupdf.Rect(34 + panel.x_offset, bounds[0], 302 + panel.x_offset, bounds[1])
                rows = parse_table(page, box, panel.x_offset, schedule, panel.code, codes)
                if schedule == EMPLOYMENT and rows[0]["label"] != "Direct civilian full-time equivalent employment":
                    raise ValueError("Unexpected employment metric")
                tables.append({"agency": panel.agency, "account": panel.account, "account_code": panel.code,
                               "schedule": schedule, "printed_page": panel.printed_page, "unit": unit,
                               "columns": COLUMNS, "bbox": list(box), "rows": rows})
    return tables


def saved_pdf(db, root, url):
    row = db.execute("""SELECT s.id,c.id,c.artifact FROM captures c JOIN sources s ON s.id=c.source
        WHERE s.url=? AND c.level='direct_http' AND c.state='succeeded'
        ORDER BY c.acquired DESC, c.id DESC LIMIT 1""", (url,)).fetchone()
    if not row:
        return None
    artifact = Path(row[2])
    digest = artifact.name
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or artifact != Path("storage/objects") / digest[:2] / digest:
        raise ValueError("Invalid PDF artifact path")
    path = (root / artifact).resolve()
    if not path.is_relative_to(root / "storage/objects"):
        raise ValueError("PDF artifact outside object store")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("PDF capture failed content-address verification")
    return row[0], row[1], data


def store_cells(store, rows, volume, chapter, *, table=None):
    agency, account, account_code = AGENCY, "Salaries and Expenses", ACCOUNT
    schedule, page, unit, field = SCHEDULE, 19, "millions of dollars", "millions_usd"
    if table is not None:
        agency, account, account_code = table["agency"], table["account"], table["account_code"]
        schedule, page, unit, field = table["schedule"], table["printed_page"], table["unit"], "value"
    for row in rows:
        for cell in row["cells"]:
            col = cell["column"]
            locator = {"type": "pdf_schedule_cell", "printed_page": page, "pdf_page": page + 6,
                       "account_code": account_code, "schedule": schedule, "row": row["code"], "column": col}
            period = "FY" + col[:4]
            measure = {"metric": row["label"], "period": period, "qualifier": col[5:],
                       "unit": unit, "basis": row["label"],
                       "original_expression": cell["expression"], "value": cell[field]}
            value = {"kind": "statistic", "title": f"{agency} · {row['label']} · {col}",
                     "original_text": cell["expression"], "entities": [agency],
                     "data": {"account": account, "fund": "Federal Funds",
                              "branch": "Legislative Branch", "hierarchy": row["headings"],
                              "source_label": row["source_label"], "fiscal_period": period,
                              "source_column": col, "unit": unit},
                     "measurements": [] if cell[field] is None else [measure]}
            if table is not None:
                locator["bbox"] = table["bbox"]
                value["title"] = f"{agency} · {account} · {row['label']} · {col}"
            chapter_ref = {"source_id": chapter[0], "capture_id": chapter[1], "url": CHAPTER,
                           "locator": {**locator, "pdf_page": page - 12}}
            store.item(TOPIC, volume[0], volume[1], value, locator, additional_references=[chapter_ref])


def run(root, collect=False, *, neighboring=False):
    root = Path(root).resolve()
    with writer_lock(root) if collect else nullcontext():
        return _process(root, collect, neighboring)


def _process(root, collect, neighboring):
    catalog = root / "storage/catalog.sqlite"
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as db:
        volume = saved_pdf(db, root, VOLUME)
        chapter = saved_pdf(db, root, CHAPTER)
    if not volume:
        raise ValueError("The saved FY2027 full Appendix capture is required")
    parsed = account_tables(volume[2], 6) if neighboring else parse_schedule(volume[2], 25)
    if not chapter and not collect:
        raise ValueError("Run --collect once to save the GovInfo chapter")
    if not chapter:
        pdf, text, metadata, status = retrieve(CHAPTER)
        if status != 200 or metadata.get("format") != "pdf":
            raise ValueError(f"GovInfo chapter download did not return a PDF (HTTP {status})")
    else:
        pdf = chapter[2]
    comparison = account_tables(pdf, -12) if neighboring else parse_schedule(pdf, 7)
    if parsed != comparison:
        raise ValueError("The full volume and chapter disagree")
    if collect:
        store = Store(root)
        with closing(store.db), store.db:
            if not chapter:
                sid = store.source(TOPIC, CHAPTER, "FY2027 Budget Appendix · Legislative Branch", "2026-04-03", "budget-appendix")
                cid = store.capture(sid, "direct_http", pdf, text, metadata, status)
                chapter = (sid, cid, pdf)
            if neighboring:
                for table in parsed:
                    store_cells(store, table["rows"], volume, chapter, table=table)
            else:
                store_cells(store, parsed, volume, chapter)
    if neighboring:
        sources = [{"url": url, "capture_id": src[1], "source_id": src[0],
                    "sha256": hashlib.sha256(src[2]).hexdigest(), "pdf_page_offset": offset}
                   for url, src, offset in [(VOLUME, volume, 6), (CHAPTER, chapter, -12)]]
        return {"tables": parsed, "sources": sources}
    return {"agency": AGENCY, "account": "Salaries and Expenses", "account_code": ACCOUNT,
            "schedule": SCHEDULE, "printed_page": 19, "columns": COLUMNS, "rows": parsed,
            "sources": [{"url": url, "source_id": src[0], "capture_id": src[1], "pdf_page": page,
                         "sha256": hashlib.sha256(src[2]).hexdigest()}
                        for url, src, page in [(VOLUME, volume, 25), (CHAPTER, chapter, 7)]]}


def report(result):
    by_code = {r["code"]: r for r in result["rows"]}

    def table_row(row):
        values = [c["expression"] if c["millions_usd"] is not None else "···" for c in row["cells"]]
        return "| " + " | ".join([row["code"], row["label"], *values]) + " |"

    lines = ["# Congressional Budget Office · Salaries and Expenses", "",
             "FY2027 Budget Appendix · Legislative Branch · Federal Funds · Printed page 19", "",
             "The program and financing schedule reports amounts in millions of dollars. "
             "FY2025 is actual. FY2026 and FY2027 are the estimates printed in the April 2026 budget.", "",
             "| Row | Measure (USD millions) | FY2025 actual | FY2026 estimate | FY2027 estimate |",
             "| --- | --- | ---: | ---: | ---: |",
             table_row(by_code["4180"]), table_row(by_code["4190"]), "",
             "## Full account schedule", "",
             f"Account {ACCOUNT}. Rows include balances, movements, subtotals and memorandum entries. "
             "The dot symbol reproduces a printed leader in a value column and carries no numeric value "
             "in this extract.", ""]
    section = None
    for row in result["rows"]:
        if row["headings"][0] != section:
            if section is not None:
                lines.append("")
            section = row["headings"][0]
            lines.extend([f"### {section.rstrip(':')}", "",
                          "| Row | Measure (USD millions) | FY2025 actual | FY2026 estimate | FY2027 estimate |",
                          "| --- | --- | ---: | ---: | ---: |"])
        lines.append(table_row(row))
    lines.extend(["", "## Source copies", ""])
    for source in result["sources"]:
        lines.append(f"- [{source['url'].split('/')[2]} · PDF page {source['pdf_page']}]"
                     f"({source['url']}#page={source['pdf_page']}) · capture `{source['capture_id']}`")
    return "\n".join(lines)


def account_report(result):
    lines = ["# FY2027 Budget Appendix · account schedules", "",
             "The FY2025 column reports actuals. FY2026 and FY2027 are estimates from the April 2026 budget. "
             "Dots reproduce unvalued source cells.", ""]
    for table in result["tables"]:
        page = table["printed_page"]
        lines.extend([f"## {table['agency']} · {table['account']}", "",
                      f"{table['schedule']} · {table['account_code']} · printed page {page}", "",
                      f"| Row | Measure ({table['unit']}) | FY2025 actual | FY2026 est. | FY2027 est. |",
                      "| --- | --- | ---: | ---: | ---: |"])
        for row in table["rows"]:
            cells = [c["expression"] if c["value"] is not None else "···" for c in row["cells"]]
            lines.append("| " + " | ".join([row["code"], row["label"], *cells]) + " |")
        lines += ["", " · ".join(f"[{s['url'].split('/')[2]}]({s['url']}#page={page+s['pdf_page_offset']})"
                                 for s in result["sources"]), ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--collect", action="store_true", help="Save the chapter and verified cells in one transaction")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--neighboring-accounts", action="store_true", help="Read the four account panels on printed pages 20–21")
    args = parser.parse_args()
    result = run(args.root, collect=args.collect, neighboring=args.neighboring_accounts)
    render = account_report if args.neighboring_accounts else report
    print(render(result) if args.format == "markdown" else dumps(result))


if __name__ == "__main__":
    main()
