import argparse
import json
from .core import Store, search, capture_sources, fallback_extract, normalize, export, status, import_packet, repair_truncated


def main():
    parser = argparse.ArgumentParser(description="Source-preserving itemized research collector")
    parser.add_argument("command", choices=["init", "search", "capture", "fallback", "normalize", "repair-truncated", "export", "status", "import", "items", "sources"])
    parser.add_argument("--topic", default="all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--file")
    parser.add_argument("--kind")
    parser.add_argument("--contains")
    args = parser.parse_args()
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
