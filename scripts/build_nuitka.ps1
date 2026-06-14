param(
    [string]$OutDir = "dist-nuitka",

    [ValidateSet("hybrid", "full")]
    [string]$Profile = "hybrid",

    [int]$Jobs = [Math]::Max(1, [Environment]::ProcessorCount - 2),

    [int]$CopyJobs = [Math]::Min(32, [Math]::Max(1, [Environment]::ProcessorCount)),

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

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE"
    }
}

function Invoke-TimedStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Script
    )

    Write-Host "==> $Name"
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        & $Script
    }
    finally {
        $timer.Stop()
        Write-Host ("<== {0}: {1:n1}s" -f $Name, $timer.Elapsed.TotalSeconds)
    }
}

function Invoke-Robocopy {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,

        [Parameter(Mandatory = $true)]
        [string]$Destination,

        [string[]]$Arguments = @()
    )

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $robocopyArgs = @(
        $Source,
        $Destination,
        "/E",
        "/MT:$CopyJobs",
        "/R:1",
        "/W:1",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP"
    ) + $Arguments
    & robocopy @robocopyArgs | Out-Host
    $exitCode = $LASTEXITCODE
    if ($exitCode -gt 7) {
        throw "robocopy failed with exit code $exitCode"
    }
    $global:LASTEXITCODE = 0
}

function Copy-CleanDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,

        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Invoke-Robocopy -Source $Source -Destination $Destination
}

function Copy-RuntimeSitePackages {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    $sitePackages = (& uv run python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
    if ($LASTEXITCODE -ne 0) {
        throw "failed to locate site-packages"
    }
    $sitePackages = [string]$sitePackages
    $excludeDirs = @(
        "computer_server",
        "som",
        "cua_pywin-*.dist-info",
        "nuitka",
        "Nuitka-*.dist-info",
        "ordered_set",
        "ordered_set-*.dist-info",
        "zstandard",
        "zstandard-*.dist-info",
        "pytest",
        "pytest-*.dist-info",
        "ruff",
        "ruff-*.dist-info",
        "black",
        "black-*.dist-info",
        "isort",
        "isort-*.dist-info",
        "mypy",
        "mypy-*.dist-info",
        "test",
        "tests",
        "__pycache__"
    )
    $excludeArgs = @("/XD") + $excludeDirs + @("/XF", "*.pth", "*.pyc")
    Invoke-Robocopy -Source $sitePackages -Destination $Destination -Arguments $excludeArgs
}

function Copy-StdlibPackage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    $stdlib = (& uv run python -c "import sysconfig; print(sysconfig.get_paths()['stdlib'])")
    if ($LASTEXITCODE -ne 0) {
        throw "failed to locate stdlib"
    }
    $source = Join-Path ([string]$stdlib) $Name
    if (Test-Path -LiteralPath $source) {
        Invoke-Robocopy -Source $source -Destination (Join-Path $Destination $Name)
    }
}

function Write-RuntimeBootstrap {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    @'
from __future__ import annotations

import os
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
for _relative in ("win32", "win32/lib", "Pythonwin"):
    _path = _BASE / _relative
    if _path.exists():
        _text = str(_path)
        if _text not in sys.path:
            sys.path.append(_text)

_pywin32_system32 = _BASE / "pywin32_system32"
if _pywin32_system32.exists():
    os.environ["PATH"] = str(_pywin32_system32) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(_pywin32_system32))
        except OSError:
            pass
'@ | Set-Content -LiteralPath (Join-Path $Destination "sitecustomize.py") -Encoding utf8
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ResolvedOutDir = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $OutDir))
$ProjectRootPrefix = $ProjectRoot.TrimEnd('\') + '\'
if (
    $ResolvedOutDir.Equals($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    -not $ResolvedOutDir.StartsWith($ProjectRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
) {
    throw "OutDir must stay inside the project root: $ResolvedOutDir"
}

Push-Location $ProjectRoot
try {
    $totalTimer = [System.Diagnostics.Stopwatch]::StartNew()
    $UvRun = @("run", "--no-sync")

    if (-not $SkipSync) {
        Invoke-TimedStep "uv sync" {
            Invoke-Checked -Command "uv" -Arguments @("sync", "--locked", "--all-groups")
        }
    }
    if (-not $SkipChecks) {
        Invoke-TimedStep "release validation" {
            Invoke-Checked -Command "uv" -Arguments (
                $UvRun + @("python", "scripts/validate_release.py", "--version", "0.1.0")
            )
        }
        Invoke-TimedStep "ruff" {
            Invoke-Checked -Command "uv" -Arguments ($UvRun + @("ruff", "check", "."))
        }
        if (-not $SkipTests) {
            Invoke-TimedStep "pytest" {
                Invoke-Checked -Command "uv" -Arguments ($UvRun + @("pytest", "-q"))
            }
        }
    }

    if (Test-Path -LiteralPath $ResolvedOutDir) {
        Remove-Item -LiteralPath $ResolvedOutDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $ResolvedOutDir | Out-Null

    $NofollowRuntimeImports = @(
        "PIL",
        "aiohttp",
        "anyio",
        "cv2",
        "fastapi",
        "fastmcp",
        "mcp",
        "matplotlib",
        "mss",
        "numpy",
        "onnxruntime",
        "playwright",
        "psutil",
        "pydantic",
        "pydantic_core",
        "pydantic_settings",
        "pynput",
        "pyperclip",
        "pywinctl",
        "rapidocr",
        "requests",
        "rich",
        "safetensors",
        "scipy",
        "screeninfo",
        "starlette",
        "timm",
        "tokenizers",
        "torch",
        "torchvision",
        "transformers",
        "uiautomation",
        "ultralytics",
        "uvicorn",
        "websockets",
        "win32api",
        "win32con",
        "win32gui",
        "win32process",
        "win32ui",
        "winrt"
    )

    $NuitkaArgs = $UvRun + @(
        "python",
        "-m",
        "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--deployment",
        "--python-flag=no_asserts",
        "--jobs=$Jobs",
        "--lto=no",
        "--remove-output",
        "--report=$ResolvedOutDir\nuitka-report.xml",
        "--output-dir=$ResolvedOutDir",
        "--output-filename=cua-win.exe",
        "--include-package=computer_server",
        "--include-package=som",
        "--include-package=email",
        "--include-package=html",
        "--include-package=http",
        "--include-package=logging",
        "--include-package=xml",
        "--include-module=zoneinfo",
        "--include-module=_zoneinfo",
        "--nofollow-import-to=*.tests",
        "--nofollow-import-to=matplotlib.tests",
        "--nofollow-import-to=numpy.tests",
        "--nofollow-import-to=som.tests",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=torch.testing",
        "scripts/nuitka_entry.py"
    )
    if ($Profile -eq "hybrid") {
        foreach ($name in $NofollowRuntimeImports) {
            $NuitkaArgs = $NuitkaArgs[0..($NuitkaArgs.Count - 2)] + @(
                "--nofollow-import-to=$name"
            ) + $NuitkaArgs[($NuitkaArgs.Count - 1)]
        }
    }
    else {
        $NuitkaArgs = $NuitkaArgs[0..($NuitkaArgs.Count - 2)] + @(
            "--include-package=fastmcp",
            "--include-package=mcp",
            "--include-package=uvicorn",
            "--include-package=starlette",
            "--include-package=rapidocr",
            "--include-package=onnxruntime",
            "--include-package=transformers",
            "--include-package=tokenizers",
            "--include-package=safetensors",
            "--include-package=timm",
            "--include-package=torch",
            "--include-package=torchvision",
            "--include-package=ultralytics",
            "--include-package=winrt",
            "--include-module=pythoncom",
            "--include-module=pywintypes",
            "--include-module=win32api",
            "--include-module=win32con",
            "--include-module=win32gui",
            "--include-module=win32process",
            "--include-module=win32ui"
        ) + $NuitkaArgs[($NuitkaArgs.Count - 1)]
    }
    Invoke-TimedStep "nuitka compile ($Profile)" {
        Invoke-Checked -Command "uv" -Arguments $NuitkaArgs
    }

    $DistDirs = @(Get-ChildItem -LiteralPath $ResolvedOutDir -Directory -Filter "*.dist")
    if ($DistDirs.Count -ne 1) {
        throw "Expected exactly one Nuitka .dist directory, found $($DistDirs.Count)"
    }
    $DistDir = $DistDirs[0].FullName
    $TargetDistDir = Join-Path $ResolvedOutDir "cua-win.dist"
    if (-not $DistDir.Equals($TargetDistDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $TargetDistDir) {
            Remove-Item -LiteralPath $TargetDistDir -Recurse -Force
        }
        Move-Item -LiteralPath $DistDir -Destination $TargetDistDir
        $DistDir = $TargetDistDir
    }
    $ExePath = Join-Path $DistDir "cua-win.exe"
    if (-not (Test-Path -LiteralPath $ExePath)) {
        throw "Nuitka executable was not produced: $ExePath"
    }

    if ($Profile -eq "hybrid") {
        Invoke-TimedStep "copy runtime site-packages" {
            Copy-RuntimeSitePackages -Destination $DistDir
        }
        Invoke-TimedStep "copy stdlib runtime data" {
            Copy-StdlibPackage -Name "email" -Destination $DistDir
            Copy-StdlibPackage -Name "html" -Destination $DistDir
            Copy-StdlibPackage -Name "http" -Destination $DistDir
            Copy-StdlibPackage -Name "logging" -Destination $DistDir
            Copy-StdlibPackage -Name "xml" -Destination $DistDir
            Copy-StdlibPackage -Name "zoneinfo" -Destination $DistDir
            Write-RuntimeBootstrap -Destination $DistDir
        }
    }
    Invoke-TimedStep "copy bundled models" {
        Copy-CleanDirectory `
            -Source (Join-Path $ProjectRoot "computer_server\models") `
            -Destination (Join-Path $DistDir "computer_server\models")
        Copy-CleanDirectory `
            -Source (Join-Path $ProjectRoot "som\weights") `
            -Destination (Join-Path $DistDir "som\weights")
        Copy-Item -LiteralPath (Join-Path $ProjectRoot "LICENSE") -Destination $DistDir -Force
        Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination $DistDir -Force
    }

    Invoke-TimedStep "hash output" {
        $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ExePath).Hash.ToLowerInvariant()
        "$Hash  cua-win.exe" | Set-Content -LiteralPath (Join-Path $DistDir "SHA256SUMS.txt") -Encoding ascii
    }
    $totalTimer.Stop()

    Write-Host "Nuitka standalone build ready:"
    Write-Host "  $ExePath"
    Write-Host "  $(Join-Path $DistDir 'SHA256SUMS.txt')"
    Write-Host ("Total build time: {0:n1}s" -f $totalTimer.Elapsed.TotalSeconds)
}
finally {
    Pop-Location
}
