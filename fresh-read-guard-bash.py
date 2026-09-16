#!/usr/bin/env python3
"""PreToolUse(Bash) — Iron Law of Current State enforcement for bash-mediated writes.

Companion to fresh-read-guard.sh. The Edit|Write guard covers
tool-mediated writes; this hook covers writes that bypass it:
  - sed -i ... file
  - awk -i inplace ... file
  - tee file (or tee -a file)
  - > file / >> file  (shell redirect)
  - python -c "...open('file', 'w'|'a')..."
  - python3 << HEREDOC ... open('file', 'w') ... HEREDOC

For each detected write target that EXISTS on disk, require a prior Read entry
in the session transcript (or the issuing subagent's transcript). Block via
JSON+stderr+exit 2 (belt-and-suspenders, mirrors fresh-read-guard.sh).

Override: env IRON_LAW_OVERRIDE=1, OR literal token "# IRON_LAW_OK" anywhere in
the command (an inline escape hatch for intentional bash-mediated writes).

Safe-fail: every parse/IO error falls through to exit 0. A broken hook must not
turn every Bash call into a block.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Shared freshness predicate — installed alongside this hook (same directory).
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from fresh_read_check import check
except ImportError:  # pragma: no cover — safe-fail, but say so
    print(
        "fresh-read-guard-bash: fresh_read_check.py missing — guard is NOT enforcing",
        file=sys.stderr,
    )
    sys.exit(0)

OVERRIDE_TOKEN = "# IRON_LAW_OK"
SKIP_PREFIXES = ("/dev/", "/proc/", "/sys/", "/tmp/", "/var/tmp/", "/run/")

# `sed -i` in any form (-i, -i'', -i.bak, -iSUFFIX). Match consumes through end of statement.
SED_I_RE = re.compile(r"\bsed\b[^|;&\n`]*?\s-i(?:\S*)?\s[^|;&\n`]*")
# `awk -i inplace ... file`
AWK_INPLACE_RE = re.compile(r"\bawk\b[^|;&\n`]*?\s-i\s+['\"]?inplace['\"]?[^|;&\n`]*")
# `tee [flags] file [file...]`
TEE_RE = re.compile(r"\btee\b\s+((?:-[\w-]+\s+)*[^|&;<>\n`]+)")
# Shell redirect to file: > file, >> file, &> file, 2> file, 2>> file. Excludes &N target.
# `(?!=)` keeps `n >= 0` from reading as a redirect whose target is `=`.
REDIR_RE = re.compile(r"(?:^|[\s;&|`(])(?:&|\d)?>>?\s*(?!=)(['\"]?)([^|&;<>\s'\"]+)\1")
# Python open(..., 'w'|'a'|'x' with optional b/t/+ flags) — anywhere in command.
PY_OPEN_RE = re.compile(r"""open\s*\(\s*(['"])([^'"]+)\1\s*,\s*(['"])[wax][btx+]*\3""")


def emit_block(reason: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out))
    print(reason, file=sys.stderr)
    sys.exit(2)


# A `cd` that a static analyzer can actually read, and one it cannot.
CD_PREFIX_RE = re.compile(r"^\s*cd\s+(?P<dir>'[^']*'|\"[^\"]*\"|[^\s;&|<>]+)\s*(?:&&|;)\s*")
ANY_CD_RE = re.compile(r"(?:^|[;&|(]|\s)cd\s")


def effective_cwd(target_cmd: str, cwd: str) -> tuple[str, bool]:
    """Directory a RELATIVE write target should resolve against.

    Returns (directory, determinable).

    A PreToolUse hook is a static analyzer of a shell command; it cannot know
    the runtime cwd in general. It CAN read a literal leading `cd <dir> &&`,
    which is exactly how index writes escaped both guards on 2026-09-04:

        cd ~/.claude/projects/<slug>/memory && python3 - <<PY
        open("MEMORY.md","w")...

    `MEMORY.md` was resolved against the SESSION cwd, matched no index, and both
    guards passed. Reading the leading cd fixes that case.

    Everything else -- `cd "$VAR"`, a cd inside a subshell, a cd after a pipe --
    returns determinable=False instead of a confident guess. Guessing a cwd
    fails OPEN, which is the failure mode this function exists to remove. What
    an undeterminable answer is worth is the caller's decision, not this
    function's: the Iron Law guard treats it as best-effort, while the MEMORY.md
    atomicity gate refuses to pass a target named MEMORY.md it cannot place.
    """
    cur = cwd
    rest = target_cmd
    while True:
        m = CD_PREFIX_RE.match(rest)
        if not m:
            break
        d = m.group("dir")
        if len(d) >= 2 and d[0] in ("'", '"') and d[-1] == d[0]:
            d = d[1:-1]
        if "$" in d or "`" in d:
            return cur, False  # destination computed at runtime
        d = os.path.expanduser(d)
        cur = d if os.path.isabs(d) else os.path.normpath(os.path.join(cur, d))
        rest = rest[m.end():]
    if ANY_CD_RE.search(rest):
        return cur, False  # a cd past the leading prefix is out of reach
    return cur, True


def resolve_path(target: str, cwd: str) -> str:
    t = target.strip().rstrip(";")
    # strip wrapping quotes that slipped through capture
    if len(t) >= 2 and t[0] in ('"', "'") and t[-1] == t[0]:
        t = t[1:-1]
    if not t:
        return ""
    p = Path(t)
    if not p.is_absolute():
        p = Path(cwd) / p
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def extract_sed_files(segment: str) -> list[str]:
    """Pull file args from a `sed -i ...` segment. Skip flags, scripts, -e/-f arg."""
    toks = segment.split()
    files: list[str] = []
    skip_next = False
    for i, t in enumerate(toks):
        if i == 0:  # 'sed'
            continue
        if skip_next:
            skip_next = False
            continue
        if t in ("-e", "-f"):
            skip_next = True
            continue
        if t.startswith("-"):
            continue
        # quoted sed-expression like 's/x/y/g' — has slashes inside quotes
        if (t.startswith("'") or t.startswith('"')) and "/" in t[1:-1]:
            continue
        files.append(t)
    return files


def extract_awk_file(segment: str) -> list[str]:
    """awk -i inplace [-v ...] '<script>' <file>. The last bare token is the file."""
    toks = segment.split()
    for t in reversed(toks):
        if t.startswith(("-", "'", '"')):
            continue
        return [t]
    return []


def extract_tee_files(rest: str) -> list[str]:
    """tee may receive multiple files. Stop at redirect/pipe."""
    files: list[str] = []
    for tok in rest.split():
        if tok.startswith("-"):
            continue
        if tok.startswith(("<", ">", "|", "&", ";", "`")):
            break
        files.append(tok)
    return files


# --- Telling code from data -------------------------------------------------
#
# The regex battery below matches text that LOOKS like a write. Shell text that
# looks like a write is not always one: a heredoc body being written into a file
# is data, and a `>` inside a quoted string is not a redirect. Measured
# 2026-09-05, the guard blocked its own author three times for commands that
# performed no such write -- and an override reached for routinely stops being
# exceptional, which quietly erodes the backstop this file is.
#
# The rule is NOT "skip heredocs" and NOT "skip quotes". Who CONSUMES the text
# decides:
#   * `cat > f <<EOT ... EOT` / `tee f <<EOT` -- the body is data being stored.
#   * `python3 - <<PY ... PY`                 -- the body is CODE that runs.
#     (That second shape is a real bypass route, closed the same day; masking
#     all heredocs would have reopened it.)
#   * `git commit -m 'a > b'`                 -- quoted, never a redirect.
#   * `bash -c "echo x > f"`                  -- quoted, but handed to a shell,
#                                                so it IS a redirect.
#
# This is preprocessing, not parsing: it narrows the class, it does not
# eliminate it. Losing a true positive is worse than keeping a false one, so
# everything here only removes targets in contexts that are provably data, and
# the suite pins every true-positive shape.

# Commands that EXECUTE what they are fed, so their heredoc body stays in scope.
STDIN_INTERPRETERS = ("python3", "python", "bash", "sh", "zsh", "node", "perl", "ruby")
HEREDOC_START_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# Command position only. `\bsh\b` also matches the EXTENSION in `cat > t.sh`,
# which made every such heredoc look interpreter-fed and left its body in scope
# -- the masking silently did nothing until this was tightened.
INTERP_WORD = r"(?:^|[\s;&|(])(?:" + "|".join(STDIN_INTERPRETERS) + r")\b"
INTERP_AT_CMD_RE = re.compile(INTERP_WORD)
INTERP_CALL_RE = re.compile(INTERP_WORD + r"[^\n]*?-c\s*$")


def mask_data_heredocs(cmd: str) -> str:
    """Blank out heredoc bodies that are DATA, keeping character offsets.

    Offsets are preserved (bodies become spaces) so quote-span positions
    computed on the result still line up with the original command.
    """
    out = list(cmd)
    for m in HEREDOC_START_RE.finditer(cmd):
        delim = m.group(2)
        line_start = cmd.rfind("\n", 0, m.start()) + 1
        prefix = cmd[line_start:m.start()]
        # An interpreter reading stdin executes the body: leave it in scope.
        if INTERP_AT_CMD_RE.search(prefix):
            continue
        body_start = cmd.find("\n", m.end())
        if body_start == -1:
            continue
        body_start += 1
        end = cmd.find("\n" + delim, body_start - 1)
        body_end = end if end != -1 else len(cmd)
        for i in range(body_start, min(body_end, len(out))):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def quoted_spans(cmd: str) -> list[tuple[int, int]]:
    """(start, end) of single- and double-quoted spans, outermost only."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c in ("'", '"'):
            j = cmd.find(c, i + 1)
            if j == -1:
                break
            spans.append((i, j))
            i = j + 1
        else:
            i += 1
    return spans


def redirect_is_live(cmd: str, pos: int, spans: list[tuple[int, int]]) -> bool:
    """Is the `>` at `pos` a redirect the shell will actually perform?

    False when it sits inside a quoted string -- unless that string is the
    argument of an interpreter's -c, where an inner shell performs it for real.
    """
    for a, b in spans:
        if a < pos < b:
            return bool(INTERP_CALL_RE.search(cmd[:a]))
    return True


def find_write_targets(cmd: str) -> list[str]:
    # Data heredoc bodies are blanked first; everything below then sees only
    # text the shell will actually act on.
    scan = mask_data_heredocs(cmd)
    spans = quoted_spans(scan)
    targets: list[str] = []
    for m in SED_I_RE.finditer(scan):
        targets.extend(extract_sed_files(m.group(0)))
    for m in AWK_INPLACE_RE.finditer(scan):
        targets.extend(extract_awk_file(m.group(0)))
    for m in TEE_RE.finditer(scan):
        targets.extend(extract_tee_files(m.group(1)))
    for m in REDIR_RE.finditer(scan):
        if not redirect_is_live(scan, m.start(2), spans):
            continue
        targets.append(m.group(2))
    for m in PY_OPEN_RE.finditer(scan):
        targets.append(m.group(2))
    return targets


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if os.environ.get("IRON_LAW_OVERRIDE") == "1":
        sys.exit(0)

    if (payload.get("tool_name") or "") != "Bash":
        sys.exit(0)

    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd:
        sys.exit(0)

    if OVERRIDE_TOKEN in cmd:
        sys.exit(0)

    transcript = payload.get("transcript_path") or ""
    if not transcript or not Path(transcript).is_file():
        sys.exit(0)

    agent_id = payload.get("agent_id") or ""
    sub_transcript = ""
    if agent_id and transcript.endswith(".jsonl"):
        cand = transcript[: -len(".jsonl")] + f"/subagents/agent-{agent_id}.jsonl"
        if Path(cand).is_file():
            sub_transcript = cand

    cwd = payload.get("cwd") or os.environ.get("PWD") or os.getcwd()
    # Follow a literal leading `cd` before resolving relative targets; without
    # this, `cd <dir> && ... > file` was checked against the wrong directory and
    # silently passed. Determinability is ignored here on purpose -- this guard
    # stays best-effort rather than blocking every dynamic cd.
    cwd, _ = effective_cwd(cmd, cwd)

    raw_targets = find_write_targets(cmd)
    if not raw_targets:
        sys.exit(0)

    transcripts = [transcript] + ([sub_transcript] if sub_transcript else [])

    seen: set[str] = set()
    unread: list[str] = []
    reasons: list[str] = []
    for raw in raw_targets:
        fp = resolve_path(raw, cwd)
        if not fp or fp in seen:
            continue
        seen.add(fp)
        if any(fp.startswith(p) for p in SKIP_PREFIXES):
            continue
        # New-file writes are allowed (mirrors Edit|Write guard's `[[ ! -e "$FP" ]] && exit 0`).
        if not Path(fp).exists():
            continue
        try:
            ok, why = check(fp, transcripts)
        except Exception:
            continue  # a broken predicate must not block
        if ok:
            continue
        unread.append(fp)
        reasons.append(why)

    if not unread:
        sys.exit(0)

    preview = ", ".join(unread[:5])
    if len(unread) > 5:
        preview += f" (+{len(unread) - 5} more)"
    detail = " | ".join(reasons[:5])
    reason = (
        f"Iron Law: Bash command would write to {preview}. {detail} "
        f"Override: env IRON_LAW_OVERRIDE=1, or inline token '{OVERRIDE_TOKEN}'."
    )
    emit_block(reason)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
