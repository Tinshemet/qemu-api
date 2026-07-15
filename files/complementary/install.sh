#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  install.sh — gorgon SINGLE-MACHINE setup
#
#  Use this when QEMU, Ollama, and the AI chat client all run on the same box.
#  For a two-machine setup use setup_server.sh + setup_client.sh separately.
#
#  What this does:
#    1. Runs install_orchestrator.sh — installs Ollama + HTTP API
#    2. Runs install_executor.sh     — installs QEMU + executor server
#    3. Runs setup_client.sh         — installs the thin chat UI pointed at localhost
#    The two scripts share a single auto-generated API token.
#
#  Run as your normal user (sudo is invoked internally where needed):
#    bash install.sh
#
#  Uninstall:
#    bash install.sh --uninstall
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN_FILE="$HOME/.gorgon.token"

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()     { echo -e "${GREEN}  ✓${RESET} $*"; }
info()   { echo -e "${CYAN}  →${RESET} $*"; }
warn()   { echo -e "${YELLOW}  ⚠${RESET} $*"; }
header() { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }

# ── uninstall mode ────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    header "Uninstalling gorgon"
    for RC in "$HOME/.zshrc" "$HOME/.bashrc"; do
        [[ -f "$RC" ]] || continue
        sed -i '/# gorgon server start/,/# gorgon server end/d' "$RC" 2>/dev/null || true
        sed -i '/# gorgon client start/,/# gorgon client end/d' "$RC" 2>/dev/null || true
        ok "Cleaned $RC"
    done
    [[ -d "$HOME/qemu-env" ]] && rm -rf "$HOME/qemu-env" && ok "Removed venv at ~/qemu-env"
    [[ -f "$TOKEN_FILE"    ]] && rm -f  "$TOKEN_FILE"     && ok "Removed token file"
    systemctl --user stop    gorgon-server 2>/dev/null || true
    systemctl --user disable gorgon-server 2>/dev/null || true
    warn "VM data at ~/.qemu_vms was NOT removed. Delete manually if desired."
    echo -e "\n${GREEN}Uninstall complete.${RESET}\n"
    exit 0
fi

# ── banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║   gorgon — single-machine installer        ║${RESET}"
echo -e "${BOLD}${CYAN}║   QEMU + Ollama + AI chat on one box         ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo "  This will run install_orchestrator.sh, install_executor.sh, then setup_client.sh"
echo "  with a shared token and SERVER_URL=http://localhost:8080"
echo ""

# ── generate or reuse token ───────────────────────────────────────────────────
header "API Token"

if [[ -n "${API_TOKEN:-}" ]]; then
    TOKEN="$API_TOKEN"
    ok "Using API_TOKEN from environment"
elif [[ -f "$TOKEN_FILE" ]]; then
    TOKEN="$(cat "$TOKEN_FILE")"
    ok "Reusing existing token from $TOKEN_FILE"
else
    TOKEN="$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")"
    printf '%s' "$TOKEN" > "$TOKEN_FILE"
    chmod 0600 "$TOKEN_FILE"
    ok "Generated new token → $TOKEN_FILE"
fi

# ── run orchestrator setup ────────────────────────────────────────────────────
header "Orchestrator Setup (Ollama + HTTP API)"

# Suppress install_orchestrator.sh's own operator-mode prompt — asked once,
# below, after the client is set up too (not mid-way through this combined install).
GORGON_SKIP_OPERATOR_PROMPT=1 API_TOKEN="$TOKEN" bash "$SCRIPT_DIR/install_orchestrator.sh"

# ── run executor setup ────────────────────────────────────────────────────────
header "Executor Setup (QEMU + executor server)"

EXECUTOR_TOKEN="$TOKEN" bash "$SCRIPT_DIR/install_executor.sh"

# ── run client setup ──────────────────────────────────────────────────────────
header "Client Setup (AI Chat UI → localhost)"

SERVER_URL="http://localhost:8080" API_TOKEN="$TOKEN" bash "$SCRIPT_DIR/setup_client.sh"

# ── operator-only mode ────────────────────────────────────────────────────────
FILES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
header "Operator-Only Mode"
echo "  By default, anyone with a shell on this machine or the API_TOKEN"
echo "  above can use gorgon — no per-user identity."
echo ""
echo "  Operator-only mode requires logging in with a username/password"
echo "  before gorgon (CLI or chat) works at all. You can turn this on"
echo "  later any time by running: gorgon login"
echo ""
read -r -p "  Enable operator-only mode now? [y/N]: " ENABLE_OPERATOR
if [[ "$ENABLE_OPERATOR" =~ ^[Yy] ]]; then
    PYTHONPATH="$FILES_DIR" python3 "$FILES_DIR/client/client_wrapper.py" login
else
    echo "  Staying operatorless for now — run 'gorgon login' any time to enable it."
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║   Single-machine install complete!           ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Reload shell, then:"
echo ""
echo -e "    ${BOLD}gorgon-serve${RESET}   — start the API server (runs in background)"
echo -e "    ${BOLD}gorgon${RESET}         — open the AI chat"
echo ""
echo -e "  ${YELLOW}Tip:${RESET} The server must be running before the client can connect."
echo -e "  On first boot, gorgon-serve starts automatically via systemd."
echo ""
echo -e "  Uninstall: ${BOLD}bash install.sh --uninstall${RESET}"
echo ""
