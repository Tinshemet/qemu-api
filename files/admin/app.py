"""
admin/app.py — The admin TUI main loop.

Ties the pieces together: sets up the terminal, then polls server state, reads
input, and redraws on a fixed frame interval until the user quits. All curses
calls happen on this single thread (see keyboard.handle_key for why).
"""

import curses
import sys
import time

from admin import api_client, config, keyboard, render, state


def run(stdscr: "curses.window") -> None:
    """curses main loop — poll server state, read input, and draw.

    All curses calls (get_wch + erase/addstr/refresh) happen from this one
    thread. See keyboard.handle_key() for why: reading input on a separate
    thread while this loop draws is what caused the visible corruption/flicker.
    """
    sys.stdout.write(f"\033]50;xft:{config.FONT_FAMILY}:size={int(config.FONT_SIZE)}\007")
    sys.stdout.write(f"\033[8;{config.TERM_ROWS};{config.TERM_COLS}t")
    sys.stdout.flush()
    time.sleep(config.STARTUP_DELAY_S)

    curses.curs_set(0)
    curses.nonl()
    stdscr.nodelay(True)
    render.init_colours(config.TEXT_COLOR)
    curses.resizeterm(*stdscr.getmaxyx())

    start = time.monotonic()

    last_fetch = 0.0
    vms, events = [], []

    while not state.quit_event.is_set():
        # Drain every buffered key immediately (nodelay(True) never blocks) —
        # relying on a curses read timeout to pace the loop made the redraw
        # rate depend on terminal/curses-build timing behavior, which made
        # the flicker worse. The explicit sleep below is what paces frames.
        while True:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                break
            keyboard.handle_key(ch)

        now = time.monotonic()
        if now - last_fetch >= config.REFRESH_S:
            try:
                vms = api_client.vm_list(api_client.exec_tool("list_vms", log=False))
            except Exception:
                vms = []
            events     = api_client.get_events(limit=config.EVENTS_LIMIT)
            last_fetch = now

        render.draw(stdscr, vms, events, now - start)
        time.sleep(config.FRAME_INTERVAL_S)


def main() -> None:
    """Entry point — run the admin TUI until the user exits."""
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass  # Ctrl-C — exit the admin TUI cleanly
