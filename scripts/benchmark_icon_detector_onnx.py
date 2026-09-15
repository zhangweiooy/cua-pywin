"""Compare the current PyTorch detector with the PyTorch-free ONNX PoC."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from scripts.benchmark_icon_semantics import _MemorySampler, _private_bytes


def _worker(args: argparse.Namespace) -> int:
    import psutil

    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    images = [
        Image.open(args.manifest.parent / row["path"]).convert("RGB") for row in rows
    ]
    process = psutil.Process()
    with _MemorySampler(process) as sampler:
        rss_before = int(process.memory_info().rss)
        if args.backend == "torch":
            import torch
            import ultralytics  # noqa: F401

            from scripts.poc_torch_icon_detector import TorchIconDetector

            torch.set_num_threads(args.threads)
            detector: Any = TorchIconDetector(
                args.torch_model,
                compute_threads=args.threads,
            )
        else:
            from scripts.poc_yolo_onnx import OnnxIconDetector

            detector = OnnxIconDetector(
                args.onnx_model,
                intra_op_num_threads=args.threads,
            )
        started = time.perf_counter()
        detector.load_model()
        load_ms = (time.perf_counter() - started) * 1000
        if args.backend == "torch":
            # Ultralytics may reset Torch's pool while constructing the predictor.
            torch.set_num_threads(args.threads)

        started = time.perf_counter()
        first_prediction = detector.detect_icons(
            images[0],
            box_threshold=0.05,
            iou_threshold=0.1,
        )
        first_inference_ms = (time.perf_counter() - started) * 1000
        if args.backend == "torch":
            effective_compute_threads = torch.get_num_threads()
        else:
            effective_compute_threads = args.threads
            forbidden_modules = sorted(
                name
                for name in sys.modules
                if name.split(".", 1)[0] in {"torch", "torchvision", "ultralytics"}
            )
            forbidden_dlls = sorted(
                item.path
                for item in process.memory_maps(grouped=True)
                if any(
                    value in item.path.casefold()
                    for value in ("torch_cpu.dll", "torch_python.dll", "torchvision")
                )
            )
            if forbidden_modules or forbidden_dlls:
                raise RuntimeError(
                    "ONNX worker loaded forbidden PyTorch runtime: "
                    f"modules={forbidden_modules}, dlls={forbidden_dlls}"
                )

        predictions: list[list[dict[str, Any]]] = []
        for index, image in enumerate(images):
            predictions.append(
                first_prediction
                if index == 0
                else detector.detect_icons(
                    image,
                    box_threshold=0.05,
                    iou_threshold=0.1,
                )
            )

        repeated: list[str] = []
        stable_latencies: list[float] = []
        for _ in range(args.repeat_count):
            started = time.perf_counter()
            result = detector.detect_icons(
                images[0],
                box_threshold=0.05,
                iou_threshold=0.1,
            )
            stable_latencies.append((time.perf_counter() - started) * 1000)
            repeated.append(json.dumps(result, sort_keys=True))
        rss_after = int(process.memory_info().rss)
        private_after = _private_bytes(process)

    print(
        json.dumps(
            {
                "backend": args.backend,
                "providers": detector.model.get_providers()
                if args.backend == "onnx"
                else ["PyTorch CPU"],
                "load_ms": load_ms,
                "first_inference_ms": first_inference_ms,
                "configured_compute_threads": args.threads,
                "effective_compute_threads": effective_compute_threads,
                "process_threads_after": process.num_threads(),
                "stable_latencies_ms": stable_latencies,
                "stable_latency_p50_ms": statistics.median(stable_latencies),
                "stable_latency_p95_ms": sorted(stable_latencies)[
                    max(0, int(len(stable_latencies) * 0.95) - 1)
                ],
                "repeat_count": args.repeat_count,
                "repeat_stable": len(set(repeated)) == 1,
                "forbidden_torch_modules": forbidden_modules
                if args.backend == "onnx"
                else None,
                "forbidden_torch_dlls": forbidden_dlls
                if args.backend == "onnx"
                else None,
                "rss_before_bytes": rss_before,
                "rss_after_bytes": rss_after,
                "private_after_bytes": private_after,
                "peak_rss_bytes": sampler.peak_rss,
                "peak_private_bytes": sampler.peak_private,
                "predictions": predictions,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _iou(first: list[float], second: list[float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(
        0.0, second[3] - second[1]
    )
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


def _match_detections(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
) -> list[tuple[int, int, float]]:
    pairs = sorted(
        (
            (_iou(first["bbox"], second["bbox"]), first_index, second_index)
            for first_index, first in enumerate(baseline)
            for second_index, second in enumerate(candidate)
        ),
        key=lambda value: (-value[0], value[1], value[2]),
    )
    used_baseline: set[int] = set()
    used_candidate: set[int] = set()
    matched: list[tuple[int, int, float]] = []
    for overlap, baseline_index, candidate_index in pairs:
        if overlap < 0.5:
            break
        if baseline_index in used_baseline or candidate_index in used_candidate:
            continue
        used_baseline.add(baseline_index)
        used_candidate.add(candidate_index)
        matched.append((baseline_index, candidate_index, overlap))
    return sorted(matched)


def _crop_hash(image: Image.Image, box: list[float]) -> str:
    width, height = image.size
    pixels = (
        round(box[0] * width),
        round(box[1] * height),
        round(box[2] * width),
        round(box[3] * height),
    )
    return hashlib.sha256(image.crop(pixels).tobytes()).hexdigest()


def _high_frequency_recall(
    detections: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    image_size: tuple[int, int],
) -> float | None:
    if not annotations:
        return None
    width, height = image_size
    hits = 0
    for annotation in annotations:
        x1, y1, x2, y2 = annotation["box"]
        normalized = [x1 / width, y1 / height, x2 / width, y2 / height]
        if any(_iou(normalized, detection["bbox"]) >= 0.3 for detection in detections):
            hits += 1
    return hits / len(annotations)


def _compare(
    rows: list[dict[str, Any]],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    manifest_dir: Path,
) -> dict[str, Any]:
    matched_count = 0
    denominator = 0
    overlaps: list[float] = []
    confidence_errors: list[float] = []
    added = 0
    missed = 0
    order_equal = True
    equal_crops = 0
    baseline_recall: list[float] = []
    candidate_recall: list[float] = []
    per_image: list[dict[str, Any]] = []
    for row, baseline_items, candidate_items in zip(
        rows, baseline["predictions"], candidate["predictions"]
    ):
        image = Image.open(manifest_dir / row["path"]).convert("RGB")
        matches = _match_detections(baseline_items, candidate_items)
        matched_count += len(matches)
        denominator += max(len(baseline_items), len(candidate_items))
        added += len(candidate_items) - len(matches)
        missed += len(baseline_items) - len(matches)
        overlaps.extend(value[2] for value in matches)
        confidence_errors.extend(
            abs(
                baseline_items[baseline_index]["confidence"]
                - candidate_items[candidate_index]["confidence"]
            )
            for baseline_index, candidate_index, _ in matches
        )
        order_equal &= all(
            baseline_index == candidate_index
            for baseline_index, candidate_index, _ in matches
        )
        equal_crops += sum(
            _crop_hash(image, baseline_items[baseline_index]["bbox"])
            == _crop_hash(image, candidate_items[candidate_index]["bbox"])
            for baseline_index, candidate_index, _ in matches
        )
        annotations = row.get("high_frequency", [])
        base_recall = _high_frequency_recall(
            baseline_items, annotations, image.size
        )
        onnx_recall = _high_frequency_recall(
            candidate_items, annotations, image.size
        )
        if base_recall is not None and onnx_recall is not None:
            baseline_recall.append(base_recall)
            candidate_recall.append(onnx_recall)
        per_image.append(
            {
                "path": row["path"],
                "baseline_count": len(baseline_items),
                "onnx_count": len(candidate_items),
                "matched": len(matches),
                "min_iou": min((value[2] for value in matches), default=None),
                "max_confidence_error": max(
                    (
                        abs(
                            baseline_items[first]["confidence"]
                            - candidate_items[second]["confidence"]
                        )
                        for first, second, _ in matches
                    ),
                    default=None,
                ),
                "baseline_high_frequency_recall": base_recall,
                "onnx_high_frequency_recall": onnx_recall,
            }
        )
    box_match_rate = matched_count / denominator if denominator else 1.0
    baseline_hf = statistics.mean(baseline_recall) if baseline_recall else None
    candidate_hf = statistics.mean(candidate_recall) if candidate_recall else None
    metrics = {
        "box_match_rate": box_match_rate,
        "matched_boxes": matched_count,
        "minimum_matched_iou": min(overlaps, default=None),
        "mean_matched_iou": statistics.mean(overlaps) if overlaps else None,
        "maximum_confidence_error": max(confidence_errors, default=None),
        "new_false_positives": added,
        "missed_baseline_boxes": missed,
        "nms_order_equal": order_equal,
        "identical_caption_crops": equal_crops,
        "baseline_high_frequency_recall": baseline_hf,
        "onnx_high_frequency_recall": candidate_hf,
        "per_image": per_image,
    }
    metrics["quality_gate_passed"] = bool(
        box_match_rate >= 0.99
        and (metrics["minimum_matched_iou"] or 0.0) >= 0.98
        and (metrics["maximum_confidence_error"] or 0.0) <= 0.01
        and added == 0
        and missed == 0
        and order_equal
        and equal_crops == matched_count
        and baseline_hf == candidate_hf
        and baseline["repeat_stable"]
        and candidate["repeat_stable"]
        and not candidate["forbidden_torch_modules"]
        and not candidate["forbidden_torch_dlls"]
    )
    return metrics


def _compare_merge_nms(
    rows: list[dict[str, Any]],
    baseline_predictions: list[list[dict[str, Any]]],
    manifest_dir: Path,
) -> dict[str, Any]:
    import torch
    import torchvision

    from som.detect import _arbitrate_ocr_icon_collisions
    from som.models import BoundingBox, IconElement, TextElement, UIElement
    from som.nms import nms
    from som.ocr import OCRProcessor

    ocr = OCRProcessor(languages=["ch_sim", "en"])
    per_image: list[dict[str, Any]] = []
    for row, detections in zip(rows, baseline_predictions):
        image = Image.open(manifest_dir / row["path"]).convert("RGB")
        icons: list[UIElement] = [
            IconElement(
                id=index + 1,
                bbox=BoundingBox(
                    x1=item["bbox"][0],
                    y1=item["bbox"][1],
                    x2=item["bbox"][2],
                    y2=item["bbox"][3],
                ),
                confidence=item["confidence"],
                scale=item.get("scale"),
            )
            for index, item in enumerate(detections)
        ]
        text_detections = ocr.detect_text(image=image, confidence_threshold=0.5)
        texts: list[UIElement] = [
            TextElement(
                id=len(icons) + index + 1,
                bbox=BoundingBox(
                    x1=item["bbox"][0],
                    y1=item["bbox"][1],
                    x2=item["bbox"][2],
                    y2=item["bbox"][3],
                ),
                content=item["content"],
                confidence=item["confidence"],
            )
            for index, item in enumerate(text_detections or [])
        ]
        retained_icons, protected_ids, diagnostics = _arbitrate_ocr_icon_collisions(
            icons, texts
        )
        all_elements = retained_icons + texts
        boxes = np.asarray(
            [element.bbox.coordinates for element in all_elements], dtype=np.float32
        )
        scores = np.asarray(
            [element.confidence for element in all_elements], dtype=np.float32
        )
        torch_kept = torchvision.ops.nms(
            torch.from_numpy(boxes), torch.from_numpy(scores), 0.1
        ).tolist()
        numpy_kept = nms(boxes, scores, 0.1)
        protected = {
            index
            for index, element in enumerate(all_elements)
            if id(element) in protected_ids
        }
        torch_positions = [
            index
            for index in range(len(all_elements))
            if index in set(torch_kept) | protected
        ]
        numpy_positions = [
            index
            for index in range(len(all_elements))
            if index in set(numpy_kept) | protected
        ]
        per_image.append(
            {
                "path": row["path"],
                "icon_count": len(retained_icons),
                "ocr_count": len(texts),
                "protected_count": len(protected),
                "torch_positions": torch_positions,
                "numpy_positions": numpy_positions,
                "equal": torch_positions == numpy_positions,
                "collision_diagnostics": diagnostics,
            }
        )
    return {
        "all_equal": all(item["equal"] for item in per_image),
        "per_image": per_image,
    }


def _parent(args: argparse.Namespace) -> int:
    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    results: dict[str, Any] = {}
    for backend in ("torch", "onnx"):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--backend",
            backend,
            "--manifest",
            str(args.manifest.resolve()),
            "--torch-model",
            str(args.torch_model.resolve()),
            "--onnx-model",
            str(args.onnx_model.resolve()),
            "--repeat-count",
            str(args.repeat_count),
            "--threads",
            str(args.threads),
        ]
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            print(completed.stdout, file=sys.stderr)
            print(completed.stderr, file=sys.stderr)
            return completed.returncode
        results[backend] = json.loads(completed.stdout.strip().splitlines()[-1])
    comparison = _compare(
        rows,
        results["torch"],
        results["onnx"],
        args.manifest.parent,
    )
    merge_nms = _compare_merge_nms(
        rows,
        results["torch"]["predictions"],
        args.manifest.parent,
    )
    comparison["quality_gate_passed"] &= merge_nms["all_equal"]
    coverage = {
        key: sorted({str(row.get(key, "unknown")) for row in rows})
        for key in ("app", "locale", "theme", "scale")
    }
    report = {
        "manifest": str(args.manifest.resolve()),
        "coverage": coverage,
        "results": results,
        "comparison": comparison,
        "merge_nms": merge_nms,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if comparison["quality_gate_passed"] else 2


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--backend", choices=("torch", "onnx"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--torch-model",
        type=Path,
        default=Path(
            "artifacts/yolo-onnx-poc/export-reviewed/source/model.pt"
        ),
    )
    parser.add_argument(
        "--onnx-model",
        type=Path,
        default=Path("som/weights/icon_detect/model.onnx"),
    )
    parser.add_argument("--repeat-count", type=int, default=100)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/yolo-onnx-poc/benchmark.json"),
    )
    return parser.parse_args()


if __name__ == "__main__":
    parsed = _parse_args()
    if parsed.worker:
        if parsed.backend is None:
            raise SystemExit("--backend is required in worker mode")
        raise SystemExit(_worker(parsed))
    raise SystemExit(_parent(parsed))
