from __future__ import annotations

import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath

import pytest

import scripts.validate_release as release_validation
from scripts.validate_release import (
    LFS_POINTER_PREFIX,
    ModelBundle,
    ReleaseValidationError,
    _validate_hash_entries,
    collect_versions,
    validate_source_models,
    validate_versions,
)


def _write_version_fixture(root: Path, version: str) -> None:
    (root / "computer_server").mkdir()
    (root / "som").mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "cua-pywin"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "computer_server/__init__.py").write_text(
        f'__version__: str = "{version}"\n',
        encoding="utf-8",
    )
    (root / "som/__init__.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (root / "computer_server/main.py").write_text(
        f'app = FastAPI(\n    title="Computer API",\n    version="{version}",\n)\n',
        encoding="utf-8",
    )


def test_validate_versions_accepts_consistent_release(tmp_path: Path) -> None:
    _write_version_fixture(tmp_path, "1.2.3")

    assert set(collect_versions(tmp_path).values()) == {"1.2.3"}
    validate_versions("1.2.3", tmp_path)


def test_validate_versions_rejects_mismatch(tmp_path: Path) -> None:
    _write_version_fixture(tmp_path, "1.2.3")
    (tmp_path / "som/__init__.py").write_text(
        '__version__ = "1.2.4"\n',
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match="som/__init__.py=1.2.4"):
        validate_versions("1.2.3", tmp_path)


def test_validate_hash_entries_requires_all_runtime_files(tmp_path: Path) -> None:
    bundle = ModelBundle(
        name="test bundle",
        source_dir=tmp_path,
        wheel_dir=Path("models"),
        required_files=("model.bin", "model_manifest.json"),
    )

    with pytest.raises(ReleaseValidationError, match="model.bin"):
        _validate_hash_entries(
            bundle,
            {"model.bin": hashlib.sha256(b"model").hexdigest()},
            ["model_manifest.json"],
        )


def test_nuitka_sitecustomize_disables_bytecode_writes() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_nuitka.ps1"
    ).read_text(encoding="utf-8")

    assert 'os.environ["PYTHONDONTWRITEBYTECODE"] = "1"' in script
    assert "sys.dont_write_bytecode = True" in script


def test_nuitka_uses_a_pruned_production_runtime_staging() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_nuitka.ps1"
    ).read_text(encoding="utf-8")

    assert '"--no-default-groups"' in script
    assert '"--no-emit-project"' in script
    assert "New-ProductionRuntimeStaging" in script
    assert '"rapidocr\\models"' in script
    assert "New-Item -ItemType Directory -Path $rapidOcrModels" in script
    assert '"opencv_videoio_ffmpeg*.dll"' in script
    assert '"testing",' not in script
    assert '"--include-package=unittest"' in script
    assert '"--include-module=cProfile"' in script
    assert '"--include-module=pydoc"' not in script
    assert '"--include-module=pstats"' in script
    assert '"--include-module=profile"' in script


def test_nuitka_bundles_clean_environment_comtypes_uia_bindings() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_nuitka.ps1"
    ).read_text(encoding="utf-8")

    assert "Initialize-ComtypesBindings" in script
    assert 'comtypes.client.GetModule(typelib)' in script
    assert '("UIAutomationCore.dll", "oleacc.dll")' in script
    assert '"--include-package=comtypes.gen"' in script
    assert '"--include-module=comtypes.stream"' in script
    assert "Assert-NuitkaComtypesModules" in script
    for module_name in (
        "comtypes.stream",
        "comtypes.gen.UIAutomationClient",
        "comtypes.gen.Accessibility",
        "comtypes.gen.stdole",
        "comtypes.gen._944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0",
        "comtypes.gen._1EA4DBF0_3C3B_11CF_810C_00AA00389B71_0_1_1",
        "comtypes.gen._00020430_0000_0000_C000_000000000046_0_2_0",
    ):
        assert module_name in script


def test_validate_source_models_rejects_lfs_pointer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    pointer = model_dir / "model.bin"
    pointer.write_bytes(LFS_POINTER_PREFIX + b"\noid sha256:abc\n")
    manifest = {
        "sha256": {"model.bin": hashlib.sha256(pointer.read_bytes()).hexdigest()}
    }
    manifest_path = model_dir / "model_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    bundle = ModelBundle(
        name="test bundle",
        source_dir=Path("models"),
        wheel_dir=PurePosixPath("models"),
        required_files=("model.bin", "model_manifest.json"),
    )
    monkeypatch.setattr(release_validation, "MODEL_BUNDLES", (bundle,))

    with pytest.raises(ReleaseValidationError, match="Git LFS pointer"):
        validate_source_models(tmp_path)
