# YOLO ONNX runtime PoC

Date: 2026-09-15. Target iteration: 0.3.2.

## Contract

- Input: full Windows desktop application screenshots, not icon crops.
- Model: an opset-17 export of the 0.3.1 OmniParser
  `som/weights/icon_detect/model.pt`.
- Runtime: ONNX Runtime CPU Execution Provider with four intra-op threads and
  one inter-op thread; no Ultralytics wrapper in production.
- Output: the existing normalized `xyxy` icon detection dictionaries.
- Distribution: export may use PyTorch; a future accepted runtime must contain
  no PyTorch, TorchVision, Ultralytics, or `.pt` model.

## Reuse decision

| Candidate | Evidence | Decision |
| --- | --- | --- |
| Ultralytics exporter and LetterBox/source post-processing | Official docs/source plus local export and pixel parity execution | Adopt for build-time export and use as parity reference only. |
| Ultralytics YOLOv8 ONNX Runtime example | Official executable example inspected | Adapt its raw-output boundary to the existing normalized bbox contract. |
| ONNX Runtime Python API | Already shipped and executed with CPU EP | Reuse as the only candidate inference runtime. |
| `ultralytics-inference` Rust crate | Official repository/docs inspected | Reject: adds a Rust runtime and packaging boundary that the existing Python service does not need. |
| OpenCV DNN NMS/inference | Official Ultralytics example inspected | Reject: inference would add a second runtime; NumPy NMS is small and has stricter stable-order semantics. |

Search coverage included the official Ultralytics export/ONNX integration,
LetterBox, detection head, scaling/NMS source, official Python ONNX example,
Microsoft ONNX Runtime examples, and the official Rust inference alternative.
Package-registry metadata was not needed beyond the locally installed pinned
versions because the selected boundary uses no new production package.

## Export provenance

- Source model SHA-256:
  `dab3d4351ad00b035db829909a4db98354d5a90f6990e4ac00222a9a95d4bf57`
- Source model: single-class YOLO11m, labels `{0: "icon"}`, strides 8/16/32.
- Export: Ultralytics 8.4.53, Torch 2.12.0 CPU, ONNX 1.22.0,
  onnxslim 0.1.96, opset 17, dynamic shape, simplify enabled, no embedded NMS.
- ONNX input: `images`, float, `[batch, 3, height, width]`.
- ONNX output: `output0`, float, `[batch, 5, anchors]`; raw `xywh` plus one
  icon score.
- Selected ONNX SHA-256:
  `67bb5832112502a17a56a6326d024ba6e0b5a287348285afeaf9073aa3a37e3e`
- Selected ONNX size: 80,526,191 bytes.

The graph metadata identifies Ultralytics and carries an AGPL-3.0 notice. The
production manifest now records that license and preserves the export metadata.

## Executed comparison

The two backends ran in separate processes. The corpus currently contains one
real Oryx and one real WeChat light-theme Chinese screenshot at native scale.

| Metric | Result |
| --- | ---: |
| PyTorch boxes | 87 |
| ONNX boxes | 87 |
| Box match rate | 100% |
| Minimum matched IoU | 0.9999936 |
| Maximum confidence error | 0.00000337 |
| New false positives / misses | 0 / 0 |
| NMS order equal | Yes |
| Icon/OCR merge NMS positions equal | Yes, including protected collisions |
| Identical Florence input crops | 87 / 87 |
| High-frequency recall, baseline / ONNX | 87.5% / 87.5% |
| 100 repeated calls stable | Both yes |

Resource and latency measurements from the isolated detector workers:

| Metric | PyTorch, 4 threads | ONNX CPU, 4 threads |
| --- | ---: | ---: |
| Load time | 1482.9 ms | 219.6 ms |
| First full-screen detection | 963.6 ms | 581.4 ms |
| Full-screen detection p50 | 860.6 ms | 552.2 ms |
| Peak RSS | 1070.7 MiB | 960.5 MiB |
| Peak Private Bytes | 1964.8 MiB | 1966.3 MiB |

## Decision

The raw export, LetterBox, decoding, both NMS stages, box order and crop
equivalence pass on the available screenshots. With equal four-thread settings,
ONNX loads about 85% faster, first inference is about 40% faster, stable p50 is
about 36% faster, and peak RSS is about 110 MiB lower. Peak Private Bytes is
effectively unchanged. The candidate therefore passes the measured quality and
performance gate for the available corpus and is the only production detector
in 0.3.2. PyTorch remains build-time only.

The required English, dark-theme and real 100/125/150/200 percent DPI screenshot
matrix is still incomplete and must not be claimed as validated.
