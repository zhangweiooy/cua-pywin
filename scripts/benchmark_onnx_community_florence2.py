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

REQUIRED_RELEASE_SOURCE_MINIMUMS = {"wechat": 8, "oryx": 8}


def _fixture_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        source = str(row.get("source") or "unknown").lower()
        counts[source] = counts.get(source, 0) + 1
    missing = sorted(
        source
        for source, minimum in REQUIRED_RELEASE_SOURCE_MINIMUMS.items()
        if counts.get(source, 0) < minimum
    )
    return {
        "source_counts": counts,
        "required_release_source_minimums": dict(
            sorted(REQUIRED_RELEASE_SOURCE_MINIMUMS.items())
        ),
        "missing_release_sources": missing,
        "release_ready": not missing,
    }


class VisionBenchmarkRunner(BenchmarkFlorence2Runner):
    """Keep Transformer INT8 fixed while selecting the vision graph."""

    def __init__(self, *args, int8_vision: bool, **kwargs) -> None:
        self.int8_vision = int8_vision
        super().__init__(*args, quantized=True, **kwargs)

    def _vision_graph_path(self, source_onnx: Path) -> Path:
        if self.int8_vision:
            return super()._vision_graph_path(source_onnx)
        return source_onnx / "vision_encoder.onnx"


def _worker(args: argparse.Namespace) -> int:
    import psutil

    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    images = [
        Image.open(args.manifest.parent / row["path"]).convert("RGB") for row in rows
    ]
    process = psutil.Process()
    runner = VisionBenchmarkRunner(
        args.source_dir,
        args.pipeline_dir,
        int8_vision=args.int8_vision,
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
        "backend": (
            "florence2_int8_community"
            if args.int8_vision
            else "florence2_int8_transformers_fp32_vision"
        ),
        "transformer_quantized": True,
        "vision_quantized": args.int8_vision,
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
    if args.fixture_manifest is None:
        fixture = args.output_dir / "fixture"
        rows = _make_fixture(fixture)
        manifest = fixture / "manifest.json"
    else:
        manifest = args.fixture_manifest.resolve()
        rows = json.loads(manifest.read_text(encoding="utf-8"))
        fixture = manifest.parent
    coverage = _fixture_coverage(rows)
    if not coverage["release_ready"] and not args.allow_incomplete_real_fixture:
        print(
            "release fixture is missing required real sources: "
            + ", ".join(coverage["missing_release_sources"]),
            file=sys.stderr,
        )
        return 2
    results: dict[str, Any] = {}
    for int8_vision in (False, True):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--manifest",
            str(manifest.resolve()),
            "--source-dir",
            str(args.source_dir.resolve()),
            "--pipeline-dir",
            str(args.pipeline_dir.resolve()),
            "--repeat-count",
            str(args.repeat_count),
        ]
        if int8_vision:
            command.append("--int8-vision")
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
                f"community worker failed (int8_vision={int8_vision})\n"
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
        "coverage": coverage,
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
    parser.add_argument("--int8-vision", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--fixture-manifest", type=Path)
    parser.add_argument("--allow-incomplete-real-fixture", action="store_true")
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
