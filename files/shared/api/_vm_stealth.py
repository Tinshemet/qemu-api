"""
_vm_stealth.py — VM Stealth Setup Mixin (guest fingerprint hardening scripts).

Provides _VmStealthMixin which is composed into QemuManager.
"""
import json
import os
import threading
from typing import Any, Dict

from .qemu_config import MachineConfig

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_STEALTH_CFG = _CFG.get("stealth", {})


class _VmStealthMixin:
    """Mixin providing guest stealth-setup script generation and completion tracking."""

    def generate_guest_setup(self, name: str) -> Dict[str, Any]:
        """Generate a guest-side stealth script for Linux or Windows.

        Writes a ``.sh`` or ``.ps1`` to ``~/.qemu_vms/<name>/`` that,
        when run inside the VM, blacklists QEMU kernel modules, installs
        a Firefox stealth WebGL profile, and patches display names.

        Args:
            name: VM name.

        Returns:
            ``{"success": True, "path": str, "vm": str, "cmd_template": str}``
            where ``cmd_template`` contains a ``{port}`` placeholder,
            or ``{"success": False, "error": str}``.

        Example::
            >>> mgr.generate_guest_setup("my-linux")
            {"success": True, "path": "~/.qemu_vms/my-linux/guest_setup.sh",
             "cmd_template": "curl http://10.0.2.2:{port}/guest_setup.sh | sudo bash"}
        """
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        vm_dir = os.path.expanduser(f"~/.qemu_vms/{name}")
        os.makedirs(vm_dir, exist_ok=True)

        mfr            = cfg.manufacturer or "Unknown"
        product        = cfg.product_name or "Unknown"
        webgl_renderer = _STEALTH_CFG.get("webgl_renderer", "Intel(R) Iris(R) Xe Graphics")
        webgl_vendor   = _STEALTH_CFG.get("webgl_vendor",   "Intel")

        is_windows = "windows" in (cfg.os_type or "").lower()

        if is_windows:
            return self._generate_guest_setup_windows(
                name, vm_dir, mfr, product, webgl_renderer, webgl_vendor,
            )
        return self._generate_guest_setup_linux(
            name, vm_dir, mfr, product, webgl_renderer, webgl_vendor,
        )

    def mark_stealth_done(self, name: str) -> Dict[str, Any]:
        """Create the ``.stealth_done`` sentinel and shut down the setup HTTP server.

        Args:
            name: VM name.

        Returns:
            ``{"success": True, "message": str}`` or error dict.

        Example::
            >>> mgr.mark_stealth_done("my-linux")
            {"success": True, "message": "Stealth setup for 'my-linux' marked complete..."}
        """
        vm_dir   = os.path.expanduser(f"~/.qemu_vms/{name}")
        sentinel = os.path.join(vm_dir, ".stealth_done")
        try:
            with open(sentinel, "w"):
                pass
        except OSError as e:
            return {"success": False, "error": str(e)}
        if name in self._setup_srvs:
            srv, _ = self._setup_srvs.pop(name)
            threading.Thread(target=srv.shutdown, daemon=True).start()
        return {
            "success": True,
            "message": f"Stealth setup for '{name}' marked complete — won't prompt again.",
        }

    # ------------------------------------------------------------------
    # Private script generators
    # ------------------------------------------------------------------

    def _generate_guest_setup_linux(
        self,
        name: str,
        vm_dir: str,
        mfr: str,
        product: str,
        webgl_renderer: str,
        webgl_vendor: str,
    ) -> Dict[str, Any]:
        """Write the Linux bash stealth script to ``vm_dir/guest_setup.sh``."""
        script_path = os.path.join(vm_dir, "guest_setup.sh")
        script = f"""\
#!/usr/bin/env bash
# Guest stealth setup for: {name} ({mfr} {product})
# Run once inside the VM as a regular user with sudo access.
set -euo pipefail

# ── Preflight: refuse to run on a live/uninstalled system ─────────────────────
if grep -qE '\\bboot=casper\\b|\\blive\\b|\\brd\\.live\\b' /proc/cmdline 2>/dev/null; then
    echo "ERROR: Live session detected (boot=casper / live in /proc/cmdline)."
    echo "       Install {mfr} {product} to disk first, then run this script."
    exit 1
fi
if [ ! -f /etc/fstab ] || ! grep -qv '^#' /etc/fstab 2>/dev/null; then
    echo "ERROR: No installed system detected (empty or missing /etc/fstab)."
    echo "       Complete the OS installation, reboot, then run this script."
    exit 1
fi
if ! command -v update-initramfs &>/dev/null && ! command -v mkinitcpio &>/dev/null && ! command -v dracut &>/dev/null; then
    echo "ERROR: No initramfs tool found (tried update-initramfs, mkinitcpio, dracut)."
    echo "       This script requires a fully installed Linux system."
    exit 1
fi

echo "=== Stealth guest setup: {name} ==="

# ── 1. Blacklist qemu_fw_cfg ─────────────────────────────────────────────────
echo "[1/4] Blacklisting qemu_fw_cfg..."
printf 'blacklist qemu_fw_cfg\\nblacklist cirrus_qemu\\n' | sudo tee /etc/modprobe.d/blacklist-qemu.conf >/dev/null
if command -v update-initramfs &>/dev/null; then
    sudo update-initramfs -u -k all
elif command -v mkinitcpio &>/dev/null; then
    sudo mkinitcpio -P
elif command -v dracut &>/dev/null; then
    sudo dracut --force
fi
echo "      Done — takes effect after reboot."

# ── 2. Firefox stealth profile ────────────────────────────────────────────────
echo "[2/4] Creating Firefox stealth profile..."
FIREFOX_BIN="$(command -v firefox 2>/dev/null || command -v firefox-esr 2>/dev/null || echo '')"
PROF_DIR="$HOME/.mozilla/firefox/stealth"
mkdir -p "$PROF_DIR"
cat > "$PROF_DIR/user.js" << 'USERJS'
user_pref("webgl.renderer-string.override", "{webgl_renderer}");
user_pref("webgl.vendor-string.override",   "{webgl_vendor}");
user_pref("webgl.disabled",       false);
user_pref("webgl.force-enabled",  true);
USERJS

if [ -n "$FIREFOX_BIN" ]; then
    PROF_INI="$HOME/.mozilla/firefox/profiles.ini"
    if ! grep -q "\\[Profile.*stealth\\]" "$PROF_INI" 2>/dev/null; then
        printf "\\n[Profile999]\\nName=stealth\\nIsRelative=1\\nPath=stealth\\n" >> "$PROF_INI"
    fi
fi
echo "      Profile written to $PROF_DIR"

# ── 3. Stealth browser launcher ───────────────────────────────────────────────
echo "[3/4] Creating stealth browser launcher..."
mkdir -p "$HOME/Desktop"
LAUNCHER="$HOME/Desktop/stealth-browser.sh"
if [ -n "$FIREFOX_BIN" ]; then
    printf '#!/usr/bin/env bash\\nexec %s --profile "$HOME/.mozilla/firefox/stealth" --no-remote "$@"\\n' "$FIREFOX_BIN" > "$LAUNCHER"
else
    printf '#!/usr/bin/env bash\\nexec firefox --profile "$HOME/.mozilla/firefox/stealth" --no-remote "$@"\\n' > "$LAUNCHER"
    echo "      WARNING: Firefox not found — install it then re-run, or edit the launcher."
fi
chmod +x "$LAUNCHER"
echo "      Launcher: $LAUNCHER"

# ── 4. lspci / lsmod stealth wrappers ────────────────────────────────────────
echo "[4/4] Installing lspci/lsmod stealth wrappers..."

LSPCI_BIN="$(command -v lspci 2>/dev/null || echo /usr/bin/lspci)"
if [ ! -x "${{LSPCI_BIN}}.real" ]; then
    sudo mv "$LSPCI_BIN" "${{LSPCI_BIN}}.real"
    sudo tee "$LSPCI_BIN" > /dev/null << 'LSPCI_WRAP'
#!/usr/bin/env python3
import subprocess, sys, re, os
real = os.path.realpath(sys.argv[0]) + '.real'
result = subprocess.run([real] + sys.argv[1:], capture_output=True, text=True)
out = result.stdout
out = re.sub(
    r'^[0-9a-f:.]+\\s+VGA compatible controller: VMware.*$',
    '00:02.0 VGA compatible controller: Intel Corporation Alder Lake-P GT2 [Iris Xe Graphics] (rev 0c)',
    out, flags=re.MULTILINE
)
def patch_block(block):
    if 'VMware' not in block or 'VGA' not in block:
        return block
    block = re.sub(r'^(Vendor:\\t).*$', r'\\1Intel Corporation', block, flags=re.MULTILINE)
    block = re.sub(r'^(Device:\\t).*$', r'\\1Alder Lake-P GT2 [Iris Xe Graphics]', block, flags=re.MULTILINE)
    block = re.sub(r'^(SVendor:\\t).*$', r'\\1Intel Corporation', block, flags=re.MULTILINE)
    block = re.sub(r'^(SDevice:\\t).*$', r'\\1Iris Xe Graphics', block, flags=re.MULTILINE)
    block = re.sub(r'^(Rev:\\t).*$', r'\\10c', block, flags=re.MULTILINE)
    return block
out = '\\n\\n'.join(patch_block(b) for b in out.split('\\n\\n'))
sys.stdout.write(out)
sys.exit(result.returncode)
LSPCI_WRAP
    sudo chmod +x "$LSPCI_BIN"
    echo "      lspci wrapper installed."
else
    echo "      lspci wrapper already present, skipping."
fi

LSMOD_BIN="$(command -v lsmod 2>/dev/null || echo /usr/sbin/lsmod)"
if [ ! -x "${{LSMOD_BIN}}.real" ]; then
    sudo mv "$LSMOD_BIN" "${{LSMOD_BIN}}.real"
    sudo tee "$LSMOD_BIN" > /dev/null << 'LSMOD_WRAP'
#!/usr/bin/env bash
REAL="$(dirname "$(readlink -f "$0")")/$(basename "$0").real"
"$REAL" "$@" | grep -v '^qemu'
LSMOD_WRAP
    sudo chmod +x "$LSMOD_BIN"
    echo "      lsmod wrapper installed."
else
    echo "      lsmod wrapper already present, skipping."
fi

echo ""
echo "=== Setup complete ==="
echo "REBOOT the VM for the qemu_fw_cfg blacklist to take effect."
echo "After reboot:"
echo "  lsmod | grep qemu          # should be empty"
echo "  cat /sys/class/dmi/id/chassis_type   # should be 9"
echo "  inxi -F                    # should show: Type: Laptop  System: {mfr}"
"""
        with open(script_path, "w") as f:
            f.write(script)
        os.chmod(script_path, 0o755)
        return {
            "success":      True,
            "path":         script_path,
            "vm":           name,
            "cmd_template": "curl http://10.0.2.2:{port}/guest_setup.sh | sudo bash",
        }

    def _generate_guest_setup_windows(
        self,
        name: str,
        vm_dir: str,
        mfr: str,
        product: str,
        webgl_renderer: str,
        webgl_vendor: str,
    ) -> Dict[str, Any]:
        """Write the Windows PowerShell stealth script to ``vm_dir/guest_setup.ps1``."""
        script_path = os.path.join(vm_dir, "guest_setup.ps1")
        script = f"""\
# Stealth guest setup for: {name} ({mfr} {product})
# Run once in an elevated PowerShell window (Run as Administrator) inside the VM.
$ErrorActionPreference = 'Stop'

Write-Host "=== Stealth guest setup: {name} ===" -ForegroundColor Cyan

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {{
    Write-Host "WARNING: Not running as Administrator. GPU spoof (step 3) will be skipped." -ForegroundColor Yellow
    Write-Host "         Re-run as Administrator for full stealth." -ForegroundColor Yellow
}}

# ── 1. Firefox stealth profile ────────────────────────────────────────────────
Write-Host "[1/3] Creating Firefox stealth profile..."
$profDir = "$env:APPDATA\\Mozilla\\Firefox\\Profiles\\stealth"
New-Item -ItemType Directory -Force -Path $profDir | Out-Null

Set-Content -Path "$profDir\\user.js" -Value @"
user_pref(`"webgl.renderer-string.override`", `"{webgl_renderer}`");
user_pref(`"webgl.vendor-string.override`",   `"{webgl_vendor}`");
user_pref(`"webgl.disabled`",      `$false);
user_pref(`"webgl.force-enabled`", `$true);
"@

$iniPath = "$env:APPDATA\\Mozilla\\Firefox\\profiles.ini"
if (Test-Path $iniPath) {{
    $ini = Get-Content $iniPath -Raw
    if ($ini -notmatch 'Profile999') {{
        Add-Content -Path $iniPath -Value "`n[Profile999]`nName=stealth`nIsRelative=1`nPath=Profiles/stealth"
    }}
}}
Write-Host "   Profile written to $profDir"

# ── 2. Desktop shortcut for stealth Firefox ───────────────────────────────────
Write-Host "[2/3] Creating desktop shortcut..."
$ffPaths = @(
    "$env:ProgramFiles\\Mozilla Firefox\\firefox.exe",
    "$env:LOCALAPPDATA\\Mozilla Firefox\\firefox.exe"
)
$created = $false
foreach ($ff in $ffPaths) {{
    if (Test-Path $ff) {{
        $ws  = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut("$env:USERPROFILE\\Desktop\\Stealth Browser.lnk")
        $lnk.TargetPath  = $ff
        $lnk.Arguments   = "--profile `"$profDir`" --no-remote"
        $lnk.Description = "Firefox with stealth WebGL profile"
        $lnk.Save()
        Write-Host "   Shortcut: $env:USERPROFILE\\Desktop\\Stealth Browser.lnk"
        $created = $true
        break
    }}
}}
if (-not $created) {{
    Write-Host "   Firefox not found — install it, then re-run this script." -ForegroundColor Yellow
}}

# ── 3. GPU display name spoof (admin required) ───────────────────────────────
Write-Host "[3/3] Spoofing GPU display name..."
if ($isAdmin) {{
    # Strategy 1: video class DriverDesc (works when VMware/virtio driver is installed)
    $videoClass = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{{4d36e968-e325-11ce-bfc1-08002be10318}}'
    if (Test-Path $videoClass) {{
        Get-ChildItem $videoClass -ErrorAction SilentlyContinue | ForEach-Object {{
            $desc = (Get-ItemProperty $_.PSPath -Name DriverDesc -ErrorAction SilentlyContinue).DriverDesc
            if ($desc -and ($desc -like '*VMware*' -or $desc -like '*SVGA*' -or $desc -like '*Standard VGA*' -or $desc -like '*Basic Display*')) {{
                Set-ItemProperty $_.PSPath -Name DriverDesc -Value '{webgl_renderer}' -ErrorAction SilentlyContinue
                $prov = (Get-ItemProperty $_.PSPath -Name ProviderName -ErrorAction SilentlyContinue).ProviderName
                if ($prov) {{ Set-ItemProperty $_.PSPath -Name ProviderName -Value '{webgl_vendor} Corporation' -ErrorAction SilentlyContinue }}
                Write-Host "   DriverDesc renamed: '$desc' -> '{webgl_renderer}'"
            }}
        }}
    }}
    # Strategy 2: FriendlyName in PCI enum key (works for basicdisplay.sys / no-driver case)
    $enumPci = 'HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\PCI'
    Get-ChildItem $enumPci -ErrorAction SilentlyContinue |
        Where-Object {{ $_.Name -like '*VEN_1234*' }} |
        ForEach-Object {{
            Get-ChildItem $_.PSPath -ErrorAction SilentlyContinue | ForEach-Object {{
                $p = $_.PSPath
                try {{
                    $acl = Get-Acl $p
                    $acl.SetAccessRule((New-Object System.Security.AccessControl.RegistryAccessRule(
                        $env:USERNAME, 'FullControl', 'Allow')))
                    Set-Acl $p $acl
                    Set-ItemProperty $p -Name FriendlyName -Value '{webgl_renderer}' -ErrorAction Stop
                    Write-Host "   FriendlyName set on $(Split-Path $p -Leaf)"
                }} catch {{
                    Write-Host "   Could not set FriendlyName: $_" -ForegroundColor Yellow
                }}
            }}
        }}
    Write-Host "   Reboot for Device Manager to reflect the change."
}} else {{
    Write-Host "   Skipped (not Administrator)." -ForegroundColor Yellow
}}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host "Launch Firefox via 'Stealth Browser' on your Desktop."
Write-Host "Verify WebGL at: https://browserleaks.com/webgl (expect: {webgl_renderer})"
"""
        with open(script_path, "w", newline="\r\n") as f:
            f.write(script)
        return {
            "success":      True,
            "path":         script_path,
            "vm":           name,
            "cmd_template": "irm http://10.0.2.2:{port}/guest_setup.ps1 | iex",
        }
