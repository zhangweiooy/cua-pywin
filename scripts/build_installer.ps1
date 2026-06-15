param(
    [string]$OutDir = "dist-installer",

    [string]$SourceDir = "dist-nuitka/cua-win.dist",

    [string]$Version = "",

    [string]$ISCCPath = "",

    [switch]$BuildNuitka,

    [switch]$SkipSync,

    [switch]$SkipChecks,

    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
}
$env:PYTHONIOENCODING = "utf-8"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [string[]]$Arguments = @()
    )

    $nativeErrorPreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    $previousNativeErrorPreference = $null
    if ($null -ne $nativeErrorPreference) {
        $previousNativeErrorPreference = $nativeErrorPreference.Value
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        & $Command @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Command failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        if ($null -ne $nativeErrorPreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativeErrorPreference
        }
    }
}

function Get-ProjectVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot
    )

    $ProjectFile = Join-Path $ProjectRoot "pyproject.toml"
    $Text = Get-Content -LiteralPath $ProjectFile -Raw
    $Match = [regex]::Match($Text, '(?m)^version\s*=\s*"([^"]+)"')
    if (-not $Match.Success) {
        throw "Could not read project version from $ProjectFile"
    }
    return $Match.Groups[1].Value
}

function Resolve-InnoCompiler {
    param(
        [string]$ExplicitPath
    )

    $Candidates = @()
    if ($ExplicitPath) {
        $Candidates += $ExplicitPath
    }

    $Command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($Command) {
        $Candidates += $Command.Source
    }

    $ProgramFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    $ProgramFiles = [Environment]::GetEnvironmentVariable("ProgramFiles")
    $LocalAppData = [Environment]::GetEnvironmentVariable("LOCALAPPDATA")
    if ($LocalAppData) {
        $Candidates += (Join-Path $LocalAppData "Programs/Inno Setup 6/ISCC.exe")
    }
    if ($ProgramFilesX86) {
        $Candidates += (Join-Path $ProgramFilesX86 "Inno Setup 6/ISCC.exe")
    }
    if ($ProgramFiles) {
        $Candidates += (Join-Path $ProgramFiles "Inno Setup 6/ISCC.exe")
    }

    foreach ($Candidate in ($Candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }

    throw "Inno Setup compiler ISCC.exe was not found. Install Inno Setup 6 or pass -ISCCPath C:\Path\To\ISCC.exe."
}

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptRoot "..")
$Version = $Version.Trim()
if (-not $Version) {
    $Version = Get-ProjectVersion -ProjectRoot $ProjectRoot
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[a-zA-Z0-9.-]+)?$') {
    throw "Version must be a semantic version without a leading v: $Version"
}

Push-Location $ProjectRoot
try {
    if ($BuildNuitka) {
        $NuitkaArgs = @{
            Version = $Version
        }
        if ($SkipSync) {
            $NuitkaArgs["SkipSync"] = $true
        }
        if ($SkipChecks) {
            $NuitkaArgs["SkipChecks"] = $true
        }
        if ($SkipTests) {
            $NuitkaArgs["SkipTests"] = $true
        }
        & (Join-Path $ScriptRoot "build_nuitka.ps1") @NuitkaArgs
    }

    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
        throw "Standalone source directory not found: $SourceDir. Run ./scripts/build_nuitka.ps1 -Version $Version first, or pass -BuildNuitka."
    }

    $ResolvedSourceDir = (Resolve-Path -LiteralPath $SourceDir).Path
    $ExePath = Join-Path $ResolvedSourceDir "cua-win.exe"
    if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        throw "cua-win.exe not found in standalone source directory: $ResolvedSourceDir"
    }

    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $ResolvedOutDir = (Resolve-Path -LiteralPath $OutDir).Path
    $ISCC = Resolve-InnoCompiler -ExplicitPath $ISCCPath
    $SetupBaseName = "cua-win-$Version-win64-setup"
    $InstallerScript = Join-Path $ProjectRoot "installer/cua-win.iss"

    Invoke-Checked -Command $ISCC -Arguments @(
        "/Qp",
        "/DAppVersion=$Version",
        "/DSourceDir=$ResolvedSourceDir",
        "/DOutputDir=$ResolvedOutDir",
        "/DSetupBaseName=$SetupBaseName",
        $InstallerScript
    )

    $SetupExe = Join-Path $ResolvedOutDir "$SetupBaseName.exe"
    if (-not (Test-Path -LiteralPath $SetupExe -PathType Leaf)) {
        throw "Installer was not produced: $SetupExe"
    }

    $Hash = Get-FileHash -LiteralPath $SetupExe -Algorithm SHA256
    $HashLine = "$($Hash.Hash.ToLowerInvariant())  $([System.IO.Path]::GetFileName($SetupExe))"
    $HashLine | Set-Content -LiteralPath (Join-Path $ResolvedOutDir "SHA256SUMS.txt") -Encoding ascii

    Write-Host "Installer build ready:"
    Write-Host "  $SetupExe"
    Write-Host "  $(Join-Path $ResolvedOutDir 'SHA256SUMS.txt')"
    Write-Host "SHA256:"
    Write-Host "  $HashLine"
}
finally {
    Pop-Location
}
