param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,

    [int]$TimeoutSeconds = 300
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

    $getWindowArgsPath = Join-Path $stateRoot "get-active-window.json"
    @{
        active = $true
    } | ConvertTo-Json -Depth 10 | Set-Content `
        -LiteralPath $getWindowArgsPath `
        -Encoding utf8

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

    $windowResult = Invoke-CuaJson -Arguments @(
        "call",
        "computer_get_window",
        ("@" + $getWindowArgsPath),
        "--json",
        "--timeout",
        [string]$TimeoutSeconds
    )
    if (-not $windowResult.success -or $null -eq $windowResult.data.window) {
        throw "SOM smoke could not resolve the active window"
    }

    $windowStateArgsPath = Join-Path $stateRoot "window-state.json"
    @{
        window = @{
            id = [int64]$windowResult.data.window.id
            pid = [int]$windowResult.data.window.pid
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
