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
REDIR_RE = re.compile(r"(?:^|[\s;&|`(])(?:&|\d)?>>?\s*(['\"]?)([^|&;<>\s'\"]+)\1")
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


def find_write_targets(cmd: str) -> list[str]:
    targets: list[str] = []
    for m in SED_I_RE.finditer(cmd):
        targets.extend(extract_sed_files(m.group(0)))
    for m in AWK_INPLACE_RE.finditer(cmd):
        targets.extend(extract_awk_file(m.group(0)))
    for m in TEE_RE.finditer(cmd):
        targets.extend(extract_tee_files(m.group(1)))
    for m in REDIR_RE.finditer(cmd):
        targets.append(m.group(2))
    for m in PY_OPEN_RE.finditer(cmd):
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
