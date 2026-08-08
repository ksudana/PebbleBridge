# PebbleBridge

Local worker + mailbox for **Codex Pigeon**, a Pebble Time 2 voice interface.

The watch (via the Pebble Core iPhone app) writes voice-note requests to `inbox/`.
This worker, running on your Mac, pulls new requests, processes each one with
[Claude Code](https://claude.com/claude-code), writes an answer to `replies/`,
regenerates `Conversation.md`, and pushes back.

```
PebbleBridge/
├── inbox/          # requests from the watch  (worker reads, never writes)
├── replies/        # answers from the worker  (immutable, one per request)
├── Conversation.md # human-readable rendered thread (auto-generated)
├── worker.py       # the local worker
└── README.md
```

## Running the worker

```bash
python3 worker.py           # one pass: pull, process new requests, push
python3 worker.py --watch   # poll forever (default every 30s)
python3 worker.py --dry-run # process but don't commit/push
```

No secrets live in this repo. The worker uses your local `git`/SSH auth to
push; the GitHub token is stored only on your phone.

## Message format

Each file is Markdown with a YAML frontmatter block:

```markdown
---
schema_version: 1
id: <uuid>
thread_id: <uuid>
parent_id: <uuid or empty>
sender: pebble | worker
created_at: 2026-08-08T16:00:00Z
status: new | complete
---

The body / voice-note transcript goes here.
```

A request in `inbox/<id>.md` is considered **answered** once a reply in
`replies/` has `parent_id: <id>`. Follow-ups keep the same `thread_id` and set
`parent_id` to the message they reply to.
