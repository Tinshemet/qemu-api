"""
fields.py — the MISSION authoring fields, as classes (the tasking twin of the
contract fields).

A mission is what you TASK an agent to do; each field is an authorable question in
the mission wizard. Only title + goal are essential; every other field left blank
INHERITS the agent's default. These classes reuse the contract Field base (its
``schema()`` emitter) and the shared forge shape strategies (str/csv/predicate/
optfloat/toolkit), so ``mission_schema_fields()`` is the SINGLE SOURCE the wizard
walks — adding a mission field is one class here, no JSON edit.
"""

from typing import Any, Dict, List

from ..agent.fields import Field


class TitleField(Field):
    spec_key = "title"; prompt = "Mission title (its name)"; shape = "str"; essential = True


class GoalField(Field):
    spec_key = "goal"; prompt = "Goal"; shape = "str"; essential = True


class SubGoalsField(Field):
    spec_key = "sub_goals"; prompt = "Sub-goals (comma-separated, optional)"; shape = "csv"


class SuccessPredicateField(Field):
    spec_key = "success_predicate"; shape = "predicate"
    prompt   = ("Done-when — checkable clauses 'criterion:target' "
                "(e.g. found:ip(web01), present:honeypot; optional)")


class RewardField(Field):
    spec_key = "reward"; shape = "optfloat"
    prompt   = "Reward — base payoff for closing it (blank = agent default)"


class ImportanceField(Field):
    spec_key = "importance"; shape = "optfloat"
    prompt   = "Importance — multiplier on the reward (blank = agent default)"


class WeightField(Field):
    spec_key = "weight"; shape = "optfloat"
    prompt   = "Weight — planning weight (blank = agent default)"


class ToolWhitelistField(Field):
    spec_key = "tool_whitelist"; shape = "toolkit"
    prompt   = "Tool whitelist for this mission (comma-separated; blank = agent toolkit)"


class ToolBlacklistField(Field):
    spec_key = "tool_blacklist"; shape = "toolkit"
    prompt   = "Extra red lines for this mission (comma-separated; blank = none beyond the agent's)"


class ScrutinyField(Field):
    spec_key = "scrutiny"; prompt = "Scrutiny level (blank = agent default)"; shape = "str"


# Every mission field class, indexed by key. Adding a field = add a class here; the
# ELICITATION ORDER (and which fields are active) is DATA — mission_fields.json's
# `field_order` — so reordering/enabling a field is a JSON edit, not a code change.
_FIELD_CLASSES = (
    TitleField, GoalField, SubGoalsField, SuccessPredicateField, RewardField,
    ImportanceField, WeightField, ToolWhitelistField, ToolBlacklistField, ScrutinyField,
)
_BY_KEY = {c.spec_key: c for c in _FIELD_CLASSES}


def mission_schema_fields(field_order: List[str]) -> List[Dict[str, Any]]:
    """The mission wizard's field schema, in the order given by ``field_order``
    (mission_fields.json). Each key looks up its Field class; unknown keys are
    skipped. This is the AUTHORING schema — a subset of the Mission model's valid
    fields in mission.py (which also carries non-authored success_criteria/reward_cost)."""
    return [_BY_KEY[k].schema() for k in field_order if k in _BY_KEY]
