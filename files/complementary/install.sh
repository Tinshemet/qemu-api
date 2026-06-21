#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  qemu-api installer for Linux Mint / Ubuntu / Debian
# ─────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HOME/qemu-env"
SHELL_RC="$HOME/.zshrc"
ALIAS_NAME="qemu-api"
OLLAMA_MODEL="llama3.1"

# ── colours ──────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}  ✓${RESET} $*"; }
info() { echo -e "${CYAN}  →${RESET} $*"; }
warn() { echo -e "${YELLOW}  ⚠${RESET} $*"; }
fail() { echo -e "${RED}  ✗${RESET} $*"; }
header() { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }

# ── uninstall mode ────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    header "Uninstalling qemu-api"
    # Remove alias and venv source from shell rc
    if [[ -f "$SHELL_RC" ]]; then
        sed -i '/# qemu-api/d' "$SHELL_RC"
        sed -i '/qemu-api/d' "$SHELL_RC"
        sed -i '/qemu-env\/bin\/activate/d' "$SHELL_RC"
        ok "Removed entries from $SHELL_RC"
    fi
    # Remove venv
    if [[ -d "$VENV_DIR" ]]; then
        rm -rf "$VENV_DIR"
        ok "Removed venv at $VENV_DIR"
    fi
    warn "VM data at ~/.qemu_vms was NOT removed. Delete manually if desired."
    echo -e "\n${GREEN}Uninstall complete.${RESET}\n"
    exit 0
fi

# ── banner ────────────────────────────────────────────────────
echo -e ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║        qemu-api  —  installer            ║${RESET}"
echo -e "${BOLD}${CYAN}║   QEMU/KVM + Ollama VM Manager           ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════╝${RESET}"
echo ""

# ── detect shell rc ───────────────────────────────────────────
if [[ -f "$HOME/.zshrc" ]]; then
    SHELL_RC="$HOME/.zshrc"
elif [[ -f "$HOME/.bashrc" ]]; then
    SHELL_RC="$HOME/.bashrc"
fi
info "Shell config: $SHELL_RC"

# ── system packages ───────────────────────────────────────────
header "System Packages"
PKGS=(
    qemu-system-x86 qemu-kvm qemu-utils qemu-system-arm
    ovmf virt-viewer tigervnc-viewer bridge-utils
    python3-pip python3-venv socat cpu-checker
    libvirt-daemon-system libvirt-clients
)
MISSING=()
for pkg in "${PKGS[@]}"; do
    if ! dpkg -l "$pkg" &>/dev/null; then
        MISSING+=("$pkg")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    info "Installing: ${MISSING[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y "${MISSING[@]}"
    ok "System packages installed"
else
    ok "All system packages already installed"
fi

# ── KVM group ─────────────────────────────────────────────────
header "KVM Permissions"
if groups "$USER" | grep -q kvm; then
    ok "User '$USER' already in kvm group"
else
    sudo usermod -aG kvm "$USER"
    sudo usermod -aG libvirt "$USER"
    warn "Added to kvm/libvirt groups — you must log out and back in for this to take effect"
fi

# ── KVM check ─────────────────────────────────────────────────
if [[ -e /dev/kvm ]]; then
    ok "KVM device available (/dev/kvm)"
else
    warn "KVM not available — check BIOS VT-x/AMD-V settings"
fi

# ── OVMF check ────────────────────────────────────────────────
header "OVMF / UEFI Firmware"
OVMF_FOUND=false
for p in /usr/share/OVMF/OVMF_CODE.fd /usr/share/edk2/ovmf/OVMF_CODE.fd \
          /usr/share/ovmf/x64/OVMF_CODE.fd /usr/share/edk2-ovmf/x64/OVMF_CODE.fd; do
    if [[ -f "$p" ]]; then
        ok "OVMF found: $p"
        OVMF_FOUND=true
        break
    fi
done
if [[ "$OVMF_FOUND" == false ]]; then
    warn "OVMF not found — SeaBIOS fallback will be used (UEFI/Windows 11 may not work)"
fi

# ── Python venv ───────────────────────────────────────────────
header "Python Virtual Environment"
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
    ok "Created venv at $VENV_DIR"
else
    ok "Venv already exists at $VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet requests psutil rich
ok "Python packages installed (requests, psutil, rich)"

# ── Ollama ────────────────────────────────────────────────────
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
    info "Starting Ollama service..."
    nohup ollama serve &>/tmp/ollama.log &
    sleep 3
    ok "Ollama started"
else
    ok "Ollama already running"
fi

# Pull model
if ollama list 2>/dev/null | grep -q "$OLLAMA_MODEL"; then
    ok "Model '$OLLAMA_MODEL' already pulled"
else
    info "Pulling $OLLAMA_MODEL (this may take a while)..."
    ollama pull "$OLLAMA_MODEL"
    ok "Model '$OLLAMA_MODEL' ready"
fi

# ── QEMU bridge helper ────────────────────────────────────────
header "Bridge Networking Setup"
BRIDGE_CONF="/etc/qemu/bridge.conf"
if [[ ! -f "$BRIDGE_CONF" ]]; then
    sudo mkdir -p /etc/qemu
    echo "allow virbr0" | sudo tee "$BRIDGE_CONF" > /dev/null
    echo "allow br0"    | sudo tee -a "$BRIDGE_CONF" > /dev/null
    ok "Created $BRIDGE_CONF (allows virbr0 and br0)"
else
    ok "Bridge config exists: $BRIDGE_CONF"
fi
BRIDGE_HELPER=$(find /usr/lib/qemu /usr/libexec -name "qemu-bridge-helper" 2>/dev/null | head -1)
if [[ -n "$BRIDGE_HELPER" ]]; then
    sudo chmod u+s "$BRIDGE_HELPER" 2>/dev/null && ok "Set setuid on $BRIDGE_HELPER" || true
fi

# ── Shell alias & venv ────────────────────────────────────────
header "Shell Integration"
# Remove any old entries first
sed -i '/# qemu-api start/,/# qemu-api end/d' "$SHELL_RC" 2>/dev/null || true

cat >> "$SHELL_RC" << SHELLEOF

# qemu-api start
source "$VENV_DIR/bin/activate"
alias $ALIAS_NAME='PYTHONPATH=$SCRIPT_DIR/.. python3 $SCRIPT_DIR/../provider/ollama_wrapper.py'
# qemu-api end
SHELLEOF
ok "Added alias '$ALIAS_NAME' and venv to $SHELL_RC"

# ── Systemd service for Ollama ────────────────────────────────
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
   systemctl --user start ollama 2>/dev/null; then
    ok "Ollama systemd user service enabled (auto-starts on login)"
else
    warn "Could not enable systemd user service — Ollama will need to be started manually"
fi

# ── VM directories ────────────────────────────────────────────
header "VM Directory"
mkdir -p "$HOME/.qemu_vms/_profiles"
ok "VM directory: ~/.qemu_vms/"
ok "Custom profiles: ~/.qemu_vms/_profiles/"

# ── Self-test ─────────────────────────────────────────────────
header "Self-Test"
if python3 "$SCRIPT_DIR/ollama_wrapper.py" system &>/dev/null; then
    ok "qemu-api self-test passed"
else
    warn "Self-test had issues — run 'qemu-api system' to diagnose"
fi

# ── Done ──────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║   Installation complete!                 ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Reload your shell:  ${BOLD}source $SHELL_RC${RESET}"
echo -e "  Then run:           ${BOLD}qemu-api${RESET}"
echo -e "  Verbose mode:       ${BOLD}qemu-api -v${RESET}"
echo -e "  Direct CLI:         ${BOLD}qemu-api list${RESET}"
echo -e "  Uninstall:          ${BOLD}bash install.sh --uninstall${RESET}"
echo ""
