"""
http_chat.py — HTTP-mode single-turn chat processor.

process_message() runs one user message through the agentic tool loop without
any stdin/stdout — the orchestrator's /chat endpoint (api_server) calls it and
gets back a dict (text, updated messages, tool_results, needs_input). The
interactive REPL equivalent is chat_loop() in cli.py.

Imports every dependency from its own source module and loads its own _CFG, so
it never imports from cli — the edge is one-directional (cli -> http_chat).
"""

import json
import os

from orchestrator.sanitizer.sanitizer import OS_TYPE_ALIASES
from orchestrator.sanitizer.context_gate import _REQUIRED as _GATE_REQUIRED
from orchestrator.executor_client import execute_tool, _VM_TOOLS
from orchestrator.preflight.validator import _preflight_check
from .ollama_client import _call_ollama
from .active_library import LIBRARY
from .context_assistant import check_context, extract_slots
from .session import get_loop_max
from .chat_turn import _is_critical
try:
    from shared.executioner.tool_executor import manager
except ImportError:
    manager = None                                                            # type: ignore[assignment]

_CFG          = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_LOOP_MAX     = get_loop_max()
_ACTION_WORDS = set(_CFG["action_words"])
# State/read-query words — folded into the "wants a tool" trigger so factual
# questions get grounded in a tool call instead of answered from memory.
_STATE_QUERY_WORDS = set(_CFG.get("state_query_words", []))
_OS_KEYWORDS  = set(_CFG["os_keywords_gate"])
_CONFIRM_YN   = {k: tuple(v) for k, v in _CFG["confirm_yn"].items()}
_CONFIRM_NAME = {k: tuple(v) for k, v in _CFG["confirm_name"].items()}
_FLEET_CONFIRM_ACTIONS = set(_CFG.get("fleet_confirm_actions", ["exec", "stop"]))  # fleet actions needing y/n


def process_message(
    user_input: str,
    messages: list,
    verbose: bool = False,
    auto_confirm: bool = False,
) -> dict:
    """
    Process one user message through the agentic tool loop without stdin/stdout.
    Used by the HTTP /chat endpoint.

    Returns:
        {
            "text": str,              # assistant response text (may be empty)
            "messages": list,         # updated conversation history
            "tool_results": list,     # [{tool, args, result}, ...] from this turn
            "needs_input": dict|None, # non-null when user confirmation/clarification is required
        }

    needs_input shape:
        {
            "type": "confirm_yn" | "confirm_name" | "confirm_critical" | "preflight" | "clarify",
            "question": str,
            "options": list[str],
            "field": str|None,
            "tool_name": str|None,
            "proposed": str|None,
        }

    When needs_input is returned the caller should:
      1. Show the question/options to the user.
      2. Send the user's reply as the next message, with auto_confirm=True if the
         user confirmed a destructive action.
    """
    import re as _re

    messages = list(messages)
    messages.append({"role": "user", "content": user_input})

    # Active Library: build once (server process is long-lived; apply() keeps it
    # fresh per action thereafter). The system prompt reads its digest each turn.
    if not LIBRARY.built:
        LIBRARY.snapshot()

    _ui = user_input.lower().strip()
    # "action" = any tool-worthy intent (an action OR a state/read query), so
    # factual questions get grounded in a tool call rather than confabulated.
    _user_wants_action = bool({t.strip('.,!?;:') for t in _ui.split()} & (_ACTION_WORDS | _STATE_QUERY_WORDS))
    _tools_called_this_turn   = False
    _tool_executed_this_turn  = False
    _context_assistant_fired  = False
    _just_clarified_fields: set = set()
    _just_clarified_values: set = set()
    _confirmed_values: set      = set()
    _confirmed_tool_types: set  = set()
    _last_had_tools             = False
    _tool_results: list         = []

    for _loop_iter in range(_LOOP_MAX):
        response = _call_ollama(messages)
        if not response:
            break

        msg = response.get("message", {})
        assistant_msg = {
            "role":       "assistant",
            "content":    msg.get("content", ""),
            "tool_calls": msg.get("tool_calls", []),
        }
        messages.append(assistant_msg)

        tool_calls = msg.get("tool_calls", [])
        _last_had_tools = bool(tool_calls)

        if not tool_calls:
            text = msg.get("content", "").strip()
            if not text and _loop_iter < _LOOP_MAX - 1:
                messages.pop()
                messages.append({"role": "user", "content": "_INTERNAL_ Your last response was empty. Please call the appropriate tool or provide a text response now."})
                continue
            if _user_wants_action and not _tools_called_this_turn and _loop_iter < _LOOP_MAX - 1:
                messages.pop()
                messages.append({"role": "user", "content": "_INTERNAL_ You responded with text but did not call any tool. You cannot perform actions by text alone — you MUST call the appropriate tool. Call the tool now."})
                continue
            return {"text": text, "messages": messages, "tool_results": _tool_results, "needs_input": None}

        _tools_called_this_turn = True
        _op_cancelled = False

        for tc in tool_calls:
            fn        = tc.get("function", {})
            tool_name = fn.get("name", "")
            raw_args  = fn.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = {}

            # ── Context assistant ──────────────────────────────────────────
            _recent_user_msgs = [
                m.get("content", "").lower() for m in messages
                if m.get("role") == "user"
                and not str(m.get("content", "")).startswith("_INTERNAL_")
            ]
            _recent_context = " ".join(_recent_user_msgs[-6:])
            if not _context_assistant_fired:
                _known_names = None
                if tool_name in _VM_TOOLS:
                    _known_names = (LIBRARY.known_names() if LIBRARY.built
                                    else {v["name"] for v in execute_tool("list_vms", {}, verbose=True, log=False)})
                _ca_hint = check_context(user_input, tool_name, raw_args, recent_context=_recent_context,
                                          known_names=_known_names)
                if _ca_hint:
                    _context_assistant_fired = True
                    if "never mentioned it" in _ca_hint:
                        _fields = _re.findall(r"You set (\w+)=", _ca_hint)
                        if _fields:
                            messages.pop()
                            return {
                                "text": "",
                                "messages": messages,
                                "tool_results": _tool_results,
                                "needs_input": {
                                    "type":      "clarify",
                                    "question":  f"What {_fields[0]} would you like to use?",
                                    "options":   [],
                                    "field":     _fields[0],
                                    "tool_name": tool_name,
                                    "proposed":  None,
                                },
                            }
                    else:
                        messages.pop()
                        messages.append({"role": "user", "content": f"_INTERNAL_ {_ca_hint} Re-evaluate and call the correct tool."})
                        break

            # ── OS type guard ──────────────────────────────────────────────
            if tool_name == "create_vm" and "os_type" not in _just_clarified_fields:
                _ui_tokens  = {t.strip('.,!?;:') for t in _ui.split()}
                _matched_kw = next(iter(_OS_KEYWORDS & _ui_tokens), None)
                if _matched_kw:
                    _canonical = OS_TYPE_ALIASES.get(_matched_kw, _matched_kw)
                    raw_args   = dict(raw_args)
                    raw_args["os_type"] = _canonical
                    _just_clarified_fields.add("os_type")
                    _just_clarified_values.add(("os_type", _canonical))
                elif "os_type" in raw_args:
                    raw_args = dict(raw_args)
                    raw_args.pop("os_type")

            # ── Preflight ──────────────────────────────────────────────────
            _pf = _preflight_check(tool_name, raw_args, manager, verbose)
            _pf_action = _pf.get("action", "ok")

            if _pf_action == "abort":
                messages.append({"role": "tool", "content": json.dumps({"success": False, "error": _pf["reason"]}, default=str)})
                messages.append({"role": "user", "content": f"_INTERNAL_ {_pf['reason']}. {_pf.get('correction', '')} Do not retry this operation."})
                break

            elif _pf_action == "auto_fix":
                raw_args = _pf["fixed_args"]

            elif _pf_action == "ask_user":
                if not auto_confirm:
                    messages.pop()
                    return {
                        "text": "",
                        "messages": messages,
                        "tool_results": _tool_results,
                        "needs_input": {
                            "type":      "preflight",
                            "question":  _pf.get("question", "Please confirm."),
                            "options":   _pf.get("options", []),
                            "field":     _pf.get("fix_field"),
                            "tool_name": tool_name,
                            "proposed":  None,
                        },
                        "pending_tool": {"tool_name": tool_name, "args": raw_args,
                                         "critical": _is_critical(tool_name, raw_args)},
                    }

            if tool_name == "create_profile" and _pf_action in ("ok", "auto_fix"):
                raw_args = dict(raw_args)
                raw_args["force"] = True

            # ── Fleet broadcast confirm (action-conditional: exec + stop) ──
            # fleet isn't in _CONFIRM_YN (that map is per-tool, not per-action),
            # so it's gated here explicitly — mirrors _fleet_confirm in chat_turn.
            if tool_name == "fleet":
                _fa   = (raw_args.get("action") or "").strip().lower()
                _fkey = ("fleet", _fa, raw_args.get("label", ""), raw_args.get("command", ""))
                if _fa in _FLEET_CONFIRM_ACTIONS and _fkey not in _confirmed_values:
                    if not auto_confirm:
                        if _fa == "exec":
                            _fq = (f"Run '{raw_args.get('command','')}' on every VM "
                                   f"labeled '{raw_args.get('label','')}'?")
                        else:
                            _fq = f"Stop every VM labeled '{raw_args.get('label','')}'?"
                        messages.pop()
                        return {
                            "text": "",
                            "messages": messages,
                            "tool_results": _tool_results,
                            "needs_input": {
                                "type":      "confirm_yn",
                                "question":  _fq,
                                "options":   ["Yes", "Cancel"],
                                "field":     "action",
                                "tool_name": "fleet",
                                "proposed":  _fa,
                            },
                            "pending_tool": {"tool_name": tool_name, "args": raw_args,
                                             "critical": False},
                        }
                    _confirmed_values.add(_fkey)

            # ── Safety confirmation gate ───────────────────────────────────
            _conf_entry = _CONFIRM_YN.get(tool_name) or _CONFIRM_NAME.get(tool_name)
            if _conf_entry:
                field, verb = _conf_entry
                proposed    = raw_args.get(field, "")

            if _conf_entry and (field, proposed) not in _just_clarified_values and (field, proposed) not in _confirmed_values:
                if not auto_confirm:
                    if _is_critical(tool_name, raw_args):
                        conf_type = "confirm_critical"
                        question  = f"{verb}: {proposed} — this will also delete its disk(s). Type YES then the VM name to confirm."
                    elif tool_name in _CONFIRM_YN:
                        conf_type = "confirm_yn"
                        question  = f"{verb}: {proposed}"
                    else:
                        conf_type = "confirm_name"
                        question  = f"{verb}: {proposed}. Type the exact name to confirm."
                    messages.pop()
                    return {
                        "text": "",
                        "messages": messages,
                        "tool_results": _tool_results,
                        "needs_input": {
                            "type":      conf_type,
                            "question":  question,
                            "options":   ["Yes", "Cancel"] if tool_name in _CONFIRM_YN else [],
                            "field":     field,
                            "tool_name": tool_name,
                            "proposed":  proposed,
                        },
                        "pending_tool": {"tool_name": tool_name, "args": raw_args,
                                         "critical": _is_critical(tool_name, raw_args)},
                    }
                _confirmed_values.add((field, proposed))

            # ── Pre-execution gate ─────────────────────────────────────────
            _gate_required  = _GATE_REQUIRED.get(tool_name, [])
            _pre_gate_result = None
            if _gate_required and tool_name != "clarify":
                _user_slots = extract_slots(user_input)
                for _clf in _just_clarified_fields:
                    if _clf in raw_args and raw_args[_clf]:
                        _user_slots[_clf] = raw_args[_clf]
                _missing_early = [
                    {"field": f, "question": q, "options": opts}
                    for f, q, opts in _gate_required
                    if f in _user_slots and _user_slots[f] is None
                    and not (
                        raw_args.get(f)
                        and isinstance(raw_args.get(f), str)
                        and (
                            raw_args[f].lower() in _recent_context
                            or raw_args[f].lower().replace(" ", "") in _recent_context.replace(" ", "")
                        )
                    )
                ]
                if _missing_early:
                    _pre_gate_result = {
                        "success":             False,
                        "clarify":             True,
                        "missing":             _missing_early,
                        "question":            _missing_early[0]["question"],
                        "options":             _missing_early[0]["options"],
                        "needs_clarification": _missing_early[0]["field"],
                        "error":               f"Missing required arguments for {tool_name}: {[m['field'] for m in _missing_early]}",
                    }

            # ── Execute ────────────────────────────────────────────────────
            if _pre_gate_result:
                result = _pre_gate_result
            else:
                result = execute_tool(tool_name, raw_args, verbose)
                _tool_executed_this_turn = True
                _tool_results.append({"tool": tool_name, "args": raw_args, "result": result})
                LIBRARY.apply(tool_name, raw_args)   # targeted registry update

            # Clarify response from executor — pause and return to client.
            if isinstance(result, dict) and result.get("clarify"):
                missing_fields = result.get("missing") or [{
                    "field":    result.get("needs_clarification", ""),
                    "question": result.get("question", "Please provide more detail."),
                    "options":  result.get("options", []),
                }]
                mf = missing_fields[0]
                messages.append({"role": "tool", "content": json.dumps(result, default=str)})
                return {
                    "text": "",
                    "messages": messages,
                    "tool_results": _tool_results,
                    "needs_input": {
                        "type":      "clarify",
                        "question":  mf["question"],
                        "options":   mf.get("options", []),
                        "field":     mf.get("field", ""),
                        "tool_name": tool_name,
                        "proposed":  None,
                    },
                }

            messages.append({"role": "tool", "content": json.dumps(result, default=str)})

        if _context_assistant_fired and not tool_calls:
            continue

        if _op_cancelled:
            continue

    if _user_wants_action and not _tool_executed_this_turn and messages:
        last = messages[-1]
        if last.get("role") == "assistant" and not last.get("tool_calls"):
            messages.pop()

    return {"text": "", "messages": messages, "tool_results": _tool_results, "needs_input": None}
