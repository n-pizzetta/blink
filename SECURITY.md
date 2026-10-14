# Security

Report security issues to the maintainer privately. Do not open a public issue
or pull request for a vulnerability.

Use the **Report a vulnerability** button on the [Security tab](https://github.com/n-pizzetta/blink/security/advisories/new). It is the only reporting channel.

## Scope

- Anything that makes a Blink hook change what an agent does: approve, deny or
  delay a tool call, or print something the agent reads.
- Anything that makes Blink read or store prompt, command or conversation text,
  or your keystrokes.
- Anything that lets another user or process write to your terminal, or run
  code, through Blink's hooks, watcher or state in `~/.cache/blink/`.
- `install` or `uninstall` losing or corrupting settings that are not Blink's.

## Out of scope

- The agents' own hook trust and permission systems.
- Third-party tooling outside this repository.

Blink runs locally and makes no network request. A report that shows Blink
sending anything anywhere is the most valuable thing you can send.
