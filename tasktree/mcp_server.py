"""MCP server over stdio (standard library only).

Each Claude session that has this server configured starts its own copy of this process.
All copies share one SQLite file, so they are several producers writing through
store.apply_event, whose BEGIN IMMEDIATE serializes them.

Protocol: newline-delimited JSON-RPC 2.0 (MCP stdio transport). Handled methods:
initialize, notifications/initialized, ping, tools/list, tools/call.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any, Callable

from . import model, store

SERVER_INFO = {"name": "tasktree", "version": "0.1.0"}
DEFAULT_PROTOCOL = "2025-06-18"

INSTRUCTIONS = (
    "ZH's personal task tree. Every piece of work is a node in one tree (workstream > project > task). "
    "At the start of substantial work, call `tree` (or `get_node`) to find where it belongs; add a node if "
    "it is new. When something changes, record it: update_node for status/next step, add_note for what "
    "happened, ask_decision when ZH must choose. Keep titles short, in the language ZH uses. Use stable, "
    "readable ids like 'job.x.prep'. Never put passwords, codes or account numbers in notes."
)

_str = {"type": "string"}
_strs = {"type": "array", "items": {"type": "string"}}
_status = {"type": "string", "enum": list(model.STATUSES)}
_type = {"type": "string", "enum": list(model.TYPES)}
_common = {
    "event_id": {"type": "string", "description": "Optional idempotency key; resending the same call with the same key is a no-op."},
    "source": {"type": "string", "description": "Who is writing, e.g. the session title."},
}
_links = {"type": "array", "items": {"type": "object", "properties": {"label": _str, "url": _str}}}


def _tool(name: str, desc: str, props: dict, required: list[str], write: bool = True) -> dict:
    p = dict(props, **_common) if write else props
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": p, "required": required, "additionalProperties": False}}


TOOLS = [
    _tool("tree", "Show the tree (or one branch) as indented text with rolled-up status: "
          "[?] needs ZH's decision, [!] blocked, [>] doing, [ ] waiting, [x] done. Done branches are hidden unless include_done.",
          {"root": _str, "include_done": {"type": "boolean"}, "max_depth": {"type": "integer"}}, [], write=False),
    _tool("get_node", "One node with its notes, history of events, and children ids.", {"id": _str}, ["id"], write=False),
    _tool("open_items", "Leaf nodes that are not done, grouped by status (decide, blocked, doing, waiting).",
          {"status": _status}, [], write=False),
    _tool("day_log", "Everything recorded on one day (YYYY-MM-DD, default today, local time), oldest first.",
          {"date": _str}, [], write=False),
    _tool("add_node", "Create a node. Omit parent for a new top-level workstream.",
          {"title": _str, "parent": _str, "id": _str, "type": _type, "status": _status, "owner": _str,
           "next": _str, "blocked_by": _strs, "links": _links, "order": {"type": "integer"}, "date": _str},
          ["title"]),
    _tool("update_node", "Change fields of a node (status, next step, title, owner, links, ...).",
          {"id": _str, "title": _str, "type": _type, "status": _status, "owner": _str, "next": _str,
           "blocked_by": _strs, "links": _links, "options": _strs, "order": {"type": "integer"}, "date": _str},
          ["id"]),
    _tool("add_note", "Append a dated note to a node (what happened, a result, a link).",
          {"id": _str, "text": _str}, ["id", "text"]),
    _tool("move_node", "Move a node under another parent (or to the top level with parent omitted).",
          {"id": _str, "parent": _str}, ["id"]),
    _tool("ask_decision", "Create a decision node that ZH must answer, with the options to choose from.",
          {"title": _str, "parent": _str, "options": _strs, "id": _str, "next": _str}, ["title", "parent", "options"]),
    _tool("answer_decision", "Record ZH's answer to a decision node (marks it done).",
          {"id": _str, "answer": _str}, ["id", "answer"]),
    _tool("archive_node", "Hide a node from views. Nothing is deleted; history stays in the log.",
          {"id": _str}, ["id"]),
    _tool("export_viewer", "JSON documents for the viewer page (one per node plus _meta). "
          "Write them to the viewer artifact's `nodes` collection.", {}, [], write=False),
]


def _new_id() -> str:
    return "n" + uuid.uuid4().hex[:8]


class Server:
    def __init__(self, db_path: str = store.DEFAULT_DB, source: str | None = None):
        self.conn = store.connect(db_path)
        self.source = source or os.environ.get("TASKTREE_SOURCE")
        self.handlers: dict[str, Callable[[dict], str]] = {
            "tree": self.t_tree, "get_node": self.t_get, "open_items": self.t_open, "day_log": self.t_day,
            "add_node": self.t_add, "update_node": self.t_update, "add_note": self.t_note,
            "move_node": self.t_move, "ask_decision": self.t_ask, "answer_decision": self.t_answer,
            "archive_node": self.t_archive, "export_viewer": self.t_export,
        }

    # ----- write tools: each builds one event and hands it to the store -----
    def _emit(self, etype: str, payload: dict, a: dict) -> str:
        ev = store.make_event(etype, payload, source=a.get("source") or self.source, event_id=a.get("event_id"))
        r = store.apply_event(self.conn, ev)
        return f"{'duplicate, already recorded' if r.duplicate else 'ok'}: {etype} {r.node_id}"

    def t_add(self, a):
        payload = {k: a[k] for k in ("title", "parent", "type", "status", "owner", "next", "blocked_by",
                                     "links", "order", "date") if a.get(k) is not None}
        payload["id"] = a.get("id") or _new_id()
        return self._emit("create_node", payload, a)

    def t_update(self, a):
        fields = {k: a[k] for k in model.UPDATABLE if k in a}
        return self._emit("update_node", {"id": a["id"], "fields": fields}, a)

    def t_note(self, a):
        return self._emit("add_note", {"id": a["id"], "text": a["text"]}, a)

    def t_move(self, a):
        return self._emit("move_node", {"id": a["id"], "parent": a.get("parent")}, a)

    def t_ask(self, a):
        payload = {"id": a.get("id") or _new_id(), "title": a["title"], "parent": a["parent"],
                   "type": "decision", "status": "decide", "options": a["options"]}
        if a.get("next"):
            payload["next"] = a["next"]
        return self._emit("create_node", payload, a)

    def t_answer(self, a):
        return self._emit("answer_decision", {"id": a["id"], "answer": a["answer"]}, a)

    def t_archive(self, a):
        return self._emit("archive_node", {"id": a["id"]}, a)

    # ----- read tools -----
    def t_tree(self, a):
        nodes = store.load_nodes(self.conn)
        if a.get("root") and a["root"] not in nodes:
            raise model.InvalidEvent(f"no node {a['root']}")
        return model.render(nodes, a.get("root"), bool(a.get("include_done")), int(a.get("max_depth") or 6))

    def t_get(self, a):
        d = store.node_detail(self.conn, a["id"])
        if d is None:
            raise model.InvalidEvent(f"no node {a['id']}")
        d["rolled_up_status"] = model.rollup(store.load_nodes(self.conn), a["id"]) if not d["archived"] else d["status"]
        return json.dumps(d, ensure_ascii=False, indent=1)

    def t_open(self, a):
        nodes = store.load_nodes(self.conn)
        leaves = [n for n in nodes.values() if not model.children(nodes, n["id"]) and n["status"] != "done"]
        want = [a["status"]] if a.get("status") else [s for s in model.STATUSES if s != "done"]
        out = []
        for s in want:
            group = [n for n in leaves if n["status"] == s]
            if group:
                out.append(f"## {s}")
                out += [f"- {n['title']} ({n['id']})" + (f" — next: {n['next']}" if n.get("next") else "") for n in group]
        return "\n".join(out) or "nothing open"

    def t_day(self, a):
        evs = store.events_on(self.conn, a.get("date"))
        if not evs:
            return "no events"
        return "\n".join(f"{d['ts']}  {d['text']}" + (f"  [{e['source']}]" if e.get("source") else "")
                         for e, d in ((e, store.describe(e)) for e in evs))

    def t_export(self, a):
        return json.dumps(store.export_for_viewer(self.conn), ensure_ascii=False)

    # ----- JSON-RPC plumbing -----
    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:          # notification: no reply
            return None
        try:
            if method == "initialize":
                version = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
                result: Any = {"protocolVersion": version, "capabilities": {"tools": {}},
                               "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                p = msg.get("params") or {}
                fn = self.handlers.get(p.get("name"))
                if fn is None:
                    return _error(mid, -32602, f"unknown tool {p.get('name')!r}")
                try:
                    result = {"content": [{"type": "text", "text": fn(p.get("arguments") or {})}]}
                except (model.InvalidEvent, KeyError, TypeError, ValueError) as e:
                    # Rule violations go back to Claude as a tool error it can read and fix.
                    result = {"content": [{"type": "text", "text": f"rejected: {e}"}], "isError": True}
            else:
                return _error(mid, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:  # never let one bad request kill the server
            return _error(mid, -32603, f"internal error: {e}")

    def serve(self, inp=sys.stdin, out=sys.stdout) -> None:
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply = _error(None, -32700, "parse error")
            else:
                reply = self.handle(msg) if isinstance(msg, dict) else _error(None, -32600, "invalid request")
            if reply is not None:
                out.write(json.dumps(reply, ensure_ascii=False) + "\n")
                out.flush()


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main() -> None:
    Server().serve()


if __name__ == "__main__":
    main()
