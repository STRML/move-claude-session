"""End-to-end tests: run the real script against a fake HOME, through a real pty.

    python3 -m unittest discover tests
"""

import fcntl
import json
import os
import pty
import select
import shlex
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

class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.paths = fixture.build(self.home)
        bindir = os.path.join(self.tmp, "bin")
        fixture.write_fake_claude(bindir)
        fixture.write_fake_remote_tools(bindir)   # never reach a real host or cmux
        self.remotes = os.path.join(self.tmp, "remotes")
        os.makedirs(self.remotes)
        self.env = fixture.env_for(self.home)
        self.env["FAKE_REMOTES"] = self.remotes
        # git must not find a repo above the temp dir, or read this machine's config.
        self.env.update(GIT_CEILING_DIRECTORIES=self.tmp, GIT_CONFIG_NOSYSTEM="1")
        self.env["PATH"] = bindir + os.pathsep + self.env.get("PATH", "/usr/bin:/bin")

    def tearDown(self):
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


class E2E(Harness):
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

    def test_single_target_skips_the_account_step(self):
        shutil.rmtree(os.path.join(self.home, ".claude-client"))   # now: default, work
        code, out = self.run_picker([("which session", ENTER)])
        self.assertEqual(code, 0, out)
        self.assertNotIn("which account", out)
        self.assertIsNotNone(self.transcript(".claude-work", S[1]), out)

    def test_account_step_preselects_the_last_target(self):
        code, out = self.run_picker([("which session", ENTER),               # S1
                                     ("which account", DOWN + ENTER)])       # work
        self.assertEqual(code, 0, out)
        code, out = self.run_picker([("which session", b"monthly"),          # S6, default
                                     ("1/", ENTER),
                                     ("which account", ENTER)])
        self.assertEqual(code, 0, out)
        self.assertIsNotNone(self.transcript(".claude-work", S[6]), out)

    def test_a_move_prints_one_summary_line_and_dry_run_lists_files(self):
        r = self.run_cli(S[4], "work")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("moved /review the fish prompt changes: default → work (3 items)", r.stdout)
        self.assertNotIn("file-history", r.stdout)
        r = self.run_cli(S[2], "default", "-n")
        self.assertIn("file-history", r.stdout)
        self.assertIn("dry run", r.stdout)

    def test_copy_option_is_gone(self):
        r = self.run_cli(S[4], "work", "--copy")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown option: --copy", r.stderr)

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

    def test_failed_copy_leaves_the_source_whole_and_no_partial_copy(self):
        src = os.path.join(self.home, ".claude", "file-history", S[1])
        os.makedirs(src, exist_ok=True)
        blocked = os.path.join(src, "unreadable")
        with open(blocked, "w") as fh:
            fh.write("x")
        os.chmod(blocked, 0)
        try:
            r = self.run_cli(S[1], "work")
        finally:
            os.chmod(blocked, 0o600)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing changed", r.stderr)
        self.assertEqual(self.transcript(".claude", S[1]), self.paths[S[1]])
        with open(self.paths[S[1]]) as fh:   # paths inside were not rewritten
            self.assertNotIn(".claude-work", fh.read())
        self.assertIsNone(self.transcript(".claude-work", S[1]))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude-work", "file-history", S[1])))

    def test_transcript_with_invalid_utf8_moves_with_bytes_intact(self):
        with open(self.paths[S[4]], "ab") as fh:
            fh.write(b'{"type":"user","message":{"content":"bad \xff\xfe byte"}}\n')
        r = self.run_cli(S[4], "work")
        self.assertEqual(r.returncode, 0, r.stderr)
        moved = self.transcript(".claude-work", S[4])
        with open(moved, "rb") as fh:
            data = fh.read()
        self.assertIn(b"bad \xff\xfe byte", data)
        self.assertIn(os.path.join(self.home, ".claude-work", "projects").encode(), data)
        self.assertIsNone(self.transcript(".claude", S[4]))

    def test_destination_collision_stops_before_copying_anything(self):
        clash = os.path.join(self.home, ".claude-work", "file-history", S[1])
        os.makedirs(clash)
        r = self.run_cli(S[1], "work")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stderr)
        self.assertIsNone(self.transcript(".claude-work", S[1]))

    def test_unreadable_running_list_blocks_the_move_unless_forced(self):
        open(os.path.join(self.home, "fake-agents-fail"), "w").close()
        r = self.run_cli(S[4], "work")
        self.assertEqual(r.returncode, 1)
        self.assertIn("can't tell which sessions are running", r.stderr)
        self.assertIn("not logged in", r.stderr)
        self.assertIsNotNone(self.transcript(".claude", S[4]))
        r = self.run_cli(S[4], "work", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_folder_group_matches_a_hashed_long_project_folder(self):
        deep = os.path.join(self.home, "code", *(["a-rather-long-directory-name"] * 8))
        os.makedirs(deep)
        name = fixture.project_folder(deep)
        self.assertGreater(len(name), 200)
        folder = os.path.join(self.home, ".claude", "projects", name[:200] + "-1a2b3c")
        os.makedirs(folder)
        sid = "cccccccc-0000-4000-8000-000000000001"
        with open(os.path.join(folder, f"{sid}.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "user", "cwd": deep, "message": {"content": "deep"}}) + "\n")
        os.utime(os.path.join(folder, f"{sid}.jsonl"), (1, 1))
        self.env["PWD"] = deep
        r = self.run_cli("--list", cwd=deep)
        self.assertIn(f"{sid}  in this folder", r.stdout)

    def test_cli_rejects_unknown_account(self):
        r = self.run_cli(S[1], "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("one of default, client, work", r.stderr)

    def test_recently_written_but_exited_session_moves_without_force(self):
        os.utime(self.paths[S[1]], None)   # written just now, no live process
        r = self.run_cli(S[1], "work")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_hosts_means_no_machine_step(self):
        os.makedirs(os.path.join(self.home, ".config", "move-claude-session"))
        with open(os.path.join(self.home, ".config", "move-claude-session", "hosts"), "w") as fh:
            fh.write("# nothing configured yet\n\n")
        code, out = self.run_picker([("which session", ENTER),
                                     ("which account", ENTER)])
        self.assertEqual(code, 0, out)
        self.assertNotIn("which machine", out)
        self.assertIsNotNone(self.transcript(".claude-client", S[1]), out)
        self.assertFalse(os.path.exists(os.path.join(self.remotes, "ssh.log")))


class RemoteHarness(Harness):
    """A fake host, studio2, reached through the fake ssh in tests/fixture.py."""

    def setUp(self):
        super().setUp()
        self.studio = fixture.build_remote(self.remotes, "studio2", [
            (".claude", "projects"), (".claude-sigint", "projects"), (".claude-rush", "fresh"),
            (".claude-code-router", "other")])
        self.hosts("studio1", "studio2")

    def hosts(self, *names):
        conf = os.path.join(self.home, ".config", "move-claude-session")
        os.makedirs(conf, exist_ok=True)
        with open(os.path.join(conf, "hosts"), "w") as fh:
            fh.write("# ssh aliases\n" + "".join(f"{n}\n" for n in names))

    def remote_transcript(self, account_dir, sid):
        hits = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.studio, account_dir))
                for f in fs if f == f"{sid}.jsonl"]
        return hits[0] if hits else None

    def ssh_log(self):
        try:
            with open(os.path.join(self.remotes, "ssh.log")) as fh:
                return fh.read()
        except FileNotFoundError:
            return ""

    def cmux_launched(self):
        with open(os.path.join(self.home, "cmux-launched")) as fh:
            return fh.read().splitlines()

    def resume_cmd(self, sid, account_dir, proj):
        cwd = os.path.join(self.home, "code", proj)
        root = os.path.join(self.studio, account_dir)
        env = ("env -u CLAUDE_CONFIG_DIR" if account_dir == ".claude"
               else f"env CLAUDE_CONFIG_DIR={shlex.quote(root)}")
        return f"cd {shlex.quote(cwd)} && {env} claude --resume {sid}"

    def assert_untouched_here(self, sid):
        self.assertEqual(self.transcript(".claude", sid), self.paths[sid])
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".claude", "file-history", sid)))
        with open(self.paths[sid]) as fh:
            self.assertNotIn(self.studio, fh.read())


class Remote(RemoteHarness):
    """Moves to another machine."""

    # ------------------------------------------------------------ picker

    def test_picker_moves_to_a_remote_account_and_resumes_in_cmux(self):
        code, out = self.run_picker([("which session", ENTER),               # S1, default
                                     ("which machine", DOWN + DOWN + ENTER),  # studio2
                                     ("account on studio2", DOWN + DOWN + ENTER)])
        self.assertEqual(code, 0, out)
        self.assertIn("no sessions yet", out)                 # rush has no projects/ yet
        moved = self.remote_transcript(".claude-sigint", S[1])
        self.assertIsNotNone(moved, out)
        self.assertIsNone(self.transcript(".claude", S[1]))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "file-history", S[1])))
        self.assertTrue(os.path.isdir(os.path.join(self.studio, ".claude-sigint", "file-history", S[1])))
        with open(moved) as fh:
            text = fh.read()
        self.assertIn(os.path.join(self.studio, ".claude-sigint", "projects"), text)
        self.assertNotIn(os.path.join(self.home, ".claude", "projects"), text)
        self.assertEqual([e for e in os.listdir(os.path.join(self.studio, ".claude-sigint"))
                          if e.startswith(".")], [])          # no staging dir left behind
        launched = self.cmux_launched()
        self.assertEqual(launched[:2], ["ssh", "studio2"])
        self.assertEqual(launched[launched.index("--command") + 1],
                         self.resume_cmd(S[1], ".claude-sigint", "billing-api"))
        self.assertIn(self.resume_cmd(S[1], ".claude-sigint", "billing-api"), out)
        log = self.ssh_log()
        self.assertIn("BatchMode=yes", log)
        self.assertIn("ConnectTimeout=", log)
        self.assertFalse(os.path.exists(os.path.join(self.home, "launched")))   # no local claude

    def test_machine_and_account_steps_preselect_the_last_choice(self):
        code, out = self.run_picker([("which session", ENTER),
                                     ("which machine", DOWN + DOWN + ENTER),
                                     ("account on studio2", DOWN + DOWN + ENTER)])
        self.assertEqual(code, 0, out)
        code, out = self.run_picker([("which session", b"monthly"),          # S6, default
                                     ("1/", ENTER),
                                     ("which machine", ENTER),
                                     ("account on studio2", ENTER)])
        self.assertEqual(code, 0, out)
        self.assertIsNotNone(self.remote_transcript(".claude-sigint", S[6]), out)

    def test_local_on_the_machine_step_is_todays_move(self):
        code, out = self.run_picker([("which session", ENTER),
                                     ("which machine", ENTER),               # local
                                     ("account?", ENTER)])
        self.assertEqual(code, 0, out)
        self.assertIsNotNone(self.transcript(".claude-client", S[1]), out)
        self.assertEqual(self.launched()["config"], os.path.join(self.home, ".claude-client"))
        self.assertEqual(self.ssh_log(), "")

    def test_hosts_can_come_from_the_environment(self):
        os.remove(os.path.join(self.home, ".config", "move-claude-session", "hosts"))
        self.env["MOVE_CLAUDE_SESSION_HOSTS"] = "studio2"
        code, out = self.run_picker([("which session", ENTER),
                                     ("which machine", DOWN + ENTER),
                                     ("account on studio2", ENTER)])   # default
        self.assertEqual(code, 0, out)
        self.assertIsNotNone(self.remote_transcript(".claude", S[1]), out)

    def test_outside_cmux_it_resumes_over_ssh_t(self):
        del self.env["CMUX_SURFACE_ID"]
        code, out = self.run_picker([("which session", ENTER),               # S1, newest
                                     ("which machine", DOWN + DOWN + ENTER),
                                     ("account on studio2", DOWN + DOWN + ENTER)])
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.home, "cmux-launched")))
        with open(os.path.join(self.remotes, "studio2.resumed")) as fh:
            resumed = fh.read()
        self.assertIn(self.resume_cmd(S[1], ".claude-sigint", "billing-api"), resumed)
        self.assertIn('"$SHELL" -lic', resumed)

    def test_missing_project_dir_on_the_remote_is_said_and_not_resumed(self):
        shutil.rmtree(os.path.join(self.home, "code", "billing-api"))
        code, out = self.run_picker([("which session", ENTER),
                                     ("which machine", DOWN + DOWN + ENTER),
                                     ("account on studio2", DOWN + DOWN + ENTER)])
        self.assertEqual(code, 0, out)
        self.assertIsNotNone(self.remote_transcript(".claude-sigint", S[1]), out)
        self.assertIn("does not exist on studio2", out)
        self.assertIn(self.resume_cmd(S[1], ".claude-sigint", "billing-api"), out)
        self.assertFalse(os.path.exists(os.path.join(self.home, "cmux-launched")))
        self.assertFalse(os.path.exists(os.path.join(self.remotes, "studio2.resumed")))

    # ------------------------------------------------------------ direct CLI

    def test_cli_account_at_host_moves_and_prints_the_command(self):
        r = self.run_cli(S[4], "sigint@studio2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("default → sigint@studio2 (3 items)", r.stdout)
        self.assertIsNotNone(self.remote_transcript(".claude-sigint", S[4]))
        self.assertIsNone(self.transcript(".claude", S[4]))
        self.assertIn("ssh -t studio2 ", r.stdout)
        self.assertIn(self.resume_cmd(S[4], ".claude-sigint", "dotfiles"), r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, "cmux-launched")))
        self.assertFalse(os.path.exists(os.path.join(self.remotes, "studio2.resumed")))

    def test_cli_moves_to_the_same_account_name_on_the_remote(self):
        r = self.run_cli(S[4], "default@studio2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(self.remote_transcript(".claude", S[4]))

    def test_cli_moves_into_an_account_with_no_projects_yet(self):
        r = self.run_cli(S[4], "rush@studio2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(self.remote_transcript(".claude-rush", S[4]))

    def test_cli_unknown_remote_account_lists_what_it_found(self):
        r = self.run_cli(S[4], "work@studio2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no account work on studio2 (one of default, rush, sigint)", r.stderr)
        self.assert_untouched_here(S[4])

    def test_unreachable_host_leaves_the_session_untouched(self):
        r = self.run_cli(S[4], "sigint@studio9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ssh: connect to host studio9", r.stderr)
        self.assertNotIn("Alarm clock", r.stderr)
        self.assert_untouched_here(S[4])

    def test_a_dropped_upload_changes_nothing_and_a_rerun_works(self):
        with open(os.path.join(self.remotes, "studio2.drop-on"), "w") as fh:
            fh.write("tar -x")
        r = self.run_cli(S[4], "sigint@studio2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing changed", r.stderr)
        self.assert_untouched_here(S[4])
        self.assertIsNone(self.remote_transcript(".claude-sigint", S[4]))
        os.remove(os.path.join(self.remotes, "studio2.drop-on"))
        r = self.run_cli(S[4], "sigint@studio2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(self.remote_transcript(".claude-sigint", S[4]))
        self.assertIsNone(self.transcript(".claude", S[4]))

    def test_a_corrupted_upload_is_refused_and_cleaned_up(self):
        open(os.path.join(self.remotes, "studio2.corrupt"), "w").close()
        r = self.run_cli(S[4], "sigint@studio2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("did not arrive intact", r.stderr)
        self.assertIn("nothing changed", r.stderr)
        self.assert_untouched_here(S[4])
        self.assertIsNone(self.remote_transcript(".claude-sigint", S[4]))
        self.assertEqual([e for e in os.listdir(os.path.join(self.studio, ".claude-sigint"))
                          if e.startswith(".")], [])

    def test_a_host_that_is_this_machine_is_refused_even_with_force(self):
        os.symlink(self.home, os.path.join(self.remotes, "loop"))   # like localhost
        r = self.run_cli(S[4], "default@loop", "--force")
        self.assertEqual(r.returncode, 1)
        self.assertIn("loop is this machine", r.stderr)
        self.assertEqual(self.transcript(".claude", S[4]), self.paths[S[4]])
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".claude", "file-history", S[4])))

    def test_a_failed_forced_move_puts_back_what_it_replaced(self):
        hist = os.path.join(self.studio, ".claude-sigint", "file-history")
        os.makedirs(os.path.join(hist, S[4]))
        with open(os.path.join(hist, S[4], "keep"), "w") as fh:
            fh.write("older copy\n")
        os.chmod(hist, 0o555)   # the last item can't be moved aside, so the move fails
        try:
            r = self.run_cli(S[4], "sigint@studio2", "--force")
        finally:
            os.chmod(hist, 0o755)
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not move", r.stderr)
        self.assert_untouched_here(S[4])
        self.assertTrue(os.path.isfile(os.path.join(hist, S[4], "keep")))
        self.assertIsNone(self.remote_transcript(".claude-sigint", S[4]))
        self.assertEqual([e for e in os.listdir(os.path.join(self.studio, ".claude-sigint"))
                          if e.startswith(".")], [])

    def test_a_rollback_that_cannot_undo_keeps_what_it_replaced(self):
        proj = os.path.basename(os.path.dirname(self.paths[S[4]]))
        locked = os.path.join(os.path.dirname(self.paths[S[4]]), S[4], "locked")
        os.makedirs(locked)
        open(os.path.join(locked, "f"), "w").close()
        os.chmod(locked, 0o555)   # its remote copy can't be emptied by the rollback
        sigint = os.path.join(self.studio, ".claude-sigint")
        older = os.path.join(sigint, "projects", proj, S[4])
        os.makedirs(older)
        open(os.path.join(older, "older"), "w").close()
        hist = os.path.join(sigint, "file-history")
        os.makedirs(os.path.join(hist, S[4]))
        os.chmod(hist, 0o555)   # the last item fails, so the move rolls back
        try:
            r = self.run_cli(S[4], "sigint@studio2", "--force")
        finally:
            for d, _, _ in os.walk(sigint):
                os.chmod(d, 0o755)
            os.chmod(locked, 0o755)
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not undo it all", r.stderr)
        self.assertNotIn("nothing changed", r.stderr)
        self.assert_untouched_here(S[4])
        kept = [os.path.join(d, f) for d, _, fs in os.walk(sigint) for f in fs if f == "older"]
        self.assertEqual(len(kept), 1, kept)
        self.assertIn(".partial.old", kept[0])
        r = self.run_cli(S[4], "sigint@studio2", "--force")   # a retry must not lose it
        self.assertEqual(r.returncode, 1)
        self.assertIn("an earlier failed move left", r.stderr)
        self.assertFalse([e for e in os.listdir(sigint) if e.endswith(".partial")])
        self.assertEqual(kept, [os.path.join(d, f) for d, _, fs in os.walk(sigint)
                                for f in fs if f == "older"])
        self.assert_untouched_here(S[4])

    def test_a_local_account_with_at_in_its_name_stays_local(self):
        os.makedirs(os.path.join(self.home, ".claude-sam@work", "projects"))
        os.makedirs(os.path.join(self.home, ".claude-sam", "projects"))
        r = self.run_cli(S[4], "sam@work")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(self.transcript(".claude-sam@work", S[4]))
        self.assertIsNone(self.transcript(".claude-sam", S[4]))
        self.assertEqual(self.ssh_log(), "")

    def test_a_bad_hosts_entry_is_skipped_not_fatal(self):
        self.hosts("studio2 (laptop)")
        code, out = self.run_picker([("which session", ENTER),
                                     ("which machine", DOWN + ENTER),
                                     ("account on studio2", ENTER)])
        self.assertEqual(code, 0, out)
        self.assertIn("skipped '(laptop)'", out)
        self.assertIsNotNone(self.remote_transcript(".claude", S[1]), out)

    def test_an_unconfirmed_commit_keeps_the_local_copy(self):
        with open(os.path.join(self.remotes, "studio2.drop-on"), "w") as fh:
            fh.write("MCS-COMMITTED")
        r = self.run_cli(S[4], "sigint@studio2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not confirm", r.stderr)
        self.assert_untouched_here(S[4])

    def test_remote_clash_stops_before_uploading(self):
        clash = os.path.join(self.studio, ".claude-sigint", "file-history", S[4])
        os.makedirs(clash)
        r = self.run_cli(S[4], "sigint@studio2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stderr)
        self.assertNotIn("tar -x", self.ssh_log())
        self.assert_untouched_here(S[4])
        r = self.run_cli(S[4], "sigint@studio2", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(self.remote_transcript(".claude-sigint", S[4]))

    def test_running_session_is_refused_before_uploading(self):
        r = self.run_cli(S[3], "sigint@studio2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("still running", r.stderr)
        self.assertNotIn("tar -x", self.ssh_log())

    def test_dry_run_lists_what_would_go_where_and_writes_nothing(self):
        r = self.run_cli(S[4], "sigint@studio2", "-n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("to sigint on studio2", r.stdout)
        self.assertIn(os.path.join(self.studio, ".claude-sigint"), r.stdout)
        self.assertIn("file-history", r.stdout)
        self.assertIn("dry run", r.stdout)
        self.assertNotIn("tar -x", self.ssh_log())
        self.assert_untouched_here(S[4])
        self.assertIsNone(self.remote_transcript(".claude-sigint", S[4]))
        self.assertEqual(sorted(os.listdir(os.path.join(self.studio, ".claude-sigint"))), ["projects"])


class Code(RemoteHarness):
    """A remote move carries the session's git checkout. The host's copy of a
    project lives under the remote home, mapped from the same absolute path here."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.remotes, "studio2.map"), "w") as fh:
            fh.write(f"{self.home}/code {self.studio}/code\n")
        self.repo = self.make_repo("dotfiles")   # S[4]'s project, in the default account

    # ------------------------------------------------------------ helpers

    def git(self, cwd, *args):
        env = {k: v for k, v in self.env.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=self.tmp)
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                            "-c", "init.defaultBranch=main", *args],
                           cwd=cwd, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
        return r.stdout.strip()

    def make_repo(self, proj):
        repo = os.path.join(self.home, "code", proj)
        self.origin = os.path.join(self.tmp, f"{proj}.git")
        self.git(self.tmp, "init", "-q", "--bare", self.origin)
        self.git(repo, "init", "-q")
        self.write(repo, ".gitignore", "*.log\n")
        for name in ("a.txt", "b.txt", "c.txt"):
            self.write(repo, name, f"{name} 1\n")
        self.commit(repo, "first")
        self.git(repo, "remote", "add", "origin", self.origin)
        self.git(repo, "push", "-q", "origin", "main")
        return repo

    def write(self, repo, name, text):
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def read(self, repo, name):
        with open(os.path.join(repo, name)) as fh:
            return fh.read()

    def commit(self, repo, msg, name="a.txt"):
        if msg != "first":
            self.write(repo, name, f"{msg}\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", msg)
        return self.head(repo)

    def head(self, repo):
        return self.git(repo, "rev-parse", "HEAD")

    def host_path(self, proj="dotfiles"):
        return os.path.join(self.studio, "code", proj)

    def host_clone(self, proj="dotfiles"):
        self.git(self.tmp, "clone", "-q", self.origin, self.host_path(proj))
        return self.host_path(proj)

    def git_state(self, git_dir):
        """Every file under a .git dir with its content, to show nothing was written."""
        state = {}
        for d, _, files in os.walk(git_dir):
            for f in files:
                path = os.path.join(d, f)
                with open(path, "rb") as fh:
                    state[os.path.relpath(path, git_dir)] = fh.read()
        return state

    def applied(self):
        return "MCS-CODE-DONE" in self.ssh_log()

    def move(self, *extra):
        return self.run_cli(S[4], "sigint@studio2", *extra)

    def fail_host_git_on(self, word):
        with open(os.path.join(self.remotes, "studio2.git-fail"), "w") as fh:
            fh.write(word)

    # ------------------------------------------------------------ carrying it

    def test_a_clean_repo_at_the_same_commit_is_left_as_it_is(self):
        host = self.host_clone()
        before = self.git_state(os.path.join(host, ".git"))
        r = self.move()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("is already on studio2", r.stdout)
        self.assertFalse(self.applied())
        self.assertEqual(self.git_state(os.path.join(host, ".git")), before)
        self.assertIsNotNone(self.remote_transcript(".claude-sigint", S[4]))

    def test_new_commits_are_carried_and_the_branch_fast_forwarded(self):
        host = self.host_clone()
        self.commit(self.repo, "second")
        head = self.commit(self.repo, "third")
        r = self.move()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("carried the code to studio2: main at", r.stdout)
        self.assertIn("2 commits", r.stdout)
        self.assertEqual(self.head(host), head)
        self.assertEqual(self.git(host, "symbolic-ref", "HEAD"), "refs/heads/main")
        self.assertEqual(self.git(host, "status", "--porcelain"), "")

    def test_modified_untracked_staged_and_deleted_files_are_carried(self):
        host = self.host_clone()
        self.write(self.repo, "a.txt", "changed here\n")
        os.remove(os.path.join(self.repo, "b.txt"))
        self.write(self.repo, "notes/new.txt", "untracked\n")
        self.write(self.repo, "staged.txt", "staged\n")
        self.git(self.repo, "add", "staged.txt")
        self.write(self.repo, "debug.log", "ignored\n")
        status = self.git(self.repo, "status", "--porcelain")
        before = self.git_state(os.path.join(self.repo, ".git"))
        r = self.move()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("4 changed files", r.stdout)
        self.assertEqual(self.read(host, "a.txt"), "changed here\n")
        self.assertFalse(os.path.exists(os.path.join(host, "b.txt")))
        self.assertEqual(self.read(host, "notes/new.txt"), "untracked\n")
        self.assertEqual(self.read(host, "staged.txt"), "staged\n")
        self.assertFalse(os.path.exists(os.path.join(host, "debug.log")))
        self.assertEqual(self.head(host), self.head(self.repo))
        # The repo here is only read: not one byte under .git changes, nor its status.
        self.assertEqual(self.git_state(os.path.join(self.repo, ".git")), before)
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), status)

    def test_a_repo_missing_on_the_host_is_made_from_the_bundle(self):
        self.commit(self.repo, "second")
        self.write(self.repo, "wip.txt", "wip\n")
        r = self.move()
        self.assertEqual(r.returncode, 0, r.stderr)
        host = self.host_path()
        self.assertEqual(self.head(host), self.head(self.repo))
        self.assertEqual(self.git(host, "symbolic-ref", "HEAD"), "refs/heads/main")
        self.assertEqual(self.git(host, "remote", "get-url", "origin"), self.origin)
        self.assertEqual(self.read(host, "wip.txt"), "wip\n")
        self.assertEqual(self.git(host, "rev-list", "--count", "HEAD"), "2")

    def test_a_detached_head_stays_detached(self):
        host = self.host_clone()
        head = self.commit(self.repo, "second")
        self.git(self.repo, "checkout", "-q", "--detach")
        r = self.move()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(host), head)
        self.assertEqual(self.git(host, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertNotEqual(self.git(host, "rev-parse", "main"), head)   # main not moved

    def test_a_linked_worktree_is_added_beside_the_main_repo_there(self):
        main = os.path.join(self.home, "code", "dotfiles-main")
        shutil.move(self.repo, main)
        self.git(main, "worktree", "add", "-q", "-b", "feat", self.repo)
        head = self.commit(self.repo, "on feat")
        self.write(self.repo, "wip.txt", "wip\n")
        before = self.git_state(os.path.join(main, ".git"))
        host_main = self.host_clone("dotfiles-main")
        r = self.move()
        self.assertEqual(r.returncode, 0, r.stderr)
        host = self.host_path()
        self.assertEqual(self.head(host), head)
        self.assertEqual(self.git(host, "symbolic-ref", "HEAD"), "refs/heads/feat")
        self.assertEqual(self.read(host, "wip.txt"), "wip\n")
        self.assertIn(host, self.git(host_main, "worktree", "list"))
        self.assertEqual(self.git_state(os.path.join(main, ".git")), before)

    def test_a_linked_worktree_whose_main_repo_is_not_there_is_refused(self):
        main = os.path.join(self.home, "code", "dotfiles-main")
        shutil.move(self.repo, main)
        self.git(main, "worktree", "add", "-q", "-b", "feat", self.repo)
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"is a worktree of {main}, which is not on studio2", r.stderr)
        self.assertNotIn("tar -x", self.ssh_log())
        self.assert_untouched_here(S[4])

    def test_a_repo_nested_in_the_working_tree_is_skipped(self):
        self.host_clone()
        nested = os.path.join(self.repo, "vendor", "lib")
        self.git(self.tmp, "init", "-q", nested)
        self.commit(nested, "nested")
        r = self.move()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skipped vendor/lib: a repo of its own", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.host_path(), "vendor")))

    # ------------------------------------------------------------ refusing

    def test_a_host_with_uncommitted_changes_is_refused_and_force_stashes_them(self):
        host = self.host_clone()
        self.write(host, "a.txt", "edited on the host\n")
        head = self.commit(self.repo, "second", name="c.txt")
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertIn("has uncommitted changes (a.txt)", r.stderr)
        self.assertIn("--force", r.stderr)
        self.assertFalse(self.applied())
        self.assertEqual(self.read(host, "a.txt"), "edited on the host\n")
        self.assert_untouched_here(S[4])
        r = self.move("--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f'as "move-claude-session {S[4]}', r.stdout)
        self.assertEqual(self.head(host), head)
        self.assertEqual(self.read(host, "a.txt"), "a.txt 1\n")
        self.assertIn(f"move-claude-session {S[4]}", self.git(host, "stash", "list"))
        self.assertIn("edited on the host", self.git(host, "stash", "show", "-p"))

    def test_a_diverged_branch_on_the_host_is_refused_even_with_force(self):
        host = self.host_clone()
        theirs = self.commit(host, "made on the host", name="c.txt")
        self.commit(self.repo, "made here")
        r = self.move("--force")
        self.assertEqual(r.returncode, 1)
        self.assertIn("main on studio2 has commits that it lacks here", r.stderr)
        self.assertFalse(self.applied())
        self.assertEqual(self.head(host), theirs)
        self.assert_untouched_here(S[4])

    def test_a_host_branch_ahead_of_this_one_is_refused(self):
        host = self.host_clone()
        ahead = self.commit(host, "only on the host")
        r = self.move("--force")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has commits that it lacks here", r.stderr)
        self.assertEqual(self.head(host), ahead)

    def test_a_path_that_is_not_this_repo_on_the_host_is_refused(self):
        self.write(self.host_path(), "readme", "something else\n")
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertIn("exists on studio2 and is not a checkout of this repo", r.stderr)
        shutil.rmtree(self.host_path())
        other = self.host_path()
        self.git(self.tmp, "init", "-q", other)
        self.commit(other, "unrelated", name="x")
        r = self.move("--force")
        self.assertEqual(r.returncode, 1)
        self.assertIn("is a different repo", r.stderr)
        self.assertFalse(self.applied())
        self.assert_untouched_here(S[4])

    def test_an_ignored_file_on_the_host_is_not_overwritten(self):
        host = self.host_clone()
        with open(os.path.join(host, ".git", "info", "exclude"), "a") as fh:
            fh.write("secret.txt\n")
        self.write(host, "secret.txt", "only on the host\n")
        self.write(self.repo, "secret.txt", "from here\n")
        r = self.move("--force")
        self.assertEqual(r.returncode, 1)
        self.assertIn("secret.txt exists on the host as an ignored file", r.stderr)
        self.assertEqual(self.read(host, "secret.txt"), "only on the host\n")
        self.assert_untouched_here(S[4])

    def test_an_ignored_file_a_carried_commit_adds_is_not_overwritten(self):
        host = self.host_clone()
        with open(os.path.join(host, ".git", "info", "exclude"), "a") as fh:
            fh.write("secret.txt\n")
        self.write(host, "secret.txt", "only on the host\n")
        self.commit(self.repo, "add secret", name="secret.txt")
        r = self.move("--force")
        self.assertEqual(r.returncode, 1)
        self.assertIn("secret.txt exists on the host as an ignored file", r.stderr)
        self.assertEqual(self.read(host, "secret.txt"), "only on the host\n")

    def test_a_host_whose_git_status_fails_is_not_read_as_clean(self):
        host = self.host_clone()
        self.write(host, "a.txt", "edited on the host\n")
        self.fail_host_git_on("status")
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("is already on studio2", r.stdout)
        self.assertEqual(self.read(host, "a.txt"), "edited on the host\n")
        self.assert_untouched_here(S[4])

    def test_a_repo_git_cannot_read_is_refused_not_skipped(self):
        with open(os.path.join(self.repo, ".git", "config"), "a") as fh:
            fh.write("[broken\n")
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertIn("can't read the git checkout", r.stderr)
        self.assertIn("--no-code", r.stderr)
        self.assert_untouched_here(S[4])

    def test_submodules_and_lfs_are_refused_before_anything_is_sent(self):
        sha = self.head(self.repo)
        self.git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},sub")
        self.git(self.repo, "commit", "-q", "-m", "submodule")
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertIn("has submodules", r.stderr)
        self.assertIn("--no-code", r.stderr)
        self.assertNotIn("MCS-AT", self.ssh_log())
        self.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        self.write(self.repo, ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
        self.write(self.repo, "x.bin", "data\n")
        self.commit(self.repo, "lfs", name="c.txt")
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertIn("uses Git LFS", r.stderr)
        self.assertNotIn("MCS-AT", self.ssh_log())
        self.assert_untouched_here(S[4])

    # ------------------------------------------------------------ failures

    def test_a_failed_checkout_on_the_host_puts_it_back_as_it_was(self):
        host = self.host_clone()
        before = self.head(host)
        self.write(host, "a.txt", "edited on the host\n")
        self.commit(self.repo, "second", name="c.txt")
        self.write(self.repo, "new.txt", "new\n")
        self.fail_host_git_on("restore")
        r = self.move("--force")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing changed", r.stderr)
        self.assertIn("injected failure", r.stderr)
        self.assertEqual(self.head(host), before)
        self.assertEqual(self.git(host, "rev-parse", "main"), before)
        self.assertEqual(self.read(host, "a.txt"), "edited on the host\n")
        self.assertEqual(self.git(host, "status", "--porcelain"), "M a.txt")
        self.assertEqual(self.git(host, "stash", "list"), "")
        self.assert_untouched_here(S[4])
        self.assertIsNone(self.remote_transcript(".claude-sigint", S[4]))

    def test_a_failed_checkout_keeps_host_files_its_own_head_tracks(self):
        host = self.host_clone()   # tracks b.txt
        self.git(self.repo, "rm", "-q", "b.txt")
        self.commit(self.repo, "drop b")
        self.write(self.repo, "b.txt", "untracked here now\n")
        self.fail_host_git_on("restore")
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing changed", r.stderr)
        self.assertEqual(self.read(host, "b.txt"), "b.txt 1\n")
        self.assertEqual(self.git(host, "status", "--porcelain"), "")

    def test_a_failed_checkout_into_an_empty_host_repo_leaves_it_empty(self):
        host = self.host_path()
        self.git(self.tmp, "init", "-q", host)
        self.git(host, "remote", "add", "origin", self.origin)
        self.fail_host_git_on("restore")
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing changed", r.stderr)
        self.assertEqual(os.listdir(host), [".git"])
        self.assertEqual(self.git(host, "for-each-ref"), "")
        self.assertEqual(self.git(host, "ls-files"), "")

    def test_a_failed_new_repo_on_the_host_is_removed(self):
        self.fail_host_git_on("restore")
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing changed", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.studio, "code")))
        self.assert_untouched_here(S[4])

    def test_a_dropped_connection_while_carrying_the_code_keeps_the_session(self):
        host = self.host_clone()
        before = self.head(host)
        self.commit(self.repo, "second")
        with open(os.path.join(self.remotes, "studio2.drop-on"), "w") as fh:
            fh.write("MCS-CODE-DONE")
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.head(host), before)   # the upload was cut, so it never applied
        self.assert_untouched_here(S[4])

    def test_a_rerun_after_the_session_failed_to_land_finds_the_code_there(self):
        host = self.host_clone()
        head = self.commit(self.repo, "second")
        self.write(self.repo, "wip.txt", "wip\n")
        open(os.path.join(self.remotes, "studio2.corrupt"), "w").close()
        r = self.move()
        self.assertEqual(r.returncode, 1)
        self.assertIn("did not arrive intact", r.stderr)
        self.assertIn("carried the code", r.stdout)
        self.assertEqual(self.head(host), head)
        self.assert_untouched_here(S[4])
        os.remove(os.path.join(self.remotes, "studio2.corrupt"))
        r = self.move()   # the host now has uncommitted changes: the ones carried
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("is already on studio2", r.stdout)
        self.assertIsNotNone(self.remote_transcript(".claude-sigint", S[4]))

    # ------------------------------------------------------------ options

    def test_no_code_moves_only_the_session(self):
        host = self.host_clone()
        before = self.head(host)
        self.commit(self.repo, "second")
        self.write(self.repo, "wip.txt", "wip\n")
        r = self.move("--no-code")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("MCS-AT", self.ssh_log())
        self.assertEqual(self.head(host), before)
        self.assertFalse(os.path.exists(os.path.join(host, "wip.txt")))
        self.assertIsNotNone(self.remote_transcript(".claude-sigint", S[4]))

    def test_a_folder_that_is_not_a_repo_moves_as_before(self):
        r = self.run_cli(S[1], "sigint@studio2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("MCS-AT", self.ssh_log())
        self.assertNotIn("carried the code", r.stdout)

    def test_dry_run_lists_the_commits_and_files_and_changes_nothing(self):
        host = self.host_clone()
        before = self.head(host)
        self.commit(self.repo, "second")
        self.commit(self.repo, "third")
        self.write(self.repo, "a.txt", "changed\n")
        os.remove(os.path.join(self.repo, "b.txt"))
        self.write(self.repo, "new.txt", "new\n")
        r = self.move("-n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"would carry the code in {self.repo} (main at", r.stdout)
        self.assertIn("fast-forwarding main from", r.stdout)
        self.assertIn("2 commits to send", r.stdout)
        self.assertIn(" third\n", r.stdout)
        self.assertIn("3 changed files", r.stdout)
        for line in ("M  a.txt", "D  b.txt", "A  new.txt"):
            self.assertIn(line, r.stdout)
        self.assertFalse(self.applied())
        self.assertNotIn("tar -x", self.ssh_log())
        self.assertEqual(self.head(host), before)
        self.assert_untouched_here(S[4])

    PICK_STUDIO2_SIGINT = [("which session", ENTER), ("which machine", DOWN + DOWN + ENTER),
                           ("account on studio2", DOWN + DOWN + ENTER)]

    def test_the_picker_carries_the_code_and_resumes_there(self):
        repo = self.make_repo("billing-api")   # S[1], the row the picker starts on
        code, out = self.run_picker(self.PICK_STUDIO2_SIGINT)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(self.host_path("billing-api")), self.head(repo))
        self.assertEqual(self.cmux_launched()[:2], ["ssh", "studio2"])

    def test_the_picker_passes_no_code_through(self):
        self.make_repo("billing-api")
        code, out = self.run_picker(self.PICK_STUDIO2_SIGINT, "--no-code")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.host_path("billing-api")))
        self.assertIn("does not exist on studio2", out)


if __name__ == "__main__":
    unittest.main()
