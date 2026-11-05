"""Command line: the same operations as the MCP tools, for ZH at a terminal.

    python3 -m tasktree tree [ROOT] [--all]
    python3 -m tasktree add "Title" [--parent P] [--id ID] [--type T] [--status S] [--next TEXT]
    python3 -m tasktree set ID status=doing next="email advisor"
    python3 -m tasktree note ID "text"
    python3 -m tasktree answer ID "option"
    python3 -m tasktree show ID | open | log [YYYY-MM-DD] | rebuild | export | serve
"""

from __future__ import annotations

import argparse
import json
import sys

from . import mcp_server, model, store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tasktree")
    ap.add_argument("--db", default=store.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tree"); t.add_argument("root", nargs="?"); t.add_argument("--all", action="store_true")
    a = sub.add_parser("add"); a.add_argument("title")
    for f in ("parent", "id", "type", "status", "owner", "next"):
        a.add_argument("--" + f)
    s = sub.add_parser("set"); s.add_argument("id"); s.add_argument("fields", nargs="+", help="key=value")
    n = sub.add_parser("note"); n.add_argument("id"); n.add_argument("text")
    w = sub.add_parser("answer"); w.add_argument("id"); w.add_argument("answer")
    sh = sub.add_parser("show"); sh.add_argument("id")
    sub.add_parser("open")
    lg = sub.add_parser("log"); lg.add_argument("date", nargs="?")
    sub.add_parser("rebuild")
    sub.add_parser("export")
    sub.add_parser("serve", help="run the MCP server on stdio")
    args = ap.parse_args(argv)

    srv = mcp_server.Server(args.db, source="cli")
    try:
        if args.cmd == "serve":
            srv.serve()
            return 0
        if args.cmd == "tree":
            out = srv.t_tree({"root": args.root, "include_done": args.all})
        elif args.cmd == "add":
            out = srv.t_add({k: v for k, v in vars(args).items() if k in
                             ("title", "parent", "id", "type", "status", "owner", "next") and v})
        elif args.cmd == "set":
            fields = {}
            for kv in args.fields:
                k, _, v = kv.partition("=")
                fields[k] = json.loads(v) if k in ("blocked_by", "links", "options", "order") else v
            out = srv.t_update(dict(fields, id=args.id))
        elif args.cmd == "note":
            out = srv.t_note({"id": args.id, "text": args.text})
        elif args.cmd == "answer":
            out = srv.t_answer({"id": args.id, "answer": args.answer})
        elif args.cmd == "show":
            out = srv.t_get({"id": args.id})
        elif args.cmd == "open":
            out = srv.t_open({})
        elif args.cmd == "log":
            out = srv.t_day({"date": args.date})
        elif args.cmd == "rebuild":
            out = f"replayed {store.rebuild(srv.conn)} events"
        else:
            out = srv.t_export({})
    except model.InvalidEvent as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
