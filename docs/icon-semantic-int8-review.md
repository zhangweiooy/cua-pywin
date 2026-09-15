# Icon semantic INT8 iteration review

The sole production backend is `florence2_int8_community`. The original
PyTorch Florence-2 bundle, quasivision PoC, and old manual one-file INT8 PoC
were removed after selection. Exploratory reports and source-graph provenance
remain ignored development artifacts and are never loaded by the runtime.

## Review findings and corrections

| Priority | Finding | Correction |
| --- | --- | --- |
| P1 | Finished batch rows could emit tokens after EOS; the last generation step performed an unused decoder call. | Freeze completed rows, bound decoder calls, test mixed-length batches. |
| P1 | Greedy code ignored forced BOS/EOS and no-repeat-ngram settings. | Apply local generation settings, compare logits rules with Transformers. |
| P1 | `only_onnxruntime=True` skipped Transformer-specific Python fusion despite earlier descriptions. | Rerun with `False`, record actual fusion counts, regenerate controlled model hashes. |
| P1 | Standalone copied the entire models directory, including old PoC weights; wheel configuration alone did not prove exclusion. | Copy explicit bundles, explicitly exclude PoC paths and reject them during wheel validation. |
| P2 | Processor comparison used three solid colors and a single word; decode punctuation cleanup differed. | Add full-byte-range pixels, deterministic noise, Chinese/special-token/punctuation decoding and official cleanup behavior. |
| P2 | Status shared the inference lock; disabled status remained idle and old inference errors survived success. | Separate inference gating from state, expose disabled state and clear recovered errors. |
| P2 | Doctor could accept a manifest omitting a required weight hash. | Require hash coverage, validate nested source/wheel files and reject LFS pointers. |

## Model provenance

Source: `onnx-community/OmniParser-v2.0_icon_caption`, revision
`0d28990c05ddeb50792d02b92f7e6e4d0f4bd086`, the community conversion of
`microsoft/OmniParser-v2.0/icon_caption`.

The Transformer quantization implementation uses ORT's `quant_pre_process`,
`optimize_model(model_type="bart", only_onnxruntime=False)` and dynamic
per-channel QInt8 for constant-weight MatMul/Gemm. The vision graph now uses
the official `quant_pre_process` followed by `quantize_dynamic` with the same
QInt8/per-channel/constant-B settings; the embedding graph remains FP32 because
it is Gather-only. This is our application of official ORT APIs, not a Microsoft
published end-to-end Florence export/serving pipeline.

Actual fused operators: encoder has 13 SkipLayerNormalization and 6 BiasGelu;
each decoder has 19 and 6 respectively. Attention/MultiHeadAttention fusion
counts are zero. The optimizer emitted non-fatal Concat removal warnings.
Successful graph loading alone is not proof that all fusions succeeded.

The controlled bundle contains the pipeline manifest, per-file SHA-256 and
license notice. Actual CPU profiling executes DynamicQuantizeMatMul in the
vision session and MatMulIntegerToFloat/DynamicQuantizeMatMul in all three
Transformer sessions; those graphs are not expanded wholesale into an FP32
model.

## Verification scope

- Processor parity is tested with the bundled reference code, offline.
- On 19 selected crops (8 synthetic, 11 WeChat), official and lightweight
  processors produce identical captions and pixel max error 0.
- Identical batches repeat stably. Single-versus-batch output differs on one
  WeChat menu crop: `Center` vs `All Sheets`; neither matches the annotation.
  The report preserves this disagreement rather than claiming universal parity.
- The reviewed 167-row run fixes the Transformer graphs at INT8 and changes
  only the vision graph. INT8 vision Top-1 is 43.1%, high-frequency is 54.5%,
  WeChat is 6/11, and Oryx is 11/16; the FP32-vision baseline is 40.7%,
  54.5%, 6/11, and 10/16 respectively. Top-3 is 61.1% versus 60.5% for FP32
  vision. Twenty-two captions changed.
  All 100 repeated first-crop calls are stable. Both paths label all eight
  decorative synthetic samples, so the measured non-icon false-positive rate
  remains 100%; this remains a quality limitation, not a resolved acceptance
  criterion.
- The measured INT8-vision peak RSS is about 1.58 GiB, effectively unchanged
  from this run's FP32-vision baseline; peak Private Bytes falls by about
  240 MiB to 2.59 GiB. First inference and 16-image batch latency improve by
  about 50% and 46%. These are exploratory CPU measurements, not production
  SLOs.

Remaining evaluation limitations: the synthetic en/zh rows duplicate the same
pixels and do not test localized UI; scale changes stroke width rather than OS
DPI; only 27 real crops are labelled. The alias matcher misses synonyms such as
`Find`/`search` and `Mic`/`microphone`. The eight decorative samples all receive
non-unknown Florence labels. Top-3 uses experimental cumulative-score beam
search rather than verified Hugging Face beam equivalence. These figures must
not be presented as production desktop accuracy or calibrated confidence.
The original requirement for a broad desktop corpus remains an evaluation task.

## Reproduction

Local checks: the 0.3.1 bundle update must pass the full test, Ruff,
compileall, doctor, source-model-hash, wheel, standalone, and installer gates
before publication. The checked-in release fixture contains 140 synthetic rows,
11 real WeChat crops, and 16 manually labelled crops from a WGC capture of Oryx
Desktop 0.19.1. The release-grade run requires at least eight samples from each
real source and records `release_ready=false` if either source falls below it.

An isolated source daemon started within the existing 90-second timeout and
served `computer_get_screen_size` successfully. Its log records detector/OCR
warm-up ready at 1579 ms, then semantic ready 1909 ms later. The temporary
daemon was stopped; the user's normal daemon was not restarted.

Run from the repository with locally acquired source graphs; runtime never
downloads models.

```powershell
uv run --no-sync --with onnx python scripts/quantize_onnx_community_florence2.py --output-dir artifacts/icon-semantic-poc/onnx-community/pipeline-vision-int8-reviewed --bundle-dir computer_server/models/omniparser_v2/icon_caption_onnx_community
uv run --no-sync python scripts/benchmark_onnx_community_florence2.py --pipeline-dir artifacts/icon-semantic-poc/onnx-community/pipeline-vision-int8-reviewed --output-dir artifacts/icon-semantic-poc/onnx-community/benchmark-vision-int8-reviewed
uv run --no-sync python scripts/verify_florence2_runtime.py
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync python scripts/validate_release.py --version 0.3.1
```

The curated WeChat and Oryx crops live under `tests/fixtures/icon_semantics` so
a clean checkout exercises the same real-source gate. The Oryx full-window
fixture also makes the frozen-runtime SOM smoke independent of foreground UI.
Commit/push, version bump, published release and installed-daemon replacement
are separate delivery steps. This review does not change an existing release tag.
