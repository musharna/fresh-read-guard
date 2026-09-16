#!/usr/bin/env python3
"""Shared freshness predicate for both fresh-read-guard hooks.

The Iron Law of Current State asks: has this session SEEN the file's CURRENT bytes?
The observable proxy is:

    mtime(file) <= timestamp of the last SUCCESSFUL Read / Edit / Write / NotebookEdit
                   of that path in this session's transcript(s)

"Successful" means the tool_use has a matching tool_result (by tool_use_id) that does
not carry ``is_error``. The timestamp used is the tool_result's, because for Edit/Write
the bytes land on disk between the tool_use and its result.

Why not a substring grep (the pre-2026-09-15 design): a grep for the Read *request*
cannot see that the Read failed (oversize file, missing binary, permission error), and
cannot see that the file changed after the Read. Both are real transcript events.

CLI: ``fresh_read_check.py FILE TRANSCRIPT [SUB_TRANSCRIPT]`` → exit 0 fresh,
exit 1 stale/unread (reason on stdout), exit 0 on any parse error (safe-fail).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Tools whose successful completion means the session holds the file's current bytes.
FRESHENING_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}
# tool_input keys that carry the path, per tool.
PATH_KEYS = ("file_path", "notebook_path")

# Filesystems store mtime at ns; transcript timestamps at ms. A write that completes in
# the same millisecond as its result is logged can appear to be "after" it by <1ms.
# Anything larger than this is a genuine later modification.
MTIME_SLACK_S = 0.05


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_fresh_ts(paths: list[str], fp: str) -> float | None:
    """Timestamp of the latest successful freshening tool call on ``fp`` across transcripts.

    Returns None when no successful call exists.
    """
    pending: dict[str, str] = {}  # tool_use_id -> tool name (only calls on fp)
    best: float | None = None
    for p in paths:
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                ts = _parse_ts(d.get("timestamp"))
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name")
                        if name not in FRESHENING_TOOLS:
                            continue
                        inp = block.get("input") or {}
                        if any(inp.get(k) == fp for k in PATH_KEYS):
                            pending[str(block.get("id"))] = str(name)
                    elif btype == "tool_result":
                        tid = str(block.get("tool_use_id"))
                        if tid not in pending:
                            continue
                        pending.pop(tid)
                        if block.get("is_error"):
                            continue
                        if ts is not None and (best is None or ts > best):
                            best = ts
    return best


def check(fp: str, transcripts: list[str]) -> tuple[bool, str]:
    """(fresh?, reason). Reason is empty when fresh."""
    ts = last_fresh_ts(transcripts, fp)
    if ts is None:
        return False, f"Iron Law: Read {fp} before editing it (no successful Read in this session)."
    try:
        mtime = os.stat(fp).st_mtime
    except OSError:
        return True, ""  # vanished between check and edit — nothing to protect
    if mtime > ts + MTIME_SLACK_S:
        when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%SZ")
        return False, (
            f"Iron Law: {fp} changed on disk since your last Read at {when} "
            f"(mtime is {mtime - ts:.0f}s later). Re-Read it before editing."
        )
    return True, ""


def subagent_transcript(transcript: str, agent_id: str) -> str | None:
    """Path of the issuing subagent's own transcript, if the harness laid one down."""
    if not agent_id or not transcript.endswith(".jsonl"):
        return None
    cand = transcript[: -len(".jsonl")] + f"/subagents/agent-{agent_id}.jsonl"
    return cand if Path(cand).is_file() else None


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        return 0  # safe-fail: malformed invocation must not block
    fp, transcript = argv[1], argv[2]
    paths = [transcript] + [a for a in argv[3:] if a]
    try:
        ok, reason = check(fp, paths)
    except Exception:  # noqa: BLE001 — a broken predicate must not turn every Edit into a block
        return 0
    if ok:
        return 0
    print(reason)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
