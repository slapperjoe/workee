<#
.SYNOPSIS
    setup_guest_ndi.ps1 — Windows VM guest NDI receiver setup for MS Teams

.DESCRIPTION
    Downloads and installs the free NDI Tools suite on the Windows guest VM.
    Configures NDI Webcam Input to auto-discover the host's NDI desktop stream
    and present it as a virtual webcam that Microsoft Teams can use as a
    camera source.

    The host must already be running OBS with NDI output (see setup_host_ndi.sh).
    The VM must be on the same network subnet as the host (bridged networking).

.PARAMETER HostIP
    If mDNS auto-discovery fails (e.g. behind NAT), provide the host's IP.
    NDI Webcam Input will be configured to connect to this IP directly.

.PARAMETER InstallDir
    Directory to install NDI Tools into (default: C:\Program Files\NDI)

.PARAMETER NoInstall
    Skip the NDI Tools download/install step (assume already installed).

.EXAMPLE
    .\setup_guest_ndi.ps1
    Full setup with auto-discovery.

.EXAMPLE
    .\setup_guest_ndi.ps1 -HostIP 192.168.122.1
    Full setup with explicit host IP (bypasses mDNS discovery).

.EXAMPLE
    .\setup_guest_ndi.ps1 -NoInstall
    Only configure, don't re-download.

.NOTES
    Requires: Windows 10/11, PowerShell 5.1+, internet access for download.
    NDI Tools v6.3.2 — free download from ndi.video.
#>

[CmdletBinding()]
param(
    [string]$HostIP = "",
    [string]$InstallDir = "${env:ProgramFiles}\NDI",
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"
$NDI_TOOLS_URL = "https://downloads.ndi.tv/Tools/NDI6Tools/NDI_6_Tools.exe"
$NDI_TOOLS_INSTALLER = "$env:TEMP\NDI_6_Tools.exe"
$NDI_WEBCAM_EXE = "$InstallDir\NDI Webcam Input\NDIWebcamInput.exe"
$NDI_ACCESS_MGR_EXE = "$InstallDir\NDI Access Manager\AccessManager.exe"

# ============================================================================
# Step 1: Download & install NDI Tools
# ============================================================================

if (-not $NoInstall) {
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host " Step 1/3: Installing NDI Tools" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan

    if (Test-Path $NDI_WEBCAM_EXE) {
        Write-Host "[INFO]  NDI Tools already installed at $InstallDir" -ForegroundColor Green
        Write-Host "[INFO]  Use -NoInstall to skip this step on re-runs." -ForegroundColor Green
    }
    else {
        Write-Host "[INFO]  Downloading NDI Tools v6 installer..." -ForegroundColor Green
        Write-Host "[INFO]  URL: $NDI_TOOLS_URL" -ForegroundColor Green

        # Use BITS transfer for reliability with large files
        try {
            Start-BitsTransfer -Source $NDI_TOOLS_URL -Destination $NDI_TOOLS_INSTALLER `
                -DisplayName "NDI Tools v6" -ErrorAction Stop
        }
        catch {
            Write-Host "[WARN]  BITS failed, falling back to Invoke-WebRequest..." -ForegroundColor Yellow
            Invoke-WebRequest -Uri $NDI_TOOLS_URL -OutFile $NDI_TOOLS_INSTALLER
        }

        if (-not (Test-Path $NDI_TOOLS_INSTALLER)) {
            Write-Error "Failed to download NDI Tools installer"
            exit 1
        }

        Write-Host "[INFO]  Installing NDI Tools (silent, ~90 seconds)..." -ForegroundColor Green
        Write-Host "[INFO]  Installer window may flash briefly — this is normal." -ForegroundColor Green

        # NDI Tools installer supports /VERYSILENT /SUPPRESSMSGBOXES (Inno Setup)
        $process = Start-Process -FilePath $NDI_TOOLS_INSTALLER `
            -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /DIR=`"$InstallDir`"" `
            -Wait -PassThru

        if ($process.ExitCode -ne 0) {
            Write-Host "[WARN]  Installer exited with code $($process.ExitCode)" -ForegroundColor Yellow
            Write-Host "[INFO]  Trying per-user install fallback..." -ForegroundColor Green
            $localDir = "$env:LOCALAPPDATA\Programs\NDI"
            Start-Process -FilePath $NDI_TOOLS_INSTALLER `
                -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /DIR=`"$localDir`"" `
                -Wait
            $InstallDir = $localDir
            $NDI_WEBCAM_EXE = "$InstallDir\NDI Webcam Input\NDIWebcamInput.exe"
        }

        Write-Host "[INFO]  NDI Tools installed to $InstallDir" -ForegroundColor Green
    }
}
else {
    Write-Host "[INFO]  Skipping install (--NoInstall)" -ForegroundColor Yellow
}

# ============================================================================
# Step 2: Configure NDI Access Manager (firewall / network)
# ============================================================================

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Step 2/3: Configuring NDI network access" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# NDI Access Manager controls which network interfaces NDI uses and
# which remote NDI sources are visible. We want to allow incoming NDI
# streams from the host.

if (Test-Path $NDI_ACCESS_MGR_EXE) {
    Write-Host "[INFO]  NDI Access Manager is available." -ForegroundColor Green

    # Registry path for NDI Access Manager settings
    $regPath = "HKCU:\Software\NDI\AccessManager"
    if (-not (Test-Path $regPath)) {
        New-Item -Path $regPath -Force | Out-Null
    }

    # Enable reception of remote NDI sources
    Set-ItemProperty -Path $regPath -Name "ReceiveRemoteSources" -Value 1 -Type DWord -Force
    Set-ItemProperty -Path $regPath -Name "AllowMulticast" -Value 1 -Type DWord -Force

    Write-Host "[INFO]  Remote NDI source reception enabled." -ForegroundColor Green
}
else {
    Write-Host "[WARN]  NDI Access Manager not found — network settings may need" -ForegroundColor Yellow
    Write-Host "[WARN]  manual configuration via the NDI Tools system tray icon." -ForegroundColor Yellow
}

# ============================================================================
# Step 3: Configure NDI Webcam Input
# ============================================================================

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Step 3/3: Configuring NDI Webcam Input" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# NDI Webcam Input presents an NDI source as a DirectShow virtual webcam.
# The source selection is persisted in the registry under:
#   HKCU\Software\NDI\Webcam\...
#
# We'll configure it to auto-connect to the host's NDI stream.

$regPath = "HKCU:\Software\NDI\Webcam"
if (-not (Test-Path $regPath)) {
    New-Item -Path $regPath -Force | Out-Null
}

# Auto-connect to source named "HostDesktop" (matching the host OBS profile)
if ($HostIP) {
    # Explicit IP: use the NDI source URI format
    $sourceUri = "ndi://${HostIP}/HostDesktop"
    Write-Host "[INFO]  Configuring for explicit host IP: $HostIP" -ForegroundColor Green
    Set-ItemProperty -Path $regPath -Name "SourceName" -Value $sourceUri -Type String -Force
}
else {
    # Auto-discovery: NDI will use mDNS to find "HostDesktop" on the LAN
    Write-Host "[INFO]  Configuring for auto-discovery (mDNS)" -ForegroundColor Green
    Set-ItemProperty -Path $regPath -Name "SourceName" -Value "HostDesktop" -Type String -Force
}

# Additional webcam settings
Set-ItemProperty -Path $regPath -Name "AutoConnect" -Value 1 -Type DWord -Force
Set-ItemProperty -Path $regPath -Name "ShowOnStartup" -Value 1 -Type DWord -Force
Set-ItemProperty -Path $regPath -Name "BufferCount" -Value 2 -Type DWord -Force   # low latency

Write-Host "[INFO]  NDI Webcam Input configured." -ForegroundColor Green

# ============================================================================
# Verify and launch
# ============================================================================

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Verification" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

if (Test-Path $NDI_WEBCAM_EXE) {
    Write-Host "[OK]    NDI Webcam Input found at:" -ForegroundColor Green
    Write-Host "        $NDI_WEBCAM_EXE" -ForegroundColor Green

    # Check if it's already running
    $running = Get-Process -Name "NDIWebcamInput" -ErrorAction SilentlyContinue
    if ($running) {
        Write-Host "[INFO]  NDI Webcam Input is already running (PID $($running.Id))" -ForegroundColor Green
    }
    else {
        Write-Host "[INFO]  Starting NDI Webcam Input..." -ForegroundColor Green
        Start-Process -FilePath $NDI_WEBCAM_EXE -WindowStyle Minimized
        Start-Sleep -Seconds 3
        Write-Host "[INFO]  NDI Webcam Input launched." -ForegroundColor Green
    }
}
else {
    Write-Host "[FAIL]  NDI Webcam Input executable not found." -ForegroundColor Red
    Write-Host "        Expected: $NDI_WEBCAM_EXE" -ForegroundColor Red
    Write-Host "        Try running without -NoInstall or check the install path." -ForegroundColor Red
    exit 2
}

# Check that the virtual webcam device is visible to Windows
Write-Host ""
Write-Host "[INFO]  Checking for NDI virtual webcam device..." -ForegroundColor Green
try {
    # Use Get-PnpDevice to look for the NDI Webcam Input virtual camera
    $cam = Get-PnpDevice -Class Camera -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "*NDI*" -or $_.Name -like "*NewTek*" }
    if ($cam) {
        Write-Host "[OK]    Virtual camera found: $($cam.Name)" -ForegroundColor Green
    }
    else {
        Write-Host "[WARN]  NDI virtual camera not found in device list." -ForegroundColor Yellow
        Write-Host "        This can take a moment after first launch." -ForegroundColor Yellow
        Write-Host "        Restart NDI Webcam Input or reboot the VM if it doesn't appear." -ForegroundColor Yellow
    }
}
catch {
    Write-Host "[WARN]  Could not query camera devices: $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Guest NDI setup complete!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host " Next steps:" -ForegroundColor White
Write-Host "  1. Make sure the host is running OBS with the NDI profile:" -ForegroundColor White
Write-Host "       obs --profile NDI_ScreenShare --collection NDI_ScreenShare" -ForegroundColor White
Write-Host ""
Write-Host "  2. Open Microsoft Teams on this VM." -ForegroundColor White
Write-Host "  3. In a meeting, click Camera → 'NDI Webcam Input'." -ForegroundColor White
Write-Host "  4. Your host desktop appears as your video feed." -ForegroundColor White
Write-Host ""
if ($HostIP) {
    Write-Host "  NOTE: Using explicit host IP $HostIP for NDI source." -ForegroundColor Yellow
    Write-Host "  If mDNS discovery works, omit -HostIP on next run." -ForegroundColor Yellow
}
else {
    Write-Host "  If the host NDI source is not found:" -ForegroundColor Yellow
    Write-Host "    - Verify the VM uses bridged networking (not NAT)" -ForegroundColor Yellow
    Write-Host "    - Re-run with -HostIP <host-bridge-ip>" -ForegroundColor Yellow
}
Write-Host ""
