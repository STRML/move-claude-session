#!/usr/bin/env python3
"""Check, against the real `claude`, that a session you quit stops counting as running.

move-claude-session decides "running" from `claude agents --json`. This script
resumes one of your sessions in a pseudo-terminal, waits until `claude agents
--json` lists it, quits it with /exit, and checks the list again right away.
No prompt is sent, so it uses no tokens. Run it after a Claude Code update.

    cd <the session's project directory>     # a folder Claude Code already trusts
    python3 tools/live-quit-check.py <session-id>

Exit status 0 means the session dropped off the list after quitting.
"""

import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time


def agents():
    out = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


def drain(fd, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                os.read(fd, 65536)
            except OSError:
                return


def main(session_id):
    pid, fd = pty.fork()
    if pid == 0:
        # A claude started from inside a Claude Code shell doesn't register as a
        # session, so drop the parent's CLAUDE*/CMUX* variables (but keep the account).
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "CMUX")) or k == "CLAUDE_CONFIG_DIR"}
        os.execvpe("claude", ["claude", "--resume", session_id], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    for _ in range(60):
        drain(fd, 0.5)
        if any(r.get("sessionId") == session_id for r in agents()):
            break
    else:
        os.kill(pid, 9)
        sys.exit("FAIL: the session never appeared in `claude agents --json` "
                 "(a trust or resume prompt may be waiting; run claude here once by hand)")
    print("listed while running ✓")

    os.write(fd, b"/exit")
    drain(fd, 0.5)
    os.write(fd, b"\r")
    started = time.time()
    while not os.waitpid(pid, os.WNOHANG)[0]:
        drain(fd, 0.1)
        if time.time() - started > 20:
            os.kill(pid, 9)
            sys.exit("FAIL: claude did not exit after /exit")
    still = [r for r in agents() if r.get("sessionId") == session_id]
    if still:
        sys.exit(f"FAIL: still listed after exit: {still}")
    print(f"gone {time.time() - started:.1f}s after /exit ✓")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
