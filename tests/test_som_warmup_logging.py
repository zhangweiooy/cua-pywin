from __future__ import annotations

import logging
import re
from types import SimpleNamespace

from computer_server.handlers import som_bridge
from computer_server.services import icon_caption_service


class _ImmediateThread:
    def __init__(self, *, target, name, daemon):
        self._target = target
        self.name = name
        self.daemon = daemon

    def start(self):
        self._target()


def _messages(caplog):
    return [record.getMessage() for record in caplog.records]


def _fake_parser(tmp_path, *, ocr_loader=lambda: None):
    return SimpleNamespace(
        detector=SimpleNamespace(load_model=lambda: None),
        ocr=SimpleNamespace(
            backend="rapidocr",
            _rapidocr_model_dir=lambda: tmp_path,
            _ensure_reader=ocr_loader,
        ),
    )


def _prepare_immediate_warmup(monkeypatch, parser):
    monkeypatch.setattr(
        som_bridge,
        "threading",
        SimpleNamespace(Thread=_ImmediateThread),
    )
    monkeypatch.setattr(som_bridge, "is_available", lambda: True)
    monkeypatch.setattr(som_bridge, "_get_parser", lambda: parser)
    monkeypatch.setattr(som_bridge, "_parser_state", "idle")
    monkeypatch.setattr(som_bridge, "_parser_failure", None)


def test_warmup_logs_correlated_stage_timings_without_model_path(
    monkeypatch,
    tmp_path,
    caplog,
):
    parser = _fake_parser(tmp_path)
    _prepare_immediate_warmup(monkeypatch, parser)
    monkeypatch.setattr(icon_caption_service, "warm_up", lambda: True)
    caplog.set_level(logging.INFO, logger=som_bridge.__name__)

    som_bridge.warm_up()

    messages = _messages(caplog)
    warmup_ids = {
        match.group(1)
        for message in messages
        if (match := re.search(r"warmup_id=([^ ]+)", message))
    }
    assert len(warmup_ids) == 1
    assert any("event=som_warmup_start" in message for message in messages)
    for stage in ("parser", "yolo", "rapidocr", "florence2"):
        assert any(
            "event=som_warmup_stage" in message
            and f"stage={stage}" in message
            and "duration_ms=" in message
            and "status=ready" in message
            for message in messages
        )
    assert any(
        "event=som_warmup_complete" in message
        and "total_duration_ms=" in message
        and "status=ready" in message
        for message in messages
    )
    assert any(
        "stage=rapidocr" in message
        and "model_root_source=bundled" in message
        and "model_root_exists=true" in message
        for message in messages
    )
    assert str(tmp_path) not in "\n".join(messages)


def test_warmup_logs_rapidocr_root_failure_without_exception_text(
    monkeypatch,
    tmp_path,
    caplog,
):
    secret = "C:/private/runtime/rapidocr/models"

    def fail_ocr():
        try:
            raise FileNotFoundError(f"model_root_dir {secret} does not exist")
        except FileNotFoundError as exc:
            raise RuntimeError("RapidOCR initialization failed") from exc

    parser = _fake_parser(tmp_path, ocr_loader=fail_ocr)
    _prepare_immediate_warmup(monkeypatch, parser)
    caplog.set_level(logging.INFO, logger=som_bridge.__name__)

    som_bridge.warm_up()

    messages = _messages(caplog)
    failure_logs = [
        message
        for message in messages
        if "stage=rapidocr" in message and "status=failed" in message
    ]
    assert len(failure_logs) == 1
    assert "error_type=RuntimeError" in failure_logs[0]
    assert "root_error_type=FileNotFoundError" in failure_logs[0]
    assert "error_code=rapidocr_model_root_missing" in failure_logs[0]
    assert secret not in "\n".join(messages)
    assert som_bridge.parser_status()["state"] == "failed"


def test_skipped_warmup_keeps_last_completed_correlation_id(monkeypatch, caplog):
    monkeypatch.setattr(
        som_bridge,
        "threading",
        SimpleNamespace(Thread=_ImmediateThread),
    )
    monkeypatch.setattr(som_bridge, "is_available", lambda: True)
    monkeypatch.setattr(som_bridge, "_parser_state", "ready")
    monkeypatch.setattr(som_bridge, "_latest_warmup_id", "completed-warmup")
    caplog.set_level(logging.INFO, logger=som_bridge.__name__)

    som_bridge.warm_up()

    assert som_bridge._latest_warmup_id == "completed-warmup"
    assert any(
        "event=som_warmup_complete" in message
        and "status=skipped" in message
        and "parser_state=ready" in message
        for message in _messages(caplog)
    )


def test_detect_sync_logs_wait_parse_convert_and_total(monkeypatch, caplog):
    parse_result = SimpleNamespace(elements=[])
    parser = SimpleNamespace(parse=lambda *args, **kwargs: parse_result)
    converted = som_bridge.VisionResult(elements=[], metadata={})
    monkeypatch.setattr(som_bridge, "is_available", lambda: True)
    monkeypatch.setattr(som_bridge, "_wait_if_loading", lambda timeout: True)
    monkeypatch.setattr(som_bridge, "_get_parser", lambda: parser)
    monkeypatch.setattr(som_bridge, "_parser_state", "ready")
    monkeypatch.setattr(som_bridge, "_latest_warmup_id", "warmup-test-id")
    monkeypatch.setattr(
        som_bridge,
        "_convert_parse_result",
        lambda *args, **kwargs: converted,
    )
    caplog.set_level(logging.INFO, logger=som_bridge.__name__)

    result = som_bridge.detect_sync(b"private-image-bytes", 20, 20)

    assert result is converted
    assert result.metadata == {}
    messages = _messages(caplog)
    detect_ids = {
        match.group(1)
        for message in messages
        if (match := re.search(r"detect_id=([^ ]+)", message))
    }
    assert len(detect_ids) == 1
    assert any(
        "event=som_detect_sync_start" in message
        and "warmup_id=warmup-test-id" in message
        for message in messages
    )
    assert any(
        "event=som_detect_sync_wait" in message
        and "duration_ms=" in message
        and "status=ready" in message
        for message in messages
    )
    for stage in ("parser", "parse", "convert"):
        assert any(
            "event=som_detect_sync_stage" in message
            and f"stage={stage}" in message
            and "duration_ms=" in message
            for message in messages
        )
    assert any(
        "event=som_detect_sync_complete" in message
        and "total_duration_ms=" in message
        and "status=ready" in message
        for message in messages
    )
    assert "private-image-bytes" not in "\n".join(messages)
