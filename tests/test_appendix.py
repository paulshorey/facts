from contextlib import closing
import json
import sqlite3

import pymupdf
import pytest

from facts import appendix
from facts.core import Store
from facts.organize_fields import adapt_item


def sample_pdf(page_number, last=8, omit_last=False, shift_columns=False):
    """A two-column schedule with a decoy outside the selected panel."""
    with pymupdf.open() as doc:
        for _ in range(page_number):
            page = doc.new_page(width=612, height=792)
        def put(x, y, text):
            page.insert_text((x, y), text, fontsize=5)

        put(312, 140, "CONGRESSIONAL BUDGET OFFICE")
        put(312, 165, "SALARIES AND EXPENSES")
        put(320, 345, appendix.SCHEDULE)
        put(312, 361, "Identification code " + appendix.ACCOUNT.replace("–", "-"))
        for x, name in zip([484, 520, 553], appendix.COLUMNS):
            put(x, 361, name)
        put(327, 373, "Budgetary resources:")
        put(333, 380, "Budget authority:")
        put(36, 387, "9999")
        for i, code in enumerate(appendix.ROW_CODES):
            if omit_last and code == appendix.ROW_CODES[-1]:
                continue
            y = 387 + i * 13
            put(312, y, code)
            put(340, y, "Example row ...........")
            put(502, y, "-1")
            put(525 if not shift_columns else 508, y, ".....")
            put(567, y, str(last))
        return doc.tobytes()


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    # The built-in PDF font encodes hyphens, so use that spelling in this fixture.
    monkeypatch.setattr(appendix, "ACCOUNT", appendix.ACCOUNT.replace("–", "-"))
    store = Store(tmp_path)
    with closing(store.db), store.db:
        sid = store.source(appendix.TOPIC, appendix.VOLUME)
        store.capture(sid, "direct_http", sample_pdf(25), "", {})
    def download(_):
        return sample_pdf(7), "sample chapter", {"format": "pdf"}, 200
    monkeypatch.setattr(appendix, "retrieve", download)
    return tmp_path


def test_replay_and_evidence(pilot, monkeypatch):
    result = appendix.run(pilot, collect=True)
    def offline(_):
        pytest.fail("Replaying retained sources requested the network")
    monkeypatch.setattr(appendix, "retrieve", offline)
    assert result == appendix.run(pilot) == appendix.run(pilot, collect=True)
    rows = result["rows"]
    assert len(rows) == 21
    assert rows[0]["headings"] == ["Budgetary resources:", "Budget authority:"]
    assert [c["millions_usd"] for c in rows[0]["cells"]] == [-1, None, 8]
    assert "FY2027 estimate" in appendix.report(result)
    with closing(sqlite3.connect(pilot / "storage/catalog.sqlite")) as db:
        items = [json.loads(raw) for raw, in db.execute("SELECT payload FROM items")]
        assert len(items) == 63
        assert all([r["locator"]["pdf_page"] for r in item["references"]] == [25, 7] for item in items)
        observed = [adapt_item(item) for item in items]
        assert all(o.content_role == "table_observation" for o in observed)
        assert {m.value for o in observed for m in o.measurements} == {"-1000000", "8000000"}
        assert sum(not o.measurements for o in observed) == 21


@pytest.mark.parametrize("changes", [{"last": 2}, {"omit_last": True}, {"shift_columns": True}])
def test_disagreement_never_publishes_partial_schedule(pilot, monkeypatch, changes):
    monkeypatch.setattr(appendix, "retrieve", lambda _: (sample_pdf(7, **changes), "", {"format": "pdf"}, 200))
    with pytest.raises(ValueError):
        appendix.run(pilot, collect=True)
    with closing(sqlite3.connect(pilot / "storage/catalog.sqlite")) as db:
        assert db.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 0


def adjacent_pdf(page_number, panels, *, wrong_header=False, different=False):
    with pymupdf.open() as doc:
        for _ in range(page_number):
            page = doc.new_page(width=612, height=792)
        for index, panel in enumerate(panels):
            offset = panel.x_offset
            def put(x, y, text):
                page.insert_text((x + offset, y), text, fontsize=5)
            for y, schedule, row, label in (
                (150, appendix.OBJECTS, "99.9", "Total new obligations"),
                (320, appendix.EMPLOYMENT, "1001", "Direct civilian full-time equivalent employment"),
            ):
                put(60, y, schedule)
                code = panels[0].code if wrong_header and index else panel.code
                put(36, y + 10, "Identification code " + code)
                for x, name in zip((208, 244, 277), appendix.COLUMNS):
                    put(x, y + 10, name)
                if schedule == appendix.OBJECTS:
                    put(52, y + 20, "Direct obligations:")
                put(36, y + 30, row)
                put(60, y + 30, label)
                # Right-to-left PDF stream order and the same row code in each panel.
                for year in (2, 1, 0):
                    value = index * 100 + year + int(different and index and year == 2)
                    put(222 + 33 * year, y + 30, str(value))
        return doc.tobytes()


@pytest.mark.parametrize("fault", [None, "account", "value"])
def test_adjacent_accounts_never_borrow_cells(pilot, monkeypatch, fault):
    panels = tuple(appendix.Panel("Agency", name, code, 20, offset, (141, 189), (311, 360), "99.9")
                   for name, code, offset in [("Left", "A-0100", 0), ("Right", "B-0100", 276)])
    monkeypatch.setattr(appendix, "PANELS", panels)
    store = Store(pilot)
    with closing(store.db), store.db:
        sid = store.source(appendix.TOPIC, appendix.VOLUME)
        store.capture(sid, "direct_http", adjacent_pdf(26, panels), "", {})
        sid = store.source(appendix.TOPIC, appendix.CHAPTER)
        store.capture(sid, "direct_http", adjacent_pdf(8, panels, wrong_header=fault=="account",
                                                       different=fault=="value"), "", {})
    if fault:
        with pytest.raises(ValueError):
            appendix.run(pilot, True, neighboring=True)
        with closing(sqlite3.connect(pilot / "storage/catalog.sqlite")) as db:
            assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
        return
    result = appendix.run(pilot, True, neighboring=True)
    assert result == appendix.run(pilot, True, neighboring=True)
    assert [t["rows"][0]["cells"][2]["value"] for t in result["tables"]] == [2, 2, 102, 102]
    with closing(sqlite3.connect(pilot / "storage/catalog.sqlite")) as db:
        items = [json.loads(raw) for raw, in db.execute("SELECT payload FROM items")]
    assert len(items) == 12
    for item in items:
        refs = item["references"]
        assert [r["locator"]["pdf_page"] for r in refs] == [26, 8]
        locator = refs[0]["locator"]
        assert refs[1]["locator"]["account_code"] == locator["account_code"]
        measurement = adapt_item(item).measurements[0]
        n = int(locator["column"][:4]) - 2025 + (100 if locator["account_code"] == "B-0100" else 0)
        if locator["schedule"] == appendix.EMPLOYMENT:
            assert (measurement.unit, measurement.value) == ("FTEs", str(n))
        else:
            assert (measurement.unit, measurement.value) == ("USD", str(n * 1_000_000))


def house_pdf(page_offset, *, wrong_anchor=False, move_row=False, changed_value=False):
    """The objects header and its last eight rows occupy different pages and panels."""
    with pymupdf.open() as doc:
        for _ in range(23 + page_offset):
            doc.new_page(width=612, height=792)
        first, second = doc[-2], doc[-1]

        def put(page, x, y, text):
            page.insert_text((x, y), text, fontsize=5)

        def header(page, dx, y, title, account):
            put(page, 60 + dx, y, title)
            put(page, 36 + dx, y + 14, "Identification code " + account)
            for x, column in zip((208, 244, 277), appendix.COLUMNS):
                put(page, dx + x, y + 14, column)

        def row(page, dx, y, code, label, index, indent=60):
            put(page, dx + 36, y, code)
            put(page, dx + indent, y, label)
            for year in (2, 1, 0):
                value = index + year + int(changed_value and code == "99.9" and year == 2)
                put(page, dx + 223 + 33 * year, y, str(value))

        put(first, 340, 72, "HOUSE OFFICE BUILDINGS")
        header(first, 276, 158, appendix.SCHEDULE, appendix.HOUSE_ACCOUNT)
        put(first, 328, 191, "Funding:")
        row(first, 276, 201, "0001", "House Office Buildings (Direct)", 22)
        header(first, 276, 690, appendix.OBJECTS, appendix.HOUSE_ACCOUNT)
        put(first, 328, 722, "Direct obligations:")
        put(first, 334, 730, "Personnel compensation:")
        for i, code in enumerate(("11.1", "11.3", "11.5")):
            if not (move_row and code == "11.5"):
                row(first, 276, 738 + 8 * i, code, "Personnel", 40 + i, 65)
        if move_row:
            row(second, 0, 61, "11.5", "Personnel", 42, 65)
        for i, code in enumerate("11.9 12.1 25.1 25.4 26.0 32.0 99.0 99.9".split()):
            row(second, 0, 68 + i * 9, code, "Object", 52 + i, 70 if i == 0 else 57)
        # Other account on the facing panel, with the same row code.
        row(second, 276, 68, "11.9", "Other account", 999)
        header(second, 0, 155, appendix.EMPLOYMENT, "other-account" if wrong_anchor else appendix.HOUSE_ACCOUNT)
        row(second, 0, 186, "1001", "Direct civilian full-time equivalent employment", 563)
        return doc.tobytes()


@pytest.mark.parametrize("fault", [None, "wrong_anchor", "move_row", "changed_value"])
def test_house_schedule_continuation(pilot, monkeypatch, fault):
    monkeypatch.setattr(appendix, "HOUSE_FINANCING_ROWS", ["0001"])
    monkeypatch.setattr(appendix, "HOUSE_ACCOUNT", "001-0127-0-1-801")
    store = Store(pilot)
    with closing(store.db), store.db:
        for url, offset in [(appendix.VOLUME, 6), (appendix.CHAPTER, -12)]:
            changes = {fault: True} if fault and offset == -12 else {}
            sid = store.source(appendix.TOPIC, url)
            store.capture(sid, "direct_http", house_pdf(offset, **changes), "", {})
    if fault:
        with pytest.raises(ValueError):
            appendix.run(pilot, collect=True, house=True)
        with closing(sqlite3.connect(pilot / "storage/catalog.sqlite")) as db:
            assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
        return
    result = appendix.run(pilot, collect=True, house=True)
    assert result == appendix.run(pilot, collect=True, house=True) == appendix.run(pilot, house=True)
    rows = result["tables"][2]["rows"]
    assert rows[0]["headings"] == ["Direct obligations:", "Personnel compensation:"]
    assert rows[1]["headings"] == ["Direct obligations:"]
    assert [c["value"] for c in rows[0]["cells"]] == [52, 53, 54]
    with closing(sqlite3.connect(pilot / "storage/catalog.sqlite")) as db:
        items = [json.loads(raw) for raw, in db.execute("SELECT payload FROM items")]
    assert len(items) == 39
    continued = next(i for i in items if i["references"][0]["locator"]["row"] == "11.9")
    locators = [r["locator"] for r in continued["references"]]
    assert [l["pdf_page"] for l in locators] == [29, 11]
    assert all(l["header_printed_page"] == 22 and l["bbox"] != l["header_bbox"] for l in locators)


def test_repeated_financing_header_preserves_section_and_checks_account():
    box = pymupdf.Rect(34, 73, 302, 148)
    with pymupdf.open() as doc:
        page = doc.new_page()
        for x, y, text in [(60, 80, "Program and Financing-Continued"),
                           (36, 100, "Identification code Senate"), (58, 115, "Budget authority:"),
                           (36, 133, "1100"), (70, 133, "Appropriation")]:
            page.insert_text((x, y), text, fontsize=5)
        for i, column in enumerate(appendix.COLUMNS):
            page.insert_text((208 + 33*i, 100), column, fontsize=5)
            page.insert_text((223 + 33*i, 133), str(i+1), fontsize=5)
        def read(account):
            return appendix.parse_table(page, box, 0, "Program and Financing-Continued", account, ["1100"],
                                        inherited=[(51.8, "Budgetary resources:")], close_sections=True)
        assert read("Senate")[0]["headings"] == ["Budgetary resources:", "Budget authority:"]
        with pytest.raises(ValueError, match="account"):
            read("House")
