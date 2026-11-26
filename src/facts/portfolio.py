"""Report how widely the local catalog draws on distinct sources across topics."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .core import ROOT


def coverage(root=ROOT):
    catalog = (Path(root) / "storage" / "catalog.sqlite").resolve()
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as db:
        # One originating source per item. Secondary citations must not inflate
        # either the denominator or the number of independently collected URLs.
        db.execute("BEGIN")
        topics = {topic: {"topic": topic, "originating_source_urls": 0, "sourced_items": 0,
                          "largest_source_item_share": None, "top_sources": []}
                  for topic, in db.execute("SELECT id FROM topics")}
        for topic, url, count in db.execute("""
            SELECT i.topic, s.url, count(*) AS n
            FROM items i JOIN sources s ON s.id=i.source JOIN topics t ON t.id=i.topic
            WHERE i.kind NOT IN ('headline', 'resource')
            GROUP BY i.topic, i.source ORDER BY i.topic, n DESC, s.url
        """):
            entry = topics[topic]
            entry["originating_source_urls"] += 1
            entry["sourced_items"] += count
            if len(entry["top_sources"]) < 5:
                entry["top_sources"].append({"url": url, "items": count})
        for entry in topics.values():
            if entry["top_sources"]:
                entry["largest_source_item_share"] = round(entry["top_sources"][0]["items"] / entry["sourced_items"], 4)
        return sorted(topics.values(), key=lambda entry: (entry["originating_source_urls"], entry["topic"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    print(json.dumps(coverage(parser.parse_args().root), indent=2))


if __name__ == "__main__":
    main()
