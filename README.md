# gorgon

**Release: [1.0-stable](https://github.com/Tinshemet/gorgon/releases/tag/1.0-stable)**

AI-driven QEMU/KVM virtual machine manager with an Ollama chat loop, remote split architecture, stealth/SMBIOS spoofing, TPM, Secure Boot, and an 11-layer automated test suite.

---

## Table of Contents

1. [What It Is](#what-it-is)
2. [Quick Start](#quick-start)
   - [Clone](#clone)
3. [Architecture](#architecture)
4. [Local Mode vs Remote Mode](#local-mode-vs-remote-mode)
5. [CLI Reference](#cli-reference)
6. [AI Chat Reference](#ai-chat-reference)
7. [VM Configuration Fields](#vm-configuration-fields)
8. [Hardware Profiles](#hardware-profiles)
9. [Unattended OS Install](#unattended-os-install)
10. [Golden-Image Templates](#golden-image-templates)
11. [Flags: -cu, -tf, -cs](#flags--cu--tf--cs)
12. [Stealth Mode](#stealth-mode)
13. [SMBIOS / Hardware Fingerprint](#smbios--hardware-fingerprint)
14. [Guest Setup Scripts](#guest-setup-scripts)
15. [Windows 11 Setup Workflow](#windows-11-setup-workflow)
16. [Bridge Networking](#bridge-networking)
17. [Remote Mode — Full Reference](#remote-mode--full-reference)
18. [API Endpoints](#api-endpoints)
19. [Configuration Reference](#configuration-reference)
20. [Test Suite](#test-suite)
21. [Directory Structure](#directory-structure)
22. [Known Issues](#known-issues)
23. [Stealth Rating Reference](#stealth-rating-reference)

---

## What It Is

`gorgon` is an AI-powered VM manager. You talk to it in plain English; an Ollama model (default: `qwen2.5:7b`) translates your intent into structured tool calls. Every tool call passes through five protection layers before QEMU sees it:

```
User Input
    ↓
qwen2.5:7b (Ollama)          — selects tool, builds args
    ↓
_sanitise_args()             — type coercion, enum normalisation, resource caps, placeholder removal
    ↓
_preflight_check()           — reality check: ok / auto_fix / ask_user / abort
    ↓
execute_tool()               — hard guards, arch checks, name validation
    ↓
QemuManager                  — actual QEMU/KVM operations
```

Even if the AI hallucinates bad arguments, they are caught and either silently corrected or the user is asked before anything dangerous happens.

The codebase is split into four canonical directories:

| Directory | Runs on | Purpose |
|---|---|---|
| `orchestrator/` | Orchestrator machine | AI (Ollama), HTTP `/chat` + `/execute` API, sanitizer, preflight |
| `executor/` | Executor machine | QEMU engine, tool dispatch, hardware fingerprint |
| `client/` | User's laptop | Thin chat TUI, optional local QEMU CLI |
| `shared/` | Bridge code | `tool_executor.py` (used by both sides in local mode), `display.py` |

In local mode all three run on one machine. In remote mode the orchestrator handles AI + routing, the executor handles QEMU directly, and the client connects over HTTPS/SSH tunnel.

---

## Quick Start

### Clone

Pick the version that matches your setup:

```bash
# Both (local mode — AI + QEMU on the same machine)
git clone https://github.com/tinshemet/gorgon.git

# Orchestrator only (AI/Ollama machine)
git clone --filter=blob:none --sparse https://github.com/Tinshemet/gorgon.git
cd gorgon
git sparse-checkout set files/orchestrator files/shared files/complementary

# Executor only (QEMU execution machine)
git clone --filter=blob:none --sparse https://github.com/Tinshemet/gorgon.git
cd gorgon
git sparse-checkout set files/executor files/shared files/complementary
```

After cloning, run the matching setup script:

| Version | Setup script |
|---|---|
| Local (all-in-one) | `bash files/complementary/install.sh` |
| Orchestrator only | `bash files/complementary/install_orchestrator.sh` |
| Executor only | `bash files/complementary/install_executor.sh` |
| Client only | `bash files/complementary/setup_client.sh` |

---

### One-Command Install

```bash
cd ~/path/to/gorgon
bash files/complementary/install.sh
```

Handles automatically:
- `apt` packages: `qemu-kvm`, `qemu-utils`, `ovmf`, `virt-viewer`, `socat`, `cpu-checker`, `libguestfs-tools` (offline disk credential edits — see [Golden-Image Templates](#golden-image-templates))
- KVM group membership
- Python venv at `~/qemu-env` with `requests psutil rich fastapi uvicorn httpx`
- Ollama install + `qwen2.5:7b` model pull
- `systemd` user service (Ollama auto-starts on login)
- Bridge networking config `/etc/qemu/bridge.conf`
- Shell alias `gorgon`
- Self-test
- **Out-of-the-box presets** (executor setup, so also covered by the full `install.sh`):
  - Bundled default hardware profiles copied into `~/.qemu_vms/_profiles/` (skipped if a same-named file already exists — never overwrites your own)
  - `template-kali` / `template-ubuntu` / `template-windows` VM shells scaffolded with unattended install pre-configured, ready to launch. Best-effort: needs the matching ISO already at its expected path to fully wire up (ISOs aren't bundled — you provide those); otherwise the shell is still created, just without install media, and `create_vm(..., force=true)` re-attaches it once the ISO's in place. Idempotent — won't touch a template that already exists.

To uninstall: `bash files/complementary/install.sh --uninstall`

### Manual Setup

```bash
sudo apt install qemu-kvm qemu-utils ovmf virt-viewer socat python3-venv
python3 -m venv ~/qemu-env
source ~/qemu-env/bin/activate
pip install requests psutil rich fastapi uvicorn httpx

# Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull qwen2.5:7b
```

Add to `~/.zshrc` / `~/.bashrc`:

```bash
source ~/qemu-env/bin/activate
export OLLAMA_MODEL=qwen2.5:7b
export SERVER_URL="http://localhost:8080"
export API_TOKEN="$(cat ~/.gorgon.token 2>/dev/null || echo '')"
alias gorgon-serve='~/start-gorgon-server.sh'
alias gorgon='PYTHONPATH=~/path/to/gorgon/files python3 ~/path/to/gorgon/files/client/client_wrapper.py'
```

### Run (Local Mode)

```bash
gorgon              # AI chat mode
gorgon -v           # verbose (shows raw tool calls)
gorgon -cu          # custom machine mode (skip product verification)
gorgon list         # direct CLI, no AI
gorgon system       # check system capabilities
gorgon -tf <name>   # fingerprint report
```

---

## Architecture

### Five-Layer Protection

```
User Input
    ↓
qwen2.5:7b (Ollama)               orchestrator/ai/ollama_client.py
Selects tool, builds args
    ↓
_sanitise_args()                   orchestrator/sanitizer/sanitizer.py
Type coercion, enum normalisation, path fixing,
placeholder removal, resource caps, arch checks
    ↓
_preflight_check()                 orchestrator/preflight/validator.py
+ _validate_with_internet
+ _validate_profile_for_host
Reality check: ok / auto_fix / ask_user / abort
    ↓
dispatch_tool()                    shared/executioner/tool_executor.py
Hard guards, arch mismatch, name validation,
Windows UEFI enforcement, clarify responses
    ↓
QemuManager                        executor/api/qemu_manager.py
Actual QEMU/KVM operations
```

### File Structure

```
files/
├── orchestrator/                    Orchestrator machine (AI + routing)
│   ├── ai/
│   │   ├── cli.py                   Chat loop, process_message(), direct sub-command CLI
│   │   ├── ollama_client.py         Ollama HTTP client + system prompt builder
│   │   ├── session.py               Conversation history (load/save/clear)
│   │   ├── tools.py                 TOOLS list — AI tool definitions
│   │   ├── context_assistant.py     Context gate for AI-aware preflight decisions
│   │   └── config.json              AI-layer config (Ollama URL, model, loop limits)
│   ├── http/
│   │   └── api_server.py            FastAPI: /chat /execute /health /images /events /rotate-token
│   ├── sanitizer/
│   │   ├── sanitizer.py             _sanitise_args() — coercion, normalisation, caps
│   │   ├── context_gate.py          Gate check for context-aware tool calls
│   │   ├── config.json              Sanitizer config (placeholder names, enum maps)
│   │   └── context_gate_config.json Context gate rules per tool
│   ├── preflight/
│   │   ├── validator.py             _preflight_check(), internet validator, profile check
│   │   └── config.json              Preflight config (timeout, caps)
│   ├── pipeline.py                  execute_tool() — sanitize → gate → name resolution → dispatch (full local-mode pipeline)
│   ├── executor_client.py           Dispatches tool calls: local in-process (via pipeline.py) or HTTP to executor
│   ├── event_log.py                 Structured event log (JSON-lines, 10 MB rotation → ~/.qemu_vms/events.log)
│   └── connection_config.json       Orchestrator connection settings (url, token, timeout)
│
├── executor/                        Executor machine (QEMU engine)
│   ├── api/
│   │   ├── qemu_config.py           Config dataclasses, hardware profiles, OVMF detection
│   │   ├── qemu_manager.py          QemuManager: composes the mixin modules below
│   │   ├── _vm_constants.py         Shared constants (paths, defaults) for all mixins
│   │   ├── _vm_lifecycle.py         create_vm, delete_vm, clone_vm, ISO auto-detect
│   │   ├── _vm_operations.py        launch_vm, stop_vm, resize_disk, update_config
│   │   ├── _vm_stealth.py           Stealth launch, guest setup script generation
│   │   ├── _vm_monitoring.py        vm_status, list_vms, monitor_vm, fingerprint
│   │   ├── _vm_runtime.py           QMP socket, send_monitor_cmd, VM watcher loop
│   │   ├── qemu_arg_builder.py      Builds the QEMU command-line argument list
│   │   ├── qmp_client.py            QMP socket client
│   │   ├── network_manager.py       Isolated network (bridge) management
│   │   └── vm_state.py              State persistence (PID tracking, .state.json)
│   ├── server.py                    Standalone FastAPI executor: /health /tools /status /execute
│   ├── config.json                  Executor server config (host, port, token)
│   └── fingerprint.py               -tf fingerprint report (inxi simulation)
│
├── client/                          User's laptop (thin UI)
│   ├── ui/
│   │   └── chat_client.py           Curses fullscreen chat TUI — POSTs to orchestrator /chat
│   ├── cli/
│   │   └── commands.py              Direct local QEMU commands (no AI)
│   ├── client_wrapper.py            Entry point: chat UI, local commands, or -cu/-tf/-cs; reads CLI_config.json
│   ├── CLI_config.json              Client TUI appearance (text_color hex, font_size)
│   └── connection_config.json       Client settings (server_url, token, ca_cert)
│
├── admin/                           Any machine that can reach the orchestrator over HTTP
│   ├── admin_tui.py                 Curses fullscreen admin dashboard (gorgon-admin) — HTTP-only, no local QEMU needed
│   ├── admin_config.json            Admin TUI appearance (text_color hex, font_size, refresh rate)
│   └── connection_config.json       Admin connection settings (orchestrator_url, token)
│
├── shared/                          Bridge code (used by both sides in local mode)
│   ├── executioner/
│   │   ├── tool_executor.py         dispatch_tool() + _run() — executor-only, no orchestrator imports
│   │   └── config.json              Executor config (VM base dir, timeouts, ISO keywords)
│   └── display.py                   Rich console rendering (tables, panels, VNC panel)
│
├── tests/
│   ├── layer1_sanitizer.py          Layer 1: sanitizer unit tests
│   ├── layer2_executor.py           Layer 2: executor + preflight
│   ├── layer3_ai.py                 Layer 3: AI integration (needs Ollama)
│   ├── layer4_profiles.py           Layer 4: random profile + HTTP tests
│   ├── layer5_property.py           Layer 5: property-based invariant tests
│   ├── layer6_context_gate.py       Layer 6: context gate tests
│   ├── layer7_context_assistant.py  Layer 7: context assistant tests
│   ├── layer8_pipeline.py           Layer 8: full pipeline tests
│   ├── layer9_pipeline_gated.py     Layer 9: gated pipeline tests
│   ├── layer10_pipeline_full.py     Layer 10: full pipeline integration
│   ├── layer11_remote_split.py      Layer 11: orchestrator/executor HTTP boundary (19 tests)
│   └── test_api.py                  11-layer test suite entry point
│
└── complementary/
    ├── install.sh               Full local setup (Ollama + QEMU + venv + alias)
    ├── install_orchestrator.sh  Orchestrator machine setup (Ollama + API server)
    ├── install_executor.sh      Executor machine setup (QEMU + executor server)
    ├── setup_client.sh          Client setup (your laptop — thin UI only)
    ├── install_admin.sh         Admin dashboard setup (any machine, HTTP-only)
    ├── setup_wsl2.ps1           Windows-side WSL2 port forwarding (run as Admin)
    ├── requirements.txt         Python dependencies
    ├── GUIDE.txt                Complete reference guide
    └── handbooks/               Deep-dive reference (dictionary, workflow, files, config, tests)
```

**Test results: 259/259 (100%)**

---

## Local Mode vs Remote Mode

### Local Mode (single machine)

Everything runs on one machine — the server hosts AI + QEMU and the user runs the chat client pointed at `localhost`.

```
┌──────────────────────────────────────────────────────────────┐
│                      One machine                             │
│                                                              │
│  client/ui/chat_client.py  (chat_loop)                      │
│    ↓ user types a prompt  →  POST /chat  (localhost)         │
│  orchestrator/http/api_server.py  (/chat endpoint)          │
│    ↓ calls process_message()                                 │
│  orchestrator/ai/ollama_client.py                           │
│    ↓ Ollama returns a tool call                              │
│  orchestrator/sanitizer/sanitizer.py  (_sanitise_args)     │
│    ↓ args cleaned and normalised                             │
│  orchestrator/preflight/validator.py  (_preflight_check)   │
│    ↓ full state check (real VM existence, real disk)         │
│  shared/executioner/tool_executor.py  (dispatch_tool)      │
│    ↓ hard guards, then dispatch                              │
│  executor/api/qemu_manager.py  (QemuManager)               │
│    → runs QEMU/KVM directly                                  │
└──────────────────────────────────────────────────────────────┘
```

Or run the client wrapper directly (single machine, server must be running):

```bash
PYTHONPATH=files python3 files/client/client_wrapper.py
```

### Remote Mode (server + thin client)

```
┌───────────────────────────┐          ┌──────────────────────────────────────┐          ┌──────────────────────────┐
│   User's laptop (client)  │          │   Orchestrator machine               │          │   Executor machine       │
│                           │          │   (AI + routing)                     │          │   (QEMU engine)          │
│  client/ui/chat_client.py │          │                                      │          │                          │
│    ↓ user types a prompt  │  HTTPS   │  orchestrator/http/api_server.py    │  HTTP    │  executor/server.py      │
│    POST /chat  ───────────┼─────────►│    ↓ process_message()               ├─────────►│    ↓ dispatch_tool()     │
│                           │          │  orchestrator/ai/ollama_client.py   │          │  executor/api/           │
│    ← {text, tool_results} │          │    ↓ Ollama tool call                │◄─────────┤  qemu_manager.py         │
│    needs_input? → confirm │          │  orchestrator/sanitizer             │          │    → runs QEMU/KVM       │
│    re-POST /chat ─────────┼─────────►│  orchestrator/preflight              │          │                          │
│                           │          │  orchestrator/executor_client.py    │          │  GET /health /tools      │
│  Rich panels rendered     │          │                                      │          │      /status /execute    │
│  locally from tool_results│          │  GET /health  /images  /rotate-token │          │                          │
│                           │◄─────────┤                                      │          │                          │
└───────────────────────────┘          └──────────────────────────────────────┘          └──────────────────────────┘
```

In local mode (`"url": "local"` in `orchestrator/executor_client.py`), the executor call is made in-process — no HTTP hop, no separate executor machine needed.

Session state (conversation history) lives on the server, keyed by `session_id`. The client needs no Ollama installation.

### Setup Scripts — Which One to Run

| Script | Run on | Purpose |
|---|---|---|
| `install.sh` | Your machine | Full local setup — QEMU + Ollama + HTTP API + everything |
| `install_orchestrator.sh` | Orchestrator machine | Ollama + HTTP API (uvicorn on port 8080); no QEMU needed |
| `install_executor.sh` | Executor machine | QEMU + executor server (uvicorn on port 8001); no Ollama needed |
| `setup_client.sh` | Your laptop | Thin client only — Python + Rich + connection config |
| `install_admin.sh` | Any machine (optional) | Admin dashboard — HTTP-only, no QEMU/Ollama needed |
| `setup_wsl2.ps1` | Windows (as Admin) | Port forwarding + firewall for WSL2 setup |

Typical three-machine scenario:

```bash
# On the orchestrator machine:
bash files/complementary/install_orchestrator.sh

# On the executor machine:
bash files/complementary/install_executor.sh

# On the executor machine (if Windows+WSL2, in Admin PowerShell):
.\files\complementary\setup_wsl2.ps1

# On your laptop:
bash files/complementary/setup_client.sh
```

`install_orchestrator.sh` asks for the executor's URL and token (`EXECUTOR_URL`/`EXECUTOR_TOKEN`) partway through, so it can be a chicken-and-egg problem if you run it before `install_executor.sh` has generated a token — just press Enter to skip and edit `orchestrator/connection_config.json`'s `url`/`token` fields by hand once the executor is set up (the script tells you exactly which fields).

Pre-seed to skip prompts:

```bash
API_TOKEN=mysecret bash install_orchestrator.sh
EXECUTOR_URL=http://192.168.1.20:8001 EXECUTOR_TOKEN=executorsecret bash install_orchestrator.sh
EXECUTOR_TOKEN=executorsecret bash install_executor.sh
SERVER_URL=http://192.168.1.10:8080 API_TOKEN=mysecret bash setup_client.sh
```

Note `API_TOKEN` (client→orchestrator) and `EXECUTOR_TOKEN` (orchestrator→executor) are two separate secrets — don't reuse one for the other.

### Starting Remote Mode

```bash
# On the orchestrator machine — start the HTTP API
source ~/qemu-env/bin/activate
cd ~/gorgon
API_TOKEN=mysecrettoken PYTHONPATH=files uvicorn orchestrator.http.api_server:app --host 0.0.0.0 --port 8080

# On the executor machine — start the executor server
source ~/qemu-env/bin/activate
cd ~/gorgon
PYTHONPATH=files uvicorn executor.server:app --host 0.0.0.0 --port 8001

# Or use the alias created by install_orchestrator.sh:
gorgon-serve

# Admin TUI — runs on any machine that can reach the orchestrator over HTTP
# (install separately: bash files/complementary/install_admin.sh):
gorgon-admin

# On your laptop — open SSH tunnel (if connecting over the internet)
ssh -N -L 8080:127.0.0.1:8080 -L 5901:127.0.0.1:5901 user@server-ip

# On your laptop — start the chat client
export SERVER_URL=http://localhost:8080   # (or LAN IP without tunnel)
export API_TOKEN=mysecrettoken
gorgon
```

### Cross-Platform Support

| Side | Linux | macOS | Windows |
|---|---|---|---|
| **Provider** (Ollama + chat) | Full | Full | Full (PowerShell) |
| **Client** (QEMU + VMs) | Full | Partial (HVF, no KVM) | WSL2 required |

If your friend's machine is Windows, the cleanest path is WSL2 (if their hardware supports KVM passthrough) or dual-booting Linux.

---

## CLI Reference

Add `-v` to any command for verbose/raw JSON output.

```bash
# Global flags
gorgon -cu <command>         # custom mode — skip product verification
gorgon -v <command>          # verbose output

# VMs
gorgon list                  # list all VMs
gorgon status <name>         # status
gorgon monitor <name|all>    # activity report
gorgon launch <name>         # start VM (SDL window, direct local engine)
gorgon launch <name> sdl     # SDL display (local window, no server needed)
gorgon launch <name> vnc     # VNC display → connect with vncviewer localhost:5900
gorgon launch <name> gtk     # GTK display
gorgon stop <name>           # stop VM
gorgon clone <src> <new>     # clone VM
gorgon config <name>         # show config JSON
gorgon resize <name> <gb>    # resize primary disk
gorgon delete <name>         # delete VM (asks confirmation)
gorgon logs <name>           # failure diagnosis
gorgon show-cmd <name>       # print QEMU launch command
gorgon -tf <name>            # fingerprint report (inxi simulation)

# Snapshots
gorgon snapshot list <vm>
gorgon snapshot create <vm> <snap>
gorgon snapshot restore <vm> <snap>
gorgon snapshot delete <vm> <snap>

# Networks
gorgon network list
gorgon network create <name>
gorgon network delete <name>
gorgon network add <net> <vm>
gorgon limit <vm> <cpu%> [mem_mb]

# Info
gorgon profiles
gorgon check-profile <name>
gorgon system
gorgon isos
gorgon cmd <vm> "<qemu monitor cmd>"

# Session
gorgon clear-session

# Remote mode only
gorgon serve [host] [port] [--cert cert.pem --key key.pem]
gorgon fetch <vm> [--out /dir]
```

---

## AI Chat Reference

**Built-in shortcuts (no AI, instant):**

| Command | What it does |
|---|---|
| `list` / `vms` / `ls` | List all VMs |
| `system` | System capabilities |
| `profiles` | Hardware profiles |
| `templates` | Golden-image templates |
| `drift` | Configuration drift check |
| `kill <name>` / `force stop <name>` | Force-kill a VM (SIGKILL), with confirm |
| `clear session` / `forget` | Clear conversation history |
| `help` / `?` | Show all shortcuts and example prompts |
| `exit` / `quit` / `q` | Exit |

**Example prompts:**

```
# VM creation
"create a Linux VM called dev-box with 4GB RAM and launch it"
"create a Windows 11 VM called win-test, ISO from Desktop Images, 8GB RAM"
"create a headless Ubuntu server called ci-runner, 2 cores, 2GB RAM"

# Hardware profiles
"create a VM modelled after a Dell G15 5520 called gaming-rig, 16GB RAM"
"use the office laptop profile, Windows 11, call it work-vm"

# Stealth / fingerprinting
"create a VM with manufacturer Dell Inc., product Latitude 5530, 8GB RAM, stealth"
"run the fingerprint report on win-test"

# Raspberry Pi / ARM
"create a Raspberry Pi 3B VM called rpi-test"

# Unattended install / golden-image templates
"create a Kali VM called template-kali, unattended install"
"create a Windows 11 VM called template-windows, unattended install, skip user creation"
"mark template-kali as a template"
"create a vm called test, give it the template-kali template disk, randomize the root and user password"
"list my templates"

# Monitoring
"list all my VMs and tell me which are running"
"why did dev-box fail? check its logs"

# Snapshots
"take a snapshot of win-test called before-update"
"restore win-test to snapshot before-update"

# Resources
"limit win-test to 50% CPU and 2GB RAM"
"resize dev-box disk to 100GB"
"clone dev-box into dev-box-staging"

# Networking
"create an isolated network between dev-box and ci-runner"

# Teardown
"stop all VMs, delete dev-box and win-test with disk files"

# Remote
"create a VM on the remote machine and show me how to connect via VNC"
"download the win-test disk image to my local machine"
```

---

## VM Configuration Fields

| Field | Type | Description |
|---|---|---|
| `name` | string | VM name (unique, alphanumeric + `-_`) |
| `os_type` | string | `linux`, `windows`, `macos`, `other` |
| `os_name` | string | Distro name for ISO auto-detection (`mint`, `kali`, etc.) |
| `memory_mb` | int | RAM in MB (min 512, max 95% of host) |
| `cpu_cores` | int | CPU cores (max = host count) |
| `cpu_threads` | int | Threads per core |
| `cpu_model` | string | QEMU CPU model (`host`, `kvm64`, `Haswell`, etc.) |
| `disk_size_gb` | int | Primary disk size in GB (min 8) |
| `disk_format` | string | `qcow2` (default) or `raw` |
| `disk_bus` | string | `virtio` (default), `sata`, `ide` |
| `disk_model` | string | Custom disk model string |
| `machine_type` | string | `q35` (default), `pc`, `virt` (ARM) |
| `machine_class` | string | `laptop`, `desktop`, `server` — sets chassis type byte |
| `display` | string | `sdl`, `gtk`, `vnc`, `none` |
| `gpu` | string | `virtio-vga`, `vmware-svga`, `std`, `qxl` |
| `audio` | string | `hda` (default), `none` |
| `bios` | string | `seabios`, `ovmf`, `ovmf_ms` (Secure Boot) |
| `uefi` | bool | Enable UEFI firmware |
| `tpm` | bool | Enable swtpm TPM 2.0 |
| `kvm` | bool | Enable KVM hardware acceleration (default `true`) |
| `stealth` | bool | Enable hardware fingerprint spoofing |
| `hardened` | bool | Hide KVM hypervisor flags from guest (`stealth` implies this) |
| `battery` | bool | Expose ACPI battery device |
| `hugepages` | bool | Use huge pages for memory |
| `network_mode` | string | `nat` (default), `bridge` |
| `bridge_iface` | string | Bridge interface name for bridge mode |
| `mac_address` | string | Custom MAC address |
| `manufacturer` | string | SMBIOS type-1 manufacturer |
| `product_name` | string | SMBIOS type-1 product name |
| `serial_number` | string | SMBIOS type-1 serial number |
| `board_product` | string | SMBIOS type-2 baseboard product |
| `bios_vendor` | string | SMBIOS type-0 BIOS vendor |
| `bios_version` | string | SMBIOS type-0 BIOS version |
| `smbios_type` | string | Chassis type string (`Notebook`, `Desktop`, `Server`) |
| `chassis_type` | string | Alias for `smbios_type` |
| `profile` | string | Apply a named hardware profile |
| `iso_path` | string | Path to installation ISO |
| `extra_args` | list | Raw QEMU args appended to the command |
| `overwrite` | bool | Delete and recreate if VM already exists |
| `unattended` | bool | Fully automate OS install ([details](#unattended-os-install)). Destructive — wipes the disk, asks confirmation |
| `unattended_username` / `unattended_password` / `unattended_locale` / `unattended_autologon` | — | Windows unattended overrides (defaults: `user` / `Passw0rd!` / `en-US` / autologon on) |
| `unattended_skip_user` | bool | Windows: automate everything except account creation — stops at the sign-in screen |
| `template` | string | Clone disk(s) from a golden image instead of creating blank ones ([details](#golden-image-templates)) |
| `randomize_root_password` / `randomize_user_password` | bool | Offline-randomize the clone's root/primary-user password (Linux templates only) |
| `new_username` | string | Offline-rename the clone's primary user account (Linux templates only) |

### Sanitizer auto-corrections

The sanitizer silently fixes many common AI mistakes before they reach execution:

- **VM names:** spaces → underscores; placeholder names cleared (`windows-vm`, `linux-vm`, `my-vm`, `test-vm`, `vm`, `unnamed`, etc.)
- **OS type aliases:** `ubuntu/mint/kali` → `linux`, `win10/win11` → `windows`, `osx/darwin` → `macos`
- **Enum normalisation:** `NAT→nat`, `SDL→sdl`, `x11→sdl`, `alsa→hda`, `wifi→nat`
- **Machine type:** profile names stripped from machine_type, profile field auto-set
- **ARM CPUs on x86:** `cortex-a53`, `cortex-a72` etc. → `host`
- **Bridge interfaces:** physical NIC names → `virbr0`
- **MAC addresses:** 7-octet MACs removed
- **Resource caps:** memory min 512MB, max 95% host; CPU max = host cores; disk min 8GB
- **ISO paths:** template strings removed; `/home/user/` → real home dir

---

## Hardware Profiles

Built-in profiles auto-apply CPU, RAM, display, audio, and SMBIOS defaults:

| Profile | Description |
|---|---|
| `dell_g15_5520` | Dell G15 Gaming Laptop — 16GB, battery, hda audio |
| `gaming_desktop` | High-performance desktop — 32GB, virtio-vga-gl |
| `office_laptop` | ThinkPad-style office — 8GB, qxl, battery |
| `server` | EPYC headless — 64GB, hugepages, iommu |
| `mac_mini` | macOS-style — 8GB, vmware-svga |
| `minimal` | 2-core 2GB headless (CI/testing) |
| `raspberry_pi_4` | ARM64 emulated Pi 4 — cortex-a72, 4GB |
| `raspberry_pi_3b` | ARM64 emulated Pi 3B — cortex-a53, 1GB, serial only |

Custom profiles stored at `~/.qemu_vms/_profiles/<name>.json`. Validated against your host on first use.

---

## Unattended OS Install

`create_vm(..., unattended=true)` fully automates OS installation — no clicking through installer screens. **Destructive** (wipes the target disk) — preflight asks for confirmation first (`force=true` skips the prompt, same as `delete_vm`).

Code lives in `files/executor/api/autoinstall/` (`windows.py`, `linux.py`, `templates/` — its own package specifically so you know where to look to add a custom answer file or a new distro; see that package's `__init__.py` for exact instructions).

### Windows

Generates an `autounattend.xml` answer-file ISO + a bootable FAT USB image (the FAT's `startup.nsh` auto-launches Setup even if you miss the "press any key" prompt). Automates disk partitioning, the hardware-check bypass (TPM/Secure Boot/RAM requirements), language screen, and OOBE.

By default it's **fully hands-off**, including account creation (`unattended_username`/`unattended_password`, default `user`/`Passw0rd!`):

```
"create a Windows 11 VM called win-test, unattended install"
```

Pass `unattended_skip_user=true` to automate everything **except** the account — Setup stops at the normal "Who's going to use this device" sign-in screen instead of auto-creating one. This is what you want when the disk is destined to become a [golden-image template](#golden-image-templates), so each clone gets its own account:

```
"create a Windows 11 VM called template-windows, unattended install, skip user creation"
```

### Linux

Direct-kernel-boots the installer (`-kernel`/`-initrd`/`-append` — the only way to pass an autoinstall/preseed kernel parameter) with a per-distro mechanism, configured in `unattended_linux` in `executor/api/config.json`:

| Distro | Installer family | Mechanism |
|---|---|---|
| Ubuntu Desktop | `casper` (Subiquity) | cloud-init NoCloud volume (`cidata.iso`) with `interactive-sections: [identity]` — automates everything, stops at "Create your account" |
| Kali | `debian-installer` | preseed injected directly into the initrd (concatenated gzip-cpio archive); `passwd/*` left unset so account creation still prompts |
| Linux Mint | `ubiquity` | **not currently supported** — Mint ships Ubiquity, not Subiquity, and Ubiquity doesn't read the autoinstall/cloud-init mechanism at all. Preseed injection pre-fills every wizard page correctly, but the GUI still needs manual clicks through each page. Known gap, not actively worked on. |

```
"create an Ubuntu VM called template-ubuntu, unattended install"
"create a Kali VM called template-kali, unattended install"
```

Both Ubuntu and Kali stop at account creation the same way Windows does with `unattended_skip_user` — no separate flag needed, it's how their installers work.

### How the VM knows the install finished

A VM using direct kernel boot re-triggers the installer on **every** boot until something clears `kernel_path`/`initrd_path`/`iso_path` — including a guest-initiated reboot right after the install finishes, which would otherwise re-run the same destructive auto-partitioning. `launch_vm` checks this automatically: a cheap disk-size pre-filter, then a real read-only check for `/etc/os-release` on the disk (partitioning alone can't produce that file, so this can't false-positive mid-install the way a size-only check can). Confirmed installed → clears the installer fields so all future launches boot the installed OS normally.

---

## Golden-Image Templates

Turn an already-set-up VM into a reusable base image, then clone new VMs from it in seconds instead of reinstalling every time.

```
"mark template-kali as a template"
"create a vm called test, give it the template-kali template disk"
"list my templates"
"remove the template mark from template-kali"
```

### Workflow

1. **Set up a source VM** the way you want the golden image to look. For [unattended installs](#unattended-os-install) destined to become templates, use `unattended_skip_user=true` (Windows) so the install finishes but account creation is left for a human — connect via VNC, create the account, then stop the VM.
   - **Ubuntu-specific gotcha:** unlike Windows and Kali, Subiquity doesn't actually write anything to the target disk until *after* the identity step resolves — there's no "mark before account creation" shortcut for Ubuntu. Always check disk size (`du -sh ~/.qemu_vms/<name>/disk0.qcow2`) before marking; a few hundred KB means nothing installed yet regardless of which screen is showing.
2. **Stop the VM gracefully** (`stop_vm` without `force` — a real ACPI shutdown, not a hard kill, so the golden image doesn't inherit a dirty-shutdown filesystem state).
3. **`mark_as_template(name)`** — flattens the VM's disk(s) (`qemu-img convert`, not a backing-file link, so the template never depends on the source VM's disk surviving) into `~/.qemu_vms/_templates/<name>/`. Tags both the source VM and the template copy with the protected `template` label.
4. **`create_vm(name=..., template=<name>)`** — clones fresh VM(s) from the template's disk(s) as QCOW2 backing files. No installer ISO gets attached (even if one would normally auto-match) — a template clone already has a real, bootable OS, and attaching an install ISO risks the VM booting the installer's boot menu instead of the cloned OS.
5. **`remove_template(name)`** (asks Yes/Cancel first) — deletes the template's disk copy and un-tags the source VM if it still exists.
6. **`list_templates()`** — mirrors `list_profiles()`; also reachable as the `templates` shortcut in chat/CLI.

### Per-clone credential handling

Every clone inherits the template disk's `/etc/shadow` byte-for-byte — same root password, same account password, same everything, unless you change it. These `create_vm` args offline-edit the *new* disk before the VM ever boots (via `virt-customize`/libguestfs — no boot required, needs `libguestfs-tools` installed):

| Arg | Effect |
|---|---|
| `randomize_root_password` | Sets a fresh random root password on the clone. Returned in the result message/`root_password` field **once** — also saved into the clone's own `config.json` (`show_config <name>` to retrieve it later). |
| `randomize_user_password` | Same, for the disk's primary (non-root) account — auto-detected as the first `/etc/passwd` entry with UID ≥ 1000. |
| `new_username` | Renames that primary account to whatever you specify (`usermod -l`/`groupmod -n` run against the guest's own files, home directory moved too). Runs *before* `randomize_user_password` in the same call, so combining both targets the renamed account correctly. |

```
"create a vm called clone1 from the template-kali template, randomize the root and user password"
"create a vm called clone2 from template-kali, rename the user to alice"
```

**Linux only.** Windows credentials live in the SAM registry hive, not `/etc/shadow` — needs a different tool (`chntpw`), not yet built.

### Notes

- Offline username/password edits require the VM to be **stopped** (libguestfs needs exclusive access to the disk file).
- `mark_as_template`/`remove_template` are gated behind the same Yes/Cancel confirmation pattern as `delete_vm`.
- The reserved `template` label and the `_templates/` folder path are config-driven (`template_label`/`dirs.templates` in `executor/api/config.json`) — not hardcoded.

---

## Flags: -cu, -tf, -cs

All three work from the real `gorgon` client entry point in both local and split mode — `client/client_wrapper.py` uses a local orchestrator/executor install when present, and otherwise falls back to the configured `SERVER_URL`/`API_TOKEN` over HTTP.

### `-cu` — Custom Machine Mode

```bash
gorgon -cu
```

Starts the AI chat with product verification disabled. Allows any `manufacturer`/`product_name` combination including fictional hardware.

**What it disables:**
- DuckDuckGo product lookup (manufacturer + product_name no longer verified)
- Memory plausibility check

**What still runs:** QEMU binary checks, ARM/x86 consistency, all other preflight, sanitizer.

Use cases: fictional hardware names, research VMs, air-gapped environments.

In split mode this calls `POST /custom-mode` on the orchestrator (see [API Endpoints](#api-endpoints)). It's a **process-global** toggle — it affects every client talking to that orchestrator, not just the one that set it.

### `-cs` — Clear Session

```bash
gorgon -cs
```

Clears the saved chat session (`~/.qemu_vms/.chat_session_id`) before starting, so the AI begins with no prior conversation history.

### `-tf` — Fingerprint Report

```bash
gorgon -tf <vmname>
```

Read-only analysis of a VM's configuration. Simulates what `inxi -M -N -C -D -A -G` would report from inside the guest OS, then checks each field against known VM fingerprint signatures.

In split mode, the full per-field breakdown table is only rendered where the executor tool actually runs — a remote `-tf` call gets back the structured score/tell counts, not the rendered table (that only appears when run directly on a machine with local QEMU).

**What it simulates:**

| inxi flag | What it reads |
|---|---|
| `-M` | System manufacturer, product, BIOS vendor, BIOS version, serial, chassis type |
| `-N` | MAC address OUI prefix, NIC driver type |
| `-C` | Hypervisor flag in /proc/cpuinfo, CPU model string |
| `-D` | Disk interface name, disk model string |
| `-A` | Audio chip device ID |
| `-G` | GPU driver type |

Each field is rated: `clean` / `detectable` / `VM tell`. Output includes a score and a recommendations panel. This tool **never modifies** the VM config.

**Known VM tells and how to reduce them:**

| Tell | Fix |
|---|---|
| MAC OUI `52:54:00` | Set `mac_address` to a non-QEMU OUI |
| `QEMU HARDDISK` | Add model= and serial= to extra_args |
| `virtio-gpu` | Use `vga` or `std` display instead |
| `virtio-net` | Use `e1000` network model |
| BIOS vendor/version | Set `bios_vendor` and `bios_version` |
| KVM flags | Set `kvm=false` (loses performance) |

---

## Stealth Mode

Enable with `"stealth": true` in VM config. `"hardened": true` is implied and applied automatically.

### What the API does automatically at launch

| Feature | Effect |
|---|---|
| Display device | Linux: `vmware-svga` (loads `vmwgfx`, no "qemu" in module name). Windows: standard `VGA` |
| CPUID (hardened) | Hides `-hypervisor` bit, `kvm=off`, `-vmx` — guest cannot detect KVM |
| ACPI OEM ID | Set to manufacturer name (e.g. `Dell I`) instead of `BOCHS` |
| USB controller | `nec-usb-xhci` (real NEC PCI IDs) instead of `qemu-xhci` |
| Guest setup | Auto-serves `guest_setup.sh` / `guest_setup.ps1` via HTTP on port 8080 on first launch |

The `.stealth_done` sentinel file in the VM directory suppresses the HTTP server on subsequent launches.

> **Important:** Never add `smm=off` to `extra_args` when using OVMF. OVMF requires SMM enabled or the Linux boot KVM-crashes at approximately 15 seconds.

---

## SMBIOS / Hardware Fingerprint

These fields map directly to SMBIOS tables visible via `inxi -F`, `dmidecode`, and Windows System Information:

```json
{
  "manufacturer":  "Dell Inc.",
  "product_name":  "Latitude 5530",
  "serial_number": "3K8LP52",
  "board_product": "0H34YX",
  "bios_vendor":   "Dell Inc.",
  "bios_version":  "1.15.0",
  "machine_class": "laptop",
  "smbios_type":   "Notebook"
}
```

**Chassis type mapping:**

| Value | SMBIOS chassis type byte |
|---|---|
| `laptop` / `notebook` | 9 |
| `desktop` | 3 |
| `server` | 17 |
| `tablet` | 30 |

**How the chassis type byte is injected:** QEMU's CLI cannot set the SMBIOS type-3 chassis byte directly. The API writes a raw binary file (`smbios_chassis.bin`) and passes it to QEMU via `-smbios file=`. Linux's DMI scanner uses last-write-wins, so the appended entry overrides QEMU's default.

After setting these fields, `inxi -F` inside the guest shows:
```
System:    Type: Laptop  System: Dell Inc.  product: Latitude 5530  serial: 3K8LP52
BIOS:      Dell Inc.     v: 1.15.0
```

---

## Guest Setup Scripts

On first launch of a stealth VM, the API automatically:
1. Generates `guest_setup.sh` (Linux) or `guest_setup.ps1` (Windows) in the VM directory
2. Serves it via HTTP on port 8080 from the host
3. Prints the command to run inside the VM

### Linux Guest Setup

Supports: Ubuntu, Linux Mint, Kali, Arch — any distro with `update-initramfs`, `mkinitcpio`, or `dracut`.

```bash
curl http://10.0.2.2:8080/guest_setup.sh | bash
```

**Step 1 — Blacklist QEMU kernel modules:**
Creates `/etc/modprobe.d/blacklist-qemu.conf`, rebuilds initramfs. Reboot required.

**Step 2 — Firefox stealth profile:**
Creates `~/.mozilla/firefox/stealth/user.js`:
```javascript
user_pref("webgl.renderer-string.override", "Intel(R) Iris(R) Xe Graphics");
user_pref("webgl.vendor-string.override",   "Intel");
```

**Step 3 — Stealth browser launcher:**
`~/Desktop/stealth-browser.sh` — launches Firefox with the stealth profile. Auto-detects `firefox` vs `firefox-esr` (Kali).

**Step 4 — lspci / lsmod wrappers:**
- `lspci` wrapper: replaces VMware SVGA II → Intel Iris Xe in standard and `-mm` output
- `lsmod` wrapper: filters any module starting with `qemu`
- Originals preserved as `lspci.real` / `lsmod.real`. Idempotent.

**Verify after reboot:**
```bash
lsmod | grep qemu                          # empty
lspci | grep VGA                           # Intel Iris Xe
cat /sys/class/dmi/id/chassis_type         # 9
inxi -F                                    # Type: Laptop  System: Dell Inc.
```

### Windows Guest Setup

```powershell
# Run in elevated PowerShell (right-click → Run as Administrator)
irm http://10.0.2.2:8080/guest_setup.ps1 | iex
```

**Step 1 — Firefox stealth profile:** `%APPDATA%\Mozilla\Firefox\Profiles\stealth\user.js`

**Step 2 — Desktop shortcut:** `Stealth Browser.lnk` → Firefox with `--profile stealth --no-remote`

**Step 3 — GPU display name spoof (Admin required):**
- Strategy 1: edits `DriverDesc` in video class registry key `{4d36e968...}` — works with VMware/SVGA driver
- Strategy 2: sets `FriendlyName` in PCI enum key for `VEN_1234` — works with `basicdisplay.sys`

Reboot after running. Device Manager shows `Intel(R) Iris(R) Xe Graphics`.

**Mark stealth as done** (suppresses HTTP server on next launch — run on the **host**):
```bash
touch ~/.qemu_vms/<name>/.stealth_done
```

---

## Windows 11 Setup Workflow

### Pre-install config

```json
{
  "bios":     "ovmf_ms",
  "tpm":      true,
  "stealth":  true,
  "hardened": true,
  "networks": []
}
```

- `bios: "ovmf_ms"` — uses `OVMF_CODE_4M.ms.fd` with Microsoft Secure Boot keys (required for Windows 11)
- `networks: []` — removes the NIC so Windows OOBE skips Microsoft account

### BIOS values

| Value | File | Use case |
|---|---|---|
| `"ovmf"` | `OVMF_CODE_4M.fd` | Standard UEFI, no Secure Boot |
| `"ovmf_ms"` | `OVMF_CODE_4M.ms.fd` | Secure Boot with Microsoft keys (Windows 11) |

### OOBE local account bypass (Windows 11 25H2+ — `bypassnro` removed)

| Method | Steps |
|---|---|
| **Remove NIC (recommended)** | `networks: []` before install. OOBE shows "I don't have internet" → "Continue with limited setup" |
| **ms-cxh:localonly** | Shift+F10 at sign-in screen → `start ms-cxh:localonly` |
| **Fake email** | Enter `a@a.com` + any password → fails → "Create a local account" appears |
| **Sign-in options** | Bottom-left of sign-in screen → "Sign-in options" → "Domain join instead" |

### Post-install steps

1. Stop the VM
2. Add NIC back to `config.json`:
   ```json
   "networks": [{"mode":"nat","model":"e1000e","mac":"F0:1F:AF:XX:XX:XX"}]
   ```
3. Relaunch the VM
4. On host: `cd ~/.qemu_vms/<name> && python3 -m http.server 8080`
5. In elevated PowerShell: `irm http://10.0.2.2:8080/guest_setup.ps1 | iex`
6. Install Firefox, reboot
7. On host: `touch ~/.qemu_vms/<name>/.stealth_done`

---

## Bridge Networking

By default VMs use QEMU NAT (`10.0.2.x`). For a real LAN IP, create a bridge on the host once:

### Ubuntu / Debian (netplan)

```yaml
# /etc/netplan/01-bridge.yaml
network:
  version: 2
  ethernets:
    enp3s0:            # check your interface: ip link
      dhcp4: false
  bridges:
    br0:
      interfaces: [enp3s0]
      dhcp4: true
      parameters:
        stp: false
        forward-delay: 0
```

```bash
sudo netplan apply
```

### Linux Mint / Debian (`/etc/network/interfaces`)

```
auto br0
iface br0 inet dhcp
    bridge_ports enp3s0
    bridge_stp off
    bridge_fd 0
```

```bash
sudo systemctl restart networking
```

### VM config for bridge mode

```json
{
  "networks": [{"mode":"bridge","bridge":"br0","model":"e1000e"}]
}
```

Note: `hardened: true` forces NAT for non-stealth VMs, but stealth VMs can use bridge to get a real LAN IP.

---

## Remote Mode — Full Reference

### VNC in Remote Mode

When a VM is launched via the chat client or `/execute` API, the server always forces `display=vnc`. VMs bind to `0.0.0.0:<port>` (accessible on the host network) by default.

**Auto-connect:** the chat client automatically opens a VNC viewer after a successful launch and shows a connection panel:

```
╭─────────────────────── VM Display — localhost:5900 ───────────────────────╮
│  ✓ VNC viewer launched automatically                                      │
│  If the window didn't appear, connect manually:                           │
│    vncviewer localhost:5900                                                │
╰───────────────────────────────────────────────────────────────────────────╯
```

VNC viewers tried in order: `vncviewer`, `tigervncviewer`, `xtigervncviewer`, `gvncviewer`, `vinagre`. Install one with:
```bash
sudo apt install tigervnc-viewer
```

**Port assignment:** first VM uses display `:0` (port 5900), second `:1` (5901), etc. The port is also shown in `vm_status` output.

**SDL/GTK display (local window):** if you're on the same machine as the server, use the direct CLI instead:
```bash
gorgon launch <name> sdl    # opens an SDL window directly
```

**For a truly remote machine** (server ≠ your laptop), open an SSH tunnel:
```bash
ssh -N -L 5900:127.0.0.1:5900 user@server-ip
vncviewer localhost:5900
```

### Liveness Monitor

When `API_URL != "local"`, a background daemon thread pings `GET /health` every 30 seconds. If the client machine stops responding, a warning appears inline in the chat session without interrupting it.

### Fetch — Download VM Disk

```bash
gorgon fetch <vm_name>
gorgon fetch <vm_name> --out /external/backups/
```

1. Checks `GET /images/{vm_name}/sha256` — skips download if local file matches
2. Resumes with HTTP Range if local file exists but checksum differs
3. Downloads with progress indicator
4. Verifies final SHA256 after download

### Token Rotation

```bash
curl -X POST http://client:8080/rotate-token \
     -H "Authorization: Bearer currenttoken" \
     -H "Content-Type: application/json" \
     -d '{"new_token": "newlongsecrettoken"}'
```

New token persists to `~/.gorgon.token` and takes effect immediately without restart.

### Friend's House — SSH Tunnel Quickstart

```bash
# Step 1: Friend's machine (Linux/WSL2)
bash files/complementary/setup_client.sh
# If WSL2 on Windows, also (as Admin in PowerShell):
.\files\complementary\setup_wsl2.ps1

# Step 2: Your laptop
bash files/complementary/setup_server.sh
# Choose remote mode (2), enter http://localhost:8080 as API_URL

# Step 3: Open SSH tunnel (leave running in a terminal tab)
ssh -N \
    -L 8080:127.0.0.1:8080 \
    -L 5901:127.0.0.1:5901 \
    -L 5902:127.0.0.1:5902 \
    friendusername@203.0.113.42    # add -p 2222 for WSL2

# Step 4: Start the chat
gorgon

# Step 5: VNC connects through the already-open tunnel
# In chat: "create a Linux VM called myvm with 4GB RAM and launch it"
# Then: vncviewer localhost:5901
```

### TLS Direct (no SSH tunnel)

For a truly remote setup without an SSH tunnel, run the orchestrator over HTTPS directly.

**1. Generate a cert on the orchestrator machine** (self-signed is fine for a single client/friend setup — use a real CA-issued cert for anything public-facing):

```bash
mkdir -p ~/tls
openssl req -x509 -newkey rsa:2048 -nodes -keyout ~/tls/orchestrator.key -out ~/tls/orchestrator.crt -days 365 \
  -subj "/CN=orchestrator" \
  -addext "subjectAltName=IP:<orchestrator-ip>,DNS:localhost"
```

Include every hostname/IP clients will actually connect through in `subjectAltName` — TLS verification fails if the cert doesn't cover the address in the URL, even if the cert is otherwise valid.

**2. Start the orchestrator with the cert:**

```bash
API_TOKEN=mysecrettoken PYTHONPATH=files uvicorn orchestrator.http.api_server:app \
  --host 0.0.0.0 --port 8080 \
  --ssl-keyfile ~/tls/orchestrator.key --ssl-certfile ~/tls/orchestrator.crt
```

**3. Copy the `.crt` (never the `.key`) to the client, and configure it to trust it** — either via env vars:

```bash
export SERVER_URL=https://<orchestrator-ip>:8080
export API_TOKEN=mysecrettoken
export API_CA_CERT=/path/to/orchestrator.crt
```

or in `files/client/connection_config.json`:

```json
{
  "server_url": "https://<orchestrator-ip>:8080",
  "token":      "mysecrettoken",
  "ca_cert":    "/path/to/orchestrator.crt",
  "verify_ssl": true
}
```

**Dev-only bypass** (skip certificate verification entirely — never use this over an untrusted network): `API_VERIFY_SSL=0`, or `"verify_ssl": false` in the client's `connection_config.json`.

**What actually gets verified:** with `ca_cert` set and `verify_ssl: true` (the default), a client connecting to a self-signed or mismatched certificate gets a clear `SSLCertVerificationError` — the connection fails closed, not open. Forgetting to set `ca_cert` on a self-signed setup also fails closed (since the cert isn't in the system's default trust store), so a misconfigured client can't accidentally connect insecurely without the explicit `API_VERIFY_SSL=0` opt-out.

For port forwarding: forward port 8443 (or whatever port you choose) on your router to the orchestrator machine, and use that in `server_url` instead of the internal IP.

---

## Chat Client TUI

The client runs as a fullscreen curses TUI (same visual style as the admin TUI):

- **Header bar** — server URL, live spinner while waiting, remote VMs (●/○)
- **Scrollable chat area** — AI responses, tool results, your messages
- **Command input** — bottom row; built-in shortcuts bypass the AI

**Auto-start (localhost only):** if the server is not running when you launch `gorgon`, the client detects this and starts it automatically in the background. No second terminal needed.

### Client shortcuts

| Input | Action |
|---|---|
| `list` / `vms` | List VMs (calls `/execute` directly) |
| `system` | Host capabilities |
| `profiles` | Hardware profiles |
| `drift` | Configuration drift check |
| `/clear` | Wipe conversation history |
| `kill <vm>` | Force-kill a VM (asks confirmation) |
| `help` / `?` | Show shortcuts |
| `q` / `quit` / `bye` | Exit |

### Client appearance

Edit `files/client/CLI_config.json`:
```json
{
  "text_color": "#aaaaaa",
  "font_size": 13
}
```

`text_color` is any hex color. `font_size` is applied via an xterm escape sequence on startup (works in xterm-compatible terminals; silently ignored elsewhere).

---

## Admin Dashboard (Admin TUI)

`files/admin/` is its own role, separate from `client/`, `orchestrator/`, and `executor/` — it's a fullscreen curses dashboard that talks to the orchestrator purely over HTTP (`/execute`, `/events`, `/health`). It can run on the orchestrator machine itself, on your laptop, or on any other machine that can reach the orchestrator's port.

```bash
bash files/complementary/install_admin.sh   # one-time setup — prompts for orchestrator URL + token
gorgon-admin
```

Displays a live dashboard (refreshes every second, per `admin_config.json`'s `refresh_rate_s`):
- **Header bar** — uptime, VM count, event count
- **VM table** — name, status (●/○), CPU, RAM, OS
- **Event feed** — timestamped log of every tool call with outcome and duration (from `GET /events`)
- **Command line** — type commands directly; `help` shows a full overlay

### Admin Commands

| Command | Action |
|---|---|
| `launch <vm>` / `start <vm>` | Start a VM |
| `stop <vm>` | Graceful stop |
| `kill <vm>` | Force-kill (SIGKILL) |
| `stopall` | Stop all running VMs |
| `list` | Print all VM names in the status line |
| `status` | Show orchestrator reachability, VM count, running count |
| `start-server` | Start `orchestrator.http.api_server` locally (only works when run **on** the orchestrator machine) |
| `shutdown` | Send SIGTERM to the local orchestrator process (same machine only) |
| `kill-server` | Send SIGKILL to the local orchestrator process (same machine only) |
| `help` | Show all commands (overlay, any key dismisses) |
| `q` / Esc / Ctrl-C | Quit the TUI |

`start-server`/`shutdown`/`kill-server` look for a locally-running orchestrator process by PID — when the admin TUI runs on a different machine than the orchestrator, these report "orchestrator not found on this machine" instead of acting remotely.

### Admin connection + appearance

`files/admin/connection_config.json` (written by `install_admin.sh`):
```json
{
  "orchestrator_url": "http://localhost:8080",
  "token": ""
}
```

`files/admin/admin_config.json`:
```json
{
  "text_color": "#aaaaaa",
  "font_size": 13,
  "refresh_rate_s": 1.0,
  "events_display_limit": 200
}
```

---

## API Endpoints

All endpoints except `/health` require `Authorization: Bearer <token>`.

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/health` | GET | No | Returns `{"status":"ok"}` — used by liveness monitor |
| `/sync` | GET | Yes | Returns server-authoritative config (shortcuts, allowed tools, visible VMs/profiles) |
| `/execute` | POST | Yes | Execute a tool call. Body: `{tool_name, args, verbose}` |
| `/images/{vm_name}` | GET | Yes | Stream primary disk (qcow2), supports HTTP Range resume |
| `/images/{vm_name}/sha256` | GET | Yes | SHA-256 checksum of primary disk |
| `/vms/{vm_name}/bundle` | GET | Yes | Stream entire VM folder (disk + config + OVMF vars) as `.tar.gz` |
| `/events` | GET | Yes | Return recent event log entries. Query: `?limit=N&since=<iso-ts>` |
| `/rotate-token` | POST | Yes | Replace token (min 16 chars) and persist to `~/.gorgon.token` |
| `/custom-mode` | POST | Yes | Toggle `-cu` custom-machine mode. Body: `{enabled: bool}`. **Process-global** — affects every client on this orchestrator, not just the caller |

### /execute request flow

1. Verify `Authorization: Bearer <token>`
2. Check `tool_name` is in `allowed_remote_tools`
3. Override SDL/GTK display → VNC + `vnc_bind_local=True` for `launch_vm`
4. Run `orchestrator/preflight/validator.py` (full, `stateless_only=False`)
5. Apply `auto_fix` correction if preflight returns it
6. Call `orchestrator/executor_client.execute_tool()` — dispatches in-process via `orchestrator/pipeline.py` in local mode, or forwards to the executor's `POST /execute` in remote mode
7. Return `{"ok": true, "result": {...}}`

Preflight `abort` → HTTP 200, body contains `{"success": false, "error": "..."}` (structured error, not an HTTP error).

Preflight `ask_user` → HTTP 200, body contains `{"clarify": true, "question": "...", "options": [...]}`.

### Tool Allowlist

`allowed_remote_tools` in `shared/executioner/config.json` controls which tools the HTTP API accepts. Tools not in the list get `403 Forbidden` before preflight runs. `send_monitor_cmd` (raw QEMU monitor access) is excluded by default.

### Client Access Control

The server controls which VMs and hardware profiles are visible and accessible to clients. This is configured in `files/orchestrator/connection_config.json`:

```json
{
  "client_allowed_vms":      ["test", "kali"],
  "client_allowed_profiles": ["desktop", "laptop"]
}
```

- **Empty list (default)** — all VMs/profiles are accessible.
- **Non-empty list** — only listed names are accessible; everything else is hidden.

Enforcement happens at two levels:
1. **`/sync`** — filters VMs and profiles before sending the client its startup inventory, so hidden resources never appear in the Resources panel.
2. **`executor_client.py`** — blocks tool calls (launch, stop, delete, etc.) targeting hidden VMs at the executor level, covering both direct `/execute` calls and AI-initiated calls through `/chat`. Hidden VMs return `"not found"` to avoid leaking their existence.

---

## Configuration Reference

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `API_URL` | `"local"` | Orchestrator→executor URL, or `"local"` for same-machine |
| `API_TOKEN` | `""` | Client→orchestrator secret. **Not** used for the orchestrator→executor hop — see `EXECUTOR_TOKEN` |
| `EXECUTOR_TOKEN` | `""` | Orchestrator→executor secret (overrides `orchestrator/connection_config.json`'s `token` field). Deliberately separate from `API_TOKEN` — they're different secrets in split mode |
| `API_TIMEOUT` | `120` | HTTP request timeout in seconds |
| `API_CA_CERT` | `None` | Path to custom CA certificate for TLS |
| `API_VERIFY_SSL` | `"1"` | Set to `"0"` to skip TLS verification (dev only) |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model name |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server URL |

### connection_config.json (two files — one per side)

**`files/orchestrator/connection_config.json`** — read by the orchestrator machine, governs how it reaches the **executor**:
```json
{
  "url":                    "local",
  "token":                  "",
  "timeout":                120,
  "verify_ssl":             true,
  "ca_cert":                "",
  "client_allowed_vms":      [],
  "client_allowed_profiles": [],
  "allowed_remote_tools":    []
}
```

`client_allowed_vms` and `client_allowed_profiles` are optional. Empty arrays (default) mean all VMs and profiles are accessible. Add names to restrict access — hidden resources appear non-existent to clients.

Set `url` to `"local"` for same-machine mode (executor code runs in-process) or `"http://<executor-host>:8001"` for remote. `token` must match the executor's own token (see `executor/config.json` / `EXECUTOR_TOKEN` below) — it is a **different secret** from the `API_TOKEN` clients use to reach this orchestrator. `install_orchestrator.sh` prompts for both `url` and `token` interactively (or accepts `EXECUTOR_URL`/`EXECUTOR_TOKEN` env vars).

**`files/client/connection_config.json`** — read by the client machine, governs how it reaches the **orchestrator**:
```json
{
  "server_url": "http://localhost:8080",
  "token":      "",
  "timeout":    120,
  "verify_ssl": true,
  "ca_cert":    null
}
```

**`files/executor/config.json`** — read by the executor machine's own HTTP server (`executor/server.py`):
```json
{
  "host":  "0.0.0.0",
  "port":  8001,
  "token": ""
}
```
`allowed_remote_tools` lives in `orchestrator/connection_config.json` (above) — the executor itself doesn't filter by tool name, since the orchestrator has already checked the allowlist before forwarding.

### Token persistence (client server)

Priority order:
1. `API_TOKEN` environment variable
2. `~/.gorgon.token` file (chmod 0600)

Server refuses to start if neither is set.

### Model Recommendations

| Model | Score | Avg time | Sanitiser fixes | Notes |
|---|---|---|---|---|
| `qwen2.5:7b` | 18/19 | ~6.5s | 2 per run | **Recommended** — sends correct args natively |
| `llama3.1:8b` | 18/19 | ~3.5s | 34 per run | Fast fallback — sanitizer does more work |
| `qwen2.5:14b` | 12/19 | ~40s | 5 per run | Too slow on CPU |
| `mistral-nemo` | 6/19 | ~40s | — | Poor tool use |

```bash
OLLAMA_MODEL=llama3.1 gorgon    # temporary
export OLLAMA_MODEL=llama3.1      # permanent
```

---

## Test Suite

11 layers, 259 tests total (100% passing).

```bash
python3 test_api.py                          # all layers (5 random profiles)
python3 test_api.py -l 1                     # sanitizer only — no Ollama, ~1ms each
python3 test_api.py -l 1,2                   # no Ollama needed, ~2s total
python3 test_api.py -l 1,2,11               # no Ollama, includes HTTP boundary tests
python3 test_api.py -l 3                     # AI tests only (needs Ollama)
python3 test_api.py -l 4 -n 100             # 100 random profiles
python3 test_api.py --quick                  # L1+L2+L5, skip L3 (fast CI)
python3 test_api.py --fuzz                   # L5 with 500 iterations
python3 test_api.py --benchmark llama3.1 qwen2.5:7b   # side-by-side model comparison
```

Layer 11 requires `API_TOKEN`:
```bash
export API_TOKEN=test && python3 test_api.py -l 1,2,11
```

### Test Layers

| Layer | File | What it tests | Needs Ollama |
|---|---|---|---|
| 1 | `layer1_sanitizer.py` | `_sanitise_args()` unit tests (~1ms each) | No |
| 2 | `layer2_executor.py` | `execute_tool()` + preflight (~10ms each) | No |
| 3 | `layer3_ai.py` | 19 randomised prompts at 5 vagueness levels (~4-11s each) | Yes |
| 4 | `layer4_profiles.py` | 8 fixed + N random profile tests (~0.5s each) | No |
| 5 | `layer5_property.py` | 5 property invariants × 50 iterations each | No |
| 6 | `layer6_context_gate.py` | `gate_check()` unit tests | No |
| 7 | `layer7_context_assistant.py` | Context assistant message templates | No |
| 8 | `layer8_pipeline.py` | Full sanitize→preflight pipeline | No |
| 9 | `layer9_pipeline_gated.py` | Pipeline with context gate enabled | No |
| 10 | `layer10_pipeline_full.py` | Pipeline + AI + context assistant | Yes |
| 11 | `layer11_remote_split.py` | HTTP boundary: auth, display override, VNC, preflight routing (19 tests) | No |

### Layer 5 Property Invariants

| Property | What it checks |
|---|---|
| `prop_sanitiser_never_crashes` | `_sanitise_args()` never raises for any input |
| `prop_preflight_never_crashes` | `_preflight_check()` always returns dict with `action` key |
| `prop_sanitiser_idempotent` | Running sanitizer twice gives same result as once |
| `prop_placeholders_always_cleared` | All placeholder names always produce empty name |
| `prop_profile_always_auto_set` | Profile names used as machine_type always set profile field |

### Reading Results

```
✓ (7f) [8.1s]  — passed, sanitizer applied 7 fixes, took 8.1 seconds
✗ (2f) [3.1s]  — failed, 2 fixes applied but assertion still failed
✓ [1.5s]       — passed, AI sent correct args natively (no fixes needed)
```

Results saved to `test_report.json` after every run.

---

## Directory Structure

```
~/.qemu_vms/
├── .state.json                  Running PID tracking (survives terminal restart)
├── .session.json                AI conversation history (last 40 messages)
├── .chat_session_id             Persisted chat session ID (chat client)
├── events.log                   Structured event log (JSON-lines, all tool calls)
├── _profiles/                   Custom hardware profiles
│   └── my-profile.json
├── _networks/                   Isolated network definitions
│   └── networks.json
├── _templates/                  Golden-image disk copies (see Golden-Image Templates)
│   └── <template-name>/
│       ├── template.json        os_type, disk metadata, labels
│       └── diskN.qcow2          Flattened disk copy (qemu-img convert, not a backing link)
└── <vm-name>/
    ├── config.json              Full MachineConfig serialised — includes root_password/
    │                            user_password/randomized_username when set via
    │                            randomize_root_password/randomize_user_password
    ├── disk0.qcow2              Primary disk image
    ├── OVMF_VARS.fd             Per-VM UEFI variable store (writable copy)
    ├── vm.pid                   PID of running QEMU process
    ├── qmp.sock                 QMP control socket
    ├── monitor.sock             Human monitor socket
    ├── serial.sock              Serial console socket
    ├── launch.log               stdout/stderr from QEMU (appended per launch)
    ├── stop_vm.log              Call-stack trace of every stop_vm call (debug aid)
    ├── smbios_chassis.bin       Raw SMBIOS type-3 binary (chassis type byte)
    ├── tpm/                     swtpm persistent TPM state
    ├── tpm.sock                 swtpm control socket (live only)
    ├── tpm.pid                  swtpm PID file
    ├── guest_setup.sh           Auto-generated Linux stealth setup script
    ├── guest_setup.ps1          Auto-generated Windows stealth setup script
    ├── autounattend.iso         Windows unattended answer-file ISO (see Unattended OS Install)
    ├── autounattend.img         Windows unattended answer-file FAT/USB image
    ├── cidata.iso               Linux (casper/Subiquity) cloud-init NoCloud volume
    ├── linux-kernel              Extracted installer kernel (direct-kernel-boot unattended Linux)
    ├── linux-initrd-preseeded   Extracted installer initrd + injected preseed (debian-installer/ubiquity)
    ├── .relaunch_after_install  Watcher flag — deleted by stop_vm to cancel auto-relaunch
    └── .stealth_done            Sentinel — suppresses HTTP server on subsequent launches
```

---

## Install-to-Boot Flow

When a VM is launched with an ISO attached, the following happens automatically:

1. QEMU starts with the ISO as the boot device and `-no-reboot`
2. A background **watcher process** is spawned, monitoring the QEMU PID
3. When the installer finishes and the guest requests a restart, QEMU exits cleanly (due to `-no-reboot`)
4. The watcher detects the exit, calls `launch_vm` again
5. `_maybe_finish_unattended_install` (`_vm_runtime.py`) checks whether the install has actually finished — a cheap disk-size pre-filter, then a real read-only check for `/etc/os-release` (size alone can false-positive mid-install, since partitioning writes real data before the OS is actually installed). If confirmed, clears `iso_path` **and** (for [unattended](#unattended-os-install) direct-kernel-boot installs) `kernel_path`/`initrd_path`/`kernel_cmdline` — without this, a VM using direct kernel boot would re-run the installer from scratch on every subsequent launch, including right after the install finishes
6. VM boots from the installed disk

**Cancelling the auto-relaunch:** calling `stop_vm` (even force-kill) at any point deletes the `.relaunch_after_install` flag before sending the signal. The watcher sees the missing flag on exit and does not relaunch.

**Stealth VMs:** after relaunch, the watcher waits for `.stealth_done` before exiting — so the HTTP server for guest setup stays active until you run the guest script and mark it done with `setup-done <name>` (or `touch ~/.qemu_vms/<name>/.stealth_done`).

---

## Known Issues

**VM crashes with no log (OVMF_VARS.fd missing):**
```bash
cp /usr/share/OVMF/OVMF_VARS_4M.fd ~/.qemu_vms/<name>/OVMF_VARS.fd
```

**"invalid datetime format" crash:**
```bash
python3 -c "
import json, os
p = os.path.expanduser('~/.qemu_vms/<name>/config.json')
d = json.load(open(p))
d['rtc_clock'] = 'utc'
json.dump(d, open(p,'w'), indent=2)
"
```

**Raspberry Pi 3B has no display — serial console only:**
```bash
gorgon open-shell <name>
# For graphical ARM64: use machine_type=virt with Ubuntu ARM64 ISO
```

**Windows 11 ARM64 ISO on x86 VM:** Will not boot. Download the x64 edition.

**Hugepages (server profile):**
```bash
echo 2048 | sudo tee /proc/sys/vm/nr_hugepages
```

**Bridge networking:**
```bash
sudo nmcli con add type bridge ifname br0 con-name br0
sudo nmcli con add type ethernet ifname ens33 master br0
sudo nmcli con up br0
echo "allow br0" | sudo tee -a /etc/qemu/bridge.conf
```

> **Never add `smm=off` to `extra_args` when using OVMF.** OVMF requires SMM enabled or Linux boot KVM-crashes at ~15 seconds.

**Layer 11 tests require API_TOKEN:**
```bash
export API_TOKEN=test && python3 test_api.py -l 1,2,11
```

**`--benchmark` flag parsing:** flags like `-s` and `-n` must come BEFORE `--benchmark`:
```bash
python3 test_api.py -s 123 -n 10 --benchmark llama3.1 qwen2.5:7b
```

---

## Stealth Rating Reference

After running `guest_setup`, approximate stealth rating is **7/10**:

| Check | Result | Notes |
|---|---|---|
| `inxi -F` system/chassis | PASS | Dell Latitude SMBIOS + chassis binary |
| `lsmod \| grep qemu` | PASS | Blacklist + lsmod wrapper |
| `lspci` GPU | PASS | Intel Iris Xe (lspci wrapper / registry) |
| Firefox WebGL | PASS | user.js profile overrides |
| ACPI OEM strings | FAIL | Still BOCHS/BXPC (future project) |
| `/sys` PCI vendor IDs | FAIL | lspci wrapper doesn't cover /sys |
| `dmesg` QEMU strings | FAIL | Still visible (future project) |

Sufficient to pass casual and automated fingerprinting checks.

---

> See [files/complementary/GUIDE.txt](files/complementary/GUIDE.txt) for the complete reference guide (v7), including the remote setup walkthrough (Section 14), all technical details, and full function-level documentation.
