# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow SemVer.

## [Unreleased]

### Fixed

- **The guard now checks that the Read succeeded and that the file is unchanged since.**
  Previously both hooks grepped the transcript for the Read _request_ substring
  (`"name":"Read","input":{"file_path":...`). That predicate could not see (a) a Read whose
  `tool_result` was an error — oversize file, missing `pdftoppm`, permission denied — so a
  later Edit of a file the model never actually saw passed; nor (b) a file modified on disk
  after the Read, so an Edit from a stale mental model passed. Both were real events in
  real transcripts. Found by an independent review panel on 2026-09-15.

### Changed

- New shared module `fresh_read_check.py` holds the predicate; both hooks call it so the
  two cannot drift. It pairs each `Read`/`Edit`/`Write`/`NotebookEdit` `tool_use` with its
  `tool_result` by id and allows the write iff `mtime(file) <= ts(latest non-error result)`.
- **Install now copies three files**, not two; the hooks warn on stderr and stop enforcing
  if `fresh_read_check.py` is not beside them.
- Deny reason strings now say _why_: "no successful Read in this session" vs
  "changed on disk since your last Read at HH:MM:SSZ".
- Your own `Edit`/`Write` of a file counts as refreshing it. A formatter hook or another
  process that rewrites the file afterwards does not — you will be asked to re-Read.

### Added

- `tests/test_freshness.py`: end-to-end subprocess tests of both hooks with synthetic
  transcripts and real mtimes — failed-Read, stale-after-Read, own-Edit-refreshes,
  never-read, new-file — each negative paired with a positive control in the same test.
  All six negatives were confirmed to fail against the pre-fix hooks.
- **Bash hook: leading `cd <dir> &&` is followed before resolving relative write targets.**
  `cd ~/x && python3 - <<PY open("f","w") PY` was resolved against the session cwd and
  passed both guards (2026-09-04). `effective_cwd()` reads a literal leading `cd` chain;
  `cd "$VAR"`, a cd after a pipe or inside a subshell return `determinable=False` rather
  than a guess (the hook stays best-effort there).
- **Bash hook: code is told from data.** Data heredoc bodies (`cat > f <<EOT`, `tee f <<EOT`)
  and `>` inside quoted strings (`git commit -m 'a > b'`) are no longer write targets —
  the guard had blocked its author three times in one day on such commands. Heredocs fed
  to an interpreter (`python3 - <<PY`, `bash <<SH`) and quoted redirects handed to a shell
  (`bash -c "echo x > f"`) STAY in scope; both are real bypass routes.
- **Bash hook: `n >= 0` is a comparison, not a redirect to `=`** (`REDIR_RE` `(?!=)`).
- `tests/test_hooks.py`: a positive and negative case for each of the above, in the same
  test; all five new tests confirmed failing against the pre-port hook. The loader honours
  `FRESH_READ_GUARD_BASH=<path>` so a test can be run against another copy of the hook.

## [1.0.0] - 2026-06-09

Initial release: `fresh-read-guard.sh` (PreToolUse Edit|Write) and
`fresh-read-guard-bash.py` (PreToolUse Bash) transcript-grep guards.
