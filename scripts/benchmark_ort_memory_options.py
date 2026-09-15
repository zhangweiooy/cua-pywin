"""Process-isolated A/B benchmark for ORT thread and allocator options."""

from __future__ import annotations

import argparse
import io
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import psutil
from PIL import Image

from scripts.benchmark_icon_semantics import _MemorySampler, _private_bytes

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT = ROOT / "artifacts/yolo-onnx-poc/screenshots/oryx-light-native.png"
DETECTOR_MODEL = ROOT / "som/weights/icon_detect/model.onnx"
FLORENCE_BUNDLE = (
    ROOT / "computer_server/models/omniparser_v2/icon_caption_onnx_community"
)


def _metrics(
    process: psutil.Process,
    sampler: _MemorySampler,
    *,
    load_ms: float,
    first_ms: float,
    stable_ms: list[float],
    output: Any,
) -> dict[str, Any]:
    time.sleep(1)
    return {
        "load_ms": load_ms,
        "first_ms": first_ms,
        "stable_p50_ms": statistics.median(stable_ms),
        "stable_p95_ms": sorted(stable_ms)[max(0, int(len(stable_ms) * 0.95) - 1)],
        "rss_after_bytes": process.memory_info().rss,
        "private_after_bytes": _private_bytes(process),
        "peak_rss_bytes": sampler.peak_rss,
        "peak_private_bytes": sampler.peak_private,
        "threads": process.num_threads(),
        "output": output,
    }


def _session_options(*, threads: int, arena: bool, memory_pattern: bool):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = arena
    options.enable_mem_pattern = memory_pattern
    return options


def _yolo_worker(args: argparse.Namespace) -> dict[str, Any]:
    import onnxruntime as ort

    from som.detection import DetectionProcessor

    image = Image.open(SCREENSHOT).convert("RGB")
    process = psutil.Process()
    detector = DetectionProcessor(
        model_path=DETECTOR_MODEL,
        force_device="cpu",
        intra_op_num_threads=args.threads,
    )
    with _MemorySampler(process) as sampler:
        started = time.perf_counter()
        session = ort.InferenceSession(
            str(DETECTOR_MODEL),
            sess_options=_session_options(
                threads=args.threads,
                arena=args.arena,
                memory_pattern=args.memory_pattern,
            ),
            providers=["CPUExecutionProvider"],
        )
        detector.model = session
        detector.input_name = session.get_inputs()[0].name
        detector.output_name = session.get_outputs()[0].name
        load_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        first = detector.detect_icons(image)
        first_ms = (time.perf_counter() - started) * 1000
        stable_ms = []
        for _ in range(args.repeat_count):
            started = time.perf_counter()
            detector.detect_icons(image)
            stable_ms.append((time.perf_counter() - started) * 1000)
    return _metrics(
        process,
        sampler,
        load_ms=load_ms,
        first_ms=first_ms,
        stable_ms=stable_ms,
        output={"count": len(first), "first": first[:3]},
    )


def _rapidocr_worker(args: argparse.Namespace) -> dict[str, Any]:
    from rapidocr import RapidOCR

    from som.ocr import OCRProcessor

    image = np.asarray(Image.open(SCREENSHOT).convert("RGB"))
    params = OCRProcessor()._rapidocr_params()
    params.update(
        {
            "EngineConfig.onnxruntime.intra_op_num_threads": args.threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
    )
    process = psutil.Process()
    with _MemorySampler(process) as sampler:
        started = time.perf_counter()
        reader = RapidOCR(params=params)
        load_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        first = reader(image, text_score=0.5)
        first_ms = (time.perf_counter() - started) * 1000
        stable_ms = []
        for _ in range(args.repeat_count):
            started = time.perf_counter()
            reader(image, text_score=0.5)
            stable_ms.append((time.perf_counter() - started) * 1000)
    texts = list(getattr(first, "txts", None) or [])
    return _metrics(
        process,
        sampler,
        load_ms=load_ms,
        first_ms=first_ms,
        stable_ms=stable_ms,
        output={"count": len(texts), "texts": texts},
    )


def _florence_images() -> list[Image.Image]:
    images: list[Image.Image] = []
    fixture_root = ROOT / "tests/fixtures/icon_semantics"
    for source in ("oryx", "wechat"):
        directory = fixture_root / source
        rows = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        images.extend(Image.open(directory / row["path"]).convert("RGB") for row in rows)
    return images[:16]


def _florence_worker(args: argparse.Namespace) -> dict[str, Any]:
    import onnxruntime as ort

    from computer_server.services.community_florence2_service import (
        CommunityFlorence2Runner,
    )

    class Runner(CommunityFlorence2Runner):
        def _session(self, path: Path) -> Any:
            return ort.InferenceSession(
                str(path),
                sess_options=_session_options(
                    threads=1,
                    arena=args.arena,
                    memory_pattern=args.memory_pattern,
                ),
                providers=["CPUExecutionProvider"],
            )

    images = _florence_images()
    process = psutil.Process()
    runner = Runner(FLORENCE_BUNDLE, FLORENCE_BUNDLE)
    with _MemorySampler(process) as sampler:
        started = time.perf_counter()
        runner.load()
        load_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        first = runner.caption_images(images)
        first_ms = (time.perf_counter() - started) * 1000
        stable_ms = []
        for _ in range(args.repeat_count):
            started = time.perf_counter()
            runner.caption_images(images)
            stable_ms.append((time.perf_counter() - started) * 1000)
    return _metrics(
        process,
        sampler,
        load_ms=load_ms,
        first_ms=first_ms,
        stable_ms=stable_ms,
        output=first,
    )


def _full_worker(args: argparse.Namespace) -> dict[str, Any]:
    import onnxruntime as ort
    from rapidocr import RapidOCR

    from computer_server.handlers.som_bridge import _crop_icon_elements
    from computer_server.services.community_florence2_service import (
        CommunityFlorence2Runner,
    )
    from som.detect import OmniParser

    class Runner(CommunityFlorence2Runner):
        def _session(self, path: Path) -> Any:
            return ort.InferenceSession(
                str(path),
                sess_options=_session_options(
                    threads=1,
                    arena=florence_arena,
                    memory_pattern=args.memory_pattern,
                ),
                providers=["CPUExecutionProvider"],
            )

    image = Image.open(SCREENSHOT).convert("RGB")
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    screenshot_bytes = encoded.getvalue()
    process = psutil.Process()
    parser = OmniParser()
    runner = Runner(FLORENCE_BUNDLE, FLORENCE_BUNDLE)
    yolo_arena = args.arena if args.yolo_arena is None else args.yolo_arena
    florence_arena = (
        args.arena if args.florence_arena is None else args.florence_arena
    )
    with _MemorySampler(process) as sampler:
        started = time.perf_counter()
        detector_session = ort.InferenceSession(
            str(DETECTOR_MODEL),
            sess_options=_session_options(
                threads=4,
                arena=yolo_arena,
                memory_pattern=args.memory_pattern,
            ),
            providers=["CPUExecutionProvider"],
        )
        parser.detector.model = detector_session
        parser.detector.input_name = detector_session.get_inputs()[0].name
        parser.detector.output_name = detector_session.get_outputs()[0].name
        params = parser.ocr._rapidocr_params()
        params.update(
            {
                "EngineConfig.onnxruntime.intra_op_num_threads": args.threads,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            }
        )
        parser.ocr.reader = RapidOCR(params=params)
        runner.load()
        load_ms = (time.perf_counter() - started) * 1000

        def run_once() -> dict[str, Any]:
            _, elements = parser.process_image(image)
            _, crops = _crop_icon_elements(
                screenshot_bytes,
                elements,
                image.width,
                image.height,
            )
            captions = runner.caption_images(crops)
            return {
                "icons": sum(element.type == "icon" for element in elements),
                "texts": [
                    element.content
                    for element in elements
                    if element.type == "text"
                ],
                "captions": captions,
            }

        started = time.perf_counter()
        first = run_once()
        first_ms = (time.perf_counter() - started) * 1000
        stable_ms = []
        for _ in range(args.repeat_count):
            started = time.perf_counter()
            run_once()
            stable_ms.append((time.perf_counter() - started) * 1000)
    return _metrics(
        process,
        sampler,
        load_ms=load_ms,
        first_ms=first_ms,
        stable_ms=stable_ms,
        output=first,
    )


def _worker(args: argparse.Namespace) -> int:
    workers = {
        "yolo": _yolo_worker,
        "rapidocr": _rapidocr_worker,
        "florence": _florence_worker,
        "full": _full_worker,
    }
    result = workers[args.component](args)
    result.update(
        {
            "component": args.component,
            "threads_config": args.threads,
            "arena": args.arena,
            "memory_pattern": args.memory_pattern,
            "yolo_arena": args.yolo_arena,
            "florence_arena": args.florence_arena,
        }
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _parent(args: argparse.Namespace) -> int:
    variants = [
        ("yolo", 4, True, True),
        ("yolo", 4, False, False),
        ("rapidocr", -1, False, True),
        ("rapidocr", 4, False, True),
        ("rapidocr", 2, False, True),
        ("rapidocr", 1, False, True),
        ("florence", 1, True, True),
        ("florence", 1, False, True),
        ("florence", 1, True, False),
        ("florence", 1, False, False),
        ("full", -1, True, True),
        ("full", 4, False, True),
    ]
    results = []
    for component, threads, arena, memory_pattern in variants:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--component",
            component,
            "--threads",
            str(threads),
            "--repeat-count",
            str(args.repeat_count),
            "--arena" if arena else "--no-arena",
            "--memory-pattern" if memory_pattern else "--no-memory-pattern",
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise RuntimeError(
                f"worker failed: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}"
            )
        results.append(json.loads(completed.stdout.strip().splitlines()[-1]))
    report = {"repeat_count": args.repeat_count, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument(
        "--component",
        choices=("yolo", "rapidocr", "florence", "full"),
    )
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--repeat-count", type=int, default=3)
    parser.add_argument("--arena", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--yolo-arena",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument(
        "--florence-arena",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument(
        "--memory-pattern",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts/ort-memory-options/benchmark.json",
    )
    return parser.parse_args()


if __name__ == "__main__":
    parsed = _parse_args()
    if parsed.worker:
        if parsed.component is None:
            raise SystemExit("--component is required in worker mode")
        raise SystemExit(_worker(parsed))
    raise SystemExit(_parent(parsed))
