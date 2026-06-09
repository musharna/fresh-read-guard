# Security Policy

## Threat model (read this first)

`fresh-read-guard` is a **best-effort developer guardrail, not a security boundary.**
It exists to catch an honest mistake — editing a file from a stale mental model
instead of the live bytes — not to contain a hostile or adversarial agent.

Two properties follow directly from that goal, and you should design around them:

### It fails _open_ by design

Every parse error, IO error, missing dependency (`jq`), or unrecognized command
shape falls through to `exit 0` (allow). This is deliberate: a guardrail that
turned every `Edit` or `Bash` call into a hard block on its own bugs would be
worse than no guardrail at all. The cost of that choice is that **a parse miss
allows the write.** If the Bash hook's regexes don't recognize a write form
(an exotic `dd`, an `ex`/`ed` script, an unusual redirect, a write performed by
a child process the hook never sees), the write proceeds unguarded.

### It is not a sandbox

The hook inspects the _command string_ and the _session transcript_. It does not:

- intercept syscalls or filesystem writes,
- track writes performed by spawned subprocesses or compiled programs,
- prevent a determined caller from bypassing it (e.g. `IRON_LAW_OVERRIDE=1`,
  the inline `# IRON_LAW_OK` token, base64/eval-obfuscated commands, or any
  write path the regexes don't model).

Do **not** rely on it to stop a malicious actor, to enforce a policy against an
untrusted agent, or as a substitute for OS-level permissions, containerization,
or filesystem sandboxing. Treat it as a lint for read-before-write discipline.

## Installing means executing

These hooks are scripts that Claude Code runs on every matching tool call with
your shell's privileges. **Review `fresh-read-guard.sh` and
`fresh-read-guard-bash.py` before installing**, exactly as you would any script
you pipe from `curl` into your config. They are small and dependency-light
specifically so they can be read end-to-end in a few minutes.

## Reporting a vulnerability

If you find a way the guard can be made to **block legitimately-read writes**,
**leak a path it shouldn't**, or **execute attacker-controlled input** beyond
the documented fail-open behavior, please report it privately:

- Open a **GitHub private security advisory**:
  <https://github.com/musharna/fresh-read-guard/security/advisories/new>

Please do not open a public issue for a suspected vulnerability. Public issues
are fine for false-positive/false-negative _behavior_ reports (a write form the
guard should recognize but doesn't), since those are correctness, not secrecy.
