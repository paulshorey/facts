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
