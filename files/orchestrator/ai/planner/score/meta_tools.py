"""
meta_tools.py — the decompose/alternatives meta-tool schemas + node constants.

The two meta-tools the model may call instead of a primitive (`decompose` = AND,
ordered steps; `alternatives` = OR, one-of), the node system prompt, and the
opaque-tool set whose results aren't self-evidently successful.
"""

from typing import Any, Dict

# OPAQUE tools: their result is free-text, so success isn't self-evident from the
# call. The honesty rule (foreign-command grounding): an opaque command with no
# declared post-condition must surface as UNVERIFIABLE, never silently `done`.
_OPAQUE_TOOLS = {"run_guest_command"}


# The meta-tool the model calls to say "this isn't one primitive — here are the steps".
DECOMPOSE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "decompose",
        "description": (
            "Use ONLY when the goal needs MORE than one primitive tool call. Break it "
            "into an ordered list of smaller sub-goals; each will then be handled by a "
            "single tool call (or decomposed again if still too big). If the goal can be "
            "done with ONE tool call, call THAT tool instead — do not decompose."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Ordered sub-goals in PLAIN ENGLISH, e.g. "
                        "['create a ubuntu vm called dev', 'launch dev']. Each is a short "
                        "instruction of WHAT to do — NEVER tool names or tool-call syntax "
                        "like create_vm(name=...). Describe the action in words."
                    ),
                },
            },
            "required": ["steps"],
        },
    },
}

# The meta-tool for an OR goal: several ways to get there, only ONE need succeed.
ALTERNATIVES_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "alternatives",
        "description": (
            "Use ONLY when the goal has SEVERAL possible approaches and just ONE needs "
            "to succeed (an OR goal). List the alternative ways to achieve the SAME goal, "
            "MOST-LIKELY first; they are tried in order and the first that works wins — "
            "the rest are skipped. This is DIFFERENT from `decompose`, whose steps must "
            "ALL be done. If the goal is one action, call THAT tool instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Alternative sub-goals in PLAIN ENGLISH, best first — each a "
                        "COMPLETE way to achieve the goal on its own, e.g. "
                        "['reboot the vm via the guest agent', 'force-reset the vm']. "
                        "NEVER tool names or call syntax."
                    ),
                },
            },
            "required": ["options"],
        },
    },
}

_NODE_SYSTEM = (
    "You are reducing a goal to tool calls. For the goal below, either call ONE tool "
    "that fully accomplishes it, or — if it needs more than one primitive action — call "
    "`decompose` with the ordered sub-steps. Prefer a single tool call when possible."
)
