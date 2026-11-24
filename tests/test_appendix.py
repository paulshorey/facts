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
