#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  setup_provider.sh — QEMU API provider machine setup (your laptop)
#
#  This is the AI side: Ollama + Python + the qemu-api chat loop.
#  Run this on YOUR machine (the one you type into).
#  The QEMU/VM side is handled by setup_client.sh on the remote machine.
#
#  Supports:
#    - Local mode  (everything on one machine, no remote server)
#    - Remote mode (talk to a friend's QEMU machine over the network)
#
#  Run as your normal user:
#    bash setup_provider.sh
#
#  Optional pre-sets to skip prompts:
#    API_URL=http://localhost:8080 API_TOKEN=mytoken bash setup_provider.sh
#    API_URL=local bash setup_provider.sh                   (local mode)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$HOME/qemu-env"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:7b}"

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()     { echo -e "${GREEN}  ✓${RESET} $*"; }
info()   { echo -e "${CYAN}  →${RESET} $*"; }
warn()   { echo -e "${YELLOW}  ⚠${RESET} $*"; }
fail()   { echo -e "${RED}  ✗${RESET} $*"; exit 1; }
header() { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }
ask()    { echo -e "${CYAN}  ?${RESET} $*"; }

# ── banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║   qemu-api — provider machine setup          ║${RESET}"
echo -e "${BOLD}${CYAN}║   AI chat loop + Ollama installer            ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""

# ── detect shell rc ───────────────────────────────────────────────────────────
if   [[ -f "$HOME/.zshrc" ]];  then SHELL_RC="$HOME/.zshrc"
elif [[ -f "$HOME/.bashrc" ]]; then SHELL_RC="$HOME/.bashrc"
else SHELL_RC="$HOME/.bashrc"; touch "$SHELL_RC"
fi
info "Shell config: $SHELL_RC"

# ── mode selection ────────────────────────────────────────────────────────────
header "Mode Selection"

if [[ -n "${API_URL:-}" ]]; then
    CHOSEN_URL="$API_URL"
    info "Using API_URL from environment: $CHOSEN_URL"
else
    echo ""
    echo "  How will this machine connect to QEMU?"
    echo ""
    echo "    1) Local mode  — QEMU runs on this machine (default)"
    echo "    2) Remote mode — QEMU runs on another machine (friend's PC, WSL2, etc.)"
    echo ""
    read -r -p "  Choice [1/2]: " MODE_CHOICE
    echo ""

    case "${MODE_CHOICE:-1}" in
        2)
            ask "Enter the remote client URL (e.g. http://localhost:8080 if tunnelling):"
            read -r -p "  API_URL: " CHOSEN_URL
            CHOSEN_URL="${CHOSEN_URL:-http://localhost:8080}"
            ;;
        *)
            CHOSEN_URL="local"
            ok "Local mode selected"
            ;;
    esac
fi

# ── API token (remote mode only) ──────────────────────────────────────────────
CHOSEN_TOKEN=""
CHOSEN_CA_CERT=""

if [[ "$CHOSEN_URL" != "local" ]]; then
    header "API Token"

    if [[ -n "${API_TOKEN:-}" ]]; then
        CHOSEN_TOKEN="$API_TOKEN"
        ok "Using API_TOKEN from environment"
    else
        echo ""
        echo "  This must match the token set on the client machine."
        echo "  (Shown at the end of setup_client.sh on the friend's machine)"
        echo ""
        read -r -p "  API_TOKEN: " CHOSEN_TOKEN
        if [[ -z "$CHOSEN_TOKEN" ]]; then
            warn "No token entered — you will need to set API_TOKEN manually before connecting."
        fi
    fi

    # TLS cert (optional)
    if [[ "$CHOSEN_URL" == https://* ]]; then
        header "TLS Certificate"
        echo ""
        echo "  HTTPS detected. If the server uses a self-signed certificate, provide"
        echo "  the path to the CA cert file (copied from the client machine)."
        echo "  Press Enter to skip (uses system CAs — correct for Let's Encrypt)."
        echo ""
        read -r -p "  Path to CA cert (or Enter to skip): " CHOSEN_CA_CERT
        CHOSEN_CA_CERT="${CHOSEN_CA_CERT:-}"
        [[ -n "$CHOSEN_CA_CERT" ]] && ok "CA cert: $CHOSEN_CA_CERT" || ok "Using system CA bundle"
    fi
fi

# ── system packages ───────────────────────────────────────────────────────────
header "System Packages"

# Minimal set — no QEMU, no KVM, no bridge tools
PKGS=(python3-venv python3-pip curl)

MISSING=()
for pkg in "${PKGS[@]}"; do
    dpkg -l "$pkg" &>/dev/null || MISSING+=("$pkg")
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    info "Installing: ${MISSING[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y "${MISSING[@]}"
    ok "System packages installed"
else
    ok "All system packages already present"
fi

# ── Python venv ───────────────────────────────────────────────────────────────
header "Python Virtual Environment"

if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
    ok "Created venv at $VENV_DIR"
else
    ok "Venv already exists at $VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet requests psutil rich fastapi uvicorn httpx
ok "Python packages installed (requests, psutil, rich, fastapi, uvicorn, httpx)"

# ── Ollama ────────────────────────────────────────────────────────────────────
header "Ollama"

if ! command -v ollama &>/dev/null; then
    info "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
    ok "Ollama installed"
else
    ok "Ollama already installed: $(ollama --version 2>/dev/null || echo 'version unknown')"
fi

# Start ollama if not running
if ! pgrep -x ollama &>/dev/null; then
    info "Starting Ollama..."
    nohup ollama serve > /tmp/ollama.log 2>&1 &
    sleep 3
fi

if pgrep -x ollama &>/dev/null; then
    ok "Ollama is running"
else
    warn "Ollama did not start — try: ollama serve &"
fi

# Pull model
if ollama list 2>/dev/null | grep -q "${OLLAMA_MODEL%%:*}"; then
    ok "Model '$OLLAMA_MODEL' already available"
else
    info "Pulling $OLLAMA_MODEL (this may take a few minutes)..."
    ollama pull "$OLLAMA_MODEL"
    ok "Model '$OLLAMA_MODEL' ready"
fi

# ── Ollama systemd service ────────────────────────────────────────────────────
header "Ollama Systemd Service"

SERVICE_FILE="$HOME/.config/systemd/user/ollama.service"
mkdir -p "$(dirname "$SERVICE_FILE")"

cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=Ollama LLM Service
After=network.target

[Service]
Type=simple
ExecStart=$(command -v ollama) serve
Restart=on-failure
RestartSec=5
Environment=HOME=$HOME

[Install]
WantedBy=default.target
SVCEOF

if systemctl --user daemon-reload 2>/dev/null && \
   systemctl --user enable ollama 2>/dev/null && \
   systemctl --user start  ollama 2>/dev/null; then
    ok "Ollama systemd service enabled (auto-starts on login)"
else
    warn "Could not enable systemd service — start Ollama manually: ollama serve &"
fi

# ── shell integration ─────────────────────────────────────────────────────────
header "Shell Integration"

# Remove any previous qemu-api provider block
sed -i '/# qemu-api provider start/,/# qemu-api provider end/d' "$SHELL_RC" 2>/dev/null || true

# Build the env var block
ENV_BLOCK=""
ENV_BLOCK+="export API_URL=\"$CHOSEN_URL\"\n"
[[ -n "$CHOSEN_TOKEN"   ]] && ENV_BLOCK+="export API_TOKEN=\"$CHOSEN_TOKEN\"\n"
[[ -n "$CHOSEN_CA_CERT" ]] && ENV_BLOCK+="export API_CA_CERT=\"$CHOSEN_CA_CERT\"\n"
ENV_BLOCK+="export OLLAMA_MODEL=\"$OLLAMA_MODEL\"\n"

cat >> "$SHELL_RC" << SHELLEOF

# qemu-api provider start
source "$VENV_DIR/bin/activate"
$(printf '%b' "$ENV_BLOCK")alias qemu-api='PYTHONPATH=$FILES_DIR python3 $FILES_DIR/provider/ollama_wrapper.py'
# qemu-api provider end
SHELLEOF

ok "Added qemu-api alias and env vars to $SHELL_RC"

# Source it in the current session too
export API_URL="$CHOSEN_URL"
[[ -n "$CHOSEN_TOKEN"   ]] && export API_TOKEN="$CHOSEN_TOKEN"
[[ -n "$CHOSEN_CA_CERT" ]] && export API_CA_CERT="$CHOSEN_CA_CERT"
export OLLAMA_MODEL="$OLLAMA_MODEL"

# ── session directory ─────────────────────────────────────────────────────────
header "Session Directory"
mkdir -p "$HOME/.qemu_vms/_profiles" "$HOME/.qemu_vms/_networks"
ok "~/.qemu_vms/ ready (session history, custom profiles)"

# ── connectivity check ────────────────────────────────────────────────────────
header "Connectivity Check"

if [[ "$CHOSEN_URL" == "local" ]]; then
    # Local mode: just verify the imports work
    if python3 -c "
import sys; sys.path.insert(0, '$FILES_DIR')
from provider.executor_client import execute_tool, API_URL
assert API_URL == 'local', f'Expected local, got {API_URL}'
print('ok')
" 2>/dev/null | grep -q ok; then
        ok "Local mode import check passed"
    else
        warn "Import check had issues — run: python3 -c \"from provider.executor_client import execute_tool\""
    fi
else
    # Remote mode: ping the client machine's /health endpoint
    info "Pinging client machine at $CHOSEN_URL/health ..."
    HEALTH_ARGS=()
    [[ -n "$CHOSEN_CA_CERT" ]] && HEALTH_ARGS+=(--cacert "$CHOSEN_CA_CERT")

    if curl -sf "${HEALTH_ARGS[@]}" "$CHOSEN_URL/health" 2>/dev/null | grep -q '"ok"'; then
        ok "Client machine is reachable and healthy"
    else
        warn "Could not reach $CHOSEN_URL/health"
        warn "Make sure the SSH tunnel or server is running on the client machine."
        warn "This is not fatal — connectivity check runs again when you start the chat."
    fi
fi

# ── summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║   Provider setup complete!                   ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════╝${RESET}"
echo ""

if [[ "$CHOSEN_URL" == "local" ]]; then
    echo -e "  Mode        : ${BOLD}local${RESET} (QEMU on this machine)"
else
    echo -e "  Mode        : ${BOLD}remote${RESET}"
    echo -e "  API_URL     : ${BOLD}$CHOSEN_URL${RESET}"
    [[ -n "$CHOSEN_TOKEN"   ]] && echo -e "  API_TOKEN   : ${BOLD}$CHOSEN_TOKEN${RESET}"
    [[ -n "$CHOSEN_CA_CERT" ]] && echo -e "  API_CA_CERT : ${BOLD}$CHOSEN_CA_CERT${RESET}"
fi

echo -e "  Model       : ${BOLD}$OLLAMA_MODEL${RESET}"
echo ""
echo -e "  Reload shell:  ${BOLD}source $SHELL_RC${RESET}"
echo -e "  Then run:      ${BOLD}qemu-api${RESET}"
echo ""

if [[ "$CHOSEN_URL" != "local" ]]; then
    echo -e "  ${YELLOW}Remember:${RESET} the SSH tunnel to the client machine must be open"
    echo -e "  before starting the chat. If you haven't opened it yet:"
    echo ""
    echo -e "    ${BOLD}ssh -N -L 8080:127.0.0.1:8080 -L 5901:127.0.0.1:5901 \\${RESET}"
    echo -e "    ${BOLD}    <user>@<client-ip>${RESET}    (add -p 2222 for WSL2)"
    echo ""
fi

echo -e "  To change mode later, edit $SHELL_RC"
echo -e "  and update the API_URL / API_TOKEN lines, then: source $SHELL_RC"
echo ""
