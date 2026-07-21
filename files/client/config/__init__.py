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
HEALTH_TIMEOUT_S     = _CONN["health_timeout_s"]
REQUEST_TIMEOUT_S    = _CONN["request_timeout_s"]
IMAGE_META_TIMEOUT_S = _CONN["image_meta_timeout_s"]
DEFAULT_PORT = _CONN["default_port"]
VNC_VIEWERS  = _CONN["vnc_viewer_candidates"]
IO_CHUNK     = _CONN["io_chunk_bytes"]

# ── appearance / behaviour ──────────────────────────────────────────────────────
TEXT_COLOR                = _UI["text_color"]
FONT_SIZE                 = int(_UI["font_size"])
FONT_FAMILY               = _UI["font_family"]
WRAP_WIDTH                = _UI["wrap_width"]
TERM_ROWS                 = _UI["terminal_rows"]
TERM_COLS                 = _UI["terminal_cols"]
STARTUP_DELAY_S           = _UI["startup_delay_s"]
AUTOSTART_POLL_COUNT      = _UI["autostart_poll_count"]
AUTOSTART_POLL_INTERVAL_S = _UI["autostart_poll_interval_s"]
LOG_PATH                  = _UI["log_path"]
TOKEN_FILE                = _UI["token_file"]
SESSION_FILE              = _UI["session_file"]
VM_BASE_DIR               = _UI["vm_base_dir"]
DEFAULT_VNC_PORT          = _UI["default_vnc_port"]

# ── server autostart ────────────────────────────────────────────────────────────
SPAWN_HOST      = _UI["spawn_host"]
SPAWN_LOG_LEVEL = _UI["spawn_log_level"]
UVICORN_APP     = _UI["uvicorn_app"]

# ── colours ─────────────────────────────────────────────────────────────────────
COLOR_HEADER_FG    = _UI["color_header_fg"]
COLOR_HEADER_BG    = _UI["color_header_bg"]
COLOR_CYAN         = _UI["color_cyan"]
COLOR_GREEN        = _UI["color_green"]
COLOR_RED          = _UI["color_red"]
COLOR_YELLOW       = _UI["color_yellow"]
COLOR_BOLD         = _UI["color_bold"]
CUSTOM_COLOR_SLOT  = _UI["custom_color_slot"]
DIM_FALLBACK_SLOT  = _UI["dim_fallback_slot"]
COLOR_FALLBACK_RGB = tuple(_UI["color_fallback_rgb"])

# curses colour-pair slot ids (unique handles for init_pair / color_pair)
C_HEADER = _UI["color_pair_header"]
C_CYAN   = _UI["color_pair_cyan"]
C_GREEN  = _UI["color_pair_green"]
C_RED    = _UI["color_pair_red"]
C_DIM    = _UI["color_pair_dim"]
C_YELLOW = _UI["color_pair_yellow"]
C_BOLD   = _UI["color_pair_bold"]

# ── UI strings ──────────────────────────────────────────────────────────────────
GLYPH_RUNNING  = _UI["glyph_running"]
GLYPH_STOPPED  = _UI["glyph_stopped"]
SPINNER_FRAMES = _UI["spinner_frames"]
HINT_LINE      = _UI["hint_line"]

# ── chat shortcut command sets (defaults; overridable per-session from /sync) ────
SC_LIST      = set(_UI["shortcut_list"])
SC_SYSTEM    = set(_UI["shortcut_system"])
SC_PROFILES  = set(_UI["shortcut_profiles"])
SC_TEMPLATES = set(_UI["shortcut_templates"])
SC_DRIFT     = set(_UI["shortcut_drift"])
SC_CLEAR     = set(_UI["shortcut_clear"])
SC_HELP      = set(_UI["shortcut_help"])
EXIT_CMDS    = set(_UI["exit_commands"])
