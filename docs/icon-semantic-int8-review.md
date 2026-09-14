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

The quantization implementation uses ORT's `quant_pre_process`,
`optimize_model(model_type="bart", only_onnxruntime=False)` and dynamic
per-channel QInt8 for constant-weight MatMul/Gemm. Vision and embedding graphs
remain FP32. This is our application of official ORT APIs, not a Microsoft
published end-to-end Florence export/serving pipeline.

Actual fused operators: encoder has 13 SkipLayerNormalization and 6 BiasGelu;
each decoder has 19 and 6 respectively. Attention/MultiHeadAttention fusion
counts are zero. The optimizer emitted non-fatal Concat removal warnings.
Successful graph loading alone is not proof that all fusions succeeded.

The controlled bundle contains the pipeline manifest, per-file SHA-256 and
license notice. Actual CPU profiling executes MatMulIntegerToFloat and
DynamicQuantizeMatMul in all three Transformer sessions; those graphs are not
expanded wholesale into an FP32 model.

## Verification scope

- Processor parity is tested with the bundled reference code, offline.
- On 19 selected crops (8 synthetic, 11 WeChat), official and lightweight
  processors produce identical captions and pixel max error 0.
- Identical batches repeat stably. Single-versus-batch output differs on one
  WeChat menu crop: `Center` vs `All Sheets`; neither matches the annotation.
  The report preserves this disagreement rather than claiming universal parity.
- The reviewed 151-row run preserves INT8 Top-1 alias-match 38.4%, high-frequency
  53.5%, WeChat 6/11. Community FP32 is 39.7%, 55.6%, and 6/11 respectively.
  All 100 repeated first-crop calls are stable.
- The measured INT8 peak RSS is about 1.97 GB and Private Bytes about 3.05 GB.
  A profiling process overlapped part of this run: latency is exploratory and
  must not be compared as a controlled speedup against earlier runs.

Remaining evaluation limitations: the synthetic en/zh rows duplicate the same
pixels and do not test localized UI; scale changes stroke width rather than OS
DPI; only 11 real crops are labelled. The alias matcher misses synonyms such as
`Find`/`search` and `Mic`/`microphone`. The eight decorative samples all receive
non-unknown Florence labels. Top-3 uses experimental cumulative-score beam
search rather than verified Hugging Face beam equivalence. These figures must
not be presented as production desktop accuracy or calibrated confidence.
The original requirement for a broad desktop corpus remains an evaluation task.

## Reproduction

Local checks: 470 tests passed; Ruff, compileall, doctor and source model hashes
passed. A local wheel was built and its internal nested model hashes validated;
PoC resources and development scripts were absent. PowerShell build/smoke
scripts were syntax checked; a new standalone installer has not been built.

An isolated source daemon started within the existing 90-second timeout and
served `computer_get_screen_size` successfully. Its log records detector/OCR
warm-up ready at 1579 ms, then semantic ready 1909 ms later. The temporary
daemon was stopped; the user's normal daemon was not restarted.

Run from the repository with locally acquired source graphs; runtime never
downloads models.

```powershell
uv run --no-sync --with onnx python scripts/quantize_onnx_community_florence2.py --output-dir artifacts/icon-semantic-poc/onnx-community/pipeline-reviewed --bundle-dir computer_server/models/omniparser_v2/icon_caption_onnx_community
uv run --no-sync python scripts/benchmark_onnx_community_florence2.py --pipeline-dir artifacts/icon-semantic-poc/onnx-community/pipeline-reviewed --output-dir artifacts/icon-semantic-poc/onnx-community/benchmark-reviewed
uv run --no-sync python scripts/verify_florence2_runtime.py
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync python scripts/validate_release.py --version 0.3.0
```

The historical benchmark automatically includes the local
`artifacts/wechat-som-20260801/wechat-window.png` when available; otherwise
it has 140 synthetic rows, not 151. Private screenshots are not bundled.
Commit/push, version bump, published release and installed-daemon replacement
are separate delivery steps. This review does not change an existing release tag.
