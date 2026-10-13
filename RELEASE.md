# Release Guide

`cua-pywin` ships its OmniParser V2 YOLO and RapidOCR models in
the Windows installer and release wheel. Release distribution uses GitHub
Releases. The current runtime does not ship Florence or make per-icon LLM calls.

## Release Boundary

- A manual `Build and release` workflow run builds and validates a candidate.
  It uploads a one-day checksum candidate, but does not upload release binaries
  or create a GitHub Release.
- Pushing a new `vX.Y.Z` tag runs the same validation, builds the Windows
  installer, and creates the GitHub Release.
- The repository is currently private. Decide whether it should become public
  before announcing a user-facing release.
- Do not publish from a checkout containing unresolved Git LFS pointers.
- Do not move an existing release tag for documentation-only changes. Move a
  release tag only to correct a failed or incomplete release workflow.

## Prepare

1. Choose a new, unused release version and update `pyproject.toml`,
   `computer_server/__init__.py`, `som/__init__.py`, and `computer_server/main.py`.
   The existing `v0.3.4` tag is historical; do not move it for this change.
2. Move completed entries from `CHANGELOG.md` under the release version and
   date.
3. Run `uv lock` after dependency changes and commit `uv.lock`.
4. Run `git lfs pull`.
5. Build the candidate:

   ```powershell
   $releaseVersion = uv run --no-sync python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
   ./scripts/build_release.ps1 -Version $releaseVersion
   ```

6. Install `dist/*.whl` in a clean Windows Python 3.12 environment.
7. Run:

   ```powershell
   cua-win doctor
   cua-win list-tools
   cua-win start
   cua-win computer_get_screen_size
   cua-win stop
   ```

8. Confirm `cua-win doctor` reports both bundled model groups as valid:
   `model:omniparser_yolo` and `model:rapidocr_ppocrv4`. Force one SOM window
   observation and check that a full-window image, OCR text, and actionable
   unnamed YOLO nodes are returned; also verify a local zoom from its
   `screenshot_id`.

## Current local candidate (2026-10-13)

The current candidate version is `0.4.0`. Its changes are listed under that
version in `CHANGELOG.md`; the existing `v0.3.4` tag remains historical.
Local build artifacts are not a published release. Run all build and packaged
smoke gates below before creating a new release tag.

Local 0.4.0 validation completed on 2026-10-13:

- 479 tests passed; Ruff, compile checks, lockfile and source/wheel validation passed.
  Ruff excluded only the unrelated, untracked `outputs/` document artifacts.
- Wheel: `dist/0.4.0/cua_pywin-0.4.0-py3-none-any.whl`. A clean Python 3.12
  environment passed all 24 doctor checks and exposed 25 tools.
- Standalone: `dist-nuitka-0.4.0/cua-win.dist/`. Teaching lifecycle and real WGC
  fixture observations with YOLO/OCR passed.
- Installer: `dist-installer/0.4.0/cua-win-0.4.0-win64-setup.exe`. Private clean
  installation, legacy-model cleanup, doctor, tool calls, screenshots, window
  observation, repeated Teaching, YOLO/OCR, and uninstall cleanup passed.
- Each artifact directory contains `SHA256SUMS.txt`. No release tag was pushed
  and no GitHub Release was created by this local validation.

## Publish

1. Commit and push the release metadata.
2. After the version update and full build gates, create a new annotated tag:

   ```powershell
   $releaseVersion = uv run --no-sync python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
   $releaseTag = "v$releaseVersion"
   if (git tag --list $releaseTag) { throw "Choose a new release version; this tag already exists." }
   git tag -a $releaseTag -m "cua-pywin $releaseVersion"
   git push origin $releaseTag
   ```

3. Wait for the `Build and release` workflow to finish.
4. Download the installer, wheel, and checksum files from the GitHub Release.
5. Verify the installer checksum and run the post-publish smoke below on a
   second Windows machine before announcing the release.
6. Review generated release notes before announcing the release.

## Post-Publish Smoke

For the normal Windows installer path, set `$releaseVersion` to the newly
published version before running the examples:

```powershell
$setupPath = ".\cua-win-$releaseVersion-win64-setup.exe"
Get-FileHash -LiteralPath $setupPath -Algorithm SHA256
& $setupPath
cua-win doctor
cua-win list-tools
cua-win start
cua-win computer_get_screen_size
cua-win call computer_get_window '{"active":true}'
cua-win stop
```

For wheel validation in a clean Python 3.12 environment:

```powershell
pip install ".\cua_pywin-$releaseVersion-py3-none-any.whl"
cua-win doctor
cua-win list-tools
```

## Updating Bundled Models

Model updates are developer-owned release changes. Replace the bundled files,
update each `model_manifest.json` source revision and SHA-256 entries, run the
full release build, and verify that the wheel works without network model
downloads. Users should never need to fetch model files separately.

The production bundles are:

| Model group | Runtime path |
| --- | --- |
| OmniParser V2 YOLO | `som/weights/icon_detect/model.onnx` |
| RapidOCR PP-OCRv4 | `computer_server/models/rapidocr_ppocrv4/` |

YOLO export uses the pinned `export` dependency group. Verify the source weight
identity, exported graph contract and PyTorch/ONNX detector parity before
replacing the bundled ONNX file. Update its manifest to the new graph's actual
hash; an export's timestamp metadata can change file identity without changing
inference results. See [model provenance and verification](docs/yolo-onnx-poc.md).

## Wheel Contents

The wheel includes models but not Python itself or third-party dependency
wheels. Installing it can still download Python dependencies from the package
index. For normal Windows users, use the Nuitka + Inno Setup installer.

## Nuitka Standalone Build

For local executable testing, build a standalone directory:

```powershell
./scripts/build_nuitka.ps1
./scripts/build_nuitka.ps1 -Version 0.4.0
```

When `-Version` is omitted, the script reads the version from `pyproject.toml`.

For fast local iteration after the virtual environment is already synced:

```powershell
./scripts/build_nuitka.ps1 -SkipSync -SkipChecks
```

The output is `dist-nuitka/cua-win.dist/cua-win.exe` plus its dependency
DLLs, Python extension modules, bundled models, and runtime data files. This is
not cryptographically uncrackable; it removes plain source files from the main
application path and raises the reverse-engineering cost. The model files and
third-party libraries remain readable by the local process.

The hybrid build exports only locked production dependencies into an isolated
staging environment before copying runtime packages. Its pruning pass removes
development/test/example/documentation files, Polars, OpenCV's unused FFmpeg
backend, and RapidOCR's duplicate default model files. The empty
`rapidocr/models` directory must remain because RapidOCR validates that root
even when CUA passes explicit bundled model paths.

Before Nuitka compilation, the build generates UIAutomationCore and oleacc
bindings into the locked build environment. Nuitka must freeze
`comtypes.stream`, `comtypes.gen.UIAutomationClient`,
`comtypes.gen.Accessibility`, stdole, and their GUID wrapper modules. The build
validates these as module definitions in `nuitka-report.xml`; do not rely on a
developer or end-user comtypes cache. Generated wrappers must not retain
build-machine typelib mtimes because Windows servicing changes DLL timestamps
without changing the typelib contract. The standalone runtime overlay skips
only the machine-specific mtime comparison and keeps the comtypes
code-generator version check.

Every standalone build runs
`scripts/validate_standalone_teaching.ps1` against the produced executable.
The smoke uses private state, learning output, and APPDATA directories; it must
start Teaching, observe recording status, stop, observe idle status, terminate
the private daemon, remove its temporary directories, and avoid generating a
comtypes cache. Dependency-only `doctor` checks are not sufficient.

Do not remove Matplotlib, `unittest`, `pydoc`/`pydoc_data`,
`cProfile`/`profile`/`pstats`, SciPy, or SymPy from the standalone without a
fresh import report. PyTorch, Torchvision, and Ultralytics are export-only and
must be absent from production artifacts, as must Transformers, Tokenizers and
all retired icon-caption bundles. The release gate also forces a real YOLO/OCR observation from
the packaged executable; a degraded overall observation does not hide an OCR
timeout or detector/OCR initialization failure.

## Windows Installer Build

Install Inno Setup 6 so `ISCC.exe` is available on PATH or in the default
`Program Files` / per-user `%LocalAppData%\Programs` location. Then build the
installer from the standalone output:

```powershell
./scripts/build_nuitka.ps1 -Version 0.4.0
./scripts/build_installer.ps1 -Version 0.4.0
```

For a one-command local build:

```powershell
./scripts/build_installer.ps1 -Version 0.4.0 -BuildNuitka -SkipSync -SkipChecks
```

The output is:

```text
dist-installer/cua-win-0.4.0-win64-setup.exe
dist-installer/SHA256SUMS.txt
```

The installer is per-user by default and installs to
`%LocalAppData%\Programs\cua-win`, so it does not require administrator
privileges. It creates Start Menu shortcuts and offers an optional current-user
PATH entry. The full `cua-win.dist` directory is installed; do not build a
Nuitka onefile executable because the bundled model payload is too large and
would add slow extraction on startup.

Every installer build runs `scripts/validate_installer.ps1` before writing the
release checksum. The gate performs a silent installation into a private
temporary directory without changing PATH or Start Menu entries, verifies the
complete standalone payload and executable hash, runs full `doctor`, checks all
25 public tools, exercises repeated screenshot capture plus a real active-window
UIA/WGC observation through a private daemon, runs the empty-cache Teaching
lifecycle twice, uninstalls the package, and rejects process, registry, PATH,
installation-directory, or state-directory residue. It refuses to replace an
existing per-user cua-win installation.
