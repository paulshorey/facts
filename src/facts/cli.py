import argparse
import json
from .core import Store, search, capture_sources, fallback_extract, normalize, export, status, import_packet, repair_truncated


def main():
    parser = argparse.ArgumentParser(description="Source-preserving itemized research collector")
    parser.add_argument("command", choices=["init", "search", "capture", "fallback", "normalize", "repair-truncated", "export", "status", "import"])
    parser.add_argument("--topic", default="all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--file")
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
    else: print(json.dumps(status(store), indent=2))


if __name__ == "__main__":
    main()
