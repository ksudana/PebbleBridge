#!/usr/bin/env python3
"""Codex Pigeon local worker.

Pulls voice-note requests the Pebble watch pushed to inbox/, answers each new
one with Claude Code, writes an immutable reply to replies/, regenerates
Conversation.md, and pushes back.

Dependency-free (Python 3 stdlib only). Processing is done by the `claude` CLI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent
INBOX = ROOT / "inbox"
REPLIES = ROOT / "replies"
CONVERSATION = ROOT / "Conversation.md"

SCHEMA_VERSION = 1
POLL_SECONDS = 30
CLAUDE_TIMEOUT = 300  # seconds per request


# --------------------------------------------------------------------------- #
# Frontmatter parsing / serialising (minimal, no PyYAML dependency)
# --------------------------------------------------------------------------- #
def parse_message(path: pathlib.Path) -> dict:
    """Parse a `--- frontmatter --- body` markdown file into a dict."""
    text = path.read_text(encoding="utf-8")
    meta: dict = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            _, front, body = parts
            for line in front.strip().splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    val = val.strip()
                    # The phone quotes string values (id: "pbl-…"); strip them
                    # so ids compare cleanly and bodies aren't double-quoted.
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                        val = val[1:-1]
                    meta[key.strip()] = val
    meta["body"] = body.strip()
    meta["_path"] = path
    return meta


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_message(directory: pathlib.Path, meta: dict, body: str,
                  stem: str | None = None) -> pathlib.Path:
    fields = ["schema_version", "id", "thread_id", "parent_id",
              "sender", "created_at", "status"]
    # Match the phone's frontmatter style: schema_version is a bare int, other
    # values are quoted strings (empty parent_id stays bare).
    def fmt(key: str) -> str:
        val = meta.get(key, "")
        if key == "schema_version" or val == "":
            return f"{key}: {val}"
        return f'{key}: "{val}"'
    front = "\n".join(fmt(k) for k in fields)
    # The phone pairs a request with the reply of the *same filename*, so
    # replies are named after the request id (passed as `stem`).
    path = directory / f"{stem or meta['id']}.md"
    path.write_text(f"---\n{front}\n---\n\n{body.strip()}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Git helpers
# --------------------------------------------------------------------------- #
def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=check
    )


def git_pull() -> None:
    # Only fast-forward: never auto-merge divergent history from the phone side.
    res = git("pull", "--ff-only", check=False)
    if res.returncode != 0:
        # No upstream yet (fresh repo) is fine; anything else is worth showing.
        stderr = res.stderr.strip()
        if "no tracking information" not in stderr.lower() and \
           "couldn't find remote ref" not in stderr.lower():
            print(f"[warn] git pull: {stderr}", file=sys.stderr)


def git_push(changed: list[pathlib.Path], summary: str) -> None:
    git("add", "-A")
    status = git("status", "--porcelain").stdout.strip()
    if not status:
        return
    git("commit", "-m", summary)
    res = git("push", check=False)
    if res.returncode != 0:
        print(f"[warn] git push: {res.stderr.strip()}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Core processing
# --------------------------------------------------------------------------- #
def load(directory: pathlib.Path) -> list[dict]:
    return [parse_message(p) for p in sorted(directory.glob("*.md"))]


def process_with_claude(request: dict, thread: list[dict]) -> str:
    """Answer a request, giving Claude the prior thread as context."""
    lines = []
    for msg in thread:
        if msg["id"] == request["id"]:
            break
        who = "Assistant" if msg.get("sender") == "worker" else "User"
        lines.append(f"{who}: {msg['body']}")
    context = "\n\n".join(lines)

    prompt = (
        "You are answering voice notes dictated on a Pebble smartwatch. "
        "Reply concisely and plainly — the answer is read on a tiny screen.\n"
        "You have WebSearch and WebFetch available. For anything time-sensitive "
        "or factual — weather, news, prices, sports, schedules, current events — "
        "you MUST use WebSearch to get live data rather than answering from memory "
        "or saying you lack access. Only skip the web for timeless questions.\n\n"
        + (f"Conversation so far:\n{context}\n\n" if context else "")
        + f"New request:\n{request['body']}"
    )

    try:
        res = subprocess.run(
            # Pre-approve web tools so live questions (weather/news) can be
            # answered without an interactive permission prompt.
            ["claude", "-p", prompt, "--allowedTools", "WebSearch", "WebFetch"],
            text=True, capture_output=True, timeout=CLAUDE_TIMEOUT,
        )
    except FileNotFoundError:
        return "[worker error] `claude` CLI not found on this machine."
    except subprocess.TimeoutExpired:
        return f"[worker error] processing timed out after {CLAUDE_TIMEOUT}s."

    if res.returncode != 0:
        return f"[worker error] claude exited {res.returncode}: {res.stderr.strip()}"
    return res.stdout.strip() or "[worker error] empty response."


def render_conversation(inbox: list[dict], replies: list[dict]) -> None:
    all_msgs = sorted(inbox + replies, key=lambda m: m.get("created_at", ""))
    threads: dict[str, list[dict]] = {}
    for m in all_msgs:
        threads.setdefault(m.get("thread_id", "unknown"), []).append(m)

    out = ["# Conversation\n",
           "_This file is auto-generated by `worker.py`. Do not edit by hand._\n"]
    for tid, msgs in threads.items():
        out.append(f"\n## Thread `{tid[:8]}`\n")
        for m in msgs:
            who = "🤖 Worker" if m.get("sender") == "worker" else "🎙️ Pebble"
            out.append(f"**{who}** · {m.get('created_at', '')}\n\n{m['body']}\n")
    CONVERSATION.write_text("\n".join(out) + "\n", encoding="utf-8")


def run_once(dry_run: bool = False) -> int:
    if not dry_run:
        git_pull()

    inbox = load(INBOX)
    replies = load(REPLIES)
    answered = {r.get("parent_id") for r in replies}

    pending = [m for m in inbox if m["id"] and m["id"] not in answered]
    if not pending:
        return 0

    changed: list[pathlib.Path] = []
    for req in pending:
        print(f"[info] processing {req['id']} …")
        answer = process_with_claude(req, inbox)
        reply_meta = {
            "schema_version": SCHEMA_VERSION,
            "id": str(uuid.uuid4()),
            "thread_id": req.get("thread_id", req["id"]),
            "parent_id": req["id"],
            "sender": "worker",
            "created_at": now_iso(),
            "status": "complete",
        }
        path = write_message(REPLIES, reply_meta, answer, stem=req["id"])
        changed.append(path)

    render_conversation(load(INBOX), load(REPLIES))

    if dry_run:
        print(f"[dry-run] wrote {len(changed)} repl(y/ies), skipped commit/push.")
    else:
        git_push(changed, f"worker: answer {len(changed)} request(s)")
    return len(changed)


def main() -> None:
    ap = argparse.ArgumentParser(description="Codex Pigeon local worker")
    ap.add_argument("--watch", action="store_true",
                    help=f"poll forever every {POLL_SECONDS}s")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS,
                    help="poll interval in seconds (with --watch)")
    ap.add_argument("--dry-run", action="store_true",
                    help="process but do not commit/push")
    args = ap.parse_args()

    if not args.watch:
        n = run_once(dry_run=args.dry_run)
        print(f"[done] answered {n} request(s).")
        return

    print(f"[watch] polling every {args.interval}s — Ctrl-C to stop.")
    while True:
        try:
            n = run_once(dry_run=args.dry_run)
            if n:
                print(f"[watch] answered {n} request(s).")
        except Exception as exc:  # keep the loop alive
            print(f"[error] {exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
