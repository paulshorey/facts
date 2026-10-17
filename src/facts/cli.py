import argparse
import json
from .core import Store, search, capture_sources, fallback_extract, normalize, export, status, import_packet, repair_truncated


def main():
    parser = argparse.ArgumentParser(description="Source-preserving itemized research collector")
    parser.add_argument("command", choices=["init", "search", "capture", "fallback", "normalize", "repair-truncated", "export", "status", "import", "items", "sources", "organize", "series"])
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
    parser.add_argument("--max-cost-usd", type=float, default=10)
    parser.add_argument("--model")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--organized", action="store_true")
    parser.add_argument("--entity")
    parser.add_argument("--category")
    parser.add_argument("--group")
    parser.add_argument("--basis")
    parser.add_argument("--content-role")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--period")
    args = parser.parse_args()
    # Read-only organization commands must not instantiate the collector Store,
    # execute DDL or load/requeue topic definitions.
    if args.command == "organize":
        from .core import dumps
        from .organize import organize
        options = {"dry_run": args.dry_run, "pilot": args.pilot, "maximum": args.max_cost_usd,
                   "workers": args.workers or 2, "use_llm": not args.no_llm}
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
    store = Store()
    store.load_topics()
    if args.command == "search": search(store, args.topic, args.limit, args.workers or 4)
    elif args.command == "capture": capture_sources(store, args.topic, args.limit or 200, args.workers or 6)
    elif args.command == "fallback": fallback_extract(store, args.topic, args.limit or 100)
    elif args.command == "normalize": normalize(store, args.topic, args.limit or 100, args.workers or 2)
    elif args.command == "repair-truncated": repair_truncated(store, args.topic)
    elif args.command == "export": export(store, args.topic)
    elif args.command == "import": import_packet(store, args.topic, args.file)
    elif args.command == 'items':
        where=[];params=[]
        if args.topic!='all':where.append('topic=?');params.append(args.topic)
        if args.kind:where.append('kind=?');params.append(args.kind)
        if args.contains:where.append('payload LIKE ?');params.append('%'+args.contains+'%')
        sql='SELECT payload FROM items'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY rowid LIMIT ?'
        for row in store.db.execute(sql,params+[args.limit or 20]):print(row[0])
    elif args.command == 'sources':
        sql='SELECT DISTINCT s.* FROM sources s JOIN topic_sources t ON t.source=s.id'+(' WHERE t.topic=?' if args.topic!='all' else '')+' ORDER BY s.rowid LIMIT ?'
        for row in store.db.execute(sql,([args.topic] if args.topic!='all' else [])+[args.limit or 20]):print(json.dumps(dict(row)))
    else: print(json.dumps(status(store), indent=2))


if __name__ == "__main__":
    main()
