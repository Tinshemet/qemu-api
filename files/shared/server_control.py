"""server_control.py — start / stop / restart the local orchestrator server.

One definition of "how the local server process is managed" (find it with
``pgrep api_server`` · SIGTERM to stop · respawn detached uvicorn to start), so
`gorgon agent load` doesn't reinvent it. Mirrors the proven admin-TUI pattern.

Restarting is a HIGH-IMPACT action — only `gorgon agent load` uses it, and only
after operator re-authentication. The respawned server re-imports contract.py
fresh, so it picks up whatever agent_select points at: that's how load swaps
the active agent.
"""
import os
import signal
import subprocess
import sys
import time
from typing import Optional

_PORT     = int(os.environ.get("GORGON_PORT", "8080"))
_LOG_PATH = os.environ.get("GORGON_SERVER_LOG", "/tmp/gorgon-orchestrator.log")


def _files_dir() -> str:
    """The repo `files/` dir — this module is files/shared/server_control.py."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def local_pid() -> Optional[int]:
    """PID of a locally running orchestrator server, or None."""
    try:
        out = subprocess.check_output(["pgrep", "-f", "api_server"], text=True).strip()
        pids = [int(p) for p in out.splitlines() if p.strip()]
        return pids[0] if pids else None
    except Exception:
        return None


def stop_server(timeout: float = 5.0) -> bool:
    """SIGTERM the local server and wait for it to exit (SIGKILL as a last resort).
    Returns True if a server was found and stopped, False if none was running."""
    pid = local_pid()
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if local_pid() is None:
            return True
        time.sleep(0.2)
    leftover = local_pid()
    if leftover:
        try:
            os.kill(leftover, signal.SIGKILL)
        except Exception:
            pass
    return True


def start_server(wait: float = 8.0) -> Optional[int]:
    """Spawn the orchestrator server detached and wait for it to come up.
    Returns its PID, or None if it didn't start in time. No-op (returns the
    existing PID) if one is already running."""
    existing = local_pid()
    if existing:
        return existing
    files_dir = _files_dir()
    env = os.environ.copy()
    env["PYTHONPATH"] = files_dir
    try:
        with open(os.path.expanduser("~/.gorgon.token")) as f:
            env["API_TOKEN"] = f.read().strip()
    except Exception:
        pass  # no token file — start without an API token (localhost only)
    with open(_LOG_PATH, "w") as log_fh:
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "orchestrator.http.api_server:app",
             "--host", "0.0.0.0", "--port", str(_PORT), "--log-level", "warning"],
            cwd=files_dir, env=env, start_new_session=True,
            stdout=log_fh, stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        pid = local_pid()
        if pid:
            return pid
        time.sleep(0.3)
    return None


def restart_server() -> Optional[int]:
    """Stop the running server (if any) and start a fresh one. Returns the new PID."""
    stop_server()
    return start_server()
