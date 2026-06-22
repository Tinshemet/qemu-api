#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  setup_server.sh — qemu-api SERVER machine setup
#
#  The server machine hosts:
#    • Ollama (AI model runner)
#    • QEMU/KVM (VM engine)
#    • qemu-api HTTP API  (uvicorn on port 8080)
#
#  Clients connect to this machine to run AI-assisted VM management.
#  For a single-machine setup, run this on your own PC and point the
#  client at http://localhost:8080.
#
#  Supports:
#    - Native Linux (Ubuntu, Debian, Linux Mint)
#    - WSL2 on Windows
#
#  Run as your normal user (uses sudo internally where needed):
#    bash setup_server.sh
#
#  Optional pre-sets:
#    API_TOKEN=mysecret bash setup_server.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$HOME/qemu-env"
START_SCRIPT="$HOME/start-qemu-api-server.sh"
SERVICE_FILE="$HOME/.config/systemd/user/qemu-api-server.service"
TOKEN_FILE="$HOME/.qemu-api.token"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:7b}"

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()     { echo -e "${GREEN}  ✓${RESET} $*"; }
info()   { echo -e "${CYAN}  →${RESET} $*"; }
warn()   { echo -e "${YELLOW}  ⚠${RESET} $*"; }
fail()   { echo -e "${RED}  ✗${RESET} $*"; exit 1; }
header() { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }

# ── banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║   qemu-api — SERVER machine setup            ║${RESET}"
echo -e "${BOLD}${CYAN}║   Ollama + QEMU + HTTP API                   ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""

# ── detect environment ────────────────────────────────────────────────────────
header "Environment Detection"

IS_WSL=false
if grep -qi microsoft /proc/version 2>/dev/null; then
    IS_WSL=true
    ok "Running inside WSL2"
else
    ok "Running on native Linux"
fi

if [[ -e /dev/kvm ]]; then
    ok "KVM is available (/dev/kvm exists)"
else
    if [[ "$IS_WSL" == true ]]; then
        fail "KVM not available in this WSL2 instance."
        echo ""
        echo "  Try: wsl --update  (run in PowerShell as Admin, then restart WSL2)"
        exit 1
    else
        warn "KVM not available — check BIOS VT-x/AMD-V settings."
        warn "VMs will run in software emulation (very slow). Continuing anyway."
    fi
fi

# ── detect shell rc ───────────────────────────────────────────────────────────
if   [[ -f "$HOME/.zshrc" ]];  then SHELL_RC="$HOME/.zshrc"
elif [[ -f "$HOME/.bashrc" ]]; then SHELL_RC="$HOME/.bashrc"
else SHELL_RC="$HOME/.bashrc"; touch "$SHELL_RC"
fi
info "Shell config: $SHELL_RC"

# ── system packages ───────────────────────────────────────────────────────────
header "System Packages"

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

PKGS=(
    qemu-system-x86 qemu-kvm qemu-utils qemu-system-arm
    ovmf socat python3-venv "python${PY_VER}-venv" python3-pip
    cpu-checker bridge-utils openssh-server curl
)

if [[ "$IS_WSL" == false ]]; then
    PKGS+=(libvirt-daemon-system libvirt-clients virt-viewer tigervnc-viewer)
fi

MISSING=()
for pkg in "${PKGS[@]}"; do
    dpkg -l "$pkg" &>/dev/null || MISSING+=("$pkg")
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    info "Installing: ${MISSING[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y "${MISSING[@]}" || {
        warn "Some packages failed — retrying individually..."
        for pkg in "${MISSING[@]}"; do
            sudo apt-get install -y "$pkg" 2>/dev/null && ok "  installed $pkg" || warn "  skipped $pkg (not available)"
        done
    }
    ok "System packages done"
else
    ok "All system packages already installed"
fi

# ── KVM group ─────────────────────────────────────────────────────────────────
header "KVM Permissions"
if groups "$USER" | grep -q kvm; then
    ok "User '$USER' already in kvm group"
else
    sudo usermod -aG kvm "$USER"
    warn "Added '$USER' to kvm group — you may need to log out and back in"
fi

# ── OVMF check ────────────────────────────────────────────────────────────────
header "OVMF / UEFI Firmware"
OVMF_FOUND=false
for p in \
    /usr/share/OVMF/OVMF_CODE_4M.fd \
    /usr/share/OVMF/OVMF_CODE.fd \
    /usr/share/edk2/ovmf/OVMF_CODE.fd \
    /usr/share/ovmf/x64/OVMF_CODE.fd; do
    if [[ -f "$p" ]]; then
        ok "OVMF found: $p"
        OVMF_FOUND=true
        break
    fi
done
[[ "$OVMF_FOUND" == false ]] && warn "OVMF not found — UEFI/Windows 11 VMs will not work"

# ── bridge networking ─────────────────────────────────────────────────────────
header "Bridge Networking"
if [[ "$IS_WSL" == false ]]; then
    BRIDGE_CONF="/etc/qemu/bridge.conf"
    if [[ ! -f "$BRIDGE_CONF" ]]; then
        sudo mkdir -p /etc/qemu
        printf 'allow virbr0\nallow br0\n' | sudo tee "$BRIDGE_CONF" > /dev/null
        ok "Created $BRIDGE_CONF"
    else
        ok "Bridge config already exists: $BRIDGE_CONF"
    fi
    BRIDGE_HELPER=$(find /usr/lib/qemu /usr/libexec -name "qemu-bridge-helper" 2>/dev/null | head -1 || true)
    [[ -n "$BRIDGE_HELPER" ]] && sudo chmod u+s "$BRIDGE_HELPER" 2>/dev/null && ok "Set setuid on bridge helper"
else
    info "WSL2: skipping bridge networking config (NAT mode only)"
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

if ollama list 2>/dev/null | grep -q "${OLLAMA_MODEL%%:*}"; then
    ok "Model '$OLLAMA_MODEL' already available"
else
    info "Pulling $OLLAMA_MODEL (this may take a few minutes)..."
    ollama pull "$OLLAMA_MODEL"
    ok "Model '$OLLAMA_MODEL' ready"
fi

# ── Ollama systemd service ────────────────────────────────────────────────────
header "Ollama Systemd Service"

OLLAMA_SERVICE="$HOME/.config/systemd/user/ollama.service"
mkdir -p "$(dirname "$OLLAMA_SERVICE")"

cat > "$OLLAMA_SERVICE" << SVCEOF
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
    ok "Ollama systemd service enabled"
else
    warn "Could not enable Ollama systemd service — start manually: ollama serve &"
fi

# ── VM directories ────────────────────────────────────────────────────────────
header "VM Directories"
mkdir -p "$HOME/.qemu_vms/_profiles" "$HOME/.qemu_vms/_networks"
ok "~/.qemu_vms/ ready"

# ── API token ─────────────────────────────────────────────────────────────────
header "API Token"

if [[ -n "${API_TOKEN:-}" ]]; then
    TOKEN="$API_TOKEN"
    ok "Using API_TOKEN from environment"
elif [[ -f "$TOKEN_FILE" ]]; then
    TOKEN="$(cat "$TOKEN_FILE")"
    ok "Using existing token from $TOKEN_FILE"
else
    echo ""
    echo "  The API token is a shared secret between server and clients."
    echo "  Press Enter to generate one automatically, or type your own:"
    echo ""
    read -r -p "  API token: " INPUT_TOKEN
    if [[ -z "$INPUT_TOKEN" ]]; then
        TOKEN="$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")"
        ok "Generated token: $TOKEN"
    else
        TOKEN="$INPUT_TOKEN"
        ok "Using provided token"
    fi
fi

printf '%s' "$TOKEN" > "$TOKEN_FILE"
chmod 0600 "$TOKEN_FILE"
ok "Token saved to $TOKEN_FILE (chmod 600)"

# ── server start script ───────────────────────────────────────────────────────
header "Server Start Script"

cat > "$START_SCRIPT" << STARTEOF
#!/usr/bin/env bash
# Auto-generated by setup_server.sh
set -euo pipefail

source "$VENV_DIR/bin/activate"
cd "$FILES_DIR"

export API_TOKEN="\$(cat "$TOKEN_FILE" 2>/dev/null || echo '')"
export PYTHONPATH="$FILES_DIR"

if [[ -z "\$API_TOKEN" ]]; then
    echo "INFO: No API token — localhost connections only (remote access disabled)"
fi

exec uvicorn server.http.api_server:app --host 0.0.0.0 --port 8080
STARTEOF

chmod +x "$START_SCRIPT"
ok "Created $START_SCRIPT"

# ── shell integration ─────────────────────────────────────────────────────────
header "Shell Integration"

sed -i '/# qemu-api server start/,/# qemu-api server end/d' "$SHELL_RC" 2>/dev/null || true

cat >> "$SHELL_RC" << SHELLEOF

# qemu-api server start
source "$VENV_DIR/bin/activate"
export OLLAMA_MODEL="$OLLAMA_MODEL"
alias qemu-api-serve='$START_SCRIPT'
qemu-api() {
    if ! curl -sf "http://localhost:8080/health" &>/dev/null; then
        echo "  → Starting qemu-api server..."
        nohup $START_SCRIPT &>/tmp/qemu-api-server.log &
        local attempts=0
        until curl -sf "http://localhost:8080/health" &>/dev/null || (( attempts++ >= 15 )); do
            sleep 0.5
        done
        if ! curl -sf "http://localhost:8080/health" &>/dev/null; then
            echo "  ✗ Server failed to start. Check /tmp/qemu-api-server.log"
            return 1
        fi
        echo "  ✓ Server ready"
    fi
    PYTHONPATH=$FILES_DIR python3 $FILES_DIR/client/client_wrapper.py "\$@"
}
# qemu-api server end
SHELLEOF

ok "Added qemu-api function and qemu-api-serve alias to $SHELL_RC"

# ── systemd service (native Linux only) ──────────────────────────────────────
header "Systemd Service (qemu-api HTTP server)"

if [[ "$IS_WSL" == false ]]; then
    mkdir -p "$(dirname "$SERVICE_FILE")"
    cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=qemu-api HTTP Server
After=network.target

[Service]
ExecStart=$START_SCRIPT
Restart=on-failure
RestartSec=5
Environment=HOME=$HOME

[Install]
WantedBy=default.target
SVCEOF

    systemctl --user daemon-reload
    systemctl --user enable qemu-api-server
    systemctl --user start  qemu-api-server
    ok "systemd user service enabled and started"

    loginctl enable-linger "$USER" 2>/dev/null || \
        warn "loginctl enable-linger failed — service requires login to run"
    ok "loginctl linger enabled"
else
    info "WSL2: using .bashrc auto-start instead of systemd"
    if ! grep -q "start-qemu-api-server" "$HOME/.bashrc" 2>/dev/null; then
        echo "" >> "$HOME/.bashrc"
        echo "# qemu-api: auto-start server" >> "$HOME/.bashrc"
        echo "nohup $START_SCRIPT > /tmp/qemu-api-server.log 2>&1 &" >> "$HOME/.bashrc"
        ok "Added server auto-start to ~/.bashrc"
    else
        ok "Server auto-start already in ~/.bashrc"
    fi
    info "Starting server in background..."
    nohup "$START_SCRIPT" > /tmp/qemu-api-server.log 2>&1 &
    sleep 2
    if pgrep -f "api_server" > /dev/null; then
        ok "Server is running (logs: /tmp/qemu-api-server.log)"
    else
        warn "Server may not have started — check: cat /tmp/qemu-api-server.log"
    fi
fi

# ── SSH server ────────────────────────────────────────────────────────────────
header "SSH Server"
if [[ "$IS_WSL" == true ]]; then
    sudo service ssh start 2>/dev/null || sudo systemctl start ssh 2>/dev/null || true
    ok "SSH started"
    if ! grep -q "service ssh start" "$HOME/.bashrc" 2>/dev/null; then
        echo "" >> "$HOME/.bashrc"
        echo "# qemu-api: keep SSH running in WSL2" >> "$HOME/.bashrc"
        echo "sudo service ssh start 2>/dev/null || true" >> "$HOME/.bashrc"
        ok "Added SSH auto-start to ~/.bashrc"
    fi
    SUDOERS_LINE="$USER ALL=(ALL) NOPASSWD: /usr/sbin/service ssh start"
    if ! sudo grep -qF "$SUDOERS_LINE" /etc/sudoers.d/wsl-ssh 2>/dev/null; then
        echo "$SUDOERS_LINE" | sudo tee /etc/sudoers.d/wsl-ssh > /dev/null
        sudo chmod 0440 /etc/sudoers.d/wsl-ssh
        ok "Added passwordless sudo for 'service ssh start'"
    fi
else
    sudo systemctl enable --now ssh 2>/dev/null || sudo systemctl enable --now sshd 2>/dev/null || true
    ok "SSH enabled and started"
fi

# ── health check ──────────────────────────────────────────────────────────────
header "Server Health Check"
sleep 2
if curl -sf http://127.0.0.1:8080/health > /dev/null 2>&1; then
    ok "HTTP API responding at http://127.0.0.1:8080/health"
else
    warn "API not yet responding — it may still be starting up"
    info "Check: curl http://127.0.0.1:8080/health"
    info "Logs (native): journalctl --user -u qemu-api-server -f"
    info "Logs (WSL2):   cat /tmp/qemu-api-server.log"
fi

# ── gather connection info ────────────────────────────────────────────────────
header "Connection Info (share these with your client machines)"

LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'unknown')"
PUBLIC_IP="$(curl -sf --max-time 4 ifconfig.me 2>/dev/null || echo 'unknown (check manually)')"
TOKEN_DISPLAY="$(cat "$TOKEN_FILE")"

echo ""
echo -e "${BOLD}  Server setup complete!${RESET}"
echo ""
echo -e "  API_TOKEN   = ${BOLD}$TOKEN_DISPLAY${RESET}"
echo -e "  Local IP    = ${BOLD}$LOCAL_IP${RESET}"
echo -e "  Public IP   = ${BOLD}$PUBLIC_IP${RESET}"
echo -e "  HTTP API    = ${BOLD}http://$LOCAL_IP:8080${RESET}"
echo ""
echo -e "${BOLD}  On each client machine:${RESET}"
echo ""
echo "    # If connecting over LAN (same network):"
echo "    SERVER_URL=http://$LOCAL_IP:8080 API_TOKEN=$TOKEN_DISPLAY qemu-api"
echo ""
echo "    # If connecting over the internet (SSH tunnel required first):"
echo "    ssh -N -L 8080:127.0.0.1:8080 -L 5901:127.0.0.1:5901 $USER@$PUBLIC_IP"
echo "    SERVER_URL=http://localhost:8080 API_TOKEN=$TOKEN_DISPLAY qemu-api"
echo ""

if [[ "$IS_WSL" == true ]]; then
    echo -e "${BOLD}  WSL2 extra step:${RESET}"
    echo "    Run setup_wsl2.ps1 as Admin in PowerShell to open the port on Windows Firewall."
    echo ""
fi

echo -e "  Model: ${BOLD}$OLLAMA_MODEL${RESET}"
echo -e "  Reload shell: ${BOLD}source $SHELL_RC${RESET}"
echo ""
