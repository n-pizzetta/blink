import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import blink

# Any intensity of the waiting marker read by shaders/attention.glsl.
WAITING = b"\x1b]4;255;#01b1"


class ProtocolTests(unittest.TestCase):
    def test_focus_and_palette_split_at_every_possible_boundary(self):
        data = b"\x1b[O\x1b]4;255;rgb:aaaa/bbbb/cccc\x1b\\\x1b[I"
        expected = [("focus", b"O"), ("palette", b"rgb:aaaa/bbbb/cccc"), ("focus", b"I")]
        for boundary in range(len(data) + 1):
            parser = blink.InputParser()
            self.assertEqual(parser.feed(data[:boundary]) + parser.feed(data[boundary:]), expected)

    def test_palette_reply_with_bell_terminator(self):
        parser = blink.InputParser()
        self.assertEqual(parser.feed(b"\x1b]4;255;rgb:aa/bb/cc\x07p"),
                         [("palette", b"rgb:aa/bb/cc"), ("key", b"p")])

    def test_other_escape_sequences_do_not_acknowledge(self):
        parser = blink.InputParser()
        self.assertEqual(parser.feed(b"\x1b[1;3A\x1b]2;unrelated\x07"), [])

    def test_viewed_request_stays_quiet_until_a_new_request(self):
        state = blink.Attention()
        state.wait("permission-1")
        state.acknowledge()
        self.assertFalse(state.wait("permission-1"))
        self.assertEqual(state.state, "viewed")
        self.assertFalse(state.pulsing)
        state.wait("question-2")
        self.assertTrue(state.pulsing)
        state.resume()
        self.assertFalse(state.pulsing)


class Pane:
    def __init__(self, directory, number):
        self.master, self.slave = pty.openpty()
        self.output = b""
        self.directory, self.number = directory, number
        self.original = f"rgb:{number:04x}/bbbb/cccc".encode()
        self.process = subprocess.Popen(
            [sys.executable, str(blink.ROOT / "blink.py"), "_pane", str(directory), "--auto-after", "3600"],
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
            start_new_session=True)
        # A terminal keeps reading its panes; a pulsing pane would block otherwise.
        self.stopped = False
        self.reader = threading.Thread(target=self.pump, daemon=True)
        self.reader.start()
        self.until(lambda: blink.palette(b"?") in self.output)
        os.write(self.master, blink.palette(self.original))
        self.until(lambda: b"SESSION" in self.output)
        self.output = b""

    def pump(self):
        while not self.stopped:
            if select.select([self.master], [], [], 0.02)[0]:
                try:
                    self.output += os.read(self.master, 65536)
                except OSError:
                    return

    def read(self, timeout=0.02):
        time.sleep(timeout)

    def until(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.read()
            if self.process.poll() is not None:
                break
        if not condition():
            raise AssertionError(f"Pane {self.number} timed out: {self.output!r}")

    def event(self, event, request=None):
        return blink.send(self.directory, self.number, event, request)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.stopped = True
        self.reader.join()
        os.close(self.master)
        os.close(self.slave)


class PTYIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="blink-test-", dir="/tmp")
        self.directory = Path(self.temp.name)
        self.panes = []

    def tearDown(self):
        for pane in self.panes:
            pane.close()
        self.temp.cleanup()

    def pane(self):
        pane = Pane(self.directory, len(self.panes) + 1)
        self.panes.append(pane)
        return pane

    def test_four_sessions_target_only_requested_pane_and_restore_exact_palette(self):
        panes = [self.pane() for _ in range(4)]
        target = panes[1]
        self.assertTrue(target.event("needs_input", "question-1")["pulsing"])
        target.until(lambda: WAITING in target.output)
        for other in (panes[0], panes[2], panes[3]):
            self.assertFalse(other.event("status")["pulsing"])
            other.read()
            self.assertNotIn(WAITING, other.output)

        # Returning to the correct surface acknowledges it, not other panes.
        os.write(panes[0].master, b"\x1b[I")
        self.assertTrue(target.event("status")["pulsing"])
        os.write(target.master, b"\x1b[I")
        target.until(lambda: blink.palette(target.original) in target.output)
        self.assertEqual(target.event("status")["state"], "viewed")
        self.assertFalse(target.event("needs_input", "question-1")["pulsing"])
        self.assertTrue(target.event("needs_input", "question-2")["pulsing"])
        self.assertEqual(target.event("resumed")["state"], "running")

    def test_termination_during_animation_restores_palette_and_terminal_modes(self):
        pane = self.pane()
        pane.event("needs_input")
        pane.until(lambda: WAITING in pane.output)
        pane.output = b""
        pane.process.send_signal(signal.SIGTERM)
        pane.until(lambda: b"\x1b[?1049l" in pane.output)
        pane.process.wait(timeout=2)
        self.assertEqual(pane.process.returncode, 0)
        self.assertIn(blink.palette(pane.original), pane.output)
        self.assertIn(b"\x1b[?1004l", pane.output)
        self.assertIn(b"\x1b[?25h", pane.output)
        self.assertEqual(json.loads((self.directory / "1.status.json").read_text())["state"], "closed")

    def test_input_and_resume_do_not_leave_a_latched_animation(self):
        pane = self.pane()
        pane.event("needs_input", "one")
        os.write(pane.master, b"\r")
        pane.until(lambda: blink.palette(pane.original) in pane.output)
        self.assertFalse(pane.event("status")["pulsing"])
        os.write(pane.master, b"\x1b[O\x1b[I\x1b[O")
        self.assertFalse(pane.event("status")["pulsing"])


if __name__ == "__main__":
    unittest.main()
