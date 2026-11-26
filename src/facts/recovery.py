"""Close failed extraction jobs against the item receipts from a reviewed recovery."""
import argparse
from contextlib import closing
import json
from pathlib import Path

from .core import ROOT, Store
from .runtime import writer_lock


def reconcile(store, receipts):
    """Record the disposition atomically and retain the failed output and attempt count."""
    with writer_lock(store.root), store.db:
        for receipt in receipts:
            job_id = receipt["job_id"]
            job = store.db.execute(
                """SELECT j.state, j.topic, j.capture, c.source, s.url FROM extraction_jobs j
                   JOIN captures c ON c.id=j.capture JOIN sources s ON s.id=c.source WHERE j.id=?""",
                (job_id,),
            ).fetchone()
            if (not job or job["state"] not in {"failed", "recovered"}
                    or job["capture"] != receipt["original_capture_id"] or job["url"] != receipt["url"]):
                raise ValueError(f"Recovery does not match failed job {job_id}")
            ids = receipt["item_ids"]
            if not ids or len(ids) != len(set(ids)) or len(ids) != receipt["rows"]:
                raise ValueError(f"Incomplete recovery row inventory for {job_id}")
            rows = store.db.execute("""
                SELECT i.id, i.capture, i.payload, c.source AS capture_source
                FROM items i LEFT JOIN captures c ON c.id=i.capture
                WHERE i.topic=? AND i.source=?
                  AND json_extract(i.payload,'$.collection_metadata.extraction_context.type')='failed_chunk_recovery'
                  AND json_extract(i.payload,'$.collection_metadata.extraction_context.original_job_id')=?
            """, (job["topic"], job["source"], job_id)).fetchall()
            if {row["id"] for row in rows} != set(ids):
                raise ValueError(f"Replacement items differ from the catalog for {job_id}")
            cells = 0
            for row in rows:
                item = json.loads(row["payload"])
                origin = item["collection_metadata"]["extraction_context"]
                if (row["capture_source"] != job["source"] or row["capture"] != receipt["capture_id"]
                        or origin.get("table") != receipt["table"]):
                    raise ValueError(f"Recovery item {row['id']} cites a different capture or table")
                cells += len(item["data"]["source_row"]["cells"])
            if not cells or cells != receipt["cells"]:
                raise ValueError(f"Incomplete recovery cell inventory for {job_id}")
            if job["state"] == "recovered":
                saved = store.db.execute(
                    "SELECT metadata FROM attempts WHERE stage='manual_recovery' AND target=? AND outcome='recovered'",
                    (job_id,),
                ).fetchone()
                if not saved or json.loads(saved[0]) != receipt:
                    raise ValueError(f"Conflicting recovery receipt for {job_id}")
                continue
            store.attempt("manual_recovery", job_id, "recovered", metadata=receipt)
            store.db.execute("UPDATE extraction_jobs SET state='recovered' WHERE id=?", (job_id,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    receipts = json.loads(args.receipts.read_text())
    with writer_lock(args.root):
        store = Store(args.root)
        with closing(store.db):
            reconcile(store, receipts)


if __name__ == "__main__":
    main()
