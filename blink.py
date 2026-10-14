#!/usr/bin/env python3
"""Blink: install the agent hooks and terminal effect, or run the Ghostty demo."""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
import uuid

import agents

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
BUILD = ROOT / ".build"
ESC = b"\x1b"
ST = ESC + b"\\"
GHOSTTY_APP = Path("/Applications/Ghostty.app")
BEGIN, END = "# >>> blink >>>", "# <<< blink <<<"


def palette(value: bytes) -> bytes:
    return ESC + b"]4;255;" + value + ST


def write_all(fd: int, data: bytes) -> None:
    while data:
        count = os.write(fd, data)
        data = data[count:]


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


class InputParser:
    """Parse focus and OSC replies even when a PTY splits escape sequences."""

    def __init__(self) -> None:
        self.buffer = b""

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        self.buffer += data
        result = []
        while self.buffer:
            if self.buffer.startswith(ESC + b"]"):
                match = re.search(rb"\x07|\x1b\\", self.buffer[2:])
                if match is None:
                    if len(self.buffer) > 4096:
                        self.buffer = b""
                    break
                end = 2 + match.end()
                body = self.buffer[2:2 + match.start()]
                if re.fullmatch(rb"4;255;rgb:[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}", body):
                    result.append(("palette", body.split(b";", 2)[2]))
                self.buffer = self.buffer[end:]
            elif self.buffer.startswith(ESC + b"["):
                match = re.match(rb"\x1b\[[0-?]*[ -/]*[@-~]", self.buffer)
                if match is None:
                    if len(self.buffer) > 128:
                        self.buffer = b""
                    break
                sequence = match.group()
                if sequence in (b"\x1b[I", b"\x1b[O"):
                    result.append(("focus", sequence[-1:]))
                self.buffer = self.buffer[len(sequence):]
            elif self.buffer == ESC:
                break
            else:
                result.append(("key", self.buffer[:1]))
                self.buffer = self.buffer[1:]
        return result


class Attention:
    """Viewed is distinct from answered. Repeated events do not re-arm."""

    def __init__(self) -> None:
        self.request: str | None = None
        self.state = "running"

    @property
    def pulsing(self) -> bool:
        return self.state == "waiting"

    def wait(self, request: str) -> bool:
        if request == self.request:
            return False
        self.request = request
        self.state = "waiting"
        return True

    def acknowledge(self) -> None:
        if self.state == "waiting":
            self.state = "viewed"

    def resume(self) -> None:
        self.request = None
        self.state = "running"


def claim_session(directory: Path) -> int:
    with (directory / "counter").open("a+") as counter:
        fcntl.flock(counter, fcntl.LOCK_EX)
        counter.seek(0)
        number = int(counter.read() or "0") + 1
        counter.seek(0)
        counter.truncate()
        counter.write(str(number))
        counter.flush()
        return number


def run_pane(directory: Path, auto_after: float = 8) -> None:
    if not os.isatty(sys.stdin.fileno()) or not os.isatty(sys.stdout.fileno()):
        raise RuntimeError("A demo pane must run in its own interactive terminal.")
    number = claim_session(directory)
    input_fd, output_fd = sys.stdin.fileno(), sys.stdout.fileno()
    saved = termios.tcgetattr(input_fd)
    parser = InputParser()
    attention = Attention()
    original_color = b"rgb:eeee/eeee/eeee"
    pulse_started = 0.0
    stop = False
    dirty = True
    started = time.monotonic()
    scheduled: float | None = started + auto_after if number == 2 else None
    reason = "Choose between SQLite and PostgreSQL"
    resized = False

    metadata = {"session": number, "pid": os.getpid(), "tty": os.ttyname(input_fd),
                "source": "simulation"}
    write_json(directory / f"{number}.json", metadata)

    def status() -> dict:
        return {**metadata, "state": attention.state, "pulsing": attention.pulsing}

    def emit(data: bytes) -> None:
        write_all(output_fd, data)

    def log(event: str) -> None:
        write_json(directory / f"{number}.status.json", status())
        with (directory / f"{number}.events.jsonl").open("a") as events:
            events.write(json.dumps({"at": time.time(), "event": event,
                                     "state": attention.state}) + "\n")

    def apply() -> None:
        nonlocal dirty, pulse_started
        # Only an ordinary bell for the tab. Tab animation is NOT implemented;
        # do not spoof it by repeatedly changing titles.
        if attention.pulsing:
            pulse_started = time.monotonic()
            emit(b"\a")
        else:
            emit(palette(original_color))
        dirty = True

    def wait(request: str) -> None:
        if attention.wait(request):
            apply()
            log("needs_input")

    def clear(viewed: bool = False) -> None:
        nonlocal scheduled
        if viewed:
            attention.acknowledge()
        else:
            attention.resume()
            scheduled = None
        apply()
        log("viewed" if viewed else "resumed")

    def handle_input(data: bytes) -> None:
        nonlocal stop, scheduled, original_color, dirty
        for event, value in parser.feed(data):
            if event == "palette":
                original_color = value
            elif event == "focus":
                if value == b"I" and attention.pulsing:
                    clear(viewed=True)
            elif event == "key":
                if value in (b"q", b"Q", b"\x03", b"\x04"):
                    stop = True
                elif value in (b"b", b"B"):
                    scheduled = time.monotonic() + 4
                    dirty = True
                elif value in (b"p", b"P"):
                    wait(str(time.monotonic_ns()))
                elif value in (b"\r", b"\n", b" ", b"c", b"C"):
                    clear()
                elif attention.pulsing:
                    clear(viewed=True)

    def on_signal(signum: int, frame: object) -> None:
        nonlocal stop, resized
        if signum == signal.SIGWINCH:
            resized = True
        else:
            stop = True

    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGWINCH):
        previous_handlers[sig] = signal.signal(sig, on_signal)

    def render() -> None:
        columns, rows = shutil.get_terminal_size((60, 22))
        width = max(12, columns - 4)
        status = {"running": "RUNNING", "waiting": "YOUR ANSWER IS NEEDED", "viewed": "SEEN - WAITING FOR AN ANSWER"}[attention.state]
        label = ["API / authentication", "Database", "Web interface", "Tests and validation"][(number - 1) % 4]
        lines = [
            f"\x1b[1;38;2;203;166;247mBLINK  /  SESSION {number}\x1b[0m",
            label,
            "",
            f"\x1b[1m{status}\x1b[0m",
            reason[:width] if attention.state != "running" else "Simulated demo session.",
            "",
            "\x1b[48;2;35;75;105m\x1b[38;2;235;245;255m  Block with its own blue background  \x1b[0m",
            "\x1b[48;2;70;43;80m\x1b[38;2;245;230;255m  Text and this background pulse too  \x1b[0m",
            "",
            "b  Request in 4 s, then switch panes",
            "p  Pulse right now",
            "Enter  Answer / resume",
            "q  Close this demo session",
            "",
            "Click here to stop the pulse.",
            "Cmd+D / Cmd+Shift+D: add a pane.",
        ]
        if scheduled is not None:
            lines[4] = f"Request in {max(0, int(scheduled - time.monotonic()) + 1)} s..."
        emit(b"\x1b[H\x1b[2J")
        for row, line in enumerate(lines[:max(1, rows - 1)], 1):
            emit(f"\x1b[{row};2H{line}".encode())

    try:
        tty.setraw(input_fd)
        emit(b"\x1b[?1049h\x1b[?25l\x1b[?1004h")
        emit(f"\x1b]2;Blink - session {number}\x1b\\".encode())
        # This is our own TUI, not an agent's input stream: safely query the
        # original color here, then restore that exact value on every exit.
        emit(palette(b"?"))
        until = time.monotonic() + 0.5
        while time.monotonic() < until:
            ready, _, _ = select.select([input_fd], [], [], max(0, until - time.monotonic()))
            if ready:
                handle_input(os.read(input_fd, 4096))
        last_second = -1
        log("started")
        while not stop:
            now = time.monotonic()
            if scheduled is not None and now >= scheduled:
                scheduled = None
                wait(str(time.monotonic_ns()))
            second = int(now)
            if dirty or resized or (scheduled is not None and second != last_second):
                render()
                dirty, resized = False, False
                last_second = second
            if attention.pulsing:
                level = 0.5 - 0.5 * math.cos(math.pi * (now - pulse_started))
                emit(agents.sequence("ghostty", level))
            ready, _, _ = select.select([input_fd], [], [], agents.FRAME if attention.pulsing else 0.1)
            if input_fd in ready:
                data = os.read(input_fd, 4096)
                if not data:
                    break
                handle_input(data)
            # Private file mailbox: no listening network port or Unix socket.
            # Atomic rename ensures a pane never consumes a partial event.
            for path in sorted(directory.glob(f"{number}.*.request.json")):
                try:
                    if path.stat().st_size > 4096:
                        raise ValueError("Event too large")
                    message = json.loads(path.read_text())
                    action = message.get("event")
                    if action == "needs_input":
                        wait(str(message.get("request_id", time.monotonic_ns())))
                    elif action == "resumed":
                        clear()
                    elif action != "status":
                        raise ValueError("Unknown event")
                    write_json(path.with_name(path.name.replace(".request.json", ".reply.json")), status())
                except (ValueError, OSError, AttributeError):
                    pass
                finally:
                    path.unlink(missing_ok=True)
    finally:
        try:
            emit(palette(original_color) + b"\x1b[0m\x1b[?1004l\x1b[?25h\x1b[?1049l")
        except OSError:
            pass
        termios.tcsetattr(input_fd, termios.TCSANOW, saved)
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
        attention.state = "closed"
        log("closed")




def ghostty_errors(app: Path, *arguments: str) -> set[str]:
    # Ghostty's validator initializes its crash reporter before reading config.
    # Keep that cache inside the project as well, including in restricted shells.
    cache = BUILD / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(app / "Contents/MacOS/ghostty"), "+validate-config", *arguments],
                            text=True, capture_output=True, env=dict(os.environ, XDG_CACHE_HOME=str(cache)))
    errors = set((result.stdout + result.stderr).splitlines())
    return errors or ({f"exit {result.returncode}"} if result.returncode else set())


def validate_ghostty(app: Path, *arguments: str, known: set[str] = frozenset()) -> None:
    """Fail on configuration errors, except those the user already had."""
    errors = ghostty_errors(app, *arguments) - known
    if errors:
        raise RuntimeError("Ghostty configuration validation failed:\n" + "\n".join(sorted(errors)))


def prepare(app: Path) -> Path:
    if not (app / "Contents/MacOS/ghostty").is_file():
        raise RuntimeError(f"Ghostty not found: {app}")
    directory = Path(tempfile.mkdtemp(prefix="blink-", dir="/tmp"))
    RUNTIME.mkdir(exist_ok=True)
    config = directory / "ghostty.config"
    command = shlex.join([sys.executable, str(ROOT / "blink.py"), "_pane", str(directory)])
    config.write_text("\n".join([
        "# Isolated Blink rendering demo. No user configuration is modified.",
        "theme = Catppuccin Mocha", "font-size = 13", "window-width = 132", "window-height = 42",
        "window-padding-x = 10", "window-padding-y = 10", "background-opacity = 1",
        "unfocused-split-opacity = 1", "palette = 255=#eeeeee",
        "custom-shader = " + str(ROOT / "shaders/attention.glsl"),
        "custom-shader-animation = false", "shell-integration = none",
        "bell-features = title,no-border,no-audio,no-system,no-attention",
        "desktop-notifications = false", "window-save-state = never",
        "quit-after-last-window-closed = true", "confirm-close-surface = false",
        "auto-update = off", "command = shell:exec " + command, "",
    ]))
    validate_ghostty(app, f"--config-file={config}")
    (RUNTIME / "last.json").write_text(json.dumps({"directory": str(directory), "config": str(config)}))
    return config


def build_launcher() -> Path:
    BUILD.mkdir(exist_ok=True)
    binary = BUILD / "GhosttyDemo"
    source = ROOT / "native/GhosttyDemo.swift"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(BUILD / "module-cache"),
                        str(source), "-o", str(binary)], check=True)
    return binary


def send(directory: Path, number: int, event: str, request_id: str | None = None) -> dict:
    if not (directory / f"{number}.json").is_file():
        raise RuntimeError(f"Unknown session {number} in this demo")
    token = uuid.uuid4().hex
    request = directory / f"{number}.{token}.request.json"
    reply = directory / f"{number}.{token}.reply.json"
    message = {"event": event}
    if request_id is not None:
        message["request_id"] = request_id
    write_json(request, message)
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if reply.exists():
                return json.loads(reply.read_text())
            time.sleep(0.02)
        raise RuntimeError(f"Session {number} did not reply (closed or disconnected)")
    finally:
        request.unlink(missing_ok=True)
        reply.unlink(missing_ok=True)


# --- Global install -------------------------------------------------------

def claude_settings() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def codex_hooks_file() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


def copilot_hooks_file() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot") / "hooks" / "blink.json"


def ghostty_config() -> Path:
    """The user's existing Ghostty config file, else the XDG default."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ghostty"
    candidates = [base / "config.ghostty", base / "config"]
    if sys.platform == "darwin":
        support = Path.home() / "Library/Application Support/com.mitchellh.ghostty"
        candidates += [support / "config.ghostty", support / "config"]
    return next((path for path in candidates if path.is_file()), base / "config")


def wanted(agent: str, home: Path) -> bool:
    return shutil.which(agent) is not None or home.is_dir()


def blink_group(group: dict) -> bool:
    return any(re.search(r"agents\.py'? (claude|codex|copilot) \w+$", str(hook.get("command", "")))
               for hook in group.get("hooks", []))


def merge_hooks(path: Path, hooks: dict) -> None:
    """Replace Blink's groups in a Claude/Codex hooks file; keep everything else."""
    data = json.loads(path.read_text()) if path.is_file() else {}
    current = data.get("hooks", {})
    for event in list(current):
        current[event] = [group for group in current[event] if not blink_group(group)]
        if not current[event]:
            del current[event]
    for event, groups in hooks.items():
        current.setdefault(event, []).extend(groups)
    if current:
        data["hooks"] = current
    else:
        data.pop("hooks", None)
    if not data:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def set_ghostty_block(path: Path, lines: list[str]) -> bool:
    """Replace or remove Blink's block in the Ghostty config. True if changed."""
    before = path.read_text() if path.is_file() else ""
    text = re.sub(rf"\n?{re.escape(BEGIN)}\n.*?{re.escape(END)}\n?", "\n", before, flags=re.S).rstrip("\n")
    if lines:
        text = (text + "\n\n" if text else "") + "\n".join([BEGIN, *lines, END])
    text = text + "\n" if text else ""
    if text == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def reload_ghostty() -> int:
    """Ghostty reloads its configuration on SIGUSR2, in every open window."""
    count = 0
    listing = subprocess.run(["ps", "-xo", "pid=,comm="], capture_output=True, text=True).stdout
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if command.strip().endswith("Ghostty.app/Contents/MacOS/ghostty"):
            os.kill(int(pid), signal.SIGUSR2)
            count += 1
    return count


def install(app: Path) -> None:
    python = agents.interpreter()
    if wanted("claude", claude_settings().parent):
        merge_hooks(claude_settings(), agents.grouped_hooks("claude", python))
        print(f"Claude Code: hooks added to {claude_settings()}")
    if wanted("codex", codex_hooks_file().parent):
        merge_hooks(codex_hooks_file(), agents.grouped_hooks("codex", python))
        print(f"Codex: hooks added to {codex_hooks_file()}")
        print("  Codex asks you to trust new hooks once: run /hooks in Codex and approve Blink's.")
    if wanted("copilot", copilot_hooks_file().parent.parent):
        copilot_hooks_file().parent.mkdir(parents=True, exist_ok=True)
        copilot_hooks_file().write_text(json.dumps(agents.copilot_hooks(python), indent=2) + "\n")
        print(f"Copilot CLI: hooks written to {copilot_hooks_file()}")
    if (app / "Contents/MacOS/ghostty").is_file():
        RUNTIME.mkdir(exist_ok=True)
        include = RUNTIME / "ghostty.conf"
        include.write_text("# Generated by blink.py install.\n"
                           f"custom-shader = {ROOT / 'shaders/attention.glsl'}\n"
                           # The hook animates the pane itself: no render loop while idle.
                           "custom-shader-animation = false\n")
        config = ghostty_config()
        previous = config.read_text() if config.is_file() else None
        known = ghostty_errors(app)
        # "?" keeps Ghostty silent if this clone is moved or deleted.
        set_ghostty_block(config, [f"config-file = ?{include}"])
        try:
            validate_ghostty(app, known=known)
        except RuntimeError:
            if previous is None:
                config.unlink()
            else:
                config.write_text(previous)
            raise
        print(f"Ghostty: shader enabled from {config}")
        windows = reload_ghostty()
        if windows:
            print("  Open Ghostty windows reloaded their configuration.")
    print(f"Hooks run with {python}. Other terminals: iTerm2 gets a tab color and badge, "
          "the rest a bell.")
    print("Done. Start an agent in any window; already running agents need a restart.")


def uninstall() -> None:
    merge_hooks(claude_settings(), {})
    merge_hooks(codex_hooks_file(), {})
    copilot_hooks_file().unlink(missing_ok=True)
    if set_ghostty_block(ghostty_config(), []):
        reload_ghostty()
    (RUNTIME / "ghostty.conf").unlink(missing_ok=True)
    print("Blink removed from Claude Code, Codex, Copilot CLI and Ghostty.")


def doctor() -> None:
    def has(path: Path) -> bool:
        try:
            return str(ROOT / "agents.py") in path.read_text()
        except OSError:
            return False
    for name, path in (("Claude Code", claude_settings()), ("Codex", codex_hooks_file()),
                       ("Copilot CLI", copilot_hooks_file()), ("Ghostty", ghostty_config())):
        found = has(path) if name != "Ghostty" else BEGIN in (path.read_text() if path.is_file() else "")
        print(f"{name:12} {'installed' if found else 'not installed'}  ({path})")
    print(f"Terminal     {agents.terminal_kind()} (TERM_PROGRAM={os.environ.get('TERM_PROGRAM', '')})")
    with agents.locked():
        agents.prune()
        waiting = [json.loads(path.read_text()) for path in sorted(agents.STATE.glob("*.json"))]
    for state in waiting:
        print(f"Waiting      {state['tty']} pid {state['agent_pid']}: {', '.join(state['requests']) or '-'}"
              f"{' (seen)' if state['viewed'] else ''}")
    if not waiting:
        print("Waiting      no agent is waiting for you")


def main() -> None:
    parser = argparse.ArgumentParser(description="Blink — light up the terminal pane of an agent that waits for you")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("install", "Install the hooks for Claude Code, Codex and Copilot CLI, and the Ghostty shader"),
                       ("uninstall", "Remove everything install added")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--app", type=Path, default=GHOSTTY_APP)
    sub.add_parser("doctor", help="Show what is installed and which agents wait")
    demo = sub.add_parser("demo", help="Open the isolated four-pane Ghostty demo")
    demo.add_argument("--prepare-only", action="store_true")
    demo.add_argument("--single", action="store_true", help="One window, without Apple Events automation")
    demo.add_argument("--app", type=Path, default=GHOSTTY_APP)
    pane = sub.add_parser("_pane", help=argparse.SUPPRESS)
    pane.add_argument("directory", type=Path)
    pane.add_argument("--auto-after", type=float, default=8)
    for name in ("wait", "clear"):
        command = sub.add_parser(name, help=f"Demo: make a session {name}")
        command.add_argument("session", type=int)
    sub.add_parser("status", help="Demo: state of the demo panes")
    args = parser.parse_args()
    if args.command == "install":
        install(args.app)
    elif args.command == "uninstall":
        uninstall()
    elif args.command == "doctor":
        doctor()
    elif args.command == "_pane":
        run_pane(args.directory, args.auto_after)
    elif args.command == "demo":
        config = prepare(args.app)
        print(f"Configuration validated: {config}", flush=True)
        if args.prepare_only:
            return
        if args.single:
            subprocess.run(["/usr/bin/open", "-na", str(args.app), "--args",
                            "--config-default-files=false", f"--config-file={config}"], check=True)
        else:
            result = subprocess.run([str(build_launcher()), str(args.app), str(config)])
            if result.returncode not in (0, 3):
                raise RuntimeError("The graphical launch failed. Run this command from your own terminal.")
    else:
        directory = Path(json.loads((RUNTIME / "last.json").read_text())["directory"])
        if args.command == "status":
            for path in sorted(directory.glob("*.status.json")):
                number = int(path.name.split(".")[0])
                try:
                    print(json.dumps(send(directory, number, "status"), ensure_ascii=False))
                except (OSError, RuntimeError):
                    print(f"Session {number}: disconnected")
        else:
            event = "needs_input" if args.command == "wait" else "resumed"
            print(json.dumps(send(directory, args.session, event), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Blink: {error}", file=sys.stderr)
        sys.exit(1)
