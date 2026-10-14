# Blink — see which agent is waiting for you

When Claude Code, Codex CLI or GitHub Copilot CLI waits for you (a
permission, a question, a plan to approve, or simply the end of its answer),
Blink lights up **that agent's own terminal pane**. In Ghostty the whole pane
pulses amber: background, text and colored blocks. The other panes stay
untouched, and typing in the pane stops the pulse.

## Install

Clone this repository anywhere, then from the clone:

```sh
python3 blink.py install
```

That's it: open any terminal window and start `claude`, `codex` or `copilot`
as usual. No wrapper, no alias, no special window. Windows that were already
open work too: Ghostty reloads its configuration on the spot, and any agent
started after the install is covered. Agents that were already running need a
restart to load the hooks.

**Codex** asks you to trust new hooks once: run `/hooks` in Codex and approve
Blink's. This is Codex's own review; Blink does not bypass it.

Check the setup, and see which agents are currently waiting:

```sh
python3 blink.py doctor
```

Remove everything:

```sh
python3 blink.py uninstall
```

Re-run `install` after moving the clone. If you delete the clone without
uninstalling, Ghostty silently ignores the missing file and the hooks fail
silently; `uninstall` from a fresh clone cleans both up.

### What `install` changes

| Target | Change |
| --- | --- |
| Claude Code | Blink's hooks merged into `~/.claude/settings.json` (your other hooks are kept) |
| Codex CLI | Blink's hooks merged into `~/.codex/hooks.json` |
| Copilot CLI | `~/.copilot/hooks/blink.json` |
| Ghostty | A marked block in your Ghostty config: `config-file = ?<clone>/.runtime/ghostty.conf` |

Agents that are not installed are skipped. `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`COPILOT_HOME` and `XDG_CONFIG_HOME` are honored. Hooks run with
`/usr/bin/python3` when it is a real interpreter (it survives `mise`/`pyenv`
upgrades), otherwise with the Python that ran `install`. Python 3.9+ and the
standard library only.

## Terminals

| Terminal | Signal |
| --- | --- |
| Ghostty | The whole pane pulses amber, only while you are not looking at it. A hidden tab is not drawn at all: it gets the bell, shown as 🔔 in its title with `bell-features = title` |
| iTerm2 | Amber tab color and a "waiting for you" badge on the pane; bell |
| Anything else (Terminal.app, WezTerm, kitty, VS Code…) | Bell |
| tmux / screen | Bell |

Detection uses `TERM_PROGRAM`. SSH sessions are not covered.

## Energy

Ghostty's usual way to animate a shader, `custom-shader-animation = always`,
renders every visible pane continuously, even when nothing waits. Measured on
one visible Ghostty 1.3.1 surface (Apple Silicon, `ps` CPU time over 20 s):

| Mode | CPU | Energy impact |
| --- | --- | --- |
| No custom shader | 0 % | 0 |
| Shader with the animation loop (`always`) | 6–7 %, permanently, per visible pane | 12–16 |
| Blink's shader, nothing waiting | 0 % | 0 |
| Blink's shader, pane pulsing | ~1.4 % | ~1.7 |

Blink therefore sets `custom-shader-animation = false`: Ghostty only redraws a
pane when its content changes. While a request is pending, a small watcher
process rewrites the pane's palette ~15 times per second, which redraws that
pane alone. Hidden tabs and windows are not rendered at all. Note that this
setting also stops the animation of any other custom shader you use.

## How it works

1. A hook fires (permission request, question tool, permission notification,
   end of turn).
   It finds the agent as its nearest non-shell parent process and takes that
   process's terminal (`ps -o tty`). An agent without a terminal, such as a
   headless `claude -p` run by another agent, is ignored.
2. A watcher process writes the signal to that terminal device. For Ghostty it
   sets palette slot 255 to `#01b1XX` (OSC 4), with `XX` the pulse intensity;
   `shaders/attention.glsl` tints the pane when it sees that marker and the pane
   is not focused (`iFocus`).
3. The signal stops and the palette is reset (OSC 104) when the agent moves on
   (tool finished, new prompt, end of session), when you type in the
   pane, or when the agent exits. Typing is detected through the terminal's
   input access time, the same value `w` uses for idle times: Blink never reads
   your keystrokes.

| CLI | Events that signal | Events that never signal |
| --- | --- | --- |
| Claude Code | PermissionRequest, AskUserQuestion, ExitPlanMode, permission and elicitation notifications, Stop | idle_prompt, authentication, subagents |
| Codex CLI | PermissionRequest; PreToolUse on request_user_input when that path emits the hook; Stop | subagents |
| Copilot CLI | notification.permission_prompt, ask_user, notification.elicitation_dialog, agentStop | permissionRequest (fires before auto-approval), agent_idle |

A question asked only in an answer's text is covered by the end of turn: there
is no keyword matching and no transcript reading. A turn that is still running,
for example waiting for a background agent, does not signal yet. Hooks only observe: they never
approve, deny, or print anything the agent reads. State lives in
`~/.cache/blink/` and holds session IDs, a pid, a terminal name and pending
request kinds; no prompt, command or conversation text. Sessions whose agent
exited, or that a new session replaced in the same pane, are forgotten.

## Limits

- The signal is written to the pane's terminal from outside the agent. A write
  could in theory land in the middle of a large frame the agent is drawing,
  causing a one-frame glitch.
- Palette slot 255 is reserved while a pane pulses. Text drawn with that indexed
  color is recolored meanwhile, then reset to your theme's value.
- iTerm2 tab colors are per tab, not per split; the badge is per pane. Untested
  on a real iTerm2 so far.
- Only tested on macOS. On Linux the hooks should work; the Ghostty step looks
  for the macOS app (`--app`).
- Verified versions: Claude Code 2.1.296, Codex CLI 0.162.0, Copilot CLI 1.0.95,
  Ghostty 1.3.1. The end-to-end hook flow is covered by tests with a fake agent
  in a real pseudo-terminal; confirm with the real agents in daily use.

## Demo without an agent

```sh
python3 blink.py demo
```

Opens a separate Ghostty instance with a temporary configuration (your own is
not touched) and four panes. Session 2 asks for an answer after eight seconds.

- Click the waiting pane: stop the pulse, status "seen".
- `Enter`: answer / resume. `b`: new request in four seconds, then switch pane.
- `p`: pulse right now. `q`: close this demo pane.

If macOS blocks automation, add panes with `Cmd+D` / `Cmd+Shift+D`, or start
with `python3 blink.py demo --single`. From another shell, `python3 blink.py
status`, `wait 2` and `clear 2` drive the last demo.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

They run the real hooks from a fake agent in a real pseudo-terminal for all
three CLIs and the three terminal kinds, check that typing stops the pulse, that
an exiting agent clears its pane, that a headless agent never lights a pane,
and that install keeps your other hooks and is idempotent.

References: [Ghostty shaders](https://ghostty.org/docs/config/reference#custom-shader),
[OSC 4 palette](https://ghostty.org/docs/vt/osc/4),
[Claude Code hooks](https://code.claude.com/docs/en/hooks),
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Copilot CLI hooks](https://docs.github.com/en/copilot/reference/hooks-reference),
[iTerm2 escape codes](https://iterm2.com/documentation-escape-codes.html).

## License

MIT, see [LICENSE](LICENSE).
