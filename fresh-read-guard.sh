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

# Anchored grep needle (anchored on tool name to avoid Edit/Write entry FPs)
NEEDLE="\"name\":\"Read\",\"input\":{\"file_path\":\"$FP\""
grep -qF "$NEEDLE" "$TRANSCRIPT" && exit 0

# When invoked from a subagent, also check the issuing subagent's own transcript.
# Harness passes the parent's transcript_path even from a subagent context, but the
# subagent's tool calls land in <parent_transcript_without_.jsonl>/subagents/agent-<agent_id>.jsonl.
# Only the issuing subagent's own transcript is checked (sibling subagents' Reads are not honored).
if [[ -n "$AGENT_ID" ]]; then
	SUB_TRANSCRIPT="${TRANSCRIPT%.jsonl}/subagents/agent-${AGENT_ID}.jsonl"
	[[ -f "$SUB_TRANSCRIPT" ]] && grep -qF "$NEEDLE" "$SUB_TRANSCRIPT" && exit 0
fi

# Block: belt-and-suspenders (both deny channels)
cat <<JSON
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Iron Law: Read $FP before editing it. Override: IRON_LAW_OVERRIDE=1"}}
JSON
echo "Iron Law: Read $FP before editing it. (override: IRON_LAW_OVERRIDE=1)" >&2
exit 2
