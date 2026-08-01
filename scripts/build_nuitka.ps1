param(
    [string]$OutDir = "dist-nuitka",

    [string]$Version = "",

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
        [string]$Source,

        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    $excludeDirs = @(
        "computer_server",
        "som",
        "cua_pywin-*.dist-info",
        "playwright",
        "playwright-*.dist-info",
        "polars",
        "polars-*.dist-info",
        "_polars_runtime_*",
        "polars_runtime_*.dist-info",
        "test",
        "tests",
        "__pycache__"
    )
    $excludeArgs = @("/XD") + $excludeDirs + @("/XF", "*.pth", "*.pyc")
    Invoke-Robocopy -Source $Source -Destination $Destination -Arguments $excludeArgs
}

function New-ProductionRuntimeStaging {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null

    $requirements = Join-Path $Destination "runtime-requirements.txt"
    $venv = Join-Path $Destination "venv"
    $buildPython = (& uv run --no-sync python -c "import sys; print(sys.executable)")
    if ($LASTEXITCODE -ne 0 -or -not $buildPython) {
        throw "failed to locate the locked build Python"
    }
    $buildPython = [string]$buildPython

    Invoke-Checked -Command "uv" -Arguments @(
        "--quiet",
        "export",
        "--locked",
        "--no-dev",
        "--no-default-groups",
        "--no-emit-project",
        "--format",
        "requirements.txt",
        "--output-file",
        $requirements
    ) | Out-Host
    Invoke-Checked -Command "uv" -Arguments @(
        "--quiet",
        "venv",
        "--clear",
        "--python",
        $buildPython,
        $venv
    ) | Out-Host

    $stagePython = Join-Path $venv "Scripts\python.exe"
    Invoke-Checked -Command "uv" -Arguments @(
        "--quiet",
        "pip",
        "sync",
        "--python",
        $stagePython,
        "--strict",
        "--require-hashes",
        $requirements
    ) | Out-Host

    $sitePackages = (& $stagePython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
    if ($LASTEXITCODE -ne 0 -or -not $sitePackages) {
        throw "failed to locate production staging site-packages"
    }
    return [string]$sitePackages
}

function Initialize-ComtypesBindings {
    $code = @'
from __future__ import annotations

import importlib
import re
from pathlib import Path

import comtypes
import comtypes.client
import comtypes.gen

gen_dir = Path(comtypes.__file__).resolve().parent / "gen"
if not gen_dir.is_dir():
    raise RuntimeError(f"comtypes gen directory is unavailable: {gen_dir}")

# Force generation into the locked build environment. Never let a developer's
# APPDATA cache decide which bindings Nuitka sees.
comtypes.client.gen_dir = str(gen_dir)
comtypes.gen.__path__ = [str(gen_dir)]

for typelib in ("UIAutomationCore.dll", "oleacc.dll"):
    comtypes.client.GetModule(typelib)

required = (
    "comtypes.gen.UIAutomationClient",
    "comtypes.gen.Accessibility",
    "comtypes.gen.stdole",
)
for module_name in required:
    module = importlib.import_module(module_name)
    module_path = Path(module.__file__).resolve()
    if gen_dir not in module_path.parents:
        raise RuntimeError(
            f"{module_name} was loaded outside the build environment: {module_path}"
        )

# Typelib mtimes vary across Windows servicing states. Keep the comtypes code
# generator version check, but do not freeze build-machine mtimes into wrappers.
check_pattern = re.compile(
    r"_check_version\((['\"][^'\"]+['\"]),\s*[^)\r\n]+\)"
)
normalized_checks = 0
for wrapper_path in gen_dir.glob("_*.py"):
    source = wrapper_path.read_text(encoding="mbcs")
    updated, replacement_count = check_pattern.subn(
        r"_check_version(\1)",
        source,
    )
    if replacement_count:
        backup_path = wrapper_path.with_name(
            wrapper_path.name + ".cua-build-backup"
        )
        if backup_path.exists():
            raise RuntimeError(f"stale comtypes build backup: {backup_path}")
        backup_path.write_bytes(wrapper_path.read_bytes())
        wrapper_path.write_text(updated, encoding="mbcs")
        normalized_checks += replacement_count

if normalized_checks < 3:
    raise RuntimeError(
        "expected UIAutomation, Accessibility, and stdole typelib checks"
    )

print(
    f"prepared comtypes UIA bindings in {gen_dir}; "
    f"normalized {normalized_checks} machine-specific mtime checks"
)
'@
    Invoke-Checked -Command "uv" -Arguments @(
        "run",
        "--no-sync",
        "python",
        "-c",
        $code
    ) | Out-Host
}

function Get-BuildComtypesDirectory {
    $comtypesDir = (& uv run --no-sync python -c @'
from pathlib import Path

import comtypes

print(Path(comtypes.__file__).resolve().parent)
'@)
    if ($LASTEXITCODE -ne 0 -or -not $comtypesDir) {
        throw "failed to locate build comtypes package"
    }
    return [string]$comtypesDir
}

function Install-StandaloneComtypesCheckerForCompilation {
    $comtypesDir = Get-BuildComtypesDirectory
    $checker = Join-Path $comtypesDir "_tlib_version_checker.py"
    $backup = $checker + ".cua-build-backup"
    $overlay = Join-Path $ProjectRoot `
        "scripts\standalone_comtypes_tlib_version_checker.py"
    if (-not (Test-Path -LiteralPath $checker)) {
        throw "Build comtypes checker is unavailable: $checker"
    }
    if (Test-Path -LiteralPath $backup) {
        throw "Stale build comtypes checker backup: $backup"
    }

    Copy-Item -LiteralPath $checker -Destination $backup
    Copy-Item -LiteralPath $overlay -Destination $checker -Force
}

function Restore-ComtypesBuildInputs {
    $comtypesDir = Get-BuildComtypesDirectory
    $backups = @(
        Get-ChildItem `
            -LiteralPath $comtypesDir `
            -Recurse `
            -File `
            -Filter "*.cua-build-backup" `
            -ErrorAction SilentlyContinue
    )
    foreach ($backup in $backups) {
        $suffix = ".cua-build-backup"
        $original = $backup.FullName.Substring(
            0,
            $backup.FullName.Length - $suffix.Length
        )
        Move-Item `
            -LiteralPath $backup.FullName `
            -Destination $original `
            -Force
    }
}

function Patch-StandaloneComtypesVersionCheck {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SitePackages
    )

    $source = Join-Path $ProjectRoot `
        "scripts\standalone_comtypes_tlib_version_checker.py"
    $destination = Join-Path $SitePackages `
        "comtypes\_tlib_version_checker.py"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Standalone comtypes checker overlay is unavailable: $source"
    }
    if (-not (Test-Path -LiteralPath $destination)) {
        throw "Staged comtypes checker is unavailable: $destination"
    }

    Copy-Item -LiteralPath $source -Destination $destination -Force
    $checker = Get-Content -LiteralPath $destination -Raw
    if (
        -not $checker.Contains('raise ImportError("Wrong version")') -or
        $checker.Contains("Typelib different than module")
    ) {
        throw "Standalone comtypes checker overlay validation failed"
    }
}

function Assert-NuitkaRequiredModules {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ReportPath
    )

    $report = Get-Content -LiteralPath $ReportPath -Raw
    $requiredModules = @(
        "comtypes.stream",
        "comtypes.gen.UIAutomationClient",
        "comtypes.gen.Accessibility",
        "comtypes.gen.stdole",
        "comtypes.gen._944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0",
        "comtypes.gen._1EA4DBF0_3C3B_11CF_810C_00AA00389B71_0_1_1",
        "comtypes.gen._00020430_0000_0000_C000_000000000046_0_2_0",
        "pydoc",
        "pydoc_data",
        "pydoc_data.topics"
    )
    foreach ($moduleName in $requiredModules) {
        $needle = '<module name="' + $moduleName + '" '
        if (-not $report.Contains($needle)) {
            throw "Nuitka report is missing required module: $moduleName"
        }
    }
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

function Copy-StdlibFile {
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
        Copy-Item -LiteralPath $source -Destination (Join-Path $Destination $Name) -Force
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

# The hybrid standalone includes source-only packages. Keep the installed
# runtime immutable when those modules are imported for the first time.
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

_BASE = Path(__file__).resolve().parent
_DLL_DIRECTORY_HANDLES = []
for _relative in ("win32", "win32/lib", "Pythonwin"):
    _path = _BASE / _relative
    if _path.exists():
        _text = str(_path)
        if _text not in sys.path:
            sys.path.append(_text)

for _relative in (
    "pywin32_system32",
    "cv2",
    "numpy.libs",
    "scipy.libs",
    "torch/lib",
    "onnxruntime/capi",
):
    _path = _BASE / _relative
    if _path.exists():
        os.environ["PATH"] = str(_path) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(_path)))
            except OSError:
                pass
'@ | Set-Content -LiteralPath (Join-Path $Destination "sitecustomize.py") -Encoding utf8
}

function Remove-RuntimeBloat {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    Get-ChildItem -LiteralPath $Destination -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }

    $removeFilePatterns = @(
        "*.a",
        "*.c",
        "*.chm",
        "*.cpp",
        "*.cuh",
        "*.h",
        "*.hpp",
        "*.lib",
        "*.pdb",
        "*.pxd",
        "*.pyc",
        "*.pyo",
        "*.pyi",
        "*.pyx"
    )
    foreach ($pattern in $removeFilePatterns) {
        Get-ChildItem -LiteralPath $Destination -Recurse -File -Filter $pattern -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }

    $removeDirectoryNames = @(
        "doc",
        "docs",
        "example",
        "examples",
        "test",
        "tests"
    )
    Get-ChildItem -LiteralPath $Destination -Recurse -Directory -ErrorAction SilentlyContinue |
        Where-Object { $removeDirectoryNames -contains $_.Name.ToLowerInvariant() } |
        Sort-Object { $_.FullName.Length } -Descending |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }

    $removeDirectories = @()
    foreach ($path in $removeDirectories) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }

    $rapidOcrModels = Join-Path $Destination "rapidocr\models"
    if (Test-Path -LiteralPath $rapidOcrModels) {
        Remove-Item -LiteralPath $rapidOcrModels -Recurse -Force
    }

    $removeFiles = @()
    foreach ($name in $removeFiles) {
        $path = Join-Path $Destination $name
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }

    Get-ChildItem -LiteralPath $Destination -Recurse -File `
        -Filter "opencv_videoio_ffmpeg*.dll" -ErrorAction SilentlyContinue |
        Remove-Item -Force
}

function Get-ProjectVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot
    )

    $pyproject = Join-Path $ProjectRoot "pyproject.toml"
    foreach ($line in Get-Content -LiteralPath $pyproject -Encoding utf8) {
        if ($line -match '^\s*version\s*=\s*"([^"]+)"\s*$') {
            return $Matches[1]
        }
    }
    throw "project version not found in $pyproject"
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Version = $Version.Trim()
if (-not $Version) {
    $Version = Get-ProjectVersion -ProjectRoot $ProjectRoot
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[a-zA-Z0-9.-]+)?$') {
    throw "Version must be a semantic version without a leading v: $Version"
}
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
                $UvRun + @("python", "scripts/validate_release.py", "--version", $Version)
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

    Invoke-TimedStep "prepare comtypes UIA bindings" {
        Initialize-ComtypesBindings
        Install-StandaloneComtypesCheckerForCompilation
    }

    $NofollowRuntimeImports = @(
        "PIL",
        "aiohttp",
        "anyio",
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
        "--include-package=comtypes.gen",
        "--include-package=ctypes",
        "--include-package=email",
        "--include-package=html",
        "--include-package=http",
        "--include-package=logging",
        "--include-package=pydoc_data",
        "--include-package=unittest",
        "--include-package=xml",
        "--include-module=cProfile",
        "--include-module=comtypes.stream",
        "--include-module=pydoc",
        "--include-module=pstats",
        "--include-module=profile",
        "--include-module=plistlib",
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
    Assert-NuitkaRequiredModules -ReportPath (Join-Path $ResolvedOutDir "nuitka-report.xml")

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
        $RuntimeStageRoot = Join-Path $ResolvedOutDir ".runtime-stage"
        Invoke-TimedStep "stage and copy locked production runtime" {
            try {
                $RuntimeSitePackages = New-ProductionRuntimeStaging `
                    -Destination $RuntimeStageRoot
                Patch-StandaloneComtypesVersionCheck `
                    -SitePackages $RuntimeSitePackages
                Copy-RuntimeSitePackages `
                    -Source $RuntimeSitePackages `
                    -Destination $DistDir
            }
            finally {
                if (Test-Path -LiteralPath $RuntimeStageRoot) {
                    Remove-Item -LiteralPath $RuntimeStageRoot -Recurse -Force
                }
            }
        }
        Invoke-TimedStep "copy stdlib runtime data" {
            Copy-StdlibPackage -Name "ctypes" -Destination $DistDir
            Copy-StdlibPackage -Name "email" -Destination $DistDir
            Copy-StdlibPackage -Name "html" -Destination $DistDir
            Copy-StdlibPackage -Name "http" -Destination $DistDir
            Copy-StdlibPackage -Name "logging" -Destination $DistDir
            Copy-StdlibPackage -Name "pydoc_data" -Destination $DistDir
            Copy-StdlibPackage -Name "unittest" -Destination $DistDir
            Copy-StdlibPackage -Name "xml" -Destination $DistDir
            Copy-StdlibPackage -Name "zoneinfo" -Destination $DistDir
            Copy-StdlibFile -Name "cProfile.py" -Destination $DistDir
            Copy-StdlibFile -Name "plistlib.py" -Destination $DistDir
            Copy-StdlibFile -Name "pydoc.py" -Destination $DistDir
            Copy-StdlibFile -Name "profile.py" -Destination $DistDir
            Copy-StdlibFile -Name "pstats.py" -Destination $DistDir
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
    Invoke-TimedStep "prune runtime bloat" {
        Remove-RuntimeBloat -Destination $DistDir
    }
    Invoke-TimedStep "validate standalone Teaching lifecycle" {
        & (Join-Path $ProjectRoot "scripts\validate_standalone_teaching.ps1") `
            -ExePath $ExePath
    }
    Invoke-TimedStep "validate standalone SOM runtime" {
        & (Join-Path $ProjectRoot "scripts\validate_standalone_som.ps1") `
            -ExePath $ExePath
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
    Restore-ComtypesBuildInputs
    Pop-Location
}
