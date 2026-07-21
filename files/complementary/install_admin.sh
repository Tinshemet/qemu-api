#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  install_admin.sh — gorgon Admin TUI setup
#
#  Installs the fullscreen admin dashboard. Run this on any machine that can
#  reach the orchestrator over HTTP — same machine, same LAN, or remote.
#
#  What this does:
#    1. Installs Python deps (requests, windows-curses on Windows)
#    2. Writes admin/config/connection_config.json with the orchestrator URL + token
#    3. Adds a  gorgon-admin  shell alias
#
#  Run as your normal user:
#    bash install_admin.sh
#
#  Override orchestrator URL / token via env:
#    SERVER_URL=http://192.168.1.10:8080 API_TOKEN=mytoken bash install_admin.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
FILES_DIR="$REPO_ROOT/files"
ADMIN_DIR="$FILES_DIR/admin"
CFG_FILE="$ADMIN_DIR/config/connection_config.json"
TOKEN_FILE="$HOME/.gorgon.token"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()     { echo -e "${GREEN}  ✓${RESET} $*"; }
info()   { echo -e "${CYAN}  →${RESET} $*"; }
warn()   { echo -e "${YELLOW}  ⚠${RESET} $*"; }
header() { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║   gorgon — Admin TUI installer             ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""

# ── Python deps ───────────────────────────────────────────────────────────────
header "Python Dependencies"
python3 -m pip install --quiet --upgrade requests
ok "requests installed"

# ── Orchestrator URL ──────────────────────────────────────────────────────────
header "Orchestrator Connection"

if [[ -n "${SERVER_URL:-}" ]]; then
    ORCH_URL="$SERVER_URL"
    ok "Using SERVER_URL from environment: $ORCH_URL"
else
    DEFAULT_URL="http://localhost:8080"
    read -rp "  Orchestrator URL [$DEFAULT_URL]: " ORCH_URL
    ORCH_URL="${ORCH_URL:-$DEFAULT_URL}"
fi

# ── Token ─────────────────────────────────────────────────────────────────────
if [[ -n "${API_TOKEN:-}" ]]; then
    TOKEN="$API_TOKEN"
    ok "Using API_TOKEN from environment"
elif [[ -f "$TOKEN_FILE" ]]; then
    TOKEN="$(cat "$TOKEN_FILE")"
    ok "Using token from $TOKEN_FILE"
else
    read -rsp "  API token (leave blank if orchestrator is local/no-auth): " TOKEN
    echo ""
fi

# ── Write connection_config.json ──────────────────────────────────────────────
header "Writing Config"

python3 - <<PYEOF
import json, os
path = "$CFG_FILE"
os.makedirs(os.path.dirname(path), exist_ok=True)   # admin/config/ ships with the repo, but be safe
cfg = {}
try:
    cfg = json.load(open(path))
except Exception:
    pass
cfg["orchestrator_url"] = "$ORCH_URL"
cfg["token"]            = "$TOKEN"
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("  wrote", path)
PYEOF
ok "connection_config.json updated"

# ── Shell alias ───────────────────────────────────────────────────────────────
header "Shell Alias"

ALIAS_LINE="alias gorgon-admin='PYTHONPATH=$FILES_DIR python3 $ADMIN_DIR/admin_tui.py'"

for RC in "$HOME/.zshrc" "$HOME/.bashrc"; do
    [[ -f "$RC" ]] || continue
    # Remove old admin alias lines
    sed -i '/gorgon-admin/d' "$RC" 2>/dev/null || true
    echo "" >> "$RC"
    echo "# gorgon admin" >> "$RC"
    echo "$ALIAS_LINE" >> "$RC"
    ok "Added alias to $RC"
done

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║   Admin TUI install complete!                ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Reload your shell, then:"
echo ""
echo -e "    ${BOLD}gorgon-admin${RESET}   — open the admin dashboard"
echo ""
echo -e "  The admin TUI connects to: ${CYAN}${ORCH_URL}${RESET}"
echo -e "  Edit ${CYAN}files/admin/config/connection_config.json${RESET} to change the target."
echo ""
