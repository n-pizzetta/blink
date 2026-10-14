"""A local interactive agent stand-in. No model, credentials, or network.

It runs in its own pseudo-terminal and calls the real hooks as children, like
the three CLIs do. Commands arrive on a separate pipe so that the pane's input
is only read when the test types in it, the way Blink notices you did.
"""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tty

agent, control = sys.argv[1], os.fdopen(int(sys.argv[2]))
root = Path(__file__).resolve().parents[2]
key = "sessionId" if agent == "copilot" else "session_id"
tty.setraw(0)


def hook(event, payload):
    result = subprocess.run([sys.executable, str(root / "agents.py"), agent, event],
                            input=json.dumps(dict(payload, **{key: "fixture-main"})).encode(),
                            capture_output=True)
    if result.returncode or result.stdout or result.stderr:
        os.write(1, b"HOOK_NOISE\r\n")


os.write(1, b"READY\r\n")
while True:
    ready, _, _ = select.select([0, control], [], [])
    if 0 in ready:
        os.read(0, 4096)  # An agent consumes what you type.
    if control not in ready:
        continue
    command = control.readline().strip()
    if command == "wait":
        if agent == "copilot":
            hook("notification", {"notification_type": "permission_prompt"})
        else:
            hook("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "PRIVATE"}})
    elif command == "done":
        hook("agentStop" if agent == "copilot" else "Stop", {})
    elif command == "answer":
        hook("userPromptSubmitted" if agent == "copilot" else "UserPromptSubmit", {"prompt": "PRIVATE"})
    elif command in ("exit", ""):
        sys.exit(7)
    os.write(1, f"DONE {command}\r\n".encode())
