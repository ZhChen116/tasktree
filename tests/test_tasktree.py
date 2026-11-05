"""Tests derived from the invariants in DESIGN.md:
I1 an event is applied at most once; I2 the node table always equals a replay of the log;
I3 the tree has no cycles and every parent exists; I4 a rejected event changes nothing;
I5 concurrent writers lose no updates.
"""

import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

from tasktree import model, store
from tasktree.mcp_server import Server

T0 = datetime(2026, 11, 4, 9, 0, tzinfo=timezone(timedelta(hours=-6)))


def ev(etype, payload, eid=None, minutes=0, source=None):
    return store.make_event(etype, payload, source=source, event_id=eid, ts=T0 + timedelta(minutes=minutes))


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def seed(conn):
    store.apply_event(conn, ev("create_node", {"id": "paper", "title": "论文", "type": "workstream"}))
    store.apply_event(conn, ev("create_node", {"id": "paper.a", "title": "Paper A", "parent": "paper", "type": "project"}, minutes=1))
    store.apply_event(conn, ev("create_node", {"id": "paper.a.submit", "title": "投稿", "parent": "paper.a", "status": "doing"}, minutes=2))
    store.apply_event(conn, ev("create_node", {"id": "paper.a.code", "title": "代码修复", "parent": "paper.a", "status": "done"}, minutes=3))


# ---------- pure rules ----------

def test_rollup_parent_takes_most_urgent_open_child():
    nodes = {
        "a": {"id": "a", "parent": None, "status": "waiting"},
        "b": {"id": "b", "parent": "a", "status": "done"},
        "c": {"id": "c", "parent": "a", "status": "doing"},
        "d": {"id": "d", "parent": "a", "status": "decide"},
    }
    assert model.rollup(nodes, "a") == "decide"
    nodes["d"]["status"] = "done"
    assert model.rollup(nodes, "a") == "doing"
    nodes["c"]["status"] = "done"
    assert model.rollup(nodes, "a") == "done"   # all children done, own status only "waiting" -> done
    nodes["a"]["status"] = "doing"
    assert model.rollup(nodes, "a") == "doing"  # parent explicitly still in progress (more work not yet split out)


def test_decide_rejects_bad_events():
    get = {"x": {"id": "x", "parent": None}}.get
    for bad in [
        {"type": "explode", "payload": {"id": "x"}},
        {"type": "create_node", "payload": {"id": "has space", "title": "t"}},
        {"type": "create_node", "payload": {"id": "y"}},                              # no title
        {"type": "create_node", "payload": {"id": "y", "title": "t", "parent": "nope"}},
        {"type": "create_node", "payload": {"id": "x", "title": "dup"}},
        {"type": "update_node", "payload": {"id": "x", "fields": {"status": "finished"}}},
        {"type": "update_node", "payload": {"id": "x", "fields": {"color": "red"}}},
        {"type": "add_note", "payload": {"id": "x", "text": "  "}},
        {"type": "update_node", "payload": {"id": "ghost", "fields": {"status": "done"}}},
    ]:
        with pytest.raises(model.InvalidEvent):
            model.decide(bad, get, "2026-11-04T09:00:00-06:00")


# ---------- I1 idempotency ----------

def test_same_event_twice_is_applied_once(conn):
    seed(conn)
    e = ev("add_note", {"id": "paper.a", "text": "advisor: submit Friday"}, eid="note-1", minutes=5)
    assert store.apply_event(conn, e).duplicate is False
    assert store.apply_event(conn, e).duplicate is True
    assert len(store.node_detail(conn, "paper.a")["notes"]) == 1


def test_reusing_an_event_id_for_different_content_is_rejected(conn):
    seed(conn)
    store.apply_event(conn, ev("add_note", {"id": "paper.a", "text": "a"}, eid="k"))
    with pytest.raises(model.InvalidEvent):
        store.apply_event(conn, ev("add_note", {"id": "paper.a", "text": "b"}, eid="k"))


# ---------- I3 tree shape, I4 rejected changes nothing ----------

def test_move_cannot_create_a_cycle_and_leaves_state_unchanged(conn):
    seed(conn)
    before = store.load_nodes(conn)
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(model.InvalidEvent):
        store.apply_event(conn, ev("move_node", {"id": "paper", "parent": "paper.a.submit"}))
    assert store.load_nodes(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == n_events  # rejected event not logged


def test_decision_answer_must_be_an_option_and_marks_done(conn):
    seed(conn)
    store.apply_event(conn, ev("create_node", {"id": "dec.award", "title": "报学生论文奖?", "parent": "paper.a",
                                               "type": "decision", "options": ["报", "不报"]}))
    assert store.load_nodes(conn)["dec.award"]["status"] == "decide"
    assert model.rollup(store.load_nodes(conn), "paper") == "decide"
    with pytest.raises(model.InvalidEvent):
        store.apply_event(conn, ev("answer_decision", {"id": "dec.award", "answer": "maybe"}))
    store.apply_event(conn, ev("answer_decision", {"id": "dec.award", "answer": "报"}))
    n = store.load_nodes(conn)["dec.award"]
    assert (n["status"], n["answer"]) == ("done", "报")


# ---------- I2 the view equals a replay of the log ----------

def test_rebuild_from_log_reproduces_the_same_tree(conn):
    seed(conn)
    store.apply_event(conn, ev("update_node", {"id": "paper.a.submit", "fields": {"status": "done"}}, minutes=9))
    store.apply_event(conn, ev("add_note", {"id": "paper.a", "text": "submitted"}, minutes=10))
    store.apply_event(conn, ev("archive_node", {"id": "paper.a.code"}, minutes=11))
    before = (store.load_nodes(conn, include_archived=True),
              [dict(r) for r in conn.execute("SELECT node_id, ts, text FROM notes ORDER BY note_id")])
    assert store.rebuild(conn) == 7
    after = (store.load_nodes(conn, include_archived=True),
             [dict(r) for r in conn.execute("SELECT node_id, ts, text FROM notes ORDER BY note_id")])
    assert after == before


# ---------- I5 concurrent writers ----------

def test_concurrent_sessions_lose_no_notes(tmp_path):
    path = str(tmp_path / "c.db")
    c0 = store.connect(path)
    seed(c0)
    errors = []

    def session(k):
        c = store.connect(path)   # each "Claude session" has its own connection, like separate MCP processes
        try:
            for i in range(25):
                store.apply_event(c, ev("add_note", {"id": "paper.a", "text": f"s{k}-{i}"}))
        except Exception as e:  # pragma: no cover
            errors.append(e)
        finally:
            c.close()

    ts = [threading.Thread(target=session, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    assert len(store.node_detail(c0, "paper.a")["notes"]) == 200
    c0.close()


def test_concurrent_updates_to_different_fields_both_survive(tmp_path):
    """Lost update check: one session keeps changing status, another keeps changing next.
    Each update is read-modify-write of the whole row, so without the write lock taken
    before the read, one session would overwrite the other's field with a stale value."""
    path = str(tmp_path / "u.db")
    c0 = store.connect(path)
    seed(c0)
    statuses = ["doing", "waiting", "blocked"] * 20 + ["doing"]

    def a():
        c = store.connect(path)
        for s in statuses:
            store.apply_event(c, ev("update_node", {"id": "paper.a.submit", "fields": {"status": s}}))
        c.close()

    def b():
        c = store.connect(path)
        for i in range(61):
            store.apply_event(c, ev("update_node", {"id": "paper.a.submit", "fields": {"next": f"step {i}"}}))
        c.close()

    ts = [threading.Thread(target=a), threading.Thread(target=b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    n = store.load_nodes(c0)["paper.a.submit"]
    assert (n["status"], n["next"]) == ("doing", "step 60")
    store.rebuild(c0)
    assert store.load_nodes(c0)["paper.a.submit"] == n
    c0.close()


# ---------- reading ----------

def test_day_log_uses_local_dates(conn):
    seed(conn)
    store.apply_event(conn, ev("add_note", {"id": "paper", "text": "next day"}, minutes=24 * 60))
    assert len(store.events_on(conn, "2026-11-04")) == 4
    assert [e["type"] for e in store.events_on(conn, "2026-11-05")] == ["add_note"]


def test_render_hides_done_branches_by_default(conn):
    seed(conn)
    text = model.render(store.load_nodes(conn))
    assert "[>] 论文 (paper)" in text and "投稿" in text
    assert "代码修复" not in text
    assert "代码修复" in model.render(store.load_nodes(conn), include_done=True)


def test_export_matches_viewer_fields(conn):
    seed(conn)
    docs = store.export_for_viewer(conn)
    assert docs["paper.a.submit"]["parent"] == "paper.a"
    assert docs["paper.a.submit"]["date"] == "11-04"
    assert docs["_meta"]["recent"][0]["text"].startswith("新建")


# ---------- MCP over stdio ----------

def test_mcp_handshake_and_tool_call(tmp_path):
    db = str(tmp_path / "m.db")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add_node", "arguments": {"id": "job", "title": "求职", "type": "workstream"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "add_node", "arguments": {"id": "job.x", "title": "x", "parent": "missing"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "tree", "arguments": {}}},
    ]
    p = subprocess.run([sys.executable, "-m", "tasktree", "--db", db, "serve"],
                       input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, timeout=30)
    replies = {r["id"]: r for r in map(json.loads, p.stdout.splitlines())}
    assert set(replies) == {1, 2, 3, 4, 5}                       # the notification got no reply
    assert replies[1]["result"]["serverInfo"]["name"] == "tasktree"
    assert "add_node" in {t["name"] for t in replies[2]["result"]["tools"]}
    assert replies[3]["result"]["content"][0]["text"].startswith("ok")
    assert replies[4]["result"]["isError"] is True               # rule violation is a readable tool error
    assert "求职 (job)" in replies[5]["result"]["content"][0]["text"]


def test_server_survives_garbage_input(tmp_path):
    s = Server(str(tmp_path / "g.db"))
    import io
    out = io.StringIO()
    s.serve(io.StringIO('not json\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n'), out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32700 and lines[1]["result"] == {}


def test_export_carries_each_nodes_own_history(conn):
    seed(conn)
    store.apply_event(conn, ev("add_note", {"id": "paper.a.submit", "text": "draft sent"}, minutes=5))
    store.apply_event(conn, ev("update_node", {"id": "paper.a.submit", "fields": {"owner": "me"}}, minutes=6))
    store.apply_event(conn, ev("update_node", {"id": "paper.a.submit", "fields": {"status": "done"}}, minutes=7))
    trail = store.export_for_viewer(conn)["paper.a.submit"]["trail"]
    assert [t["kind"] for t in trail] == ["create", "note", "done"]   # the owner-only update is left out
    assert trail[1] == {"ts": "11-04", "kind": "note", "text": "draft sent"}
