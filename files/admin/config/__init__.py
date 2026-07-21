"""
admin/config — Configuration for the admin TUI (loader + data files).

This package holds every config file for the admin TUI alongside the loader:
  admin_config.defaults.json  — the single manifest of every setting + default
  admin_config.json           — the user's overrides (win on merge)
  connection_config.json      — orchestrator URL + token (written by install_admin.sh)

The loader below contains no literal setting values of its own — it just merges
the two admin_config files and exposes each value as a named constant, so code
refers to `config.HTTP_TIMEOUT_S` rather than a magic number and there's one
folder to read to see everything the TUI can be configured with.
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))            # …/files/admin/config
FILES_DIR = os.path.dirname(os.path.dirname(_HERE))           # …/files (the PYTHONPATH root)


def load_json(path: str) -> dict:
    """Load a JSON file, returning an empty dict on any error."""
    try:
        return json.load(open(path))
    except Exception:
        return {}


_DEFAULTS  = load_json(os.path.join(_HERE, "admin_config.defaults.json"))
_OVERRIDES = load_json(os.path.join(_HERE, "admin_config.json"))
_CONN_CFG  = load_json(os.path.join(_HERE, "connection_config.json"))
_CFG       = {**_DEFAULTS, **_OVERRIDES}   # user overrides win


def _c(key: str):
    """Fetch a merged setting; KeyError if the defaults manifest is missing it."""
    return _CFG[key]


# ── connection ─────────────────────────────────────────────────────────────────
ORCH_URL = os.environ.get("SERVER_URL", _CONN_CFG.get("orchestrator_url", _c("default_orchestrator_url")))
TOKEN_FILE = _c("token_file")


def token() -> str:
    """Return the admin API token from the environment, config, or token file."""
    t = os.environ.get("API_TOKEN") or _CONN_CFG.get("token", "")
    if t:
        return t
    try:
        with open(os.path.expanduser(TOKEN_FILE)) as f:
            return f.read().strip()
    except Exception:
        return ""


# ── general ────────────────────────────────────────────────────────────────────
TEXT_COLOR   = _c("text_color")
FONT_SIZE    = _c("font_size")
FONT_FAMILY  = _c("font_family")
REFRESH_S    = _c("refresh_rate_s")
DEFAULT_PORT = _c("default_port")
LOG_PATH     = _c("log_path")
EVENTS_LIMIT = _c("events_display_limit")

# ── protocol (endpoints, tool names, process targets) ──────────────────────────
EXECUTE_PATH  = _c("execute_path")
HEALTH_PATH   = _c("health_path")
EVENTS_PATH   = _c("events_path")
TOOL_STOP     = _c("tool_stop")
TOOL_LAUNCH   = _c("tool_launch")
TOOL_LIST     = _c("tool_list")
UVICORN_APP   = _c("uvicorn_app")
PGREP_PATTERN = _c("pgrep_pattern")

# ── network ────────────────────────────────────────────────────────────────────
HTTP_TIMEOUT_S   = _c("http_timeout_s")
HEALTH_TIMEOUT_S = _c("health_timeout_s")
HEALTH_CACHE_S   = _c("health_cache_s")

# ── server spawn / restart ─────────────────────────────────────────────────────
SPAWN_HOST              = _c("spawn_host")
SPAWN_LOG_LEVEL         = _c("spawn_log_level")
STARTUP_WAIT_TICKS      = _c("startup_wait_ticks")
STARTUP_WAIT_INTERVAL_S = _c("startup_wait_interval_s")
RESTART_GRACE_S         = _c("restart_grace_s")
RESTART_POLL_INTERVAL_S = _c("restart_poll_interval_s")
RESTART_KILL_WAIT_S     = _c("restart_kill_wait_s")

# ── terminal ───────────────────────────────────────────────────────────────────
TERM_ROWS        = _c("term_rows")
TERM_COLS        = _c("term_cols")
STARTUP_DELAY_S  = _c("startup_delay_s")
FRAME_INTERVAL_S = _c("frame_interval_s")

# ── colours ────────────────────────────────────────────────────────────────────
COLOR_HEADER_FG    = _c("color_header_fg")
COLOR_HEADER_BG    = _c("color_header_bg")
COLOR_CYAN         = _c("color_cyan")
COLOR_GREEN        = _c("color_green")
COLOR_RED          = _c("color_red")
COLOR_YELLOW       = _c("color_yellow")
DIM_FALLBACK_SLOT  = _c("dim_fallback_slot")
CUSTOM_COLOR_SLOT  = _c("custom_color_slot")
COLOR_FALLBACK_RGB = tuple(_c("color_fallback_rgb"))

# ── UI strings ─────────────────────────────────────────────────────────────────
APP_TITLE      = _c("app_title")
GLYPH_RUNNING  = _c("glyph_running")
GLYPH_STOPPED  = _c("glyph_stopped")
RAM_UNIT       = _c("ram_unit")
SECTION_VMS    = _c("section_vms")
SECTION_EVENTS = _c("section_events")
HELP_TITLE     = _c("help_title")
HELP_FOOTER    = _c("help_footer")
HINT_LINE      = _c("hint_line")
LABEL_VM       = _c("label_vm")
LABEL_STATUS   = _c("label_status")
LABEL_CPU      = _c("label_cpu")
LABEL_RAM      = _c("label_ram")
LABEL_OS       = _c("label_os")
LABEL_TIME     = _c("label_time")
LABEL_TOOL     = _c("label_tool")
LABEL_TARGET   = _c("label_target")
LABEL_RESULT   = _c("label_result")
LABEL_MS       = _c("label_ms")
HELP_SECTIONS  = _c("help_sections")

# ── dashboard layout ───────────────────────────────────────────────────────────
BODY_RESERVE_ROWS        = _c("body_reserve_rows")
VM_ROW_DIVISOR           = _c("vm_row_divisor")
VM_ROWS_BOTTOM_MARGIN    = _c("vm_rows_bottom_margin")
EVENT_ROWS_BOTTOM_MARGIN = _c("event_rows_bottom_margin")
PROMPT_ROW_FROM_BOTTOM   = _c("prompt_row_from_bottom")
HINT_ROW_FROM_BOTTOM     = _c("hint_row_from_bottom")
SEPARATOR_ROW_FROM_BOTTOM = _c("separator_row_from_bottom")

VM_NAME_X           = _c("vm_name_x")
VM_STATUS_X         = _c("vm_status_x")
VM_META_X           = _c("vm_meta_x")
VM_NAME_WIDTH       = _c("vm_name_width")
VM_STATUS_HDR_WIDTH = _c("vm_status_hdr_width")
VM_STATUS_WIDTH     = _c("vm_status_width")
VM_CPU_WIDTH        = _c("vm_cpu_width")
VM_RAM_WIDTH        = _c("vm_ram_width")
VM_NAME_TRUNC       = _c("vm_name_trunc")
VM_OS_TRUNC         = _c("vm_os_trunc")

EV_NAME_X        = _c("ev_name_x")
EV_RESULT_X      = _c("ev_result_x")
EV_MS_X          = _c("ev_ms_x")
EV_TS_WIDTH      = _c("ev_ts_width")
EV_TOOL_WIDTH    = _c("ev_tool_width")
EV_TARGET_WIDTH  = _c("ev_target_width")
EV_RESULT_WIDTH  = _c("ev_result_width")
EV_MS_WIDTH      = _c("ev_ms_width")
EV_TS_TRUNC      = _c("ev_ts_trunc")
EV_TARGET_TRUNC  = _c("ev_target_trunc")
EV_TOOL_TRUNC    = _c("ev_tool_trunc")
EV_OUTCOME_TRUNC = _c("ev_outcome_trunc")

HELP_BOX_MAX_W      = _c("help_box_max_w")
HELP_BOX_W_MARGIN   = _c("help_box_w_margin")
HELP_BOX_H_MARGIN   = _c("help_box_h_margin")
HELP_TOTAL_ROWS_PAD = _c("help_total_rows_pad")
HELP_TITLE_X        = _c("help_title_x")
HELP_CMD_X          = _c("help_cmd_x")
HELP_DESC_X         = _c("help_desc_x")
HELP_CMD_WIDTH      = _c("help_cmd_width")
HELP_DESC_X_PAD     = _c("help_desc_x_pad")
