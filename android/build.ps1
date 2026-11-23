<#
.SYNOPSIS
    Build cn.wkeeli.phonecontrol APK without Gradle.

.DESCRIPTION
    Manual Android build chain:
        aapt2 compile -> aapt2 link -> javac -> d8 -> zip classes.dex into base apk
        -> zipalign -p 4 -> apksigner sign

    Toolchain (auto-detected, can be overridden with env vars):
        JDK 17 : $env:JAVA_HOME, or C:\dev-tools\jdk-17*
        SDK    : $env:ANDROID_HOME / $env:ANDROID_SDK_ROOT, or C:\Android\Sdk
                 needs platforms\android-34\android.jar + build-tools\34.0.0

    Build-time config comes from android\private.properties (gitignored), keys:
        server.url   default wss://wkeeli.cn/agent/ws/device
        device.key   default empty (App asks the user to fill it in)
        keystore.path / keystore.pass / key.alias / key.pass   signing (defaults below)

    Output: android\build\phone-control.apk

.NOTES
    aapt2.exe (native) cannot open absolute paths containing non-ASCII characters,
    so all workspace files are passed to it as paths relative to this directory.
    The script chdir's to its own folder first for exactly that reason.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File android\build.ps1
    powershell -ExecutionPolicy Bypass -File android\build.ps1 -Verify
    powershell -ExecutionPolicy Bypass -File android\build.ps1 -SkipSign
#>
[CmdletBinding()]
param(
    [switch]$SkipSign,
    [switch]$Verify
)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
# aapt2 (native, narrow-char main) breaks on absolute non-ASCII paths: work relative
Push-Location -LiteralPath $root
[Environment]::CurrentDirectory = $root

$relBuild = 'build'
$absBuild = Join-Path $root $relBuild

function Write-Step([string]$text) {
    Write-Host "[build] $text" -ForegroundColor Cyan
}

function Fail([string]$text) {
    Write-Host "[build] ERROR: $text" -ForegroundColor Red
    exit 1
}

function Assert-LastExit([string]$step) {
    if ($LASTEXITCODE -ne 0) {
        Fail "$step failed with exit code $LASTEXITCODE"
    }
}

# ---------------------------------------------------------------- toolchain

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

function Resolve-Sdk {
    $candidates = @()
    if ($env:ANDROID_HOME) { $candidates += $env:ANDROID_HOME }
    if ($env:ANDROID_SDK_ROOT) { $candidates += $env:ANDROID_SDK_ROOT }
    $candidates += 'C:\Android\Sdk'
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'Android\Sdk') }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path (Join-Path $candidate 'platform-tools'))) {
            return $candidate
        }
    }
    return $null
}

$jdkDir = Resolve-Jdk
if (-not $jdkDir) {
    Fail 'JDK not found. Install JDK 17 (e.g. to C:\dev-tools\jdk-17*) or set JAVA_HOME.'
}
$sdkDir = Resolve-Sdk
if (-not $sdkDir) {
    Fail 'Android SDK not found. Set ANDROID_HOME or install to C:\Android\Sdk.'
}

$javacExe = Join-Path $jdkDir 'bin\javac.exe'
$keytoolExe = Join-Path $jdkDir 'bin\keytool.exe'

$buildToolsRoot = Join-Path $sdkDir 'build-tools'
if (-not (Test-Path $buildToolsRoot)) {
    Fail "build-tools not found under $sdkDir. Install 'build-tools;34.0.0' with sdkmanager."
}
$buildToolsDir = Join-Path $buildToolsRoot '34.0.0'
if (-not (Test-Path (Join-Path $buildToolsDir 'aapt2.exe'))) {
    $fallback = @(Get-ChildItem $buildToolsRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'aapt2.exe') } |
        Sort-Object Name -Descending | Select-Object -First 1)
    if ($fallback.Count -eq 0) {
        Fail "aapt2.exe not found under $buildToolsRoot. Wait for SDK install / run sdkmanager."
    }
    $buildToolsDir = $fallback[0].FullName
    Write-Step "build-tools 34.0.0 not found, falling back to $buildToolsDir"
}

$aapt2 = Join-Path $buildToolsDir 'aapt2.exe'
$d8 = Join-Path $buildToolsDir 'd8.bat'
$zipalign = Join-Path $buildToolsDir 'zipalign.exe'
$apksigner = Join-Path $buildToolsDir 'apksigner.bat'
foreach ($tool in @($aapt2, $d8, $zipalign, $apksigner)) {
    if (-not (Test-Path $tool)) {
        Fail "missing build tool: $tool"
    }
}

$platformJar = Join-Path $sdkDir 'platforms\android-34\android.jar'
if (-not (Test-Path $platformJar)) {
    $fallbackPlatform = @(Get-ChildItem (Join-Path $sdkDir 'platforms') -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'android.jar') } |
        Sort-Object Name -Descending | Select-Object -First 1)
    if ($fallbackPlatform.Count -eq 0) {
        Fail "android.jar not found. Install 'platforms;android-34' with sdkmanager."
    }
    $platformJar = Join-Path $fallbackPlatform[0].FullName 'android.jar'
    Write-Step "android-34 not found, falling back to $platformJar"
}

$env:JAVA_HOME = $jdkDir
$env:Path = (Join-Path $jdkDir 'bin') + ';' + $env:Path

Write-Step "JDK        : $jdkDir"
Write-Step "SDK        : $sdkDir"
Write-Step "build-tools: $buildToolsDir"
Write-Step "android.jar: $platformJar"

# ---------------------------------------------------------------- build config

$settings = @{
    'server.url'    = 'wss://wkeeli.cn/agent/ws/device'
    'device.key'    = ''
    'keystore.path' = ''
    'keystore.pass' = 'android'
    'key.alias'     = 'androiddebugkey'
    'key.pass'      = 'android'
}
$privateFile = Join-Path $root 'private.properties'
if (Test-Path $privateFile) {
    foreach ($line in Get-Content -LiteralPath $privateFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ($trimmed.Length -eq 0 -or $trimmed.StartsWith('#') -or $trimmed.StartsWith(';')) {
            continue
        }
        $eq = $trimmed.IndexOf('=')
        if ($eq -lt 1) {
            continue
        }
        $name = $trimmed.Substring(0, $eq).Trim()
        $value = $trimmed.Substring($eq + 1).Trim()
        $settings[$name] = $value
    }
    Write-Step "config     : private.properties"
} else {
    Write-Step "config     : defaults (no private.properties)"
}

function ConvertTo-JavaString([string]$value) {
    if ($null -eq $value) {
        return ''
    }
    return $value.Replace('\', '\\').Replace('"', '\"').Replace("`r", '\r').Replace("`n", '\n').Replace("`t", '\t')
}

# ---------------------------------------------------------------- prepare dirs

Write-Step 'cleaning build dir'
if (Test-Path -LiteralPath $absBuild) {
    Remove-Item -LiteralPath $absBuild -Recurse -Force
}
$relGen = Join-Path $relBuild 'gen'
$relClasses = Join-Path $relBuild 'classes'
$relDex = Join-Path $relBuild 'dex'
foreach ($dir in @($absBuild, (Join-Path $root $relGen), (Join-Path $root $relClasses), (Join-Path $root $relDex))) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$buildConfigJava = Join-Path $root (Join-Path $relGen 'cn\wkeeli\phonecontrol\BuildConfig.java')
New-Item -ItemType Directory -Force -Path (Split-Path $buildConfigJava -Parent) | Out-Null
$buildConfigText = @"
package cn.wkeeli.phonecontrol;

/** Generated by android/build.ps1 - do not edit. */
public final class BuildConfig {
    public static final String SERVER_URL = "$(ConvertTo-JavaString $settings['server.url'])";
    public static final String DEVICE_KEY = "$(ConvertTo-JavaString $settings['device.key'])";
    public static final int VERSION_CODE = 1;
    public static final String VERSION_NAME = "1.0.0";

    private BuildConfig() {
    }
}
"@
Set-Content -LiteralPath $buildConfigJava -Value $buildConfigText -Encoding ASCII
Write-Step "generated BuildConfig.java (server.url=$($settings['server.url']), device.key set=$([bool]($settings['device.key'])))"

# ---------------------------------------------------------------- aapt2

Write-Step 'aapt2 compile'
$relResZip = Join-Path $relBuild 'res.zip'
& $aapt2 compile --dir (Join-Path '.' 'res') -o $relResZip
Assert-LastExit 'aapt2 compile'

Write-Step 'aapt2 link'
$relBaseApk = Join-Path $relBuild 'base.apk'
& $aapt2 link -o $relBaseApk -I $platformJar --manifest (Join-Path '.' 'AndroidManifest.xml') --java $relGen $relResZip
Assert-LastExit 'aapt2 link'

$rJava = Join-Path $root (Join-Path $relGen 'cn\wkeeli\phonecontrol\R.java')
if (-not (Test-Path -LiteralPath $rJava)) {
    Fail "aapt2 link did not generate R.java at $rJava"
}

# ---------------------------------------------------------------- javac

Write-Step 'javac'
$sourceFiles = @(Get-ChildItem -Path (Join-Path $root 'src') -Recurse -Filter '*.java' |
    ForEach-Object { $_.FullName.Substring($root.Length + 1) })
$sourceFiles += (Join-Path $relGen 'cn\wkeeli\phonecontrol\R.java')
$sourceFiles += (Join-Path $relGen 'cn\wkeeli\phonecontrol\BuildConfig.java')
Write-Step "compiling $($sourceFiles.Count) java files"

$javacArgs = @(
    '-encoding', 'UTF-8',
    '-nowarn',
    '-source', '8',
    '-target', '8',
    '-bootclasspath', $platformJar,
    '-d', $relClasses
) + $sourceFiles
& $javacExe @javacArgs
Assert-LastExit 'javac'

# ---------------------------------------------------------------- d8

Write-Step 'd8 (classes.dex)'
# d8 does not accept a directory: pack javac output into a jar first
$jarExe = Join-Path $jdkDir 'bin\jar.exe'
$relClassesJar = Join-Path $relBuild 'classes.jar'
& $jarExe cf $relClassesJar -C $relClasses .
Assert-LastExit 'jar (classes.jar)'
& $d8 --release --min-api 26 --lib $platformJar --output (Join-Path '.' $relDex) (Join-Path '.' $relClassesJar)
Assert-LastExit 'd8'
$dexFile = Join-Path $root (Join-Path $relDex 'classes.dex')
if (-not (Test-Path -LiteralPath $dexFile)) {
    Fail "d8 did not produce $dexFile"
}

# ---------------------------------------------------------------- package apk

Write-Step 'packaging classes.dex into apk'
Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null
Add-Type -AssemblyName System.IO.Compression | Out-Null
$unsignedApk = Join-Path $absBuild 'unsigned.apk'
Copy-Item -LiteralPath (Join-Path $root $relBaseApk) -Destination $unsignedApk -Force
$archive = [System.IO.Compression.ZipFile]::Open($unsignedApk, [System.IO.Compression.ZipArchiveMode]::Update)
try {
    $oldEntry = $archive.GetEntry('classes.dex')
    if ($oldEntry) { $oldEntry.Delete() }
    $newEntry = $archive.CreateEntry('classes.dex', [System.IO.Compression.CompressionLevel]::Optimal)
    $entryStream = $newEntry.Open()
    $dexStream = [System.IO.File]::OpenRead($dexFile)
    try {
        $dexStream.CopyTo($entryStream)
    } finally {
        $dexStream.Dispose()
        $entryStream.Dispose()
    }
} finally {
    $archive.Dispose()
}
Write-Step "classes.dex size: $((Get-Item -LiteralPath $dexFile).Length) bytes"

Write-Step 'zipalign -p -f 4'
$relUnsigned = Join-Path $relBuild 'unsigned.apk'
$relAligned = Join-Path $relBuild 'aligned.apk'
& $zipalign -p -f 4 $relUnsigned $relAligned
Assert-LastExit 'zipalign'

# ---------------------------------------------------------------- sign

$finalApk = Join-Path $absBuild 'phone-control.apk'
$relFinal = Join-Path $relBuild 'phone-control.apk'
if ($SkipSign) {
    Write-Step 'skipping signing (-SkipSign)'
    Copy-Item -LiteralPath (Join-Path $root $relAligned) -Destination $finalApk -Force
} else {
    $keystorePath = $settings['keystore.path']
    if ([string]::IsNullOrWhiteSpace($keystorePath)) {
        $keystorePath = Join-Path $root 'debug.keystore'
    } elseif (-not [System.IO.Path]::IsPathRooted($keystorePath)) {
        $keystorePath = Join-Path $root $keystorePath
    }
    if (-not (Test-Path -LiteralPath $keystorePath)) {
        Write-Step "generating debug keystore: debug.keystore"
        & $keytoolExe -genkeypair -v -keystore $keystorePath `
            -alias $settings['key.alias'] `
            -keyalg RSA -keysize 2048 -validity 10000 `
            -storepass $settings['keystore.pass'] `
            -keypass $settings['key.pass'] `
            -dname 'CN=Android Debug,O=Android,C=US'
        Assert-LastExit 'keytool'
    }
    Write-Step 'apksigner sign'
    & $apksigner sign `
        --ks $keystorePath `
        --ks-pass ('pass:' + $settings['keystore.pass']) `
        --key-pass ('pass:' + $settings['key.pass']) `
        --ks-key-alias $settings['key.alias'] `
        --out $relFinal $relAligned
    Assert-LastExit 'apksigner sign'
}

if (-not (Test-Path -LiteralPath $finalApk)) {
    Fail "expected apk not found: $finalApk"
}

# ---------------------------------------------------------------- verify

if (-not $SkipSign -or $Verify) {
    Write-Step 'apksigner verify'
    & $apksigner verify --print-certs $relFinal
    Assert-LastExit 'apksigner verify'
}

if ($Verify) {
    Write-Step 'aapt2 dump badging (package / sdk / permissions)'
    $badging = & $aapt2 dump badging $relFinal
    $badging | Where-Object {
        $_ -match '^package:' -or
        $_ -match '^sdkVersion:' -or
        $_ -match '^targetSdkVersion:' -or
        $_ -match '^uses-permission:'
    } | ForEach-Object { Write-Host $_ }
    Write-Step 'apk contents'
    $zip = [System.IO.Compression.ZipFile]::OpenRead($finalApk)
    try {
        $zip.Entries | ForEach-Object { Write-Host ("  " + $_.FullName + " (" + $_.Length + " bytes)") }
    } finally {
        $zip.Dispose()
    }
}

$sizeKb = [math]::Round((Get-Item -LiteralPath $finalApk).Length / 1KB, 1)
Write-Host ''
Write-Host "[build] OK -> $finalApk ($sizeKb KB)" -ForegroundColor Green
Write-Host "[build] install with: C:\Android\Sdk\platform-tools\adb.exe install -r `"$finalApk`"" -ForegroundColor Green
