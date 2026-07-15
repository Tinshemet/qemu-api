#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  install_orchestrator.sh — gorgon ORCHESTRATOR machine setup
#
#  The orchestrator machine hosts:
#    • Ollama (AI model runner)
#    • gorgon HTTP API  (uvicorn on port 8080)
#    • Sanitizer, preflight, context gate
#
#  It does NOT need QEMU/KVM.  VM execution happens on the executor machine.
#  In local (single-machine) mode run install.sh instead.
#
#  Optional pre-sets:
#    API_TOKEN=mysecret OLLAMA_MODEL=qwen2.5:7b bash install_orchestrator.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$HOME/qemu-env"
START_SCRIPT="$HOME/start-gorgon-orchestrator.sh"
SERVICE_FILE="$HOME/.config/systemd/user/gorgon-orchestrator.service"
TOKEN_FILE="$HOME/.gorgon.token"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:7b}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()     { echo -e "${GREEN}  ✓${RESET} $*"; }
info()   { echo -e "${CYAN}  →${RESET} $*"; }
warn()   { echo -e "${YELLOW}  ⚠${RESET} $*"; }
header() { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║   gorgon — ORCHESTRATOR machine setup      ║${RESET}"
echo -e "${BOLD}${CYAN}║   Ollama + HTTP API (no QEMU)                ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""

IS_WSL=false
if grep -qi microsoft /proc/version 2>/dev/null; then IS_WSL=true; ok "Running inside WSL2"
else ok "Running on native Linux"; fi

if   [[ -f "$HOME/.zshrc" ]];  then SHELL_RC="$HOME/.zshrc"
elif [[ -f "$HOME/.bashrc" ]]; then SHELL_RC="$HOME/.bashrc"
else SHELL_RC="$HOME/.bashrc"; touch "$SHELL_RC"; fi
info "Shell config: $SHELL_RC"

header "System Packages"

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PKGS=(python3-venv "python${PY_VER}-venv" python3-pip curl openssh-server)

MISSING=()
for pkg in "${PKGS[@]}"; do
    # dpkg -l returns success (and a "not installed" row) for packages dpkg
    # merely *knows about* — purged/removed/never-configured — not just ones
    # that are actually installed, so it under-reports what's missing.
    # dpkg-query's Status field is the reliable check.
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "^install ok installed" || MISSING+=("$pkg")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    sudo apt-get update -qq
    sudo apt-get install -y "${MISSING[@]}" || {
        for pkg in "${MISSING[@]}"; do
            sudo apt-get install -y "$pkg" 2>/dev/null && ok "  installed $pkg" || warn "  skipped $pkg"
        done
    }
fi
# curl isn't optional — the Ollama install below and the health check at the
# end both hard-depend on it. Fail loudly here instead of letting a silently
# "skipped" package surface as a confusing "curl: command not found" deep
# inside the Ollama step.
command -v curl >/dev/null 2>&1 || {
    echo "ERROR: curl is required but could not be installed. Install it manually and re-run." >&2
    exit 1
}
ok "System packages done"

header "Python Virtual Environment"
if [[ -f "$VENV_DIR/bin/activate" ]]; then
    ok "Venv already exists at $VENV_DIR"
else
    # A previous failed attempt (e.g. the matching python3.X-venv package
    # wasn't installed, so ensurepip couldn't run) can leave $VENV_DIR
    # existing but incomplete — no bin/activate. Checking for activate
    # itself, not just the directory, catches that instead of silently
    # reusing a broken venv.
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
    [[ -f "$VENV_DIR/bin/activate" ]] || {
        echo "ERROR: venv creation failed — is python3-venv installed for this Python version?" >&2
        exit 1
    }
    ok "Created venv at $VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet requests psutil rich fastapi uvicorn httpx
ok "Python packages installed"

header "Ollama"
if ! command -v ollama &>/dev/null; then
    info "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
    ok "Ollama installed"
else
    ok "Ollama already installed: $(ollama --version 2>/dev/null || echo 'version unknown')"
fi

if ! pgrep -x ollama &>/dev/null; then
    nohup ollama serve > /tmp/ollama.log 2>&1 &
    sleep 3
fi

if ollama list 2>/dev/null | grep -q "${OLLAMA_MODEL%%:*}"; then
    ok "Model '$OLLAMA_MODEL' already available"
else
    info "Pulling $OLLAMA_MODEL..."
    ollama pull "$OLLAMA_MODEL"
    ok "Model '$OLLAMA_MODEL' ready"
fi

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
systemctl --user daemon-reload 2>/dev/null && \
    systemctl --user enable ollama 2>/dev/null && \
    systemctl --user start  ollama 2>/dev/null && \
    ok "Ollama systemd service enabled" || \
    warn "Could not enable Ollama systemd service — start manually: ollama serve &"

header "Orchestrator Config"

ORC_CFG="$FILES_DIR/orchestrator/connection_config.json"
if [[ ! -f "$ORC_CFG" ]]; then
    cat > "$ORC_CFG" << CFGEOF
{
  "url":                    "local",
  "token":                  "",
  "timeout":                120,
  "verify_ssl":             true,
  "ca_cert":                "",
  "client_allowed_vms":      [],
  "client_allowed_profiles": [],
  "allowed_remote_tools": [
    "create_vm", "launch_vm", "stop_vm", "delete_vm", "clone_vm",
    "list_vms", "vm_status", "monitor_vm", "show_config", "update_config",
    "resize_disk", "check_disk", "print_command", "fingerprint_vm",
    "snapshot_create", "snapshot_list", "snapshot_restore", "snapshot_delete",
    "create_network", "delete_network", "list_networks", "add_vm_to_network",
    "create_profile", "delete_profile", "list_profiles", "check_profile_compatibility",
    "check_system", "scan_isos", "get_vm_logs", "setup_done", "generate_guest_setup",
    "open_display", "open_shell"
  ]
}
CFGEOF
    ok "Created $ORC_CFG"
else
    ok "$ORC_CFG already exists — skipping"
fi

header "Executor Connection"
echo ""
echo "  This orchestrator needs to know how to reach the executor machine's"
echo "  QEMU engine (executor/server.py, uvicorn on port 8001 by default)."
echo ""

if [[ -n "${EXECUTOR_URL:-}" ]]; then
    EXEC_URL="$EXECUTOR_URL"
    ok "Using EXECUTOR_URL from environment: $EXEC_URL"
else
    read -r -p "  Executor URL (e.g. http://192.168.1.20:8001, blank to skip for now): " EXEC_URL
fi

if [[ -n "${EXECUTOR_TOKEN:-}" ]]; then
    EXEC_TOKEN="$EXECUTOR_TOKEN"
    ok "Using EXECUTOR_TOKEN from environment"
elif [[ -n "$EXEC_URL" ]]; then
    echo "  This must match the token shown at the end of install_executor.sh"
    echo "  (saved on the executor machine at ~/.gorgon-executor.token)."
    read -r -p "  Executor token: " EXEC_TOKEN
else
    EXEC_TOKEN=""
fi

if [[ -n "$EXEC_URL" ]]; then
    python3 -c "
import json, sys
path, url, token = sys.argv[1], sys.argv[2], sys.argv[3]
c = json.load(open(path))
c['url'] = url
c['token'] = token
json.dump(c, open(path, 'w'), indent=2)
" "$ORC_CFG" "$EXEC_URL" "$EXEC_TOKEN"
    ok "Wrote executor url + token to $ORC_CFG"
    if [[ -z "$EXEC_TOKEN" ]]; then
        warn "No executor token set — orchestrator→executor calls will be rejected (401)."
        warn "Set it later: edit \"token\" in $ORC_CFG"
    fi
else
    warn "Skipped — $ORC_CFG still has \"url\": \"local\" (works only if executor/ is also installed on this machine)."
    warn "Before running in split mode, edit $ORC_CFG and set:"
    warn "  \"url\":   \"http://<executor-host>:8001\""
    warn "  \"token\": \"<token from install_executor.sh>\""
fi

AI_CFG="$FILES_DIR/orchestrator/ai/config.json"
if [[ -f "$AI_CFG" ]]; then
    python3 -c "
import json, sys
path = sys.argv[1]; model = sys.argv[2]
c = json.load(open(path))
c['model'] = model
json.dump(c, open(path, 'w'), indent=4)
" "$AI_CFG" "$OLLAMA_MODEL"
    ok "Wrote model '$OLLAMA_MODEL' to $AI_CFG"
fi

mkdir -p "$HOME/.qemu_vms/_profiles" "$HOME/.qemu_vms/_networks"
ok "~/.qemu_vms/ ready"

header "API Token"
if [[ -n "${API_TOKEN:-}" ]]; then
    TOKEN="$API_TOKEN"
    ok "Using API_TOKEN from environment"
elif [[ -f "$TOKEN_FILE" ]]; then
    TOKEN="$(cat "$TOKEN_FILE")"
    ok "Using existing token from $TOKEN_FILE"
else
    echo ""
    echo "  This is the token clients use to authenticate to this orchestrator"
    echo "  (separate from the executor's own token, configured above). Press Enter to auto-generate:"
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
ok "Token saved to $TOKEN_FILE"

header "Orchestrator Start Script"
cat > "$START_SCRIPT" << STARTEOF
#!/usr/bin/env bash
set -euo pipefail

source "$VENV_DIR/bin/activate"
cd "$FILES_DIR"

export API_TOKEN="\$(cat "$TOKEN_FILE" 2>/dev/null || echo '')"
export PYTHONPATH="$FILES_DIR"

EXISTING=\$(lsof -ti:8080 2>/dev/null || true)
if [[ -n "\$EXISTING" ]]; then
    echo "  → Freeing port 8080 (PID \$(echo \$EXISTING | tr '\n' ' '))..."
    echo "\$EXISTING" | xargs kill 2>/dev/null || true
    sleep 1
fi

exec uvicorn orchestrator.http.api_server:app --host 0.0.0.0 --port 8080
STARTEOF
chmod +x "$START_SCRIPT"
ok "Created $START_SCRIPT"

header "Shell Integration"
sed -i '/# gorgon orchestrator start/,/# gorgon orchestrator end/d' "$SHELL_RC" 2>/dev/null || true
cat >> "$SHELL_RC" << SHELLEOF

# gorgon orchestrator start
source "$VENV_DIR/bin/activate"
export PATH="\$HOME/.local/bin:\$PATH"
alias gorgon-serve='$START_SCRIPT'
alias gorgon='PYTHONPATH=$FILES_DIR python3 $FILES_DIR/client/client_wrapper.py'
# gorgon orchestrator end
SHELLEOF
ok "Added aliases to $SHELL_RC"
info "For the admin dashboard, run: bash files/complementary/install_admin.sh"

header "Systemd Service"
if [[ "$IS_WSL" == false ]]; then
    mkdir -p "$(dirname "$SERVICE_FILE")"
    cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=gorgon Orchestrator HTTP Server
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
    systemctl --user enable gorgon-orchestrator
    systemctl --user start  gorgon-orchestrator
    ok "systemd service enabled and started"
    loginctl enable-linger "$USER" 2>/dev/null || warn "loginctl enable-linger failed"
else
    nohup "$START_SCRIPT" > /tmp/gorgon-orchestrator.log 2>&1 &
    sleep 2
    pgrep -f "api_server" > /dev/null && ok "Server running" || warn "Server may not have started"
fi

header "Health Check"
sleep 2
if curl -sf http://127.0.0.1:8080/health > /dev/null 2>&1; then
    ok "Orchestrator API responding at http://127.0.0.1:8080/health"
else
    warn "API not yet responding — check: curl http://127.0.0.1:8080/health"
fi

# ── operator-only mode ────────────────────────────────────────────────────────
# Skipped when called as a sub-step of install.sh (the single-machine
# installer) — that script asks this exact question once, itself, at the very
# end, after the client is set up too. Standalone/split-mode use of this
# script (no install.sh wrapping it) asks here instead.
if [[ "${GORGON_SKIP_OPERATOR_PROMPT:-0}" != "1" ]]; then
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
        PYTHONPATH="$FILES_DIR" python3 -m orchestrator.ai.cli login
    else
        info "Staying operatorless for now — run 'gorgon login' any time to enable it."
    fi
fi

LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'unknown')"
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║   Orchestrator setup complete!               ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  API_TOKEN  = ${BOLD}$TOKEN${RESET}  (client-facing — give this to setup_client.sh)"
echo -e "  Local IP   = ${BOLD}$LOCAL_IP${RESET}"
echo -e "  HTTP API   = ${BOLD}http://$LOCAL_IP:8080${RESET}"
if [[ -n "$EXEC_URL" ]]; then
    echo -e "  Executor   = ${BOLD}$EXEC_URL${RESET} (configured)"
else
    echo -e "  Executor   = ${YELLOW}not configured yet${RESET} — edit $ORC_CFG once install_executor.sh has run"
fi
echo ""
echo -e "  Next: on the executor machine run ${BOLD}install_executor.sh${RESET}"
echo -e "  Then: point clients at ${BOLD}http://$LOCAL_IP:8080${RESET} with the API_TOKEN above"
echo -e "  ${YELLOW}Note:${RESET} the client token and the executor token are separate secrets —"
echo -e "  the client token above is NOT the same as the executor's own token."
echo -e "  Reload shell: ${BOLD}source $SHELL_RC${RESET}"
echo ""
