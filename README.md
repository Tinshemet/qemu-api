# qemu-api

A QEMU VM management API with stealth, SMBIOS spoofing, TPM, Secure Boot, and multi-distro guest setup automation.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Creating a VM](#creating-a-vm)
3. [Stealth Mode](#stealth-mode)
4. [SMBIOS / Hardware Fingerprint](#smbios--hardware-fingerprint)
5. [Guest Setup Scripts](#guest-setup-scripts)
   - [Linux](#linux-guest-setup)
   - [Windows](#windows-guest-setup)
6. [Windows 11 Setup Workflow](#windows-11-setup-workflow)
7. [Bridge Networking (Wired LAN)](#bridge-networking-wired-lan)
8. [TPM & Secure Boot](#tpm--secure-boot)
9. [Stealth Rating Reference](#stealth-rating-reference)

---

## Quick Start

```bash
# Start the API
python3 -m files.api

# Or use the CLI
python3 -m files.ai
```

---

## Creating a VM

```python
from files.api.qemu_manager import QemuManager

mgr = QemuManager()
mgr.create_vm({
    "name": "my-vm",
    "os_type": "linux",
    "memory_mb": 4096,
    "cpu_cores": 2,
    "disk_size_gb": 40,
})
mgr.launch_vm("my-vm")
```

---

## Stealth Mode

Set `stealth: true` in the VM config to enable hardware fingerprint spoofing. Combine with `hardened: true` to also hide the KVM hypervisor bit from the guest.

```json
{
  "stealth": true,
  "hardened": true
}
```

**What stealth mode does automatically:**

| Feature | Effect |
|---|---|
| Display device | Linux: `vmware-svga` (loads `vmwgfx`, no "qemu" in module name). Windows: `VGA` (standard, no VMware fingerprint) |
| CPUID (`hardened`) | Hides `-hypervisor` bit, `kvm=off`, `-vmx` — guest cannot detect KVM |
| ACPI OEM ID | Set to manufacturer name (e.g. `Dell I`) instead of `BOCHS` |
| USB controller | `nec-usb-xhci` (real NEC PCI IDs) instead of `qemu-xhci` |
| Guest setup | Auto-serves `guest_setup.sh` / `guest_setup.ps1` via HTTP on first launch |

**Things stealth mode does NOT do automatically (require guest_setup script):**
- lspci wrapper (Linux)
- lsmod wrapper (Linux)
- Firefox WebGL profile
- Windows Device Manager GPU rename

---

## SMBIOS / Hardware Fingerprint

These fields are exposed in VM config and map directly to SMBIOS tables visible via `inxi -F`, `dmidecode`, and Windows System Information.

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

**`machine_class` / `smbios_type`** sets the chassis type byte (SMBIOS type 3):
- `laptop` / `notebook` → chassis type 9 (Laptop)
- `desktop` → chassis type 3
- `server` → chassis type 17

The chassis type byte is injected via a raw SMBIOS binary (`smbios_chassis.bin`) because QEMU's CLI cannot set this byte directly. Linux's DMI scanner uses last-write-wins, so the appended entry overrides QEMU's default.

After filling in SMBIOS fields, `inxi -F` inside the guest should show:
```
System:    Type: Laptop System: Dell Inc. product: Latitude 5530 serial: 3K8LP52
```

---

## Guest Setup Scripts

On first launch of a stealth VM, the API automatically:
1. Generates a `guest_setup.sh` (Linux) or `guest_setup.ps1` (Windows) in the VM directory
2. Serves it via HTTP on port 8080 from the host
3. Prints the command to run inside the VM

Run it once after installing the OS. It marks itself done via a `.stealth_done` sentinel file so it won't re-serve on subsequent launches.

### Linux Guest Setup

Supports: **Ubuntu, Linux Mint, Kali, Arch**, and any distro with `update-initramfs`, `mkinitcpio`, or `dracut`.

```bash
curl http://10.0.2.2:8080/guest_setup.sh | bash
```

**What it does (4 steps):**

**Step 1 — Blacklist QEMU kernel modules:**
Adds `/etc/modprobe.d/blacklist-qemu.conf`:
```
blacklist qemu_fw_cfg
blacklist cirrus_qemu
```
Rebuilds initramfs (auto-detects `update-initramfs` / `mkinitcpio` / `dracut`).

**Step 2 — Firefox stealth profile:**
Creates `~/.mozilla/firefox/stealth/user.js` with WebGL overrides:
```javascript
user_pref("webgl.renderer-string.override", "Intel(R) Iris(R) Xe Graphics");
user_pref("webgl.vendor-string.override",   "Intel");
```

**Step 3 — Stealth browser launcher:**
Creates `~/Desktop/stealth-browser.sh` that launches Firefox with the stealth profile. Auto-detects `firefox` vs `firefox-esr` (Kali).

**Step 4 — lspci / lsmod wrappers:**
Replaces system `lspci` and `lsmod` with wrapper scripts that filter VM fingerprints:
- `lspci`: replaces VMware SVGA II entry with `Intel Corporation Alder Lake-P GT2 [Iris Xe Graphics]` in both standard and `-mm` (machine-readable) output
- `lsmod`: filters any module starting with `qemu`

Original binaries are preserved as `lspci.real` / `lsmod.real`. Wrappers are idempotent.

**After running:** reboot, then verify:
```bash
lsmod | grep qemu                          # should be empty
lspci | grep VGA                           # should show Intel Iris Xe
cat /sys/class/dmi/id/chassis_type         # should be 9
inxi -F                                    # should show: Type: Laptop  System: Dell Inc.
```

### Windows Guest Setup

```powershell
irm http://10.0.2.2:8080/guest_setup.ps1 | iex
```

Run in an **elevated PowerShell** (right-click → Run as Administrator).

**What it does (3 steps):**

**Step 1 — Firefox stealth profile:**
Creates `%APPDATA%\Mozilla\Firefox\Profiles\stealth\user.js` with the same WebGL overrides as Linux.

**Step 2 — Desktop shortcut:**
Creates `Stealth Browser.lnk` on the Desktop pointing to Firefox with `--profile stealth --no-remote`.

**Step 3 — GPU display name spoof (Admin required):**
Renames the GPU in Device Manager to `Intel(R) Iris(R) Xe Graphics` via two strategies:
- **Strategy 1:** Edits `DriverDesc` in the video class registry key (`{4d36e968...}`) — works when VMware SVGA or virtio driver is installed
- **Strategy 2:** Sets `FriendlyName` in the PCI enum key for `VEN_1234` (QEMU standard VGA) — works with the built-in `basicdisplay.sys` fallback driver

Reboot after running for Device Manager to reflect the change.

---

## Windows 11 Setup Workflow

### Pre-install config

```json
{
  "bios": "ovmf_ms",
  "tpm": true,
  "stealth": true,
  "hardened": true,
  "networks": []
}
```

- `bios: "ovmf_ms"` — uses `OVMF_CODE_4M.ms.fd` with Microsoft Secure Boot keys (required for Windows 11)
- `networks: []` — removes the NIC so Windows OOBE skips Microsoft account and offers local account creation

### OOBE local account bypass (Windows 11 25H2+)

`oobe\bypassnro` was removed in 25H2. Use one of these instead:

| Method | Steps |
|---|---|
| **Remove NIC (recommended)** | Set `networks: []` before install. OOBE shows "I don't have internet" → "Continue with limited setup" |
| **ms-cxh:localonly** | At the sign-in screen, press Shift+F10, type `start ms-cxh:localonly` |
| **Fake email** | Enter `a@a.com` + any password → fails → "Create a local account" link appears |
| **Sign-in options** | Bottom-left of sign-in screen → "Sign-in options" → "Domain join instead" |

### Post-install steps

1. Stop the VM, add the NIC back to config.json:
```json
"networks": [
  {
    "mode": "nat",
    "model": "e1000e",
    "mac": "F0:1F:AF:XX:XX:XX"
  }
]
```
2. Relaunch the VM
3. Start the HTTP server on the host: `cd ~/.qemu_vms/<vm-name> && python3 -m http.server 8080`
4. Run guest setup in elevated PowerShell: `irm http://10.0.2.2:8080/guest_setup.ps1 | iex`
5. Install Firefox
6. Reboot

---

## Bridge Networking (Wired LAN)

By default VMs use QEMU NAT (`10.0.2.x`). For a real LAN IP, create a bridge on the host once and attach the physical NIC to it.

### Ubuntu / Debian (netplan)

```yaml
# /etc/netplan/01-bridge.yaml
network:
  version: 2
  ethernets:
    enp3s0:            # your wired interface — check with: ip link
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

### VM config

```json
{
  "networks": [
    {
      "mode": "bridge",
      "bridge": "br0",
      "model": "e1000e"
    }
  ]
}
```

The VM gets a real DHCP IP from your router. Note: `hardened: true` forces NAT for non-stealth VMs, but stealth VMs bypass this restriction intentionally.

---

## TPM & Secure Boot

### TPM

Set `"tpm": true` in config. Requires `swtpm` on the host:

```bash
sudo apt install swtpm
```

The API starts `swtpm` automatically before launching the VM. TPM state is persisted in `~/.qemu_vms/<name>/tpm/`.

### Secure Boot

| `bios` value | File | Use case |
|---|---|---|
| `"ovmf"` | `OVMF_CODE_4M.fd` | Standard UEFI, no Secure Boot |
| `"ovmf_ms"` | `OVMF_CODE_4M.ms.fd` | Secure Boot with Microsoft keys (required for Windows 11) |

**Important:** Never add `smm=off` to machine args when using OVMF. OVMF requires SMM or Linux boot KVM-crashes at ~15 seconds.

UEFI variables (boot order, Secure Boot state) are stored per-VM in `~/.qemu_vms/<name>/OVMF_VARS.fd`.

---

## Stealth Rating Reference

Approximate stealth ratings achievable with this toolchain:

| Check | Stealth VM (after guest_setup) | Notes |
|---|---|---|
| `inxi -F` system/chassis | ✓ Shows real laptop identity | SMBIOS fields + chassis binary |
| `lsmod \| grep qemu` | ✓ Empty | Blacklist + lsmod wrapper |
| `lspci` GPU | ✓ Intel Iris Xe | lspci wrapper (Linux) / registry (Windows) |
| Firefox WebGL | ✓ Intel Iris Xe | user.js profile overrides |
| ACPI OEM strings | ✗ Still BOCHS/BXPC | Future project |
| `/sys` PCI vendor IDs | ✗ Still 1234:1111 / 15ad | lspci wrapper doesn't cover /sys |
| `dmesg` QEMU strings | ✗ Still visible | Future project |

**Approximate rating: 7/10** — sufficient to pass casual and automated fingerprinting checks.
