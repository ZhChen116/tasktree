# TaskTree design notes

This document walks through the 8 questions of a small backend system design checklist to explain why TaskTree is built the way it is. It has the same skeleton as the classic "rewards checker" exercise: **a stream of events comes in → each is applied in order → state in a database is updated → someone queries it**. The differences are that the state here is a tree, and the producers are several Claude sessions running at the same time.

## 0. A concrete example first

Suppose these things happen across a few sessions in one day (simplified):

| Time | Written by | Event | Change to the tree |
|---|---|---|---|
| 10:36 | Paper A session | `update_node(paper.a.table, status=done)` | The supplementary table is done |
| 12:37 | Paper A session | `add_note(paper.a, "code fix uploaded")` | Paper A gets a timestamped note |
| 14:10 | Paper B session | `ask_decision(paper.b.run, options=[start, wait])` | The Paper B branch now "needs a decision" |
| 14:10 | Paper B session (network hiccup, retry) | The same `event_id` is sent again | Nothing changes; the call returns duplicate |
| 21:40 | Hub session | `answer_decision(paper.b.run, "start")` | The decision is done; Paper B is back to "doing" |

"What did I do today?" is then the list of that day's events in time order. "Where am I stuck?" is the open leaves of the tree, grouped by status.

## 1. Q1: What are the events, and where do they come from?

There are six event types: `create_node`, `update_node`, `add_note`, `move_node`, `answer_decision` and `archive_node`. Each event has:

- `event_id`: the idempotency key. A caller can supply one (for example, reuse it on retry); otherwise a random one is generated.
- `ts`: local time (with time zone) and the matching epoch seconds.
- `type`, `payload` (for example `{"id": "paper.a", "text": "..."}`) and `source` (which session wrote it).

Events come from three places: Claude sessions writing through MCP tools; the owner writing from a terminal with `python3 -m tasktree`; and decisions clicked on the viewer page, which Claude reads back and writes as `answer_decision`.

## 2. Q2: What is the state, and what is it keyed by?

| Table | Which kind of state | Key |
|---|---|---|
| `events` | Processing record, and the single source of truth | `event_id` UNIQUE; `seq` fixes replay order |
| `nodes` | Business state: the current shape of the tree (a materialized view) | `id`, e.g. `job.x.prep` |
| `notes` | Output record: each timestamped note | One row per `event_id` |

There is no "consumer position" (offset), because there is no feed file: each event is applied in the same transaction it arrives in, so there is never a question of "how far have we read".

Why store both `events` and `nodes`: `status` in `nodes` is overwritten, so once it changes from `doing` to `done` the old value is gone. `events` keeps the full history, so the system can answer both "what is its status now" and "when, and by whom, was it changed to this". That is event sourcing; `nodes` is a cached result kept for fast queries.

## 3. Q3: Invariants

- **I1**: An `event_id` is applied at most once. Guaranteed by `events.event_id UNIQUE` plus `INSERT OR IGNORE`. The same `event_id` with different content is rejected, since it means the caller misused the key.
- **I2**: `nodes` and `notes` always equal the result of replaying `events` from the start. Every write happens in one transaction, and the `rebuild` command can replay at any time to check.
- **I3**: The tree has no cycles and every `parent` exists. `decide` checks this on create and move.
- **I4**: A rejected event changes nothing and is not logged.
- **I5**: Concurrent writes from several sessions never lose an update.

## 4. Q4: Semantics that need to be pinned down

- **Status of a parent**: a leaf shows its own status. A parent shows the most urgent status among its open children and itself. When all children are done, the parent counts as done, unless it is itself explicitly marked doing, blocked or needs a decision (meaning there is work not yet split out). Urgency order: needs decision > blocked > doing > waiting > done.
- **Deletion**: nothing is deleted, only archived (`archive_node`). History always stays in the log.
- **Decisions**: a decision node carries `options`, and the answer must be one of them. Answering marks it done.
- **"Today"**: computed by local date (America/Chicago), not UTC.

## 5. Q5: Components and data ownership

```text
Claude session A ─┐
Claude session B ─┼─ MCP (stdio) ─▶ store.apply_event ─▶ SQLite (~/.tasktree/tasktree.db)
Terminal CLI ─────┘                                         │
                                                            ▼ export_viewer
                                         Claude artifact page (read-only view, works on a phone)
```

- `store.py` is the only module that writes SQL.
- Every Claude session starts its own MCP server process, so there is **more than one writer**. This differs from the single-writer rewards checker, and is why the locking in Q6 is needed.
- The database file lives outside the repository (`~/.tasktree/`), so the public code repository never contains personal data.

## 6. Q6: Failure checklist (10 common failure modes)

| # | Does it apply? | How this system handles it |
|---|---|---|
| 1 Duplicate events | Yes: Claude may resend after a tool call times out | `event_id` UNIQUE; `INSERT OR IGNORE` is the first statement in the transaction |
| 2 Concurrent writes | Yes: several sessions write at once | Each write is one `BEGIN IMMEDIATE` transaction that takes the write lock before reading nodes; `busy_timeout` queues for 30 seconds. Since the transaction's first statement is a write (the dedup insert), node reads always happen after the write lock is held |
| 3 Crash midway | Yes: the process can be killed | Event, node and note are written in one transaction: all or nothing |
| 4 Restart | Mostly not relevant: no offset | All state is in SQLite; `rebuild` can recover the views from the log |
| 5 Bad input | Yes: Claude may pass a wrong field | `decide` validates and raises `InvalidEvent`, returned to Claude as a tool error; the server does not crash |
| 6 Partial lines | Partly: bad JSON on stdio | Return a JSON-RPC parse error and keep reading the next line |
| 7 Time | Yes: the boundary of "today" | Store both local time and epoch, query by local date; tests use a fixed clock |
| 8 Unbounded state | Slowly | A year is a few thousand events, fine for SQLite; old events can be snapshotted later if needed |
| 9 External side effects | Yes: syncing to the artifact page | The page is only a read-only copy; a failed sync does not affect local data, and the next full export fixes it |
| 10 Money | Not involved | — |

## 7. Q7: Core functions

```text
apply_event(conn, event) -> Result          # store.py, one transaction
    BEGIN IMMEDIATE
    INSERT OR IGNORE INTO events(...)       # dedup; rowcount == 0 → duplicate
    change = decide(event, get_node, ts)    # model.py, pure function: validate and compute only
    write nodes / notes
    COMMIT (any exception → ROLLBACK)

decide(event, get, now) -> Change           # pure: never touches the database
rollup(nodes, id) -> status                 # pure: aggregates upward from the leaves
rebuild(conn)                               # clear nodes/notes, replay every event by seq
```

## 8. Q8: Tests

Each test in `tests/test_tasktree.py` maps to an invariant:

- the same event sent twice takes effect once (I1);
- an `event_id` reused for different content is rejected (I1);
- a move that would create a cycle is rejected and the state is unchanged (I3, I4);
- replaying the log gives the same tree (I2);
- 8 connections writing 25 notes each give exactly 200 notes (I5);
- two connections updating different fields of the same node at once keep both final values (I5, lost update);
- the day log is selected by local date;
- MCP handshake, tool calls, tool errors and bad input.

## 9. Mapping to production

| TaskTree | At larger scale |
|---|---|
| SQLite file + `BEGIN IMMEDIATE` | Postgres + row locks or a conditional `UPDATE` |
| `events` table | Kafka topic or an event store |
| `nodes` table | Materialized view / read model (CQRS, command query responsibility segregation) |
| Syncing the artifact page | A frontend that subscribes to the event stream and updates incrementally |

Reference: Kleppmann, *Designing Data-Intensive Applications*, the "Transactions" and "Stream Processing" chapters (event logs, derived state, immutability).
