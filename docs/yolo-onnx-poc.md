# 视觉运行方案与 YOLO ONNX 验证

## 现行方案（2026-10-13）

当前候选版本为 `0.4.0`，本次 Florence 移除列在 `CHANGELOG.md` 的对应版本下。
下文早期 0.3.4 本地产物仅为历史验证记录，不代表 0.4.0 安装包验收或已发布。

主 LLM 结合完整窗口截图、UIA/MSAA 语义、OCR 文本和 YOLO 图标位置理解界面。
截图可以按 `max_image_dimension` 缩放，但观察保留整个目标窗口的上下文。
优先选择已有 `element_index`，携带最新 `observation_id` 操作；小目标仍不清楚时，
再用父 `screenshot_id` 调用 `computer_zoom`。放大后仍优先匹配已有节点；
仅当坐标来自放大图时使用 `zoom_id`。

YOLO 图标节点保留索引、边界框、中心、点击能力和检测置信度，`name` 为空。
检测置信度不是图标含义的置信度，文本查询也不会匹配未命名图标。歧义 OCR/图标
对保留双方定位，由主 LLM 看图判断。SOM 仅在语义树 poor/empty 或显式
`force_som=true` 时运行；模型推理在工作线程中执行，预热不消除首帧推理成本。

运行时只保留 YOLO ONNX 和 RapidOCR 两组模型，不加载 Florence、Transformers、
Tokenizers、PyTorch、Torchvision 或 Ultralytics，也不逐图标发起 LLM API 调用。
PyTorch 和 Ultralytics 仅用于开发者离线导出与基线对比。

## 当前模型与证据

- 模型：`som/weights/icon_detect/model.onnx`，81,208,227 bytes（77.4 MiB）。
- 当前 ONNX SHA-256：
  `9faf8c1720b508605f8f9bbd44bb8e9a4c9a615f2ea6cc6f63878062a78707f7`。
- 源权重来自本地旧 standalone 中的同一专用 OmniParser V2 模型；其 SHA-256 与
  下方历史来源记录及当前 `model_manifest.json` 一致。
- 导出工具版本、opset 17、动态形状和无内嵌 NMS 的图输入/输出约定与下方来源记录一致。
  重新导出产生了新的文件哈希，当前 manifest 已同步；图输出一致性以本次
  源权重/ONNX 实测对比为依据。
- 2026-10-13 对一张真实 Oryx 窗口截图进行独立进程对比：32 个原始检测框全部匹配，
  最小 IoU 0.9999941，最大置信度差 0.00000337，无新增框或漏框；检测顺序及
  OCR/图标合并 NMS 一致，两种后端各重复 5 次结果稳定。
- 源码视觉链路在同一截图上返回 8 个未命名可点击图标和 46 个 OCR 文本节点。
  该进程未加载上述已移除的模型/分词运行模块。
- 实际源 daemon 的 `/call` 观测及局部放大通过：返回 1280×757 的目标视图、
  326 个 UIA 节点、65 个 vision 节点，`computer_zoom` 返回有效图像和 `zoom_id`。
- 本次源码验证：466 项测试通过；源码 Ruff、compileall、skill 校验、
  源模型及 wheel 校验通过。standalone 和 installer 尚未针对本次修改重建。

本机源码视觉管线的独立测量（1624×1291 Oryx 截图）：

| 阶段 | 结果 |
| --- | ---: |
| YOLO/OCR 模型加载 | 1.251 s |
| 首次 parse + 节点转换 | 11.417 s |
| 后续 3 次管线调用中位数 | 4.039 s |
| 后续调用进程 RSS | 约 217 MiB |

上述管线测量不包含 UIA/MSAA、窗口捕获或 HTTP/CLI 开销。真实源 daemon 首次
窗口观测另测得 29.335 s，不能与管线耗时混为一谈。64 个图标节点的纯转换阶段
另测得 13.621 ms → 0.082 ms；旧路径的 caption 推理在该对比中关闭，差异主要来自
删除截图解码与裁剪，不能据此推算完整推理加速。所有数值均为本机样本，不是 SLO，
也没有完成英文、深色主题和混合 DPI 的广泛准确率验收。

本地证据保存在忽略的 `artifacts/` 中：

- `yolo-onnx-restored-20261013/export_manifest.json`、`parity.json`；
- `no-florence-runtime-smoke-20261013.py`；
- `no-florence-wheel-20261013/cua_pywin-0.3.4-py3-none-any.whl`
  （82,647,257 bytes，约 78.8 MiB；未包含已移除的 caption 资源）。
- 退役 Florence 源码、测试和研究文件保存在
  `retired-florence2-20261013/`，不参与生产导入、构建或测试。

这些本地文件不随 Git 自动交付；发布流程以 [RELEASE.md](../RELEASE.md) 为准。
本次文档整理没有重新构建 wheel，以上 wheel 验证对应此前实现完成后的构建。

## 复核与重新导出

验证现有模型和版本：

```powershell
uv run --no-sync cua-win doctor --no-desktop --no-daemon --no-dependencies --json
uv run --no-sync python scripts/validate_release.py --version 0.4.0
```

如需重新导出，用已经核对来源的本地源权重路径替换示例路径：

```powershell
uv sync --group export --group test --group dev
uv run --no-sync python scripts/export_icon_detector_onnx.py --model D:/models/omniparser-v2/model.pt --output-dir artifacts/yolo-export-candidate
```

导出结果是候选，不会自动替换包内模型。通过原权重/ONNX 框匹配、顺序、
置信度及重复稳定性对比后，再替换 `model.onnx` 并更新 manifest 的实际哈希。
发布前仍需完整源码、wheel、standalone 和 installer 检查。

---

## 历史：2026-09-15 的 0.3.2 YOLO ONNX PoC

以下保留当时的图身份、两张截图对比和性能数据。旧 SHA-256、文件大小、Florence
裁剪检查及发布结论不代表当前模型或当前交付状态。

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
