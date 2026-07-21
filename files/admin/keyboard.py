"""
admin/keyboard.py — Keypress handling for the admin TUI.

One keypress → an update to the command buffer / help mode, or a spawned
dispatch thread on Enter.
"""

import curses
import threading

from admin import commands, state


def handle_key(ch) -> None:
    """Apply one keypress to the command buffer / help mode.

    Called only from the main loop in app.run() — never spawn this on a
    background thread. curses/ncurses isn't safe to call concurrently from
    multiple threads against the same window; a prior version read keys on a
    separate thread while run() drew from the main thread, and the two
    unsynchronized streams of curses calls caused visible corruption — most
    noticeable in the help overlay, which repaints a fresh window every tick.
    dispatch() itself still runs on its own thread (it may block on a
    network call) but it never touches curses, only shared Python state
    under state.lock, so that stays safe.
    """
    # A "real" keypress — as opposed to a non-key curses event (KEY_RESIZE, etc.)
    # that also flows through get_wch(). Only these should be able to dismiss
    # help; treating literally any event as "close" let a stray non-key event
    # slam it shut immediately after opening.
    is_real_key = isinstance(ch, str) or ch in (curses.KEY_ENTER, curses.KEY_BACKSPACE)
    with state.lock:
        if state.help_mode:
            if is_real_key:
                state.help_mode = False
        elif ch in (3, "\x03", 27, "\x1b"):
            state.quit_event.set()
        elif ch == "q" and not state.cmd_buf:
            state.quit_event.set()
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            cmd = state.cmd_buf.strip()
            state.cmd_buf = ""
            threading.Thread(target=commands.dispatch, args=(cmd,), daemon=True).start()
        elif ch in (curses.KEY_BACKSPACE, "\x7f", 8):
            state.cmd_buf = state.cmd_buf[:-1]
            state.cmd_msg = ""
        elif isinstance(ch, str) and ch.isprintable():
            state.cmd_buf += ch
            state.cmd_msg = ""
