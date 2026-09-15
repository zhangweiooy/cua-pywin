"""Offline verification of processor parity, batching and actual CPU INT8 kernels."""

from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

from computer_server.services.community_florence2_service import (
    CommunityFlorence2Runner,
)
from scripts.benchmark_icon_semantics import _match


def verify(bundle: Path, fixture: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)

    class ProfiledRunner(CommunityFlorence2Runner):
        def _session(self, path):
            options = ort.SessionOptions()
            options.enable_profiling = True
            options.profile_file_prefix = str(output / path.stem)
            return ort.InferenceSession(
                str(path), sess_options=options, providers=["CPUExecutionProvider"]
            )

    runner = ProfiledRunner(bundle, bundle)
    runner.load()
    rows = json.loads(fixture.read_text(encoding="utf-8"))
    # High-frequency synthetic crops and all available real desktop crops.
    selected = rows[:8] + [row for row in rows if row.get("source") == "wechat"]
    images = [
        Image.open(fixture.parent / row["path"]).convert("RGB") for row in selected
    ]
    individual = [runner.caption_images([image])[0] for image in images]
    batched = runner.caption_images(images)
    differences = [
        {
            "path": row["path"],
            "expected": row["expected"],
            "single": a,
            "batch": b,
            "single_correct": _match(row["expected"], a),
            "batch_correct": _match(row["expected"], b),
        }
        for row, a, b in zip(selected, individual, batched)
        if a != b
    ]
    if any(
        item["single_correct"] and not item["batch_correct"] for item in differences
    ):
        raise AssertionError(f"batching lost a correct fixture label: {differences}")
    if batched != runner.caption_images(images):
        raise AssertionError("repeated identical batches are unstable")

    # Official processor is test-only; share one set of ONNX sessions.
    from transformers import BartTokenizerFast, CLIPImageProcessor

    spec = importlib.util.spec_from_file_location(
        "reference_processor",
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "florence2"
        / "processing_florence2.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    official = module.Florence2Processor(
        CLIPImageProcessor.from_pretrained(str(bundle), local_files_only=True),
        BartTokenizerFast.from_pretrained(str(bundle), local_files_only=True),
    )
    arguments = dict(
        images=images,
        text=["<CAPTION>"] * len(images),
        return_tensors="np",
        do_resize=False,
    )
    light_inputs, reference_inputs = (
        runner._processor(**arguments),
        official(**arguments),
    )
    np.testing.assert_array_equal(
        light_inputs["input_ids"], reference_inputs["input_ids"]
    )
    np.testing.assert_allclose(
        light_inputs["pixel_values"],
        reference_inputs["pixel_values"],
        atol=1e-6,
        rtol=0,
    )
    runner._processor = official
    reference_captions = runner.caption_images(images)
    if reference_captions != batched:
        raise AssertionError("official/lightweight captions differ")
    profiles = {}
    for name, session in runner._sessions.items():
        path = Path(session.end_profiling())
        events = json.loads(path.read_text(encoding="utf-8"))
        kernels = [
            event["args"]
            for event in events
            if event.get("cat") == "Node" and event.get("args", {}).get("provider")
        ]
        operators = Counter(event.get("op_name") for event in kernels)
        providers = sorted({event["provider"] for event in kernels})
        if providers != ["CPUExecutionProvider"]:
            raise AssertionError(f"unexpected providers: {providers}")
        quantized = {
            op: count
            for op, count in operators.items()
            if any(token in op for token in ("Integer", "Quant", "QLinear"))
        }
        if name in {"encoder", "decoder", "decoder_with_past"} and not quantized:
            raise AssertionError(f"no quantized kernels executed: {name}")
        profiles[name] = {"providers": providers, "quantized_kernel_calls": quantized}
    return {
        "samples": len(images),
        "single_batch_exact_match": individual == batched,
        "batch_differences": differences,
        "repeated_batch_stable": True,
        "official_lightweight_caption_match": True,
        "pixel_max_abs_error": float(
            np.max(
                np.abs(light_inputs["pixel_values"] - reference_inputs["pixel_values"])
            )
        ),
        "profiles": profiles,
        "captions": batched,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bundle",
        type=Path,
        default=Path(
            "computer_server/models/omniparser_v2/icon_caption_onnx_community"
        ),
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/benchmark/fixture/manifest.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/runtime-verification"),
    )
    args = parser.parse_args()
    result = verify(args.bundle, args.fixture, args.output)
    (args.output / "verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
