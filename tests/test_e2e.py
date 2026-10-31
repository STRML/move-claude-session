"""End-to-end tests: run the real script against a fake HOME, through a real pty.

    python3 -m unittest discover tests
"""

import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import fixture  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "move-claude-session")
S = {i + 1: s[0] for i, s in enumerate(fixture.SESSIONS)}   # S[1] .. S[6]

ENTER, ESC, DOWN = b"\r", b"\x1b", b"\x1bOB"   # keypad mode sends ESC O B

FAKE_CLAUDE = """#!/bin/sh
{ echo "argv=$*"; echo "cwd=$(pwd)"; echo "config=${CLAUDE_CONFIG_DIR-<unset>}"; } > "$HOME/launched"
"""


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.realpath(os.path.join(self.tmp, "home"))
        os.makedirs(self.home)
        self.live = subprocess.Popen(["sleep", "60"])
        self.paths = fixture.build(self.home, live_pid=self.live.pid)
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as fh:
            fh.write(FAKE_CLAUDE)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.env = fixture.env_for(self.home)
        self.env["PATH"] = bindir + os.pathsep + self.env.get("PATH", "/usr/bin:/bin")

    def tearDown(self):
        self.live.kill()
        self.live.wait()
        shutil.rmtree(self.tmp)

    # ------------------------------------------------------------ helpers

    def run_cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, SCRIPT, *args], env=self.env, cwd=cwd,
                              capture_output=True, text=True, timeout=30)

    def run_picker(self, steps, *args):
        """steps: [(text to wait for, keys to send)]. Returns (exit code, output)."""
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, SCRIPT, *args], self.env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 140, 0, 0))
        out = b""

        def read_until(needle, timeout=10):
            nonlocal out
            end = time.time() + timeout
            while needle.encode() not in out:
                if time.time() > end:
                    raise AssertionError(f"never saw {needle!r} in:\n{out.decode(errors='replace')}")
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready:
                    try:
                        out += os.read(fd, 65536)
                    except OSError:
                        break

        for needle, keys in steps:
            read_until(needle)
            time.sleep(0.15)
            os.write(fd, keys)
        end, status = time.time() + 10, None
        while status is None and time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    pass
            done, st = os.waitpid(pid, os.WNOHANG)
            status = st if done else None
        if status is None:
            os.kill(pid, 9)
            _, status = os.waitpid(pid, 0)
        os.close(fd)
        return os.waitstatus_to_exitcode(status), out.decode(errors="replace")

    def launched(self):
        with open(os.path.join(self.home, "launched")) as fh:
            return dict(line.rstrip("\n").split("=", 1) for line in fh)

    def transcript(self, account_dir, sid):
        hits = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.home, account_dir))
                for f in fs if f == f"{sid}.jsonl"]
        return hits[0] if hits else None

    # ------------------------------------------------------------ ranking

    def test_list_groups_terminal_then_folder_then_all(self):
        r = self.run_cli("--list", cwd=os.path.join(self.home, "code", "billing-api"))
        self.assertEqual(r.returncode, 0, r.stderr)
        groups = ("in this terminal", "in this folder", "all sessions")
        rows = [(line[:36], next(g for g in groups if line[38:].startswith(g)))
                for line in r.stdout.splitlines()]
        self.assertEqual(rows, [
            (S[1], "in this terminal"), (S[5], "in this terminal"),   # S5 is a day old
            (S[6], "in this folder"),
            (S[2], "all sessions"), (S[3], "all sessions"), (S[4], "all sessions"),
        ])
        self.assertIn("running", r.stdout.splitlines()[4])
        self.assertNotIn("system-reminder", r.stdout)

    def test_folder_group_matches_a_session_recorded_under_a_symlink(self):
        link = os.path.join(self.tmp, "link-to-billing")   # like /tmp -> /private/tmp
        os.symlink(os.path.join(self.home, "code", "billing-api"), link)
        sid = "bbbbbbbb-0000-4000-8000-000000000001"
        folder = os.path.join(self.home, ".claude", "projects", fixture.project_folder(link))
        os.makedirs(folder)
        with open(os.path.join(folder, f"{sid}.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "user", "cwd": link,
                                 "message": {"content": "hi"}}) + "\n")
        os.utime(os.path.join(folder, f"{sid}.jsonl"), (1, 1))   # oldest of all
        self.env["PWD"] = link
        r = self.run_cli("--list", cwd=link)
        self.assertIn(f"{sid}  in this folder", r.stdout)
        self.env["PWD"] = os.path.join(self.home, "code", "dashboard")   # stale $PWD
        r = self.run_cli("--list", cwd=link)
        self.assertIn(f"{sid}  all sessions", r.stdout)   # stale $PWD is ignored
        self.assertIn(f"{S[6]}  in this folder", r.stdout)   # real path still matches

    def test_without_cmux_there_is_no_terminal_group(self):
        del self.env["CMUX_SURFACE_ID"]
        r = self.run_cli("--list")
        self.assertNotIn("in this terminal", r.stdout)
        self.assertEqual(r.stdout.splitlines()[0].split()[0], S[1])   # newest first

    def test_titles_follow_rename_then_ai_title_then_first_prompt(self):
        rows = {line.split()[0]: line for line in self.run_cli("--list").stdout.splitlines()}
        self.assertTrue(rows[S[1]].endswith("Add retry with backoff to the invoice webhook"))
        self.assertTrue(rows[S[4]].endswith("/review the fish prompt changes"))
        self.assertTrue(rows[S[6]].endswith("Write the monthly usage report query"))
        self.assertNotIn("stale generated title", "".join(rows.values()))
        self.assertNotIn("first prompt of", "".join(rows.values()))

    def test_label_cache_refreshes_when_a_transcript_changes(self):
        self.run_cli("--list")
        self.assertTrue(os.path.isfile(os.path.join(
            self.home, ".cache", "move-claude-session", "labels.json")))
        with open(self.paths[S[2]], "a") as fh:
            fh.write(json.dumps({"type": "ai-title", "aiTitle": "Renamed by Claude"},
                                separators=(",", ":")) + "\n")
        rows = {line.split()[0]: line for line in self.run_cli("--list").stdout.splitlines()}
        self.assertTrue(rows[S[2]].endswith("Renamed by Claude"))

    def test_label_cache_keeps_other_folders_and_drops_moved_transcripts(self):
        cache = os.path.join(self.home, ".cache", "move-claude-session", "labels.json")
        self.env["CMUX_SURFACE_ID"] = "none"
        self.run_cli("--list")
        with open(cache) as fh:
            before = set(json.load(fh))
        self.run_cli(S[4], "work")   # moves S4's transcript away
        self.run_cli("--list", cwd=os.path.join(self.home, "code", "dashboard"))
        with open(cache) as fh:
            after = set(json.load(fh))
        self.assertNotIn(self.paths[S[4]], after)
        self.assertEqual(before - {self.paths[S[4]]}, after - {self.transcript(".claude-work", S[4])})

    # ------------------------------------------------------------ picker

    def test_enter_enter_moves_this_panes_session_and_resumes_it(self):
        code, out = self.run_picker([("which session", ENTER),
                                     ("which account", ENTER)])
        self.assertEqual(code, 0, out)
        moved = self.transcript(".claude-client", S[1])
        self.assertIsNotNone(moved, out)
        self.assertIsNone(self.transcript(".claude", S[1]))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".claude-client", "file-history", S[1])))
        with open(moved) as fh:
            text = fh.read()
        self.assertIn(os.path.join(self.home, ".claude-client", "projects"), text)
        self.assertNotIn(os.path.join(self.home, ".claude", "projects"), text)
        got = self.launched()
        self.assertEqual(got["argv"], f"--resume {S[1]}")
        self.assertEqual(got["cwd"], os.path.join(self.home, "code", "billing-api"))
        self.assertEqual(got["config"], os.path.join(self.home, ".claude-client"))

    def test_filter_and_arrow_pick_another_session_and_account(self):
        code, out = self.run_picker([("which session", b"contact"),
                                     ("1/6", ENTER),
                                     ("which account", DOWN + ENTER)])
        self.assertEqual(code, 0, out)
        self.assertIsNotNone(self.transcript(".claude-work", S[5]), out)
        self.assertEqual(self.launched()["cwd"], os.path.join(self.home, "code", "acme-site"))

    def test_moving_to_default_unsets_config_dir(self):
        self.env["CLAUDE_CONFIG_DIR"] = os.path.join(self.home, ".claude-work")
        code, out = self.run_picker([("which session", DOWN + ENTER),   # S5, in client
                                     ("which account", ENTER)])
        self.assertEqual(code, 0, out)
        self.assertIsNotNone(self.transcript(".claude", S[5]), out)
        self.assertEqual(self.launched()["config"], "<unset>")

    def test_running_session_cannot_be_picked(self):
        code, out = self.run_picker([("which session", DOWN * 3 + ENTER),   # S3
                                     ("running sessions can't move", ESC)])
        self.assertEqual(code, 1, out)
        self.assertIsNotNone(self.transcript(".claude-work", S[3]))
        self.assertFalse(os.path.exists(os.path.join(self.home, "launched")))

    def test_escape_on_account_step_changes_nothing(self):
        code, out = self.run_picker([("which session", ENTER),
                                     ("which account", ESC)])
        self.assertEqual(code, 1, out)
        self.assertIsNotNone(self.transcript(".claude", S[1]))

    def test_dry_run_picker_moves_nothing_and_launches_nothing(self):
        code, out = self.run_picker([("which session", ENTER),
                                     ("which account", ENTER)], "-n")
        self.assertEqual(code, 0, out)
        self.assertIn("dry run", out)
        self.assertIsNotNone(self.transcript(".claude", S[1]))
        self.assertFalse(os.path.exists(os.path.join(self.home, "launched")))

    # ------------------------------------------------------------ direct CLI

    def test_cli_moves_by_id_and_prints_resume_command(self):
        r = self.run_cli(S[4], "work")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(self.transcript(".claude-work", S[4]))
        work = os.path.join(self.home, ".claude-work")
        self.assertIn(f"CLAUDE_CONFIG_DIR={work} claude --resume {S[4]}", r.stdout)

    def test_cli_refuses_running_session_without_force(self):
        r = self.run_cli(S[3], "default")
        self.assertEqual(r.returncode, 1)
        self.assertIn("still running", r.stderr)
        r = self.run_cli(S[3], "default", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(self.transcript(".claude", S[3]))

    def test_cli_rejects_unknown_account(self):
        r = self.run_cli(S[1], "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("one of default, client, work", r.stderr)

    def test_recently_written_but_exited_session_moves_without_force(self):
        os.utime(self.paths[S[1]], None)   # written just now, no live process
        r = self.run_cli(S[1], "work")
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
