"""live_tree.py — a streaming Rich view of the Score plan tree as it executes.

The autonomous loop emits node-lifecycle events (enter / plan / leaf / close) via the
engine's `on_node` hook; this consumes them into a live goal→primitive tree, so an
operator watches the plan form, branch, act, and close in real time — the way a task
list streams, but the reasoning tree. Pure display: it never drives the run, and a
render error can never break it (the engine guards the callback).

Wire it (attended runs only):

    tree = LivePlanTree(goal)
    with Live(tree.render(), console=console, auto_refresh=False) as live:
        tree.on_update = lambda: live.update(tree.render(), refresh=True)
        run_autonomous_live(goal, mission=m, on_node=tree.handle)
"""
from typing import Any, Callable, Dict, List, Optional, Tuple

from rich.tree import Tree

# status → (glyph, rich style). Covers every node status the engine can emit, plus the
# transient "running/acting" states this view synthesizes from enter/leaf events.
_STATUS = {
    "running":    ("⏳", "yellow"),
    "acting":     ("▸",  "cyan"),
    "pending":    ("·",  "dim"),
    "done":       ("✓",  "green"),
    "failed":     ("✗",  "red"),
    "unverified": ("?",  "red"),
    "blocked":    ("⊘",  "red"),
    "forbidden":  ("⊘",  "red"),
    "aborted":    ("■",  "red"),
    "partial":    ("◐",  "yellow"),
    "skipped":    ("–",  "dim"),
    "no_action":  ("∅",  "dim"),
}


class LivePlanTree:
    """Builds a Rich tree from engine on_node events. A node's identity is its full
    goal-path (tuple(path) + (goal,)), so children attach under the right parent even when
    goal strings repeat elsewhere in the tree."""

    def __init__(self, root_goal: str):
        self._root: Tuple[str, ...] = (root_goal,)
        self._node: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        self._kids: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = {}
        self.on_update: Optional[Callable[[], None]] = None
        self._touch(self._root, root_goal, "running")

    def _touch(self, key: Tuple[str, ...], goal: str, status: str) -> None:
        n = self._node.get(key)
        if n is None:
            self._node[key] = {"goal": goal, "status": status, "tool": None, "mode": None, "flag": None}
        elif status:
            self._node[key]["status"] = status

    def handle(self, ev: Dict[str, Any]) -> None:
        """Consume one engine event and refresh the view."""
        path = tuple(ev.get("path") or [])
        goal = ev.get("goal", "")
        key = path + (goal,)
        kind = ev.get("kind")
        if kind == "enter":
            # A node already closed (a fast re-attempt) shouldn't be reset to running.
            if self._node.get(key, {}).get("status") not in _STATUS or key not in self._node:
                self._touch(key, goal, "running")
        elif kind == "plan":
            self._touch(key, goal, "running")
            self._node[key]["mode"] = ev.get("mode")
            kids = []
            for c in ev.get("children") or []:
                ck = key + (c,)
                self._touch(ck, c, "pending")
                kids.append(ck)
            self._kids[key] = kids                       # a re-plan replaces the child list
        elif kind == "leaf":
            self._touch(key, goal, "acting")
            self._node[key]["tool"] = ev.get("tool")
            if ev.get("rationale"):
                self._node[key]["rationale"] = ev["rationale"]   # the stated reason (D1 audit)
        elif kind == "close":
            self._touch(key, goal, ev.get("status") or "done")
            if ev.get("revised"):
                self._node[key]["flag"] = "revised"
            if ev.get("reason") and ev.get("status") in ("blocked", "skipped", "unverified", "forbidden"):
                self._node[key]["flag"] = ev["reason"]
        if self.on_update:
            self.on_update()

    def _label(self, key: Tuple[str, ...]) -> str:
        n = self._node[key]
        glyph, style = _STATUS.get(n["status"], ("·", "white"))
        parts = [f"[{style}]{glyph}[/{style}] {n['goal']}"]
        if n.get("tool"):
            parts.append(f"[dim]→ {n['tool']}[/dim]")
        if n.get("mode") == "or":
            parts.append("[dim](or)[/dim]")
        if n.get("flag"):
            parts.append(f"[dim italic]{n['flag']}[/dim italic]")
        if n.get("rationale"):
            r = n["rationale"]
            parts.append(f"[dim italic]“{r[:60]}{'…' if len(r) > 60 else ''}”[/dim italic]")
        return " ".join(parts)

    def _build(self, key: Tuple[str, ...], into: Tree) -> None:
        for ck in self._kids.get(key, []):
            if ck in self._node:
                branch = into.add(self._label(ck))
                self._build(ck, branch)

    def render(self) -> Tree:
        """The current tree as a Rich renderable (safe to call any time)."""
        t = Tree(self._label(self._root))
        self._build(self._root, t)
        return t
