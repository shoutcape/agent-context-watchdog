"""Claude Code hook adapter. Transcript text stays local to this process."""

import json
import math
from pathlib import Path

from .config import caller
from .server import rpc


def transcript_usage(path, fallback_model="unknown"):
    # A bounded tail avoids repeatedly reading multi-GB transcripts. If the latest
    # usage record isn't in the tail, report unknown rather than a false zero.
    # A new session may not have written its transcript yet; that is unknown too.
    if not Path(path).exists():
        return fallback_model, None
    with Path(path).open("rb") as stream:
        stream.seek(0, 2)
        offset = max(0, stream.tell() - 8 * 1024 * 1024)
        stream.seek(offset)
        if offset:
            stream.readline()
        lines = stream.read().splitlines()
    trailing = 0
    model = fallback_model
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue  # A writer may not have completed the final JSON line yet.
        if entry.get("isSidechain") or entry.get("agentId"):
            continue
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            return model, None
        message = entry.get("message", {})
        if not isinstance(message, dict):
            continue
        if entry.get("type") == "assistant":
            model = message.get("model") or model
            usage = message.get("usage")
            if not isinstance(usage, dict) or "input_tokens" not in usage:
                continue
            fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
            counts = [usage.get(key, 0) for key in fields]
            if any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in counts):
                return model, None
            if not sum(counts):
                continue  # A failed/placeholder response is not a context reset.
            return model, math.ceil(sum(counts)) + trailing
        if entry.get("type") == "user":
            trailing += math.ceil(len(json.dumps(message.get("content", ""), ensure_ascii=False)) / 4)
    return model, None


def run_hook(paths, payload):
    if payload.get("agent_id") or payload.get("hook_event_name") == "SubagentStop":
        return
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop"):
        return
    model, tokens = transcript_usage(payload["transcript_path"], payload.get("model") or "unknown")
    identity = caller()
    response = rpc(paths, {
        **identity, "op": "observe", "kind": "claude", "session": payload["session_id"],
        "model": model, "tokens": tokens, "quality": "estimated", "cwd": payload["cwd"],
    })
    notification = response.get("notification")
    if not notification or (event == "Stop" and payload.get("stop_hook_active")):
        return
    # The agent already weighed the first reminder. Blocking a finished turn for a repeat
    # only forces a wasted turn, so hold it for the next prompt or tool call instead.
    if event == "Stop" and notification.get("repeat"):
        return
    if event == "Stop":
        output = {"decision": "block", "reason": notification["text"]}
    else:
        output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": notification["text"]}}
    print(json.dumps(output), flush=True)
    rpc(paths, {**identity, "op": "delivered", "key": response["key"], "id": notification["id"]})
