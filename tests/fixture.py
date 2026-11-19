"""Build a fake HOME with three Claude Code accounts and made-up sessions.

The tests use it, and so do the README screenshots (`demo/demo.tape`):

    python3 tests/fixture.py /tmp/demo/home /tmp/demo/bin

Every session, path and prompt here is invented.
"""

import json
import os
import re
import sys
import time

SURFACE = "11111111-1111-4111-8111-111111111111"     # "this terminal pane"

# id, account, project dir under ~/code, tab title, minutes since last write,
# whether cmux saw it run in this terminal pane.
# The tab title is written as an ai-title record, except where TITLE_SOURCE says.
SESSIONS = [
    ("aaaaaaaa-0000-4000-8000-000000000001", "default", "billing-api",
     "Add retry with backoff to the invoice webhook", 2, True),
    ("aaaaaaaa-0000-4000-8000-000000000002", "work", "dashboard",
     "Fix the chart legend overlapping on mobile", 25, False),
    ("aaaaaaaa-0000-4000-8000-000000000003", "work", "infra",
     "Upgrade the Postgres cluster to 17", 70, False),
    ("aaaaaaaa-0000-4000-8000-000000000004", "default", "dotfiles",
     "/review the fish prompt changes", 180, False),
    ("aaaaaaaa-0000-4000-8000-000000000005", "client", "acme-site",
     "Migrate the contact form to the new CRM", 1500, True),
    ("aaaaaaaa-0000-4000-8000-000000000006", "default", "billing-api",
     "Write the monthly usage report query", 4000, False),
]
TITLE_SOURCE = {
    "aaaaaaaa-0000-4000-8000-000000000004": "prompt",   # no ai-title yet
    "aaaaaaaa-0000-4000-8000-000000000006": "rename",   # /rename beats ai-title
}
RUNNING = "aaaaaaaa-0000-4000-8000-000000000003"   # listed by `claude agents --json`

ACCOUNT_DIRS = {"default": ".claude", "work": ".claude-work", "client": ".claude-client"}


def project_folder(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def user_line(sid, cwd, text):
    if text.startswith("/"):
        name, _, args = text.partition(" ")
        text = (f"<command-message>{name[1:]}</command-message>\n"
                f"<command-name>{name}</command-name>\n"
                f"<command-args>{args}</command-args>")
    return {"type": "user", "sessionId": sid, "cwd": cwd,
            "message": {"role": "user", "content": text}}


# Stands in for `claude`. `claude agents --json` prints the account's
# fake-agents.json (or fails when ~/fake-agents-fail exists); anything else records
# how it was launched in ~/launched.
FAKE_CLAUDE = """#!/bin/sh
if [ "$1" = agents ]; then
  [ -f "$HOME/fake-agents-fail" ] && { echo "agents: not logged in" >&2; exit 1; }
  cat "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/fake-agents.json" 2>/dev/null || echo '[]'
  exit 0
fi
{ echo "argv=$*"; echo "cwd=$(pwd)"; echo "config=${CLAUDE_CONFIG_DIR-<unset>}"; } > "$HOME/launched"
echo "(claude $* starts here)"
"""


# Stands in for `ssh`. A host is a directory under $FAKE_REMOTES, which becomes the
# remote HOME; the command runs there under sh, the way sshd hands it to the login
# shell. A host with no directory is unreachable. `$FAKE_REMOTES/<host>.drop-on`
# holds a word: a command containing it gets the first 512 bytes of stdin, then the
# connection drops. With `$FAKE_REMOTES/<host>.corrupt`, every uploaded transcript
# gains a byte. `-t` (the resume) only records the command. Every call prints
# ssh_config noise on stderr, as a ProxyCommand can on a call that succeeds.
FAKE_SSH = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_REMOTES/ssh.log"
echo "Alarm clock: 14  perl -e 'ualarm 300000' nc -z host 22" >&2
tty=no
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -t) tty=yes; shift ;;
    --) shift; break ;;
    -*) shift ;;
    *) break ;;
  esac
done
host=$1; shift
home="$FAKE_REMOTES/$host"
if [ ! -d "$home" ]; then
  echo "ssh: connect to host $host port 22: Operation timed out" >&2
  exit 255
fi
if [ $tty = yes ]; then
  printf '%s\\n' "$*" > "$FAKE_REMOTES/$host.resumed"
  exit 0
fi
cd "$home" || exit 255
drop=$(cat "$FAKE_REMOTES/$host.drop-on" 2>/dev/null)
if [ -n "$drop" ] && case "$*" in *"$drop"*) true ;; *) false ;; esac; then
  head -c 512 | HOME="$home" sh -c "$*" >/dev/null 2>&1
  echo "Connection to $host closed by remote host." >&2
  exit 255
fi
HOME="$home" sh -c "$*"
status=$?
if [ -f "$FAKE_REMOTES/$host.corrupt" ]; then   # flip the upload's transcripts
  find "$home" -path '*.partial/*.jsonl' -exec sh -c 'echo x >> "$1"' sh {} \\;
fi
exit $status
"""

# Stands in for `cmux`: records how it was called in ~/cmux-launched.
FAKE_CMUX = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a"; done > "$HOME/cmux-launched"
"""


def write_tool(bindir: str, name: str, body: str) -> None:
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, name)
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, 0o755)


def write_fake_claude(bindir: str) -> None:
    write_tool(bindir, "claude", FAKE_CLAUDE)


def write_fake_remote_tools(bindir: str) -> None:
    write_tool(bindir, "ssh", FAKE_SSH)
    write_tool(bindir, "cmux", FAKE_CMUX)


def build_remote(remotes: str, host: str, accounts) -> str:
    """A fake remote host whose home holds these config dirs, each a (dir name,
    state) pair: "projects" (has sessions), "fresh" (logged in, no projects/ yet,
    so only .claude.json) or "other" (a dir that isn't a Claude Code account).
    Returns the remote home."""
    home = os.path.join(remotes, host)
    for name, state in accounts:
        os.makedirs(os.path.join(home, name, "projects" if state == "projects" else ""),
                    exist_ok=True)
        if state == "fresh":
            open(os.path.join(home, name, ".claude.json"), "w").close()
    return home


def build(home: str) -> dict:
    """Create the fake HOME. Returns {session_id: transcript path}."""
    home = os.path.abspath(home)
    for d in ACCOUNT_DIRS.values():
        os.makedirs(os.path.join(home, d, "projects"), exist_ok=True)
    now = time.time()
    paths, cmux = {}, {}
    for sid, account, proj, title, minutes, in_terminal in SESSIONS:
        cwd = os.path.join(home, "code", proj)
        os.makedirs(cwd, exist_ok=True)
        acct_root = os.path.join(home, ACCOUNT_DIRS[account])
        folder = os.path.join(acct_root, "projects", project_folder(cwd))
        os.makedirs(os.path.join(folder, sid, "tool-results"), exist_ok=True)
        result = os.path.join(folder, sid, "tool-results", "out.txt")
        with open(result, "w") as fh:
            fh.write("tool output\n")
        path = os.path.join(folder, f"{sid}.jsonl")
        source = TITLE_SOURCE.get(sid, "ai")
        lines = [
            {"type": "system", "sessionId": sid, "cwd": cwd,
             "content": "<system-reminder>ignored</system-reminder>"},
            user_line(sid, cwd, "/clear"),
            user_line(sid, cwd, title if source == "prompt" else f"first prompt of {proj}"),
            {"type": "assistant", "sessionId": sid, "cwd": cwd,
             "message": {"role": "assistant", "content": f"Saved to {result}"}},
        ]
        if source != "prompt":
            lines.append({"type": "ai-title", "sessionId": sid,
                          "aiTitle": "stale generated title"})
            lines.append({"type": "ai-title", "sessionId": sid,
                          "aiTitle": "generated title" if source == "rename" else title})
        if source == "rename":
            lines.append({"type": "custom-title", "sessionId": sid, "customTitle": title})
        with open(path, "w") as fh:
            # Compact separators, the way Claude Code writes transcripts.
            fh.writelines(json.dumps(line, separators=(",", ":")) + "\n" for line in lines)
        os.makedirs(os.path.join(acct_root, "file-history", sid), exist_ok=True)
        stamp = now - minutes * 60
        os.utime(path, (stamp, stamp))
        paths[sid] = path
        cmux[sid] = {"sessionId": sid, "transcriptPath": path, "updatedAt": stamp,
                     "surfaceId": SURFACE if in_terminal else "another-pane"}
    os.makedirs(os.path.join(home, ".cmuxterm"), exist_ok=True)
    with open(os.path.join(home, ".cmuxterm", "claude-hook-sessions.json"), "w") as fh:
        json.dump({"sessions": cmux}, fh)
    with open(os.path.join(home, ".claude-work", "fake-agents.json"), "w") as fh:
        json.dump([{"pid": 4242, "sessionId": RUNNING, "cwd": os.path.join(home, "code", "infra"),
                    "kind": "interactive", "name": "infra-7c", "status": "idle"}], fh)
    return paths


def env_for(home: str) -> dict:
    """Environment that points the tool at the fake HOME and pane."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CMUX_", "CLAUDE_", "XDG_", "MOVE_CLAUDE_SESSION_"))}
    env.update(HOME=home, CMUX_SURFACE_ID=SURFACE,
               TERM="xterm-256color", LANG="en_US.UTF-8")
    return env


if __name__ == "__main__":
    build(sys.argv[1])
    write_fake_claude(sys.argv[2])
