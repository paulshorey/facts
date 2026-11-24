import argparse
import json
from .runtime import exclusive_writer
from .core import Store, search, source_checks, observed_item, capture_sources, fallback_extract, normalize, export, status, import_packet, repair_truncated


def main():
    parser = argparse.ArgumentParser(description="Source-preserving itemized research collector")
    parser.add_argument("command", choices=["init", "search", "capture", "fallback", "normalize", "repair-truncated", "export", "status", "import", "items", "sources", "organize", "series", "triage"])
    parser.add_argument("--topic", default="all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--file")
    parser.add_argument("--kind")
    parser.add_argument("--contains")
    parser.add_argument("--view", choices=["observed", "organized", "grouped"], default="observed")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--incremental", action="store_true", help="Continue cached organization work (also the default)")
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--input-usd-per-million", type=float)
    parser.add_argument("--output-usd-per-million", type=float)
    parser.add_argument("--existing-only", action="store_true", help="Process only the existing extraction queue")
    parser.add_argument("--model")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--cached-only", action="store_true", help="Replay saved semantic/grouping output; prohibit provider submissions")
    parser.add_argument("--retry-failed", action="store_true", help="Explicitly retry terminal semantic jobs within the cost ceiling")
    parser.add_argument("--organized", action="store_true")
    parser.add_argument("--entity")
    parser.add_argument("--category")
    parser.add_argument("--group")
    parser.add_argument("--basis")
    parser.add_argument("--content-role")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--period")
    parser.add_argument("--apply", action="store_true", help="triage: defer the planned jobs (default is a dry run)")
    parser.add_argument("--restore", nargs="?", const="all", help="triage: return deferred jobs (optionally one reason) to the queue")
    args = parser.parse_args()
    if (args.dry_run or args.pilot or args.incremental or args.no_llm or args.model or args.cached_only) and args.command != "organize":
        parser.error("Organization processing options require the organize command")
    if args.retry_failed and args.command not in {"organize", "normalize"}:
        parser.error("--retry-failed requires organize or normalize")
    if args.command == "normalize" and (args.topic == "all" or args.limit <= 0 or args.max_cost_usd is None):
        parser.error("normalize requires one --topic, positive --limit and explicit --max-cost-usd")
    if (args.existing_only or args.input_usd_per_million is not None or args.output_usd_per_million is not None) and args.command != "normalize":
        parser.error("Extraction rates/--existing-only require normalize")
    if (args.apply or args.restore) and args.command != "triage":
        parser.error("--apply/--restore require triage")
    if args.apply and args.restore:
        parser.error("Choose --apply or --restore")
    if args.command == "triage" and args.topic == "all":
        parser.error("triage requires one --topic")
    if args.dry_run and args.pilot:
        parser.error("Choose --dry-run or --pilot")
    if args.organized and args.command != "export":
        parser.error("--organized requires export")
    if args.view != "observed" and args.command != "items":
        parser.error("--view requires items")
    # Read-only organization commands must not instantiate the collector Store,
    # execute DDL or load/requeue topic definitions.
    if args.command == "organize":
        from .core import dumps
        from .organize import organize
        options = {"dry_run": args.dry_run, "pilot": args.pilot, "maximum": 10 if args.max_cost_usd is None else args.max_cost_usd,
                   "workers": args.workers or 2, "use_llm": not args.no_llm, "retry_failed": args.retry_failed, "cached_only": args.cached_only}
        if args.model:
            options["model"] = args.model
        print(dumps(organize(topic=args.topic, **options))); return
    if args.command == "items" and args.view != "observed":
        from .core import dumps
        from .organize_store import OrganizedStore
        store = OrganizedStore(readonly=True)
        for value in store.query(args.topic, args.view, args.limit or 20, args.contains, args.entity, args.category, args.group, args.kind, args.basis, args.content_role, args.date_from, args.date_to):
            print(dumps(value))
        return
    if args.command == "export" and args.organized:
        from .organize_export import export_organized
        print(json.dumps(export_organized(topic=args.topic), indent=2)); return
    if args.command == "series":
        from .core import dumps
        from .organize_export import query_series
        for value in query_series(topic=args.topic, period=args.period, basis=args.basis, limit=args.limit or 20):
            print(dumps(value))
        return
    if args.command == "triage":
        return triage_command(args)
    return collector_command(args)


def triage_command(args):
    from . import triage
    from .core import ROOT
    if not (args.apply or args.restore):
        # A preview must not load topic files, create searches or run DDL.
        reader = triage.CatalogReader(ROOT)
        try:
            decisions = triage.plan(reader, args.topic)
        finally:
            reader.db.close()
        print(json.dumps({"topic": args.topic, "applied": False, "deferred": triage.summarize(decisions)})); return
    return triage_write(args)


@exclusive_writer
def triage_write(args):
    from . import triage
    store = Store()
    if args.restore:
        print(json.dumps({"restored": triage.restore(store, args.topic, None if args.restore == "all" else args.restore)})); return
    decisions = triage.plan(store, args.topic)
    triage.apply(store, args.topic, decisions)
    print(json.dumps({"topic": args.topic, "applied": True, "deferred": triage.summarize(decisions)}))


@exclusive_writer
def collector_command(args):
    store = Store()
    store.load_topics()
    if args.command == "search": search(store, args.topic, args.limit, args.workers or 4)
    elif args.command == "capture": capture_sources(store, args.topic, args.limit or 200, args.workers or 6)
    elif args.command == "fallback": fallback_extract(store, args.topic, args.limit or 100)
    elif args.command == "normalize":
        print(json.dumps(normalize(store, args.topic, args.limit, args.workers or 2,
            maximum=args.max_cost_usd, input_rate=args.input_usd_per_million,
            output_rate=args.output_usd_per_million, retry_failed=args.retry_failed, existing_only=args.existing_only)))
    elif args.command == "repair-truncated": repair_truncated(store, args.topic)
    elif args.command == "export": export(store, args.topic)
    elif args.command == "import": import_packet(store, args.topic, args.file)
    elif args.command == 'items':
        where=[];params=[]
        if args.topic!='all':where.append('topic=?');params.append(args.topic)
        if args.kind:where.append('kind=?');params.append(args.kind)
        if args.contains:where.append('payload LIKE ?');params.append('%'+args.contains+'%')
        sql='SELECT payload FROM items'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY rowid LIMIT ?'
        checks = source_checks(store.db, args.topic)
        for raw, in store.db.execute(sql, params+[args.limit or 20]):
            print(json.dumps(observed_item(raw, checks), ensure_ascii=False))
    elif args.command == 'sources':
        sql='SELECT DISTINCT s.* FROM sources s JOIN topic_sources t ON t.source=s.id'+(' WHERE t.topic=?' if args.topic!='all' else '')+' ORDER BY s.rowid LIMIT ?'
        for row in store.db.execute(sql,([args.topic] if args.topic!='all' else [])+[args.limit or 20]):print(json.dumps(dict(row)))
    else: print(json.dumps(status(store), indent=2))


if __name__ == "__main__":
    main()
