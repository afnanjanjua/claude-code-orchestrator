# claude-code-orchestrator

Watch all your live Claude Code terminals at once. Get a paste-ready file
showing which terminals are idle (with their last response) and which are
still running.

Useful if you're orchestrating multiple Claude Code panes from another
chat (e.g. Anthropic's Claude.ai with a Slack/coord integration), and
you're tired of manually copy-pasting each terminal's response back to
the orchestrator.

## What it does

```
~/Desktop/cowork-inbox.md          ← refreshed every few seconds
─────────────────────────────────
t1: <last assistant response from terminal renamed `1`>

t2: <last assistant response from terminal renamed `2`>

t5: waiting (mid tool_use, no events for 642s — check pane)

...

---
running: t3, t4, t8 | waiting for next prompt: t1, t2, t5, t6, t7
```

- Body shows the **last completed assistant turn** for each idle pane —
  you copy-paste straight into your orchestrator chat.
- Footer summarizes which panes are still running vs. ready for the
  next prompt.
- macOS notification fires when a pane flips to idle.
- Running terminals are **excluded from the body** so the file stays
  clean for pasting elsewhere.

## How it works

Claude Code writes a JSON metadata file for every live interactive CLI
process at `~/.claude/sessions/<PID>.json`. Each file maps:

- the OS process ID,
- the conversation session ID (the `.jsonl` filename in
  `~/.claude/projects/<encoded-cwd>/`),
- a user-assigned `name` (set by running `/rename <NAME>` inside the
  Claude session),
- a live status (`busy`, `shell`, `idle`).

The poller reads these files plus the conversation `.jsonl`s to
classify each pane as `RUNNING` / `IDLE` / `WAITING` / `UNKNOWN`, and
extracts the most recent `end_turn` assistant message for the idle
ones.

## Quick start

```bash
git clone https://github.com/itpixelz/claude-code-orchestrator.git
cd claude-code-orchestrator

# In each Claude Code terminal you want to track, run once:
#     /rename 1     (in pane 1)
#     /rename 2     (in pane 2)
#     ...

# Run the poller (one-shot):
python3 poll.py

# Or watch continuously (refresh every 5s):
python3 poll.py --watch
```

Open `~/Desktop/cowork-inbox.md` in your editor of choice. It refreshes
every cycle. macOS notifications fire when a pane goes idle.

## Usage

```
python3 poll.py [options]

Options:
  --watch                 Loop forever instead of one-shot
  -i N, --interval N      Seconds between polls when --watch (default 5)
  --project SUBSTRING     Only include sessions whose cwd contains this
                          substring (default: all live sessions)
  --out PATH              Inbox markdown path
                          (default ~/Desktop/cowork-inbox.md)
  --status PATH           Status text path
                          (default ~/Desktop/cowork-status.txt)
  --state PATH            Prev-state cache path
                          (default ~/.claude-code-orchestrator/prev_state.json)
  --include-unnamed       Include sessions you haven't /renamed
                          (labeled pid<N>)
  --quiet                 Suppress per-cycle stdout
```

### Examples

```bash
# Only watch sessions whose cwd contains "my-project"
python3 poll.py --watch --project my-project

# Write to a custom file in your project
python3 poll.py --watch --out ./terminals.md --interval 3

# Include all sessions, even unnamed ones (use sparingly)
python3 poll.py --watch --include-unnamed
```

## Labeling your terminals

Inside each Claude Code session you want to track, run:

```
/rename 1
```

(or any string — the poller will use it as the label, prefixing with
`t`). The poller picks up the name automatically on the next cycle.

To avoid duplicates, give each pane a unique name (e.g. `1` through
`N`).

## State classification

| State | Meaning | Source |
|---|---|---|
| `RUNNING` | Currently rendering an assistant turn or running a tool. Excluded from inbox body; named in footer. | CLI status `busy`/`shell` (when fresh) OR last assistant `stop_reason='tool_use'` with recent file mtime. |
| `IDLE` | Last assistant message ended cleanly. Full response written to inbox body. Footer lists as "waiting for next prompt". | Last assistant `stop_reason='end_turn'`. |
| `WAITING` | Mid-tool with no events for >10 minutes. Likely stuck on a permission prompt or hung. | `stop_reason='tool_use'` + old mtime. |
| `UNKNOWN` | No assistant message yet, or unrecognized state. | Default fallback. |

The CLI metadata's `status` field is trusted only if it was updated in
the last 60 seconds. Otherwise the poller falls back to event-based
classification from the conversation `.jsonl`. This handles the common
case where Claude Code wrote `status='shell'` 20 minutes ago and never
re-synced.

## Layout

```
claude-code-orchestrator/
├── poll.py          # the poller
├── README.md
├── LICENSE          # MIT
└── install.sh       # optional install / symlink helper
```

The poller is a single Python file with no dependencies beyond the
standard library. Tested on macOS with Python 3.10+. Should work on
Linux too (the notification call is a no-op there); not tested on
Windows.

## Where Claude Code stores its data

For reference (everything below is read by the poller):

```
~/.claude/sessions/<PID>.json
    Per-live-process metadata. Created when an interactive `claude`
    starts; deleted when the process exits cleanly. Contains the
    sessionId, cwd, name (set by /rename), and current status.

~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl
    The full conversation history for one Claude Code session.
    JSONL where each line is an event: user prompt, assistant
    response, tool call, tool result, system message, etc.

~/.claude/history.jsonl
    Global cross-session prompt history. Every prompt you've ever
    typed, with timestamp, project, and sessionId.
```

The poller never writes to any of these — it's read-only.

## Privacy

The poller reads your Claude Code conversation files locally and
writes the inbox/status files locally. It does not phone home, upload,
or analyze any data outside your machine.

## Roadmap

Possible v0.2 features (not yet built):

- **Prompt injection**: AppleScript-based "paste this prompt into pane
  t3" helper for round-tripping orchestrator → pane.
- **Outcome logging**: per-pane round-trip metrics
  (prompt → response time, completion shape).
- **Web dashboard**: small local web view of the same data.
- **Non-macOS notifications**: libnotify on Linux, toast on Windows.

PRs welcome.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

Built because manually copy-pasting between 10 Claude Code panes and a
single orchestrator chat was eating hours of the day.
