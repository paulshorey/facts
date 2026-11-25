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
        rows = db.execute("""
            WITH by_source AS (
                SELECT topic, source, count(*) AS items
                FROM items
                WHERE kind NOT IN ('headline', 'resource') AND source IS NOT NULL
                GROUP BY topic, source
            )
            SELECT t.id, count(b.source), coalesce(sum(b.items), 0), coalesce(max(b.items), 0)
            FROM topics t LEFT JOIN by_source b ON b.topic = t.id
            GROUP BY t.id ORDER BY count(b.source), t.id
        """)
        return [{"topic": topic, "originating_source_urls": sources, "sourced_items": items,
                 "largest_source_item_share": round(largest / items, 4) if items else None}
                for topic, sources, items, largest in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    print(json.dumps(coverage(parser.parse_args().root), indent=2))


if __name__ == "__main__":
    main()
