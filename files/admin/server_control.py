"""
admin/server_control.py — Local orchestrator process control.

Only useful when the admin TUI runs ON the orchestrator machine: find, spawn,
stop, and restart the local server process. All timing/host/port knobs come
from admin.config.
"""

import os
import subprocess
import sys
import time

from admin import config


def local_pid() -> "int | None":
    """Return the PID of a locally running server process, or None."""
    try:
        out = subprocess.check_output(["pgrep", "-f", config.PGREP_PATTERN], text=True).strip()
        pids = [int(p) for p in out.splitlines() if p.strip()]
        return pids[0] if pids else None
    except Exception:
        return None


def spawn_server() -> "int | None":
    """Spawn the orchestrator server detached (the start-server body, shared with
    the restart verb). Returns the new PID, or None if it didn't come up."""
    env = os.environ.copy()
    env["PYTHONPATH"] = config.FILES_DIR
    try:
        with open(os.path.expanduser("~/.gorgon.token")) as f:
            env["API_TOKEN"] = f.read().strip()
    except Exception:
        pass  # no token file — run without an API token (localhost only)
    with open(config.LOG_PATH, "w") as log_fh:
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", config.UVICORN_APP,
             "--host", config.SPAWN_HOST, "--port", str(config.DEFAULT_PORT),
             "--log-level", config.SPAWN_LOG_LEVEL],
            cwd=config.FILES_DIR, env=env, start_new_session=True,
            stdout=log_fh, stderr=subprocess.STDOUT,
        )
    for _ in range(config.STARTUP_WAIT_TICKS):   # wait for it to come up
        time.sleep(config.STARTUP_WAIT_INTERVAL_S)
        pid = local_pid()
        if pid:
            return pid
    return None
