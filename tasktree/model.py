"""Pure rules of the task tree: validation, event -> change, status roll-up, text rendering.

Nothing here touches the database, so every rule can be tested with plain dicts.
The store (store.py) calls `decide` inside one transaction and writes what it returns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

STATUSES = ("decide", "blocked", "doing", "waiting", "done")
# Higher rank wins when rolling a parent's status up from its children.
RANK = {"decide": 5, "blocked": 4, "doing": 3, "waiting": 2, "done": 1}
TYPES = ("workstream", "project", "task", "decision", "deliverable", "job")
EVENT_TYPES = ("create_node", "update_node", "add_note", "move_node", "answer_decision", "archive_node")
UPDATABLE = ("title", "type", "status", "owner", "next", "blocked_by", "links", "options", "order", "date")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_DEPTH = 32


class InvalidEvent(ValueError):
    """The event is malformed or would break an invariant; it is rejected, never half-applied."""


@dataclass
class Change:
    """What applying one event does to the state. Produced by `decide`, written by the store."""

    upsert: Optional[dict] = None          # full node row to insert or replace
    note: Optional[dict] = None            # {"node_id", "text"}
    touched: list[str] = field(default_factory=list)


def _check_id(node_id) -> str:
    if not isinstance(node_id, str) or not ID_RE.match(node_id):
        raise InvalidEvent(f"bad node id {node_id!r} (letters, digits, . _ -; max 64)")
    return node_id


def _check_fields(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k not in UPDATABLE:
            raise InvalidEvent(f"field {k!r} cannot be set")
        if k == "status" and v not in STATUSES:
            raise InvalidEvent(f"status must be one of {STATUSES}, got {v!r}")
        if k == "type" and v not in TYPES:
            raise InvalidEvent(f"type must be one of {TYPES}, got {v!r}")
        if k == "title" and (not isinstance(v, str) or not v.strip()):
            raise InvalidEvent("title must be a non-empty string")
        if k in ("blocked_by", "links", "options") and v is not None and not isinstance(v, list):
            raise InvalidEvent(f"{k} must be a list")
        if k == "blocked_by" and v:
            for b in v:
                _check_id(b)
        if k == "order" and v is not None and (not isinstance(v, int) or isinstance(v, bool)):
            raise InvalidEvent("order must be an int")
        out[k] = v.strip() if k == "title" else v
    return out


def _is_ancestor(candidate: str, node_id: str, get: Callable[[str], Optional[dict]]) -> bool:
    """True if walking up from `candidate` reaches `node_id`, i.e. node_id is candidate or one of
    its ancestors. Moving node_id under such a candidate would create a cycle."""
    cur, steps = candidate, 0
    while cur is not None and steps <= MAX_DEPTH + 1:
        if cur == node_id:
            return True
        row = get(cur)
        cur = row.get("parent") if row else None
        steps += 1
    return False


def _depth(parent: Optional[str], get) -> int:
    d, cur = 0, parent
    while cur is not None:
        d += 1
        row = get(cur)
        cur = row.get("parent") if row else None
        if d > MAX_DEPTH:
            break
    return d


def decide(event: dict, get: Callable[[str], Optional[dict]], now: str) -> Change:
    """Turn one event into a Change, or raise InvalidEvent.

    `get(id)` returns the current node row (dict) or None. `now` is an ISO timestamp,
    injected so tests do not depend on the clock.
    """
    etype = event.get("type")
    if etype not in EVENT_TYPES:
        raise InvalidEvent(f"unknown event type {etype!r}")
    p = event.get("payload") or {}
    if not isinstance(p, dict):
        raise InvalidEvent("payload must be an object")
    node_id = _check_id(p.get("id"))
    current = get(node_id)

    if etype == "create_node":
        if current is not None:
            raise InvalidEvent(f"node {node_id} already exists")
        parent = p.get("parent")
        if parent is not None:
            _check_id(parent)
            if get(parent) is None:
                raise InvalidEvent(f"parent {parent} does not exist")
            if _depth(parent, get) >= MAX_DEPTH:
                raise InvalidEvent("tree too deep")
        fields = _check_fields({k: v for k, v in p.items() if k in UPDATABLE})
        if "title" not in fields:
            raise InvalidEvent("create_node needs a title")
        row = {
            "id": node_id, "parent": parent, "title": fields["title"],
            "type": fields.get("type", "task"), "status": fields.get("status", "waiting"),
            "owner": fields.get("owner"), "next": fields.get("next"),
            "blocked_by": fields.get("blocked_by") or [], "links": fields.get("links") or [],
            "options": fields.get("options") or [], "answer": None,
            "order": fields.get("order", 0), "date": fields.get("date") or now[:10],
            "created_at": now, "updated_at": now, "archived": False,
        }
        if row["type"] == "decision" and "status" not in fields:
            row["status"] = "decide"
        return Change(upsert=row, touched=[node_id])

    if current is None:
        raise InvalidEvent(f"node {node_id} does not exist")

    if etype == "update_node":
        fields = _check_fields(p.get("fields") or {})
        if not fields:
            raise InvalidEvent("update_node needs at least one field")
        row = dict(current, **fields, updated_at=now)
        return Change(upsert=row, touched=[node_id])

    if etype == "add_note":
        text = p.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidEvent("note text must be a non-empty string")
        return Change(upsert=dict(current, updated_at=now), note={"node_id": node_id, "text": text.strip()},
                      touched=[node_id])

    if etype == "move_node":
        parent = p.get("parent")
        if parent is not None:
            _check_id(parent)
            if get(parent) is None:
                raise InvalidEvent(f"parent {parent} does not exist")
            if _is_ancestor(parent, node_id, get):
                raise InvalidEvent("move would create a cycle")
        return Change(upsert=dict(current, parent=parent, updated_at=now), touched=[node_id])

    if etype == "answer_decision":
        answer = p.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise InvalidEvent("answer must be a non-empty string")
        opts = current.get("options") or []
        if opts and answer not in opts:
            raise InvalidEvent(f"answer must be one of {opts}")
        return Change(upsert=dict(current, answer=answer, status="done", updated_at=now), touched=[node_id])

    # archive_node: hidden from views, history kept
    return Change(upsert=dict(current, archived=True, updated_at=now), touched=[node_id])


# ---------- reading the tree ----------

def children(nodes: dict[str, dict], parent: Optional[str]) -> list[dict]:
    ks = [n for n in nodes.values() if n.get("parent") == parent and not n.get("archived")]
    return sorted(ks, key=lambda n: (n.get("order") or 0, n.get("created_at") or ""))


def rollup(nodes: dict[str, dict], node_id: str, _seen: Optional[set] = None) -> str:
    """A leaf shows its own status. A parent shows the most urgent status among its open
    children and its own status. When every child is done, the parent is done unless it is
    itself marked doing, blocked or decide (work continues beyond the listed children)."""
    seen = _seen if _seen is not None else set()
    n = nodes[node_id]
    if node_id in seen:
        return n.get("status", "waiting")
    seen.add(node_id)
    ks = children(nodes, node_id)
    if not ks:
        return n.get("status", "waiting")
    sts = [rollup(nodes, k["id"], seen) for k in ks]
    if all(s == "done" for s in sts):
        own = n.get("status")
        return own if own in ("doing", "blocked", "decide") else "done"
    best = max((s for s in sts if s != "done"), key=RANK.get)
    own = n.get("status")
    if own and own != "done" and RANK[own] > RANK[best]:
        best = own
    return best


MARK = {"decide": "[?]", "blocked": "[!]", "doing": "[>]", "waiting": "[ ]", "done": "[x]"}


def render(nodes: dict[str, dict], root: Optional[str] = None, include_done: bool = False,
           max_depth: int = 6) -> str:
    """Indented text tree for Claude and the terminal. Done branches are folded unless asked."""
    lines: list[str] = []

    def walk(n: dict, depth: int) -> None:
        st = rollup(nodes, n["id"])
        if st == "done" and not include_done and depth > 0:
            return
        extra = []
        if n.get("next") and st != "done":
            extra.append(f"next: {n['next']}")
        if n.get("owner"):
            extra.append(f"@{n['owner']}")
        if n.get("type") == "decision" and n.get("options") and not n.get("answer"):
            extra.append("options: " + " / ".join(n["options"]))
        tail = ("  — " + "; ".join(extra)) if extra else ""
        lines.append(f"{'  ' * depth}{MARK[st]} {n['title']} ({n['id']}){tail}")
        if depth < max_depth:
            for k in children(nodes, n["id"]):
                walk(k, depth + 1)

    roots = [nodes[root]] if root else children(nodes, None)
    for r in roots:
        walk(r, 0)
    return "\n".join(lines) if lines else "(empty tree)"
