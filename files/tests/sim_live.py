#!/usr/bin/env python3
"""
sim_live.py — WATCH the autonomous loop run against the real local model, live.

Streams every step as it happens: each planning call to the model, the move it
picked (decompose vs a tool), the contract's handling, execution (safe stub — no
real VMs), and verified completion. Because each call to the local model takes a
few seconds, you follow it in real time.

Usage:
  PYTHONPATH=files python3 files/tests/sim_live.py "GOAL"
  PYTHONPATH=files python3 files/tests/sim_live.py "GOAL" --prompt doorman
  PYTHONPATH=files python3 files/tests/sim_live.py "GOAL" --model qwen2.5:7b

  --prompt raw     (default) decomposition-first: send ONLY the node instruction
                   (the tree's decompose-or-primitive prompt). Tests whether the
                   model decomposes when NOT told to "act immediately".
  --prompt doorman prepend the Doorman system prompt (its "call the tool right now"
                   bias) — the current live behavior, for comparison.
"""
import os
import sys
import time
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from orchestrator.ai.agent import contract as C
from orchestrator.ai.planner.autonomous import run_autonomous
from orchestrator.ai.planner.score import _first_tool_call

_AI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orchestrator/ai")
_CFG = json.load(open(os.path.join(_AI, "config.json")))["ollama"]


def log(msg):
    print(msg, flush=True)


class World:
    def __init__(self, vms=None):
        self.vms = {k: dict(v) for k, v in (vms or {}).items()}

    def execute(self, tool, args):
        n = args.get("name") or args.get("new_name")
        log(f"        ⚙  EXECUTE {tool}(" + ", ".join(f"{k}={v}" for k, v in args.items()) + ")")
        if tool == "create_vm":
            self.vms[n] = {"status": "stopped"}
        elif tool == "launch_vm" and n in self.vms:
            self.vms[n]["status"] = "running"
        elif tool == "stop_vm" and n in self.vms:
            self.vms[n]["status"] = "stopped"
        elif tool == "delete_vm":
            self.vms.pop(n, None)
        return {"success": True}


def build_model(mode, model_name):
    """Return a call_model(messages, tools) that either sends the node prompt raw or
    prepends the Doorman persona — and narrates each call live."""
    def _post(messages, tools):
        payload = {"model": model_name, "messages": messages, "tools": tools,
                   "stream": False, "options": {"temperature": _CFG["temperature"], "num_ctx": _CFG["num_ctx"]}}
        return requests.post(_CFG["url"] + "/api/chat", json=payload, timeout=_CFG["timeout"]).json()

    def call_model(messages, tools):
        if mode == "doorman":
            from orchestrator.ai.chat.ollama_client import _build_system_prompt
            messages = [{"role": "system", "content": _build_system_prompt()}] + messages
        goal = next((m["content"].replace("Goal: ", "") for m in reversed(messages) if m["role"] == "user"), "?")
        offered = [t["function"]["name"] for t in tools]
        log(f'\n  ◇ [model] planning: "{goal}"')
        log(f"           offered: {', '.join(offered)}")
        t0 = time.time()
        resp = _post(messages, tools)
        dt = time.time() - t0
        name, args = _first_tool_call(resp)
        if name == "decompose":
            log(f"    ↳ DECOMPOSE → {args.get('steps')}   ({dt:.1f}s)")
        elif name:
            tier = C.resolve_tier(name, args)
            act = C._HANDLING["autonomous"][tier]
            log(f"    ↳ tool: {name}({args})   [tier={tier} → {act}]   ({dt:.1f}s)")
        else:
            log(f"    ↳ (no tool call)   ({dt:.1f}s)")
        return resp

    return call_model


def show(n, d=0):
    pad = "   " + "  " * d
    if n.get("children"):
        log(f'{pad}◇ "{n["goal"][:50]}" →')
        for c in n["children"]:
            show(c, d + 1)
    else:
        tag = n["status"] + (f":{n.get('reason')}" if n.get("reason") else "")
        log(f'{pad}• "{n["goal"][:50]}"  ⇒  {n.get("tool", "—")}  [{tag}]')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("goal")
    ap.add_argument("--prompt", choices=["raw", "doorman"], default="raw")
    ap.add_argument("--model", default=_CFG["model"])
    ap.add_argument("--flat", action="store_true",
                    help="disable decompose-first scaffolding (see the raw one-shot behavior)")
    a = ap.parse_args()

    names = ("create_vm", "launch_vm", "stop_vm", "list_vms", "delete_vm")
    tools = [f for f in json.load(open(os.path.join(_AI, "tools.json")))
             if f["function"]["name"] in names]
    gate = lambda t, ar: C._HANDLING["autonomous"][C.resolve_tier(t, ar)]
    world = World()

    log("═" * 72)
    log(f'GOAL   : "{a.goal}"')
    log(f"MODEL  : {a.model} (real)   PROMPT: {a.prompt}   EXEC: safe stub   DISPOSITION: autonomous")
    log("═" * 72)
    t0 = time.time()
    r = run_autonomous(a.goal, call_model=build_model(a.prompt, a.model), execute=world.execute,
                       tools=tools, vms_getter=lambda: world.vms, gate=gate,
                       decompose_first=not a.flat, max_depth=3)
    log("\n" + "─" * 72 + "\nFINAL PLAN + HANDLING:")
    show(r["root"])
    log(f"\nRESULT : {r['root']['status'].upper()}   summary: " +
        str({k: v for k, v in r["summary"].items() if k != "status"}))
    log(f"WORLD  : " + (str({k: v['status'] for k, v in world.vms.items()}) or "(empty)"))
    log(f"(total {time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
