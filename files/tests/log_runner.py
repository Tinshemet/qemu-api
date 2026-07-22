#!/usr/bin/env python3
"""
log_runner.py — run a batch of prompts through the autonomous loop and emit a clean,
timestamped step log (decompose → gate → run → verify → result). Real local model
plans; execution is a safe stub (no real VMs). Writes the log to autonomous_run.log.

Usage:  PYTHONPATH=files python3 files/tests/log_runner.py ["extra prompt" ...]
"""
import os
import sys
import time
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from orchestrator.ai.agent import contract as C
from orchestrator.ai.planner.score import run_score, _first_tool_call
from orchestrator.ai.planner.autonomous import make_library_verifier

_AI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orchestrator/ai")
_CFG = json.load(open(os.path.join(_AI, "config.json")))["ollama"]
_MODEL = _CFG["model"]

_LOG = []
def emit(s=""):
    print(s, flush=True)
    _LOG.append(s)

def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class World:
    def __init__(self):
        self.vms = {}
    def execute(self, tool, args):
        n = args.get("name") or args.get("new_name")
        if tool == "create_vm": self.vms[n] = {"status": "stopped"}
        elif tool == "launch_vm" and n in self.vms: self.vms[n]["status"] = "running"
        elif tool == "stop_vm" and n in self.vms: self.vms[n]["status"] = "stopped"
        elif tool == "delete_vm": self.vms.pop(n, None)
        return {"success": True}


def raw_model(messages, tools):
    payload = {"model": _MODEL, "messages": messages, "tools": tools, "stream": False,
               "options": {"temperature": _CFG["temperature"], "num_ctx": _CFG["num_ctx"]}}
    return requests.post(_CFG["url"] + "/api/chat", json=payload, timeout=_CFG["timeout"]).json()


_TOOL_NAMES = ("create_vm", "launch_vm", "stop_vm", "list_vms", "delete_vm")
_TOOLS = [f for f in json.load(open(os.path.join(_AI, "tools.json")))
          if f["function"]["name"] in _TOOL_NAMES]


def run_prompt(goal):
    emit()
    emit(f'Prompt: "{goal}"')
    world = World()
    step = {"n": 0}
    def num():
        step["n"] += 1
        return step["n"]

    def call_model(messages, tools):
        resp = raw_model(messages, tools)
        name, args = _first_tool_call(resp)
        if name == "decompose":
            steps = args.get("steps") or []
            emit(f"  [{ts()}] {num()}. decomposed into: " + ", ".join(f'"{s}"' for s in steps))
        return resp

    def gate(tool, args):
        act = C._HANDLING["autonomous"][C.resolve_tier(tool, args)]
        if act in ("halt", "checkpoint"):
            emit(f"  [{ts()}] {num()}. gate {tool} -> {act.upper()}"
                 + ("  (blocked, not executed)" if act == "halt" else "  (savepoint first)"))
        return act

    def execute(tool, args):
        r = world.execute(tool, args)
        ok = not (isinstance(r, dict) and (r.get("success") is False or r.get("error")))
        argstr = ", ".join(f"{k}={v}" for k, v in args.items())
        emit(f"  [{ts()}] {num()}. running {tool}({argstr}) -> {'success' if ok else 'FAIL'}")
        return r

    _base_verify = make_library_verifier(lambda: world.vms)
    def verify(criterion, tool, args, result):
        ok = _base_verify(criterion, tool, args, result)
        emit(f"  [{ts()}]      └ verify {tool} '{criterion}' -> {'ok' if ok else 'UNVERIFIED'}")
        return ok

    t0 = time.time()
    r = run_score(goal, call_model=call_model, execute=execute, tools=_TOOLS,
                  gate=gate, verify=verify, decompose_first=True, max_depth=3)
    dt = time.time() - t0
    world_str = ", ".join(f"{k}={v['status']}" for k, v in world.vms.items()) or "(empty)"
    s = r["root"]["status"].upper()
    emit(f"  [{ts()}] {num()}. RESULT: {s}   executed={len(r['ledger'])}   world={{{world_str}}}   ({dt:.1f}s)")


def main():
    prompts = sys.argv[1:] or [
        "create a linux vm named web and launch it",
        "create two linux vms named alpha and beta",
        "delete the web vm",
    ]
    emit("=" * 72)
    emit(f"AUTONOMOUS RUN LOG   model={_MODEL} (real)   execution=safe stub   {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    emit("=" * 72)
    for p in prompts:
        run_prompt(p)
    out = os.path.join(os.path.dirname(_AI), "autonomous_run.log")
    open(out, "w").write("\n".join(_LOG) + "\n")
    emit()
    emit(f"(log saved to {out})")


if __name__ == "__main__":
    main()
