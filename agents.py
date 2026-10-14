"""Agent hooks: light up the agent's own terminal pane while it waits for you.

Installed globally by `blink.py install`, so every new agent session in any
terminal window is covered, including windows opened before the install.
Hooks only observe: they never approve, deny or print anything to the agent.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
STATE = Path.home() / ".cache" / "blink"
AGENTS = ("claude", "codex", "copilot")
QUESTIONS = {
    "claude": {"AskUserQuestion", "ExitPlanMode"},
    "codex": {"request_user_input", "functions.request_user_input"},
    "copilot": {"ask_user"},
}
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "ksh", "tcsh"}
FRAME = 1 / 15
ESC, ST, BEL = b"\x1b", b"\x1b\\", b"\a"


def normalize(agent: str, event: str, payload: dict) -> dict | None:
    """Whitelist semantic events; idle notifications never pulse."""
    if agent not in AGENTS or not isinstance(payload, dict):
        return None
    # Claude subagent notifications must not clear the foreground's request.
    if payload.get("agent_id") or payload.get("parent_tool_use_id"):
        return None
    kind = event.lower()
    tool = str(payload.get("tool_name", payload.get("toolName", "")))
    result = {
        "agent": agent,
        "session_id": str(payload.get("session_id", payload.get("sessionId", ""))),
        "turn_id": str(payload.get("turn_id", "")),
        "tool": tool,
        "tool_id": str(payload.get("tool_use_id", payload.get("toolCallId", ""))),
    }
    if kind == "sessionstart":
        return dict(result, action="start", source=str(payload.get("source", "")))
    if kind == "sessionend":
        return dict(result, action="end")
    if kind in ("userpromptsubmit", "userpromptsubmitted", "interrupt"):
        return dict(result, action="resume")
    if kind in ("stop", "agentstop"):
        # End of turn: the agent waits for your next message, which covers
        # questions asked only in the answer's text.
        return dict(result, action="wait", reason="done")
    if kind in ("posttooluse", "posttoolusefailure"):
        return dict(result, action="tool_done")
    if kind == "pretooluse" and tool in QUESTIONS[agent]:
        return dict(result, action="wait", reason="question")
    # Copilot's permissionRequest fires BEFORE its rules/auto-approval engine.
    # Use notification.permission_prompt instead to avoid false positives.
    if kind == "permissionrequest" and agent in ("claude", "codex"):
        return dict(result, action="wait", reason="permission")
    if kind == "notification":
        notification = payload.get("notification_type")
        if notification == "permission_prompt":
            return dict(result, action="wait", reason="permission")
        if notification in ("elicitation_dialog", "elicitation_url_dialog"):
            return dict(result, action="wait", reason="question")
    return None


def apply(requests: dict, event: dict) -> bool:
    """Update pending requests in place. True when a new request arms the pane;
    a repeated notification for the same request does not re-arm it."""
    action = event["action"]
    if action == "wait":
        current = requests.get(event["reason"])
        request = {key: event.get(key, "") for key in ("tool", "tool_id", "turn_id")}
        if current is None:
            requests[event["reason"]] = request
            return True
        # The delayed permission notification and the immediate permission
        # hook describe the same request; keep whichever names the tool.
        if request["tool"] and not current["tool"]:
            requests[event["reason"]] = request
    elif action in ("start", "resume", "end"):
        requests.clear()
    elif action == "tool_done":
        for reason, request in list(requests.items()):
            if request["turn_id"] and event.get("turn_id") and request["turn_id"] != event["turn_id"]:
                continue
            if request["tool_id"]:
                matches = request["tool_id"] == event.get("tool_id")
            elif request["tool"]:
                matches = request["tool"] == event.get("tool")
            else:
                # Copilot notifications and turn ends name no tool: progress
                # means the agent is working again.
                matches = True
            if matches:
                del requests[reason]
    return False


def terminal_kind(environment=os.environ) -> str:
    # Multiplexers own the pane's palette and colors: only the bell passes.
    if environment.get("TMUX") or environment.get("STY"):
        return "bell"
    program = environment.get("TERM_PROGRAM", "")
    if program == "ghostty":
        return "ghostty"
    if program == "iTerm.app":
        return "iterm"
    return "bell"


def agent_terminal() -> tuple[int, str] | None:
    """The agent is the hook's nearest non-shell ancestor; use its terminal.

    Hooks may run without a controlling terminal (Claude Code detaches them).
    A headless agent started by another agent's tool has no terminal either,
    so it can never light up the pane of the agent that launched it.
    """
    pid = os.getppid()
    for _ in range(4):
        fields = subprocess.run(["ps", "-o", "ppid=,tty=,comm=", "-p", str(pid)],
                                capture_output=True, text=True).stdout.split(None, 2)
        if len(fields) < 3:
            return None
        parent, terminal, command = fields
        if Path(command.strip()).name.lstrip("-") not in SHELLS:
            if terminal in ("?", "??", "-") or not terminal:  # Linux, macOS, BSD.
                return None
            return pid, terminal if terminal.startswith("/") else "/dev/" + terminal
        pid = int(parent)
    return None


def sequence(kind: str, level: float | None) -> bytes:
    """Bytes that light (level 0..1) or clear (None) a pane."""
    if kind == "ghostty":
        if level is None:
            return ESC + b"]104;255" + ST
        # Read by shaders/attention.glsl: #01b1 = waiting, last byte = intensity.
        return ESC + b"]4;255;#01b1%02x" % round(255 * level) + ST
    if kind == "iterm":
        if level is None:
            return ESC + b"]6;1;bg;*;default" + BEL + ESC + b"]1337;SetBadgeFormat=" + BEL
        badge = base64.b64encode("⏳ waiting for you".encode())
        return (ESC + b"]6;1;bg;red;brightness;255" + BEL + ESC + b"]6;1;bg;green;brightness;145" + BEL +
                ESC + b"]6;1;bg;blue;brightness;43" + BEL + ESC + b"]1337;SetBadgeFormat=" + badge + BEL)
    return b""


def emit(tty: str, data: bytes) -> bool:
    """One short non-blocking write: a stalled terminal never hangs an agent."""
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, data)
        return True
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def state_path(session: str) -> Path:
    return STATE / (session.replace("/", "_") + ".json")


@contextlib.contextmanager
def locked():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(STATE / "lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def load(session: str) -> dict | None:
    try:
        return json.loads(state_path(session).read_text())
    except (OSError, ValueError):
        return None


def save(session: str, state: dict | None) -> None:
    path = state_path(session)
    if state is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


def armed(state: dict) -> bool:
    return bool(state["requests"]) and not state["viewed"]


def prune(tty: str | None = None) -> None:
    """Forget sessions whose agent is gone, or that a new session replaced in
    the same pane (Copilot keeps its process across sessions). Hold the lock."""
    for path in STATE.glob("*.json"):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not alive(state["agent_pid"]) or (state["tty"] == tty and not alive(state["watcher"])):
            path.unlink(missing_ok=True)


def hook_main(agent: str, event: str) -> None:
    """Silent, non-blocking observer even if Blink errors."""
    try:
        message = normalize(agent, event, json.loads(sys.stdin.buffer.read()))
        if message is None:
            return
        session = message["session_id"] or f"{agent}-{os.getppid()}"
        # Fast path for the frequent events of a session that waits for nothing.
        if message["action"] != "wait" and not state_path(session).exists():
            return
        with locked():
            state = load(session)
            if state is None:
                if message["action"] != "wait":
                    return
                found = agent_terminal()
                if found is None:
                    return
                prune(found[1])
                state = {"agent_pid": found[0], "tty": found[1], "kind": terminal_kind(),
                         "requests": {}, "viewed": False, "watcher": None}
            if apply(state["requests"], message):
                state["viewed"] = False
                if state["kind"] == "bell":
                    emit(state["tty"], BEL)
            if armed(state) and state["kind"] != "bell" and not alive(state["watcher"]):
                state["watcher"] = subprocess.Popen(
                    [sys.executable, str(ROOT / "agents.py"), "_watch", session],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True).pid
            keep = state["requests"] or alive(state["watcher"])
            save(session, state if keep else None)
    except Exception:
        # In particular, never let a failed preToolUse observer deny a tool.
        return


def watch(session: str) -> None:
    """Single writer of the pane's attention state while a request is armed.

    Stops when the request is answered, when you type in the pane (its input
    read time moves, like `w` idle times), or when the agent or pane is gone.
    """
    with locked():
        state = load(session)
    if state is None or state["kind"] == "bell":
        return
    tty, kind = state["tty"], state["kind"]
    try:
        seen = os.stat(tty).st_atime_ns
    except OSError:
        return
    started = time.monotonic()
    emit(tty, BEL + (b"" if kind == "ghostty" else sequence(kind, 1.0)))
    while True:
        if kind == "ghostty":
            # Each palette change redraws only this pane: a ~15 fps pulse
            # without Ghostty's always-on animation loop.
            emit(tty, sequence(kind, 0.5 - 0.5 * math.cos(math.pi * (time.monotonic() - started))))
        time.sleep(FRAME)
        try:
            typed = os.stat(tty).st_atime_ns != seen
        except OSError:
            typed = True
        with locked():
            state = load(session)
            gone = state is None or not alive(state["agent_pid"])
            if typed and state is not None:
                state["viewed"] = True
            if gone or not armed(state):
                emit(tty, sequence(kind, None))
                if state is not None:
                    state["watcher"] = None
                save(session, state if state is not None and state["requests"] and not gone else None)
                return


def interpreter() -> str:
    """A versioned Python (mise, pyenv, venv) can vanish on upgrade and
    silently disable the hooks: prefer the system one when it is real."""
    system = "/usr/bin/python3"
    if not os.access(system, os.X_OK):
        return sys.executable
    if sys.platform == "darwin" and subprocess.run(["xcode-select", "-p"], capture_output=True).returncode:
        return sys.executable  # Only the "install developer tools" stub.
    return system


def hook_command(agent: str, event: str, python: str) -> list[str]:
    return [python, str(ROOT / "agents.py"), agent, event]


def grouped_hooks(agent: str, python: str) -> dict:
    """Claude Code and Codex share this hooks.json schema."""
    entries = [("UserPromptSubmit", None), ("SessionEnd", None), ("PermissionRequest", None),
               ("PostToolUse", None), ("Stop", None)]
    if agent == "claude":
        entries += [("PreToolUse", "^(AskUserQuestion|ExitPlanMode)$"),
                    ("Notification", "^(permission_prompt|elicitation_dialog|elicitation_url_dialog)$"),
                    ("PostToolUseFailure", None)]
    else:
        entries += [("PreToolUse", r"^(functions\.)?request_user_input$"), ("Interrupt", None)]
    hooks = {}
    for event, matcher in entries:
        group = {"hooks": [{"type": "command", "timeout": 5,
                            "command": shlex.join(hook_command(agent, event, python))}]}
        if matcher:
            group["matcher"] = matcher
        hooks[event] = [group]
    return hooks


def copilot_hooks(python: str) -> dict:
    entries = [("sessionEnd", None), ("userPromptSubmitted", None), ("preToolUse", "ask_user"),
               ("postToolUse", None), ("postToolUseFailure", None), ("agentStop", None),
               ("notification", "permission_prompt|elicitation_dialog")]
    hooks = {}
    for event, matcher in entries:
        command = hook_command("copilot", event, python)
        handler = {"type": "command", "exec": command[0], "args": command[1:], "timeoutSec": 5}
        if matcher:
            handler["matcher"] = matcher
        hooks[event] = [handler]
    return {"version": 1, "hooks": hooks}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "_watch":
        watch(sys.argv[2])
    elif len(sys.argv) == 3:
        hook_main(sys.argv[1], sys.argv[2])
