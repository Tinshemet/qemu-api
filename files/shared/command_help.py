"""
command_help.py — Render help from the single command catalog.

Both help surfaces call in here so they render the same authored list:
  - terminal help (`gorgon help`)      -> render_terminal_panel(...)
  - AI-chat CLI help (curses scrollback) -> visible_commands(...) + each entry's ai_example

Every view is filtered for compliance with the allowed-tools list before rendering.
"""
from typing import Any, Dict, List, Optional, Set, Tuple

_DEFAULT_ORDER = ["VM lifecycle", "Disk & snapshots", "Networking",
                  "Inspect", "Stealth", "Transfer"]


# Imports the authored catalog from the executor package (single-box / local mode).
# In: nothing → Out: (catalog, category_order) or (None, None) if unavailable.
def load_local_catalog() -> Tuple[Optional[List[Dict[str, Any]]], Optional[List[str]]]:
    """Return the local COMMAND_CATALOG, or (None, None) if the executor pkg isn't importable."""
    try:
        from executor.command_catalog import COMMAND_CATALOG, CATEGORY_ORDER
        return COMMAND_CATALOG, CATEGORY_ORDER
    except Exception:
        return None, None


# Filters the catalog for compliance with the allowed-tools list, then sorts it.
# In: catalog, allowed_tools set, terminal_only bool → Out: filtered+sorted entries.
def visible_commands(catalog: List[Dict[str, Any]],
                     allowed_tools: Optional[Set[str]] = None,
                     terminal_only: bool = False,
                     order: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Keep entries whose tool is allowed (empty allow-set = unrestricted); sort by category.

    A client-side entry (no tools, e.g. fetch/bundle) is always kept. When
    ``terminal_only`` is set, AI-only capabilities (``terminal == False``) are dropped.
    """
    order = order or _DEFAULT_ORDER
    allowed = allowed_tools or set()

    def _ok(entry: Dict[str, Any]) -> bool:
        if terminal_only and entry.get("terminal", True) is False:
            return False
        tools = entry.get("tools") or []
        if not tools:
            return True                       # client-side op — always available
        if not allowed:
            return True                       # unrestricted
        return any(t in allowed for t in tools)

    kept = [e for e in catalog if _ok(e)]
    kept.sort(key=lambda e: (order.index(e["category"]) if e["category"] in order else len(order),
                             e.get("command") or "~"))
    return kept


# Escapes Rich markup brackets so "[vm|all]" renders literally.
def _esc(text: str) -> str:
    return text.replace("[", r"\[")


# Renders the terminal help body (Rich markup) grouped by category.
# In: catalog, allowed_tools → Out: markup string for a Panel.
def render_terminal_panel(catalog: List[Dict[str, Any]],
                          allowed_tools: Optional[Set[str]] = None,
                          order: Optional[List[str]] = None) -> str:
    """Build the grouped `gorgon help` body: command + args + description, allowed-filtered."""
    entries = visible_commands(catalog, allowed_tools, terminal_only=True, order=order)
    lines = ["[bold]gorgon — direct QEMU commands (no AI)[/bold]\n"]
    current = None
    for e in entries:
        if e["category"] != current:
            current = e["category"]
            suffix = " [dim](stealth VMs only)[/dim]" if current == "Stealth" else ""
            lines.append(f"\n[bold cyan]{current}[/bold cyan]{suffix}")
        verb = e["command"] + (" " + e["args"] if e["args"] else "")
        lines.append(f"  {_esc(verb):<30} {e['desc']}")
    lines.append("\n[dim]Create or manage VMs conversationally: run "
                 "[bold]gorgon[/bold] with no arguments (AI chat).[/dim]")
    return "\n".join(lines)
