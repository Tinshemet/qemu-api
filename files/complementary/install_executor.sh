#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  install_executor.sh — qemu-api EXECUTOR machine setup
#
#  The executor machine hosts:
#    • QEMU/KVM (VM engine)
#    • executor HTTP server  (uvicorn on port 8001)
#
#  It does NOT need Ollama.  AI/routing happens on the orchestrator machine.
#  In local (single-machine) mode run install.sh instead.
#
#  Optional pre-sets:
#    EXECUTOR_TOKEN=mysecret bash install_executor.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$HOME/qemu-env"
START_SCRIPT="$HOME/start-qemu-api-executor.sh"
SERVICE_FILE="$HOME/.config/systemd/user/qemu-api-executor.service"
TOKEN_FILE="$HOME/.qemu-api-executor.token"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()     { echo -e "${GREEN}  ✓${RESET} $*"; }
info()   { echo -e "${CYAN}  →${RESET} $*"; }
warn()   { echo -e "${YELLOW}  ⚠${RESET} $*"; }
header() { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║   qemu-api — EXECUTOR machine setup          ║${RESET}"
echo -e "${BOLD}${CYAN}║   QEMU/KVM + executor server                 ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""

IS_WSL=false
if grep -qi microsoft /proc/version 2>/dev/null; then IS_WSL=true; ok "Running inside WSL2"
else ok "Running on native Linux"; fi

if [[ -e /dev/kvm ]]; then ok "KVM is available"
else warn "KVM not available — VMs will run in software emulation (very slow)"; fi

if   [[ -f "$HOME/.zshrc" ]];  then SHELL_RC="$HOME/.zshrc"
elif [[ -f "$HOME/.bashrc" ]]; then SHELL_RC="$HOME/.bashrc"
else SHELL_RC="$HOME/.bashrc"; touch "$SHELL_RC"; fi
info "Shell config: $SHELL_RC"

header "System Packages"

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PKGS=(
    qemu-system-x86 qemu-kvm qemu-utils qemu-system-arm
    ovmf socat python3-venv "python${PY_VER}-venv" python3-pip
    acpica-tools genisoimage mtools swtpm
    cpu-checker bridge-utils curl
)
if [[ "$IS_WSL" == false ]]; then
    PKGS+=(libvirt-daemon-system libvirt-clients virt-viewer tigervnc-viewer)
fi

MISSING=()
for pkg in "${PKGS[@]}"; do
    dpkg -l "$pkg" &>/dev/null || MISSING+=("$pkg")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    sudo apt-get update -qq
    sudo apt-get install -y "${MISSING[@]}" || {
        for pkg in "${MISSING[@]}"; do
            sudo apt-get install -y "$pkg" 2>/dev/null && ok "  installed $pkg" || warn "  skipped $pkg"
        done
    }
fi
ok "System packages done"

header "KVM Permissions"
if groups "$USER" | grep -q kvm; then
    ok "User '$USER' already in kvm group"
else
    sudo usermod -aG kvm "$USER"
    warn "Added '$USER' to kvm group — log out and back in to activate"
fi

header "OVMF / UEFI Firmware"
OVMF_FOUND=false
for p in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd \
         /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/ovmf/x64/OVMF_CODE.fd; do
    if [[ -f "$p" ]]; then ok "OVMF found: $p"; OVMF_FOUND=true; break; fi
done
[[ "$OVMF_FOUND" == false ]] && warn "OVMF not found — UEFI/Windows 11 VMs will not work"

header "Stealth ACPI Battery"
BAT_DSL="$FILES_DIR/executor/api/acpi/battery.dsl"
BAT_AML="$FILES_DIR/executor/api/acpi/battery.aml"
if command -v iasl >/dev/null 2>&1 && [[ -f "$BAT_DSL" ]]; then
    if ( cd "$(dirname "$BAT_DSL")" && iasl -tc "$(basename "$BAT_DSL")" >/dev/null 2>&1 ); then
        ok "Compiled battery SSDT — laptop-persona stealth VMs get an ACPI battery"
    else
        warn "battery SSDT compile failed — laptop stealth VMs will have no battery"
    fi
elif [[ -f "$BAT_AML" ]]; then
    ok "Battery SSDT already present (prebuilt battery.aml)"
else
    warn "iasl (acpica-tools) missing — laptop stealth VMs will have no ACPI battery"
fi

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
ok "Python packages installed"

mkdir -p "$HOME/.qemu_vms/_profiles" "$HOME/.qemu_vms/_networks"
ok "~/.qemu_vms/ ready"

header "Executor Token"
if [[ -n "${EXECUTOR_TOKEN:-}" ]]; then
    TOKEN="$EXECUTOR_TOKEN"
    ok "Using EXECUTOR_TOKEN from environment"
elif [[ -f "$TOKEN_FILE" ]]; then
    TOKEN="$(cat "$TOKEN_FILE")"
    ok "Using existing token from $TOKEN_FILE"
else
    echo ""
    echo "  The executor token is the shared secret the orchestrator uses to call this machine."
    echo "  Use the same value you set as 'url' token in orchestrator/connection_config.json."
    echo "  Press Enter to auto-generate, or type the orchestrator token:"
    echo ""
    read -r -p "  Executor token: " INPUT_TOKEN
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
ok "Token saved to $TOKEN_FILE"

# Patch executor/config.json with the token
EXEC_CFG="$FILES_DIR/executor/config.json"
if [[ -f "$EXEC_CFG" ]]; then
    python3 -c "
import json, sys
path = sys.argv[1]; tok = sys.argv[2]
c = json.load(open(path))
c['token'] = tok
json.dump(c, open(path, 'w'), indent=4)
" "$EXEC_CFG" "$TOKEN"
    ok "Wrote token to $EXEC_CFG"
fi

header "Executor Start Script"
cat > "$START_SCRIPT" << STARTEOF
#!/usr/bin/env bash
set -euo pipefail

source "$VENV_DIR/bin/activate"
cd "$FILES_DIR"

export EXECUTOR_TOKEN="\$(cat "$TOKEN_FILE" 2>/dev/null || echo '')"
export PYTHONPATH="$FILES_DIR"

EXISTING=\$(lsof -ti:8001 2>/dev/null || true)
if [[ -n "\$EXISTING" ]]; then
    echo "  → Freeing port 8001 (PID \$(echo \$EXISTING | tr '\n' ' '))..."
    echo "\$EXISTING" | xargs kill 2>/dev/null || true
    sleep 1
fi

exec uvicorn executor.server:app --host 0.0.0.0 --port 8001
STARTEOF
chmod +x "$START_SCRIPT"
ok "Created $START_SCRIPT"

header "Shell Integration"
sed -i '/# qemu-api executor start/,/# qemu-api executor end/d' "$SHELL_RC" 2>/dev/null || true
cat >> "$SHELL_RC" << SHELLEOF

# qemu-api executor start
source "$VENV_DIR/bin/activate"
export PATH="\$HOME/.local/bin:\$PATH"
alias qemu-api-executor='$START_SCRIPT'
# qemu-api executor end
SHELLEOF
ok "Added aliases to $SHELL_RC"

header "Systemd Service"
if [[ "$IS_WSL" == false ]]; then
    mkdir -p "$(dirname "$SERVICE_FILE")"
    cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=qemu-api Executor Server
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
    systemctl --user enable qemu-api-executor
    systemctl --user start  qemu-api-executor
    ok "systemd service enabled and started"
    loginctl enable-linger "$USER" 2>/dev/null || warn "loginctl enable-linger failed"
else
    nohup "$START_SCRIPT" > /tmp/qemu-api-executor.log 2>&1 &
    sleep 2
    pgrep -f "executor.server" > /dev/null && ok "Executor running" || warn "Executor may not have started"
fi

header "Health Check"
sleep 2
if curl -sf http://127.0.0.1:8001/health > /dev/null 2>&1; then
    ok "Executor responding at http://127.0.0.1:8001/health"
else
    warn "Executor not yet responding — check: curl http://127.0.0.1:8001/health"
fi

LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'unknown')"
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║   Executor setup complete!                   ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  EXECUTOR_TOKEN = ${BOLD}$TOKEN${RESET}"
echo -e "  Local IP       = ${BOLD}$LOCAL_IP${RESET}"
echo -e "  Executor API   = ${BOLD}http://$LOCAL_IP:8001${RESET}"
echo ""
echo -e "  On the orchestrator machine, set in orchestrator/connection_config.json:"
echo -e "    ${BOLD}\"url\": \"http://$LOCAL_IP:8001\"${RESET}"
echo -e "    ${BOLD}\"token\": \"$TOKEN\"${RESET}"
echo ""
echo -e "  Reload shell: ${BOLD}source $SHELL_RC${RESET}"
echo ""
