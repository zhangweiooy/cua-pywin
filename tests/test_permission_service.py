from __future__ import annotations

import pytest

from computer_server.services.permission_service import (
    PermissionDenied,
    RuntimePermissionPolicy,
    resolve_allowed_path,
)


def test_resolve_allowed_path_restricts_to_configured_roots(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setenv("CUA_FILE_ROOTS", str(allowed))

    assert resolve_allowed_path(str(allowed / "file.txt"), "read_text") == allowed / "file.txt"
    with pytest.raises(PermissionDenied):
        resolve_allowed_path(str(blocked / "file.txt"), "read_text")


def test_resolve_allowed_path_can_disable_dangerous_operations(tmp_path, monkeypatch):
    monkeypatch.setenv("CUA_DISABLE_DANGEROUS_FILE_TOOLS", "1")

    with pytest.raises(PermissionDenied):
        resolve_allowed_path(str(tmp_path / "file.txt"), "write_text", dangerous=True)


def test_runtime_policy_disables_run_command(monkeypatch):
    monkeypatch.setenv("CUA_DISABLE_RUN_COMMAND", "true")

    with pytest.raises(PermissionDenied):
        RuntimePermissionPolicy.from_env().require_run_command()


def test_runtime_policy_disables_local_open(tmp_path, monkeypatch):
    monkeypatch.setenv("CUA_DISABLE_LOCAL_OPEN", "true")

    with pytest.raises(PermissionDenied):
        RuntimePermissionPolicy.from_env().require_local_open(str(tmp_path / "file.txt"))


def test_runtime_policy_disables_cdp_javascript(monkeypatch):
    monkeypatch.setenv("CUA_DISABLE_CDP_JAVASCRIPT", "true")

    with pytest.raises(PermissionDenied):
        RuntimePermissionPolicy.from_env().require_cdp_javascript()
