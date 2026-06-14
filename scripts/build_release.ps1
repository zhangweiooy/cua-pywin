param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+(?:[a-zA-Z0-9.-]+)?$')]
    [string]$Version,

    [string]$OutDir = "dist"
)

$ErrorActionPreference = "Stop"

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
    Invoke-Checked -Command "git" -Arguments @("lfs", "pull")
    Invoke-Checked -Command "uv" -Arguments @("sync", "--locked", "--all-groups")
    Invoke-Checked -Command "uv" -Arguments @(
        "run", "python", "scripts/validate_release.py", "--version", $Version
    )
    Invoke-Checked -Command "uv" -Arguments @("run", "ruff", "check", ".")
    Invoke-Checked -Command "uv" -Arguments @("run", "pytest", "-q")
    Invoke-Checked -Command "uv" -Arguments @(
        "run", "python", "-m", "compileall", "-q", "computer_server", "som"
    )

    if (Test-Path -LiteralPath $ResolvedOutDir) {
        Remove-Item -LiteralPath $ResolvedOutDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $ResolvedOutDir | Out-Null

    Invoke-Checked -Command "uv" -Arguments @(
        "build", "--wheel", "--out-dir", $ResolvedOutDir
    )
    $Wheels = @(Get-ChildItem -LiteralPath $ResolvedOutDir -Filter *.whl)
    if ($Wheels.Count -ne 1) {
        throw "Expected exactly one wheel in $ResolvedOutDir, found $($Wheels.Count)"
    }

    Invoke-Checked -Command "uv" -Arguments @(
        "run",
        "python",
        "scripts/validate_release.py",
        "--version",
        $Version,
        "--wheel",
        $Wheels[0].FullName
    )
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Wheels[0].FullName).Hash.ToLowerInvariant()
    $ChecksumPath = Join-Path $ResolvedOutDir "SHA256SUMS.txt"
    "$Hash  $($Wheels[0].Name)" | Set-Content -LiteralPath $ChecksumPath -Encoding ascii

    Write-Host "Release candidate ready:"
    Write-Host "  $($Wheels[0].FullName)"
    Write-Host "  $ChecksumPath"
}
finally {
    Pop-Location
}
