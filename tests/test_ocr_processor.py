from __future__ import annotations

import builtins
import sys
import types

import numpy as np
import pytest
from PIL import Image

from som.ocr import DEFAULT_OCR_TIMEOUT_SECONDS, OCRProcessor


def _reset_ocr_processor():
    OCRProcessor._shared_reader = None
    OCRProcessor._shared_key = None


def test_default_ocr_timeout_allows_cold_cpu_initialization():
    assert DEFAULT_OCR_TIMEOUT_SECONDS == 20.0


def test_rapidocr_object_result_is_normalized(monkeypatch):
    _reset_ocr_processor()

    class FakeOutput:
        boxes = np.array(
            [
                [[10, 20], [50, 20], [50, 40], [10, 40]],
                [[0, 0], [10, 0], [10, 10], [0, 10]],
            ],
            dtype=float,
        )
        txts = ("微信", "low")
        scores = (0.91, 0.1)

    class FakeRapidOCR:
        def __call__(self, image_np, text_score=None):
            assert image_np.shape == (100, 200, 3)
            assert text_score == 0.5
            return FakeOutput()

    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        types.SimpleNamespace(RapidOCR=lambda **_kwargs: FakeRapidOCR()),
    )
    monkeypatch.setenv("CUA_OCR_BACKEND", "rapidocr")

    result = OCRProcessor().detect_text(Image.new("RGB", (200, 100), "white"))

    assert result == [
        {
            "type": "text",
            "bbox": [0.05, 0.2, 0.25, 0.4],
            "content": "微信",
            "confidence": 0.91,
            "interactivity": False,
        }
    ]


def test_rapidocr_list_result_is_supported(monkeypatch):
    _reset_ocr_processor()

    class FakeRapidOCR:
        def __call__(self, _image_np, text_score=None):
            return [
                ([[20, 10], [80, 10], [80, 30], [20, 30]], "搜索", 0.88),
            ]

    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        types.SimpleNamespace(RapidOCR=lambda **_kwargs: FakeRapidOCR()),
    )
    monkeypatch.setenv("CUA_OCR_BACKEND", "rapidocr")

    result = OCRProcessor().detect_text(Image.new("RGB", (200, 100), "white"))

    assert result[0]["content"] == "搜索"
    assert result[0]["bbox"] == [0.1, 0.1, 0.4, 0.3]
    assert result[0]["confidence"] == 0.88


def test_rapidocr_default_denoise_keeps_single_character_detections(monkeypatch):
    _reset_ocr_processor()

    class FakeRapidOCR:
        def __call__(self, _image_np, text_score=None):
            return [
                ([[1, 1], [6, 1], [6, 6], [1, 6]], "#", 0.99),
                ([[10, 10], [14, 10], [14, 14], [10, 14]], "O", 0.99),
                ([[15, 10], [22, 10], [22, 18], [15, 18]], "口", 0.99),
                ([[20, 10], [80, 10], [80, 30], [20, 30]], "搜索", 0.88),
                ([[90, 10], [120, 10], [120, 30], [90, 30]], "OK", 0.9),
            ]

    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        types.SimpleNamespace(RapidOCR=lambda **_kwargs: FakeRapidOCR()),
    )
    monkeypatch.setenv("CUA_OCR_BACKEND", "rapidocr")

    result = OCRProcessor().detect_text(Image.new("RGB", (200, 100), "white"))

    assert [item["content"] for item in result] == ["#", "O", "口", "搜索", "OK"]


def test_rapidocr_strict_denoise_filters_isolated_artifacts(monkeypatch):
    _reset_ocr_processor()

    class FakeRapidOCR:
        def __call__(self, _image_np, text_score=None):
            return [
                ([[1, 1], [6, 1], [6, 6], [1, 6]], "#", 0.99),
                ([[10, 10], [14, 10], [14, 14], [10, 14]], "O", 0.99),
                ([[15, 10], [22, 10], [22, 18], [15, 18]], "口", 0.99),
                ([[20, 10], [80, 10], [80, 30], [20, 30]], "搜索", 0.88),
                ([[90, 10], [120, 10], [120, 30], [90, 30]], "OK", 0.9),
            ]

    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        types.SimpleNamespace(RapidOCR=lambda **_kwargs: FakeRapidOCR()),
    )
    monkeypatch.setenv("CUA_OCR_BACKEND", "rapidocr")
    monkeypatch.setenv("CUA_OCR_DENOISE", "strict")

    result = OCRProcessor().detect_text(Image.new("RGB", (200, 100), "white"))

    assert [item["content"] for item in result] == ["搜索", "OK"]


def test_rapidocr_normalizes_search_icon_artifact(monkeypatch):
    _reset_ocr_processor()

    class FakeRapidOCR:
        def __call__(self, _image_np, text_score=None):
            return [
                ([[20, 10], [80, 10], [80, 30], [20, 30]], "Q搜索", 0.88),
            ]

    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        types.SimpleNamespace(RapidOCR=lambda **_kwargs: FakeRapidOCR()),
    )
    monkeypatch.setenv("CUA_OCR_BACKEND", "rapidocr")

    result = OCRProcessor().detect_text(Image.new("RGB", (200, 100), "white"))

    assert result[0]["content"] == "搜索"


def test_ocr_denoise_can_be_disabled(monkeypatch):
    _reset_ocr_processor()

    class FakeRapidOCR:
        def __call__(self, _image_np, text_score=None):
            return [
                ([[1, 1], [6, 1], [6, 6], [1, 6]], "#", 0.99),
            ]

    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        types.SimpleNamespace(RapidOCR=lambda **_kwargs: FakeRapidOCR()),
    )
    monkeypatch.setenv("CUA_OCR_BACKEND", "rapidocr")
    monkeypatch.setenv("CUA_OCR_DENOISE", "0")

    result = OCRProcessor().detect_text(Image.new("RGB", (200, 100), "white"))

    assert result[0]["content"] == "#"


def test_rapidocr_import_failure_returns_empty(monkeypatch):
    _reset_ocr_processor()
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "rapidocr":
            raise ImportError("missing rapidocr")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "rapidocr", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setenv("CUA_OCR_BACKEND", "rapidocr")

    assert OCRProcessor().detect_text(Image.new("RGB", (10, 10), "white")) == []


def test_rapidocr_uses_explicit_bundled_model_paths(monkeypatch, tmp_path):
    _reset_ocr_processor()
    expected_names = {
        "Det.model_path": "ch_PP-OCRv4_det_mobile.onnx",
        "Cls.model_path": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        "Rec.model_path": "ch_PP-OCRv4_rec_mobile.onnx",
        "Rec.rec_keys_path": "ppocr_keys_v1.txt",
    }
    for name in expected_names.values():
        (tmp_path / name).write_bytes(b"model")

    captured = {}

    class FakeRapidOCR:
        pass

    def create_rapidocr(*, params):
        captured.update(params)
        return FakeRapidOCR()

    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        types.SimpleNamespace(RapidOCR=create_rapidocr),
    )
    monkeypatch.setenv("CUA_RAPIDOCR_MODEL_DIR", str(tmp_path))

    reader = OCRProcessor()._create_rapidocr_reader()

    assert isinstance(reader, FakeRapidOCR)
    for key, name in expected_names.items():
        assert captured[key] == str(tmp_path / name)
    assert captured["Global.model_root_dir"] == str(tmp_path)
    assert captured["Global.log_level"] == "error"


def test_rapidocr_missing_bundled_model_fails_closed(monkeypatch, tmp_path):
    constructor_called = False

    def create_rapidocr(*, params):
        nonlocal constructor_called
        constructor_called = True
        raise AssertionError(f"RapidOCR constructor must not be called: {params}")

    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        types.SimpleNamespace(RapidOCR=create_rapidocr),
    )
    monkeypatch.setenv("CUA_RAPIDOCR_MODEL_DIR", str(tmp_path))

    with pytest.raises(RuntimeError, match="Bundled RapidOCR model files are missing"):
        OCRProcessor()._create_rapidocr_reader()

    assert constructor_called is False


def test_easyocr_developer_fallback_disables_download(monkeypatch):
    captured = {}

    class FakeReader:
        def __init__(self, languages, **kwargs):
            captured["languages"] = languages
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "easyocr",
        types.SimpleNamespace(Reader=FakeReader),
    )

    reader = OCRProcessor(backend="easyocr")._create_easyocr_reader()

    assert isinstance(reader, FakeReader)
    assert captured["download_enabled"] is False
