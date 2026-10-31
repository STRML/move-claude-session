"""Build a fake HOME with three Claude Code accounts and made-up sessions.

The tests use it, and so do the README screenshots (`demo/demo.tape`):

    python3 tests/fixture.py /tmp/demo-home

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
RUNNING = "aaaaaaaa-0000-4000-8000-000000000003"   # registered by a live process

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


def build(home: str, live_pid: int = 0) -> dict:
    """Create the fake HOME. `live_pid` registers RUNNING as alive under that pid.
    Returns {session_id: transcript path}."""
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
    if live_pid:
        sessions = os.path.join(home, ".claude-work", "sessions")
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, f"{live_pid}.json"), "w") as fh:
            json.dump({"pid": live_pid, "sessionId": RUNNING}, fh)
    return paths


def env_for(home: str) -> dict:
    """Environment that points the tool at the fake HOME and pane."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CMUX_", "CLAUDE_", "XDG_"))}
    env.update(HOME=home, CMUX_SURFACE_ID=SURFACE,
               TERM="xterm-256color", LANG="en_US.UTF-8")
    return env


if __name__ == "__main__":
    build(sys.argv[1], live_pid=int(sys.argv[2]) if len(sys.argv) > 2 else 0)
