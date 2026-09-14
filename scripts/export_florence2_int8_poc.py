"""Export the bundled OmniParser Florence-2 icon model and make an INT8 PoC.

This is an offline build-time tool.  It exports one no-cache logits step and
the runtime performs the small greedy decode loop outside ONNX Runtime.  The
original PyTorch model is never used by the INT8 runtime service.

The exporter intentionally quantizes only MatMul/Gemm weights.  Vision
convolutions, LayerNorm, Softmax, and preprocessing remain FP32.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

LOGGER = logging.getLogger("export_florence2_int8_poc")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_wrapper(torch: Any, model: Any) -> Any:
    class _Wrapper(torch.nn.Module):
        def __init__(self, wrapped: Any) -> None:
            super().__init__()
            self.wrapped = wrapped

        def forward(self, input_ids: Any, pixel_values: Any, decoder_input_ids: Any) -> Any:
            output = self.wrapped(
                input_ids=input_ids,
                pixel_values=pixel_values,
                decoder_input_ids=decoder_input_ids,
                use_cache=False,
                return_dict=True,
            )
            return output.logits[:, -1, :]

    return _Wrapper(model).eval()


def _export_fp32(
    *,
    torch: Any,
    model: Any,
    processor: Any,
    output_path: Path,
) -> dict[str, Any]:
    wrapper = _build_wrapper(torch, model)
    image = Image.new("RGB", (64, 64), "white")
    inputs = processor(
        images=[image],
        text=["<CAPTION>"],
        return_tensors="pt",
        do_resize=False,
    )
    input_ids = inputs["input_ids"]
    pixel_values = inputs["pixel_values"].to(dtype=torch.float32)
    decoder_input_ids = torch.tensor([[2]], dtype=torch.long)
    started = time.perf_counter()
    with torch.inference_mode():
        torch.onnx.export(
            wrapper,
            (input_ids, pixel_values, decoder_input_ids),
            str(output_path),
            input_names=["input_ids", "pixel_values", "decoder_input_ids"],
            output_names=["logits"],
            dynamic_axes={
                "input_ids": {0: "batch"},
                "pixel_values": {0: "batch"},
                "decoder_input_ids": {0: "batch", 1: "decoder_sequence"},
                "logits": {0: "batch"},
            },
            opset_version=17,
            do_constant_folding=True,
            dynamo=False,
        )
    return {
        "duration_seconds": time.perf_counter() - started,
        "input_ids_shape": list(input_ids.shape),
        "pixel_values_shape": list(pixel_values.shape),
        "decoder_input_ids_shape": list(decoder_input_ids.shape),
    }


def _quantize(fp32_path: Path, int8_path: Path) -> dict[str, Any]:
    from onnxruntime.quantization import QuantType, quantize_dynamic

    started = time.perf_counter()
    quantize_dynamic(
        str(fp32_path),
        str(int8_path),
        op_types_to_quantize=["MatMul", "Gemm"],
        per_channel=True,
        reduce_range=False,
        weight_type=QuantType.QInt8,
        extra_options={"MatMulConstBOnly": True},
    )
    return {"duration_seconds": time.perf_counter() - started}


def _verify(
    *,
    torch: Any,
    model: Any,
    processor: Any,
    fp32_path: Path,
    int8_path: Path,
) -> dict[str, Any]:
    import onnx
    import onnxruntime as ort

    image = Image.new("RGB", (64, 64), "white")
    inputs = processor(
        images=[image],
        text=["<CAPTION>"],
        return_tensors="pt",
        do_resize=False,
    )
    decoder_input_ids = torch.tensor([[2]], dtype=torch.long)
    with torch.inference_mode():
        reference = model(
            input_ids=inputs["input_ids"],
            pixel_values=inputs["pixel_values"].to(dtype=torch.float32),
            decoder_input_ids=decoder_input_ids,
            use_cache=False,
            return_dict=True,
        ).logits[:, -1, :].cpu().numpy()

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sessions = {
        "fp32": ort.InferenceSession(
            str(fp32_path), sess_options=options, providers=["CPUExecutionProvider"]
        ),
        "int8": ort.InferenceSession(
            str(int8_path), sess_options=options, providers=["CPUExecutionProvider"]
        ),
    }
    feed = {
        "input_ids": inputs["input_ids"].cpu().numpy(),
        "pixel_values": inputs["pixel_values"].cpu().numpy().astype(np.float32),
        "decoder_input_ids": decoder_input_ids.cpu().numpy(),
    }
    outputs = {name: session.run(None, feed)[0] for name, session in sessions.items()}
    fp32_output = np.asarray(outputs["fp32"], dtype=np.float32)
    int8_output = np.asarray(outputs["int8"], dtype=np.float32)
    fp32_model = onnx.load(str(fp32_path), load_external_data=False)
    int8_model = onnx.load(str(int8_path), load_external_data=False)
    int8_ops: dict[str, int] = {}
    for node in int8_model.graph.node:
        int8_ops[node.op_type] = int8_ops.get(node.op_type, 0) + 1
    return {
        "providers": {
            name: session.get_providers() for name, session in sessions.items()
        },
        "reference_top1": int(np.argmax(reference, axis=-1)[0]),
        "fp32_top1": int(np.argmax(fp32_output, axis=-1)[0]),
        "int8_top1": int(np.argmax(int8_output, axis=-1)[0]),
        "fp32_max_abs_error_vs_pytorch": float(
            np.max(np.abs(fp32_output - reference))
        ),
        "int8_max_abs_error_vs_pytorch": float(
            np.max(np.abs(int8_output - reference))
        ),
        "int8_top1_matches_fp32": bool(
            np.argmax(int8_output, axis=-1)[0] == np.argmax(fp32_output, axis=-1)[0]
        ),
        "fp32_node_count": len(fp32_model.graph.node),
        "int8_node_count": len(int8_model.graph.node),
        "int8_operator_counts": int8_ops,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path("computer_server/models/omniparser_v2/icon_caption"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/florence2_int8"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor
    except Exception as exc:
        LOGGER.error("Florence export dependencies unavailable: %s", exc)
        return 2

    model_dir = args.model_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    fp32_path = output_dir / "model.fp32.onnx"
    int8_path = output_dir / "model.int8.onnx"
    processor = AutoProcessor.from_pretrained(
        str(model_dir), local_files_only=True, trust_remote_code=True
    )
    model = AutoModelForCausalLM.from_pretrained(
        str(model_dir),
        local_files_only=True,
        trust_remote_code=True,
        torch_dtype=torch.float32,
    ).eval()
    export = _export_fp32(
        torch=torch,
        model=model,
        processor=processor,
        output_path=fp32_path,
    )
    quantize = _quantize(fp32_path, int8_path)
    verify = _verify(
        torch=torch,
        model=model,
        processor=processor,
        fp32_path=fp32_path,
        int8_path=int8_path,
    )
    manifest = {
        "source_model_dir": str(model_dir),
        "source_manifest": json.loads(
            (model_dir / "model_manifest.json").read_text(encoding="utf-8")
        ),
        "export": export,
        "quantization": {
            "method": "onnxruntime.quantize_dynamic",
            "weight_type": "QInt8",
            "op_types": ["MatMul", "Gemm"],
            "per_channel": True,
            "reduce_range": False,
            "runtime_provider": "CPUExecutionProvider",
            **quantize,
        },
        "verification": verify,
        "files": {
            name: {
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for name, path in (
                ("model.fp32.onnx", fp32_path),
                ("model.int8.onnx", int8_path),
            )
        },
    }
    (output_dir / "model_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
