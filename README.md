# move-claude-session

Move a [Claude Code](https://claude.com/claude-code) session to another account and resume it there. Run it, pick the session, pick the account.

![Picking a session, picking an account, resuming](demo/demo.gif)

If you run more than one Claude Code account (one per `CLAUDE_CONFIG_DIR`), every session lives in the account that started it. When that account hits its usage limit, the conversation is stuck there. This moves the transcript and everything attached to it into another account's config dir, then runs `claude --resume` under that account.

## Install

Python 3.9 or later, no dependencies.

```sh
git clone https://github.com/STRML/move-claude-session
ln -s "$PWD/move-claude-session/move-claude-session" ~/.local/bin/
```

Any directory on your `PATH` works in place of `~/.local/bin`.

## Use

Quit the session, then in the same terminal:

```sh
move-claude-session
```

1. Pick the session. The one that last ran in this terminal pane is on top and already selected, so this is usually just Enter.
2. Pick the account to move it to.

It moves the session, `cd`s to the session's project directory, and resumes it under the new account.

![The session picker](demo/pick-session.png)

| Key | Action |
|---|---|
| ↑ ↓ | Move the selection |
| Typing | Filter (space-separated words, all must match) |
| Enter | Pick |
| Esc | Cancel. Nothing changes. |

Rows marked `running` belong to a live Claude Code process and can't be picked until it exits.

![The account picker](demo/pick-account.png)

![The move, then the resume](demo/done.png)

### Without the picker

```sh
move-claude-session <session-id> <account>
```

This moves the session and prints the resume command instead of running it.

| Option | Effect |
|---|---|
| `-n`, `--dry-run` | Show what would move. Change nothing. |
| `--copy` | Leave the original in place. |
| `--force` | Overwrite files already at the destination, and move a session that is still running. |
| `--list` | Print the picker's rows in order, with session IDs, and exit. |

## Accounts

An account is any config dir in your home directory with a `projects/` folder in it:

| Account | Config dir | `claude` runs with |
|---|---|---|
| `default` | `~/.claude` | `CLAUDE_CONFIG_DIR` unset |
| `<name>` | `~/.claude-<name>` | `CLAUDE_CONFIG_DIR=~/.claude-<name>` |

To add one, run `CLAUDE_CONFIG_DIR=~/.claude-work claude` once and log in.

## How the picker orders sessions

```
»  last ran in this terminal pane    ┐  from cmux's hook log
·  ran in this cmux workspace        ┘  (~/.cmuxterm/claude-hook-sessions.json)
   then the 40 most recently written sessions, across all accounts
```

The pane matching comes from [cmux](https://cmux.dev), which records the pane each Claude Code session runs in. Without cmux the list is ordered by recency, so the session you just quit is still at or near the top.

Each row shows the title Claude Code puts on the terminal tab: your `/rename` title if you set one, else the title Claude Code generated, else your first prompt.

Titles are cached in `~/.cache/move-claude-session/labels.json` (`$XDG_CACHE_HOME` is respected), keyed on each transcript's size and mtime, so a session you just quit always shows its current title. On a machine with about 6,200 transcripts, the cache takes a run from 0.3 s to under 0.1 s.

## What moves

Paths are relative to the account's config dir:

```
projects/<project>/<session-id>.jsonl     the transcript
projects/<project>/<session-id>/          subagent transcripts, saved tool output
projects/<project>/cship/<session-id>-*   cship caches
<any other dir>/*<session-id>*            file-history, tasks, session-env, ...
```

The session keeps its project folder name in the new account, so `claude --resume` finds it from the same working directory. Every old path inside the moved transcripts is rewritten to its new location, so saved tool output still resolves.

A session is running while its process is alive and registered in `<config dir>/sessions/<pid>.json`. Claude Code deletes that file on exit.

## Tests

```sh
python3 -m unittest discover tests
```

The tests build a fake home directory with three accounts and made-up sessions (`tests/fixture.py`), drive the picker through a real pseudo-terminal, and check what moved and how `claude` was launched. The screenshots use the same fixture; `vhs demo/demo.tape` re-renders them.

## License

MIT
