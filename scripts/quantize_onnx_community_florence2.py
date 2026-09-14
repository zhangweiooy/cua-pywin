"""Run the official ONNX Runtime pre-process/optimization/INT8 pipeline.

The source graphs come from the ONNX Community conversion of the specialized
OmniParser Florence-2 checkpoint.  Vision and token-embedding graphs remain
FP32; the Transformer encoder and both decoder graphs use symbolic/ONNX shape
pre-processing, the ONNX Runtime Transformer optimizer, then dynamic per-
channel QInt8 quantization of MatMul/Gemm weights.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("quantize_onnx_community_florence2")
GRAPH_NAMES = (
    "encoder_model",
    "decoder_model",
    "decoder_with_past_model",
)


def bundle_pipeline(source_dir: Path, pipeline_dir: Path, bundle_dir: Path) -> None:
    """Stage the reviewed local graphs and generate checksums from actual bytes."""
    files = {
        name: source_dir / name
        for name in (
            "added_tokens.json",
            "config.json",
            "configuration_florence2.py",
            "generation_config.json",
            "merges.txt",
            "preprocessor_config.json",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.json",
            "onnx/vision_encoder.onnx",
            "onnx/embed_tokens.onnx",
        )
    }
    files.update(
        {
            f"int8/{name}.int8.onnx": pipeline_dir / "int8" / f"{name}.int8.onnx"
            for name in GRAPH_NAMES
        }
    )
    files["processing_florence2.py"] = source_dir / "processing_florence2.py"
    files["LICENSE"] = source_dir / "LICENSE"
    files["pipeline_manifest.json"] = pipeline_dir / "pipeline_manifest.json"
    for name, source in files.items():
        target = bundle_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
    manifest = json.loads(
        (pipeline_dir / "pipeline_manifest.json").read_text(encoding="utf-8")
    )
    manifest = {
        "source": manifest["source"],
        "pipeline": manifest["pipeline"],
        "sha256": {name: _sha256(bundle_dir / name) for name in files},
    }
    manifest["source"]["license"] = (
        "MIT; bundled processor code retains its Apache-2.0 header"
    )
    (bundle_dir / "model_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optimize_and_quantize(source: Path, output_dir: Path) -> dict[str, Any]:
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from onnxruntime.quantization.shape_inference import quant_pre_process
    from onnxruntime.transformers.optimizer import optimize_model

    preprocessed_dir = output_dir / "preprocessed"
    optimized_dir = output_dir / "optimized"
    quantized_dir = output_dir / "int8"
    for directory in (preprocessed_dir, optimized_dir, quantized_dir):
        directory.mkdir(parents=True, exist_ok=True)

    stem = source.stem
    preprocessed = preprocessed_dir / f"{stem}.pre.onnx"
    optimized = optimized_dir / f"{stem}.opt.onnx"
    quantized = quantized_dir / f"{stem}.int8.onnx"

    started = time.perf_counter()
    quant_pre_process(
        source,
        preprocessed,
        skip_optimization=False,
        skip_symbolic_shape=False,
        skip_onnx_shape=False,
    )
    preprocess_seconds = time.perf_counter() - started

    started = time.perf_counter()
    transformer_model = optimize_model(
        str(preprocessed),
        model_type="bart",
        num_heads=12,
        hidden_size=768,
        opt_level=2,
        use_gpu=False,
        only_onnxruntime=False,
    )
    transformer_model.save_model_to_file(
        str(optimized),
        use_external_data_format=False,
    )
    optimize_seconds = time.perf_counter() - started
    fusion_statistics = transformer_model.get_fused_operator_statistics()

    started = time.perf_counter()
    quantize_dynamic(
        optimized,
        quantized,
        op_types_to_quantize=["MatMul", "Gemm"],
        per_channel=True,
        reduce_range=False,
        weight_type=QuantType.QInt8,
        extra_options={"MatMulConstBOnly": True},
    )
    quantize_seconds = time.perf_counter() - started

    return {
        "source": str(source),
        "preprocessed": str(preprocessed),
        "optimized": str(optimized),
        "int8": str(quantized),
        "durations_seconds": {
            "preprocess": preprocess_seconds,
            "transformer_optimize": optimize_seconds,
            "quantize": quantize_seconds,
        },
        "fusion_statistics": fusion_statistics,
        "files": {
            name: {
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for name, path in (
                ("source", source),
                ("preprocessed", preprocessed),
                ("optimized", optimized),
                ("int8", quantized),
            )
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/onnx-community/onnx"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/onnx-community/pipeline"),
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        help="Stage local reviewed resources into this controlled bundle",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    results: dict[str, Any] = {}
    for name in GRAPH_NAMES:
        source = args.source_dir / f"{name}.onnx"
        if not source.is_file():
            raise FileNotFoundError(source)
        LOGGER.info("processing %s", source.name)
        results[name] = _optimize_and_quantize(source, args.output_dir)

    manifest = {
        "source": {
            "repository": "onnx-community/OmniParser-v2.0_icon_caption",
            "revision": "0d28990c05ddeb50792d02b92f7e6e4d0f4bd086",
            "specialized_model": "microsoft/OmniParser-v2.0/icon_caption",
        },
        "pipeline": {
            "preprocess": "onnxruntime.quantization.shape_inference.quant_pre_process",
            "symbolic_shape_inference": True,
            "onnx_shape_inference": True,
            "transformer_optimizer": "onnxruntime.transformers.optimizer.optimize_model",
            "transformer_model_type": "bart",
            "num_heads": 12,
            "hidden_size": 768,
            "optimization_level": 2,
            "only_onnxruntime": False,
            "quantization": "onnxruntime.quantization.quantize_dynamic",
            "weight_type": "QInt8",
            "op_types": ["MatMul", "Gemm"],
            "per_channel": True,
            "reduce_range": False,
            "execution_provider": "CPUExecutionProvider",
        },
        "graphs": results,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "pipeline_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if args.bundle_dir is not None:
        bundle_pipeline(args.source_dir.parent, args.output_dir, args.bundle_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
