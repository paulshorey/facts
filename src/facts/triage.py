"""Deterministic, reversible triage of the queued LLM extraction backlog.

Deferral never deletes jobs, captures or text. A deferred job keeps its chunk
and is excluded from dispatch until restored. Each decision is recorded as a
``triage`` attempt with its reason and evidence.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

# Only demonstrably empty captures (e.g. image thumbnails) are deferred: a short headline
# or excerpt can still carry a complete allegation, figure or quotation.
MIN_ALNUM_CHARS = 20
REASONS = ("insufficient_text", "structured_dataset", "duplicate_capture_text")


class CatalogReader:
    """Read-only catalog access for previews: no DDL, topic loading or writes."""
    def __init__(self, root):
        self.root = Path(root)
        self.db = sqlite3.connect(f"file:{self.root / 'storage/catalog.sqlite'}?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row

    def read(self, path):
        return (self.root / path).read_text(errors="replace")


def _normalized_digest(store, artifact):
    if not artifact:
        return None, 0
    text = store.read(artifact)
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest(), sum(ch.isalnum() for ch in text)


def plan(store, topic):
    """Return {job_id: (reason, evidence)} for queued jobs worth deferring."""
    db = store.db
    rows = db.execute("""SELECT j.id, j.capture, j.chunk, c.source, c.text_artifact, c.acquired
        FROM extraction_jobs j JOIN captures c ON c.id=j.capture
        WHERE j.topic=? AND j.state='queued' ORDER BY j.id""", (topic,)).fetchall()
    datasets = {r[0] for r in db.execute("SELECT source FROM datasets WHERE topic=? AND source IS NOT NULL", (topic,))}
    captures = {}
    for row in rows:
        if row["capture"] not in captures:
            digest, size = _normalized_digest(store, row["text_artifact"])
            captures[row["capture"]] = {"source": row["source"], "digest": digest, "size": size}
    # Among identical texts keep the capture with the most completed work, then the first id.
    done = dict(db.execute("SELECT capture, count(*) FROM extraction_jobs WHERE topic=? AND state IN ('succeeded','split') GROUP BY 1", (topic,)).fetchall())
    by_digest = defaultdict(list)
    for capture, info in captures.items():
        if info["digest"]:
            by_digest[info["digest"]].append(capture)
    # Duplicate groups also consider identical captures that have no queued work left.
    for row in db.execute("""SELECT DISTINCT c.id, c.text_artifact FROM captures c JOIN extraction_jobs j ON j.capture=c.id
            WHERE j.topic=? AND j.state IN ('succeeded','split')""", (topic,)):
        if row["id"] not in captures:
            digest, _ = _normalized_digest(store, row["text_artifact"])
            if digest in by_digest:
                by_digest[digest].append(row["id"])
    # Compare chunk *content*, not indexes: duplicates can chunk at different boundaries.
    # A top-level job holds the original chunk text; split children share its index.
    chunk_digest = defaultdict(set)
    covered = defaultdict(set)
    for row in db.execute("""SELECT capture, chunk, text_artifact, state, id FROM extraction_jobs WHERE topic=?""", (topic,)):
        if row["id"].startswith("extract-part_"):
            continue
        digest, _ = _normalized_digest(store, row["text_artifact"])
        chunk_digest[(row["capture"], row["chunk"])].add(digest)
    # A job covers its text only if it, or (when split) its whole child tree, can still
    # yield items: queued/running/succeeded. Failed or deferred descendants break coverage.
    states = {r["id"]: (r["state"], r["capture"], r["chunk"]) for r in
              db.execute("SELECT id, state, capture, chunk FROM extraction_jobs WHERE topic=?", (topic,))}
    children = defaultdict(list)
    for row in db.execute("SELECT target, metadata FROM attempts WHERE stage='split_extraction' AND outcome='succeeded'"):
        if row["target"] in states:
            children[row["target"]] += [c["job_id"] for c in json.loads(row["metadata"]).get("children", [])]
    live = {}

    def is_live(job, depth=0):
        if job not in live:
            state = states.get(job, ("missing",))[0]
            if state in ("queued", "running", "succeeded"):
                live[job] = True
            elif state == "split" and depth < 50:
                live[job] = bool(children[job]) and all(is_live(c, depth + 1) for c in children[job])
            else:
                live[job] = False
        return live[job]
    for job, (state, capture, chunk) in states.items():
        if not job.startswith("extract-part_") and is_live(job):
            covered[capture] |= chunk_digest[(capture, chunk)]
    keeper = {}
    for digest, group in by_digest.items():
        group = sorted(set(group))
        # Prefer the capture covering the most chunk texts, then the most completed work.
        chosen = sorted(group, key=lambda c: (-len(covered[c]), -done.get(c, 0), c))[0]
        for capture in group:
            keeper[capture] = chosen
    decisions = {}
    for row in rows:
        info = captures[row["capture"]]
        if info["size"] < MIN_ALNUM_CHARS:
            decisions[row["id"]] = ("insufficient_text", {"capture": row["capture"], "alphanumeric_chars": info["size"]})
        elif info["source"] in datasets:
            decisions[row["id"]] = ("structured_dataset", {"capture": row["capture"], "source": info["source"]})
        elif (keeper.get(row["capture"], row["capture"]) != row["capture"]
              and chunk_digest[(row["capture"], row["chunk"])]
              and chunk_digest[(row["capture"], row["chunk"])] <= covered[keeper[row["capture"]]]):
            # Defer only chunks whose exact text the kept capture still covers.
            decisions[row["id"]] = ("duplicate_capture_text", {"capture": row["capture"], "kept_capture": keeper[row["capture"]],
                                                               "normalized_text_sha256": info["digest"]})
    return decisions


def apply(store, topic, decisions):
    with store.db:
        for job, (reason, evidence) in decisions.items():
            changed = store.db.execute("UPDATE extraction_jobs SET state='deferred',error=? WHERE id=? AND state='queued'",
                                       ("deferred:" + reason, job)).rowcount
            if changed:
                store.attempt("triage", job, "deferred", None, {"reason": reason, **evidence})


def restore(store, topic, reason=None):
    clause, args = ("AND error=?", (topic, "deferred:" + reason)) if reason else ("", (topic,))
    rows = [r[0] for r in store.db.execute(f"SELECT id FROM extraction_jobs WHERE topic=? AND state='deferred' {clause}", args)]
    with store.db:
        for job in rows:
            store.db.execute("UPDATE extraction_jobs SET state='queued',error=NULL WHERE id=?", (job,))
            store.attempt("triage", job, "restored", None, {"reason": reason})
    return len(rows)


def summarize(decisions):
    counts = defaultdict(int)
    for reason, _ in decisions.values():
        counts[reason] += 1
    return dict(counts)
