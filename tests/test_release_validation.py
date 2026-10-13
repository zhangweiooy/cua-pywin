from __future__ import annotations

import hashlib
import json
import tomllib
import zipfile
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
    assert (
        "Remove-Item -LiteralPath $rapidOcrModels -Recurse -Force" in script
    )
    assert "New-Item -ItemType Directory -Path $rapidOcrModels" not in script
    assert '"opencv_videoio_ffmpeg*.dll"' in script
    assert '"testing",' not in script
    assert '"--include-package=unittest"' in script
    assert '"--include-module=cProfile"' in script
    assert '"--include-module=pydoc"' in script
    assert '"--include-package=pydoc_data"' in script
    assert '"--include-module=pstats"' in script
    assert '"--include-module=profile"' in script
    for package in ("onnx", "onnxslim", "torch", "torchvision", "transformers", "ultralytics"):
        assert f'"{package}",' in script
        assert f'"--include-package={package}"' not in script


def test_nuitka_bundles_clean_environment_comtypes_uia_bindings() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_nuitka.ps1"
    ).read_text(encoding="utf-8")

    assert "Initialize-ComtypesBindings" in script
    assert 'comtypes.client.GetModule(typelib)' in script
    assert '("UIAutomationCore.dll", "oleacc.dll")' in script
    assert "normalized_checks < 3" in script
    assert ".cua-build-backup" in script
    assert "Install-StandaloneComtypesCheckerForCompilation" in script
    assert "Restore-ComtypesBuildInputs" in script
    assert "Patch-StandaloneComtypesVersionCheck" in script
    assert "standalone_comtypes_tlib_version_checker.py" in script
    assert "validate standalone Teaching lifecycle" in script
    assert "validate_standalone_teaching.ps1" in script
    assert "validate standalone SOM runtime" in script
    assert "validate_standalone_som.ps1" in script
    teaching_smoke = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "validate_standalone_teaching.ps1"
    ).read_text(encoding="utf-8")
    assert '$env:APPDATA = $privateAppData' in teaching_smoke
    assert "generatedComtypesFiles.Count -ne 0" in teaching_smoke
    som_smoke = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "validate_standalone_som.ps1"
    ).read_text(encoding="utf-8")
    assert '$env:CUA_OCR_TIMEOUT_SECONDS = "60"' in som_smoke
    assert "without changing the runtime default" in som_smoke
    assert "force_som = $true" in som_smoke
    assert "oryx-window-0.19.1.png" in som_smoke
    assert "fixture-hwnd.txt" in som_smoke
    assert "targetable HWND" in som_smoke
    assert "icon_semantic_status" not in som_smoke
    assert "[int]$vision.num_text -lt 1" in som_smoke
    assert '"--include-package=comtypes.gen"' in script
    assert '"--include-module=comtypes.stream"' in script
    assert "Assert-NuitkaRequiredModules" in script
    for module_name in (
        "comtypes.stream",
        "comtypes.gen.UIAutomationClient",
        "comtypes.gen.Accessibility",
        "comtypes.gen.stdole",
        "comtypes.gen._944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0",
        "comtypes.gen._1EA4DBF0_3C3B_11CF_810C_00AA00389B71_0_1_1",
        "comtypes.gen._00020430_0000_0000_C000_000000000046_0_2_0",
        "pydoc",
        "pydoc_data",
        "pydoc_data.topics",
    ):
        assert module_name in script


def test_installer_build_requires_clean_environment_lifecycle() -> None:
    root = Path(__file__).resolve().parents[1]
    build_script = (root / "scripts" / "build_installer.ps1").read_text(
        encoding="utf-8"
    )
    installer_script = (root / "installer" / "cua-win.iss").read_text(
        encoding="utf-8"
    )
    validator = (root / "scripts" / "validate_installer.ps1").read_text(
        encoding="utf-8"
    )

    assert "validate installed package in a clean private environment" in build_script
    assert "validate_installer.ps1" in build_script
    assert "validate_standalone_teaching.ps1" in validator
    assert "validate_standalone_som.ps1" in validator
    assert "foreach ($pass in 1..2)" in validator
    assert "foreach ($capturePass in 1..3)" in validator
    assert '"computer_screenshot"' in validator
    assert '"computer_get_window_state"' in validator
    assert 'capture_backend -notin @("wgc", "screen_crop")' in validator
    assert 'uia_quality -notin @("rich", "ok", "poor")' in validator
    assert '"list-tools"' in validator
    assert "@($tools.tools).Count -ne 25" in validator
    assert "/MERGETASKS=!addtopath" in validator
    assert "Refusing to replace an existing cua-win installation" in validator
    assert "Uninstaller did not remove the installation directory" in validator
    assert "changed the current user's PATH" in validator
    assert "[InstallDelete]" in installer_script
    assert "[UninstallDelete]" in installer_script
    assert 'Type: dirifempty; Name: "{app}"' in installer_script
    assert 'Type: dirifempty; Name: "{app}\\som\\weights\\icon_detect"' in installer_script
    assert "RemoveDir(ExpandConstant('{app}'))" in installer_script
    assert 'Type: filesandordirs; Name: "{app}\\computer_server\\models\\omniparser_v2"' in installer_script
    assert "legacy Florence vision graph" in validator
    assert "Installer did not remove the legacy Florence model bundle" in validator
    assert 'Type: files; Name: "{app}\\som\\weights\\icon_detect\\model.pt"' in installer_script
    assert "legacy PyTorch YOLO weight" in validator
    assert "Installer did not remove the legacy PyTorch YOLO weight" in validator
    assert 'Type: filesandordirs; Name: "{app}\\easyocr"' in installer_script
    assert 'Type: filesandordirs; Name: "{app}\\easyocr-*.dist-info"' in installer_script
    assert "Installer did not remove the legacy EasyOCR runtime" in validator
    assert "FirstOutput: Boolean;" in installer_script
    assert "if not FirstOutput then" in installer_script
    assert "until not HasMore;" in installer_script


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


def test_nested_bundle_validates_source_and_wheel(monkeypatch, tmp_path):
    import zipfile
    from scripts.validate_release import validate_wheel
    directory = tmp_path / "models" / "int8"
    directory.mkdir(parents=True)
    content = b"quantized test graph"
    (directory / "model.onnx").write_bytes(content)
    manifest = {"sha256": {"int8/model.onnx": hashlib.sha256(content).hexdigest()}}
    (directory.parent / "model_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    bundle = ModelBundle("nested", Path("models"), PurePosixPath("models"), ("int8/model.onnx", "model_manifest.json"))
    monkeypatch.setattr(release_validation, "MODEL_BUNDLES", (bundle,))
    validate_source_models(tmp_path)
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("test.dist-info/METADATA", "Version: 0.3.0")
        archive.writestr("models/model_manifest.json", json.dumps(manifest))
        archive.writestr("models/int8/model.onnx", content)
    validate_wheel(wheel, "0.3.0", tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("computer_server/models/omniparser_v2/icon_caption_int8/model.int8.onnx", content)
    with pytest.raises(ReleaseValidationError, match="development-only"):
        validate_wheel(wheel, "0.3.0", tmp_path)


def test_validate_wheel_rejects_bundle_forbidden_file(monkeypatch, tmp_path):
    from scripts.validate_release import validate_wheel

    content = b"quantized test graph"
    model_dir = tmp_path / "models"
    (model_dir / "int8").mkdir(parents=True)
    (model_dir / "int8/model.onnx").write_bytes(content)
    manifest = {
        "sha256": {"int8/model.onnx": hashlib.sha256(content).hexdigest()}
    }
    (model_dir / "model_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    bundle = ModelBundle(
        "nested",
        Path("models"),
        PurePosixPath("models"),
        ("int8/model.onnx", "model_manifest.json"),
        ("onnx/vision_encoder.onnx",),
    )
    monkeypatch.setattr(release_validation, "MODEL_BUNDLES", (bundle,))
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("test.dist-info/METADATA", "Version: 0.3.1")
        archive.writestr("models/model_manifest.json", json.dumps(manifest))
        archive.writestr("models/int8/model.onnx", content)
        archive.writestr("models/onnx/vision_encoder.onnx", b"fp32")

    with pytest.raises(ReleaseValidationError, match="forbidden files"):
        validate_wheel(wheel, "0.3.1", tmp_path)


def test_removed_easyocr_is_absent_from_project_and_lock() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    declared = list(pyproject["project"].get("dependencies", ()))
    for dependencies in pyproject["project"].get(
        "optional-dependencies", {}
    ).values():
        declared.extend(dependencies)
    assert not any(
        dependency.lower().startswith("easyocr") for dependency in declared
    )

    with (root / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    assert "easyocr" not in {package["name"].lower() for package in lock["package"]}


def test_validate_wheel_rejects_removed_easyocr_dependency(monkeypatch, tmp_path):
    from scripts.validate_release import validate_wheel

    monkeypatch.setattr(release_validation, "MODEL_BUNDLES", ())
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "test.dist-info/METADATA",
            "Version: 0.3.1\nRequires-Dist: EasyOCR>=1.7\n",
        )

    with pytest.raises(ReleaseValidationError, match="removed runtime dependencies"):
        validate_wheel(wheel, "0.3.1", tmp_path)


def test_runtime_models_only_include_yolo_and_ocr():
    assert {bundle.name for bundle in release_validation.MODEL_BUNDLES} == {
        "OmniParser V2 YOLO", "RapidOCR PP-OCRv4",
    }
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    assert not any(
        dependency.startswith(("tokenizers", "transformers"))
        for dependency in project["project"]["dependencies"]
    )
    with (root / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    assert not {"tokenizers", "transformers"} & {
        package["name"] for package in lock["package"]
    }


@pytest.mark.parametrize(
    "member",
    [
        "computer_server/models/omniparser_v2/icon_caption_onnx_community/int8/vision_encoder.int8.onnx",
        "computer_server/models/omniparser_v2/icon_caption/model.safetensors",
    ],
)
def test_wheel_rejects_removed_caption_models(monkeypatch, tmp_path, member):
    monkeypatch.setattr(release_validation, "MODEL_BUNDLES", ())
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("test.dist-info/METADATA", "Version: 0.3.4")
        archive.writestr(member, b"obsolete caption model")
    with pytest.raises(ReleaseValidationError, match="development-only"):
        release_validation.validate_wheel(wheel, "0.3.4", tmp_path)


@pytest.mark.parametrize("dependency", ["tokenizers", "transformers"])
def test_wheel_rejects_removed_caption_dependency(monkeypatch, tmp_path, dependency):
    monkeypatch.setattr(release_validation, "MODEL_BUNDLES", ())
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "test.dist-info/METADATA",
            f"Version: 0.3.4\nRequires-Dist: {dependency}>=1\n",
        )
    with pytest.raises(ReleaseValidationError, match="removed runtime dependencies"):
        release_validation.validate_wheel(wheel, "0.3.4", tmp_path)
