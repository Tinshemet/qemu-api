#!/usr/bin/env python3
"""
test_reward_cost.py — the certainty-equivalent (μ, σ²) decision layer.

Proves the design's load-bearing properties: cost from risk facts; passivity is
killed (a reward-less goal fails worth-it, a rewarded one passes); value BACKUP fixes
the horizon effect; risk-aversion (λ) penalizes variance; OR picks the max-CE
alternative; and economics scores a resolved tree.

Run:  PYTHONPATH=files python3 files/tests/test_reward_cost.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.reward_cost import (
    DEFAULTS, cfg_with, leaf_cost, ce, worth_it, backup, economics,
)

_PASS = 0
_FAIL = 0


def check(label, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


def approx(a, b, e=1e-6):
    return abs(a - b) < e


def main():
    c = cfg_with(None)

    print("leaf cost from risk facts")
    check("reversible cheap leaf", approx(leaf_cost({"commitment": 1.0, "reversible": True}, c), 1.0 + 0.3))
    check("irreversibility adds a route-bias", approx(leaf_cost({"commitment": 1.0, "reversible": False}, c), 1.0 + 0.3 + 0.2))
    check("destructiveness is NOT in cost", leaf_cost({"destructiveness": 1.0, "commitment": 0.0, "reversible": True}, c)
          == leaf_cost({"destructiveness": 0.0, "commitment": 0.0, "reversible": True}, c))

    print("\npassivity is killed: reward-less skip, rewarded act")
    passive = backup({"kind": "leaf", "cost": 1.0, "p": 0.9, "reward": 0.0}, c)
    check("reward-less leaf: CE ≤ θ → NOT worth it", not worth_it(passive["ce"], c))
    rewarded = backup({"kind": "and", "reward": 5.0,
                       "children": [{"kind": "leaf", "cost": 1.0, "p": 0.9, "reward": 0.0}]}, c)
    check("rewarded branch: CE > θ → worth it", worth_it(rewarded["ce"], c))
    check("reward books on CLOSURE (P·R lifts μ)", rewarded["mu"] > passive["mu"])

    print("\nvalue BACKUP fixes the horizon effect")
    costly_leaf = backup({"kind": "leaf", "cost": 2.0, "p": 0.9, "reward": 0.0}, c)
    check("locally: a costly leaf is not worth it", not worth_it(costly_leaf["ce"], c))
    backed = backup({"kind": "and", "reward": 5.0,
                     "children": [{"kind": "leaf", "cost": 2.0, "p": 0.9, "reward": 0.0}]}, c)
    check("backed-up: same leaf under a rewarded parent IS worth it", worth_it(backed["ce"], c))

    print("\nrisk aversion (λ) penalizes variance")
    check("same μ, more σ² → lower CE", ce(1.0, 4.0, c) < ce(1.0, 0.0, c))
    check("a certain reward (σ²=0) is unaffected by λ",
          ce(1.0, 0.0, cfg_with({"lambda": 5.0})) == ce(1.0, 0.0, c))
    check("higher λ punishes the speculative branch harder",
          ce(1.0, 4.0, cfg_with({"lambda": 1.0})) < ce(1.0, 4.0, c))

    print("\nOR picks the max-CE alternative")
    both = backup({"kind": "or", "children": [
        {"kind": "leaf", "cost": 0.0, "p": 1.0, "reward": 2.0},   # ce = 2
        {"kind": "leaf", "cost": 0.0, "p": 1.0, "reward": 1.0}]}, c)  # ce = 1
    check("OR = the better alternative", approx(both["ce"], 2.0))

    print("\neconomics over a resolved tree")
    tree = {"goal": "set up web", "status": "done", "children": [
        {"goal": "create web", "status": "done", "tool": "create_vm"},
        {"goal": "launch web", "status": "done", "tool": "launch_vm"}]}
    risk = {"create_vm": {"commitment": 1.0, "reversible": True},
            "launch_vm": {"commitment": 1.0, "reversible": True}}
    econ = economics(tree, cost_of=lambda t: risk.get(t), reward=5.0)
    check("done goal earns its reward", econ["reward"] == 5.0)
    check("cost is the sum of leaf costs", econ["cost"] > 0)
    check("worth-it (reward beats cost)", econ["worth_it"] is True)

    print("\np_self: measured from the ledger, drives the dials")
    from orchestrator.ai.planner.reward_cost import p_self_estimate, dials, should_commit
    led = [{"tool": "a", "ok": True}, {"tool": "b", "ok": True},
           {"tool": "c", "ok": True}, {"tool": "d", "ok": False}]
    check("p_self = fraction of leaves that succeeded", approx(p_self_estimate(led), 0.75))
    check("empty ledger -> default", p_self_estimate([]) == 0.9)
    steady, shaky = dials(0.9), dials(0.6)
    check("shakier model -> higher worth-it bar θ", shaky["theta"] > steady["theta"])
    check("shakier model -> more risk-averse λ", shaky["lambda"] > steady["lambda"])
    check("shakier model -> shallower depth budget", shaky["D_max"] < steady["D_max"])

    print("\nlookahead: deliberation scales with irreversibility")
    check("a reversible step just acts (no simulation)", should_commit({"reversible": True}, reward=0.0) is True)
    check("an irreversible step with no payoff is NOT committed",
          should_commit({"reversible": False, "commitment": 1.0}, reward=0.0) is False)
    check("an irreversible step worth its cost IS committed",
          should_commit({"reversible": False, "commitment": 0.1}, reward=10.0) is True)

    print("\nsigned reward: a penalty (R<0) is a priceable 'don't'")
    pen = backup({"kind": "and", "reward": -5.0, "children": [{"kind": "leaf", "cost": 0.5, "p": 0.9, "reward": 0.0}]}, c)
    check("a penalized action is NOT worth it", not worth_it(pen["ce"], c))
    big = backup({"kind": "and", "reward": 20.0, "children": [{"kind": "leaf", "cost": 0.5, "p": 0.9, "reward": 0.0}]}, c)
    check("a big enough reward overcomes cost (priceable)", worth_it(big["ce"], c))

    print("\nsuperadditive sub-goal reward (anti-fizzle): partial credit as branches close")
    # A deep AND chain: p_root = 0.9^4 ≈ 0.656, so a root-only reward collapses with depth.
    deep = {"goal": "root", "status": "done", "children": [
        {"goal": "a", "status": "done", "tool": "t"},
        {"goal": "b", "status": "done", "children": [
            {"goal": "b1", "status": "done", "tool": "t"},
            {"goal": "b2", "status": "done", "children": [
                {"goal": "b2a", "status": "done", "tool": "t"}]}]}]}
    risk_free = {"t": {"commitment": 0.0, "reversible": True}}
    of = lambda t: risk_free.get(t)
    free = {"w_time": 0.0, "time": 0.0, "H": 0.0}   # isolate the reward term (no cost, no holding)
    base = economics(deep, cost_of=of, reward=10.0, cfg={"alpha": 0.0, **free})
    supr = economics(deep, cost_of=of, reward=10.0, cfg={"alpha": 0.6, **free})
    check("α=0 books only at the root (reward rides the full-depth product)",
          approx(base["mu"], 10.0 * (0.9 ** 3)))   # 3 leaves in the chain
    check("α>0 lifts μ — sub-goal closures earn at their shallower, higher P", supr["mu"] > base["mu"])
    check("reward is CONSERVED: all p=1 → total booked == R (no depth farming)",
          approx(economics(deep, cost_of=of, reward=10.0,
                           cfg={"alpha": 0.6, "p_world": 1.0, **free})["mu"], 10.0))
    check("α=0 is exactly the original root-only behavior (backward compatible)",
          approx(base["mu"], economics(deep, cost_of=of, reward=10.0, cfg=dict(free))["mu"]))
    # a sub-goal NOT closed forfeits its share, but closed siblings still bank theirs
    partial = {"goal": "root", "status": "blocked", "children": [
        {"goal": "a", "status": "done", "tool": "t"},
        {"goal": "b", "status": "blocked", "tool": "t"}]}
    pe = economics(partial, cost_of=of, reward=10.0, cfg={"alpha": 1.0, **free})
    check("partial run still banks the closed sub-goal's share (μ > 0)", pe["mu"] > 0.0)

    print("\neconomics_tree: per-NODE μ/CE breakdown (verbose autonomous view)")
    from orchestrator.ai.planner.reward_cost import economics_tree
    tree2 = {"goal": "root", "status": "done", "children": [
        {"goal": "a", "status": "done", "tool": "t"},
        {"goal": "b", "status": "done", "children": [
            {"goal": "b1", "status": "done", "tool": "t"}]}]}
    et = economics_tree(tree2, cost_of=lambda t: {"commitment": 0.0, "reversible": True},
                        reward=10.0, cfg={"alpha": 0.5, "w_time": 0.0, "time": 0.0})
    check("annotates every node with a goal + ce", et["goal"] == "root" and "ce" in et)
    check("nesting mirrors the resolved tree", [c["goal"] for c in et["children"]] == ["a", "b"])
    check("a leaf node carries its tool", et["children"][0]["tool"] == "t")
    check("root ce == whole-run economics ce (same α plan + backup)",
          approx(et["ce"], economics(tree2, cost_of=lambda t: {"commitment": 0.0, "reversible": True},
                                     reward=10.0, cfg={"alpha": 0.5, "w_time": 0.0, "time": 0.0})["ce"]))

    print("\ncompound_ce: α steers the LIVE worth-it gate for deep routes")
    from orchestrator.ai.planner.reward_cost import compound_ce
    # a 5-step route: at α=0 its CE is the fizzled full-depth value; α>0 lifts it.
    fizz = compound_ce(5, cfg_with({"alpha": 0.0, "H": 0.0}), reward=10.0, p=0.9, cost=0.1)
    lift = compound_ce(5, cfg_with({"alpha": 0.6, "H": 0.0}), reward=10.0, p=0.9, cost=0.1)
    check("α>0 raises a deep route's CE above the α=0 fizzle", lift > fizz)
    fizz0 = compound_ce(5, cfg_with({"alpha": 0.0, "H": 0.0, "lambda": 0.0}), reward=10.0, p=0.9, cost=0.1)
    check("α=0, λ=0: CE=μ ≈ the collapsed full-depth reward (R·p^5 − Σcost)",
          approx(fizz0, 10.0 * (0.9 ** 5) - 5 * 0.1))
    check("a deep route can clear θ=0 with α but not without",
          worth_it(lift, cfg_with(None)) and not worth_it(fizz, cfg_with(None)))
    check("empty/zero-step route → None (caller keeps it, doesn't prune)",
          compound_ce(0, cfg_with(None), reward=10.0) is None)

    print("\nlearned p_world: per-tool success rate from observed outcomes (not RL)")
    from orchestrator.ai.planner.reward_cost import (tool_counts, merge_counts, p_world_estimate, p_world_lookup)
    led2 = [{"tool": "reliable", "ok": True}] * 8 + [{"tool": "reliable", "ok": False}] * 2 + \
           [{"tool": "flaky", "ok": False}] * 6 + [{"tool": "flaky", "ok": True}] * 2
    counts = tool_counts(led2)
    check("counts tally ok/n per tool", counts["reliable"] == {"ok": 8, "n": 10})
    pw = p_world_estimate(counts, c)
    check("a reliable tool learns a HIGH p_world", pw["reliable"] > 0.8)
    check("a flaky tool learns a LOW p_world (below the 0.9 default)", pw["flaky"] < 0.5)
    sparse = p_world_estimate(tool_counts([{"tool": "once", "ok": True}]), c)
    check("sparse data stays pinned near the default (one call can't reach 1.0)", sparse["once"] < 0.95)
    lookup = p_world_lookup(pw, c)
    check("lookup returns the learned value", approx(lookup("flaky"), pw["flaky"]))
    check("an unobserved tool falls back to the static default", approx(lookup("never_seen"), c["p_world"]))
    merged = merge_counts({"t": {"ok": 1, "n": 2}}, {"t": {"ok": 3, "n": 4}}, {"u": {"ok": 1, "n": 1}})
    check("counts accumulate across runs (forward-fed)", merged["t"] == {"ok": 4, "n": 6})

    print("\nedge: extreme probabilities don't break the math")
    check("p=1 (certain) → zero variance", backup({"kind": "leaf", "cost": 0.0, "p": 1.0, "reward": 5.0}, c)["var"] == 0.0)
    check("p=0 (impossible) → μ is just −cost", approx(backup({"kind": "leaf", "cost": 1.0, "p": 0.0, "reward": 5.0}, c)["mu"], -1.0))
    check("dials clamp p_self=1.0 (no div-by-zero)", dials(1.0)["D_max"] >= 1)
    check("dials clamp p_self=0.0", dials(0.0)["D_max"] >= 1)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
