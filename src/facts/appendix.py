"""Collect the CBO Salaries and Expenses schedule from the FY2027 Budget Appendix."""
import argparse
from collections import defaultdict
from contextlib import closing, nullcontext
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


def parse_schedule(pdf, page_number):
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        page = document[page_number - 1]
        lines = defaultdict(list)
        for block in page.get_text("dict", clip=TABLE)["blocks"]:
            for line in block.get("lines", []):
                spans = line["spans"]
                x, y = spans[0]["origin"]
                lines[round(y, 1)].append((round(x, 1), "".join(s["text"] for s in spans)))
        if not page.search_for("CONGRESSIONAL BUDGET OFFICE") or not page.search_for("SALARIES AND EXPENSES"):
            raise ValueError("Missing account title")

    ordered = [sorted(parts) for _, parts in sorted(lines.items())]
    headers = [[t for _, t in line] for line in ordered[:2]]
    if headers != [[SCHEDULE], [f"Identification code {ACCOUNT}", *COLUMNS]]:
        raise ValueError("Unexpected schedule, account, scale or fiscal columns")

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
        (_, code), (x, raw_label), *cells = parts
        # Removing the dot leaders leaves the printed row description intact.
        label = re.sub(r"\s*\.{2,}\s*$", "", raw_label)
        # Bold section headings apply to totals printed at the same indentation.
        if not headings:
            raise ValueError("Missing row section")
        path = [h for indent, h in headings if indent < x or indent == headings[0][0]]
        values = []
        for (start, raw), column, left, right in zip(cells, COLUMNS, (481, 514, 547), (513, 546, 577), strict=True):
            if not left <= start < right or not re.fullmatch(r"-?\d+(?:,\d{3})*|\.{2,}", raw):
                raise ValueError(f"Unexpected cell in {code}, {column}: {raw}")
            values.append({"column": column, "expression": raw,
                           "millions_usd": None if raw.startswith(".") else int(raw.replace(",", ""))})
        rows.append({"code": code, "label": label, "source_label": raw_label,
                     "headings": path, "cells": values})
    if [r["code"] for r in rows] != ROW_CODES:
        raise ValueError("Row inventory differs from the reviewed schedule")
    return rows


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


def store_cells(store, rows, volume, chapter):
    for row in rows:
        for cell in row["cells"]:
            col = cell["column"]
            locator = {"type": "pdf_schedule_cell", "printed_page": 19, "pdf_page": 25,
                       "account_code": ACCOUNT, "schedule": SCHEDULE, "row": row["code"], "column": col}
            period = "FY" + col[:4]
            measure = {"metric": row["label"], "period": period, "qualifier": col[5:],
                       "unit": "millions of dollars", "basis": row["label"],
                       "original_expression": cell["expression"], "value": cell["millions_usd"]}
            value = {"kind": "statistic", "title": f"{AGENCY} · {row['label']} · {col}",
                     "original_text": cell["expression"], "entities": [AGENCY],
                     "data": {"account": "Salaries and Expenses", "fund": "Federal Funds",
                              "branch": "Legislative Branch", "hierarchy": row["headings"],
                              "source_label": row["source_label"], "fiscal_period": period,
                              "source_column": col, "unit": "millions of dollars"},
                     "measurements": [] if cell["millions_usd"] is None else [measure]}
            chapter_ref = {"source_id": chapter[0], "capture_id": chapter[1], "url": CHAPTER,
                           "locator": {**locator, "pdf_page": 7}}
            store.item(TOPIC, volume[0], volume[1], value, locator, additional_references=[chapter_ref])


def run(root, collect=False):
    root = Path(root).resolve()
    with writer_lock(root) if collect else nullcontext():
        return _process(root, collect)


def _process(root, collect):
    catalog = root / "storage/catalog.sqlite"
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as db:
        volume = saved_pdf(db, root, VOLUME)
        chapter = saved_pdf(db, root, CHAPTER)
    if not volume:
        raise ValueError("The saved FY2027 full Appendix capture is required")
    rows = parse_schedule(volume[2], 25)
    if not chapter and not collect:
        raise ValueError("Run --collect once to save the GovInfo chapter")
    if not chapter:
        pdf, text, metadata, status = retrieve(CHAPTER)
        if status != 200 or metadata.get("format") != "pdf":
            raise ValueError(f"GovInfo chapter download did not return a PDF (HTTP {status})")
    else:
        pdf = chapter[2]
    if rows != parse_schedule(pdf, 7):
        raise ValueError("The full volume and chapter disagree")
    if collect:
        store = Store(root)
        with closing(store.db), store.db:
            if not chapter:
                sid = store.source(TOPIC, CHAPTER, "FY2027 Budget Appendix · Legislative Branch", "2026-04-03", "budget-appendix")
                cid = store.capture(sid, "direct_http", pdf, text, metadata, status)
                chapter = (sid, cid, pdf)
            store_cells(store, rows, volume, chapter)
    return {"agency": AGENCY, "account": "Salaries and Expenses", "account_code": ACCOUNT,
            "schedule": SCHEDULE, "printed_page": 19, "columns": COLUMNS, "rows": rows,
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--collect", action="store_true", help="Save the chapter and verified cells in one transaction")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()
    result = run(args.root, collect=args.collect)
    print(report(result) if args.format == "markdown" else dumps(result))


if __name__ == "__main__":
    main()
