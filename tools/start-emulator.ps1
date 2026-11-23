# 启动 Android 模拟器（端到端测试用；复用已有的 wkeeli AVD，android-30 x86_64）
# 用法: powershell -ExecutionPolicy Bypass -File tools/start-emulator.ps1 [-Reset]
param([switch]$Reset)
$ErrorActionPreference = 'Continue'
$env:JAVA_HOME = (Get-ChildItem 'C:\dev-tools' -Directory -Filter 'jdk-17*' | Select-Object -First 1).FullName
$env:ANDROID_SDK_ROOT = 'C:\Android\Sdk'
$env:PATH = "$env:JAVA_HOME\bin;C:\Android\Sdk\platform-tools;C:\Android\Sdk\emulator;$env:PATH"
$avd = 'wkeeli'
$adb = 'C:\Android\Sdk\platform-tools\adb.exe'
function Log($m) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $m" }

& $adb start-server 2>&1 | Out-Null
$running = (& $adb devices 2>&1 | Out-String) -match 'emulator-\d+\s+device'
if ($running) {
    Log 'emulator already running'
} else {
    $args = @('-avd', $avd, '-no-window', '-no-audio', '-no-boot-anim', '-gpu', 'swiftshader_indirect', '-no-snapshot-save', '-port', '5554')
    if ($Reset) { $args += @('-wipe-data') }
    Log "starting emulator: $($args -join ' ')"
    Start-Process -FilePath 'C:\Android\Sdk\emulator\emulator.exe' -ArgumentList $args -WindowStyle Hidden | Out-Null
}

Log 'waiting for device ...'
& $adb -s emulator-5554 wait-for-device 2>&1 | Out-Null
$booted = ''
for ($i = 0; $i -lt 180; $i++) {
    $booted = (& $adb -s emulator-5554 shell getprop sys.boot_completed 2>&1 | Out-String).Trim()
    if ($booted -eq '1') { break }
    Start-Sleep -Seconds 2
}
Log "boot_completed=$booted"
if ($booted -ne '1') { Log 'BOOT_FAILED'; exit 1 }
& $adb -s emulator-5554 shell wm size 2>&1 | Out-Host
& $adb -s emulator-5554 shell getprop ro.build.version.sdk 2>&1 | Out-Host
Log 'EMULATOR_READY'
