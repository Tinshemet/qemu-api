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

_DIR = os.path.expanduser("~/.gorgon")


def _safe(agent: Optional[str]) -> str:
    """A filesystem-safe agent key (never traverses out of the store dir)."""
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", agent or "default") or "default"


def store_path(agent: Optional[str]) -> str:
    return os.path.join(_DIR, f"findings.{_safe(agent)}.json")


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
    os.makedirs(_DIR, exist_ok=True)
    path = store_path(agent)
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
    return os.path.join(_DIR, f"toolstats.{_safe(agent)}.json")


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
    os.makedirs(_DIR, exist_ok=True)
    path = tool_stats_path(agent)
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
