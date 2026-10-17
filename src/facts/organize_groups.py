"""Reversible grouping of observations; source observations are never merged."""
from __future__ import annotations

import json
import copy
import re
from collections import defaultdict
from collections import Counter

from .contracts import VERSION
from .core import dumps, ident
from .organize_llm import enqueue, run_jobs
from .organize_store import name_key

STOP = set("the a an and or of for to in on by with at from as is was were are be has have had that this it its his her their said says reported report according trump donald president family us federal government million billion dollars".split())
ACTIONS = ["invest", "contract", "pardon", "lawsuit", "settlement", "deni", "purchas", "tariff", "announce", "dinner", "donat", "loan", "disclos", "appoint", "payment", "trade", "fundrais", "sold", "acqui"]


def tokens(record):
    return set(re.findall(r"\w+", record.get("title", "").casefold()))-STOP


def compatible(a, b):
    """Reject different occurrences mechanically before interpreting similarity."""
    ea = {e.get("entity_id") or name_key(e["name"]) for e in a.get("entities", [])}
    eb = {e.get("entity_id") or name_key(e["name"]) for e in b.get("entities", [])}
    if ea and eb and not ea.intersection(eb):
        return False
    def dates(record):
        return [d for d in record.get("dates", []) if d.get("start") and d.get("role") not in {"publication", "report", "acquisition", "unknown", "reporting_period"}]
    da, db = dates(a), dates(b)
    if da and db and not any(x["start"] <= y["end"] and y["start"] <= x["end"] for x in da for y in db):
        return False
    ta, tb = tokens(a), tokens(b)
    if len(ta.intersection(tb)) < 2:
        return False
    aa = {word for word in ACTIONS if any(t.startswith(word) for t in ta)}
    ab = {word for word in ACTIONS if any(t.startswith(word) for t in tb)}
    if aa and ab and not aa.intersection(ab):
        return False
    return len(ta.intersection(tb))/max(1, min(len(ta), len(tb))) >= 0.45


def brief(value):
    return {k: value.get(k) for k in ["id", "title", "kind", "subtype", "dates", "entities"]} | {"detail": value.get("detail", "")[:2000], "version": value["processing"]["version"]}


def apply_decisions(store, job, payload, parsed, artifact):
    entries = {e["id"]: e for e in payload["entries"]}
    counts = Counter(r.input_id for r in parsed.results)
    for result in parsed.results:
        if result.input_id not in entries or counts[result.input_id] != 1:
            continue  # Keep unmatched/ambiguous observations as their own groups.
        entry = entries[result.input_id]
        if result.relation == "no_match":
            continue
        candidate = next((c for c in entry["candidates"] if c["id"] == result.candidate_id), None)
        if candidate is None:
            continue
        relation = result.relation
        if result.relation == "same_occurrence" and not compatible(entry, candidate):
            relation = "related_subject"
        store.db.execute("INSERT OR REPLACE INTO organized_relations VALUES (?,?,?,?,?,?)", (entry["id"], entry["version"], candidate["id"], candidate["version"], relation, dumps({"method": "structured_llm", "job_id": job["id"], "response_artifact": artifact, "applied_input_artifact": store.put(payload), "requested_relation": result.relation, "applied_relation": relation})))


def exact_identity(value):
    entities = sorted({e.get("entity_id") or name_key(e["name"]) for e in value.get("entities", [])})
    return (value["topic"], name_key(value["title"]), name_key(value.get("detail", "")),
            dumps(value.get("dates", [])), dumps(value.get("measurements", [])), tuple(entities))


def build_groups(store, topics, inputs, budget, model, workers, use_llm):
    # Also safe when an older running organizer imports this stage after upgrade.
    store.db.execute("CREATE TABLE IF NOT EXISTS organized_group_history (id TEXT NOT NULL,version TEXT NOT NULL,topic TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,version))")
    selected = {p["id"] for p in inputs}
    observations = {}
    for row in store.db.execute("SELECT payload FROM organized_records WHERE active=1 AND record_type='observation' AND content_role='substantive' ORDER BY id"):
        value = json.loads(row[0])
        if value["topic"] in topics:
            observations[value["id"]] = value
    exact, entries = {}, defaultdict(list)
    for iid, value in observations.items():
        # Identical descriptions can be grouped without asking the model to
        # rediscover an extraction duplicate; distinct source records remain.
        key = exact_identity(value)
        if key in exact:
            target = observations[exact[key]]
            store.db.execute("INSERT OR IGNORE INTO organized_relations VALUES (?,?,?,?,?,?)", (iid, value["processing"]["version"], target["id"], target["processing"]["version"], "same_occurrence", dumps({"method": "identical_description"})))
            continue
        exact[key] = iid
        if not selected.intersection(value.get("input_ids", [])):
            continue
        terms = sorted(tokens(value), key=lambda x: (-len(x), x))[:9]
        if len(terms) < 2:
            continue
        expression = " OR ".join('"'+word+'"' for word in terms)
        matches = store.db.execute("SELECT record_id FROM organized_search WHERE organized_search MATCH ? AND topic=? ORDER BY rank LIMIT 30", (expression, value["topic"])).fetchall()
        candidates = [observations[r[0]] for r in matches if r[0] in observations and r[0] < iid and compatible(value, observations[r[0]])][:5]
        if candidates:
            entries[value["topic"]].append({**brief(value), "candidates": [brief(c) for c in candidates]})
    ids = set()
    def fingerprint(payload, config):
        value = copy.deepcopy(payload)
        for e in value["entries"]:
            e.pop("version", None)
            for c in e.get("candidates", []):
                c.pop("version", None)
        return ident("group-context", value, {k:v for k,v in config.items() if k not in {"input_usd_per_million","output_usd_per_million","pricing_basis"}})
    cached = {}
    for previous in store.db.execute("SELECT * FROM organized_jobs WHERE stage='grouping' AND response_artifact IS NOT NULL ORDER BY updated"):
        from .contracts import GroupBatch
        try:
            response = json.loads(store.read(previous["response_artifact"]))
            choice = response["raw"]["choices"][0]
            if choice.get("finish_reason") == "length":
                continue
            GroupBatch.model_validate_json(choice["message"]["content"])
            previous_payload = json.loads(store.read(previous["input_artifact"]))
            cached[fingerprint(previous_payload,json.loads(previous["settings"]))] = dict(previous)
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    def submit_grouping(topic, batch):
        from .contracts import GroupBatch
        payload = {"entries": batch}
        jid = enqueue(store, topic, "grouping", payload, model)
        ids.add(jid)
        previous = store.db.execute("SELECT * FROM organized_jobs WHERE id=?", (jid,)).fetchone()
        source = cached.get(fingerprint(payload,json.loads(previous["settings"])))
        if previous["state"] == "queued" and source:
            store.db.execute("UPDATE organized_jobs SET state='succeeded',response_artifact=?,error=NULL WHERE id=?", (source["response_artifact"], jid))
            store.original.attempt("organize_grouping_cache", jid, "cached", source["response_artifact"], {"from_job":source["id"],"applied_input_artifact":store.put(payload)})
            previous = store.db.execute("SELECT * FROM organized_jobs WHERE id=?", (jid,)).fetchone()
        if previous["state"] == "succeeded" and previous["response_artifact"]:
            raw = json.loads(store.read(previous["response_artifact"]))
            parsed = GroupBatch.model_validate_json(raw["raw"]["choices"][0]["message"]["content"])
            # Processing-version changes alone do not invalidate a semantic
            # match. Apply the cached choice to the current compatible versions.
            apply_decisions(store, previous, payload, parsed, previous["response_artifact"])
    if use_llm:
        for topic, values in entries.items():
            batch, length = [], 0
            for value in values:
                size = len(dumps(value))
                if batch and (len(batch) >= 10 or length+size > 28000):
                    submit_grouping(topic, batch); batch, length = [], 0
                batch.append(value); length += size
            if batch:
                submit_grouping(topic, batch)
        store.db.commit()
        result = run_jobs(store, topics, "grouping", budget, lambda j, p, r, a: apply_decisions(store, j, p, r, a), workers, ids)
    else:
        result = {"state": "deterministic_only"}
    # Rebuild derived memberships from the current record versions and reusable
    # relations. Ignore stale edges after any normalization version changes.
    for topic in topics:
        store.db.execute("DELETE FROM organized_memberships WHERE group_id IN (SELECT id FROM organized_groups WHERE topic=?)", (topic,))
        store.db.execute("DELETE FROM organized_groups WHERE topic=?", (topic,))
    roots = {key: key for key in observations}
    def root(key):
        while roots[key] != key:
            key = roots[key]
        return key
    related = []
    for row in store.db.execute("SELECT * FROM organized_relations ORDER BY record_id,target_id"):
        a, b = row["record_id"], row["target_id"]
        if a not in observations or b not in observations:
            continue
        if observations[a]["processing"]["version"] != row["version"] or observations[b]["processing"]["version"] != row["target_version"]:
            continue
        if row["relation"] == "same_occurrence":
            ra, rb = root(a), root(b)
            # Match representatives too; do not let a chain of weak similarities
            # unite occurrences with incompatible dates/actions.
            exact_match = json.loads(row["payload"]).get("method") == "identical_description"
            can_join = (exact_identity(observations[ra]) == exact_identity(observations[rb])
                        if exact_match else compatible(observations[ra], observations[rb]))
            if ra != rb and can_join:
                roots[max(ra, rb)] = min(ra, rb)
        else:
            related.append((a, b))
    members = defaultdict(list)
    for iid in observations:
        members[root(iid)].append(iid)
    def save_group(payload):
        payload["member_versions"] = {iid: observations[iid]["processing"]["version"] for iid in payload["member_ids"]}
        payload["version"] = ident("group-version", payload)
        store.db.execute("INSERT OR IGNORE INTO organized_group_history VALUES (?,?,?,?)", (payload["id"],payload["version"],payload["topic"],dumps(payload)))
        store.db.execute("INSERT INTO organized_groups VALUES (?,?,?,?)", (payload["id"],payload["topic"],payload["group_kind"],dumps(payload)))
    for anchor, group_members in members.items():
        value = observations[anchor]
        gid = ident("group", value["topic"], "occurrence", anchor)
        payload = {"schema_version": VERSION, "id": gid, "topic": value["topic"], "group_kind": "occurrence",
                   "title": value["title"], "anchor_id": anchor, "member_ids": sorted(group_members),
                   "observation_count": len(group_members), "source_url_count": len({r.get("url") for m in group_members for r in observations[m].get("references", []) if r.get("url")}),
                   "related_observation_ids": sorted({b if a in group_members else a for a, b in related if a in group_members or b in group_members})}
        save_group(payload)
        store.db.executemany("INSERT INTO organized_memberships VALUES (?,?,?)", [(gid, m, "same_occurrence") for m in group_members])
    cases = defaultdict(list)
    for iid, value in observations.items():
        for category in value.get("categories", []):
            cases[(value["topic"], category)].append(iid)
    for (topic, category), group_members in cases.items():
        gid = ident("group", topic, "subject", category)
        payload = {"schema_version": VERSION, "id": gid, "topic": topic, "group_kind": "subject", "title": category.replace("-", " "), "category": category, "member_ids": sorted(group_members), "observation_count": len(group_members)}
        save_group(payload)
        store.db.executemany("INSERT INTO organized_memberships VALUES (?,?,?)", [(gid, m, "subject") for m in group_members])
    comparisons = defaultdict(set)
    for iid, value in observations.items():
        for m in value.get("measurements", []):
            if m.get("period") and m.get("unit") and m.get("metric") not in {None, "unspecified"}:
                key = (value["topic"], name_key(m["metric"]), m["period"], m["unit"], m.get("basis"),
                       tuple(sorted(name_key(e["name"]) for e in value.get("entities", []))))
                comparisons[key].add(iid)
    comparison_count = 0
    for key, group_members in comparisons.items():
        if len(group_members) < 2:
            continue
        gid = ident("group", "comparison", key)
        payload = {"schema_version": VERSION, "id": gid, "topic": key[0], "group_kind": "comparison",
                   "title": key[1]+" ("+key[2]+")", "member_ids": sorted(group_members), "observation_count": len(group_members)}
        save_group(payload)
        store.db.executemany("INSERT INTO organized_memberships VALUES (?,?,?)", [(gid, m, "comparison") for m in group_members])
        comparison_count += 1
    store.db.commit()
    return {**result, "occurrence_groups": len(members), "subject_groups": len(cases),
            "comparison_groups": comparison_count,
            "grouped_observations": sum(len(v) for v in members.values()),
            "multiple_observation_groups": sum(len(v) > 1 for v in members.values())}
