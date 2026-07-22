"""
shared/config — Configuration for the cross-role shared utilities (loader + data).

These are the settings used by the modules under ``shared/`` — the ones every
role (client, admin, orchestrator, executor) reaches for: agent selection, the
audit log, the signing key, the display theme, and the local-server spawn/stop
plumbing that `gorgon agent load` and the admin TUI share.

  shared_config.defaults.json  — the single manifest of every setting + default
  shared_config.json           — the user's overrides (win on merge)

Like the admin/client loaders, this file holds no literal setting values of its
own — it merges the two JSON files and exposes each as a named constant, so code
refers to `config.PGREP_PATTERN` rather than a magic string. Three settings have
a long-standing environment override (`GORGON_PORT`, `GORGON_SERVER_LOG`,
`GORGON_AGENT`); those still win over the JSON so existing deployments don't
change behaviour.
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))            # …/files/shared/config
FILES_DIR = os.path.dirname(os.path.dirname(_HERE))           # …/files (the PYTHONPATH root)


def load_json(path: str) -> dict:
    """Load a JSON file, returning an empty dict on any error."""
    try:
        return json.load(open(path))
    except Exception:
        return {}


_DEFAULTS  = load_json(os.path.join(_HERE, "shared_config.defaults.json"))
_OVERRIDES = load_json(os.path.join(_HERE, "shared_config.json"))
_CFG       = {**_DEFAULTS, **_OVERRIDES}   # user overrides win


def _c(key: str):
    """Fetch a merged setting; KeyError if the defaults manifest is missing it."""
    return _CFG[key]


# ── agent selection + bundles ───────────────────────────────────────────────────
AGENT_SELECTION_FILE = os.path.expanduser(_c("agent_selection_file"))
DEFAULT_AGENT        = _c("default_agent")
AGENT_ENV_VAR        = _c("agent_env_var")
AGENTS_DIR           = os.path.expanduser(_c("agents_dir"))   # ~/.qemu_vms/_agents — bundle root

# ── on-disk secrets / logs ──────────────────────────────────────────────────────
AUDIT_LOG_FILE   = os.path.expanduser(_c("audit_log_file"))
SIGNING_KEY_FILE = os.path.expanduser(_c("signing_key_file"))
TOKEN_FILE       = os.path.expanduser(_c("token_file"))

# ── local-server spawn / stop (env wins over JSON, as it always has) ────────────
SERVER_HOST      = _c("server_host")
SERVER_PORT      = int(os.environ.get(_c("server_port_env_var"), _c("server_port")))
UVICORN_APP      = _c("uvicorn_app")
SERVER_LOG_LEVEL = _c("server_log_level")
SERVER_LOG_PATH  = os.environ.get(_c("server_log_env_var"), _c("server_log_path"))
PGREP_PATTERN    = _c("pgrep_pattern")
STARTUP_WAIT_S          = _c("startup_wait_s")
STARTUP_POLL_INTERVAL_S = _c("startup_poll_interval_s")
STOP_TIMEOUT_S          = _c("stop_timeout_s")
STOP_POLL_INTERVAL_S    = _c("stop_poll_interval_s")

# ── display theme (rich) ────────────────────────────────────────────────────────
# Lives here, not in orchestrator/ai/config.json: it is a presentation concern and
# shared/display.py is its only reader.
THEME = _c("theme")
