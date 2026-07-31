#!/usr/bin/env python3
"""
PreToolUse hook — block destructive Bash commands.

Receives JSON on stdin, writes deny decision to stdout, exits 0.
Registered in .claude/settings.json for the Bash tool.

This is a defense-in-depth layer: it fires even when
--dangerously-skip-permissions is active, and even inside sub-agents
(e.g. the planner), so it covers patterns the static allow/deny list
in settings.json cannot fully express.
"""
import json
import re
import sys

data = json.load(sys.stdin)
command = data.get("tool_input", {}).get("command", "")

# Each entry: (regex_pattern, human_readable_reason)
BLOCKED = [
    (
        r"(?<!\w)rm\s+[^|&;]*-[a-zA-Z]*[rR][a-zA-Z]*[fF]"
        r"|(?<!\w)rm\s+[^|&;]*-[a-zA-Z]*[fF][a-zA-Z]*[rR]"
        r"|(?<!\w)rm\s+[^|&;]*--force[^|&;]*--recursive"
        r"|(?<!\w)rm\s+[^|&;]*--recursive[^|&;]*--force",
        "Recursive force-remove (rm -rf or equivalent) is not permitted",
    ),
    (
        r"(?<!\w)git\s+push\s+[^|&;]*(?:-f\b|--force)",
        "Force-push is not permitted; use the human approval gate for pushes",
    ),
    (
        r"(?:^|[|&;`\n]\s*)sudo\b",
        "sudo is not permitted inside the agent loop",
    ),
    (
        r"(?<!\w)dd\s+if=",
        "dd if= is not permitted",
    ),
    (
        r"(?<!\w):()\{.*\};:",
        "Fork-bomb pattern detected",
    ),
    (
        r">\s*/dev/sd[a-z]\b|>\s*/dev/nvme",
        "Writing directly to block devices is not permitted",
    ),
]

for pattern, reason in BLOCKED:
    if re.search(pattern, command, re.IGNORECASE):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"block-destructive hook: {reason}",
            }
        }))
        sys.exit(0)

sys.exit(0)
