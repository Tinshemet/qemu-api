#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  setup_client.sh — QEMU API server setup for the client (QEMU) machine
#
#  Supports:
#    - Native Linux (Ubuntu, Debian, Linux Mint)
#    - WSL2 on Windows
#
#  Run as your normal user (uses sudo internally where needed):
#    bash setup_client.sh
#
#  Optional: pre-set the API token to avoid the prompt:
#    API_TOKEN=mysecrettoken bash setup_client.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$HOME/qemu-env"
START_SCRIPT="$HOME/start-qemu-api.sh"
SERVICE_FILE="$HOME/.config/systemd/user/qemu-api.service"
TOKEN_FILE="$HOME/.qemu-api.token"

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()     { echo -e "${GREEN}  ✓${RESET} $*"; }
info()   { echo -e "${CYAN}  →${RESET} $*"; }
warn()   { echo -e "${YELLOW}  ⚠${RESET} $*"; }
fail()   { echo -e "${RED}  ✗${RESET} $*"; }
header() { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }

# ── banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║   qemu-api — client machine setup            ║${RESET}"
echo -e "${BOLD}${CYAN}║   QEMU/KVM API server installer              ║${RESET}"
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
        echo "  If /dev/kvm still doesn't appear, your hardware or Windows version"
        echo "  may not support KVM in WSL2. Consider dual-booting Linux instead."
        echo ""
        exit 1
    else
        warn "KVM not available — check BIOS VT-x/AMD-V settings."
        warn "VMs will run in software emulation (very slow). Continuing anyway."
    fi
fi

# ── system packages ───────────────────────────────────────────────────────────
header "System Packages"

PKGS=(
    qemu-system-x86 qemu-kvm qemu-utils qemu-system-arm
    ovmf socat python3-venv python3-pip cpu-checker
    bridge-utils openssh-server
)

# libvirt packages are not available in all WSL2 environments; skip if unavailable
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
    /usr/share/ovmf/x64/OVMF_CODE.fd \
    /usr/share/edk2-ovmf/x64/OVMF_CODE.fd; do
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
    echo "  The API token is a shared secret between the server and your laptop."
    echo "  It must match on both sides. Choose something long and random."
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

# ── start script ──────────────────────────────────────────────────────────────
header "Server Start Script"

cat > "$START_SCRIPT" << STARTEOF
#!/usr/bin/env bash
# Auto-generated by setup_client.sh — edit API_TOKEN or PORT as needed
set -euo pipefail

source "$VENV_DIR/bin/activate"
cd "$FILES_DIR"

export API_TOKEN="\$(cat "$TOKEN_FILE" 2>/dev/null || echo '')"

if [[ -z "\$API_TOKEN" ]]; then
    echo "ERROR: No API token found at $TOKEN_FILE"
    echo "Run: echo 'yourtoken' > $TOKEN_FILE && chmod 600 $TOKEN_FILE"
    exit 1
fi

exec python3 provider/ollama_wrapper.py serve 127.0.0.1 8080
STARTEOF

chmod +x "$START_SCRIPT"
ok "Created $START_SCRIPT"

# ── SSH server ────────────────────────────────────────────────────────────────
header "SSH Server"
if [[ "$IS_WSL" == true ]]; then
    # WSL2 needs manual sshd start each time unless scripted
    sudo service ssh start 2>/dev/null || sudo systemctl start ssh 2>/dev/null || true
    ok "SSH started"

    # Add auto-start to .bashrc if not already there
    if ! grep -q "service ssh start" "$HOME/.bashrc" 2>/dev/null; then
        echo "" >> "$HOME/.bashrc"
        echo "# qemu-api: keep SSH running in WSL2" >> "$HOME/.bashrc"
        echo "sudo service ssh start 2>/dev/null || true" >> "$HOME/.bashrc"
        ok "Added SSH auto-start to ~/.bashrc"
    else
        ok "SSH auto-start already in ~/.bashrc"
    fi

    # sudoers entry so ssh start doesn't need password
    SUDOERS_LINE="$USER ALL=(ALL) NOPASSWD: /usr/sbin/service ssh start"
    if ! sudo grep -qF "$SUDOERS_LINE" /etc/sudoers.d/wsl-ssh 2>/dev/null; then
        echo "$SUDOERS_LINE" | sudo tee /etc/sudoers.d/wsl-ssh > /dev/null
        sudo chmod 0440 /etc/sudoers.d/wsl-ssh
        ok "Added passwordless sudo for 'service ssh start'"
    else
        ok "Passwordless sudo for SSH already configured"
    fi
else
    sudo systemctl enable --now ssh 2>/dev/null || sudo systemctl enable --now sshd 2>/dev/null || true
    ok "SSH enabled and started"
fi

# ── systemd service (native Linux only) ──────────────────────────────────────
header "Systemd Service"
if [[ "$IS_WSL" == false ]]; then
    mkdir -p "$(dirname "$SERVICE_FILE")"
    cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=QEMU API Server
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
    systemctl --user enable qemu-api
    systemctl --user start qemu-api
    ok "systemd user service enabled and started"
    ok "Server auto-starts on login"

    # Enable linger so service runs even when not logged in
    loginctl enable-linger "$USER" 2>/dev/null || \
        warn "loginctl enable-linger failed — service requires login to run"
    ok "loginctl linger enabled (service runs without active login session)"
else
    info "WSL2: systemd is not fully supported — using start script instead"
    info "The server will start when WSL2 is opened."

    # Add server auto-start to .bashrc for WSL2
    if ! grep -q "start-qemu-api" "$HOME/.bashrc" 2>/dev/null; then
        echo "" >> "$HOME/.bashrc"
        echo "# qemu-api: auto-start server" >> "$HOME/.bashrc"
        echo "nohup $START_SCRIPT > /tmp/qemu-api.log 2>&1 &" >> "$HOME/.bashrc"
        ok "Added server auto-start to ~/.bashrc"
    else
        ok "Server auto-start already in ~/.bashrc"
    fi

    # Start it now
    info "Starting server in background..."
    nohup "$START_SCRIPT" > /tmp/qemu-api.log 2>&1 &
    sleep 2
    if pgrep -f "ollama_wrapper.py serve" > /dev/null; then
        ok "Server is running (logs: /tmp/qemu-api.log)"
    else
        warn "Server may not have started — check: cat /tmp/qemu-api.log"
    fi
fi

# ── verify server ─────────────────────────────────────────────────────────────
header "Server Health Check"
sleep 2
if curl -sf http://127.0.0.1:8080/health > /dev/null 2>&1; then
    ok "Server is responding at http://127.0.0.1:8080/health"
else
    warn "Server not yet responding — it may still be starting up"
    info "Check: curl http://127.0.0.1:8080/health"
    info "Logs:  journalctl --user -u qemu-api -f  (native)"
    info "Logs:  cat /tmp/qemu-api.log              (WSL2)"
fi

# ── gather connection info ────────────────────────────────────────────────────
header "Connection Info"

LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'unknown')"
PUBLIC_IP="$(curl -sf --max-time 4 ifconfig.me 2>/dev/null || echo 'unknown (check manually)')"
WSL2_IP="$LOCAL_IP"

TOKEN_DISPLAY="$(cat "$TOKEN_FILE")"

echo ""
echo -e "${BOLD}  Setup complete. Share these details with yourself (the laptop):${RESET}"
echo ""
echo -e "  API_TOKEN   = ${BOLD}$TOKEN_DISPLAY${RESET}"
echo -e "  Public IP   = ${BOLD}$PUBLIC_IP${RESET}"

if [[ "$IS_WSL" == true ]]; then
    echo -e "  WSL2 IP     = ${BOLD}$WSL2_IP${RESET}  (changes on reboot)"
    echo ""
    echo -e "${BOLD}  Next step on Windows:${RESET}"
    echo -e "  Run (as Admin in PowerShell):  setup_wsl2.ps1"
    echo ""
    echo -e "${BOLD}  Then on your laptop:${RESET}"
    echo "    ssh -N -L 8080:127.0.0.1:8080 -L 5901:127.0.0.1:5901 -p 2222 $USER@$PUBLIC_IP"
    echo "    export API_URL=http://localhost:8080"
    echo "    export API_TOKEN=$TOKEN_DISPLAY"
    echo "    python3 provider/ollama_wrapper.py"
else
    echo ""
    echo -e "${BOLD}  On your laptop:${RESET}"
    echo "    ssh -N -L 8080:127.0.0.1:8080 -L 5901:127.0.0.1:5901 $USER@$PUBLIC_IP"
    echo "    export API_URL=http://localhost:8080"
    echo "    export API_TOKEN=$TOKEN_DISPLAY"
    echo "    python3 provider/ollama_wrapper.py"
fi
echo ""
