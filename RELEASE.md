# Release Guide

`cua-pywin` ships its OmniParser V2 YOLO, Florence-2, and RapidOCR models in
the Windows installer and release wheel. The resulting artifacts are large and
are distributed through GitHub Releases rather than PyPI.

## Release Boundary

- A manual `Build and release` workflow run builds and validates a candidate.
  It uploads a one-day checksum candidate, but does not upload release binaries
  or create a GitHub Release.
- Pushing a tag such as `v0.3.4` runs the same validation, builds the Windows
  installer, and creates the GitHub Release.
- The repository is currently private. Decide whether it should become public
  before announcing a user-facing release.
- Do not publish from a checkout containing unresolved Git LFS pointers.
- Do not move an existing release tag for documentation-only changes. Move a
  release tag only to correct a failed or incomplete release workflow.

## Prepare

1. Update the version in `pyproject.toml`, `computer_server/__init__.py`,
   `som/__init__.py`, and `computer_server/main.py`.
2. Move completed entries from `CHANGELOG.md` under the release version and
   date.
3. Run `uv lock` after dependency changes and commit `uv.lock`.
4. Run `git lfs pull`.
5. Build the candidate:

   ```powershell
   ./scripts/build_release.ps1 -Version 0.3.4
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

8. Confirm `cua-win doctor` reports all three bundled model groups as valid.

## Publish

1. Commit and push the release metadata.
2. Create an annotated tag:

   ```powershell
   git tag -a v0.3.4 -m "cua-pywin 0.3.4"
   git push origin v0.3.4
   ```

3. Wait for the `Build and release` workflow to finish.
4. Download the installer, wheel, and checksum files from the GitHub Release.
5. Verify the installer checksum and run the post-publish smoke below on a
   second Windows machine before announcing the release.
6. Review generated release notes before announcing the release.

## Post-Publish Smoke

For the normal Windows installer path:

```powershell
Get-FileHash .\cua-win-0.3.4-win64-setup.exe -Algorithm SHA256
.\cua-win-0.3.4-win64-setup.exe
cua-win doctor
cua-win list-tools
cua-win start
cua-win computer_get_screen_size
cua-win call computer_get_window '{"active":true}'
cua-win stop
```

For wheel validation in a clean Python 3.12 environment:

```powershell
pip install .\cua_pywin-0.3.4-py3-none-any.whl
cua-win doctor
cua-win list-tools
```

## Updating Bundled Models

Model updates are developer-owned release changes. Replace the bundled files,
update each `model_manifest.json` source revision and SHA-256 entries, run the
full release build, and verify that the wheel works without network model
downloads. Users should never need to fetch model files separately.

## Wheel Packaging Limit

The wheel includes models but not Python itself or third-party dependency
wheels. Installing it can still download Python dependencies from the package
index. For normal Windows users, use the Nuitka + Inno Setup installer.

## Nuitka Standalone Build

For local executable testing, build a standalone directory:

```powershell
./scripts/build_nuitka.ps1
./scripts/build_nuitka.ps1 -Version 0.3.4
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
must be absent from production artifacts. The release gate also forces a real SOM/OCR/Florence-2 observation from
the packaged executable; a degraded overall observation does not hide an OCR
timeout or icon-caption initialization failure.

## Windows Installer Build

Install Inno Setup 6 so `ISCC.exe` is available on PATH or in the default
`Program Files` / per-user `%LocalAppData%\Programs` location. Then build the
installer from the standalone output:

```powershell
./scripts/build_nuitka.ps1 -Version 0.3.4
./scripts/build_installer.ps1 -Version 0.3.4
```

For a one-command local build:

```powershell
./scripts/build_installer.ps1 -Version 0.3.4 -BuildNuitka -SkipSync -SkipChecks
```

The output is:

```text
dist-installer/cua-win-0.3.4-win64-setup.exe
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
