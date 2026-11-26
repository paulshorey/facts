import sqlite3

from facts.portfolio import coverage


def test_coverage_keeps_empty_topics_and_counts_originating_sources(tmp_path):
    directory = tmp_path / "storage"
    directory.mkdir()
    with sqlite3.connect(directory / "catalog.sqlite") as db:
        db.execute("CREATE TABLE topics(id TEXT)")
        db.execute("CREATE TABLE sources(id TEXT, url TEXT)")
        db.executemany("INSERT INTO sources VALUES (?,?)", [(s, "https://example.org/" + s) for s in "abcd"])
        db.execute("CREATE TABLE items(topic TEXT, source TEXT, kind TEXT)")
        db.executemany("INSERT INTO topics VALUES (?)", [("old",), ("new",)])
        db.executemany("INSERT INTO items VALUES ('old',?,?)", [
            ("a", "statistic"), ("a", "fact"), ("b", "statistic"),
            ("c", "headline"), ("d", "resource"), (None, "fact")])
    empty, old = coverage(tmp_path)
    assert empty == {"topic": "new", "sourced_items": 0, "originating_source_urls": 0,
                     "largest_source_item_share": None, "top_sources": []}
    assert (old["sourced_items"], old["originating_source_urls"], old["largest_source_item_share"]) == (3, 2, 0.6667)
    assert old["top_sources"] == [{"url": "https://example.org/a", "items": 2},
                                  {"url": "https://example.org/b", "items": 1}]
