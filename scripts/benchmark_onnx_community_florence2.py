"""Benchmark the official ONNX Community split-graph pipeline."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from PIL import Image

from scripts.benchmark_florence2_beam import BenchmarkFlorence2Runner
from computer_server.services.icon_caption_service import (
    FLORENCE2_COMMUNITY_CONFIDENCE_PROVENANCE,
    IconCaption,
    normalize_caption,
)
from scripts.benchmark_icon_semantics import (
    _MemorySampler,
    _evaluate,
    _make_fixture,
    _private_bytes,
)


def _worker(args: argparse.Namespace) -> int:
    import psutil

    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    images = [
        Image.open(args.manifest.parent / row["path"]).convert("RGB") for row in rows
    ]
    process = psutil.Process()
    runner = BenchmarkFlorence2Runner(
        args.source_dir,
        args.pipeline_dir,
        quantized=args.quantized,
        batch_size=64,
        max_new_tokens=20,
    )
    with _MemorySampler(process) as sampler:
        rss_before = int(process.memory_info().rss)
        started = time.perf_counter()
        runner.load()
        load_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        first = runner.caption_images(images[:1])
        first_ms = (time.perf_counter() - started) * 1000
        stable_times: list[float] = []
        for _ in range(10):
            started = time.perf_counter()
            runner.caption_images(images[:1])
            stable_times.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        runner.caption_images(images[:16])
        batch_ms = (time.perf_counter() - started) * 1000
        raw_captions = runner.caption_images(images)
        repeated: list[str | None] = []
        for _ in range(args.repeat_count):
            repeated.append(runner.caption_images(images[:1])[0])
        rss_after = int(process.memory_info().rss)
        private_after = _private_bytes(process)

    captions = [
        IconCaption(
            raw_caption=raw,
            normalized_label=normalize_caption(raw),
            confidence_provenance=FLORENCE2_COMMUNITY_CONFIDENCE_PROVENANCE,
            backend="florence2_int8_community",
        )
        for raw in raw_captions
    ]
    top3_started = time.perf_counter()
    top3_candidates = runner.caption_images_topk(images, beam_width=3)
    top3_ms = (time.perf_counter() - top3_started) * 1000
    captions = [
        replace(
            caption,
            candidates=tuple(
                (normalize_caption(candidate) or candidate, 0.0)
                for candidate in candidates
            ),
        )
        for caption, candidates in zip(captions, top3_candidates)
    ]
    result = {
        "backend": "florence2_int8_community"
        if args.quantized
        else "florence2_onnx_community_fp32",
        "quantized": args.quantized,
        "torch_imported": "torch" in sys.modules,
        "top3_method": "experimental cumulative-logprob beam search; not HF beam parity",
        "dataset_samples": len(rows),
        "providers": runner.providers,
        "load_ms": load_ms,
        "first_inference_ms": first_ms,
        "stable_single_p50_ms": statistics.median(stable_times),
        "stable_single_p95_ms": sorted(stable_times)[
            max(0, int(len(stable_times) * 0.95) - 1)
        ],
        "batch_inference_ms": batch_ms,
        "top3_inference_ms": top3_ms,
        "top3_samples": len(top3_candidates),
        "repeat_count": args.repeat_count,
        "repeated_unique_labels": sorted(set(repeated), key=lambda value: str(value)),
        "repeated_stable": len(set(repeated)) <= 1,
        "rss_before_bytes": rss_before,
        "rss_after_bytes": rss_after,
        "private_after_bytes": private_after,
        "peak_rss_bytes": sampler.peak_rss,
        "peak_private_bytes": sampler.peak_private,
        "accuracy": _evaluate(rows, captions),
        "first_caption": first[0] if first else None,
        "predictions": [
            {
                "path": row["path"],
                "expected": row["expected"],
                "raw_caption": caption.raw_caption,
                "label": caption.label,
            }
            for row, caption in zip(rows, captions)
        ],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _parent(args: argparse.Namespace) -> int:
    fixture = args.output_dir / "fixture"
    _make_fixture(fixture)
    results: dict[str, Any] = {}
    for quantized in (False, True):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--manifest",
            str((fixture / "manifest.json").resolve()),
            "--source-dir",
            str(args.source_dir.resolve()),
            "--pipeline-dir",
            str(args.pipeline_dir.resolve()),
            "--repeat-count",
            str(args.repeat_count),
        ]
        if quantized:
            command.append("--quantized")
        environment = dict(os.environ)
        environment["TRANSFORMERS_OFFLINE"] = "1"
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            print(
                f"community worker failed (quantized={quantized})\n"
                f"{completed.stdout}\n{completed.stderr}",
                file=sys.stderr,
            )
            return completed.returncode
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        results[result["backend"]] = result
    report = {
        "fixture": str(fixture.resolve()),
        "source_dir": str(args.source_dir.resolve()),
        "pipeline_dir": str(args.pipeline_dir.resolve()),
        "results": results,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--quantized", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/onnx-community"),
    )
    parser.add_argument(
        "--pipeline-dir",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/onnx-community/pipeline"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/onnx-community/benchmark"),
    )
    parser.add_argument("--repeat-count", type=int, default=100)
    return parser.parse_args(argv)


if __name__ == "__main__":
    arguments = _parse_args()
    if arguments.worker:
        if arguments.manifest is None:
            raise SystemExit("--manifest is required in worker mode")
        raise SystemExit(_worker(arguments))
    raise SystemExit(_parent(arguments))
