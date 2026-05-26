#!/usr/bin/env python3
"""
claude-code-orchestrator — poll.py

Watches your live Claude Code sessions and writes a paste-ready file
showing which terminals are idle (with their last response) and which
are running.

Discovery uses ~/.claude/sessions/<PID>.json, which Claude Code writes
for every interactive CLI process. Label each pane with `/rename N`
inside its Claude session — the poller picks up the name automatically.

Usage:
    python3 poll.py                  # one-shot
    python3 poll.py --watch          # loop every 5s
    python3 poll.py --watch -i 3     # loop every 3s
    python3 poll.py --project NAME   # filter by cwd substring
    python3 poll.py --out PATH       # custom inbox path
    python3 poll.py --quiet          # suppress per-cycle stdout

Outputs (default):
    ~/Desktop/cowork-inbox.md    paste-ready blob, refreshed each cycle
    ~/Desktop/cowork-status.txt  one-line-per-pane status

License: MIT
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

CLAUDE_HOME = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_HOME / "sessions"
PROJECTS_DIR = CLAUDE_HOME / "projects"

DEFAULT_OUTBOX = Path.home() / "Desktop" / "cowork-inbox.md"
DEFAULT_STATUS = Path.home() / "Desktop" / "cowork-status.txt"
DEFAULT_PREV_STATE = Path.home() / ".claude-code-orchestrator" / "prev_state.json"

# Tunables
WAITING_THRESHOLD_S = 600       # mid-tool quiet > 10min = WAITING (probably stuck)
META_STALE_THRESHOLD_S = 60     # session metadata older than this is unreliable


# ---------------------------------------------------------------------------
# Session-file parsing
# ---------------------------------------------------------------------------

def read_jsonl_tail(path: Path, max_lines: int = 200) -> list[dict]:
    """Read last N parseable JSON events from a JSONL file efficiently."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, max_lines * 8192)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    out = []
    for line in data.splitlines()[-max_lines:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def last_assistant_message(events: list[dict]) -> tuple[str, str]:
    """Return (last_text, last_stop_reason) for the most recent assistant turn."""
    for e in reversed(events):
        if e.get("type") != "assistant":
            continue
        msg = e.get("message", {})
        stop = msg.get("stop_reason", "")
        content = msg.get("content", [])
        if isinstance(content, str):
            return content, stop
        text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        full = "\n".join(t for t in text_parts if t.strip())
        return (full if full.strip() else ""), stop
    return "", ""


def last_end_turn_text(events: list[dict]) -> str:
    """Return assistant text from the most recent end_turn message."""
    for e in reversed(events):
        if e.get("type") != "assistant":
            continue
        msg = e.get("message", {})
        if msg.get("stop_reason") != "end_turn":
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            return content
        text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        full = "\n".join(t for t in text_parts if t.strip())
        if full.strip():
            return full
    return ""


def classify(session_jsonl: Path) -> tuple[str, str, dict]:
    """
    Event-based classification using stop_reason as the primary signal.
      end_turn  → IDLE (final response delivered)
      tool_use  + recent mtime → RUNNING
      tool_use  + old mtime    → WAITING (likely stuck on permission prompt)
      no assistant yet         → UNKNOWN
    """
    try:
        age = time.time() - session_jsonl.stat().st_mtime
    except FileNotFoundError:
        return "UNKNOWN", "", {"age_s": -1, "stop_reason": "?", "size_kb": 0}

    events = read_jsonl_tail(session_jsonl, max_lines=200)
    last_text, last_stop = last_assistant_message(events)
    info = {
        "age_s": int(age),
        "stop_reason": last_stop or "?",
        "size_kb": session_jsonl.stat().st_size // 1024,
    }

    if last_stop == "end_turn":
        text = last_end_turn_text(events) or last_text
        return "IDLE", text, info
    if last_stop == "tool_use":
        if age < WAITING_THRESHOLD_S:
            return "RUNNING", last_text, info
        return "WAITING", last_text, info
    return "UNKNOWN", last_text, info


def reconcile_state(cli_status: str, classified_state: str) -> str:
    """
    Combine cli_status (from session metadata) with event-based classification.

    cli_status:
      'busy'  → RUNNING (CLI rendering an assistant turn)
      'shell' → RUNNING (CLI shelling out)
      'idle'  → trust event classifier
      'stale' → trust event classifier (metadata frozen / process gone)
    """
    if cli_status in ("busy", "shell"):
        return "RUNNING"
    return classified_state


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_terminals(project_filter: str | None = None) -> list[dict]:
    """
    Return a list of live-terminal records sorted by user-assigned name.

    Source of truth: ~/.claude/sessions/<PID>.json — Claude Code writes
    these for every live interactive CLI process. The `name` field is set
    by `/rename N` inside the Claude session.
    """
    if not SESSIONS_DIR.exists():
        return []

    records = []
    now = time.time()

    for meta_path in SESSIONS_DIR.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if meta.get("kind") != "interactive":
            continue

        sid = meta.get("sessionId")
        if not sid:
            continue

        cwd = meta.get("cwd", "")
        if project_filter and project_filter not in cwd:
            continue

        # Resolve the session JSONL by sid; project dir is encoded from cwd
        project_dir = _cwd_to_project_dir(cwd)
        session_jsonl = project_dir / f"{sid}.jsonl" if project_dir else None
        if not session_jsonl or not session_jsonl.exists():
            continue

        updated_at_ms = meta.get("updatedAt", 0)
        meta_age_s = max(0, now - updated_at_ms / 1000)
        raw_status = meta.get("status", "?")
        cli_status = "stale" if meta_age_s > META_STALE_THRESHOLD_S else raw_status

        records.append({
            "label": str(meta.get("name", f"pid{meta.get('pid', '?')}")),
            "pid": meta.get("pid"),
            "session": session_jsonl,
            "sid": sid,
            "cwd": cwd,
            "cli_status": cli_status,
            "cli_status_raw": raw_status,
            "meta_age_s": int(meta_age_s),
        })

    records.sort(key=_label_sort_key)
    return records


def _cwd_to_project_dir(cwd: str) -> Path | None:
    """
    Claude Code encodes the project directory by replacing slashes with dashes:
      /Users/foo/bar  →  ~/.claude/projects/-Users-foo-bar/
    """
    if not cwd:
        return None
    encoded = cwd.replace("/", "-")
    p = PROJECTS_DIR / encoded
    return p if p.exists() else None


def _label_sort_key(record_or_label):
    """Sort numerically when label is a number, lexicographically otherwise."""
    label = record_or_label["label"] if isinstance(record_or_label, dict) else record_or_label
    try:
        return (0, int(label))
    except ValueError:
        return (1, str(label))


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def macos_notify(title: str, body: str) -> None:
    """Show a macOS notification via osascript. No-op on other OSes."""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            check=False, capture_output=True, timeout=3,
        )
    except Exception:
        pass


def load_prev_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_prev_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def strip_leading_separators(text: str) -> str:
    """Remove leading `---` markdown separators that precede some Claude responses."""
    cleaned = text.lstrip()
    while cleaned.startswith("---"):
        if "\n" not in cleaned:
            return ""
        cleaned = cleaned.split("\n", 1)[1].lstrip()
    return cleaned


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(
    inbox_path: Path,
    status_path: Path,
    prev_state_path: Path,
    project_filter: str | None,
    include_unnamed: bool,
    verbose: bool,
    exclude_labels: set[str] | None = None,
) -> dict:
    records = discover_terminals(project_filter)
    if not records:
        if verbose:
            msg = f"No live sessions found in {SESSIONS_DIR}"
            if project_filter:
                msg += f" (filter: cwd contains '{project_filter}')"
            print(msg, file=sys.stderr)
        # Still write empty files so consumers don't see stale data
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_text("---\nno live terminals\n")
        status_path.write_text("# no live terminals\n")
        return {}

    prev = load_prev_state(prev_state_path)
    current = {}
    rows = []
    inbox_entries: list[tuple[str, str]] = []
    state_counter: Counter[str] = Counter()

    for r in records:
        label = r["label"]
        if not include_unnamed and label.startswith("pid"):
            continue
        if exclude_labels and label in exclude_labels:
            continue

        classified, text, info = classify(r["session"])
        state = reconcile_state(r["cli_status"], classified)
        state_counter[state] += 1
        current[label] = {
            "state": state,
            "session": r["sid"],
            "age_s": info["age_s"],
            "pid": r["pid"],
        }

        rows.append(
            f"t{label:<4} {state:8} cli={r['cli_status']:6} "
            f"age={info['age_s']:>5}s stop={info['stop_reason']:9} "
            f"sid={r['sid'][:8]} pid={r['pid']} size={info['size_kb']}KB"
        )

        # Body entries: skip RUNNING (footer summarizes them)
        if state == "RUNNING":
            continue
        if state == "IDLE" and text.strip():
            value = strip_leading_separators(text)
        elif state == "WAITING":
            value = f"waiting (mid tool_use, no events for {info['age_s']}s — check pane)"
        elif state == "UNKNOWN":
            value = "unknown"
        else:
            value = state.lower()
        inbox_entries.append((f"t{label}", value))

    # Status file
    status_lines = [
        f"# claude-code-orchestrator status — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# {dict(state_counter)}",
        "",
        *rows,
    ]
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text("\n".join(status_lines) + "\n")

    # Inbox
    inbox_blocks = [f"{label}: {value}" for label, value in inbox_entries]

    running_labels = sorted(
        (lbl for lbl, st in current.items() if st["state"] == "RUNNING"),
        key=_label_sort_key,
    )
    waiting_labels = sorted(
        (lbl for lbl, st in current.items() if st["state"] == "IDLE"),
        key=_label_sort_key,
    )

    footer_parts = []
    if running_labels:
        footer_parts.append("running: " + ", ".join(f"t{l}" for l in running_labels))
    if waiting_labels:
        footer_parts.append(
            "waiting for next prompt: " + ", ".join(f"t{l}" for l in waiting_labels)
        )
    footer = " | ".join(footer_parts) if footer_parts else "no live terminals"

    body = "\n\n".join(inbox_blocks)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_text((body + "\n\n" if body else "") + f"---\n{footer}\n")

    # Notify on new IDLE transitions
    newly_idle = [
        lbl for lbl, info in current.items()
        if info["state"] == "IDLE" and prev.get(lbl, {}).get("state") != "IDLE"
    ]
    if newly_idle:
        macos_notify(
            f"Cowork: {len(newly_idle)} terminal(s) ready",
            "t" + ", t".join(newly_idle) + f" — paste from {inbox_path}",
        )

    save_prev_state(prev_state_path, current)

    if verbose:
        print(f"\n=== {datetime.now().strftime('%H:%M:%S')} — {dict(state_counter)} ===")
        for row in rows:
            print(row)
        if newly_idle:
            print(f"\n>>> NEW IDLE: {newly_idle} — notification sent")
        print(f"\nInbox:  {inbox_path}")
        print(f"Status: {status_path}")

    return current


def main():
    ap = argparse.ArgumentParser(
        description="Poll live Claude Code sessions and write a paste-ready inbox.",
        epilog=(
            "Inside each Claude session, run `/rename N` to label it with a "
            "number — that name appears as t<N> in the inbox."
        ),
    )
    ap.add_argument("--watch", action="store_true", help="loop forever")
    ap.add_argument(
        "-i", "--interval", type=int, default=5,
        help="seconds between polls when --watch (default 5)",
    )
    ap.add_argument(
        "--project", type=str, default=None,
        help="filter sessions whose cwd contains this substring "
             "(e.g. 'my-project'); default: include all live sessions",
    )
    ap.add_argument(
        "--out", type=Path, default=DEFAULT_OUTBOX,
        help=f"inbox markdown path (default {DEFAULT_OUTBOX})",
    )
    ap.add_argument(
        "--status", type=Path, default=DEFAULT_STATUS,
        help=f"status text path (default {DEFAULT_STATUS})",
    )
    ap.add_argument(
        "--state", type=Path, default=DEFAULT_PREV_STATE,
        help=f"prev-state cache (default {DEFAULT_PREV_STATE})",
    )
    ap.add_argument(
        "--include-unnamed", action="store_true",
        help="include sessions you haven't /renamed (labeled pid<N>)",
    )
    ap.add_argument(
        "--exclude", type=str, default=None,
        help="comma-separated labels to exclude from output (e.g. 'N,9')",
    )
    ap.add_argument("--quiet", action="store_true", help="suppress per-cycle stdout")
    args = ap.parse_args()

    exclude_labels = (
        {x.strip() for x in args.exclude.split(",") if x.strip()}
        if args.exclude else None
    )

    if not args.watch:
        run_once(
            args.out, args.status, args.state,
            args.project, args.include_unnamed, not args.quiet,
            exclude_labels,
        )
        return

    if not args.quiet:
        scope = f"cwd∋'{args.project}'" if args.project else "all live sessions"
        print(
            f"claude-code-orchestrator polling {scope} every {args.interval}s. "
            f"Ctrl-C to stop.\n  inbox:  {args.out}\n  status: {args.status}\n"
        )
    try:
        while True:
            run_once(
                args.out, args.status, args.state,
                args.project, args.include_unnamed, not args.quiet,
                exclude_labels,
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if not args.quiet:
            print("\nstopped.")


if __name__ == "__main__":
    main()
