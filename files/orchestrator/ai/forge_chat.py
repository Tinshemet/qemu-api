"""forge_chat.py — the multi-turn contract-forge WIZARD for /chat.

Walks forge_fields.json one question per chat turn, holding state between turns
in the session. DETERMINISTIC — the local 8B model is never involved: a weak
model can't reliably run a 15-field elicitation, and a state machine does it
perfectly (and can't be talked out of the safeword gate).

Forging is high-impact, so the wizard opens with operator RE-AUTHENTICATION
(a password turn) before eliciting anything.

Pure/testable: advance() takes (state, answer, verify_password) and returns
(new_state, reply, needs_input, result). The HTTP layer owns session storage and
supplies the password check; this module owns the flow. Wizard turns are never
written to chat history, so the password never lands in a stored transcript.

State: {"phase": "auth"|"elicit"|"safeword", "step": int, "spec": {...},
        "attempts": int, "essential_only": bool}
"""
import os
from typing import Any, Callable, Dict, Optional, Tuple

from . import forge

_MAX_PW_ATTEMPTS = 3
_CANCEL_WORDS    = {"cancel", "abort", "quit", "/cancel", "stop"}

# Creation verbs — the wizard triggers on the intent to MAKE a contract, not on
# show/sign (those act on an existing file and stay CLI operations).
_FORGE_VERBS = {"forge", "make", "create", "new", "draft", "build", "negotiate", "start", "write"}
# A VM noun means "contract" is the target of a VM action ("a vm to test smart
# contracts"), not a request to forge one — leave it to the AI.
_VM_NOUNS    = {"vm", "vms", "machine", "guest", "box", "instance"}

_PW_INPUT = {"type": "password", "question": "Operator password:"}


def looks_like_forge_intent(message: str) -> bool:
    """True when a chat message is asking to forge a contract (deterministic)."""
    words = {w.strip(".,!?;:'\"") for w in (message or "").lower().split()}
    if not (words & {"contract", "contracts"}):
        return False
    if words & _VM_NOUNS:
        return False
    return bool(words & _FORGE_VERBS)


def start(*, needs_auth: bool = True, essential_only: bool = False,
          schema: Dict[str, Any] = None) -> Dict[str, Any]:
    """Begin a wizard. Pre-applies every non-asked field's default so the spec is
    complete before elicitation. needs_auth=False skips the password phase (used
    pre-bootstrap when no operator accounts exist)."""
    schema = schema or forge._load_fields()
    spec: Dict[str, Any] = {}
    asked_keys = {f["key"] for f in forge.asked_fields(schema, essential_only)}
    for field in schema["fields"]:
        if field["key"] not in asked_keys:
            forge._set_dotted(spec, field["key"], forge.default_value(field))
    return {
        "phase":          "auth" if needs_auth else "elicit",
        "step":           0,
        "spec":           spec,
        "attempts":       0,
        "essential_only": essential_only,
    }


def current_prompt(state: Dict[str, Any], schema: Dict[str, Any] = None) -> Tuple[str, Optional[dict]]:
    """(reply_text, needs_input) for the step the wizard is waiting on right now."""
    schema = schema or forge._load_fields()
    phase = state["phase"]
    if phase == "auth":
        return ("Forging a contract is operator-gated. Enter your operator password to continue:",
                dict(_PW_INPUT))
    if phase == "elicit":
        asked = forge.asked_fields(schema, state["essential_only"])
        field = asked[state["step"]]
        return (f"({state['step'] + 1}/{len(asked)}) {field['prompt']}", None)
    if phase == "safeword":
        return (schema.get("safeword_prompt", "Sign with a safeword (blank to cancel):"), None)
    return ("", None)


def advance(state: Dict[str, Any], answer: str, *,
            verify_password: Optional[Callable[[str], bool]] = None,
            write_dir: str = ".", schema: Dict[str, Any] = None
            ) -> Tuple[Optional[Dict[str, Any]], str, Optional[dict], Optional[dict]]:
    """Process one answer. Returns (new_state, reply, needs_input, result):
      - new_state None  → the wizard is finished (signed, aborted, or cancelled)
      - result          → {"path", "name"} on a successful signing, else None
    verify_password(pw)->bool is consulted only in the auth phase; None there
    means degrade-open (no operators exist).
    """
    schema = schema or forge._load_fields()
    ans = (answer or "").strip()

    # Cancel at any point, including the password prompt — bailing out of a forge
    # is always allowed (an explicit "cancel" beats treating it as a bad password).
    if ans.lower() in _CANCEL_WORDS:
        return None, "Forge cancelled — nothing signed.", None, None

    if state["phase"] == "auth":
        if verify_password is None or verify_password(answer):
            state["phase"] = "elicit"
            reply, ni = current_prompt(state, schema)
            return state, "✓ Authenticated. Let's forge a contract.\n" + reply, ni, None
        state["attempts"] += 1
        if state["attempts"] >= _MAX_PW_ATTEMPTS:
            return None, "Too many failed attempts — forge aborted.", None, None
        return (state,
                f"Incorrect password ({state['attempts']}/{_MAX_PW_ATTEMPTS}). Try again:",
                dict(_PW_INPUT), None)

    if state["phase"] == "elicit":
        asked = forge.asked_fields(schema, state["essential_only"])
        field = asked[state["step"]]
        forge._set_dotted(state["spec"], field["key"], forge.parse_answer(field, answer))
        state["step"] += 1
        if state["step"] < len(asked):
            reply, ni = current_prompt(state, schema)
            return state, reply, ni, None
        # Elicitation done — forge + coherence review.
        g = forge.forge(state["spec"])
        issues = forge.review(g)
        if issues:
            return None, "✗ The contract has issues — revise and re-forge:\n" \
                   + "\n".join(f"  - {i}" for i in issues), None, None
        state["phase"] = "safeword"
        reply, ni = current_prompt(state, schema)
        return state, forge.render(g) + "\n\n" + reply, ni, None

    if state["phase"] == "safeword":
        if not ans:
            return None, "Cancelled — not signed.", None, None
        path, issues = forge.finalize_forge(state["spec"], ans, write_dir)
        if path is None:
            return None, "✗ " + "; ".join(issues), None, None
        name = os.path.basename(path)
        return None, (f"✔ Sealed → {name}   (I am thou, thou art I — the contract is sealed.)\n"
                      f"Load it with:  gorgon agent load {name}"), None, {"path": path, "name": name}

    return None, "", None, None
