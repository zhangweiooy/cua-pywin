param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,

    [int]$TimeoutSeconds = 300,

    [string]$FixtureImagePath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$nativeErrorPreference = Get-Variable `
    -Name PSNativeCommandUseErrorActionPreference `
    -ErrorAction SilentlyContinue
$previousNativeErrorPreference = $null
if ($null -ne $nativeErrorPreference) {
    $previousNativeErrorPreference = $nativeErrorPreference.Value
    $PSNativeCommandUseErrorActionPreference = $false
}

$resolvedExe = (Resolve-Path -LiteralPath $ExePath).Path
if (-not $FixtureImagePath) {
    $FixtureImagePath = Join-Path $PSScriptRoot (
        "..\tests\fixtures\icon_semantics\oryx\oryx-window-0.19.1.png"
    )
}
$resolvedFixtureImage = (Resolve-Path -LiteralPath $FixtureImagePath).Path
$bundleRoot = Join-Path (
    Split-Path -Parent $resolvedExe
) "computer_server\models\omniparser_v2\icon_caption_onnx_community"
$int8VisionPath = Join-Path $bundleRoot "int8\vision_encoder.int8.onnx"
$fp32VisionPath = Join-Path $bundleRoot "onnx\vision_encoder.onnx"
if (-not (Test-Path -LiteralPath $int8VisionPath -PathType Leaf)) {
    throw "Standalone bundle is missing the INT8 vision graph: $int8VisionPath"
}
if (Test-Path -LiteralPath $fp32VisionPath -PathType Leaf) {
    throw "Standalone bundle contains the deprecated FP32 vision graph: $fp32VisionPath"
}
$tempRoot = [System.IO.Path]::GetFullPath($env:TEMP)
$stateRoot = Join-Path $tempRoot (
    "cua-win-som-smoke-" +
    [DateTime]::UtcNow.ToString("yyyyMMddHHmmssfff")
)
$stateRoot = [System.IO.Path]::GetFullPath($stateRoot)
$tempPrefix = $tempRoot.TrimEnd('\') + '\'
if (-not $stateRoot.StartsWith(
    $tempPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "SOM smoke state path escaped the temp directory: $stateRoot"
}

$environmentNames = @(
    "APPDATA",
    "CUA_ENABLE_FLORENCE2",
    "CUA_OCR_TIMEOUT_SECONDS",
    "CUA_SKIP_SOM_WARMUP",
    "CUA_WIN_STATE_DIR",
    "HF_HOME",
    "HF_HUB_OFFLINE",
    "HF_MODULES_CACHE",
    "PYTHONIOENCODING",
    "TRANSFORMERS_OFFLINE"
)
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable(
        $name,
        "Process"
    )
}

$daemonPid = $null
$fixtureProcess = $null
$failure = $null
$cleanupFailure = $null

function Invoke-CuaJson {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $raw = (& $resolvedExe @Arguments 2>&1 | Out-String).Trim()
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw (
            "cua-win exited with code $exitCode for arguments " +
            "'$($Arguments -join ' ')': $raw"
        )
    }
    try {
        return ($raw | ConvertFrom-Json)
    }
    catch {
        throw "cua-win returned invalid JSON: $raw"
    }
}

try {
    New-Item -ItemType Directory -Path $stateRoot | Out-Null
    $privateAppData = Join-Path $stateRoot "appdata"
    $hfHome = Join-Path $stateRoot "huggingface"
    $hfModules = Join-Path $stateRoot "transformers-modules"
    New-Item -ItemType Directory -Path $privateAppData | Out-Null
    New-Item -ItemType Directory -Path $hfHome | Out-Null
    New-Item -ItemType Directory -Path $hfModules | Out-Null

    $env:APPDATA = $privateAppData
    $env:CUA_ENABLE_FLORENCE2 = "1"
    # Single-core release runners can take more than the 20-second production
    # budget on the first frozen RapidOCR inference. Keep this smoke bounded by
    # the outer 300-second command timeout without changing the runtime default.
    $env:CUA_OCR_TIMEOUT_SECONDS = "60"
    $env:CUA_SKIP_SOM_WARMUP = "1"
    $env:CUA_WIN_STATE_DIR = $stateRoot
    $env:HF_HOME = $hfHome
    $env:HF_HUB_OFFLINE = "1"
    $env:HF_MODULES_CACHE = $hfModules
    $env:PYTHONIOENCODING = "utf-8"
    $env:TRANSFORMERS_OFFLINE = "1"

    $fixtureTitle = "cua-win-som-fixture-" + [DateTime]::UtcNow.ToString(
        "yyyyMMddHHmmssfff"
    )
    $fixtureScriptPath = Join-Path $stateRoot "show-fixture.ps1"
    @'
param([string]$ImagePath, [string]$WindowTitle, [string]$HandlePath)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class CuaFixtureWindow {
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr handle, int command);
}
"@
$image = [System.Drawing.Image]::FromFile($ImagePath)
try {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = $WindowTitle
    $form.StartPosition = "CenterScreen"
    $form.ClientSize = New-Object System.Drawing.Size(1280, 861)
    $picture = New-Object System.Windows.Forms.PictureBox
    $picture.Dock = "Fill"
    $picture.Image = $image
    $picture.SizeMode = "Zoom"
    $form.Controls.Add($picture)
    $form.Show()
    [void][CuaFixtureWindow]::ShowWindow($form.Handle, 5)
    [string][int64]$form.Handle | Set-Content -LiteralPath $HandlePath -Encoding ascii
    [System.Windows.Forms.Application]::Run($form)
}
finally {
    $image.Dispose()
}
'@ | Set-Content -LiteralPath $fixtureScriptPath -Encoding utf8
    $fixtureHandlePath = Join-Path $stateRoot "fixture-hwnd.txt"
    $fixtureProcess = Start-Process `
        -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList @(
            "-NoProfile", "-Sta", "-ExecutionPolicy", "Bypass",
            "-File", $fixtureScriptPath,
            "-ImagePath", $resolvedFixtureImage,
            "-WindowTitle", $fixtureTitle,
            "-HandlePath", $fixtureHandlePath
        ) `
        -WindowStyle Hidden `
        -PassThru

    $daemon = Invoke-CuaJson -Arguments @(
        "start",
        "--timeout",
        [string]$TimeoutSeconds,
        "--json"
    )
    if ($daemon.status -ne "ok") {
        throw "Standalone daemon did not report status=ok"
    }
    $daemonPid = [int]$daemon.pid

    $fixtureWindow = $null
    $fixtureDeadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($null -eq $fixtureWindow -and [DateTime]::UtcNow -lt $fixtureDeadline) {
        if ($fixtureProcess.HasExited) {
            throw "SOM fixture window process exited before exposing a window"
        }
        if (Test-Path -LiteralPath $fixtureHandlePath -PathType Leaf) {
            $fixtureHandle = [int64](
                Get-Content -LiteralPath $fixtureHandlePath -Raw
            ).Trim()
            $fixtureWindow = @{
                id = $fixtureHandle
                pid = [int]$fixtureProcess.Id
            }
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if ($null -eq $fixtureWindow) {
        throw "SOM smoke fixture process did not expose a targetable HWND"
    }

    $windowStateArgsPath = Join-Path $stateRoot "window-state.json"
    @{
        window = @{
            id = [int64]$fixtureWindow.id
            pid = [int]$fixtureWindow.pid
        }
        force_som = $true
        max_nodes = 20
        agent_text_limit = 200
    } | ConvertTo-Json -Depth 10 | Set-Content `
        -LiteralPath $windowStateArgsPath `
        -Encoding utf8

    $windowState = Invoke-CuaJson -Arguments @(
        "call",
        "computer_get_window_state",
        ("@" + $windowStateArgsPath),
        "--json",
        "--timeout",
        [string]$TimeoutSeconds
    )
    $vision = $windowState.data.vision_metadata
    $semanticDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ($windowState.success -and $vision.icon_semantic_status -in @("idle", "loading") -and [DateTime]::UtcNow -lt $semanticDeadline) {
        Start-Sleep -Milliseconds 250
        $windowState = Invoke-CuaJson -Arguments @(
            "call", "computer_get_window_state", ("@" + $windowStateArgsPath),
            "--json", "--timeout", [string]$TimeoutSeconds
        )
        $vision = $windowState.data.vision_metadata
    }
    $captureMetadata = $windowState.data.capture.capture_metadata
    if (
        -not $windowState.success -or
        @($windowState.artifacts).Count -lt 1 -or
        $captureMetadata.capture_backend -ne "wgc" -or
        [int]$windowState.data.vision_node_count -lt 1 -or
        [int]$vision.num_icons -lt 1 -or
        [int]$vision.num_text -lt 1 -or
        $vision.icon_semantic_status -ne "ready" -or
        [int]$vision.icon_caption_count -lt 1
    ) {
        throw (
            "Standalone SOM smoke failed: " +
            "backend=$($captureMetadata.capture_backend), " +
            "vision_nodes=$($windowState.data.vision_node_count), " +
            "icons=$($vision.num_icons), text=$($vision.num_text), " +
            "icon_status=$($vision.icon_semantic_status), " +
            "captions=$($vision.icon_caption_count)"
        )
    }

    $generatedComtypesFiles = @(
        Get-ChildItem `
            -LiteralPath $privateAppData `
            -Recurse `
            -File `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*\comtypes_cache\*" }
    )
    if ($generatedComtypesFiles.Count -ne 0) {
        throw (
            "Standalone SOM smoke generated comtypes cache files: " +
            $generatedComtypesFiles.Count
        )
    }
}
catch {
    $daemonLog = Join-Path $stateRoot "cli-daemon.log"
    $logText = ""
    if (Test-Path -LiteralPath $daemonLog -PathType Leaf) {
        $logText = (Get-Content -LiteralPath $daemonLog -Raw).Trim()
    }
    $failure = [System.Exception]::new(
        (
            $_.Exception.Message +
            $(if ($logText) {
                "`nStandalone daemon log:`n$logText"
            } else {
                ""
            })
        ),
        $_.Exception
    )
}
finally {
    try {
        if (Test-Path -LiteralPath $resolvedExe) {
            & $resolvedExe stop 2>&1 | Out-Null
        }
        if ($null -ne $daemonPid) {
            Wait-Process -Id $daemonPid -Timeout 10 -ErrorAction SilentlyContinue
            if (Get-Process -Id $daemonPid -ErrorAction SilentlyContinue) {
                Stop-Process -Id $daemonPid -Force
                Wait-Process `
                    -Id $daemonPid `
                    -Timeout 10 `
                    -ErrorAction SilentlyContinue
            }
            if (Get-Process -Id $daemonPid -ErrorAction SilentlyContinue) {
                throw "Standalone daemon process did not exit: $daemonPid"
            }
        }
        if ($null -ne $fixtureProcess -and -not $fixtureProcess.HasExited) {
            [void]$fixtureProcess.CloseMainWindow()
            Wait-Process `
                -Id $fixtureProcess.Id `
                -Timeout 5 `
                -ErrorAction SilentlyContinue
            if (Get-Process -Id $fixtureProcess.Id -ErrorAction SilentlyContinue) {
                Stop-Process -Id $fixtureProcess.Id -Force
                Wait-Process `
                    -Id $fixtureProcess.Id `
                    -Timeout 10 `
                    -ErrorAction SilentlyContinue
            }
        }
        if (Test-Path -LiteralPath $stateRoot) {
            Remove-Item -LiteralPath $stateRoot -Recurse -Force
        }
        if (Test-Path -LiteralPath $stateRoot) {
            throw "SOM smoke state directory was not removed: $stateRoot"
        }
    }
    catch {
        $cleanupFailure = $_
    }
    finally {
        foreach ($name in $environmentNames) {
            [Environment]::SetEnvironmentVariable(
                $name,
                $previousEnvironment[$name],
                "Process"
            )
        }
        if ($null -ne $nativeErrorPreference) {
            $PSNativeCommandUseErrorActionPreference = (
                $previousNativeErrorPreference
            )
        }
    }
}

if ($null -ne $failure) {
    throw $failure
}
if ($null -ne $cleanupFailure) {
    throw $cleanupFailure
}

Write-Output "[OK] standalone SOM/OCR/Florence-2 runtime passed"
