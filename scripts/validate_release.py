from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
FORBIDDEN_RUNTIME_DISTRIBUTIONS = frozenset(
    {"easyocr", "torch", "torchvision", "ultralytics", "ultralytics-thop", "tokenizers", "transformers"}
)


class ReleaseValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelBundle:
    name: str
    source_dir: Path
    wheel_dir: PurePosixPath
    required_files: tuple[str, ...]
    forbidden_files: tuple[str, ...] = ()


MODEL_BUNDLES = (
    ModelBundle(
        name="OmniParser V2 YOLO",
        source_dir=Path("som/weights/icon_detect"),
        wheel_dir=PurePosixPath("som/weights/icon_detect"),
        required_files=("model.onnx", "model_manifest.json"),
        forbidden_files=("model.pt",),
    ),
    ModelBundle(
        name="RapidOCR PP-OCRv4",
        source_dir=Path("computer_server/models/rapidocr_ppocrv4"),
        wheel_dir=PurePosixPath("computer_server/models/rapidocr_ppocrv4"),
        required_files=(
            "ch_PP-OCRv4_det_mobile.onnx",
            "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            "ch_PP-OCRv4_rec_mobile.onnx",
            "ppocr_keys_v1.txt",
            "model_manifest.json",
        ),
    ),
)


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _read_version_assignment(path: Path) -> str:
    match = re.search(
        r'^__version__(?:\s*:\s*[^=]+)?\s*=\s*["\']([^"\']+)["\']',
        path.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match is None:
        raise ReleaseValidationError(f"version assignment not found: {path}")
    return match.group(1)


def _read_fastapi_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"app\s*=\s*FastAPI\((?:(?!\n\)).)*?\bversion\s*=\s*[\"']([^\"']+)[\"']",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        raise ReleaseValidationError(f"FastAPI version not found: {path}")
    return match.group(1)


def collect_versions(root: Path = PROJECT_ROOT) -> dict[str, str]:
    with (root / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    try:
        project_version = pyproject["project"]["version"]
    except (KeyError, TypeError) as exc:
        raise ReleaseValidationError("project.version is missing") from exc
    if not isinstance(project_version, str):
        raise ReleaseValidationError("project.version must be a string")
    return {
        "pyproject.toml": project_version,
        "computer_server/__init__.py": _read_version_assignment(
            root / "computer_server/__init__.py"
        ),
        "som/__init__.py": _read_version_assignment(root / "som/__init__.py"),
        "computer_server/main.py": _read_fastapi_version(
            root / "computer_server/main.py"
        ),
    }


def validate_versions(expected_version: str, root: Path = PROJECT_ROOT) -> None:
    versions = collect_versions(root)
    mismatches = {
        location: version
        for location, version in versions.items()
        if version != expected_version
    }
    if mismatches:
        details = ", ".join(
            f"{location}={version}" for location, version in mismatches.items()
        )
        raise ReleaseValidationError(
            f"version mismatch; expected {expected_version}: {details}"
        )


def _load_source_manifest(bundle: ModelBundle, root: Path) -> dict:
    manifest_path = root / bundle.source_dir / "model_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        hashes = manifest["sha256"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(
            f"{bundle.name} manifest is invalid: {manifest_path}"
        ) from exc
    if not isinstance(hashes, dict) or not hashes:
        raise ReleaseValidationError(
            f"{bundle.name} manifest has no SHA-256 entries"
        )
    return manifest


def _validate_hash_entries(
    bundle: ModelBundle,
    expected_hashes: dict,
    available_files: Iterable[str],
) -> None:
    available = set(available_files)
    forbidden = set(bundle.forbidden_files) & available
    if forbidden:
        raise ReleaseValidationError(
            f"{bundle.name} contains forbidden files: "
            f"{', '.join(sorted(forbidden))}"
        )
    missing = set(bundle.required_files) - available
    if missing:
        raise ReleaseValidationError(
            f"{bundle.name} is missing required files: {', '.join(sorted(missing))}"
        )
    unhashed = (set(bundle.required_files) - {"model_manifest.json"}) - set(
        expected_hashes
    )
    if unhashed:
        raise ReleaseValidationError(
            f"{bundle.name} manifest is missing hashes for: "
            f"{', '.join(sorted(unhashed))}"
        )
    for name, expected in expected_hashes.items():
        if (
            not isinstance(name, str)
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or "\\" in name
            or ":" in name
            or not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)
        ):
            raise ReleaseValidationError(
                f"{bundle.name} has an invalid SHA-256 entry: {name!r}"
            )


def validate_source_models(root: Path = PROJECT_ROOT) -> None:
    for bundle in MODEL_BUNDLES:
        bundle_dir = root / bundle.source_dir
        if not bundle_dir.is_dir():
            raise ReleaseValidationError(
                f"{bundle.name} source directory is missing: {bundle_dir}"
            )
        manifest = _load_source_manifest(bundle, root)
        expected_hashes = manifest["sha256"]
        available_files = [
            path.relative_to(bundle_dir).as_posix()
            for path in bundle_dir.rglob("*")
            if path.is_file()
        ]
        _validate_hash_entries(bundle, expected_hashes, available_files)
        for name, expected in expected_hashes.items():
            path = bundle_dir / name
            if not path.is_file():
                raise ReleaseValidationError(
                    f"{bundle.name} manifest references missing file: {path}"
                )
            with path.open("rb") as handle:
                prefix = handle.read(len(LFS_POINTER_PREFIX))
                if prefix.startswith(LFS_POINTER_PREFIX):
                    raise ReleaseValidationError(
                        f"{bundle.name} contains a Git LFS pointer: {path}"
                    )
                handle.seek(0)
                actual = _sha256_stream(handle)
            if actual.lower() != expected.lower():
                raise ReleaseValidationError(
                    f"{bundle.name} checksum mismatch for {name}: "
                    f"expected {expected}, got {actual}"
                )


def _wheel_metadata_text(archive: zipfile.ZipFile) -> str:
    metadata_names = [
        name
        for name in archive.namelist()
        if name.endswith(".dist-info/METADATA")
    ]
    if len(metadata_names) != 1:
        raise ReleaseValidationError(
            f"wheel must contain exactly one METADATA file, found {len(metadata_names)}"
        )
    return archive.read(metadata_names[0]).decode("utf-8")


def _wheel_metadata_version(metadata_text: str) -> str:
    match = re.search(r"^Version:\s*(.+)$", metadata_text, flags=re.MULTILINE)
    if match is None:
        raise ReleaseValidationError("wheel METADATA has no Version field")
    return match.group(1).strip()


def _validate_wheel_runtime_dependencies(metadata_text: str) -> None:
    dependencies = {
        match.group(1).lower().replace("_", "-")
        for match in re.finditer(
            r"^Requires-Dist:\s*([A-Za-z0-9_.-]+)",
            metadata_text,
            flags=re.MULTILINE,
        )
    }
    forbidden = dependencies & FORBIDDEN_RUNTIME_DISTRIBUTIONS
    if forbidden:
        raise ReleaseValidationError(
            "wheel declares removed runtime dependencies: "
            + ", ".join(sorted(forbidden))
        )


def validate_wheel(
    wheel_path: Path,
    expected_version: str,
    root: Path = PROJECT_ROOT,
) -> None:
    if not wheel_path.is_file():
        raise ReleaseValidationError(f"wheel not found: {wheel_path}")
    with zipfile.ZipFile(wheel_path) as archive:
        metadata_text = _wheel_metadata_text(archive)
        wheel_version = _wheel_metadata_version(metadata_text)
        if wheel_version != expected_version:
            raise ReleaseValidationError(
                f"wheel version mismatch: expected {expected_version}, got {wheel_version}"
            )
        _validate_wheel_runtime_dependencies(metadata_text)
        names = set(archive.namelist())
        forbidden = (
            "computer_server/models/omniparser_v2/",
            "computer_server/models/quasivision_icon_classifier/",
        )
        if any(name.startswith(forbidden) for name in names):
            raise ReleaseValidationError("wheel includes development-only icon models")
        for bundle in MODEL_BUNDLES:
            source_manifest = _load_source_manifest(bundle, root)
            manifest_name = str(bundle.wheel_dir / "model_manifest.json")
            if manifest_name not in names:
                raise ReleaseValidationError(
                    f"{bundle.name} manifest is missing from wheel"
                )
            wheel_manifest = json.loads(archive.read(manifest_name))
            if wheel_manifest != source_manifest:
                raise ReleaseValidationError(
                    f"{bundle.name} wheel manifest differs from source"
                )
            expected_hashes = source_manifest["sha256"]
            available_files = []
            for name in names:
                member_path = PurePosixPath(name)
                try:
                    relative = member_path.relative_to(bundle.wheel_dir)
                except ValueError:
                    continue
                if relative.parts:
                    available_files.append(relative.as_posix())
            _validate_hash_entries(bundle, expected_hashes, available_files)
            for name, expected in expected_hashes.items():
                member = str(bundle.wheel_dir / name)
                if member not in names:
                    raise ReleaseValidationError(
                        f"{bundle.name} wheel file is missing: {member}"
                    )
                with archive.open(member) as handle:
                    prefix = handle.read(len(LFS_POINTER_PREFIX))
                    if prefix.startswith(LFS_POINTER_PREFIX):
                        raise ReleaseValidationError(
                            f"{bundle.name} wheel contains a Git LFS pointer: {member}"
                        )
                with archive.open(member) as handle:
                    actual = _sha256_stream(handle)
                if actual.lower() != expected.lower():
                    raise ReleaseValidationError(
                        f"{bundle.name} wheel checksum mismatch for {name}: "
                        f"expected {expected}, got {actual}"
                    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate cua-pywin source and release wheel integrity."
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Expected release version without a leading v.",
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        help="Optional built wheel to validate in addition to the source tree.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_versions(args.version)
        validate_source_models()
        if args.wheel is not None:
            validate_wheel(args.wheel.resolve(), args.version)
    except ReleaseValidationError as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"release validation passed for cua-pywin {args.version}")
    if args.wheel is not None:
        print(f"validated wheel: {args.wheel.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
