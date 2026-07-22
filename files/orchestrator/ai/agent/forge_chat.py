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

# Tokens meaning "leave this optional field blank" — the curses client swallows an
# empty line, so an explicit skip token is how you blank a field in the chat wizard.
_SKIP_WORDS = {"-", "skip"}


def _wizard_cfg():
    """Wizard behaviour knobs from forge_fields.json's `wizard` block (attempts,
    cancel words, password prompt) — config, not hardcoded. Sensible fallbacks if
    the block is absent."""
    w = forge._load_fields().get("wizard", {}) or {}
    return {
        "max_attempts": int(w.get("max_password_attempts", 3)),
        "cancel_words": {s.lower() for s in (w.get("cancel_words")
                         or ["cancel", "abort", "quit", "/cancel", "stop"])},
        "pw_prompt":    w.get("password_prompt", "Operator password:"),
    }


def _pw_input(cfg=None):
    """The password needs_input envelope. `type` is a fixed wire-protocol constant
    (the client masks on it); only the prompt text is configurable."""
    cfg = cfg or _wizard_cfg()
    return {"type": "password", "question": cfg["pw_prompt"]}


def _intent_words():
    """(forge_verbs, vm_nouns) from forge_fields.json's `intent` block — the
    trigger words are config, not hardcoded. Falls back to sensible defaults if
    the block is absent."""
    intent = forge._load_fields().get("intent", {}) or {}
    verbs = set(intent.get("forge_verbs") or
                ["forge", "make", "create", "new", "draft", "build", "negotiate", "start", "write"])
    nouns = set(intent.get("vm_nouns") or ["vm", "vms", "machine", "guest", "box", "instance"])
    return verbs, nouns


# Contract lifecycle actions that are CLI-only (they act on a file, need an editor,
# or a positional arg) — the chat recognizes the intent and points at the command.
_CLI_ACTIONS = [
    ("edit",  "gorgon contract edit <file>"),
    ("amend", "gorgon contract edit <file>"),
    ("sign",  "gorgon contract sign <file> <safeword>"),
    ("list",  "gorgon contract list"),
    ("show",  "gorgon contract show <file>"),
    ("view",  "gorgon contract show <file>"),
]


def contract_cli_redirect(message: str) -> Optional[str]:
    """If a chat message asks to sign/edit/show/list a contract (CLI-only actions,
    unlike forge which runs in-chat), return the command to run; else None. Same
    deterministic style as looks_like_forge_intent — never reaches the model."""
    _, vm_nouns = _intent_words()
    words = {w.strip(".,!?;:'\"") for w in (message or "").lower().split()}
    if not (words & {"contract", "contracts"}) or (words & vm_nouns):
        return None
    for verb, cmd in _CLI_ACTIONS:
        if verb in words:
            return f"That's a CLI action (it works on a contract file directly). Run:\n  {cmd}"
    return None


def looks_like_forge_intent(message: str) -> bool:
    """True when a chat message is asking to forge a contract (deterministic).

    Triggers on a create-verb + "contract", but not when a VM noun is present
    (so "a vm to test smart contracts" is left to the AI). Words come from the
    `intent` block in forge_fields.json.
    """
    forge_verbs, vm_nouns = _intent_words()
    words = {w.strip(".,!?;:'\"") for w in (message or "").lower().split()}
    if not (words & {"contract", "contracts"}):
        return False
    if words & vm_nouns:
        return False
    return bool(words & forge_verbs)


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
                _pw_input())
    if phase == "elicit":
        asked = forge.asked_fields(schema, state["essential_only"])
        field = asked[state["step"]]
        optional = not field.get("essential")
        hint = "   (blank or - to skip)" if optional else ""
        # needs_input type 'prompt' tells the client this is a free-text answer; the
        # 'allow_empty' flag lets it submit an empty line (optional fields only),
        # since the client otherwise swallows a blank Enter.
        return (f"({state['step'] + 1}/{len(asked)}) {field['prompt']}{hint}",
                {"type": "prompt", "allow_empty": optional})
    if phase == "safeword":
        return (schema.get("safeword_prompt", "Sign with a safeword (blank to cancel):"),
                {"type": "prompt", "allow_empty": True})
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
    cfg = _wizard_cfg()
    ans = (answer or "").strip()

    # Cancel at any point — bailing out of a forge is always allowed. But in the
    # AUTH phase a cancel word ("stop", "quit", ...) might genuinely BE the
    # operator's password, so try it as one first and only cancel if it fails to
    # authenticate. Outside auth the intent is unambiguous.
    is_cancel = ans.lower() in cfg["cancel_words"]
    if is_cancel and state["phase"] != "auth":
        return None, "Forge cancelled — nothing signed.", None, None

    if state["phase"] == "auth":
        if verify_password is None or verify_password(answer):
            state["phase"] = "elicit"
            reply, ni = current_prompt(state, schema)
            return state, "✓ Authenticated. Let's forge a contract.\n" + reply, ni, None
        if is_cancel:   # not the password → honour the explicit cancel
            return None, "Forge cancelled — nothing signed.", None, None
        state["attempts"] += 1
        if state["attempts"] >= cfg["max_attempts"]:
            return None, "Too many failed attempts — forge aborted.", None, None
        return (state,
                f"Incorrect password ({state['attempts']}/{cfg['max_attempts']}). Try again:",
                _pw_input(cfg), None)

    if state["phase"] == "elicit":
        asked = forge.asked_fields(schema, state["essential_only"])
        field = asked[state["step"]]
        # "-"/"skip" blanks an OPTIONAL field (essential fields keep the literal).
        raw = "" if (not field.get("essential") and ans.lower() in _SKIP_WORDS) else answer
        value = forge.parse_answer(field, raw)
        problems = forge.validate_answer(field, value, state["spec"])
        if problems:
            # Stay on this field; report the issues and re-ask (immediate feedback).
            reply, ni = current_prompt(state, schema)
            return state, "✗ " + "; ".join(problems) + "\n" + reply, ni, None
        forge._set_dotted(state["spec"], field["key"], value)
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
