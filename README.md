# fresh-read-guard

Two [Claude Code](https://docs.claude.com/en/docs/claude-code) `PreToolUse` hooks that enforce **read-before-write**: an `Edit`, `Write`, or bash-mediated write to a file that hasn't been `Read` in the current session is blocked until you read it.

It's a guardrail for the "**Iron Law of Current State**" — don't mutate a file based on what you _remember_ it contains; read the live bytes first. Editing from a stale mental model (or a post-compaction summary) is how silent corruption and clobbered changes happen.

## Why this exists

Two long-standing Claude Code behaviors motivate it. Both were filed upstream and **closed as `not planned`**, so the gaps are still present in current Claude Code:

- **[anthropics/claude-code#29709](https://github.com/anthropics/claude-code/issues/29709)** — _"Claude Code circumvents PreToolUse:Edit hook via Bash tool."_ A `PreToolUse(Edit|Write)` hook does nothing about `sed -i`, `tee`, shell redirects, or `python -c "open(f,'w')"`. The model can route around an Edit guard entirely by writing through Bash.
- **[anthropics/claude-code#16546](https://github.com/anthropics/claude-code/issues/16546)** — _"Model attempts file edits without reading file first."_ The built-in read-tracking also doesn't survive **context compaction**: once the conversation is summarized, the in-context record of "I read this file" is gone, so the model re-edits from a lossy summary.

`fresh-read-guard` addresses both by checking the **on-disk session transcript** (JSONL) for a prior `Read` of the exact file — a stateless check that survives compaction — and by covering the Bash write paths the Edit hook can't see.

## What's in the box

| Hook                       | Event                     | Covers                                                                                                   |
| -------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------- |
| `fresh-read-guard.sh`      | `PreToolUse(Edit\|Write)` | Tool-mediated writes                                                                                     |
| `fresh-read-guard-bash.py` | `PreToolUse(Bash)`        | `sed -i`, `awk -i inplace`, `tee`, `>`/`>>` redirects, `python open(...,'w'\|'a'\|'x')` (incl. heredocs) |

Both:

- **Grep the transcript, not in-context memory** — so a `Read` from 200 messages and three compactions ago still counts.
- **Check the issuing subagent's own transcript too** — a subagent that read the file is honored; sibling subagents' reads are not.
- **Allow new-file creation** — writing a path that doesn't exist yet is never blocked (there's nothing to read).
- **Safe-fail** — any parse/IO error falls through to `exit 0`. A broken guard must never turn every edit into a block.
- **Belt-and-suspenders deny** — emit the `permissionDecision` JSON on stdout _and_ a message on stderr _and_ `exit 2`, because the deny channels have had version-specific bugs; emitting all three means at least one lands.

## Install

Copy the two hooks somewhere (e.g. `~/.claude/hooks/`) and make them executable:

```bash
mkdir -p ~/.claude/hooks
curl -fsSL https://raw.githubusercontent.com/musharna/fresh-read-guard/main/fresh-read-guard.sh      -o ~/.claude/hooks/fresh-read-guard.sh
curl -fsSL https://raw.githubusercontent.com/musharna/fresh-read-guard/main/fresh-read-guard-bash.py -o ~/.claude/hooks/fresh-read-guard-bash.py
chmod +x ~/.claude/hooks/fresh-read-guard.sh ~/.claude/hooks/fresh-read-guard-bash.py
```

Then wire them into `~/.claude/settings.json` (see [`examples/settings.json`](examples/settings.json)):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/fresh-read-guard.sh"
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/fresh-read-guard-bash.py"
          }
        ]
      }
    ]
  }
}
```

The `.sh` hook requires [`jq`](https://jqlang.github.io/jq/). The `.py` hook is pure stdlib (Python 3.8+).

## Overriding

When you genuinely need to write without a prior read:

- **Either** set `IRON_LAW_OVERRIDE=1` in the environment, **or**
- **(Bash hook only)** put the literal token `# IRON_LAW_OK` anywhere in the command.

## How the check works

Each hook reads the `transcript_path` from the hook payload and looks for the needle:

```
"name":"Read","input":{"file_path":"<resolved-abs-path>"
```

If present, the write is allowed. The Bash hook first parses the command to extract write targets (handling `sed -i`/`-i.bak`, `awk -i inplace`, multi-file `tee`, every redirect form, and `open(path, 'w'|'a'|'x'...)`), resolves each against the command's `cwd`, skips ephemeral paths (`/tmp`, `/proc`, `/dev`, …), and only blocks targets that already exist on disk without a prior read.

## License

MIT — see [LICENSE](LICENSE).
