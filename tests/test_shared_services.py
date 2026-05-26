from __future__ import annotations

from io import BytesIO

from PIL import Image

from computer_server.services.ocr_service import OCRService
from computer_server.services.uia_service import UIACallGate


def test_uia_call_gate_returns_result():
    gate = UIACallGate()

    assert gate.run_with_timeout(lambda: "ok", timeout=0.5) == "ok"


def test_uia_call_gate_returns_none_on_exception():
    gate = UIACallGate()

    def fail():
        raise RuntimeError("boom")

    assert gate.run_with_timeout(fail, timeout=0.5) is None


def test_ocr_service_detect_png_uses_existing_processor():
    class FakeOCR:
        def detect_text(self, img, confidence_threshold, timeout_seconds):
            return [{
                "content": f"{img.width}x{img.height}",
                "confidence": confidence_threshold,
                "timeout": timeout_seconds,
            }]

    img = Image.new("RGB", (3, 2), "white")
    buf = BytesIO()
    img.save(buf, format="PNG")

    service = OCRService()
    service._ocr = FakeOCR()

    assert service.detect_png(buf.getvalue(), timeout_seconds=7) == [{
        "content": "3x2",
        "confidence": 0.4,
        "timeout": 7,
    }]
