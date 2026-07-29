param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,

    [int]$TimeoutSeconds = 120
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
    "cua-win-teaching-smoke-" +
    [DateTime]::UtcNow.ToString("yyyyMMddHHmmssfff")
)
$stateRoot = [System.IO.Path]::GetFullPath($stateRoot)
$tempPrefix = $tempRoot.TrimEnd('\') + '\'
if (-not $stateRoot.StartsWith(
    $tempPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Teaching smoke state path escaped the temp directory: $stateRoot"
}

$previousStateDir = $env:CUA_WIN_STATE_DIR
$previousSkipWarmup = $env:CUA_SKIP_SOM_WARMUP
$previousPythonIoEncoding = $env:PYTHONIOENCODING
$previousAppData = $env:APPDATA
$daemonPid = $null
$failure = $null
$cleanupFailure = $null

function Invoke-CuaJson {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [int[]]$AllowedExitCodes = @(0)
    )

    $raw = (& $resolvedExe @Arguments 2>&1 | Out-String).Trim()
    $exitCode = $LASTEXITCODE
    if ($AllowedExitCodes -notcontains $exitCode) {
        throw (
            "cua-win exited with code $exitCode for arguments " +
            "'$($Arguments -join ' ')': $raw"
        )
    }
    try {
        $payload = $raw | ConvertFrom-Json
    }
    catch {
        throw "cua-win returned invalid JSON: $raw"
    }
    return [PSCustomObject]@{
        ExitCode = $exitCode
        Payload = $payload
    }
}

try {
    New-Item -ItemType Directory -Path $stateRoot | Out-Null
    $privateAppData = Join-Path $stateRoot "appdata"
    New-Item -ItemType Directory -Path $privateAppData | Out-Null
    $env:CUA_WIN_STATE_DIR = $stateRoot
    $env:CUA_SKIP_SOM_WARMUP = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:APPDATA = $privateAppData

    $learningOutput = Join-Path $stateRoot "learning"
    $sessionId = "standalone-teaching-smoke"
    $startArgsPath = Join-Path $stateRoot "learn-start.json"
    $statusArgsPath = Join-Path $stateRoot "learn-status.json"
    $stopArgsPath = Join-Path $stateRoot "learn-stop.json"
    @{
        action = "start"
        session_id = $sessionId
        output_dir = $learningOutput
    } | ConvertTo-Json -Depth 10 | Set-Content `
        -LiteralPath $startArgsPath `
        -Encoding utf8
    @{
        action = "status"
    } | ConvertTo-Json -Depth 10 | Set-Content `
        -LiteralPath $statusArgsPath `
        -Encoding utf8
    @{
        action = "stop"
        session_id = $sessionId
    } | ConvertTo-Json -Depth 10 | Set-Content `
        -LiteralPath $stopArgsPath `
        -Encoding utf8

    $daemon = Invoke-CuaJson -Arguments @(
        "start",
        "--timeout",
        [string]$TimeoutSeconds,
        "--json"
    )
    if ($daemon.Payload.status -ne "ok") {
        throw "Standalone daemon did not report status=ok"
    }
    $daemonPid = [int]$daemon.Payload.pid

    $start = Invoke-CuaJson -Arguments @(
        "call",
        "computer_learn",
        ("@" + $startArgsPath),
        "--json",
        "--timeout",
        [string]$TimeoutSeconds
    )
    if (
        -not $start.Payload.success -or
        $start.Payload.data.status -ne "recording"
    ) {
        throw "Teaching start did not enter recording state"
    }

    $recording = Invoke-CuaJson -Arguments @(
        "call",
        "computer_learn",
        ("@" + $statusArgsPath),
        "--json",
        "--timeout",
        [string]$TimeoutSeconds
    )
    if (
        -not $recording.Payload.success -or
        -not $recording.Payload.data.recording -or
        $recording.Payload.data.session_id -ne $sessionId
    ) {
        throw "Teaching status did not confirm recording"
    }

    $stop = Invoke-CuaJson `
        -Arguments @(
            "call",
            "computer_learn",
            ("@" + $stopArgsPath),
            "--json",
            "--timeout",
            [string]$TimeoutSeconds
        ) `
        -AllowedExitCodes @(0)
    if (
        -not $stop.Payload.success -or
        $stop.Payload.data.sessionId -ne $sessionId -or
        $null -eq $stop.Payload.data.cutoffSequence -or
        $null -eq $stop.Payload.data.actions -or
        $null -eq $stop.Payload.data.summary -or
        $null -eq $stop.Payload.data.summary.durationSeconds -or
        $null -eq $stop.Payload.data.summary.actionCount -or
        $null -eq $stop.Payload.data.summary.observationCount -or
        $null -eq $stop.Payload.data.summary.degradedObservationCount
    ) {
        throw "Teaching stop did not return the strict DesktopStopResult contract"
    }

    $idle = Invoke-CuaJson -Arguments @(
        "call",
        "computer_learn",
        ("@" + $statusArgsPath),
        "--json",
        "--timeout",
        [string]$TimeoutSeconds
    )
    if (
        -not $idle.Payload.success -or
        $idle.Payload.data.recording -or
        $idle.Payload.data.mode -ne "off"
    ) {
        throw "Teaching status did not return to idle"
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
            "Standalone Teaching generated comtypes cache files: " +
            $generatedComtypesFiles.Count
        )
    }
}
catch {
    $failure = $_
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
            throw "Teaching smoke state directory was not removed: $stateRoot"
        }
    }
    catch {
        $cleanupFailure = $_
    }
    finally {
        $env:CUA_WIN_STATE_DIR = $previousStateDir
        $env:CUA_SKIP_SOM_WARMUP = $previousSkipWarmup
        $env:PYTHONIOENCODING = $previousPythonIoEncoding
        $env:APPDATA = $previousAppData
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

Write-Output "[OK] standalone Teaching lifecycle passed"
