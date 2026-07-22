"""chat_client/operator.py — mission + claim operator verbs wired into the chat."""

import threading

import curses

from client.ui.chat_client import state
from client.ui.chat_client.colors import cp as _cp, C_CYAN, C_DIM, C_GREEN, C_RED, C_YELLOW
from client.ui.chat_client.conn import _auth_store, _auth_sessions
from client.ui.chat_client.history import add as _add


def mission_worker(goal, mission_name, verbose) -> None:
    """Background thread body — run an autonomous mission and queue its result. The
    autonomous loop is long-running, so it can't block the curses draw loop; the
    result is rendered on the main thread from the drain (see render_mission_result)."""
    try:
        from orchestrator.ai.planner.autonomous import run_autonomous_live
        from orchestrator.ai.mission import mission as M
        if mission_name:
            mobj, st = M.load(mission_name)
            if not mobj:
                state.resp_q.put({"_mission": {"goal": mission_name, "error": f"no mission '{mission_name}' ({st})"}})
                return
            goal = mobj.goal
        else:
            mobj = M.Mission.ephemeral(goal)
        result = run_autonomous_live(goal, mission=mobj)
        state.resp_q.put({"_mission": {"goal": goal, "result": result}})
    except Exception as e:                                   # never let a worker crash take down the UI
        state.resp_q.put({"_mission": {"goal": goal or mission_name, "error": str(e)}})


def render_mission_result(m: dict) -> None:
    """Render a queued mission result on the main thread."""
    if m.get("error"):
        _add(f"  ✖ mission failed: {m['error']}", _cp(C_RED))
        return
    r = m.get("result", {})
    s = r.get("summary", {}) or {}
    ok = r.get("ok")
    _add(f"  {'✔' if ok else '✖'} mission '{m['goal']}' — status={s.get('status')} "
         f"executed={s.get('executed')}", _cp(C_GREEN if ok else C_YELLOW) | curses.A_BOLD)
    econ = r.get("economics")
    if econ:
        _add(f"    economics: μ={econ.get('mu')} ce={econ.get('ce')} "
             f"cost={econ.get('cost')} reward={econ.get('reward')}", _cp(C_DIM))
    review = r.get("claims_for_review") or []
    if review:
        _add(f"    ⚠ {len(review)} claim(s) need confirmation — claim confirm <fact>:", _cp(C_YELLOW))
        for c in review:
            _add(f"      {c['fact']} = {c['value']!r}   ← {c.get('evidence') or '—'}", _cp(C_DIM))


def apply_claim(action: str, fact: str) -> None:
    """Confirm/reject a claim in the active agent's store (post operator re-auth)."""
    from orchestrator.ai.planner import findings_store as store
    from orchestrator.ai.agent.contract import active_agent_key
    key = active_agent_key()
    ok = store.confirm(key, fact) if action == "confirm" else store.reject(key, fact)
    if ok:
        _add(f"  ✔ {action}ed {fact}", _cp(C_GREEN))
    else:
        _add(f"  ✖ no {'pending ' if action == 'confirm' else ''}claim '{fact}'", _cp(C_RED))


def handle_claim(arg: str) -> None:
    """`claim [list] | confirm <fact> | reject <fact>` inside the chat."""
    from orchestrator.ai.planner import findings_store as store
    from orchestrator.ai.agent.contract import active_agent_key
    key = active_agent_key()
    parts = arg.split()
    sub = parts[0] if parts else "list"
    if sub == "list" or not parts:
        data = store.listing(key)
        _add(f"  Claims for agent {key}", _cp(C_CYAN) | curses.A_BOLD)
        if not data["pending"] and not data["verified"]:
            _add("    none", _cp(C_DIM))
        for e in data["pending"]:
            _add(f"    [pending] {e['fact']} = {e['value']!r}", _cp(C_YELLOW))
            _add(f"        evidence: {e.get('evidence') or '—'}", _cp(C_DIM))
        for e in data["verified"]:
            _add(f"    [verified] {e['fact']} = {e['value']!r}", _cp(C_GREEN))
    elif sub in ("confirm", "reject") and len(parts) >= 2:
        fact = " ".join(parts[1:])
        if _auth_store is not None and _auth_store.operators_exist():
            state.pending_claim = (sub, fact)   # main loop captures the masked password next
            state.is_password = True
            _add(f"  Operator password to {sub} {fact}:", _cp(C_YELLOW) | curses.A_BOLD)
        else:
            apply_claim(sub, fact)              # pre-bootstrap / no operators → no gate
    else:
        _add("  Usage: claim [list] | confirm <fact> | reject <fact>", _cp(C_DIM))


def handle_mission(arg: str, verbose: bool) -> None:
    """`mission [list] | run <name> | new | "<goal>"` inside the chat."""
    from orchestrator.ai.mission import mission as M
    from orchestrator.ai.agent.contract import active_agent_key
    parts = arg.split()
    sub = parts[0] if parts else "list"
    if sub == "list" or not parts:
        ms = M.list_missions()
        _add(f"  Missions for agent {active_agent_key()}", _cp(C_CYAN) | curses.A_BOLD)
        if not ms:
            _add("    none — author one in a terminal: gorgon mission new", _cp(C_DIM))
        for m in ms:
            _add(f"    {m['name']:<22} {m['title']}  ({m['status']})", _cp(C_DIM))
    elif sub == "new":
        _add("  The mission wizard needs a full terminal prompt — run: gorgon mission new", _cp(C_YELLOW))
    elif sub == "show" and len(parts) >= 2:
        m, status = M.load(parts[1])
        if not m:
            _add(f"  ✖ no mission '{parts[1]}' ({status})", _cp(C_RED))
        else:
            for line in M.render(m).split("\n"):
                _add(line, _cp(C_DIM))
            _add(f"  integrity: {status}", _cp(C_DIM))
    elif sub in ("run", "show") and len(parts) < 2:
        _add(f"  Usage: mission {sub} <name>", _cp(C_DIM))
    elif sub == "run" and len(parts) >= 2:
        _add(f"  ▶ running mission {parts[1]}…", _cp(C_CYAN))
        state.waiting = True
        threading.Thread(target=mission_worker, args=(None, parts[1], verbose), daemon=True).start()
    elif sub == "run":
        _add("  Usage: mission run <name>", _cp(C_DIM))
    else:                                       # bare goal → quick ephemeral mission
        _add(f"  ▶ mission: {arg}…", _cp(C_CYAN))
        state.waiting = True
        threading.Thread(target=mission_worker, args=(arg, None, verbose), daemon=True).start()
