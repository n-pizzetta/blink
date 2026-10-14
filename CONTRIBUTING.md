# Contributing

Thanks for helping. Blink is small on purpose: two Python files, a shader, no
dependencies. Please keep it that way.

## Setup

```sh
git clone https://github.com/n-pizzetta/blink.git
cd blink
python3 -m unittest discover -s tests -v
```

No agent, model, Ghostty or network needed: the tests run the real hooks from a
fake agent ([`tests/fixtures/cli.py`](tests/fixtures/cli.py)) in a real
pseudo-terminal. CI runs them on macOS and Linux with Python 3.9 and 3.14.

| File | Role |
| --- | --- |
| [`agents.py`](agents.py) | Hook entry point, watcher process, generated hook configurations |
| [`blink.py`](blink.py) | `install`, `uninstall`, `doctor`, and the demo |
| [`shaders/attention.glsl`](shaders/attention.glsl) | The Ghostty shader that tints a waiting pane |

## Rules

- **Standard library only, Python 3.9+.** On macOS the hooks run with
  `/usr/bin/python3`, which is 3.9.
- **Hooks only observe.** A hook never prints, never fails, never blocks the
  agent, and never approves or denies anything. Errors are swallowed.
- **Nothing private.** Never read or store prompt, command or conversation
  text, or keystrokes. The tests check that state files hold none.
- **Install is reversible.** `install` and `uninstall` keep settings that are
  not Blink's and stay idempotent.
- **Every behavior change comes with a test.**
- Code, comments, commits and docs in English. Commits follow
  [Conventional Commits](https://www.conventionalcommits.org/):
  `fix(hooks): ignore agents without a terminal`.

## Trying a change with a real agent

Run `python3 blink.py install` from your clone, then restart the agent: running
agents keep their old hooks. `python3 blink.py doctor` shows what is installed
and the pending state in `~/.cache/blink/`.

## When an agent CLI changes

Blink depends on each CLI's hook events and on how it starts hooks. When a new
version breaks it:

1. Note the version and the hook event or payload that changed.
2. Update `normalize()`, `grouped_hooks()` or `copilot_hooks()` in `agents.py`.
3. Add or adjust a test.
4. Update the verified versions under "Limits" in the README.

## Pull requests

Small and focused, one change per pull request, with the tests passing. Report
security issues privately as described in [`SECURITY.md`](SECURITY.md), not in
a pull request.
