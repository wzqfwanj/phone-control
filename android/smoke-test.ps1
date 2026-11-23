<#
.SYNOPSIS
    WsClient 冒烟自测（纯 JDK，不需要 Android 设备）。

.DESCRIPTION
    默认启动 android\tools\ws_echo_server.py（本机 python + websockets），
    然后用 JDK 编译 src\...\WsClient.java + tools\...\WsSmokeTest.java 并连接它，
    验证 WebSocket 握手 / 文本帧 / 二进制帧 / 服务端消息 / ping-pong。

    如果把 server/server.py 跑起来了，也可以直接测真服务端：
        powershell -File android\smoke-test.ps1 -Url "ws://127.0.0.1:8090/agent/ws/device?key=<配对码>"

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File android\smoke-test.ps1
#>
[CmdletBinding()]
param(
    [string]$Url = '',
    [int]$Port = 8099
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Push-Location -LiteralPath $root
[Environment]::CurrentDirectory = $root

function Write-Step([string]$text) {
    Write-Host "[smoke] $text" -ForegroundColor Cyan
}

function Fail([string]$text) {
    Write-Host "[smoke] ERROR: $text" -ForegroundColor Red
    exit 1
}

function Resolve-Jdk {
    $candidates = @()
    if ($env:JAVA_HOME) { $candidates += $env:JAVA_HOME }
    if (Test-Path 'C:\dev-tools') {
        $candidates += @(Get-ChildItem 'C:\dev-tools' -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'jdk*' } | Sort-Object Name -Descending |
            ForEach-Object { $_.FullName })
    }
    $javacCommand = Get-Command javac -ErrorAction SilentlyContinue
    if ($javacCommand) {
        $candidates += (Split-Path (Split-Path $javacCommand.Source -Parent) -Parent)
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path (Join-Path $candidate 'bin\javac.exe'))) {
            return $candidate
        }
    }
    return $null
}

$jdkDir = Resolve-Jdk
if (-not $jdkDir) {
    Fail 'JDK not found. Install JDK 17 or set JAVA_HOME.'
}
$javacExe = Join-Path $jdkDir 'bin\javac.exe'
$javaExe = Join-Path $jdkDir 'bin\java.exe'
Write-Step "JDK: $jdkDir"

$outDir = Join-Path $root 'build\wstest'
if (Test-Path -LiteralPath $outDir) {
    Remove-Item -LiteralPath $outDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

Write-Step 'compiling WsClient + WsSmokeTest (plain JDK, no android.jar)'
$sources = @(
    (Join-Path '.' 'src\cn\wkeeli\phonecontrol\WsClient.java'),
    (Join-Path '.' 'tools\cn\wkeeli\phonecontrol\WsSmokeTest.java')
)
& $javacExe -encoding UTF-8 -nowarn -d (Join-Path '.' 'build\wstest') $sources
if ($LASTEXITCODE -ne 0) {
    Fail "javac failed with exit code $LASTEXITCODE"
}

$serverProcess = $null
$serverLog = Join-Path $outDir 'server.log'
if ([string]::IsNullOrWhiteSpace($Url)) {
    $Url = "ws://127.0.0.1:$Port/agent/ws/device?key=test"
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Fail 'python not found; cannot start tools/ws_echo_server.py'
    }
    $echoServer = Join-Path $root 'tools\ws_echo_server.py'
    Write-Step "starting echo server: python $echoServer $Port"
    $serverProcess = Start-Process -FilePath $python.Source `
        -ArgumentList @($echoServer, $Port) `
        -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $serverLog `
        -RedirectStandardError (Join-Path $outDir 'server.err.log')
    Start-Sleep -Milliseconds 1500
    if ($serverProcess.HasExited) {
        Fail "echo server exited early; see $serverLog"
    }
}

try {
    Write-Step "running WsSmokeTest -> $Url"
    & $javaExe -cp (Join-Path '.' 'build\wstest') cn.wkeeli.phonecontrol.WsSmokeTest $Url
    $code = $LASTEXITCODE
} finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
    }
}

if (Test-Path -LiteralPath $serverLog) {
    Write-Step 'echo server log'
    Get-Content -LiteralPath $serverLog | ForEach-Object { Write-Host "  $_" }
}

if ($code -ne 0) {
    Fail "WsSmokeTest failed with exit code $code"
}
Write-Host ''
Write-Host '[smoke] OK' -ForegroundColor Green
exit 0
