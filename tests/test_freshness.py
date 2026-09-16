"""Freshness predicate tests — the guard must observe READ SUCCESS and FILE CURRENCY.

Each negative case is paired with a positive control in the same test so a broken
harness cannot read as "blocked". Both hooks are exercised end-to-end via subprocess
with a real payload, a synthetic transcript, and a real temp file whose mtime we set.

Bug these tests were written against (2026-09-15 panel finding): the hooks grepped for
a Read *request* substring, so (a) a Read whose tool_result was an error still passed,
and (b) a file modified after its last Read still passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SH_HOOK = REPO_ROOT / "fresh-read-guard.sh"
PY_HOOK = REPO_ROOT / "fresh-read-guard-bash.py"

T0 = 1_800_000_000  # 2027-01-15T08:00:00Z, arbitrary fixed epoch


def iso(epoch: float) -> str:
    ms = int(round((epoch - int(epoch)) * 1000))
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{ms:03d}Z"


def entry(kind: str, block: dict, ts: float) -> str:
    role = "assistant" if kind == "tool_use" else "user"
    # Real transcripts are compact JSON (no spaces after separators).
    return json.dumps(
        {"type": role, "timestamp": iso(ts), "message": {"role": role, "content": [block]}},
        separators=(",", ":"),
    )


def read_pair(fp: str, ts: float, *, ok: bool = True, tid: str = "toolu_r1") -> list[str]:
    use = {"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": fp}}
    res: dict = {"type": "tool_result", "tool_use_id": tid, "content": "1\tx\n"}
    if not ok:
        res["is_error"] = True
        res["content"] = "File content exceeds maximum allowed tokens"
    return [entry("tool_use", use, ts), entry("tool_result", res, ts + 0.5)]


def edit_pair(fp: str, ts: float, *, tid: str = "toolu_e1") -> list[str]:
    use = {
        "type": "tool_use",
        "id": tid,
        "name": "Edit",
        "input": {"file_path": fp, "old_string": "a", "new_string": "b"},
    }
    res = {"type": "tool_result", "tool_use_id": tid, "content": "The file has been updated."}
    return [entry("tool_use", use, ts), entry("tool_result", res, ts + 0.5)]


def write_transcript(tmp_path: Path, lines: list[str]) -> Path:
    t = tmp_path / "session.jsonl"
    t.write_text("\n".join(lines) + "\n")
    return t


def touch(fp: Path, epoch: float) -> None:
    os.utime(fp, (epoch, epoch))


def run_sh(transcript: Path, fp: Path, tool: str = "Edit") -> subprocess.CompletedProcess:
    payload = {
        "tool_name": tool,
        "transcript_path": str(transcript),
        "tool_input": {"file_path": str(fp), "old_string": "a", "new_string": "b"},
    }
    return subprocess.run(
        ["bash", str(SH_HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={**os.environ, "IRON_LAW_OVERRIDE": ""},
    )


def run_py(transcript: Path, fp: Path) -> subprocess.CompletedProcess:
    payload = {
        "tool_name": "Bash",
        "transcript_path": str(transcript),
        "cwd": str(fp.parent),
        "tool_input": {"command": f"sed -i 's/a/b/' {fp}"},
    }
    return subprocess.run(
        [sys.executable, str(PY_HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={**os.environ, "IRON_LAW_OVERRIDE": ""},
    )


RUNNERS = [pytest.param(run_sh, id="sh-hook"), pytest.param(run_py, id="py-hook")]


@pytest.fixture
def target() -> Iterator[Path]:
    # The Bash hook deliberately skips /tmp/ targets, so the guarded file lives
    # in a scratch dir under the repo (gitignored), not under pytest's tmp_path.
    scratch = REPO_ROOT / "tests" / "_scratch"
    scratch.mkdir(exist_ok=True)
    fp = scratch / f"foo_{os.getpid()}_{time.time_ns()}.py"
    fp.write_text("a\n")
    touch(fp, T0)  # file last modified at T0
    yield fp
    fp.unlink(missing_ok=True)


@pytest.mark.parametrize("run", RUNNERS)
def test_failed_read_blocks_but_successful_read_passes(run, tmp_path: Path, target: Path):
    # negative: the only Read of the file returned is_error → must block
    bad = write_transcript(tmp_path, read_pair(str(target), T0 + 10, ok=False))
    r = run(bad, target)
    assert r.returncode == 2, f"failed Read was accepted as a read: {r.stdout} {r.stderr}"
    assert "deny" in r.stdout
    # positive control: same shape, result succeeded → must pass
    good = write_transcript(tmp_path, read_pair(str(target), T0 + 10, ok=True))
    r = run(good, target)
    assert r.returncode == 0, f"successful Read was blocked: {r.stdout} {r.stderr}"


@pytest.mark.parametrize("run", RUNNERS)
def test_file_changed_after_read_blocks_but_unchanged_passes(run, tmp_path: Path, target: Path):
    tr = write_transcript(tmp_path, read_pair(str(target), T0 + 10, ok=True))
    # positive control: mtime T0 < read at T0+10 → pass
    assert run(tr, target).returncode == 0
    # negative: file modified after the read → block, reason names the staleness
    touch(target, T0 + 60)
    r = run(tr, target)
    assert r.returncode == 2, f"stale file was accepted: {r.stdout} {r.stderr}"
    assert "changed on disk" in r.stdout


@pytest.mark.parametrize("run", RUNNERS)
def test_own_edit_after_read_counts_as_fresh(run, tmp_path: Path, target: Path):
    """Read → Edit (by this session) → file mtime moves → a further Edit must pass."""
    lines = read_pair(str(target), T0 + 10, ok=True) + edit_pair(str(target), T0 + 20)
    tr = write_transcript(tmp_path, lines)
    touch(target, T0 + 20.2)  # written by the Edit, before its tool_result at +20.5
    assert run(tr, target).returncode == 0
    # but a write landing AFTER the Edit's result is external → block
    touch(target, T0 + 90)
    assert run(tr, target).returncode == 2


@pytest.mark.parametrize("run", RUNNERS)
def test_never_read_blocks(run, tmp_path: Path, target: Path):
    other = target.parent / "other.py"
    other.write_text("z\n")
    tr = write_transcript(tmp_path, read_pair(str(other), T0 + 10))
    r = run(tr, target)
    assert r.returncode == 2
    assert "Read" in r.stdout


def test_new_file_write_passes(tmp_path: Path):
    tr = write_transcript(tmp_path, [])
    fp = tmp_path / "brand_new.py"
    assert run_sh(tr, fp, tool="Write").returncode == 0
