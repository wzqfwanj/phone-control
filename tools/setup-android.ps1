# 一次性工具链准备：JDK17 + Android cmdline-tools + build-tools/platform
# 用法: powershell -ExecutionPolicy Bypass -File tools/setup-android.ps1
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$tools = 'C:\dev-tools'
$sdk = 'C:\Android\Sdk'
New-Item -ItemType Directory -Force $tools | Out-Null

function Log($m) { $ts = Get-Date -Format 'HH:mm:ss'; Write-Host "[$ts] $m" }

# 1. JDK 17 (Temurin, tuna 镜像)
$jdkDir = Get-ChildItem $tools -Directory -Filter 'jdk-17*' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $jdkDir) {
    Log 'downloading JDK17 ...'
    $url = 'https://mirrors.tuna.tsinghua.edu.cn/Adoptium/17/jdk/x64/windows/OpenJDK17U-jdk_x64_windows_hotspot_17.0.21.1_2.zip'
    $zip = Join-Path $tools 'jdk17.zip'
    if (-not (Test-Path $zip)) { Invoke-WebRequest -Uri $url -OutFile $zip -TimeoutSec 900 }
    Log 'extracting JDK17 ...'
    Expand-Archive -Path $zip -DestinationPath $tools -Force
    $jdkDir = Get-ChildItem $tools -Directory -Filter 'jdk-17*' | Select-Object -First 1
}
$env:JAVA_HOME = $jdkDir.FullName
$env:PATH = "$($jdkDir.FullName)\bin;$env:PATH"
Log "JAVA_HOME=$env:JAVA_HOME"
& "$env:JAVA_HOME\bin\java.exe" -version 2>&1 | Out-Host

# 2. Android commandline-tools
$sdkmanager = Join-Path $sdk 'cmdline-tools\latest\bin\sdkmanager.bat'
if (-not (Test-Path $sdkmanager)) {
    Log 'downloading commandline-tools ...'
    $zip = Join-Path $tools 'cmdline-tools.zip'
    if (-not (Test-Path $zip)) {
        Invoke-WebRequest -Uri 'https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip' -OutFile $zip -TimeoutSec 900
    }
    Log 'extracting commandline-tools ...'
    $raw = Join-Path $tools 'cmdtools-raw'
    if (Test-Path $raw) { Remove-Item $raw -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $raw -Force
    $src = Join-Path $raw 'cmdline-tools'
    $dst = Join-Path $sdk 'cmdline-tools\latest'
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Move-Item $src $dst
}

# 3. SDK packages
Log 'installing platforms;android-34 build-tools;34.0.0 platform-tools ...'
$yes = ("y`n" * 40)
$yes | & $sdkmanager --sdk_root=$sdk --licenses 2>&1 | Select-Object -Last 3 | Out-Host
& $sdkmanager --sdk_root=$sdk 'platform-tools' 'platforms;android-34' 'build-tools;34.0.0' 2>&1 | Select-Object -Last 15 | Out-Host

Log 'DONE'
Get-ChildItem $sdk | Select-Object -ExpandProperty Name | Out-Host
Test-Path (Join-Path $sdk 'platforms\android-34\android.jar')
Test-Path (Join-Path $sdk 'build-tools\34.0.0\aapt2.exe')
Test-Path (Join-Path $sdk 'build-tools\34.0.0\apksigner.bat')
