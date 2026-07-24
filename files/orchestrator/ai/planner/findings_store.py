"""
findings_store.py — durable, per-agent persistence for CLAIM findings (and, in a
sibling file, learned tool reliability).

An unverifiable claim (phone_number, email, balance, …) can't be probe-confirmed,
so grounding parks it as `pending` and a human decides. That decision has to
OUTLIVE the run: the ledger is rebuilt fresh each run, so confirmed knowledge would
evaporate without a store. This file is that store — one JSON per active agent at
``~/.gorgon/findings.<agent>.json`` — holding claim entries:

  • pending  — recorded, awaiting `gorgon claim confirm`; NOT usable to close a goal.
  • verified — a human marked it true; loaded back into the next run as a usable fact.

Probe-grounded facts (ip(web), mesh(x)) are deliberately NOT persisted: they're
cheap to re-derive and go stale, so caching them across runs would hand back wrong
answers. Scope is per-agent so a doorman run's claims never leak into a barenboim run.

A SEPARATE per-agent file ``~/.gorgon/toolstats.<agent>.json`` holds per-tool
world-reliability tallies (`load_tool_counts` / `merge_tool_counts`) — the durable
memory behind learned p_world, kept apart from claims so the two never collide. Unlike
probe facts these SHOULD persist: they're the accumulated evidence of how each tool
behaves, and they only get better with more runs.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

from shared.bundle import Bundle


def _safe(agent: Optional[str]) -> str:
    """A filesystem-safe agent key (never traverses out of the bundle root)."""
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", agent or "default") or "default"


def store_path(agent: Optional[str]) -> str:
    """The agent's claim store inside its bundle (~/.gorgon/_agents/<agent>/findings.json)."""
    return Bundle(_safe(agent)).findings_path


def load(agent: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """The stored claim entries for an agent ({} if none / unreadable — never raises)."""
    try:
        with open(store_path(agent)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(agent: Optional[str], entries: Dict[str, Dict[str, Any]]) -> None:
    """Atomically replace the agent's store with `entries` (write-temp-then-rename,
    so a crash mid-write can't corrupt the file)."""
    path = store_path(agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def merge_into(agent: Optional[str], entries: Dict[str, Dict[str, Any]]) -> None:
    """Fold a run's claim entries into the store. A human-VERIFIED fact already in
    the store wins over an incoming pending re-claim (don't undo a confirmation);
    otherwise the incoming entry is written. Called at run end so new pending claims
    survive for later review."""
    data = load(agent)
    for k, v in (entries or {}).items():
        if not isinstance(v, dict):
            continue
        if data.get(k, {}).get("status") == "verified" and v.get("status") != "verified":
            continue
        data[k] = dict(v)
    save(agent, data)


def tool_stats_path(agent: Optional[str]) -> str:
    """Sibling store to the claims file — per-tool WORLD-reliability tallies, kept
    separate so it never collides with claim entries (which are keyed by fact)."""
    return Bundle(_safe(agent)).toolstats_path


def load_tool_counts(agent: Optional[str]) -> Dict[str, Dict[str, int]]:
    """The accumulated `{tool: {"ok": s, "n": n}}` tallies for an agent — the durable
    memory behind learned p_world, so it survives process restarts (not just in-process
    `prior=` forwarding). {} if none / unreadable — never raises."""
    try:
        with open(tool_stats_path(agent)) as f:
            data = json.load(f)
    except Exception:
        return {}
    out: Dict[str, Dict[str, int]] = {}
    if isinstance(data, dict):
        for tool, a in data.items():
            if isinstance(a, dict) and "n" in a:
                out[tool] = {"ok": int(a.get("ok", 0)), "n": int(a.get("n", 0))}
    return out


def merge_tool_counts(agent: Optional[str], counts: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
    """ADD a run's OWN per-tool tallies into the store (accumulate ok/n) and persist
    atomically. Pass only THIS run's counts — never the already-merged prior+run total,
    or the prior gets double-counted. Returns the new accumulated store."""
    data = load_tool_counts(agent)
    for tool, a in (counts or {}).items():
        if not isinstance(a, dict):
            continue
        o = data.setdefault(tool, {"ok": 0, "n": 0})
        o["ok"] += int(a.get("ok", 0))
        o["n"] += int(a.get("n", 0))
    path = tool_stats_path(agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return data


def clear_tool_counts(agent: Optional[str]) -> bool:
    """Wipe an agent's learned tool-reliability tallies (e.g. after a range change makes
    the old p_world stale). Returns True if a store existed and was removed."""
    path = tool_stats_path(agent)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


# The p_self reliability dials — the GLOBAL model-reliability control (θ/λ/D_max)
# derived from a run's success rate. Persisted apart from the per-tool toolstats store
# (which stays the SSOT for per-tool counts) so the two never mix: this file holds ONLY
# the dial scalars, so loading it can never double-count tool tallies.
_DIAL_KEYS = ("p_self", "theta", "lambda", "D_max")


def reliability_path(agent: Optional[str]) -> str:
    """Sibling store to toolstats — the durable p_self dials (θ/λ/D_max), so the p_self
    forward-feed loop survives restarts the way p_world already does, instead of only
    chaining via an in-memory `prior=` the live drivers never pass."""
    return Bundle(_safe(agent)).reliability_path


def load_reliability(agent: Optional[str]) -> Dict[str, Any]:
    """The stored p_self dials `{p_self, theta, lambda, D_max}` for an agent ({} if none /
    unreadable — never raises). Dial scalars ONLY; per-tool counts live in the separate
    toolstats store and are never read from here."""
    try:
        with open(reliability_path(agent)) as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in _DIAL_KEYS if k in data}


def save_reliability(agent: Optional[str], dials: Dict[str, Any]) -> None:
    """Atomically persist THIS run's p_self dials so the next run inherits a shakier/steadier
    stance without hand-fed `prior=`. Stores the dial scalars ONLY — any `tool_counts` on the
    dict are dropped (the toolstats store is their SSOT). No-op if no dial keys are present."""
    out = {k: dials[k] for k in _DIAL_KEYS if k in (dials or {})}
    if not out:
        return
    path = reliability_path(agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def clear_reliability(agent: Optional[str]) -> bool:
    """Wipe an agent's persisted p_self dials (paired with clear_tool_counts on a reset, so
    the forward-fed stance resets too). Returns True if a store existed and was removed."""
    try:
        os.remove(reliability_path(agent))
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def confirm(agent: Optional[str], fact: str) -> bool:
    """Mark a pending claim TRUE. Returns False if the fact isn't a pending claim."""
    data = load(agent)
    e = data.get(fact)
    if not e or e.get("status") != "pending":
        return False
    e["status"] = "verified"          # keep `evidence` as the audit trail
    data[fact] = e
    save(agent, data)
    return True


def reject(agent: Optional[str], fact: str) -> bool:
    """Drop a claim entirely (a human judged it false). Returns False if absent."""
    data = load(agent)
    if fact not in data:
        return False
    del data[fact]
    save(agent, data)
    return True


def listing(agent: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    """The store split into {pending, verified} lists for display, each entry
    [{fact, value, evidence, source}], sorted by fact."""
    data = load(agent)
    out: Dict[str, List[Dict[str, Any]]] = {"pending": [], "verified": []}
    for k, v in sorted(data.items()):
        bucket = v.get("status")
        if bucket in out:
            out[bucket].append({
                "fact": k, "value": v.get("value"),
                "evidence": v.get("evidence"), "source": v.get("source"),
            })
    return out
