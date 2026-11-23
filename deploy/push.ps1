# deploy/push.ps1 - sync phone-control to the dev host and restart its systemd service.
#
# Idempotent. Steps:
#   1. preflight: server/server.py, server/requirements.txt, web/dist/index.html exist locally
#   2. pack server/ + web/dist/ into tarballs, upload via scp to dev:/tmp/phone-control-push
#   3. run deploy/remote-install.sh on dev, which installs code under /opt/phone-control,
#      KEEPS an existing .env (creates one with a random DEVICE_KEY on first deploy),
#      creates/reuses .venv + pip installs server/requirements.txt, installs the systemd
#      unit, then daemon-reload + enable + restart and self-checks /healthz
#   4. print endpoints (+ DEVICE_KEY with -ShowDeviceKey)
#
# Usage (Windows PowerShell 5.1+):
#   powershell -ExecutionPolicy Bypass -File deploy/push.ps1
#   powershell -ExecutionPolicy Bypass -File deploy/push.ps1 -ShowDeviceKey
#
# Prerequisites:
#   - ~/.ssh/config Host dev (root, passwordless sudo on that host; `ssh dev` works key-based)
#   - local ssh/scp/tar (OpenSSH Client + Windows 10+ built-in tar)
#   - frontend already built: cd web; npm run build  (produces web/dist)
#
# Notes:
#   - The real .env (device pairing code) never leaves the dev host; this script only
#     creates it on first deploy and always preserves it afterwards.
#   - frp tunnel (dev:8090 -> aliyun:18090) and the Caddy reverse proxy are configured
#     once by hand; see deploy/README.md. This script only handles the app service.
param(
    [string]$Remote = "dev",
    [string]$RemoteRoot = "/opt/phone-control",
    [string]$ServiceName = "phone-control",
    [switch]$ShowDeviceKey
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-Native {
    param([string]$What)
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE)"
    }
}

function Fail-Preflight {
    param([string]$Message)
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

$Root = Split-Path -Parent $PSScriptRoot
$ServerDir = Join-Path $Root "server"
$DistDir = Join-Path $Root "web\dist"
$Stage = "/tmp/phone-control-push"

# --------------------------------------------------------------- preflight
Write-Step "preflight (local)"
if (-not (Test-Path -LiteralPath (Join-Path $ServerDir "server.py"))) {
    Fail-Preflight "server/server.py not found - the Python service must be implemented first."
}
if (-not (Test-Path -LiteralPath (Join-Path $ServerDir "requirements.txt"))) {
    Fail-Preflight "server/requirements.txt not found (expected: websockets)."
}
if (-not (Test-Path -LiteralPath (Join-Path $DistDir "index.html"))) {
    Fail-Preflight "web/dist/index.html not found - build the frontend first: cd web; npm run build"
}
foreach ($asset in @("phone-control.service", "env.example", "remote-install.sh")) {
    $path = Join-Path $PSScriptRoot $asset
    if (-not (Test-Path -LiteralPath $path)) {
        Fail-Preflight "missing deploy asset: $path"
    }
}
foreach ($exe in @("ssh", "scp", "tar")) {
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
        Fail-Preflight "$exe not found in PATH (OpenSSH Client / Windows 10+ tar required)."
    }
}

# --------------------------------------------------------------- connectivity
Write-Step "check ssh connectivity: $Remote"
ssh -o ConnectTimeout=10 -o BatchMode=yes $Remote "true"
Assert-Native "ssh $Remote"

$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("phone-control-push-" + [System.Guid]::NewGuid().ToString("N"))
$exitCode = 0
try {
    New-Item -ItemType Directory -Path $Tmp -Force | Out-Null

    # ----------------------------------------------------------- stage
    # Copy into an ASCII temp path first, so tar/scp never have to handle the
    # (possibly non-ASCII) repository path.
    Write-Step "pack server/ and web/dist/"
    Copy-Item -LiteralPath $ServerDir -Destination (Join-Path $Tmp "server") -Recurse -Force
    Copy-Item -LiteralPath $DistDir -Destination (Join-Path $Tmp "web") -Recurse -Force
    Get-ChildItem -Path (Join-Path $Tmp "server") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    tar -C (Join-Path $Tmp "server") -cf (Join-Path $Tmp "server.tar") .
    Assert-Native "tar server/"
    tar -C (Join-Path $Tmp "web") -cf (Join-Path $Tmp "web.tar") .
    Assert-Native "tar web/dist/"
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "phone-control.service") -Destination (Join-Path $Tmp "phone-control.service") -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "env.example") -Destination (Join-Path $Tmp "env.example") -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "remote-install.sh") -Destination (Join-Path $Tmp "remote-install.sh") -Force

    # ----------------------------------------------------------- upload
    Write-Step "upload staging to ${Remote}:$Stage"
    ssh $Remote "rm -rf $Stage && mkdir -p $Stage"
    Assert-Native "prepare $Stage on $Remote"
    scp -q `
        (Join-Path $Tmp "server.tar") `
        (Join-Path $Tmp "web.tar") `
        (Join-Path $Tmp "phone-control.service") `
        (Join-Path $Tmp "env.example") `
        (Join-Path $Tmp "remote-install.sh") `
        "${Remote}:$Stage/"
    Assert-Native "scp upload"

    # ----------------------------------------------------------- install
    Write-Step "install on $Remote (remote-install.sh)"
    ssh -o ConnectTimeout=15 $Remote "STAGE='$Stage' REMOTE_ROOT='$RemoteRoot' SERVICE_NAME='$ServiceName' bash '$Stage/remote-install.sh'"
    Assert-Native "remote install"

    # ----------------------------------------------------------- done
    Write-Step "deployed"
    Write-Host "  local healthz : http://127.0.0.1:8090/healthz (on $Remote)"
    Write-Host "  browser entry : https://wkeeli.cn/control/    (after the frp + Caddy steps in deploy/README.md)"
    Write-Host "  device channel: wss://wkeeli.cn/agent/ws/device?key=<DEVICE_KEY>"

    if ($ShowDeviceKey) {
        Write-Step "DEVICE_KEY"
        $keyLine = ssh $Remote "grep '^CONTROL_DEVICE_KEY=' '$RemoteRoot/.env'"
        Write-Host "  $keyLine"
    } else {
        Write-Host ""
        $hint = '  DEVICE_KEY (phone pairing code): ssh ' + $Remote + ' "grep ^CONTROL_DEVICE_KEY= ' + $RemoteRoot + '/.env"'
        Write-Host $hint
        Write-Host "  (re-run with -ShowDeviceKey to print it here)"
    }

    ssh $Remote "rm -rf '$Stage'" | Out-Null
} catch {
    $exitCode = 1
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Staging kept for debugging: ssh $Remote 'ls -l $Stage'" -ForegroundColor Yellow
} finally {
    if (Test-Path -LiteralPath $Tmp) {
        Remove-Item -LiteralPath $Tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($exitCode -ne 0) {
    exit $exitCode
}
