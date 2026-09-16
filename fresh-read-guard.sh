#!/usr/bin/env bash
# fresh-read-guard.sh — PreToolUse(Edit|Write) Iron Law of Current State enforcement.
#
# Blocks edits to files not Read in this session. Override: IRON_LAW_OVERRIDE=1.
#
# Stateless transcript-grep — survives compaction (on-disk JSONL preserves Read entries
# even when working context is summarized). Belt-and-suspenders deny: emits structured
# permissionDecision JSON to stdout AND stderr AND exits 2 (both deny channels have
# documented bugs; emitting both means at least one channel succeeds across versions).
#
# Safe-fail: every parse error falls through to exit 0. A broken hook must not turn
# every Edit into a block.

trap 'exit 0' ERR

[[ "$IRON_LAW_OVERRIDE" == "1" ]] && exit 0

command -v jq >/dev/null 2>&1 || exit 0

PAYLOAD=$(cat)
TRANSCRIPT=$(jq -r '.transcript_path // empty' <<<"$PAYLOAD")
TOOL=$(jq -r '.tool_name // empty' <<<"$PAYLOAD")
FP=$(jq -r '.tool_input.file_path // empty' <<<"$PAYLOAD")
AGENT_ID=$(jq -r '.agent_id // empty' <<<"$PAYLOAD")

# Allow-pass conditions
[[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" ]] && exit 0
[[ -z "$FP" ]] && exit 0
[[ "$TOOL" != "Edit" && "$TOOL" != "Write" ]] && exit 0
[[ ! -e "$FP" ]] && exit 0 # new-file Write — no prior content to read

# Freshness predicate lives in fresh_read_check.py (shared with the Bash hook):
# the last SUCCESSFUL Read/Edit/Write of $FP (tool_result present, not is_error) must be
# no older than the file's mtime. A grep for the Read *request* can see neither.
CHECK="$(dirname "$(readlink -f "$0")")/fresh_read_check.py"
if [[ ! -f "$CHECK" ]] || ! command -v python3 >/dev/null 2>&1; then
	echo "fresh-read-guard: $CHECK or python3 missing — guard is NOT enforcing" >&2
	exit 0
fi

# When invoked from a subagent, the issuing subagent's own transcript is also consulted:
# <parent_transcript_without_.jsonl>/subagents/agent-<agent_id>.jsonl (siblings are not).
SUB_TRANSCRIPT=""
if [[ -n "$AGENT_ID" ]]; then
	CAND="${TRANSCRIPT%.jsonl}/subagents/agent-${AGENT_ID}.jsonl"
	[[ -f "$CAND" ]] && SUB_TRANSCRIPT="$CAND"
fi

if REASON=$(python3 "$CHECK" "$FP" "$TRANSCRIPT" "$SUB_TRANSCRIPT"); then
	exit 0
fi
[[ -z "$REASON" ]] && exit 0 # predicate errored internally → safe-fail
REASON="$REASON Override: IRON_LAW_OVERRIDE=1"

# Block: belt-and-suspenders (both deny channels)
jq -cn --arg r "$REASON" \
	'{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
echo "$REASON" >&2
exit 2
