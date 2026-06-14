# Bundled RapidOCR PP-OCRv4 Models

This directory contains the Chinese PP-OCRv4 mobile detection and recognition
models plus the PP-OCR mobile angle classifier used by `cua-pywin`.

The files were copied without conversion from the `rapidocr==3.8.1` Python
distribution. RapidOCR's model registry identifies these binaries as the
RapidOCR 3.8.0 ONNX release.

Runtime code passes every model path explicitly to RapidOCR. This avoids
network access and prevents future RapidOCR package-layout or default-model
changes from silently changing the OCR pipeline.

Upstream project: https://github.com/RapidAI/RapidOCR

License: Apache-2.0. See `LICENSE`.
