"""Tests for fresh-read-guard hooks.

Covers two load-bearing assumptions:

1. The transcript "needle" both hooks grep for matches the *actual* on-disk
   Claude Code JSONL transcript format. If upstream changes the JSONL shape,
   the fixture test breaks loudly instead of the guard silently never matching.

2. The Bash hook's write-target extraction (`find_write_targets`) returns the
   right targets for each supported write form, and yields nothing for the
   negative cases the code is designed to ignore.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "transcript.jsonl"


def _load_bash_hook():
    """Import fresh-read-guard-bash.py (hyphenated name) as a module."""
    # FRESH_READ_GUARD_BASH lets the suite run against a different copy of
    # the hook (e.g. `git show <ref>:fresh-read-guard-bash.py > /tmp/old.py`)
    # to see a new test FAIL on the code it was written against.
    path = Path(os.environ.get("FRESH_READ_GUARD_BASH") or REPO_ROOT / "fresh-read-guard-bash.py")
    spec = importlib.util.spec_from_file_location("fresh_read_guard_bash", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_bash_hook()


# ---------------------------------------------------------------------------
# 1. Transcript-needle fixture test
# ---------------------------------------------------------------------------


# This is the literal needle both hooks search for. It mirrors the bash hook's
# NEEDLE and the python hook's f-string in main(). Keep in sync with the hooks.
def needle_for(file_path: str) -> str:
    return f'"name":"Read","input":{{"file_path":"{file_path}"'


def test_fixture_exists_and_is_readable():
    assert FIXTURE.is_file(), f"missing transcript fixture: {FIXTURE}"


def test_needle_matches_synthetic_transcript():
    """The needle for a Read'd path is a literal substring of the transcript."""
    content = FIXTURE.read_text()
    needle = needle_for("/home/user/project/foo.py")
    assert needle in content, (
        "Transcript JSONL format drifted: the hook needle no longer matches a "
        "Read tool_use entry. Update both hooks and this fixture together.\n"
        f"needle: {needle!r}"
    )


def test_needle_does_not_match_unread_path():
    """A path that was only Edited (not Read) must NOT match the Read needle."""
    content = FIXTURE.read_text()
    # bar.py appears in an Edit entry, never a Read entry.
    assert needle_for("/home/user/project/bar.py") not in content


def test_python_hook_needle_construction_matches_fixture():
    """The exact needle main() builds for a resolved path is in the fixture.

    main() does: needle = f'"name":"Read","input":{{"file_path":"{fp}"'
    This asserts that construction (not just our local copy) hits the fixture.
    """
    fp = "/home/user/project/foo.py"
    main_needle = f'"name":"Read","input":{{"file_path":"{fp}"'
    assert main_needle == needle_for(fp)
    assert main_needle in FIXTURE.read_text()


# ---------------------------------------------------------------------------
# 2. Target-extraction table tests (find_write_targets)
# ---------------------------------------------------------------------------
#
# NOTE: find_write_targets() does NOT apply SKIP_PREFIXES, cwd-resolution, or
# existence filtering — those happen later in main(). So /tmp paths and brand
# new files ARE returned here; they're filtered downstream. The negative cases
# below are inputs the *regexes themselves* must not extract a target from.

POSITIVE_CASES = [
    # (description, command, expected_targets_subset)
    (
        "sed -i",
        "sed -i 's/a/b/' /home/user/project/foo.py",
        ["/home/user/project/foo.py"],
    ),
    (
        "sed -i.bak suffix",
        "sed -i.bak 's/a/b/' /home/user/project/foo.py",
        ["/home/user/project/foo.py"],
    ),
    (
        "awk -i inplace",
        "awk -i inplace '{print}' /home/user/project/data.txt",
        ["/home/user/project/data.txt"],
    ),
    (
        "tee multi-file",
        "echo hi | tee /home/user/project/a.txt /home/user/project/b.txt",
        ["/home/user/project/a.txt", "/home/user/project/b.txt"],
    ),
    (
        "tee -a append",
        "echo hi | tee -a /home/user/project/log.txt",
        ["/home/user/project/log.txt"],
    ),
    (
        "redirect overwrite >",
        "echo hi > /home/user/project/out.txt",
        ["/home/user/project/out.txt"],
    ),
    (
        "redirect append >>",
        "echo hi >> /home/user/project/out.txt",
        ["/home/user/project/out.txt"],
    ),
    (
        "redirect stderr 2>",
        "cmd 2> /home/user/project/err.txt",
        ["/home/user/project/err.txt"],
    ),
    (
        "redirect all &>",
        "cmd &> /home/user/project/all.txt",
        ["/home/user/project/all.txt"],
    ),
    (
        "python open w",
        "python3 -c \"open('/home/user/project/x.py', 'w').write('hi')\"",
        ["/home/user/project/x.py"],
    ),
    (
        "python open a",
        "python3 -c \"open('/home/user/project/x.py', 'a').write('hi')\"",
        ["/home/user/project/x.py"],
    ),
    (
        "python open wb",
        "python3 -c \"open('/home/user/project/x.bin', 'wb').write(b'hi')\"",
        ["/home/user/project/x.bin"],
    ),
    (
        "python heredoc open w",
        "python3 << 'PY'\nopen('/home/user/project/gen.py', 'w').write('x')\nPY",
        ["/home/user/project/gen.py"],
    ),
]


@pytest.mark.parametrize(
    "desc,cmd,expected",
    POSITIVE_CASES,
    ids=[c[0] for c in POSITIVE_CASES],
)
def test_find_write_targets_positive(desc, cmd, expected):
    got = hook.find_write_targets(cmd)
    for want in expected:
        assert want in got, f"{desc}: expected {want!r} in {got!r}"


NEGATIVE_CASES = [
    # (description, command) — must NOT yield any of the "real" file targets.
    ("process substitution input", "diff <(sort a) <(sort b)"),
    ("stderr-to-stdout dup 2>&1", "make 2>&1 | tee_nothing"),
    ("plain pipe no redirect", "cat a | grep b | wc -l"),
    ("read-only sed (no -i)", "sed 's/a/b/' /home/user/project/foo.py"),
    ("read-only awk (no inplace)", "awk '{print}' /home/user/project/foo.py"),
    (
        "python open read-mode",
        "python3 -c \"open('/home/user/project/foo.py', 'r').read()\"",
    ),
]


@pytest.mark.parametrize(
    "desc,cmd",
    NEGATIVE_CASES,
    ids=[c[0] for c in NEGATIVE_CASES],
)
def test_find_write_targets_negative(desc, cmd):
    got = hook.find_write_targets(cmd)
    # No real project file should be extracted as a write target.
    assert all("/home/user/project/" not in t for t in got), (
        f"{desc}: unexpected target(s) extracted: {got!r}"
    )


def test_stderr_to_stdout_dup_does_not_extract_fd():
    """`2>&1` must not be read as a redirect to a file named '&1' or '1'."""
    got = hook.find_write_targets("make 2>&1")
    assert "&1" not in got
    assert "1" not in got


def test_brand_new_file_extracted_but_skipped_downstream():
    """A redirect to a non-existent file IS extracted; main() skips it via existence check.

    Documents the division of labor: extraction is permissive, main() filters.
    """
    cmd = "echo hi > /home/user/project/brand_new_does_not_exist.txt"
    got = hook.find_write_targets(cmd)
    assert "/home/user/project/brand_new_does_not_exist.txt" in got


def test_skip_prefix_tmp_extracted_but_filtered_downstream():
    """SKIP_PREFIXES (e.g. /tmp) are NOT applied in find_write_targets, only in main()."""
    assert "/tmp/" in hook.SKIP_PREFIXES
    got = hook.find_write_targets("echo hi > /tmp/x")
    assert "/tmp/x" in got  # extracted here; main() drops it via SKIP_PREFIXES


# ---------------------------------------------------------------------------
# 3. Ported fork behaviours: telling code from data, and cwd resolution.
# ---------------------------------------------------------------------------
#
# Each test pairs the negative (this is NOT a write) with a positive control
# (this IS one) so a harness that extracts nothing cannot read as a pass.


def test_ge_comparison_is_not_a_redirect():
    """`n >= 0` is a comparison; `n > 0.txt` (same shape, no `=`) is a redirect."""
    assert hook.find_write_targets("[[ $n >= 0 ]] && echo ok") == []
    assert hook.find_write_targets("python3 -c 'print(1 >= 0)'") == []
    got = hook.find_write_targets("echo x > /home/user/project/0.txt")
    assert got == ["/home/user/project/0.txt"]


def test_effective_cwd_reads_literal_leading_cd_only():
    """`cd /a && echo x > rel.txt` resolves against /a; `cd "$D" && ...` is not determinable."""
    cwd, ok = hook.effective_cwd("cd /a && echo x > rel.txt", "/session")
    assert (cwd, ok) == ("/a", True)
    assert hook.resolve_path("rel.txt", cwd) == "/a/rel.txt"
    cwd, ok = hook.effective_cwd('cd "$D" && echo x > rel.txt', "/session")
    assert ok is False
    # A cd past the leading prefix (after a pipe / inside the command) is out of reach.
    cwd, ok = hook.effective_cwd("echo x | (cd /b && cat > rel.txt)", "/session")
    assert (cwd, ok) == ("/session", False)
    # No cd at all: session cwd, determinable.
    assert hook.effective_cwd("echo x > rel.txt", "/session") == ("/session", True)
    # Relative and chained cds compose.
    assert hook.effective_cwd("cd a; cd b && echo x > f", "/s") == ("/s/a/b", True)


def test_data_heredoc_body_is_not_a_write():
    """`cat > f <<EOT ... >x ... EOT` writes only f; the `>x` in the body is data."""
    cmd = "cat > /home/user/project/f <<EOT\necho hi > /home/user/project/x\nEOT"
    got = hook.find_write_targets(cmd)
    assert got == ["/home/user/project/f"]
    # `.sh` in the target must not read as the `sh` interpreter (extension != command).
    cmd = "cat > /home/user/project/t.sh <<'EOT'\necho hi > /home/user/project/x\nEOT"
    assert hook.find_write_targets(cmd) == ["/home/user/project/t.sh"]
    # tee-fed heredoc likewise.
    cmd = "tee /home/user/project/f <<EOT\nopen('/home/user/project/x', 'w')\nEOT"
    assert hook.find_write_targets(cmd) == ["/home/user/project/f"]


def test_interpreter_fed_heredoc_stays_in_scope():
    """`python3 - <<PY open('g','w') PY` executes its body, so g IS a write target."""
    cmd = "python3 - <<PY\nopen('/home/user/project/g', 'w').write('x')\nPY"
    assert hook.find_write_targets(cmd) == ["/home/user/project/g"]
    cmd = "cd /x && python3 - <<'PY'\nopen('/home/user/project/g', 'w')\nPY"
    assert "/home/user/project/g" in hook.find_write_targets(cmd)
    cmd = "bash <<'SH'\necho x > /home/user/project/g\nSH"
    assert "/home/user/project/g" in hook.find_write_targets(cmd)
    # Negative control: the same body fed to cat is data.
    cmd = "cat <<PY\nopen('/home/user/project/g', 'w').write('x')\nPY"
    assert hook.find_write_targets(cmd) == []


def test_quoted_gt_is_not_a_redirect_unless_handed_to_a_shell():
    """`git commit -m 'a > b'` yields nothing; `bash -c "echo x > f"` yields f."""
    assert hook.find_write_targets("git commit -m 'a > b'") == []
    assert hook.find_write_targets('git commit -m "fix: a > /home/user/project/b"') == []
    assert hook.find_write_targets('bash -c "echo x > /home/user/project/f"') == [
        "/home/user/project/f"
    ]
    assert hook.find_write_targets("sh -c 'echo x > /home/user/project/f'") == [
        "/home/user/project/f"
    ]
    # Unquoted redirect beside a quoted `>` is still live.
    assert hook.find_write_targets("echo 'a > b' > /home/user/project/f") == [
        "/home/user/project/f"
    ]
