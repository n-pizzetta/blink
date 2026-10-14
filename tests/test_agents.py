import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import agents
import blink

FIXTURE = agents.ROOT / "tests/fixtures/cli.py"
# The pane's shell: it owns the terminal and outlives the agent it starts.
SHELL = ("import fcntl, subprocess, sys, termios, time; fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
         "code = subprocess.call(sys.argv[1:], pass_fds=(int(sys.argv[-1]),)); "
         "print(f'EXITED {code}', end='\\r\\n', flush=True); time.sleep(60)")
WAITING = b"\x1b]4;255;#01b1"
CLEAR = b"\x1b]104;255\x1b\\"


class NormalizeTests(unittest.TestCase):
    def test_real_permission_paths_for_all_three_agents(self):
        for agent in ("claude", "codex"):
            event = agents.normalize(agent, "PermissionRequest", {"tool_name": "Bash"})
            self.assertEqual((event["action"], event["reason"]), ("wait", "permission"))
        self.assertIsNone(agents.normalize("copilot", "permissionRequest", {"toolName": "bash"}))
        event = agents.normalize("copilot", "notification", {"notification_type": "permission_prompt"})
        self.assertEqual(event["reason"], "permission")

    def test_question_tools_turn_end_and_no_idle_alerts(self):
        for agent, tool in (("claude", "AskUserQuestion"), ("codex", "request_user_input"), ("copilot", "ask_user")):
            self.assertEqual(agents.normalize(agent, "PreToolUse", {"tool_name": tool})["reason"], "question")
            self.assertIsNone(agents.normalize(agent, "PreToolUse", {"tool_name": "view"}))
            for notification in ("idle_prompt", "agent_idle", "agent_completed", "shell_completed", "auth_success"):
                self.assertIsNone(agents.normalize(agent, "Notification", {"notification_type": notification}))
        for agent, event in (("claude", "Stop"), ("codex", "Stop"), ("copilot", "agentStop")):
            self.assertEqual(agents.normalize(agent, event, {})["reason"], "done")

    def test_subagent_events_are_ignored(self):
        self.assertIsNone(agents.normalize("claude", "PermissionRequest", {"agent_id": "sub"}))


class RequestTests(unittest.TestCase):
    def test_background_tool_completion_cannot_clear_another_request(self):
        requests = {}
        request = {"action": "wait", "reason": "permission", "tool_id": "id1", "tool": "Bash"}
        self.assertTrue(agents.apply(requests, request))
        agents.apply(requests, {"action": "tool_done", "tool_id": "id2", "tool": "Bash"})
        self.assertIn("permission", requests)
        self.assertFalse(agents.apply(requests, dict(request, tool_id="", tool="")))
        agents.apply(requests, {"action": "tool_done", "tool_id": "id1", "tool": "Bash"})
        self.assertEqual(requests, {})
        self.assertTrue(agents.apply(requests, dict(request, tool_id="id3")))

    def test_notification_then_named_permission_keeps_the_tool(self):
        requests = {}
        self.assertTrue(agents.apply(requests, {"action": "wait", "reason": "permission"}))
        self.assertFalse(agents.apply(requests, {"action": "wait", "reason": "permission", "tool": "Bash"}))
        agents.apply(requests, {"action": "tool_done", "tool": "Read"})
        self.assertIn("permission", requests)
        agents.apply(requests, {"action": "tool_done", "tool": "Bash"})
        self.assertEqual(requests, {})

    def test_copilot_can_signal_a_second_permission_in_the_same_turn(self):
        requests = {}
        agents.apply(requests, {"action": "wait", "reason": "permission"})
        agents.apply(requests, {"action": "tool_done", "tool": "bash"})
        self.assertEqual(requests, {})
        self.assertTrue(agents.apply(requests, {"action": "wait", "reason": "permission"}))

    def test_turn_end_and_new_prompt_clear_everything(self):
        for action in ("resume", "end", "start"):
            requests = {}
            agents.apply(requests, {"action": "wait", "reason": "question", "tool": "AskUserQuestion"})
            agents.apply(requests, {"action": action})
            self.assertEqual(requests, {})

    def test_turn_end_waits_until_next_prompt_or_new_work(self):
        requests = {}
        self.assertTrue(agents.apply(requests, {"action": "wait", "reason": "done"}))
        agents.apply(requests, {"action": "resume"})
        self.assertEqual(requests, {})
        agents.apply(requests, {"action": "wait", "reason": "done"})
        agents.apply(requests, {"action": "tool_done", "tool": "Bash"})
        self.assertEqual(requests, {})

    def test_terminal_kinds(self):
        self.assertEqual(agents.terminal_kind({"TERM_PROGRAM": "ghostty"}), "ghostty")
        self.assertEqual(agents.terminal_kind({"TERM_PROGRAM": "iTerm.app"}), "iterm")
        self.assertEqual(agents.terminal_kind({"TERM_PROGRAM": "Apple_Terminal"}), "bell")
        self.assertEqual(agents.terminal_kind({"TERM_PROGRAM": "ghostty", "TMUX": "/tmp/x"}), "bell")


class Agent:
    """A fake agent in its own pseudo-terminal, like a real pane."""

    def __init__(self, agent, home, terminal="ghostty"):
        self.master, self.slave = pty.openpty()
        read, self.control = os.pipe()
        environment = dict(os.environ, HOME=home, TERM_PROGRAM=terminal)
        for name in ("TMUX", "STY"):
            environment.pop(name, None)
        self.process = subprocess.Popen([sys.executable, "-c", SHELL, sys.executable, str(FIXTURE), agent, str(read)],
                                        stdin=self.slave, stdout=self.slave, stderr=self.slave,
                                        pass_fds=(read,), env=environment, start_new_session=True)
        os.close(read)
        self.output = b""
        self.expect(b"READY")

    def read(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.02)[0]:
                self.output += os.read(self.master, 65536)

    def expect(self, needle, timeout=5):
        deadline = time.monotonic() + timeout
        while needle not in self.output and time.monotonic() < deadline:
            self.read(0.05)
        if needle not in self.output:
            raise AssertionError(f"{needle!r} not in {self.output[-300:]!r}")
        before, _, after = self.output.partition(needle)
        self.output = after
        return before

    def send(self, command):
        os.write(self.control, command.encode() + b"\n")
        if command != "exit":
            # The watcher can react before the agent reports DONE: keep that output.
            before = self.expect(f"DONE {command}".encode())
            self.output = before + self.output
            return before

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
        os.close(self.master)
        self.process.wait()
        for fd in (self.slave, self.control):
            os.close(fd)


class HookTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="blink-home-")
        self.state = Path(self.home.name) / ".cache/blink"
        self.agents = []

    def tearDown(self):
        for agent in self.agents:
            agent.close()
        time.sleep(0.2)  # Let watchers notice their agent is gone.
        self.home.cleanup()

    def start(self, agent, terminal="ghostty"):
        process = Agent(agent, self.home.name, terminal)
        self.agents.append(process)
        return process

    def states(self):
        return list(self.state.glob("*.json"))

    def test_each_agent_pulses_its_own_pane_until_answered(self):
        for name in agents.AGENTS:
            with self.subTest(agent=name):
                agent = self.start(name)
                agent.send("wait")
                agent.expect(b"\a")
                for _ in range(3):  # An animation, not a single color.
                    agent.expect(WAITING)
                agent.send("answer")
                agent.expect(CLEAR)
                agent.read(0.3)
                self.assertNotIn(WAITING, agent.output)
                self.assertNotIn(b"HOOK_NOISE", agent.output)
                self.assertEqual(self.states(), [])

    def test_end_of_turn_pulses_until_the_next_prompt(self):
        for name in agents.AGENTS:
            with self.subTest(agent=name):
                agent = self.start(name)
                agent.send("done")
                agent.expect(WAITING)
                agent.send("answer")
                agent.expect(CLEAR)

    @unittest.skipIf(sys.platform.startswith("linux"),
                     "Linux updates a terminal's access time at most every 8 seconds")
    def test_typing_in_the_pane_stops_the_pulse_and_the_same_request_stays_quiet(self):
        agent = self.start("claude")
        agent.send("wait")
        agent.expect(WAITING)
        os.write(agent.master, b"y")
        agent.expect(CLEAR)
        agent.send("wait")
        agent.read(0.4)
        self.assertNotIn(WAITING, agent.output)
        state = json.loads(self.states()[0].read_text())
        self.assertTrue(state["viewed"])
        self.assertNotIn("PRIVATE", self.states()[0].read_text())
        agent.send("answer")
        agent.send("wait")
        agent.expect(WAITING)

    def test_agent_exit_clears_the_pane(self):
        agent = self.start("codex")
        agent.send("wait")
        agent.expect(WAITING)
        agent.send("exit")
        agent.expect(b"EXITED 7")
        agent.expect(CLEAR)
        agent.read(0.3)
        self.assertEqual(self.states(), [])

    def test_new_session_forgets_dead_and_replaced_ones(self):
        agent = self.start("copilot")
        finished = subprocess.Popen([sys.executable, "-c", ""])
        finished.wait()
        self.state.mkdir(parents=True)
        stale = {"dead": (finished.pid, "/dev/ttys999"), "replaced": (os.getpid(), os.ttyname(agent.slave)),
                 "other": (os.getpid(), "/dev/ttys999")}
        for session, (pid, tty) in stale.items():
            (self.state / f"{session}.json").write_text(json.dumps(
                {"agent_pid": pid, "tty": tty, "kind": "ghostty", "requests": {"done": {}},
                 "viewed": True, "watcher": None}))
        agent.send("done")
        agent.expect(WAITING)
        self.assertEqual(sorted(path.stem for path in self.states()), ["fixture-main", "other"])

    def test_iterm_gets_a_tab_color_and_badge(self):
        agent = self.start("claude", "iTerm.app")
        agent.send("wait")
        agent.expect(b"\x1b]6;1;bg;red;brightness;255\a")
        agent.expect(b"\x1b]1337;SetBadgeFormat=")
        agent.send("answer")
        agent.expect(b"\x1b]6;1;bg;*;default\a\x1b]1337;SetBadgeFormat=\a")

    def test_other_terminals_only_ring_the_bell(self):
        agent = self.start("copilot", "Apple_Terminal")
        before = agent.send("wait")
        self.assertIn(b"\a", before)
        agent.send("answer")
        agent.read(0.3)
        self.assertNotIn(b"\x1b]", agent.output)

    def test_headless_agent_without_terminal_never_lights_a_pane(self):
        # Like `claude -p` run by another agent's tool: no terminal of its own.
        code = ("import subprocess, sys; subprocess.run([sys.executable, sys.argv[1], 'claude', "
                "'PermissionRequest'], input=b'{\"session_id\": \"headless\"}')")
        result = subprocess.run([sys.executable, "-c", code, str(agents.ROOT / "agents.py")],
                                env=dict(os.environ, HOME=self.home.name, TERM_PROGRAM="ghostty"),
                                capture_output=True, start_new_session=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual(self.states(), [])

    def test_invalid_payload_is_silent(self):
        result = subprocess.run([sys.executable, str(agents.ROOT / "agents.py"), "copilot", "preToolUse"],
                                input=b"invalid json", capture_output=True,
                                env=dict(os.environ, HOME=self.home.name))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))


class InstallTests(unittest.TestCase):
    def test_generated_hooks_only_observe(self):
        hooks = [agents.grouped_hooks("claude", "/usr/bin/python3"),
                 agents.grouped_hooks("codex", "/usr/bin/python3"), agents.copilot_hooks("/usr/bin/python3")]
        text = json.dumps(hooks)
        for bad in ("--dangerously-bypass-hook-trust", "--allow-all", "--dangerously-skip-permissions",
                    "SessionStart", "sessionStart", "permissionRequest"):
            self.assertNotIn(bad, text)
        for group in hooks[0].values():
            self.assertTrue(blink.blink_group(group[0]))

    def test_merge_keeps_foreign_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "settings.json"
            foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}
            path.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [foreign]}}))
            for _ in range(2):
                blink.merge_hooks(path, agents.grouped_hooks("claude", "/usr/bin/python3"))
            data = json.loads(path.read_text())
            self.assertEqual(data["model"], "opus")
            self.assertEqual(data["hooks"]["PreToolUse"][0], foreign)
            self.assertEqual(len(data["hooks"]["PreToolUse"]), 2)
            self.assertEqual(len(data["hooks"]["Stop"]), 1)
            blink.merge_hooks(path, {})
            self.assertEqual(json.loads(path.read_text()), {"model": "opus", "hooks": {"PreToolUse": [foreign]}})

    def test_ghostty_block_is_replaced_then_removed(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config"
            path.write_text("theme = catppuccin-mocha\n")
            blink.set_ghostty_block(path, ["config-file = ?/old"])
            blink.set_ghostty_block(path, ["config-file = ?/new"])
            self.assertEqual(path.read_text(), "theme = catppuccin-mocha\n\n"
                             f"{blink.BEGIN}\nconfig-file = ?/new\n{blink.END}\n")
            blink.set_ghostty_block(path, [])
            self.assertEqual(path.read_text(), "theme = catppuccin-mocha\n")


if __name__ == "__main__":
    unittest.main()
