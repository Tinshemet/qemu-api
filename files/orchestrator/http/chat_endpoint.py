"""
orchestrator/http/chat_endpoint.py — the /chat turn handler.

The full agentic tool loop (Ollama + tool execution), the forge-wizard elicitation,
the contract-CLI redirect, and the confirmed-action fast path — everything the /chat
route dispatches once auth has passed. Kept out of api_server.py so that module stays
routing + auth; this holds the logic. Session state lives in session_store (shared
with the route) so both mutate the same dict.
"""
import json
import uuid
from typing import Any, Dict, Optional

from . import session_store


def _handle_grant_command(message: str) -> Optional[str]:
    """Operator run_command grant commands, handled deterministically. Returns a reply, or None
    if the message isn't a grant command. Operator-only by construction: only a typed user
    message reaches here, and the AI emits tool calls, not user messages — it can't self-grant.

      grant read <path>    grant net / allow net    revoke net    revoke (clear all)    grants
    """
    from orchestrator import run_command_grants as grants
    m   = message.strip()
    low = m.lower()
    if low in ("grants", "show grants", "grant status"):
        return grants.describe()
    if low in ("revoke", "revoke grants", "clear grants", "ungrant"):
        grants.clear()
        return "run_command grants revoked — back to workspace-only, no network."
    if low in ("grant net", "allow net", "grant network", "allow network"):
        grants.set_net(True)
        return "run_command network access GRANTED for this session."
    if low in ("revoke net", "deny net", "revoke network", "block net"):
        grants.set_net(False)
        return "run_command network access revoked."
    if low.startswith("grant read"):
        path = m[len("grant read"):].strip().lstrip(":").strip()
        if not path:
            return "Usage: grant read <path>"
        r = grants.add_read(path)
        return (f"run_command read access GRANTED for this session: {r['path']}"
                if r.get("success") else f"Could not grant: {r.get('error')}")
    return None


def handle_chat(req: Any, operator: Optional[str]) -> Dict[str, Any]:
    """
    Process one AI chat turn server-side and return the response.

    Session state (conversation history) is kept in-memory on the server,
    keyed by session_id.

    When a dangerous operation requires confirmation, the response contains
    needs_input with the question.  The client shows the dialog and re-sends
    the user's reply with auto_confirm=True once confirmed.
    """
    from orchestrator.ai.chat.cli import process_message
    from orchestrator.executor_client import execute_tool
    from orchestrator.ai.agent import forge_chat
    from orchestrator.ai.agent import forge as _forge
    import shared.bundle as _bundle
    _FORGE_DIR = _bundle.AGENTS_ROOT   # a forged agent lands in its bundle

    session_store.evict_expired()
    sid     = req.session_id or str(uuid.uuid4())
    session = session_store.get_session(sid)
    messages      = list(session["messages"])
    pending_tool  = session.get("pending_tool")
    critical_step2 = session.get("critical_step2", False)
    forge_wizard   = session.get("forge_wizard")

    # ── Operator run_command grants (deterministic; the AI never reaches this — it emits
    # tool calls, not user messages). Skipped mid forge-wizard (those are wizard answers).
    if forge_wizard is None:
        _greply = _handle_grant_command(req.message)
        if _greply is not None:
            return {"session_id": sid, "text": _greply, "tool_results": [], "needs_input": None}

    # ── Forge wizard: deterministic multi-turn contract elicitation ───────────
    # High-impact, so it opens with operator re-auth. Runs entirely here, never
    # touching the AI loop, and its turns (incl. the password) are never appended
    # to `messages` — the transcript stays clean.
    if forge_wizard is not None or (
        not (req.auto_confirm and pending_tool)
        and forge_chat.looks_like_forge_intent(req.message)
    ):
        from orchestrator.auth import store as _op_store

        def _verify(pw: str) -> bool:
            return bool(operator) and _op_store.verify_password(operator, pw)

        if forge_wizard is None:
            state = forge_chat.start(needs_auth=_op_store.operators_exist())
            reply, needs_input = forge_chat.current_prompt(state)
        else:
            state, reply, needs_input, _result = forge_chat.advance(
                forge_wizard, req.message, verify_password=_verify, write_dir=_FORGE_DIR)

        session_store.SESSIONS[sid] = {**session, "messages": messages, "forge_wizard": state,
                          "pending_tool": None, "critical_step2": False}
        return {"session_id": sid, "text": reply,
                "tool_results": [], "needs_input": needs_input}

    # ── Contract CLI redirect: sign/edit/show/list are CLI-only ───────────────
    if forge_wizard is None and not (req.auto_confirm and pending_tool):
        _redirect = forge_chat.contract_cli_redirect(req.message)
        if _redirect:
            return {"session_id": sid, "text": _redirect,
                    "tool_results": [], "needs_input": None}

    # ── Fast-path: confirmed action — skip Ollama ─────────────────────────────
    if req.auto_confirm and pending_tool:
        tool_name = pending_tool["tool_name"]
        args      = pending_tool["args"]
        is_critical = pending_tool.get("critical", False)

        # Critical tools require a second confirmation: user must type the VM name.
        if is_critical and not critical_step2:
            expected = args.get("name", "")
            session_store.SESSIONS[sid] = {**session, "messages": messages,
                               "pending_tool": pending_tool, "critical_step2": True}
            return {
                "session_id":  sid,
                "text":        "",
                "tool_results": [],
                "needs_input": {
                    "type":      "confirm_critical",
                    "question":  f"Type '{expected}' to permanently confirm:",
                    "options":   [],
                    "tool_name": tool_name,
                    "proposed":  expected,
                },
            }

        # Critical step 2: validate the name the user typed.
        if is_critical and critical_step2:
            expected = args.get("name", "")
            typed    = req.message.strip()
            if typed.lower() != expected.lower():
                session_store.SESSIONS[sid] = {**session, "messages": messages,
                                   "pending_tool": pending_tool, "critical_step2": True}
                return {
                    "session_id":  sid,
                    "text":        f"Name didn't match — expected '{expected}'. Try again or type 'cancel'.",
                    "tool_results": [],
                    "needs_input": {
                        "type":      "confirm_critical",
                        "question":  f"Type '{expected}' to permanently confirm:",
                        "options":   [],
                        "tool_name": tool_name,
                        "proposed":  expected,
                    },
                }

        # Inject delete_disks=True for irreversible deletes so disks are cleaned up.
        if tool_name == "delete_vm":
            args = {**args, "delete_disks": True}

        try:
            result_data = execute_tool(tool_name, args, req.verbose)
        except Exception as exc:
            result_data = {"success": False, "error": str(exc)}

        # If executor requires clarification, ask the user then let AI re-plan.
        if result_data.get("clarify"):
            missing_fields = result_data.get("missing") or [{
                "field":    result_data.get("needs_clarification", ""),
                "question": result_data.get("question", "Please provide more detail."),
                "options":  result_data.get("options", []),
            }]
            mf = missing_fields[0]
            # Keep pending_tool so the user's answer goes through the normal AI path
            # with context about what was already confirmed.
            session_store.SESSIONS[sid] = {
                "messages": messages + [
                    {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": tool_name, "arguments": args}}]},
                    {"role": "tool", "content": json.dumps(result_data, default=str)},
                ],
                "pending_tool": None,
                "critical_step2": False,
            }
            return {
                "session_id":  sid,
                "text":        "",
                "tool_results": [],
                "needs_input": {
                    "type":      "clarify",
                    "question":  mf.get("question", "Please provide more detail."),
                    "options":   mf.get("options", []),
                    "field":     mf.get("field", ""),
                    "tool_name": tool_name,
                    "proposed":  None,
                },
            }

        ok_flag = result_data.get("success", True)
        label   = tool_name.replace("_", " ")
        text    = f"Done — {label} completed." if ok_flag else result_data.get("error", "Failed.")
        # Store a proper tool-call sequence so Ollama doesn't repeat the action next turn.
        updated_messages = messages + [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": tool_name, "arguments": args}}]},
            {"role": "tool",      "content": json.dumps(result_data, default=str)},
            {"role": "assistant", "content": text},
        ]
        session_store.SESSIONS[sid] = {"messages": updated_messages, "pending_tool": None, "critical_step2": False}
        return {
            "session_id":  sid,
            "text":        text,
            "tool_results": [{"tool": tool_name, "args": args, "result": result_data}],
            "needs_input": None,
        }

    # ── Normal AI path ────────────────────────────────────────────────────────
    result = process_message(
        user_input   = req.message,
        messages     = messages,
        verbose      = req.verbose,
        auto_confirm = req.auto_confirm,
    )

    session_store.SESSIONS[sid] = {
        "messages":     result["messages"],
        "pending_tool": result.get("pending_tool"),
    }

    return {
        "session_id":  sid,
        "text":        result["text"],
        "tool_results": result["tool_results"],
        "needs_input": result["needs_input"],
    }
