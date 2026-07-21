"""
admin/render.py — curses colours and dashboard drawing for the admin TUI.

Owns the colour-pair setup and every draw routine (header, VM table, event
table, input line, and the help overlay). All geometry, labels, glyphs, the
colour palette, and the help-overlay content come from admin.config (i.e.
admin_config.*.json), so appearance is tuned in JSON, not here. The only
literals kept in code are pure arithmetic (curses' 0–1000 colour scale, the
0–255 byte range, MB→GB) and screen-relative math (w-1, box_h-2, …).
"""

import curses

from admin import api_client, config, state

# Colour-pair identifiers — symbolic slots, not tunables (their numbers are just
# unique ids handed to curses.init_pair / color_pair).
C_NORMAL = 0
C_HEADER = 1
C_CYAN   = 2
C_GREEN  = 3
C_RED    = 4
C_DIM    = 5
C_YELLOW = 6


def _curses_color(name: str) -> int:
    """Resolve a curses colour NAME (e.g. "BLUE") to its curses.COLOR_* value."""
    return getattr(curses, f"COLOR_{name}")


def _hex_to_curses(hex_color: str) -> tuple:
    """Convert a #RRGGBB hex colour to a curses 0-1000 RGB triple."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return config.COLOR_FALLBACK_RGB
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)


def init_colours(color_hex: str = config.TEXT_COLOR) -> None:
    """Initialise curses colour pairs from the configured palette + accent hex."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_HEADER, _curses_color(config.COLOR_HEADER_FG), _curses_color(config.COLOR_HEADER_BG))
    curses.init_pair(C_CYAN,   _curses_color(config.COLOR_CYAN),   -1)
    curses.init_pair(C_GREEN,  _curses_color(config.COLOR_GREEN),  -1)
    curses.init_pair(C_RED,    _curses_color(config.COLOR_RED),    -1)
    curses.init_pair(C_YELLOW, _curses_color(config.COLOR_YELLOW), -1)
    if curses.can_change_color():
        r, g, b = _hex_to_curses(color_hex)
        curses.init_color(config.CUSTOM_COLOR_SLOT, r, g, b)
        curses.init_pair(C_DIM, config.CUSTOM_COLOR_SLOT, -1)
    else:
        curses.init_pair(C_DIM, config.DIM_FALLBACK_SLOT, -1)


def _cp(n: int) -> int:
    """Return the curses attribute for colour-pair number ``n``."""
    return curses.color_pair(n)


# ── draw ──────────────────────────────────────────────────────────────────────

def _hline(stdscr: "curses.window", row: int, w: int, label: str = "") -> None:
    """Draw a horizontal rule at ``row``, optionally labelled."""
    if row < 0:
        return
    try:
        if label:
            left  = max(0, (w - len(label) - 2) // 2)
            right = max(0, w - left - len(label) - 2)
            stdscr.addstr(row, 0,    "─" * left,                   _cp(C_DIM))
            stdscr.addstr(row, left, f" {label} ",                  _cp(C_CYAN) | curses.A_BOLD)
            stdscr.addstr(row, left + len(label) + 2, "─" * right, _cp(C_DIM))
        else:
            stdscr.addstr(row, 0, "─" * (w - 1), _cp(C_DIM))
    except curses.error:
        pass  # addstr past the screen edge — skip the section rule


def draw(stdscr: "curses.window", vms: list, events: list, uptime_s: float) -> None:
    """Redraw the dashboard — header, VM table, event table, and input line.

    stdscr.erase() only runs in the dashboard branch below, never while help
    is showing. Confirmed with a pty + raw-byte capture: erasing stdscr every
    tick even though it's never refreshed while help is open caused curses to
    emit a stray full-screen clear (`ESC[H ESC[J`) roughly one tick after the
    help box was drawn, wiping it with nothing to replace it. help_mode itself
    was never the problem — it stayed True throughout, confirmed by tracing.
    """
    h, w = stdscr.getmaxyx()

    if state.help_mode:
        _draw_help(stdscr, h, w)
        return

    reset_help_cache()
    stdscr.erase()

    uptime  = f"{int(uptime_s//3600):02d}:{int((uptime_s%3600)//60):02d}:{int(uptime_s%60):02d}"
    online  = api_client.server_online()
    srv_str = f"online @ {config.ORCH_URL}" if online else f"unreachable @ {config.ORCH_URL}"
    hdr     = (f"  {config.APP_TITLE}   uptime {uptime}   vms {len(vms)}   "
               f"events {len(events)}   {srv_str}  ")
    try:
        stdscr.addstr(0, 0, hdr.ljust(w - 1), _cp(C_HEADER) | curses.A_BOLD)
    except curses.error:
        pass  # addstr past the screen edge — skip the header

    body_h  = h - config.BODY_RESERVE_ROWS
    max_vms = min(len(vms), max(1, body_h // config.VM_ROW_DIVISOR))
    max_evs = max(1, body_h - max_vms)

    row = 1
    _hline(stdscr, row, w, config.SECTION_VMS); row += 1

    cols_vm = (f"  {config.LABEL_VM:<{config.VM_NAME_WIDTH}} "
               f"{config.LABEL_STATUS:<{config.VM_STATUS_HDR_WIDTH}} "
               f"{config.LABEL_CPU:>{config.VM_CPU_WIDTH}}  "
               f"{config.LABEL_RAM:>{config.VM_RAM_WIDTH}}  {config.LABEL_OS}")
    try:
        stdscr.addstr(row, 0, cols_vm[:w-1], _cp(C_CYAN) | curses.A_BOLD)
    except curses.error:
        pass  # addstr past the screen edge — skip the VM column header
    row += 1
    try:
        stdscr.addstr(row, 0, "  " + "─" * (w - 3), _cp(C_DIM))
    except curses.error:
        pass  # addstr past the screen edge — skip the rule
    row += 1

    for v in vms[:max_vms]:
        if row >= h - config.VM_ROWS_BOTTOM_MARGIN:
            break
        status = v.get("status", "?")
        dot    = config.GLYPH_RUNNING if status == "running" else config.GLYPH_STOPPED
        color  = _cp(C_GREEN) if status == "running" else _cp(C_DIM)
        name   = v.get("name", "")[:config.VM_NAME_TRUNC]
        os_s   = v.get("os", "")[:config.VM_OS_TRUNC]
        ram    = f"{v.get('memory_mb', 0) // 1024}{config.RAM_UNIT}"
        cpu    = str(v.get("cpu_cores", ""))
        try:
            stdscr.addstr(row, config.VM_NAME_X,   f"{name:<{config.VM_NAME_WIDTH}} ", _cp(C_NORMAL))
            stdscr.addstr(row, config.VM_STATUS_X, f"{dot}{status:<{config.VM_STATUS_WIDTH}}", color)
            stdscr.addstr(row, config.VM_META_X,
                          f"{cpu:>{config.VM_CPU_WIDTH}}  {ram:>{config.VM_RAM_WIDTH}}  {os_s}", _cp(C_NORMAL))
        except curses.error:
            pass  # addstr past the screen edge — skip this VM row
        row += 1

    _hline(stdscr, row, w, config.SECTION_EVENTS); row += 1

    cols_ev = (f"  {config.LABEL_TIME:<{config.EV_TS_WIDTH}} "
               f"{config.LABEL_TOOL:<{config.EV_TOOL_WIDTH}} "
               f"{config.LABEL_TARGET:<{config.EV_TARGET_WIDTH}} "
               f"{config.LABEL_RESULT:<{config.EV_RESULT_WIDTH}} "
               f"{config.LABEL_MS:>{config.EV_MS_WIDTH}}")
    try:
        stdscr.addstr(row, 0, cols_ev[:w-1], _cp(C_CYAN) | curses.A_BOLD)
    except curses.error:
        pass  # addstr past the screen edge — skip the events column header
    row += 1
    try:
        stdscr.addstr(row, 0, "  " + "─" * (w - 3), _cp(C_DIM))
    except curses.error:
        pass  # addstr past the screen edge — skip the rule
    row += 1

    for e in events[-max_evs:]:
        if row >= h - config.EVENT_ROWS_BOTTOM_MARGIN:
            break
        ts        = e.get("ts", "")[:config.EV_TS_TRUNC].replace("T", " ")
        outcome   = e.get("outcome", "")
        args      = e.get("args", {})
        target    = (args.get("name") or args.get("profile") or "")[:config.EV_TARGET_TRUNC]
        ms        = str(int(e.get("duration_ms", 0)))
        tool      = e.get("tool", "")[:config.EV_TOOL_TRUNC]
        outcome_s = outcome[:config.EV_OUTCOME_TRUNC]
        res_color = (_cp(C_GREEN)  if outcome == "ok"
                     else _cp(C_YELLOW) if outcome == "already_running"
                     else _cp(C_RED))
        try:
            stdscr.addstr(row, config.EV_NAME_X,
                          f"{ts:<{config.EV_TS_WIDTH}} {tool:<{config.EV_TOOL_WIDTH}} "
                          f"{target:<{config.EV_TARGET_WIDTH}} ", _cp(C_NORMAL))
            stdscr.addstr(row, config.EV_RESULT_X, f"{outcome_s:<{config.EV_RESULT_WIDTH}}", res_color)
            stdscr.addstr(row, config.EV_MS_X,     f"{ms:>{config.EV_MS_WIDTH}}", _cp(C_DIM))
        except curses.error:
            pass  # addstr past the screen edge — skip this event row
        row += 1

    _hline(stdscr, h - config.SEPARATOR_ROW_FROM_BOTTOM, w)
    with state.lock:
        prompt = f" > {state.cmd_buf}"
        msg    = f"   {state.cmd_msg}" if state.cmd_msg else ""
    try:
        stdscr.addstr(h - config.PROMPT_ROW_FROM_BOTTOM, 0, prompt[:w-1], _cp(C_CYAN) | curses.A_BOLD)
        if msg:
            p_end = min(len(prompt), w - 2)
            stdscr.addstr(h - config.PROMPT_ROW_FROM_BOTTOM, p_end, msg[:w - p_end - 1], _cp(C_DIM))
    except curses.error:
        pass  # addstr past the screen edge — skip the prompt line
    try:
        stdscr.addstr(h - config.HINT_ROW_FROM_BOTTOM, 0, config.HINT_LINE[:w-1], _cp(C_DIM))
    except curses.error:
        pass  # addstr past the screen edge — skip the hint line

    stdscr.refresh()


_help_win_key = None


def reset_help_cache() -> None:
    """Force the next help open to redraw fresh (call whenever the dashboard,
    not help, is on screen — the physical screen has since been overwritten)."""
    global _help_win_key
    _help_win_key = None


def _allowed_tools() -> "set | None":
    """Return the executor's allowed-tools set for help filtering, or None if unrestricted.

    Mirrors client/cli/commands.py's _allowed_tools() exactly — same direct-import approach,
    same graceful fallback. Dispatch already enforces this allowlist for free (admin's exec_tool()
    goes through /execute, which executor_client.execute_tool() gates); this is purely about
    making the help overlay stop listing commands that would actually be rejected.
    """
    try:
        from orchestrator.executor_client import _ALLOWED_TOOLS
        return set(_ALLOWED_TOOLS) or None
    except Exception:
        return None


def _draw_help(stdscr: "curses.window", h: int, w: int) -> None:
    """Draw the help overlay listing the admin commands (content from config.HELP_SECTIONS).

    Reuses the same curses window across frames instead of creating a fresh
    one every ~50ms tick. Measured with a pty harness: recreating the window
    every tick sent ~120KB/3s of terminal traffic for fully static content
    (~27x the dashboard's idle traffic, which reuses stdscr and lets curses'
    diffing correctly send almost nothing for an unchanged frame). That write
    volume, not a rendering-technique guess, was the confirmed cause of the
    help-only flicker.
    """
    global _help_win_key
    key = (h, w)
    if _help_win_key == key:
        return  # already drawn at this size — nothing has changed
    _help_win_key = key

    # Each help entry's "tools" is the tool(s) required to actually run the command, or
    # null for admin-only actions (server control, navigation) that never go through
    # /execute and so are never allowlist-gated. Dispatch already enforces the allowlist
    # (admin's exec_tool() goes through /execute -> executor_client.execute_tool()) — this
    # filtering only keeps the help text honest about what would actually work.
    allowed = _allowed_tools()

    def _cmd_visible(tools) -> bool:
        return not tools or allowed is None or any(t in allowed for t in tools)

    SECTIONS = [
        (sec["title"], [(c["label"], c["text"]) for c in sec["commands"] if _cmd_visible(c.get("tools"))])
        for sec in config.HELP_SECTIONS
    ]
    SECTIONS = [(section, cmds) for section, cmds in SECTIONS if cmds]

    total_rows = sum(1 + len(cmds) for _, cmds in SECTIONS) + len(SECTIONS) + config.HELP_TOTAL_ROWS_PAD
    box_h = min(total_rows + 2, h - config.HELP_BOX_H_MARGIN)
    box_w = min(config.HELP_BOX_MAX_W, w - config.HELP_BOX_W_MARGIN)
    y     = max(0, (h - box_h) // 2)
    x     = max(0, (w - box_w) // 2)

    try:
        win = curses.newwin(box_h, box_w, y, x)
        win.border()
        win.addstr(0, config.HELP_TITLE_X, f" {config.HELP_TITLE} ", _cp(C_CYAN) | curses.A_BOLD)
        row = 1
        for section, cmds in SECTIONS:
            if row >= box_h - 2:
                break
            win.addstr(row, config.HELP_TITLE_X, section, _cp(C_CYAN) | curses.A_BOLD)
            row += 1
            for cmd, desc in cmds:
                if row >= box_h - 2:
                    break
                win.addstr(row, config.HELP_CMD_X, f"{cmd:<{config.HELP_CMD_WIDTH}}", curses.A_BOLD)
                win.addstr(row, config.HELP_DESC_X, desc[:box_w - config.HELP_DESC_X_PAD], _cp(C_DIM))
                row += 1
            row += 1
        if row < box_h - 1:
            win.addstr(box_h - 1, config.HELP_TITLE_X, config.HELP_FOOTER, _cp(C_DIM))
        win.refresh()
    except curses.error:
        pass  # addstr past the popup edge — stop drawing the box
