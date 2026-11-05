"""Deterministic, reversible triage of the queued LLM extraction backlog.

Deferral never deletes jobs, captures or text. A deferred job keeps its chunk
and is excluded from dispatch until restored. Each decision is recorded as a
``triage`` attempt with its reason and evidence.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict

MIN_TEXT_CHARS = 200
REASONS = ("insufficient_text", "structured_dataset", "duplicate_capture_text")


def _normalized_digest(store, artifact):
    if not artifact:
        return None, 0
    text = store.read(artifact)
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest(), len(text)


def plan(store, topic):
    """Return {job_id: (reason, evidence)} for queued jobs worth deferring."""
    db = store.db
    rows = db.execute("""SELECT j.id, j.capture, c.source, c.text_artifact, c.acquired
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
    keeper = {}
    for digest, group in by_digest.items():
        chosen = sorted(set(group), key=lambda c: (-done.get(c, 0), c))[0]
        for capture in group:
            keeper[capture] = chosen
    decisions = {}
    for row in rows:
        info = captures[row["capture"]]
        if info["size"] < MIN_TEXT_CHARS:
            decisions[row["id"]] = ("insufficient_text", {"capture": row["capture"], "chars": info["size"]})
        elif info["source"] in datasets:
            decisions[row["id"]] = ("structured_dataset", {"capture": row["capture"], "source": info["source"]})
        elif keeper.get(row["capture"], row["capture"]) != row["capture"]:
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
