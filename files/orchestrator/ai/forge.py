"""
forge.py — contract forging: turn a negotiated spec into a signed .grgn agent.

A campaign contract, in this architecture, IS a .grgn: forging produces one. The flow
mirrors project_campaign_contract:

    elicit a spec  →  forge()  →  review()  →  (negotiate: fix issues, re-forge)  →  sign()

The Doorman runs the elicitation (asks the graded questions, collects answers) — a way
to build/test the forging workflow before Conductors exist. Note: conceptually the
CONDUCTOR is the signing party; here the Doorman just DRIVES the forge, producing a
Conductor's contract file.

The forged contract inherits the shared INNATE layer (tiers, formula, per-tool risk
baseline) from doorman.grgn — "same innate risk, different role + campaign" — and adds:
  - a campaign layer (goal, scrutiny, ethics, legality, caveats, success criteria,
    reward, safeword) — the negotiated policy,
  - the tool WHITELIST (only these are assessed/offered) or BLACKLIST (→ forbidden),
  - hard red lines (→ the legal filter's `forbidden` list),
  - all its own POLICY, so the file is self-contained and portable.

Two-phase agency: PRE-sign the contract is negotiable (review surfaces issues, the human
revises); `sign()` locks it and sets the safeword. An incoherent contract can't be signed.
"""
import json
import os
from typing import Any, Dict, List

_AI = os.path.dirname(os.path.abspath(__file__))


def _base_innate() -> Dict[str, Any]:
    """The shared innate layer (tiers, formula, per-tool risk baseline) from doorman.grgn."""
    return json.load(open(os.path.join(_AI, "doorman.grgn")))["contract"]


def _build_prompt(spec: Dict[str, Any]) -> List[str]:
    p = spec.get("persona", {})
    caveats = spec.get("caveats") or []
    lines = [
        f"You are {p.get('name', 'an agent')} — {p.get('role', 'an autonomous agent')}, "
        f"operating under a signed campaign contract.",
        f"CAMPAIGN GOAL: {spec.get('goal', '')}.",
        f"DONE WHEN: {spec.get('success_criteria', '(unspecified)')}.",
        f"SCRUTINY: {spec.get('scrutiny', 'strict')} — "
        + {"strict": "use only goal-related tools; do not explore beyond the goal.",
           "medium": "you may explore beyond the goal for coverage.",
           "loose": "you are free to act as needed in service of the goal."}.get(spec.get("scrutiny", "strict"), ""),
        f"ETHICS: {spec.get('ethics', '(unspecified)')}.  LEGALITY: {spec.get('legality', '(unspecified)')}.",
    ]
    if caveats:
        lines.append("CAVEATS: " + "; ".join(caveats) + ".")
    lines += ["Work toward the goal within the contract; never cross a red line.", "{state_section}"]
    return lines


def forge(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble a .grgn agent from a negotiated contract spec. NOT yet signed."""
    base = _base_innate()
    tools_spec = spec.get("tools", {}) or {}
    mode = tools_spec.get("mode", "whitelist")
    listed = tools_spec.get("list", [])

    # Whitelisted tools inherit their innate risk assessment; others aren't offered.
    tools = {t: base["tools"][t] for t in listed if t in base.get("tools", {})}
    forbidden = list(spec.get("forbidden", []))
    if mode == "blacklist":
        forbidden = sorted(set(forbidden) | set(listed))
        tools = {}   # blacklist mode: everything but the blacklist is allowed (no per-tool whitelist)

    return {
        "_type": "gorgon.agent/v1",
        "_forged": True,
        "_doc": f"Forged campaign contract: {spec.get('title', '(untitled)')}.",
        "persona": {
            "name": spec.get("persona", {}).get("name"),
            "role": spec.get("persona", {}).get("role", "autonomous agent"),
            "disposition": spec.get("persona", {}).get("disposition", "autonomous"),
            "layers": ["innate", "campaign"],
        },
        "prompts": {"system": _build_prompt(spec)},
        "contract": {
            "tiers": base["tiers"],
            "formula": base["formula"],
            "fleet_actions": base.get("fleet_actions", {}),
            "tools": tools,
            "forbidden": forbidden,
            "campaign": {
                "title": spec.get("title"),
                "description": spec.get("description"),           # free-text context (not gate-enforced)
                "goal": spec.get("goal"),
                "sub_goals": spec.get("sub_goals", []),
                "scrutiny": spec.get("scrutiny", "strict"),       # strict | medium | loose
                "tool_mode": mode,
                "toolkit": listed if mode == "whitelist" else None,   # what the agent may CALL
                "ethics": spec.get("ethics"),                     # abide [rules] | surface to user | disregard
                "legality": spec.get("legality"),                 # follow [laws] | none
                "caveats": spec.get("caveats", []),
                "rules": spec.get("rules", []),                   # [{text, weight}] — 0=wildcard/void-if-broken, 1=default
                "success_criteria": spec.get("success_criteria"),   # human prose
                "success_predicate": spec.get("success_predicate") or None,  # checkable {criterion,target} clauses = the ROOT gate
                "reward": spec.get("reward", 1.0),
                "expiry": spec.get("expiry"),         # ISO date | None (never); enforced at load
                "safeword": None,     # set at signing
                "signed": False,      # two-phase: negotiable until signed
            },
        },
    }


def review(grgn: Dict[str, Any]) -> List[str]:
    """Coherence check (the AI-review / arithmetic-ruling step): surface every problem
    that would make the contract un-signable. Empty list = coherent."""
    try:
        from executor.command_catalog import KNOWN_TOOLS
    except ImportError:
        KNOWN_TOOLS = frozenset()
    # state criteria (checked vs the VM registry) + epistemic ones (checked vs findings).
    _CRITERIA = {"present", "absent", "running", "stopped", "restored", "mesh", "reachable", "probe", "found"}
    issues: List[str] = []
    c = grgn.get("contract", {})
    camp = c.get("campaign", {})
    offered, forbidden = set(c.get("tools", {})), set(c.get("forbidden", []))
    toolkit = set(camp.get("toolkit") or [])

    for t in offered | forbidden | toolkit:
        if KNOWN_TOOLS and t not in KNOWN_TOOLS:
            issues.append(f"references a tool that doesn't exist: {t!r}")
    both = (offered | toolkit) & forbidden
    if both:
        issues.append(f"tool is BOTH offered and forbidden (contradiction): {sorted(both)}")
    if not grgn.get("persona", {}).get("name"):
        issues.append("no persona name")
    if not camp.get("goal"):
        issues.append("no goal")
    if not camp.get("success_criteria"):
        issues.append("no success criteria — 'done' is undefined (mis-specified contract)")
    for clause in camp.get("success_predicate") or []:
        crit, target = clause.get("criterion"), clause.get("target")
        if crit not in _CRITERIA:
            issues.append(f"root predicate clause has an uncheckable criterion: {crit!r} (want one of {sorted(_CRITERIA)})")
        if not target:
            issues.append(f"root predicate clause has no target: {clause!r}")
    if camp.get("tool_mode") == "whitelist" and not camp.get("toolkit"):
        issues.append("empty toolkit — the agent can do nothing")
    return issues


def sign(grgn: Dict[str, Any], safeword: str) -> Dict[str, Any]:
    """The signing ceremony: lock a COHERENT contract and set its safeword. Refuses an
    incoherent contract (the conscience gate at entry) or a missing safeword."""
    issues = review(grgn)
    if issues:
        raise ValueError("cannot sign an incoherent contract: " + "; ".join(issues))
    if not safeword:
        raise ValueError("a safeword is required to sign (the kill-switch)")
    grgn["contract"]["campaign"]["safeword"] = safeword
    grgn["contract"]["campaign"]["signed"] = True
    return grgn


def render(grgn: Dict[str, Any], width: int = 68) -> str:
    """A human-readable terminal view of a contract — laid out to the campaign-contract
    structure (title+description · goal+sub-goals · scrutiny/tools/ethics/legality ·
    weighted rules · red lines · success criteria · safeword). For `gorgon contract show`."""
    p = grgn.get("persona", {})
    c = grgn.get("contract", {})
    camp = c.get("campaign", {})
    bar = "─" * width

    def wrap(label, text):
        return f"  {label:<11}{text}"

    L = ["",                                      # leading blank line so the box starts fresh
         "╔" + "═" * width + "╗",
         f"  CAMPAIGN CONTRACT — {camp.get('title') or '(innate)'}"
         + ("     ✔ SIGNED" if camp.get("signed") else "     … unsigned · negotiable"),
         "╠" + "═" * width + "╣",
         wrap("Signatory", f"{p.get('name','?')}  ·  {p.get('role','')}  ·  {p.get('disposition','?')}")]
    if camp.get("description"):
        L.append(wrap("Context", camp["description"]))
    if camp:
        L += [bar,
              wrap("Goal", camp.get("goal", "")),
              ]
        for sg in camp.get("sub_goals", []):
            L.append(wrap("  ·", sg))
        L += [wrap("Done when", camp.get("success_criteria", "(undefined)"))]
        pred = camp.get("success_predicate") or []
        if pred:
            L.append(wrap("  root gate", " ∧ ".join(f"{cl.get('criterion')}:{cl.get('target')}" for cl in pred)))
        L += [bar,
              wrap("Scrutiny", camp.get("scrutiny", "")),
              wrap("Ethics", camp.get("ethics", "")),
              wrap("Legality", camp.get("legality", "")),
              wrap("Reward", _reward_render(camp.get("reward", ""))),
              wrap("Expires", camp.get("expiry") or "never")]

    L += [bar,
          wrap("Toolkit", ", ".join(camp.get("toolkit") or sorted(c.get("tools", {})) or ["(none)"])
               + (f"   [{camp.get('tool_mode')}]" if camp.get("tool_mode") else "")),
          wrap("Blacklist", (", ".join(c.get("forbidden", [])) or "(none)") + "   [weight 0 · inviolable]")]

    rules = camp.get("rules", [])
    if rules or camp.get("caveats"):
        L.append(bar)
        L.append("  RULES  (weight: 0 = wildcard/void-if-broken · 1 = default · higher = weaker)")
        for r in rules:
            w = r.get("weight", 1)
            L.append(f"    [{w}] {r.get('text','')}")
        for cav in camp.get("caveats", []):
            L.append(f"    [·] {cav}")

    L += [bar,
          wrap("Safeword", "set  (kill-switch armed)" if camp.get("safeword") else "— (set at signing)"),
          "╚" + "═" * width + "╝"]
    return "\n".join(L)


def write_grgn(grgn: Dict[str, Any], path: str) -> str:
    """Persist a forged .grgn — ENCRYPTED (Fernet), so the safeword and campaign
    never sit in cleartext on disk. Point GORGON_AGENT at it to run the agent.
    Falls back to plaintext only if the crypto layer is unavailable."""
    try:
        from shared.grgn_sign import write_encrypted
        return write_encrypted(grgn, path)
    except Exception:
        with open(path, "w") as f:                 # degraded: crypto layer absent
            json.dump(grgn, f, indent=2, ensure_ascii=False)
        return path


def _csv(s: str):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _predicate(s: str):
    """Parse 'present:honeypot, absent:web01' → the structured root-predicate clauses.
    Each 'criterion:target' becomes {'criterion':…, 'target':…}; review() validates them."""
    out = []
    for chunk in _csv(s):
        crit, _, target = chunk.partition(":")
        out.append({"criterion": crit.strip(), "target": target.strip()})
    return out


class FieldType:
    """Strategy for a forge field's TYPE — how to parse an answer into a spec
    value, and how to render that value back for display.

    A new field type is a new SUBCLASS registered in ``_FIELD_TYPES``; field
    INSTANCES stay data-driven in forge_fields.json (each entry names its
    ``parse`` type). This is the seam that lets new field behaviour arrive by
    inheritance instead of editing elicit_spec()/render() — e.g. a future
    ToolkitField could validate names against the executor registry here.
    """

    def parse(self, raw, field):
        raise NotImplementedError

    def validate(self, value, field, spec=None):
        """Field-specific checks on a parsed value → list of issue strings (empty
        = OK). The base type accepts anything; subclasses tighten. Run per field
        during elicitation (immediate feedback) — see validate_answer(). ``spec``
        is the answers-so-far, for cross-field checks (e.g. a red line that's also
        whitelisted)."""
        return []

    def format(self, value, field):
        """Human-readable rendering of a stored value (for `contract show`)."""
        return "" if value in ("", None) else str(value)


class StrField(FieldType):
    def parse(self, raw, field):
        v = (raw or "").strip()
        if v:
            return v
        d = field.get("default")
        return d if d is not None else ""


class CsvField(FieldType):
    def parse(self, raw, field):
        return _csv(raw)


class ToolkitField(CsvField):
    """A CSV of tool NAMES, validated against the executor registry. This field
    type owns the tools-SSOT check: a name absent from KNOWN_TOOLS is drift. The
    executor is still the real gate (an unknown call is rejected there), so this
    is early, friendly feedback — not the enforcement point. Degrades to no-op if
    the executor package (and thus the registry) isn't importable."""

    def validate(self, value, field, spec=None):
        issues = []
        try:
            from executor.command_catalog import KNOWN_TOOLS
        except ImportError:
            KNOWN_TOOLS = None
        if KNOWN_TOOLS:
            issues += [f"unknown tool '{t}' — not in the executor registry"
                       for t in value if t not in KNOWN_TOOLS]
        # Cross-field: a field may declare `conflicts_with` another field's list
        # (e.g. red lines vs the toolkit) — catch the contradiction inline instead
        # of at review() after every question is answered.
        other_key = field.get("conflicts_with")
        if other_key and spec is not None:
            clash = set(value) & set(_get_dotted(spec, other_key) or [])
            if clash:
                issues.append("already whitelisted, can't also be a red line: "
                              + ", ".join(sorted(clash)))
        return issues


class PredicateField(FieldType):
    _CRITERIA = {"present", "absent", "running", "stopped", "restored", "mesh", "reachable", "probe", "found"}

    def parse(self, raw, field):
        return _predicate(raw)

    def validate(self, value, field, spec=None):
        issues = []
        for clause in value or []:
            crit, target = clause.get("criterion"), clause.get("target")
            if crit not in self._CRITERIA:
                issues.append(f"'{crit}' is not a checkable criterion "
                              f"(use one of {sorted(self._CRITERIA)})")
            elif not target:
                issues.append(f"criterion '{crit}' needs a target, e.g. {crit}:vm1")
        return issues


class FloatField(FieldType):
    def parse(self, raw, field):
        v = (raw or "").strip()
        return float(v) if v else float(field.get("default", 1))


class ImportanceField(FieldType):
    """Reward-as-importance: an importance WORD maps to a reward number, so the
    operator answers 'how much does this goal matter?' instead of guessing a
    unitless number. The word→number map is the field's ``levels`` (data-driven).
    Blank → default; a raw number is still accepted; an unknown word → default."""

    def parse(self, raw, field):
        levels = {k.lower(): v for k, v in (field.get("levels") or {}).items()}
        key = (raw or "").strip().lower() or str(field.get("default", "")).lower()
        if key in levels:
            return float(levels[key])
        try:
            return float(raw)                       # an explicit number is fine too
        except (TypeError, ValueError):
            return float(levels.get(str(field.get("default", "")).lower(), 1.0))

    def format(self, value, field):
        if value in ("", None):
            return ""
        for word, num in (field.get("levels") or {}).items():
            try:
                if float(num) == float(value):
                    return f"{word} ({value})"      # e.g. "important (10)"
            except (TypeError, ValueError):
                pass
        return str(value)


class ExpiryField(FieldType):
    """Optional contract expiry — the twin of ToolkitField, showing the same
    'new field type = one subclass' seam. Accepts an ISO date (2026-12-31) or a
    duration (30d / 6w / 3m / 1y), normalized to an absolute ISO date; blank →
    None (never expires). validate() rejects a garbled or already-past date;
    contract.py enforces it at load (an expired contract is refused, fail-closed)."""

    def parse(self, raw, field):
        import re
        from datetime import date, timedelta
        s = (raw or "").strip().lower()
        if not s:
            return None
        m = re.fullmatch(r"(\d+)\s*([dwmy])", s)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * n
            return (date.today() + timedelta(days=days)).isoformat()
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            return s                                # keep raw so validate() can flag it

    def validate(self, value, field, spec=None):
        if not value:
            return []
        from datetime import date
        try:
            d = date.fromisoformat(value)
        except ValueError:
            return [f"unparseable expiry {value!r} — use YYYY-MM-DD or a duration like 30d"]
        return [f"expiry {value} is already in the past"] if d < date.today() else []

    def format(self, value, field):
        return "never" if not value else str(value)


# Field-type registry — forge_fields.json names a type per field; add a type by
# subclassing FieldType and registering it here (instances stay in the JSON).
_FIELD_TYPES = {
    "str":        StrField(),
    "csv":        CsvField(),
    "toolkit":    ToolkitField(),
    "predicate":  PredicateField(),
    "float":      FloatField(),
    "importance": ImportanceField(),
    "expiry":     ExpiryField(),
}


def _field_type(field: Dict[str, Any]) -> FieldType:
    return _FIELD_TYPES[field["parse"]]


def _reward_render(value) -> str:
    """Render a reward via its field type (ImportanceField shows 'tier (n)')."""
    if value == "" or value is None:
        return ""
    for f in _load_fields()["fields"]:
        if f.get("key") == "reward":
            return _field_type(f).format(value, f)
    return str(value)


def _load_fields() -> Dict[str, Any]:
    """The declarative forge field schema (questions, order, parsers, defaults)."""
    return json.load(open(os.path.join(_AI, "forge_fields.json")))


def _set_dotted(spec: Dict[str, Any], key: str, value: Any) -> None:
    """Set spec[a][b]=value for a dotted key 'a.b', creating dicts as needed."""
    parts = key.split(".")
    d = spec
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def _get_dotted(spec: Dict[str, Any], key: str) -> Any:
    """Read spec[a][b] for a dotted key 'a.b', or None if any level is missing."""
    d = spec
    for p in key.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def asked_fields(schema: Dict[str, Any], essential_only: bool = False) -> List[Dict[str, Any]]:
    """The fields that get PROMPTED, in order: skip ask=false constants, and —
    in essential_only — skip non-essential fields (they take their default)."""
    return [f for f in schema["fields"]
            if f.get("ask", True) is not False
            and (not essential_only or f.get("essential", False))]


def default_value(field: Dict[str, Any]) -> Any:
    """The value for a field that ISN'T being asked — a constant (ask=false) or
    an unprompted default (parse the empty answer through the field's type)."""
    if field.get("ask", True) is False:
        return field.get("value", field.get("default"))
    return _field_type(field).parse("", field)


def parse_answer(field: Dict[str, Any], raw: str) -> Any:
    """Parse a raw answer for a field through its declared field type."""
    return _field_type(field).parse(raw, field)


def validate_answer(field: Dict[str, Any], value: Any, spec: Dict[str, Any] = None) -> List[str]:
    """Field-type validation for a parsed value (empty list = OK). ``spec`` is the
    answers-so-far, for cross-field checks."""
    return _field_type(field).validate(value, field, spec)


def elicit_spec(ask, *, essential_only: bool = False, schema: Dict[str, Any] = None,
                out=None) -> Dict[str, Any]:
    """Build a contract spec by walking the declarative field schema.

    `ask(prompt) -> str` supplies each answer (console.input in the CLI, one chat
    turn in the wizard, scripted in tests). Fields are visited in schema order —
    that order IS the elicitation order. With ``essential_only`` (the simpler
    terminal forge) non-essential fields take their default without being asked;
    ``ask=false`` fields (e.g. tool_mode) are constants and never prompt. The
    resulting spec is fed to forge(); safeword/signing happen separately.

    If ``out`` is given, each answer is validated through its field type and
    re-asked (with the issues printed) until it passes — immediate per-field
    feedback (e.g. an unknown tool name). Without ``out`` validation is skipped
    here and left to review(), preserving the old parse-only behavior for
    callers that can't re-prompt.
    """
    schema = schema or _load_fields()
    asked = {f["key"] for f in asked_fields(schema, essential_only)}
    spec: Dict[str, Any] = {}
    for field in schema["fields"]:
        if field["key"] not in asked:
            _set_dotted(spec, field["key"], default_value(field))
            continue
        value = parse_answer(field, ask(field["prompt"]))
        if out is not None:
            issues = validate_answer(field, value, spec)
            while issues:
                for i in issues:
                    out(f"  ✗ {i}")
                value = parse_answer(field, ask(field["prompt"]))
                issues = validate_answer(field, value, spec)
        _set_dotted(spec, field["key"], value)
    return spec


def finalize_forge(spec: Dict[str, Any], safeword: str, write_dir: str = ".",
                   overwrite: bool = True):
    """forge → review → sign → write, given a completed spec and safeword.

    Returns (path, issues): a written path with no issues on success, or
    (None, issues) if the contract is incoherent, the safeword is blank, or the
    target file exists with overwrite=False. Shared by every forge frontend so
    the forge/review/sign/write core lives in exactly one place.
    """
    g = forge(spec)
    issues = review(g)
    if issues:
        return None, issues
    if not safeword:
        return None, ["a safeword is required to sign (the kill-switch)"]
    sign(g, safeword)
    name = (spec.get("persona", {}).get("name") or "agent").lower()
    path = os.path.join(write_dir, f"{name}.grgn")
    if os.path.exists(path) and not overwrite:
        return None, [f"{path} exists — choose a different agent name"]
    write_grgn(g, path)
    return path, []


def forge_interactive(ask, out, write_dir: str = ".", overwrite: bool = True,
                      essential_only: bool = False):
    """The Doorman-driven forging DIALOGUE: elicit the spec, forge → review →
    (negotiate: on issues, abort so the operator can revise) → sign → write.

    `ask(prompt) -> str` supplies answers (console.input in the CLI; scriptable in
    tests); `out(text)` prints (console.print). Returns the written path, or None if
    review found issues. Two-phase: nothing is signed until the safeword is given.
    With ``essential_only`` this is the simpler terminal forge — only the essential
    fields are asked, the rest defaulted. The questions themselves come from
    forge_fields.json, not this function.
    """
    schema = _load_fields()
    out(schema.get("header", "═ Forge a campaign contract ═"))
    spec = elicit_spec(ask, essential_only=essential_only, schema=schema, out=out)
    g = forge(spec)
    issues = review(g)
    if issues:
        out("✗ The contract has issues — revise and re-forge:")
        for i in issues:
            out(f"    - {i}")
        return None
    out(render(g))
    sw = ask(schema.get("safeword_prompt",
                        "Contract is coherent. Sign with a SAFEWORD to seal it (blank to cancel)"))
    if not sw:
        out("  Cancelled — not signed.")
        return None
    path, issues = finalize_forge(spec, sw, write_dir, overwrite)
    if path is None:
        for i in issues:
            out(f"✗ {i}")
        return None
    out(f"  I am thou, thou art I — the contract is sealed.")
    out(f"  ✔ → {path}   ·   run it with  GORGON_AGENT={os.path.basename(path)}")
    return path
