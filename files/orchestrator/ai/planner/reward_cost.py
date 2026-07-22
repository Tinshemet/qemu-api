"""
reward_cost.py — the certainty-equivalent (μ, σ²) decision layer (reward-cost step 2).

Cost-only planning has a trivial optimum: inaction (a skipped branch costs ~0, so
avoidance always wins). This layer flips it to **reward − cost** so ACTION is preferred
when a goal is worth it, and passivity has a price (forgone reward).

The design (per gorgon-reward-cost-tree):
    cost(ℓ) = w_r·resource + w_t·time + κ·¬rev        # catalog/contract facts
    μ(g)    = P(g)·R_g − Σcost − H·open_steps         # reward books on branch CLOSURE
    σ²(g)   = P(g)(1−P(g))·R_g² + Σ child-variance
    CE(g)   = μ(g) − (λ/2)·σ²(g)                      # certainty-equivalent (mean-variance)
    P(g)    = Π p_world(leaf)                         # world-noise; p_world is LEARNED per tool,
                                                      # p_self is a global dial (step 5)
    AND = all children needed (gate on Π p);  OR = the max-CE alternative.

Key invariants the math enforces:
- Reward is GOAL-RELATIVE (books only on closing a branch), never intrinsic to a tool —
  so "solve branches beats spawn leaves" is a theorem, not a rule, and reward-hacking a
  tool for points is impossible.
- Destructiveness is NOT in this scalar (that would make the tree passive / afraid to do
  necessary irreversible work) — it is the CONSENT gate (step 3). Only a small
  reversibility route-bias κ lives in cost.
- Value BACKUP fixes the horizon effect: a locally-costly leaf under a high-reward parent
  has positive BACKED-UP CE, so it isn't greedily pruned.

Two knobs adapt the base scheme (both preserve the invariants above):
- α (alpha) SHARES the root reward across sub-goal closures instead of booking it all at
  the root — deep plans earn partial credit as branches close, so a valuable long plan
  doesn't fizzle when its full-depth P → 0. Conserved (shares sum to R at p=1, so depth
  can't farm reward). α=0 is the original root-only behavior. See `economics`'s to_plan.
- p_world is LEARNED per tool from observed outcomes (Beta-smoothed toward the static
  default) rather than being a fixed guess. Adaptive PARAMETER estimation, not RL — the
  planner is unchanged. See `p_world_estimate` / `tool_counts`.

Pure and config-driven — the constants (θ, λ, H, κ, weights, α, R, p_world, p_world_k) are
the real calibration risk, so they're all in DEFAULTS and overridable per call.
"""
from typing import Any, Callable, Dict, List, Optional

# The calibration knobs. Structure is designed; VALUES are not — tuned per deployment
# in reward_cost.json (a .grgn contract's formula.reward_cost overrides per agent). See
# that file's _doc for each knob's meaning.
def _load_defaults() -> Dict[str, float]:
    import json as _json
    import os as _os
    with open(_os.path.join(_os.path.dirname(__file__), "reward_cost.json")) as _f:
        return {k: v for k, v in _json.load(_f).items() if not k.startswith("_")}


DEFAULTS: Dict[str, float] = _load_defaults()


def cfg_with(overrides: Optional[Dict[str, float]]) -> Dict[str, float]:
    c = dict(DEFAULTS)
    if overrides:
        c.update(overrides)
    return c


def leaf_cost(risk: Optional[Dict[str, Any]], cfg: Dict[str, float]) -> float:
    """cost(ℓ) = w_r·resource + w_t·time + κ·¬rev. From the tool's risk facts
    (resource ≈ commitment; reversibility = a small route-bias). Destructiveness is
    deliberately NOT here — it's the consent gate, not a cost."""
    r = risk or {}
    resource = float(r.get("commitment", 0.0))
    irr = 0.0 if r.get("reversible", True) else 1.0
    return cfg["w_resource"] * resource + cfg["w_time"] * cfg["time"] + cfg["kappa"] * irr


def ce(mu: float, var: float, cfg: Dict[str, float]) -> float:
    """Certainty-equivalent: mean penalized by variance × risk-aversion."""
    return mu - (cfg["lambda"] / 2.0) * var


def worth_it(node_ce: float, cfg: Dict[str, float]) -> bool:
    """The worth-it gate: pursue iff backed-up CE clears θ. A reward-less goal has
    CE ≤ θ (skip — nothing to gain); a goal whose reward beats its cost clears it."""
    return node_ce > cfg["theta"]


def backup(node: Dict[str, Any], cfg: Dict[str, float]) -> Dict[str, float]:
    """Back up (mu, var, p, ce) through an abstract plan node.

    node = {"kind": "leaf", "cost": c, "p": p, "reward": r}
         | {"kind": "and"|"or", "children": [...], "reward": R_close}
    AND gates on all children (P = Π p, sum μ/σ²) and books R_close·P at closure, minus
    the WIP holding cost. OR takes the single max-CE alternative.
    """
    kind = node.get("kind", "leaf")
    if kind == "leaf":
        p = float(node.get("p", cfg["p_world"]))
        r = float(node.get("reward", 0.0))
        cost = float(node.get("cost", 0.0))
        mu = p * r - cost
        var = p * (1 - p) * r * r
        return {"mu": mu, "var": var, "p": p, "ce": ce(mu, var, cfg)}

    kids = [backup(c, cfg) for c in node.get("children", [])]
    R = float(node.get("reward", 0.0))
    if kind == "or":
        if not kids:
            return {"mu": 0.0, "var": 0.0, "p": 1.0, "ce": 0.0}
        # OR = the single best alternative. The node's closure reward rides on the CHOSEN
        # alternative succeeding, so book R·p PER alternative BEFORE the max — a higher-p
        # option can win on total value even with lower standalone CE (the reward term
        # R·p rewards reliability). Booking after the max would pick the wrong branch.
        def closed(k: Dict[str, float]) -> Dict[str, float]:
            P = k["p"]
            mu = k["mu"] + R * P
            var = k["var"] + P * (1 - P) * R * R
            return {"mu": mu, "var": var, "p": P, "ce": ce(mu, var, cfg)}
        return max((closed(k) for k in kids), key=lambda x: x["ce"])

    # AND
    P = 1.0
    sum_mu = sum_var = 0.0
    for k in kids:
        P *= k["p"]
        sum_mu += k["mu"]
        sum_var += k["var"]
    open_steps = len(kids)
    mu = sum_mu + R * P - cfg["H"] * open_steps
    var = sum_var + P * (1 - P) * R * R
    return {"mu": mu, "var": var, "p": P, "ce": ce(mu, var, cfg)}


def compound_ce(n_steps: int, cfg: Optional[Dict[str, float]] = None, *,
                reward: float, p: Optional[float] = None, cost: float = 0.0) -> Optional[float]:
    """Backed-up CE of an n-step AND plan under superadditive α crediting — used to price
    a COMPOUND OR alternative BEFORE executing it, so the live worth-it gate ranks a
    deep-but-reliable route by its α-credited value instead of dismissing it on a fizzled
    full-depth product. α>0 credits the sub-steps as they'd close (the route competes and
    can clear θ); α=0 reproduces the collapse (a brittle deep route correctly ranks low).
    Returns None for a non-positive step count (nothing to price → caller keeps it)."""
    if n_steps <= 0:
        return None
    c = cfg_with(cfg)
    pw = c["p_world"] if p is None else p
    share = (c["alpha"] * reward) / n_steps        # pushed down to each sub-step closure
    keep  = (1.0 - c["alpha"]) * reward            # kept at the plan's own closure
    leaves = [{"kind": "leaf", "cost": cost, "p": pw, "reward": share} for _ in range(n_steps)]
    return backup({"kind": "and", "reward": keep, "children": leaves}, c)["ce"]


import math


# Pseudo-tools the engine runs for control flow / verification, NOT goal-advancing
# leaves: checkpoints, rollbacks, and read-only finding probes. They must not
# pollute p_self (fraction of SUCCESSFUL LEAVES) or the per-tool p_world store —
# checkpoints ~always succeed, so counting them biases p̂_self upward, which would
# make the NEXT run LESS cautious (lower θ/λ, deeper budget). Backwards.
_NON_LEAF_TOOLS = frozenset({"checkpoint", "rollback", "guest_probe"})


def _entry_ok(e: Dict[str, Any]) -> bool:
    """Whether a ledger/event entry recorded a success. Ledger entries carry a
    boolean `ok`; event-log entries (event_log.py) instead carry a string
    `outcome` ("ok"/error text) with no `ok` field — support both so either
    source can feed the estimators without a schema mismatch silently scoring
    every tool as a failure."""
    if "ok" in e:
        return bool(e.get("ok"))
    return e.get("outcome") == "ok"


def p_self_estimate(ledger, default: float = 0.9) -> float:
    """The weak model's aggregate reliability `p̂_self`, measured BACKWARD from the
    ledger (fraction of executed leaves that succeeded). p_self is forward-UNmeasurable
    per-move (asking the model to self-rate is a second bad draw), so it's a GLOBAL
    control, never priced per-node. Empty ledger → `default`."""
    outs = [1.0 if _entry_ok(e) else 0.0
            for e in (ledger or [])
            if e.get("tool") and e.get("tool") not in _NON_LEAF_TOOLS]
    return sum(outs) / len(outs) if outs else default


def tool_counts(ledger) -> Dict[str, Dict[str, int]]:
    """Raw per-tool outcome tallies `{tool: {"ok": s, "n": n}}` from an event log /
    ledger (each entry carries `tool` + `ok`). These accumulate ACROSS runs — merge
    the prior tallies with a run's and feed them forward, the same way p_self's dials
    do. Unlike p_self (one GLOBAL model-reliability number), this is per-TOOL WORLD
    reliability: how often each primitive actually succeeds in THIS environment."""
    counts: Dict[str, Dict[str, int]] = {}
    for e in ledger or []:
        t = e.get("tool")
        if not t or t in _NON_LEAF_TOOLS:
            continue
        a = counts.setdefault(t, {"ok": 0, "n": 0})
        a["n"] += 1
        if _entry_ok(e):
            a["ok"] += 1
    return counts


def merge_counts(*tallies: Optional[Dict[str, Dict[str, int]]]) -> Dict[str, Dict[str, int]]:
    """Combine per-tool tallies (prior runs + this run) into one accumulated view."""
    out: Dict[str, Dict[str, int]] = {}
    for t in tallies:
        for tool, a in (t or {}).items():
            o = out.setdefault(tool, {"ok": 0, "n": 0})
            o["ok"] += int(a.get("ok", 0))
            o["n"] += int(a.get("n", 0))
    return out


def p_world_estimate(counts: Optional[Dict[str, Dict[str, int]]],
                     cfg: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """LEARN p_world per tool from observed outcomes — a Beta(k·p₀, k·(1−p₀)) prior
    updated by the tallies: `p̂ = (ok + k·p₀) / (n + k)`. Sparse data stays pinned near
    the static default p₀ (one lucky call can't jump a tool to 1.0); as `n` grows the
    estimate converges to the empirical success rate. This is adaptive PARAMETER
    estimation, NOT reinforcement learning — the decision-theoretic planner is unchanged,
    it just reads a data-grounded p_world instead of a fixed config value."""
    c = cfg_with(cfg)
    p0, k = c["p_world"], c["p_world_k"]
    return {tool: (a["ok"] + k * p0) / (a["n"] + k)
            for tool, a in (counts or {}).items() if a.get("n")}


def p_world_lookup(p_map: Optional[Dict[str, float]],
                   cfg: Optional[Dict[str, float]] = None) -> Callable[[str], float]:
    """A `p_of(tool)` closure over a learned p_world map, falling back to the static
    default for tools never yet observed. Pass to `economics(..., p_of=)` / the CE
    estimator so planning prices each primitive by its measured reliability."""
    c = cfg_with(cfg)
    default = c["p_world"]
    m = p_map or {}
    return lambda tool: m.get(tool, default)


def dials(p_self: float, cfg: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Turn measured p_self into the decision constants (the design's p_self dials):
        θ = θ₀ + β(1−p̂),  λ = λ₀ + γ(1−p̂),  D_max = ⌊ln(ρ_min)/ln(p̂)⌋.
    A shakier model → higher worth-it bar, more risk-aversion, and a SHALLOWER depth
    budget (depth = a self-noise budget: brittle deep plans are cut)."""
    c = cfg_with(cfg)
    p = min(max(p_self, 0.01), 0.99)
    return {
        "p_self": round(p, 4),
        "theta":  round(c["theta"] + c["beta"] * (1 - p), 4),
        "lambda": round(c["lambda"] + c["gamma"] * (1 - p), 4),
        "D_max":  max(1, int(math.floor(math.log(c["rho_min"]) / math.log(p)))),
    }


def should_commit(risk: Optional[Dict[str, Any]], cfg: Optional[Dict[str, float]] = None,
                  *, reward: float = 0.0, p: Optional[float] = None) -> bool:
    """Deliberation scales with IRREVERSIBILITY (the corrigibility principle). A
    REVERSIBLE step just acts — reality is a free, perfect oracle (act-observe-correct),
    so no simulation. An IRREVERSIBLE/expensive step (can't course-correct) is gated on
    its SIMULATED certainty-equivalent: commit only if it's worth it."""
    c = cfg_with(cfg)
    if (risk or {}).get("reversible", True):
        return True                                  # act-observe-correct
    pw = c["p_world"] if p is None else p
    mu = pw * reward - leaf_cost(risk, c)
    var = pw * (1 - pw) * reward * reward
    return worth_it(ce(mu, var, c), c)


def economics(root: Dict[str, Any], *,
              cost_of: Callable[[str], Optional[Dict[str, Any]]],
              cfg: Optional[Dict[str, float]] = None,
              reward: Optional[float] = None,
              p_of: Optional[Callable[[str], float]] = None) -> Dict[str, Any]:
    """Turn a RESOLVED score.py tree into reward-cost economics.

    Walks the tree, prices each executed leaf via `cost_of(tool) -> risk` and its
    world-success prob via `p_of(tool)` (the LEARNED p_world, or the static default when
    omitted), books `reward` on closure — at the root, and (when α > 0) shared across
    sub-goal closures too — and backs up (μ, σ², CE). Returns {mu, var, ce, cost, reward,
    worth_it} — the tree made reward-cost-aware.
    """
    c = cfg_with(cfg)
    R = c["R"] if reward is None else reward
    alpha = c["alpha"]

    def to_plan(n: Dict[str, Any], is_root: bool, budget: float) -> Dict[str, Any]:
        # SUPERADDITIVE sub-goal reward (anti-fizzle): each sub-goal KEEPS (1−α)·budget as
        # its completion bonus — booked on ITS OWN closure (a shallow, higher partial-
        # product P) — and PUSHES α·budget down to its children. A deep plan therefore
        # earns partial credit as sub-branches close, instead of staking the whole reward
        # on the root's full-depth product (which collapses toward 0). α=0 recovers the
        # original "book only at the root" behavior. Conserved: shares sum to R when every
        # p=1, so depth can't farm reward — the [[reward-cost-model]] no-hacking invariant.
        done = n.get("status") == "done"
        keep = (1.0 - alpha) * budget
        push = alpha * budget
        kids = n.get("children")
        if kids:
            kind = "or" if n.get("mode") == "or" else "and"
            # an OR node's untried alternatives (skipped) never ran — drop them so they
            # don't dilute the max-over-alternatives backup with 0-cost phantom leaves.
            if kind == "or":
                kids = [k for k in kids if k.get("status") != "skipped"] or kids
            # AND: children split the pushed budget (all are needed → conserved by sum).
            # OR: only ONE alternative runs, so each carries the FULL push (the chosen one
            # alone must hold the sub-goal reward — same reason OR's R_close rides the max).
            child_budget = push if kind == "or" else (push / len(kids) if kids else 0.0)
            return {"kind": kind,
                    "reward": keep if done else 0.0,
                    "children": [to_plan(k, False, child_budget) for k in kids]}
        tool = n.get("tool")
        risk = cost_of(tool) if tool else None
        cost = leaf_cost(risk, c) if tool else 0.0
        p = (p_of(tool) if (p_of and tool) else c["p_world"])
        # a leaf is the bottom of the plan — it books its WHOLE assigned budget on closure
        # (the root's if it's a single-step goal; its pushed sub-goal share otherwise).
        r = budget if done else 0.0
        return {"kind": "leaf", "cost": cost, "p": p, "reward": r}

    plan = to_plan(root, True, R)
    b = backup(plan, c)

    total_cost = [0.0]
    def walk_cost(n):
        t = n.get("tool")
        if t and not n.get("children"):
            total_cost[0] += leaf_cost(cost_of(t), c)
        for k in n.get("children", []):
            walk_cost(k)
    walk_cost(root)

    return {"mu": round(b["mu"], 4), "var": round(b["var"], 4), "ce": round(b["ce"], 4),
            "cost": round(total_cost[0], 4), "reward": R if root.get("status") == "done" else 0.0,
            "worth_it": worth_it(b["ce"], c)}


def economics_tree(root: Dict[str, Any], *,
                   cost_of: Callable[[str], Optional[Dict[str, Any]]],
                   cfg: Optional[Dict[str, float]] = None,
                   reward: Optional[float] = None,
                   p_of: Optional[Callable[[str], float]] = None) -> Dict[str, Any]:
    """PER-NODE reward-cost breakdown of a resolved tree — the same μ/σ²/CE/worth_it that
    `economics` reports for the whole run, but at EVERY sub-goal, so a verbose autonomous
    run can show WHERE value and uncertainty sit. Returns a nested
    {goal, mu, var, ce, worth_it, tool?, children?}. Uses the SAME α-distributed plan and
    backup as `economics`, so a node's CE here is exactly its backed-up contribution."""
    c = cfg_with(cfg)
    R = c["R"] if reward is None else reward
    alpha = c["alpha"]

    def plan(n: Dict[str, Any], budget: float) -> Dict[str, Any]:
        goal = n.get("goal", "?")
        done = n.get("status") == "done"
        keep, push = (1.0 - alpha) * budget, alpha * budget
        kids = n.get("children")
        if kids:
            kind = "or" if n.get("mode") == "or" else "and"
            if kind == "or":
                kids = [k for k in kids if k.get("status") != "skipped"] or kids
            cb = push if kind == "or" else (push / len(kids) if kids else 0.0)
            return {"kind": kind, "goal": goal, "reward": keep if done else 0.0,
                    "children": [plan(k, cb) for k in kids]}
        tool = n.get("tool")
        cost = leaf_cost(cost_of(tool), c) if tool else 0.0
        p = (p_of(tool) if (p_of and tool) else c["p_world"])
        return {"kind": "leaf", "goal": goal, "tool": tool,
                "cost": cost, "p": p, "reward": budget if done else 0.0}

    def annotate(node: Dict[str, Any]) -> Dict[str, Any]:
        b = backup(node, c)
        out = {"goal": node["goal"], "mu": round(b["mu"], 4), "var": round(b["var"], 4),
               "ce": round(b["ce"], 4), "worth_it": worth_it(b["ce"], c)}
        if node.get("children"):
            out["children"] = [annotate(k) for k in node["children"]]
        elif node.get("tool"):
            out["tool"] = node["tool"]
        return out

    return annotate(plan(root, R))
