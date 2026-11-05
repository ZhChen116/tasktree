# TaskTree

A personal task tree that Claude keeps up to date. Every piece of work (papers, job search, courses, reading) is a node in one tree; every change is an event in an append-only log. Any Claude Code session can read and write the tree through a small MCP server, so work spread across many sessions ends up in one place.

```text
[?] Research (paper)
  [?] Paper B (paper.b)
    [?] Run the large experiment? (paper.b.run) — options: yes / not yet
  [>] Paper A (paper.a)
    [>] Submit (paper.a.submit) — next: final read-through with advisor
[>] Job search (job)
  [>] Company X onsite (job.x) — next: practice system design
```

- **Event-sourced**: `events` is the source of truth; the `nodes` table is derived from it in the same transaction and can be rebuilt by replaying the log.
- **Idempotent**: every event carries an `event_id`; sending the same event twice applies it once.
- **Safe with many writers**: each Claude session runs its own server process; SQLite `BEGIN IMMEDIATE` serializes their writes.
- **No dependencies**: Python 3.10+ standard library only, including the MCP stdio server.

Design notes (in Chinese, following an 8-question backend design checklist): [DESIGN.md](DESIGN.md).

## Use with Claude Code

```bash
claude mcp add --scope user tasktree -- python3 -m tasktree serve
```

Run it from a shell where `tasktree` is importable (for example `pip install -e .` in this folder, or set `PYTHONPATH`). Data lives in `~/.tasktree/tasktree.db` (override with `TASKTREE_DB`); nothing personal is stored in this repository.

Tools: `tree`, `get_node`, `open_items`, `day_log`, `add_node`, `update_node`, `add_note`, `move_node`, `ask_decision`, `answer_decision`, `archive_node`, `export_viewer`.

## Command line

```bash
python3 -m tasktree tree            # open branches; --all to include done ones
python3 -m tasktree add "Read DDIA ch.11" --parent learn --status doing
python3 -m tasktree set learn.ddia status=done
python3 -m tasktree note job.x "booked interview slots"
python3 -m tasktree log 2026-11-04  # what happened that day
python3 -m tasktree rebuild         # replay the log into the node table
```

## Viewer

`viewer/index.html` is a read-only page that renders the tree from documents shaped like `export_viewer` output (status roll-up, decisions, blockers, recent activity). It is written for a Claude artifact with a `db` capability; Claude copies the export into it.

## Tests

```bash
python3 -m pytest -q
```
