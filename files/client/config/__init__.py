"""
client/config — Configuration for the gorgon client (loader + data files).

This package holds every config file for the client alongside the loader:
  CLI_config.defaults.json         — appearance/behaviour defaults manifest
  CLI_config.json                  — appearance overrides (text_color, font_size)
  connection_config.defaults.json  — connection defaults manifest
  connection_config.json           — connection overrides (written by setup_client.sh)

Each *.json pair is merged (overrides win). Environment variables still win over
both, preserving the previous behaviour (SERVER_URL / API_TOKEN / API_TIMEOUT /
API_CA_CERT / API_VERIFY_SSL). The connection derivation used to be copy-pasted
in cli/commands.py, cli/commands_helpers.py and ui/chat_client.py — it lives here
now, in one place.
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))            # …/files/client/config
FILES_DIR = os.path.dirname(os.path.dirname(_HERE))           # …/files (the PYTHONPATH root)


def load_json(path: str) -> dict:
    """Load a JSON file, returning an empty dict on any error."""
    try:
        return json.load(open(path))
    except Exception:
        return {}


def _merged(defaults_name: str, override_name: str) -> dict:
    """defaults overlaid by user overrides (overrides win)."""
    return {**load_json(os.path.join(_HERE, defaults_name)),
            **load_json(os.path.join(_HERE, override_name))}


_UI   = _merged("CLI_config.defaults.json", "CLI_config.json")
_CONN = _merged("connection_config.defaults.json", "connection_config.json")


# ── connection (environment variables win over the config files) ────────────────
SERVER  = os.environ.get("SERVER_URL", _CONN["server_url"])
TOKEN   = os.environ.get("API_TOKEN",  _CONN["token"])
TIMEOUT = int(os.environ.get("API_TIMEOUT", _CONN["timeout"]))
CA_CERT = os.environ.get("API_CA_CERT", _CONN.get("ca_cert") or None)
VERIFY  = False if os.environ.get("API_VERIFY_SSL", "1") == "0" else (CA_CERT or _CONN["verify_ssl"])
HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
VNC_VIEWERS = _CONN["vnc_viewer_candidates"]
IO_CHUNK    = _CONN["io_chunk_bytes"]

# ── appearance / behaviour ──────────────────────────────────────────────────────
TEXT_COLOR                = _UI["text_color"]
FONT_SIZE                 = int(_UI["font_size"])
WRAP_WIDTH                = _UI["wrap_width"]
TERM_ROWS                 = _UI["terminal_rows"]
TERM_COLS                 = _UI["terminal_cols"]
AUTOSTART_POLL_COUNT      = _UI["autostart_poll_count"]
AUTOSTART_POLL_INTERVAL_S = _UI["autostart_poll_interval_s"]
LOG_PATH                  = _UI["log_path"]
