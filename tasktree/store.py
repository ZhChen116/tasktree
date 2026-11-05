"""SQLite store: the append-only event log and the node table derived from it.

`events` is the source of truth. `nodes` and `notes` are a materialized view: they are
written in the SAME transaction as the event that changes them, and `rebuild` can
recompute them from the log at any time. This is the only module that writes SQL.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from . import model

DEFAULT_DB = os.path.expanduser(os.environ.get("TASKTREE_DB", "~/.tasktree/tasktree.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,   -- replay order
    event_id  TEXT NOT NULL UNIQUE,                -- idempotency key
    ts        TEXT NOT NULL,                       -- local ISO time, with offset
    ts_epoch  REAL NOT NULL,                       -- for time-range queries
    type      TEXT NOT NULL,
    node_id   TEXT NOT NULL,
    payload   TEXT NOT NULL,                       -- JSON
    source    TEXT                                 -- which session / person sent it
);
CREATE INDEX IF NOT EXISTS events_by_time ON events(ts_epoch);
CREATE INDEX IF NOT EXISTS events_by_node ON events(node_id, seq);
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    parent      TEXT REFERENCES nodes(id),
    title       TEXT NOT NULL,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('decide','blocked','doing','waiting','done')),
    owner       TEXT,
    next        TEXT,
    blocked_by  TEXT NOT NULL DEFAULT '[]',
    links       TEXT NOT NULL DEFAULT '[]',
    options     TEXT NOT NULL DEFAULT '[]',
    answer      TEXT,
    ord         INTEGER NOT NULL DEFAULT 0,
    date        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS nodes_by_parent ON nodes(parent);
CREATE TABLE IF NOT EXISTS notes (
    note_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id   TEXT NOT NULL REFERENCES nodes(id),
    ts        TEXT NOT NULL,
    text      TEXT NOT NULL,
    event_id  TEXT NOT NULL UNIQUE REFERENCES events(event_id)
);
"""

_JSON_COLS = ("blocked_by", "links", "options")


def now_local() -> datetime:
    return datetime.now().astimezone()


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # isolation_level=None: we issue BEGIN/COMMIT ourselves.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # readers never block the writer
    conn.execute("PRAGMA busy_timeout=30000")     # several Claude sessions may write at once
    conn.executescript(SCHEMA)
    return conn


def _row_to_node(r: sqlite3.Row) -> dict:
    d = dict(r)
    for c in _JSON_COLS:
        d[c] = json.loads(d[c] or "[]")
    d["order"] = d.pop("ord")
    d["archived"] = bool(d["archived"])
    return d


def _get(conn, node_id: str) -> Optional[dict]:
    r = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return _row_to_node(r) if r else None


def _write(conn, change: model.Change, ts: str, event_id: str) -> None:
    n = change.upsert
    if n is not None:
        conn.execute(
            "INSERT INTO nodes(id, parent, title, type, status, owner, next, blocked_by, links, options, "
            "answer, ord, date, created_at, updated_at, archived) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET parent=excluded.parent, title=excluded.title, type=excluded.type, "
            "status=excluded.status, owner=excluded.owner, next=excluded.next, blocked_by=excluded.blocked_by, "
            "links=excluded.links, options=excluded.options, answer=excluded.answer, ord=excluded.ord, "
            "date=excluded.date, updated_at=excluded.updated_at, archived=excluded.archived",
            (n["id"], n.get("parent"), n["title"], n["type"], n["status"], n.get("owner"), n.get("next"),
             json.dumps(n.get("blocked_by") or [], ensure_ascii=False),
             json.dumps(n.get("links") or [], ensure_ascii=False),
             json.dumps(n.get("options") or [], ensure_ascii=False),
             n.get("answer"), n.get("order") or 0, n.get("date"), n["created_at"], n["updated_at"],
             int(bool(n.get("archived")))),
        )
    if change.note is not None:
        conn.execute("INSERT INTO notes(node_id, ts, text, event_id) VALUES (?,?,?,?)",
                     (change.note["node_id"], ts, change.note["text"], event_id))


@dataclass(frozen=True)
class Result:
    event_id: str
    node_id: str
    duplicate: bool


def make_event(etype: str, payload: dict, source: Optional[str] = None,
               event_id: Optional[str] = None, ts: Optional[datetime] = None) -> dict:
    ts = ts or now_local()
    return {"event_id": event_id or uuid.uuid4().hex, "type": etype, "payload": payload,
            "source": source, "ts": ts.isoformat(timespec="seconds"), "ts_epoch": ts.timestamp()}


def apply_event(conn: sqlite3.Connection, event: dict) -> Result:
    """Record one event and update the tree, atomically.

    Same shape as the Rewards Checker's process_order: BEGIN IMMEDIATE takes the write lock
    before anything is read, the idempotency insert is the first statement, the pure rule
    function decides, and every write commits together or not at all.
    """
    payload_json = json.dumps(event.get("payload") or {}, ensure_ascii=False, sort_keys=True)
    node_id = (event.get("payload") or {}).get("id")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(event_id, ts, ts_epoch, type, node_id, payload, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (event["event_id"], event["ts"], event["ts_epoch"], event.get("type"), str(node_id),
             payload_json, event.get("source")),
        )
        if cur.rowcount == 0:
            old = conn.execute("SELECT type, payload FROM events WHERE event_id = ?",
                               (event["event_id"],)).fetchone()
            if (old["type"], old["payload"]) != (event.get("type"), payload_json):
                raise model.InvalidEvent(f"event_id {event['event_id']} was already used for a different event")
            conn.execute("COMMIT")
            return Result(event["event_id"], str(node_id), duplicate=True)
        change = model.decide(event, lambda i: _get(conn, i), event["ts"])
        _write(conn, change, event["ts"], event["event_id"])
        conn.execute("COMMIT")
        return Result(event["event_id"], str(node_id), duplicate=False)
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def rebuild(conn: sqlite3.Connection) -> int:
    """Drop the derived tables and replay the whole log. Returns the number of events replayed."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM notes")
        conn.execute("DELETE FROM nodes")
        rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        for r in rows:
            ev = {"event_id": r["event_id"], "type": r["type"], "payload": json.loads(r["payload"]),
                  "ts": r["ts"]}
            _write(conn, model.decide(ev, lambda i: _get(conn, i), r["ts"]), r["ts"], r["event_id"])
        conn.execute("COMMIT")
        return len(rows)
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---------- read-only queries ----------

def load_nodes(conn, include_archived: bool = False) -> dict[str, dict]:
    sql = "SELECT * FROM nodes" + ("" if include_archived else " WHERE archived = 0")
    return {r["id"]: _row_to_node(r) for r in conn.execute(sql)}


def node_detail(conn, node_id: str) -> Optional[dict]:
    n = _get(conn, node_id)
    if n is None:
        return None
    n["notes"] = [dict(r) for r in conn.execute(
        "SELECT ts, text FROM notes WHERE node_id = ? ORDER BY note_id", (node_id,))]
    n["history"] = [dict(r) for r in conn.execute(
        "SELECT ts, type, payload, source FROM events WHERE node_id = ? ORDER BY seq", (node_id,))]
    n["children"] = [r["id"] for r in conn.execute(
        "SELECT id FROM nodes WHERE parent = ? AND archived = 0 ORDER BY ord, created_at", (node_id,))]
    return n


def events_on(conn, day: Optional[str] = None) -> list[dict]:
    """All events whose local date is `day` (YYYY-MM-DD, default today), oldest first."""
    d = datetime.strptime(day, "%Y-%m-%d") if day else now_local().replace(tzinfo=None)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0).astimezone()
    end = (start.replace(tzinfo=None) + timedelta(days=1)).astimezone()
    rows = conn.execute(
        "SELECT e.ts, e.type, e.node_id, e.payload, e.source, n.title FROM events e "
        "LEFT JOIN nodes n ON n.id = e.node_id WHERE e.ts_epoch >= ? AND e.ts_epoch < ? ORDER BY e.seq",
        (start.timestamp(), end.timestamp())).fetchall()
    return [dict(r) for r in rows]


def _mmdd(iso: Optional[str]) -> Optional[str]:
    return iso[5:10] if iso and len(iso) >= 10 else None


def trail_step(e: dict) -> dict | None:
    """One step of a node's own history, without the title (the viewer already shows it).
    Updates that change neither status nor next step are left out."""
    p = json.loads(e["payload"]) if isinstance(e.get("payload"), str) else (e.get("payload") or {})
    t, f = e["type"], p.get("fields") or {}
    if t == "create_node":
        kind, text = "create", "记下这件事"
    elif t == "add_note":
        kind, text = "note", p.get("text", "")
    elif t == "answer_decision":
        kind, text = "answer", f"决定：{p.get('answer')}"
    elif t == "update_node" and "status" in f:
        kind, text = f["status"], model.STATUS_LABEL.get(f["status"], f["status"]) + (f"；下一步：{f['next']}" if f.get("next") else "")
    elif t == "update_node" and f.get("next"):
        kind, text = "next", f"下一步改为：{f['next']}"
    elif t == "move_node":
        kind, text = "move", f"移到 {p.get('parent')}"
    else:
        return None
    return {"ts": e["ts"][5:10], "kind": kind, "text": text}


def export_for_viewer(conn, recent: int = 40, trail_max: int = 12) -> dict[str, dict]:
    """Documents for the viewer page: one per node (same fields as the original project-tree page)
    plus `_meta` with a snapshot time and the most recent events."""
    docs: dict[str, dict] = {}
    for n in load_nodes(conn).values():
        doc = {"parent": n["parent"], "title": n["title"], "type": n["type"], "status": n["status"],
               "order": n["order"], "date": _mmdd(n["date"]), "updated": _mmdd(n["updated_at"])}
        for k in ("owner", "next", "answer"):
            if n.get(k):
                doc[k] = n[k]
        for k in ("blocked_by", "links", "options"):
            if n.get(k):
                doc[k] = n[k]
        docs[n["id"]] = doc
    # Each node's own history, oldest first, so the viewer can draw a leaf's progress as a tree.
    for r in conn.execute("SELECT ts, type, node_id, payload FROM events ORDER BY seq"):
        if r["node_id"] in docs:
            step = trail_step(dict(r))
            if step:
                docs[r["node_id"]].setdefault("trail", []).append(step)
    for doc in docs.values():
        if len(doc.get("trail", [])) > trail_max:
            doc["trail"] = doc["trail"][:1] + doc["trail"][-(trail_max - 1):]
    rows = conn.execute(
        "SELECT e.ts, e.type, e.node_id, e.payload, n.title FROM events e LEFT JOIN nodes n ON n.id = e.node_id "
        "ORDER BY e.seq DESC LIMIT ?", (recent,)).fetchall()
    docs["_meta"] = {
        "snapshot": now_local().strftime("%m-%d %H:%M"),
        "recent": [describe(dict(r)) for r in rows],
    }
    return docs


def describe(e: dict) -> dict:
    """One-line human description of an event, for logs and the viewer."""
    p = json.loads(e["payload"]) if isinstance(e.get("payload"), str) else (e.get("payload") or {})
    title = e.get("title") or p.get("title") or e.get("node_id")
    t = e["type"]
    if t == "create_node":
        text = f"新建 {title}"
    elif t == "update_node":
        f = p.get("fields") or {}
        parts = [f"状态→{f['status']}" if "status" in f else None,
                 f"下一步：{f['next']}" if f.get("next") else None]
        parts = [x for x in parts if x] or ["更新 " + ", ".join(f)]
        text = f"{title}：" + "；".join(parts)
    elif t == "add_note":
        text = f"{title}：{p.get('text', '')}"
    elif t == "move_node":
        text = f"移动 {title} → {p.get('parent')}"
    elif t == "answer_decision":
        text = f"决定 {title}：{p.get('answer')}"
    else:
        text = f"归档 {title}"
    return {"ts": e["ts"][5:16].replace("T", " "), "node": e.get("node_id"), "text": text}
