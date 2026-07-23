from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from comtypes.tools.codegenerator import version as generator_version


def _standalone_checker():
    checker_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "standalone_comtypes_tlib_version_checker.py"
    )
    return runpy.run_path(str(checker_path))["_check_version"]


def test_standalone_checker_ignores_machine_specific_typelib_mtime() -> None:
    check_version = _standalone_checker()

    check_version(generator_version, 0)
    check_version(generator_version, 9999999999.0)


def test_standalone_checker_preserves_generator_version_validation() -> None:
    check_version = _standalone_checker()

    with pytest.raises(ImportError, match="Wrong version"):
        check_version("incompatible-generator", 0)
