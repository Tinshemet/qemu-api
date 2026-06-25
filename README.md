# qemu-api

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
9. [Flags: -cu and -tf](#flags--cu-and--tf)
10. [Stealth Mode](#stealth-mode)
11. [SMBIOS / Hardware Fingerprint](#smbios--hardware-fingerprint)
12. [Guest Setup Scripts](#guest-setup-scripts)
13. [Windows 11 Setup Workflow](#windows-11-setup-workflow)
14. [Bridge Networking](#bridge-networking)
15. [Remote Mode — Full Reference](#remote-mode--full-reference)
16. [API Endpoints](#api-endpoints)
17. [Configuration Reference](#configuration-reference)
18. [Test Suite](#test-suite)
19. [Directory Structure](#directory-structure)
20. [Known Issues](#known-issues)
21. [Stealth Rating Reference](#stealth-rating-reference)

---

## What It Is

`qemu-api` is an AI-powered VM manager. You talk to it in plain English; an Ollama model (default: `qwen2.5:7b`) translates your intent into structured tool calls. Every tool call passes through five protection layers before QEMU sees it:

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

The codebase is split into three canonical directories:

| Directory | Runs on | Purpose |
|---|---|---|
| `server/` | Server machine | AI (Ollama), QEMU engine, HTTP `/chat` + `/execute` API |
| `client/` | User's laptop | Thin chat UI, optional local QEMU CLI |
| `shared/` | Both | QEMU engine, sanitizer, preflight, display |

In local mode the server and client run on the same machine. In remote mode the server machine hosts AI + QEMU; the user's laptop runs the thin client and connects over HTTPS/SSH tunnel.

---

## Quick Start

### Clone

Pick the version that matches your setup:

```bash
# Both (local mode — AI + QEMU on the same machine)
git clone https://github.com/tinshemet/qemu-api.git

# Server only (AI/Ollama machine in a remote split)
git clone --filter=blob:none --sparse https://github.com/tinshemet/qemu-api.git
cd qemu-api
git sparse-checkout set files/server files/shared files/complementary

# Client only (QEMU execution machine in a remote split)
git clone --filter=blob:none --sparse https://github.com/tinshemet/qemu-api.git
cd qemu-api
git sparse-checkout set files/client files/shared files/complementary
```

After cloning, run the matching setup script:

| Version | Setup script |
|---|---|
| Both | `bash files/complementary/install.sh` |
| Server only | `bash files/complementary/setup_server.sh` |
| Client only | `bash files/complementary/setup_client.sh` |

---

### One-Command Install

```bash
cd ~/path/to/qemu-api
bash files/complementary/install.sh
```

Handles automatically:
- `apt` packages: `qemu-kvm`, `qemu-utils`, `ovmf`, `virt-viewer`, `socat`, `cpu-checker`
- KVM group membership
- Python venv at `~/qemu-env` with `requests psutil rich fastapi uvicorn httpx`
- Ollama install + `qwen2.5:7b` model pull
- `systemd` user service (Ollama auto-starts on login)
- Bridge networking config `/etc/qemu/bridge.conf`
- Shell alias `qemu-api`
- Self-test

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
export API_TOKEN="$(cat ~/.qemu-api.token 2>/dev/null || echo '')"
alias qemu-api-serve='~/start-qemu-api-server.sh'
alias qemu-api='PYTHONPATH=~/path/to/qemu-api/files python3 ~/path/to/qemu-api/files/client/client_wrapper.py'
```

### Run (Local Mode)

```bash
qemu-api              # AI chat mode
qemu-api -v           # verbose (shows raw tool calls)
qemu-api -cu          # custom machine mode (skip product verification)
qemu-api list         # direct CLI, no AI
qemu-api system       # check system capabilities
qemu-api -tf <name>   # fingerprint report
```

---

## Architecture

### Five-Layer Protection

```
User Input
    ↓
qwen2.5:7b (Ollama)               server/ai/ollama_client.py
Selects tool, builds args
    ↓
_sanitise_args()                   shared/sanitizer/sanitizer.py
Type coercion, enum normalisation, path fixing,
placeholder removal, resource caps, arch checks
    ↓
_preflight_check()                 shared/preflight/validator.py
+ _validate_with_internet
+ _validate_profile_for_host
Reality check: ok / auto_fix / ask_user / abort
    ↓
execute_tool()                     shared/executioner/tool_executor.py
Hard guards, arch mismatch, name validation,
Windows UEFI enforcement, clarify responses
    ↓
QemuManager                        shared/api/qemu_manager.py
Actual QEMU/KVM operations
```

### File Structure

```
files/
├── server/                          Server machine (AI + QEMU)
│   ├── ai/
│   │   ├── cli.py                   Chat loop, process_message(), direct sub-command CLI
│   │   ├── ollama_client.py         Ollama HTTP client + system prompt builder
│   │   ├── session.py               Conversation history (load/save/clear)
│   │   ├── tools.py                 TOOLS list — AI tool definitions
│   │   ├── context_assistant.py     Context gate for AI-aware preflight decisions
│   │   └── config.json              AI-layer config (Ollama URL, model, loop limits)
│   ├── http/
│   │   └── api_server.py            FastAPI: /chat /execute /health /images /rotate-token
│   ├── executor_client.py           Re-exports shared.executioner.execute_tool (always local)
│   └── connection_config.json       Server connection settings (url, token, timeout)
│
├── client/                          User's laptop (thin UI)
│   ├── ui/
│   │   └── chat_client.py           Rich chat UI — POSTs to server /chat endpoint
│   ├── cli/
│   │   └── commands.py              Direct local QEMU commands (no AI)
│   ├── client_wrapper.py            Entry point: chat UI or local commands
│   └── connection_config.json       Client settings (server_url, token, ca_cert)
│
├── shared/                          Used by both server and client
│   ├── api/
│   │   ├── qemu_config.py           Config dataclasses, hardware profiles, OVMF detection
│   │   ├── qemu_manager.py          VM lifecycle engine: create, launch, monitor, QMP
│   │   ├── qemu_arg_builder.py      Builds the QEMU command-line argument list
│   │   ├── qmp_client.py            QMP socket client
│   │   ├── network_manager.py       Isolated network (bridge) management
│   │   └── vm_state.py              State persistence (PID tracking, .state.json)
│   ├── executioner/
│   │   ├── tool_executor.py         execute_tool() — hard guards, dispatch to QemuManager
│   │   └── config.json              Executor config (VM base dir, timeouts)
│   ├── display.py                   Rich console rendering (tables, panels, VNC panel)
│   ├── fingerprint.py               -tf fingerprint report (inxi simulation)
│   ├── preflight/
│   │   ├── validator.py             _preflight_check(), internet validator, profile check
│   │   └── config.json              Preflight config (timeout, caps)
│   └── sanitizer/
│       ├── sanitizer.py             _sanitise_args() — coercion, normalisation, caps
│       ├── context_gate.py          Gate check for context-aware tool calls
│       ├── config.json              Sanitizer config (placeholder names, enum maps)
│       └── context_gate_config.json Context gate rules per tool
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
│   ├── layer11_remote_split.py      Layer 11: server/client HTTP boundary (19 tests)
│   └── test_api.py                  11-layer test suite entry point
│
└── complementary/
    ├── install.sh           Full local setup (Ollama + QEMU + venv + alias)
    ├── setup_server.sh      Server setup (Ollama + QEMU + HTTP API) — Linux or WSL2
    ├── setup_client.sh      Client setup (your laptop — thin UI only)
    ├── setup_wsl2.ps1       Windows-side WSL2 port forwarding (run as Admin)
    ├── requirements.txt     Python dependencies
    ├── GUIDE.txt            Complete reference guide
    └── handbooks/           Deep-dive reference (dictionary, workflow, files, config, tests)
```

**Test results: 134/134 (100%)**

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
│  server/http/api_server.py  (/chat endpoint)                 │
│    ↓ calls process_message()                                 │
│  server/ai/ollama_client.py                                  │
│    ↓ Ollama returns a tool call                              │
│  shared/sanitizer/sanitizer.py  (_sanitise_args)            │
│    ↓ args cleaned and normalised                             │
│  shared/preflight/validator.py  (_preflight_check)          │
│    ↓ full state check (real VM existence, real disk)         │
│  shared/executioner/tool_executor.py  (execute_tool)        │
│    ↓ hard guards, then dispatch                              │
│  shared/api/qemu_manager.py  (QemuManager)                  │
│    → runs QEMU/KVM directly                                  │
└──────────────────────────────────────────────────────────────┘
```

Or run the client wrapper directly (single machine, server must be running):

```bash
PYTHONPATH=files python3 files/client/client_wrapper.py
```

### Remote Mode (server + thin client)

```
┌───────────────────────────┐          ┌──────────────────────────────────────┐
│   User's laptop (client)  │          │   Server machine                     │
│                           │          │   (AI + QEMU)                        │
│  client/ui/chat_client.py │          │                                      │
│    ↓ user types a prompt  │  HTTPS   │  server/http/api_server.py           │
│    POST /chat  ───────────┼─────────►│    ↓ process_message()               │
│                           │          │  server/ai/ollama_client.py          │
│    ← {text, tool_results} │          │    ↓ Ollama tool call                │
│    needs_input? → confirm │          │  shared/sanitizer                    │
│    re-POST /chat ─────────┼─────────►│  shared/preflight                    │
│                           │          │  shared/executioner/tool_executor    │
│  Rich panels rendered     │          │  shared/api/qemu_manager             │
│  locally from tool_results│          │    → runs QEMU/KVM on server         │
│                           │◄─────────┤  GET /health  /images  /rotate-token │
└───────────────────────────┘          └──────────────────────────────────────┘
```

Session state (conversation history) lives on the server, keyed by `session_id`. The client needs no Ollama installation.

### Setup Scripts — Which One to Run

| Script | Run on | Purpose |
|---|---|---|
| `install.sh` | Your machine | Full local setup — QEMU + Ollama + HTTP API + everything |
| `setup_server.sh` | Server machine | Ollama + QEMU + HTTP API (uvicorn on port 8080); works on native Linux and WSL2 |
| `setup_client.sh` | Your laptop | Thin client only — Python + Rich + connection config; no QEMU needed |
| `setup_wsl2.ps1` | Windows (as Admin) | Port forwarding + firewall for WSL2 server setup |

Typical remote scenario:

```bash
# On the server machine (Linux/WSL2):
bash files/complementary/setup_server.sh

# On the server machine (if Windows+WSL2, in Admin PowerShell):
.\files\complementary\setup_wsl2.ps1

# On your laptop:
bash files/complementary/setup_client.sh
```

Pre-seed to skip prompts:

```bash
API_TOKEN=mysecret bash setup_server.sh
SERVER_URL=http://192.168.1.10:8080 API_TOKEN=mysecret bash setup_client.sh
```

### Starting Remote Mode

```bash
# On the server machine — start the HTTP API
source ~/qemu-env/bin/activate
cd ~/qemu-api
API_TOKEN=mysecrettoken PYTHONPATH=files uvicorn server.http.api_server:app --host 0.0.0.0 --port 8080

# Or use the alias created by setup_server.sh:
qemu-api-serve

# On your laptop — open SSH tunnel (if connecting over the internet)
ssh -N -L 8080:127.0.0.1:8080 -L 5901:127.0.0.1:5901 user@server-ip

# On your laptop — start the chat client
export SERVER_URL=http://localhost:8080   # (or LAN IP without tunnel)
export API_TOKEN=mysecrettoken
qemu-api
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
qemu-api -cu <command>         # custom mode — skip product verification
qemu-api -v <command>          # verbose output

# VMs
qemu-api list                  # list all VMs
qemu-api status <name>         # status
qemu-api monitor <name|all>    # activity report
qemu-api launch <name>         # start VM (SDL window, direct local engine)
qemu-api launch <name> sdl     # SDL display (local window, no server needed)
qemu-api launch <name> vnc     # VNC display → connect with vncviewer localhost:5900
qemu-api launch <name> gtk     # GTK display
qemu-api stop <name>           # stop VM
qemu-api clone <src> <new>     # clone VM
qemu-api config <name>         # show config JSON
qemu-api resize <name> <gb>    # resize primary disk
qemu-api delete <name>         # delete VM (asks confirmation)
qemu-api logs <name>           # failure diagnosis
qemu-api show-cmd <name>       # print QEMU launch command
qemu-api -tf <name>            # fingerprint report (inxi simulation)

# Snapshots
qemu-api snapshot list <vm>
qemu-api snapshot create <vm> <snap>
qemu-api snapshot restore <vm> <snap>
qemu-api snapshot delete <vm> <snap>

# Networks
qemu-api network list
qemu-api network create <name>
qemu-api network delete <name>
qemu-api network add <net> <vm>
qemu-api limit <vm> <cpu%> [mem_mb]

# Info
qemu-api profiles
qemu-api check-profile <name>
qemu-api system
qemu-api isos
qemu-api cmd <vm> "<qemu monitor cmd>"

# Session
qemu-api clear-session

# Remote mode only
qemu-api serve [host] [port] [--cert cert.pem --key key.pem]
qemu-api fetch <vm> [--out /dir]
```

---

## AI Chat Reference

**Built-in shortcuts (no AI, instant):**

| Command | What it does |
|---|---|
| `list` / `vms` / `ls` | List all VMs |
| `system` | System capabilities |
| `profiles` | Hardware profiles |
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

## Flags: -cu and -tf

### `-cu` — Custom Machine Mode

```bash
qemu-api -cu
```

Starts the AI chat with product verification disabled. Allows any `manufacturer`/`product_name` combination including fictional hardware.

**What it disables:**
- DuckDuckGo product lookup (manufacturer + product_name no longer verified)
- Memory plausibility check

**What still runs:** QEMU binary checks, ARM/x86 consistency, all other preflight, sanitizer.

Use cases: fictional hardware names, research VMs, air-gapped environments.

### `-tf` — Fingerprint Report

```bash
qemu-api -tf <vmname>
```

Read-only analysis of a VM's configuration. Simulates what `inxi -M -N -C -D -A -G` would report from inside the guest OS, then checks each field against known VM fingerprint signatures.

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
qemu-api launch <name> sdl    # opens an SDL window directly
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
qemu-api fetch <vm_name>
qemu-api fetch <vm_name> --out /external/backups/
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

New token persists to `~/.qemu-api.token` and takes effect immediately without restart.

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
qemu-api

# Step 5: VNC connects through the already-open tunnel
# In chat: "create a Linux VM called myvm with 4GB RAM and launch it"
# Then: vncviewer localhost:5901
```

For TLS direct (no SSH): generate certs with openssl, forward port 8443 on router, set `API_URL=https://...` and `API_CA_CERT` on provider.

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
| `/rotate-token` | POST | Yes | Replace token (min 16 chars) and persist to `~/.qemu-api.token` |

### /execute request flow

1. Verify `Authorization: Bearer <token>`
2. Check `tool_name` is in `allowed_remote_tools`
3. Override SDL/GTK display → VNC + `vnc_bind_local=True` for `launch_vm`
4. Run `shared/preflight/validator.py` (full, `stateless_only=False`)
5. Apply `auto_fix` correction if preflight returns it
6. Call `shared/executioner/tool_executor.execute_tool()`
7. Return `{"ok": true, "result": {...}}`

Preflight `abort` → HTTP 200, body contains `{"success": false, "error": "..."}` (structured error, not an HTTP error).

Preflight `ask_user` → HTTP 200, body contains `{"clarify": true, "question": "...", "options": [...]}`.

### Tool Allowlist

`allowed_remote_tools` in `shared/executioner/config.json` controls which tools the HTTP API accepts. Tools not in the list get `403 Forbidden` before preflight runs. `send_monitor_cmd` (raw QEMU monitor access) is excluded by default.

### Client Access Control

The server controls which VMs and hardware profiles are visible and accessible to clients. This is configured in `files/server/connection_config.json`:

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
| `API_URL` | `"local"` | Remote client URL, or `"local"` for same-machine |
| `API_TOKEN` | `""` | Shared secret for API authentication |
| `API_TIMEOUT` | `120` | HTTP request timeout in seconds |
| `API_CA_CERT` | `None` | Path to custom CA certificate for TLS |
| `API_VERIFY_SSL` | `"1"` | Set to `"0"` to skip TLS verification (dev only) |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model name |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server URL |

### connection_config.json (two files — one per side)

**`files/server/connection_config.json`** — read by the AI/Ollama machine:
```json
{
  "url":                    "local",
  "token":                  "",
  "timeout":                120,
  "verify_ssl":             true,
  "ca_cert":                "",
  "client_allowed_vms":      [],
  "client_allowed_profiles": []
}
```

`client_allowed_vms` and `client_allowed_profiles` are optional. Empty arrays (default) mean all VMs and profiles are accessible. Add names to restrict access — hidden resources appear non-existent to clients.

Set `url` to `"local"` for same-machine mode or `"http://10.0.2.2:8080"` for remote.

**`files/client/connection_config.json`** — read by the QEMU machine's API server:
```json
{
  "port":    8080,
  "token":   "",
  "timeout": 120,
  "allowed_remote_tools": ["create_vm", "launch_vm", "stop_vm", ...]
}
```

### Token persistence (client server)

Priority order:
1. `API_TOKEN` environment variable
2. `~/.qemu-api.token` file (chmod 0600)

Server refuses to start if neither is set.

### Model Recommendations

| Model | Score | Avg time | Sanitiser fixes | Notes |
|---|---|---|---|---|
| `qwen2.5:7b` | 18/19 | ~6.5s | 2 per run | **Recommended** — sends correct args natively |
| `llama3.1:8b` | 18/19 | ~3.5s | 34 per run | Fast fallback — sanitizer does more work |
| `qwen2.5:14b` | 12/19 | ~40s | 5 per run | Too slow on CPU |
| `mistral-nemo` | 6/19 | ~40s | — | Poor tool use |

```bash
OLLAMA_MODEL=llama3.1 qemu-api    # temporary
export OLLAMA_MODEL=llama3.1      # permanent
```

---

## Test Suite

11 layers, 134 tests total (100% passing).

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
├── _profiles/                   Custom hardware profiles
│   └── my-profile.json
├── _networks/                   Isolated network definitions
│   └── networks.json
└── <vm-name>/
    ├── config.json              Full MachineConfig serialised
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
5. `_maybe_auto_detach_iso` detects the installed OS (disk > 2 GB actual data), removes the ISO from config
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
qemu-api open-shell <name>
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
