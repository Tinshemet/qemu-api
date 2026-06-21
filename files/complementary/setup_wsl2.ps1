# setup_wsl2.ps1 — Windows-side WSL2 port forwarding setup for qemu-api
#
# Run this in PowerShell as Administrator AFTER running setup_client.sh inside WSL2.
#
# What it does:
#   1. Gets the current WSL2 internal IP
#   2. Forwards Windows port 2222 -> WSL2 port 22 (SSH)
#   3. Adds a Windows Firewall rule to allow port 2222
#   4. Creates a scheduled Task that re-runs the port forward on every reboot
#      (necessary because the WSL2 IP changes on each Windows restart)
#
# Usage:
#   Right-click PowerShell -> Run as Administrator
#   cd to the directory containing this file
#   .\setup_wsl2.ps1
#
# To uninstall:
#   .\setup_wsl2.ps1 -Uninstall

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "qemu-api WSL2 port forward"
$FirewallRuleName = "qemu-api WSL2 SSH"
$WindowsPort = 2222
$WSL2Port = 22

# ── colour helpers ────────────────────────────────────────────────────────────
function Ok($msg)   { Write-Host "  [OK]  $msg" -ForegroundColor Green }
function Info($msg) { Write-Host "  [->]  $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "  [!]   $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "  [X]   $msg" -ForegroundColor Red }
function Header($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ── check admin ───────────────────────────────────────────────────────────────
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "This script must be run as Administrator."
    Fail "Right-click PowerShell and choose 'Run as Administrator'."
    exit 1
}

# ── uninstall ─────────────────────────────────────────────────────────────────
if ($Uninstall) {
    Header "Removing qemu-api WSL2 setup"

    try {
        netsh interface portproxy delete v4tov4 listenport=$WindowsPort listenaddress=0.0.0.0 2>$null
        Ok "Removed port proxy rule"
    } catch { Warn "Port proxy rule not found (already removed)" }

    try {
        Remove-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
        Ok "Removed firewall rule"
    } catch { Warn "Firewall rule not found" }

    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Ok "Removed scheduled task"
    } catch { Warn "Scheduled task not found" }

    # Remove the helper script
    $HelperScript = "$env:SystemRoot\System32\qemu-api-wsl2-forward.ps1"
    if (Test-Path $HelperScript) {
        Remove-Item $HelperScript -Force
        Ok "Removed helper script"
    }

    Write-Host "`nUninstall complete.`n" -ForegroundColor Green
    exit 0
}

# ── banner ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  qemu-api — WSL2 Windows setup" -ForegroundColor Cyan
Write-Host "  Configures SSH port forwarding from Windows to WSL2" -ForegroundColor Cyan
Write-Host ""

# ── check WSL2 is available ───────────────────────────────────────────────────
Header "WSL2 Check"

try {
    $wslStatus = wsl --status 2>&1
    Ok "WSL is installed"
} catch {
    Fail "WSL is not installed. Run: wsl --install"
    exit 1
}

# Get WSL2 IP
$WSL2IP = (wsl hostname -I 2>$null).Trim().Split(" ")[0]
if ([string]::IsNullOrEmpty($WSL2IP)) {
    Fail "Could not get WSL2 IP address."
    Fail "Make sure WSL2 is running (open a WSL2 terminal first)."
    exit 1
}
Ok "WSL2 IP: $WSL2IP"

# Verify SSH is running in WSL2
$sshCheck = wsl bash -c "ss -tlnp 2>/dev/null | grep ':22 '" 2>$null
if ([string]::IsNullOrEmpty($sshCheck)) {
    Warn "SSH does not appear to be running in WSL2."
    Warn "In WSL2 terminal, run: sudo service ssh start"
    Warn "Continuing setup anyway — start SSH before connecting."
}

# ── port forwarding ───────────────────────────────────────────────────────────
Header "Port Forwarding (Windows $WindowsPort -> WSL2 $WSL2Port)"

# Remove any existing rule for this port first (idempotent)
netsh interface portproxy delete v4tov4 `
    listenport=$WindowsPort listenaddress=0.0.0.0 2>$null | Out-Null

# Add the new rule
netsh interface portproxy add v4tov4 `
    listenport=$WindowsPort listenaddress=0.0.0.0 `
    connectport=$WSL2Port connectaddress=$WSL2IP | Out-Null

Ok "Port proxy: 0.0.0.0:$WindowsPort -> ${WSL2IP}:$WSL2Port"

# Verify
$proxy = netsh interface portproxy show v4tov4 | Select-String "$WindowsPort"
if ($proxy) {
    Ok "Verified port proxy is active"
} else {
    Warn "Could not verify port proxy — check: netsh interface portproxy show v4tov4"
}

# ── firewall rule ─────────────────────────────────────────────────────────────
Header "Windows Firewall"

# Remove existing rule if present
Remove-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue

New-NetFirewallRule `
    -DisplayName $FirewallRuleName `
    -Direction Inbound `
    -Protocol TCP `
    -LocalPort $WindowsPort `
    -Action Allow `
    -Profile Any | Out-Null

Ok "Firewall rule added: allow TCP $WindowsPort inbound"

# ── scheduled task — refresh port forward on reboot ──────────────────────────
Header "Scheduled Task (auto-refresh on reboot)"

# Write the helper script to System32 so Task Scheduler can find it
$HelperScript = "$env:SystemRoot\System32\qemu-api-wsl2-forward.ps1"

@"
# Auto-generated by setup_wsl2.ps1 — refreshes WSL2 port forward on reboot
`$WSL2Port = $WSL2Port
`$WindowsPort = $WindowsPort

# Wait for WSL2 to be available (may take a few seconds after boot)
`$attempts = 0
do {
    Start-Sleep -Seconds 3
    `$WSL2IP = (wsl hostname -I 2>`$null).Trim().Split(" ")[0]
    `$attempts++
} while ([string]::IsNullOrEmpty(`$WSL2IP) -and `$attempts -lt 10)

if ([string]::IsNullOrEmpty(`$WSL2IP)) {
    Write-EventLog -LogName Application -Source "qemu-api" -EventId 1 -EntryType Warning `
        -Message "qemu-api WSL2 port forward: could not get WSL2 IP after 30s" 2>`$null
    exit 1
}

netsh interface portproxy delete v4tov4 listenport=`$WindowsPort listenaddress=0.0.0.0 2>`$null
netsh interface portproxy add v4tov4 ``
    listenport=`$WindowsPort listenaddress=0.0.0.0 ``
    connectport=`$WSL2Port connectaddress=`$WSL2IP
"@ | Set-Content -Path $HelperScript -Encoding UTF8

Ok "Helper script written to $HelperScript"

# Register the scheduled task
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$HelperScript`""

$Trigger = New-ScheduledTaskTrigger -AtStartup

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false

$Principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Refreshes the WSL2->Windows port forward for qemu-api after each reboot" | Out-Null

Ok "Scheduled task '$TaskName' registered (runs as SYSTEM at startup)"

# ── gather info for the user ──────────────────────────────────────────────────
Header "Connection Info"

$PublicIP = (Invoke-WebRequest -Uri "https://ifconfig.me" -UseBasicParsing -TimeoutSec 5).Content.Trim()
$WSLUser  = (wsl bash -c "echo `$USER" 2>$null).Trim()

# Get token from WSL2 if it exists
$TokenRaw = (wsl bash -c "cat ~/.qemu-api.token 2>/dev/null").Trim()
$TokenDisplay = if ($TokenRaw) { $TokenRaw } else { "<your-token>" }

Write-Host ""
Write-Host "  Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "  Public IP  : $PublicIP"
Write-Host "  WSL2 user  : $WSLUser"
Write-Host "  SSH port   : $WindowsPort"
Write-Host "  API_TOKEN  : $TokenDisplay"
Write-Host ""
Write-Host "  On your laptop, run:" -ForegroundColor Cyan
Write-Host ""
Write-Host "    ssh -N \`" -ForegroundColor White
Write-Host "        -L 8080:127.0.0.1:8080 \`" -ForegroundColor White
Write-Host "        -L 5901:127.0.0.1:5901 \`" -ForegroundColor White
Write-Host "        -L 5902:127.0.0.1:5902 \`" -ForegroundColor White
Write-Host "        -p $WindowsPort ${WSLUser}@${PublicIP}" -ForegroundColor White
Write-Host ""
Write-Host "    export API_URL=http://localhost:8080" -ForegroundColor White
Write-Host "    export API_TOKEN=$TokenDisplay" -ForegroundColor White
Write-Host "    python3 provider/ollama_wrapper.py" -ForegroundColor White
Write-Host ""
Write-Host "  To uninstall: .\setup_wsl2.ps1 -Uninstall" -ForegroundColor DarkGray
Write-Host ""
