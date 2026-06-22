#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  setup_client.sh — qemu-api CLIENT machine setup (your laptop)
#
#  Sets up the thin chat UI that connects to a qemu-api server.
#  No QEMU or Ollama needed here — the server handles all of that.
#
#  What this installs:
#    • Python venv + requests, rich
#    • Shell alias: qemu-api  (starts the AI chat client)
#    • client/connection_config.json with SERVER_URL and token
#
#  Run as your normal user:
#    bash setup_client.sh
#
#  Optional pre-sets:
#    SERVER_URL=http://192.168.1.10:8080 API_TOKEN=mytoken bash setup_client.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$HOME/qemu-env"
CLIENT_CFG="$FILES_DIR/client/connection_config.json"

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()     { echo -e "${GREEN}  ✓${RESET} $*"; }
info()   { echo -e "${CYAN}  →${RESET} $*"; }
warn()   { echo -e "${YELLOW}  ⚠${RESET} $*"; }
header() { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }
ask()    { echo -e "${CYAN}  ?${RESET} $*"; }

# ── banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║   qemu-api — client machine setup            ║${RESET}"
echo -e "${BOLD}${CYAN}║   AI chat UI (connects to remote server)     ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""

# ── detect shell rc ───────────────────────────────────────────────────────────
if   [[ -f "$HOME/.zshrc" ]];  then SHELL_RC="$HOME/.zshrc"
elif [[ -f "$HOME/.bashrc" ]]; then SHELL_RC="$HOME/.bashrc"
else SHELL_RC="$HOME/.bashrc"; touch "$SHELL_RC"
fi
info "Shell config: $SHELL_RC"

# ── server URL ────────────────────────────────────────────────────────────────
header "Server Connection"

if [[ -n "${SERVER_URL:-}" ]]; then
    CHOSEN_URL="$SERVER_URL"
    ok "Using SERVER_URL from environment: $CHOSEN_URL"
else
    echo ""
    echo "  Enter the qemu-api server URL."
    echo "  (If the server is on the same machine, use http://localhost:8080)"
    echo ""
    read -r -p "  SERVER_URL [http://localhost:8080]: " CHOSEN_URL
    CHOSEN_URL="${CHOSEN_URL:-http://localhost:8080}"
    ok "Server URL: $CHOSEN_URL"
fi

# ── API token ─────────────────────────────────────────────────────────────────
header "API Token"

if [[ -n "${API_TOKEN:-}" ]]; then
    CHOSEN_TOKEN="$API_TOKEN"
    ok "Using API_TOKEN from environment"
else
    echo ""
    echo "  This must match the token shown at the end of setup_server.sh."
    echo ""
    read -r -p "  API_TOKEN: " CHOSEN_TOKEN
    if [[ -z "$CHOSEN_TOKEN" ]]; then
        warn "No token entered — set API_TOKEN in your shell before running qemu-api."
    fi
fi

# TLS cert (optional, for self-signed HTTPS servers)
CHOSEN_CA_CERT=""
if [[ "$CHOSEN_URL" == https://* ]]; then
    header "TLS Certificate"
    echo ""
    echo "  HTTPS detected. If the server uses a self-signed cert, provide the path"
    echo "  to the CA cert file (copy it from the server machine)."
    echo "  Press Enter to skip (correct for Let's Encrypt)."
    echo ""
    read -r -p "  Path to CA cert (or Enter to skip): " CHOSEN_CA_CERT
    CHOSEN_CA_CERT="${CHOSEN_CA_CERT:-}"
    [[ -n "$CHOSEN_CA_CERT" ]] && ok "CA cert: $CHOSEN_CA_CERT" || ok "Using system CA bundle"
fi

# ── system packages ───────────────────────────────────────────────────────────
header "System Packages"

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PKGS=(python3-venv "python${PY_VER}-venv" python3-pip curl)

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
pip install --quiet requests rich
ok "Python packages installed (requests, rich)"

# ── write connection config ───────────────────────────────────────────────────
header "Connection Config"

mkdir -p "$(dirname "$CLIENT_CFG")"

CA_JSON="null"
[[ -n "$CHOSEN_CA_CERT" ]] && CA_JSON="\"$CHOSEN_CA_CERT\""

cat > "$CLIENT_CFG" << CFGEOF
{
  "server_url": "$CHOSEN_URL",
  "token":      "$CHOSEN_TOKEN",
  "ca_cert":    $CA_JSON,
  "verify_ssl": true,
  "timeout":    120
}
CFGEOF

ok "Written to $CLIENT_CFG"

# ── shell integration ─────────────────────────────────────────────────────────
header "Shell Integration"

sed -i '/# qemu-api client start/,/# qemu-api client end/d' "$SHELL_RC" 2>/dev/null || true

ENV_BLOCK=""
ENV_BLOCK+="export SERVER_URL=\"$CHOSEN_URL\"\n"
[[ -n "$CHOSEN_TOKEN"   ]] && ENV_BLOCK+="export API_TOKEN=\"$CHOSEN_TOKEN\"\n"
[[ -n "$CHOSEN_CA_CERT" ]] && ENV_BLOCK+="export API_CA_CERT=\"$CHOSEN_CA_CERT\"\n"

cat >> "$SHELL_RC" << SHELLEOF

# qemu-api client start
source "$VENV_DIR/bin/activate"
$(printf '%b' "$ENV_BLOCK")qemu-api() {
    if ! curl -sf "\$SERVER_URL/health" &>/dev/null; then
        echo "  ⚠  Server at \$SERVER_URL is not reachable."
        echo "     Make sure the server is running and the SSH tunnel is open (if remote)."
        echo "     Trying to connect anyway..."
    fi
    PYTHONPATH=$FILES_DIR python3 $FILES_DIR/client/client_wrapper.py "\$@"
}
# qemu-api client end
SHELLEOF

ok "Added qemu-api function to $SHELL_RC"

# ── connectivity check ────────────────────────────────────────────────────────
header "Connectivity Check"

CURL_ARGS=()
[[ -n "$CHOSEN_CA_CERT" ]] && CURL_ARGS+=(--cacert "$CHOSEN_CA_CERT")

if curl -sf "${CURL_ARGS[@]}" "$CHOSEN_URL/health" 2>/dev/null | grep -q '"ok"'; then
    ok "Server is reachable and healthy"
else
    warn "Could not reach $CHOSEN_URL/health"
    warn "Make sure the server is running: ssh to server and run qemu-api-serve"
    warn "If connecting over the internet, open the SSH tunnel first:"
    echo ""
    echo "    ssh -N -L 8080:127.0.0.1:8080 -L 5901:127.0.0.1:5901 <user>@<server-ip>"
    echo ""
fi

# ── summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║   Client setup complete!                     ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  SERVER_URL  : ${BOLD}$CHOSEN_URL${RESET}"
[[ -n "$CHOSEN_TOKEN"   ]] && echo -e "  API_TOKEN   : ${BOLD}$CHOSEN_TOKEN${RESET}"
[[ -n "$CHOSEN_CA_CERT" ]] && echo -e "  API_CA_CERT : ${BOLD}$CHOSEN_CA_CERT${RESET}"
echo ""
echo -e "  Reload shell : ${BOLD}source $SHELL_RC${RESET}"
echo -e "  Then run     : ${BOLD}qemu-api${RESET}"
echo ""
echo -e "  ${YELLOW}Tip:${RESET} If not on the same LAN as the server, open the SSH tunnel first:"
echo ""
echo "    ssh -N -L 8080:127.0.0.1:8080 -L 5901:127.0.0.1:5901 <user>@<server-ip>"
echo ""
