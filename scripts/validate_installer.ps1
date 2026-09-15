param(
    [Parameter(Mandatory = $true)]
    [string]$SetupExe,

    [Parameter(Mandatory = $true)]
    [string]$SourceDir,

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

$resolvedSetup = (Resolve-Path -LiteralPath $SetupExe).Path
$resolvedSource = (Resolve-Path -LiteralPath $SourceDir).Path
$tempRoot = [System.IO.Path]::GetFullPath($env:TEMP)
$smokeRoot = Join-Path $tempRoot (
    "cua-win-installer-smoke-" +
    [DateTime]::UtcNow.ToString("yyyyMMddHHmmssfff")
)
$smokeRoot = [System.IO.Path]::GetFullPath($smokeRoot)
$tempPrefix = $tempRoot.TrimEnd('\') + '\'
if (-not $smokeRoot.StartsWith(
    $tempPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Installer smoke path escaped the temp directory: $smokeRoot"
}

$installDir = Join-Path $smokeRoot "installed"
$stateDir = Join-Path $smokeRoot "state"
$privateAppData = Join-Path $smokeRoot "appdata"
$uninstallKey = (
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\" +
    "{B99CB8B0-5DE5-4A0B-AEF9-69A6A3F64668}_is1"
)
$previousStateDir = $env:CUA_WIN_STATE_DIR
$previousSkipWarmup = $env:CUA_SKIP_SOM_WARMUP
$previousPythonIoEncoding = $env:PYTHONIOENCODING
$previousAppData = $env:APPDATA
$previousUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$daemonPid = $null
$failure = $null
$cleanupFailure = $null

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [string[]]$Arguments = @()
    )

    $process = Start-Process `
        -FilePath $Command `
        -ArgumentList $Arguments `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw (
            "$Command failed with exit code $($process.ExitCode) for arguments " +
            "'$($Arguments -join ' ')'"
        )
    }
}

function Invoke-CuaJson {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ExePath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $raw = (& $ExePath @Arguments 2>&1 | Out-String).Trim()
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw (
            "Installed cua-win exited with code $exitCode for arguments " +
            "'$($Arguments -join ' ')': $raw"
        )
    }
    try {
        return ($raw | ConvertFrom-Json)
    }
    catch {
        throw "Installed cua-win returned invalid JSON: $raw"
    }
}

function Get-ProcessesForExecutable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ExePath
    )

    $expectedPath = [System.IO.Path]::GetFullPath($ExePath)
    return @(
        Get-Process -Name "cua-win" -ErrorAction SilentlyContinue |
        Where-Object {
            try {
                $candidatePath = [System.IO.Path]::GetFullPath($_.Path)
                $candidatePath.Equals(
                    $expectedPath,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            }
            catch {
                $false
            }
        }
    )
}

function Stop-PrivateDaemon {
    param(
        [string]$ExePath
    )

    if ($ExePath -and (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        & $ExePath stop 2>&1 | Out-Null
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
            throw "Installed daemon process did not exit: $daemonPid"
        }
        $script:daemonPid = $null
    }
}

try {
    if (Test-Path -LiteralPath $uninstallKey) {
        throw (
            "Refusing to replace an existing cua-win installation during " +
            "installer validation: $uninstallKey"
        )
    }

    New-Item -ItemType Directory -Path $smokeRoot | Out-Null
    New-Item -ItemType Directory -Path $stateDir | Out-Null
    New-Item -ItemType Directory -Path $privateAppData | Out-Null
    $legacyVision = Join-Path $installDir (
        "computer_server\models\omniparser_v2\" +
        "icon_caption_onnx_community\onnx\vision_encoder.onnx"
    )
    New-Item -ItemType Directory -Path (Split-Path -Parent $legacyVision) -Force |
        Out-Null
    Set-Content -LiteralPath $legacyVision -Value "legacy fp32 vision graph"

    Invoke-NativeChecked -Command $resolvedSetup -Arguments @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/NOICONS",
        "/MERGETASKS=!addtopath",
        "/DIR=`"$installDir`""
    )

    $installedExe = Join-Path $installDir "cua-win.exe"
    $uninstaller = Join-Path $installDir "unins000.exe"
    if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) {
        throw "Installed cua-win.exe was not found: $installedExe"
    }
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw "Installer did not create an uninstaller: $uninstaller"
    }
    if (Test-Path -LiteralPath $legacyVision -PathType Leaf) {
        throw "Installer did not remove the legacy FP32 vision graph"
    }
    $int8Vision = Join-Path $installDir (
        "computer_server\models\omniparser_v2\" +
        "icon_caption_onnx_community\int8\vision_encoder.int8.onnx"
    )
    if (-not (Test-Path -LiteralPath $int8Vision -PathType Leaf)) {
        throw "Installer did not install the INT8 vision graph"
    }

    $sourceFiles = @(
        Get-ChildItem -LiteralPath $resolvedSource -Recurse -File
    )
    $missingOrDifferent = @(
        foreach ($sourceFile in $sourceFiles) {
            $relativePath = $sourceFile.FullName.Substring(
                $resolvedSource.Length
            ).TrimStart('\', '/')
            $installedFile = Join-Path $installDir $relativePath
            if (-not (Test-Path -LiteralPath $installedFile -PathType Leaf)) {
                $relativePath
                continue
            }
            if ((Get-Item -LiteralPath $installedFile).Length -ne $sourceFile.Length) {
                $relativePath
            }
        }
    )
    if ($missingOrDifferent.Count -ne 0) {
        throw (
            "Installed payload is missing or changed files: " +
            (($missingOrDifferent | Select-Object -First 10) -join ", ")
        )
    }

    $sourceExeHash = (Get-FileHash `
        -LiteralPath (Join-Path $resolvedSource "cua-win.exe") `
        -Algorithm SHA256).Hash
    $installedExeHash = (Get-FileHash `
        -LiteralPath $installedExe `
        -Algorithm SHA256).Hash
    if ($installedExeHash -ne $sourceExeHash) {
        throw "Installed cua-win.exe hash does not match the standalone source"
    }

    $env:CUA_WIN_STATE_DIR = $stateDir
    $env:CUA_SKIP_SOM_WARMUP = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:APPDATA = $privateAppData

    $doctor = Invoke-CuaJson -ExePath $installedExe -Arguments @(
        "doctor",
        "--json"
    )
    if (
        -not $doctor.ok -or
        [int]$doctor.summary.warn -ne 0 -or
        [int]$doctor.summary.fail -ne 0
    ) {
        throw (
            "Installed doctor failed: ok=$($doctor.ok), " +
            "warn=$($doctor.summary.warn), fail=$($doctor.summary.fail)"
        )
    }

    $tools = Invoke-CuaJson -ExePath $installedExe -Arguments @(
        "list-tools",
        "--json",
        "--no-daemon"
    )
    if (@($tools.tools).Count -ne 25) {
        throw "Installed package exposed $(@($tools.tools).Count) tools, expected 25"
    }

    $emptyArgsPath = Join-Path $smokeRoot "empty-arguments.json"
    "{}" | Set-Content -LiteralPath $emptyArgsPath -Encoding utf8
    $daemon = Invoke-CuaJson -ExePath $installedExe -Arguments @(
        "start",
        "--timeout",
        [string]$TimeoutSeconds,
        "--json"
    )
    if ($daemon.status -ne "ok") {
        throw "Installed daemon did not report status=ok"
    }
    $daemonPid = [int]$daemon.pid

    foreach ($toolName in @("computer_get_screen_size")) {
        $result = Invoke-CuaJson -ExePath $installedExe -Arguments @(
            "call",
            $toolName,
            ("@" + $emptyArgsPath),
            "--json",
            "--timeout",
            [string]$TimeoutSeconds
        )
        if (-not $result.success) {
            throw "Installed $toolName smoke failed"
        }
    }
    foreach ($capturePass in 1..3) {
        $result = Invoke-CuaJson -ExePath $installedExe -Arguments @(
            "call",
            "computer_screenshot",
            ("@" + $emptyArgsPath),
            "--json",
            "--timeout",
            [string]$TimeoutSeconds
        )
        if (
            -not $result.success -or
            @($result.artifacts).Count -ne 1 -or
            $result.artifacts[0].role -ne "screen"
        ) {
            throw "Installed screenshot smoke pass $capturePass failed"
        }
    }

    $getWindowArgsPath = Join-Path $smokeRoot "get-active-window.json"
    @{
        active = $true
    } | ConvertTo-Json -Depth 10 | Set-Content `
        -LiteralPath $getWindowArgsPath `
        -Encoding utf8
    $windowResult = Invoke-CuaJson -ExePath $installedExe -Arguments @(
        "call",
        "computer_get_window",
        ("@" + $getWindowArgsPath),
        "--json",
        "--timeout",
        [string]$TimeoutSeconds
    )
    if (-not $windowResult.success -or $null -eq $windowResult.data.window) {
        throw "Installed active-window lookup failed"
    }

    $windowStateArgsPath = Join-Path $smokeRoot "window-state.json"
    @{
        window = @{
            id = [int64]$windowResult.data.window.id
            pid = [int]$windowResult.data.window.pid
        }
        max_nodes = 20
        agent_text_limit = 200
    } | ConvertTo-Json -Depth 10 | Set-Content `
        -LiteralPath $windowStateArgsPath `
        -Encoding utf8
    $windowState = Invoke-CuaJson -ExePath $installedExe -Arguments @(
        "call",
        "computer_get_window_state",
        ("@" + $windowStateArgsPath),
        "--json",
        "--timeout",
        [string]$TimeoutSeconds
    )
    $captureMetadata = $windowState.data.capture.capture_metadata
    if (
        -not $windowState.success -or
        @($windowState.artifacts).Count -lt 1 -or
        $captureMetadata.capture_backend -ne "wgc" -or
        $windowState.data.uia_quality -notin @("rich", "ok", "poor") -or
        [int]$windowState.data.uia_node_count -lt 1
    ) {
        throw (
            "Installed UIA/WGC window-state smoke failed: " +
            "backend=$($captureMetadata.capture_backend), " +
            "uia_quality=$($windowState.data.uia_quality), " +
            "uia_nodes=$($windowState.data.uia_node_count)"
        )
    }
    Stop-PrivateDaemon -ExePath $installedExe

    $teachingValidator = Join-Path $PSScriptRoot (
        "validate_standalone_teaching.ps1"
    )
    foreach ($pass in 1..2) {
        & $teachingValidator `
            -ExePath $installedExe `
            -TimeoutSeconds $TimeoutSeconds
    }
    $somValidator = Join-Path $PSScriptRoot "validate_standalone_som.ps1"
    & $somValidator `
        -ExePath $installedExe `
        -TimeoutSeconds ([Math]::Max($TimeoutSeconds, 300))

    if (@(Get-ProcessesForExecutable -ExePath $installedExe).Count -ne 0) {
        throw "Installed cua-win process remained after smoke tests"
    }

    Invoke-NativeChecked -Command $uninstaller -Arguments @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART"
    )

    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while (
        (Test-Path -LiteralPath $installDir) -and
        [DateTime]::UtcNow -lt $deadline
    ) {
        Start-Sleep -Milliseconds 250
    }
    if (Test-Path -LiteralPath $installDir) {
        throw "Uninstaller did not remove the installation directory: $installDir"
    }
    if (Test-Path -LiteralPath $uninstallKey) {
        throw "Uninstaller left its registration behind: $uninstallKey"
    }
    if (
        [Environment]::GetEnvironmentVariable("Path", "User") -ne
        $previousUserPath
    ) {
        throw "Silent installer validation changed the current user's PATH"
    }
}
catch {
    $diagnostics = @()
    if ($null -ne $daemonPid) {
        $daemonProcess = Get-Process `
            -Id $daemonPid `
            -ErrorAction SilentlyContinue
        $diagnostics += "daemon_alive=$($null -ne $daemonProcess)"
        $diagnostics += "daemon_pid=$daemonPid"
    }
    $daemonLog = Join-Path $stateDir "cli-daemon.log"
    if (Test-Path -LiteralPath $daemonLog -PathType Leaf) {
        $logText = (Get-Content -LiteralPath $daemonLog -Raw).Trim()
        if ($logText) {
            $diagnostics += "daemon_log=$logText"
        }
    }
    $failure = [System.Exception]::new(
        (
            $_.Exception.Message +
            $(if ($diagnostics.Count) {
                "`nInstaller smoke diagnostics: " + ($diagnostics -join "; ")
            } else {
                ""
            })
        ),
        $_.Exception
    )
}
finally {
    try {
        $candidateExe = Join-Path $installDir "cua-win.exe"
        Stop-PrivateDaemon -ExePath $candidateExe

        $candidateUninstaller = Join-Path $installDir "unins000.exe"
        if (Test-Path -LiteralPath $candidateUninstaller -PathType Leaf) {
            Invoke-NativeChecked `
                -Command $candidateUninstaller `
                -Arguments @(
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART"
                )
        }

        $deadline = [DateTime]::UtcNow.AddSeconds(60)
        while (
            (Test-Path -LiteralPath $installDir) -and
            [DateTime]::UtcNow -lt $deadline
        ) {
            Start-Sleep -Milliseconds 250
        }
        if (Test-Path -LiteralPath $smokeRoot) {
            Remove-Item -LiteralPath $smokeRoot -Recurse -Force
        }
        if (Test-Path -LiteralPath $smokeRoot) {
            throw "Installer smoke directory was not removed: $smokeRoot"
        }
        if (
            [Environment]::GetEnvironmentVariable("Path", "User") -ne
            $previousUserPath
        ) {
            throw "Installer smoke changed the current user's PATH"
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

Write-Output "[OK] installer clean-environment lifecycle passed"
