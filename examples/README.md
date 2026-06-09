# examples

[`settings.json`](settings.json) is a minimal, copy-pasteable Claude Code
settings fragment that wires both hooks under `PreToolUse`:

- `Edit|Write` → `fresh-read-guard.sh`
- `Bash` → `fresh-read-guard-bash.py`

Merge the `hooks` block into your existing `~/.claude/settings.json` (or a
project-scoped `.claude/settings.json`). The `$HOME/.claude/hooks/...` paths
assume you installed the scripts as shown in the top-level
[README](../README.md#install); adjust if you put them elsewhere.

## Overriding (the part JSON can't tell you)

`settings.json` is plain JSON, so it can't carry inline comments explaining the
escape hatches. When you genuinely need to write without a prior `Read`:

- **Env var (both hooks):** set `IRON_LAW_OVERRIDE=1` in the environment. The
  hook checks it first and exits `0` (allow) immediately.
- **Inline token (Bash hook only):** put the literal token `# IRON_LAW_OK`
  anywhere in the command. The Bash hook treats its presence as an explicit
  opt-out for that single command — handy for an intentional `sed -i` /
  redirect / `tee` on a file you don't want to read first.

Both are deliberate, visible opt-outs. They are not a security boundary — see
[../SECURITY.md](../SECURITY.md): the guard fails _open_ by design and is not a
sandbox.
