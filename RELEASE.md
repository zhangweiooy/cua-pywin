# Release Guide

`cua-pywin` ships its OmniParser V2 YOLO, Florence-2, and RapidOCR models
inside the wheel. The resulting artifact is approximately 1 GB and is
distributed through GitHub Releases rather than PyPI.

## Release Boundary

- A manual `Build and release` workflow run builds and validates a candidate.
  It does not create a GitHub Release.
- Pushing a tag such as `v0.1.0` runs the same validation and creates the
  GitHub Release.
- The repository is currently private. Decide whether it should become public
  before announcing a user-facing release.
- Do not publish from a checkout containing unresolved Git LFS pointers.

## Prepare

1. Update the version in `pyproject.toml`, `computer_server/__init__.py`,
   `som/__init__.py`, and `computer_server/main.py`.
2. Move completed entries from `CHANGELOG.md` under the release version and
   date.
3. Run `uv lock` after dependency changes and commit `uv.lock`.
4. Run `git lfs pull`.
5. Build the candidate:

   ```powershell
   ./scripts/build_release.ps1 -Version 0.1.0
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
   git tag -a v0.1.0 -m "cua-pywin 0.1.0"
   git push origin v0.1.0
   ```

3. Wait for the `Build and release` workflow to finish.
4. Download the wheel and `SHA256SUMS.txt` from the GitHub Release and verify
   the checksum on another Windows machine.
5. Review generated release notes before announcing the release.

## Updating Bundled Models

Model updates are developer-owned release changes. Replace the bundled files,
update each `model_manifest.json` source revision and SHA-256 entries, run the
full release build, and verify that the wheel works without network model
downloads. Users should never need to fetch model files separately.

## Current Packaging Limit

The wheel includes models but not Python itself or third-party dependency
wheels. Installing it can still download Python dependencies from the package
index. A fully offline, one-click distribution would require a separate
installer or wheelhouse and is not part of the current release format.

## Nuitka Standalone Build

For local executable testing, build a standalone directory:

```powershell
./scripts/build_nuitka.ps1
./scripts/build_nuitka.ps1 -Version 0.1.0
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
