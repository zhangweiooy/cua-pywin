"""Benchmark the three icon-semantic candidates in isolated processes.

The fixture is deliberately small and deterministic.  It measures the actual
service boundary, not only raw ONNX calls, and never loads two semantic models
in the same worker process.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw

CORE_LABELS = (
    "search",
    "settings",
    "arrow_left",
    "close",
    "overflow_menu",
    "reply_or_send",
    "upload",
    "save_or_download",
    "menu",
    "file",
    "profile_avatar_or_user",
)
HIGH_FREQUENCY = {
    "search",
    "settings",
    "arrow_left",
    "close",
    "overflow_menu",
    "reply_or_send",
    "upload",
    "save_or_download",
}


def _draw_icon(label: str, *, theme: str, scale: float, seed: int) -> Image.Image:
    size = 64
    background = (30, 34, 42) if theme == "dark" else (247, 248, 250)
    foreground = (235, 238, 244) if theme == "dark" else (35, 39, 47)
    canvas = Image.new("RGB", (size * 4, size * 4), background)
    draw = ImageDraw.Draw(canvas)
    s = 4.0 * scale

    def xy(value: float) -> int:
        return int(round(value * 4))

    def line(points: Iterable[tuple[float, float]], width: float = 3.0) -> None:
        draw.line(
            [(xy(x), xy(y)) for x, y in points],
            fill=foreground,
            width=max(1, int(width * s)),
            joint="curve",
        )

    def ellipse(box: tuple[float, float, float, float], width: float = 3.0) -> None:
        draw.ellipse(
            tuple(xy(value) for value in box),
            outline=foreground,
            width=max(1, int(width * s)),
        )

    if label == "search":
        ellipse((17, 17, 41, 41), 3)
        line(((38, 38), (50, 50)), 4)
    elif label == "settings":
        ellipse((23, 23, 41, 41), 3)
        ellipse((29, 29, 35, 35), 3)
        for angle in range(0, 360, 45):
            import math

            radians = math.radians(angle)
            line(
                (
                    (32 + math.cos(radians) * 17, 32 + math.sin(radians) * 17),
                    (32 + math.cos(radians) * 23, 32 + math.sin(radians) * 23),
                ),
                4,
            )
    elif label == "arrow_left":
        line(((48, 32), (18, 32), (29, 21)), 4)
        line(((18, 32), (29, 43)), 4)
    elif label == "close":
        line(((19, 19), (45, 45)), 4)
        line(((45, 19), (19, 45)), 4)
    elif label == "overflow_menu":
        for x in (22, 32, 42):
            draw.ellipse((xy(x - 3), xy(29), xy(x + 3), xy(35)), fill=foreground)
    elif label == "menu":
        for y in (22, 32, 42):
            line(((18, y), (46, y)), 4)
    elif label == "reply_or_send":
        draw.polygon(
            [(xy(16), xy(31)), (xy(48), xy(16)), (xy(37), xy(48)), (xy(30), xy(35))],
            outline=foreground,
        )
        line(((16, 31), (30, 35), (48, 16)), 3)
    elif label == "upload":
        line(((32, 45), (32, 18)), 4)
        line(((32, 18), (21, 29)), 4)
        line(((32, 18), (43, 29)), 4)
        line(((18, 45), (46, 45)), 4)
    elif label == "save_or_download":
        line(((32, 18), (32, 45)), 4)
        line(((32, 45), (21, 34)), 4)
        line(((32, 45), (43, 34)), 4)
        line(((18, 18), (46, 18)), 4)
    elif label == "file":
        draw.polygon(
            [
                (xy(21), xy(14)),
                (xy(38), xy(14)),
                (xy(45), xy(21)),
                (xy(45), xy(50)),
                (xy(21), xy(50)),
            ],
            outline=foreground,
        )
        line(((38, 14), (38, 22), (45, 22)), 3)
        line(((26, 32), (40, 32)), 3)
    elif label == "profile_avatar_or_user":
        ellipse((23, 14, 41, 32), 3)
        draw.arc(
            (xy(15), xy(28), xy(49), xy(56)),
            180,
            360,
            fill=foreground,
            width=max(1, int(3 * s)),
        )
    elif label == "decorative":
        for y in range(size * 4):
            value = (y * 13 + seed * 17) % 255
            draw.line(
                (0, y, size * 4, y), fill=(value, (value * 3) % 255, (value * 7) % 255)
            )
        for x in range(0, size * 4, 19):
            draw.ellipse(
                (
                    x,
                    (seed * 11 + x) % (size * 3),
                    x + 31,
                    (seed * 11 + x) % (size * 3) + 31,
                ),
                fill=(255, 255, 255),
            )

    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def _make_fixture(root: Path) -> list[dict[str, Any]]:
    image_dir = root / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    index = 0
    for locale in ("en", "zh"):
        for theme in ("light", "dark"):
            for scale in (0.75, 1.0, 1.5):
                for label in CORE_LABELS:
                    filename = f"{index:04d}_{label}_{locale}_{theme}_{scale:.2f}.png"
                    _draw_icon(label, theme=theme, scale=scale, seed=index).save(
                        image_dir / filename
                    )
                    rows.append(
                        {
                            "path": str(Path("images") / filename),
                            "expected": label.replace("_", " "),
                            "icon": True,
                            "high_frequency": label in HIGH_FREQUENCY,
                            "locale": locale,
                            "theme": theme,
                            "scale": scale,
                            "source": "synthetic",
                        }
                    )
                    index += 1
    for theme in ("light", "dark"):
        for seed in range(4):
            filename = f"{index:04d}_decorative_{theme}_{seed}.png"
            _draw_icon("decorative", theme=theme, scale=1.0, seed=seed).save(
                image_dir / filename
            )
            rows.append(
                {
                    "path": str(Path("images") / filename),
                    "expected": "unknown",
                    "icon": False,
                    "high_frequency": False,
                    "locale": "n/a",
                    "theme": theme,
                    "scale": 1.0,
                    "source": "synthetic",
                }
            )
            index += 1
    screenshot_path = Path("artifacts/wechat-som-20260801/wechat-window.png")
    if screenshot_path.is_file():
        screenshot = Image.open(screenshot_path).convert("RGB")
        real_crops = (
            ("search", (108, 61, 145, 100)),
            ("minimize", (1098, 4, 1138, 42)),
            ("maximize", (1162, 4, 1202, 42)),
            ("close", (1225, 4, 1265, 42)),
            ("overflow_menu", (1210, 60, 1250, 101)),
            ("call_or_mobile", (1158, 60, 1200, 101)),
            ("folder", (565, 852, 606, 902)),
            ("cut", (620, 852, 660, 902)),
            ("microphone", (745, 852, 785, 902)),
            ("menu", (24, 853, 67, 906)),
            ("volume", (1081, 852, 1123, 901)),
        )
        for label, box in real_crops:
            filename = f"{index:04d}_wechat_{label}.png"
            screenshot.crop(box).resize((64, 64), Image.Resampling.LANCZOS).save(
                image_dir / filename
            )
            rows.append(
                {
                    "path": str(Path("images") / filename),
                    "expected": label.replace("_", " "),
                    "icon": True,
                    "high_frequency": label in HIGH_FREQUENCY
                    or label in {"search", "close"},
                    "locale": "zh",
                    "theme": "light",
                    "scale": "native",
                    "source": "wechat",
                }
            )
            index += 1
    (root / "manifest.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return rows


def _private_bytes(process: Any) -> int:
    info = process.memory_full_info()
    return int(
        getattr(info, "private", getattr(info, "uss", process.memory_info().rss))
    )


class _MemorySampler:
    def __init__(self, process: Any) -> None:
        self.process = process
        self.stop_event = threading.Event()
        self.peak_rss = 0
        self.peak_private = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.peak_rss = max(self.peak_rss, int(self.process.memory_info().rss))
                self.peak_private = max(self.peak_private, _private_bytes(self.process))
            except Exception:
                pass
            self.stop_event.wait(0.02)

    def __enter__(self) -> "_MemorySampler":
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1.0)


def _match(expected: str, predicted: str | None) -> bool:
    if expected == "unknown":
        return predicted is None
    if predicted is None:
        return False
    normalized = predicted.casefold().replace("_", " ")
    aliases = {
        "arrow left": ("arrow left", "back", "left arrow"),
        "reply or send": ("reply or send", "send", "paper plane"),
        "save or download": ("save or download", "download", "save"),
        "profile avatar or user": (
            "profile avatar or user",
            "profile",
            "user",
            "avatar",
        ),
        "overflow menu": ("overflow menu", "more", "ellipsis"),
    }
    return any(alias in normalized for alias in aliases.get(expected, (expected,)))


def _evaluate(rows: list[dict[str, Any]], captions: list[Any]) -> dict[str, Any]:
    if len(rows) != len(captions):
        raise ValueError("prediction count does not match fixture")
    top1: list[bool] = []
    unknown: list[bool] = []
    high_frequency: list[bool] = []
    by_dimension: dict[str, dict[str, list[bool]]] = {}
    top3_hits = 0
    top3_total = 0
    for row, caption in zip(rows, captions):
        predicted = caption.label if caption is not None else None
        good = _match(row["expected"], predicted)
        top1.append(good)
        if not row["icon"]:
            unknown.append(good)
        elif row["high_frequency"]:
            high_frequency.append(good)
        for key, value in (
            ("locale", row["locale"]),
            ("theme", row["theme"]),
            ("scale", str(row["scale"])),
            ("source", row.get("source", "unknown")),
        ):
            bucket = by_dimension.setdefault(key, {}).setdefault(value, [])
            bucket.append(good)
        if caption is not None and caption.candidates:
            candidates = [label for label, _ in caption.candidates]
            top3_total += 1
            if any(_match(row["expected"], candidate) for candidate in candidates[:3]):
                top3_hits += 1
    return {
        "top1_accuracy": sum(top1) / len(top1) if top1 else None,
        "top3_recall": top3_hits / top3_total if top3_total else None,
        "top3_samples": top3_total,
        "unknown_accuracy_non_icons": sum(unknown) / len(unknown) if unknown else None,
        "non_icon_false_positive_rate": 1.0 - (sum(unknown) / len(unknown))
        if unknown
        else None,
        "high_frequency_top1_accuracy": sum(high_frequency) / len(high_frequency)
        if high_frequency
        else None,
        "by_dimension": {
            key: {value: sum(values) / len(values) for value, values in buckets.items()}
            for key, buckets in by_dimension.items()
        },
    }


def _freeform_top3(service: Any, images: list[Image.Image]) -> list[tuple[str, ...]]:
    """Run benchmark-only beam search for the generative backends."""
    if service.backend == "florence2":
        torch = service._torch
        processor = service._processor
        inputs = processor(
            images=images,
            text=["<CAPTION>"] * len(images),
            return_tensors="pt",
            do_resize=False,
        )
        inputs = service._move_inputs(inputs, service._device, service._dtype)
        with torch.inference_mode():
            generated = service._model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=service.max_new_tokens,
                num_beams=3,
                num_return_sequences=3,
                do_sample=False,
                early_stopping=False,
            )
        decoded = processor.batch_decode(generated, skip_special_tokens=True)
        return [
            tuple(str(item) for item in decoded[start : start + 3])
            for start in range(0, len(decoded), 3)
        ]
    if service.backend != "florence2_int8":
        return []

    processor = service._processor
    inputs = processor(
        images=images,
        text=["<CAPTION>"] * len(images),
        return_tensors="np",
        do_resize=False,
    )
    input_ids = service._as_numpy(inputs["input_ids"], np.int64)
    pixel_values = service._as_numpy(inputs["pixel_values"], np.float32)
    beam_width = 3
    batch_size = len(images)
    sequences = np.full(
        (batch_size, beam_width, 1),
        service._decoder_start_token_id,
        dtype=np.int64,
    )
    scores = np.full((batch_size, beam_width), -np.inf, dtype=np.float32)
    scores[:, 0] = 0.0
    finished = np.zeros((batch_size, beam_width), dtype=bool)
    for _ in range(service.max_new_tokens):
        flat_sequences = sequences.reshape(batch_size * beam_width, -1)
        logits = np.asarray(
            service._session.run(
                [service._session_output_name],
                {
                    "input_ids": np.repeat(input_ids, beam_width, axis=0),
                    "pixel_values": np.repeat(pixel_values, beam_width, axis=0),
                    "decoder_input_ids": flat_sequences,
                },
            )[0],
            dtype=np.float32,
        )
        if logits.ndim == 3:
            logits = logits[:, -1, :]
        logits -= logits.max(axis=-1, keepdims=True)
        log_probabilities = logits - np.log(np.exp(logits).sum(axis=-1, keepdims=True))
        log_probabilities = log_probabilities.reshape(batch_size, beam_width, -1)
        log_probabilities[finished] = -np.inf
        log_probabilities[finished, service._eos_token_id] = 0.0
        candidate_scores = scores[:, :, None] + log_probabilities
        flat_scores = candidate_scores.reshape(batch_size, -1)
        selected = np.argpartition(flat_scores, -beam_width, axis=1)[:, -beam_width:]
        selected_scores = np.take_along_axis(flat_scores, selected, axis=1)
        order = np.argsort(-selected_scores, axis=1)
        selected = np.take_along_axis(selected, order, axis=1)
        scores = np.take_along_axis(selected_scores, order, axis=1)
        previous = selected // log_probabilities.shape[-1]
        tokens = selected % log_probabilities.shape[-1]
        rows = np.arange(batch_size)[:, None]
        sequences = np.concatenate(
            (
                sequences[rows, previous],
                tokens[:, :, None].astype(np.int64),
            ),
            axis=2,
        )
        finished = finished[rows, previous] | (tokens == service._eos_token_id)
        if bool(finished.all()):
            break
    decoded = processor.batch_decode(
        sequences[:, :, 1:].reshape(batch_size * beam_width, -1),
        skip_special_tokens=True,
    )
    return [
        tuple(str(item) for item in decoded[start : start + beam_width])
        for start in range(0, len(decoded), beam_width)
    ]


def _worker(args: argparse.Namespace) -> int:
    import psutil

    os.environ["CUA_FLORENCE2_DEVICE"] = "cpu"

    from scripts.poc_icon_backends import PocIconCaptionService as IconCaptionService

    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    images = [
        Image.open(args.manifest.parent / row["path"]).convert("RGB") for row in rows
    ]
    process = psutil.Process()
    service_args: dict[str, Any] = {
        "backend": args.backend,
        "enabled": True,
        "batch_size": 64,
        "max_new_tokens": 20,
    }
    if args.backend == "quasivision":
        service_args.update(
            model_name_or_path=str(args.quasivision_model),
        )
        os.environ["CUA_QUASIVISION_LABELS"] = str(args.quasivision_labels)
    elif args.backend == "florence2_int8":
        service_args.update(
            model_name_or_path=str(args.florence_int8_model),
            processor_name_or_path=str(args.florence_model_dir),
        )
    else:
        service_args.update(
            model_name_or_path=str(args.florence_model_dir),
            processor_name_or_path=str(args.florence_model_dir),
        )
    service = IconCaptionService(**service_args)
    top3: list[tuple[str, ...]] = []
    with _MemorySampler(process) as sampler:
        rss_before = int(process.memory_info().rss)
        load_started = time.perf_counter()
        ready = service.ensure_ready()
        load_ms = (time.perf_counter() - load_started) * 1000
        if not ready:
            print(
                json.dumps(
                    {"backend": args.backend, "ready": False, "status": service.status}
                )
            )
            return 1
        first_started = time.perf_counter()
        first = service.caption_icons(images[:1])
        first_ms = (time.perf_counter() - first_started) * 1000
        stable_times: list[float] = []
        for _ in range(10):
            started = time.perf_counter()
            service.caption_icons(images[:1])
            stable_times.append((time.perf_counter() - started) * 1000)
        batch_started = time.perf_counter()
        service.caption_icons(images[: min(16, len(images))])
        batch_ms = (time.perf_counter() - batch_started) * 1000
        predictions = service.caption_icons(images)
        repeated: list[str | None] = []
        for _ in range(args.repeat_count):
            repeated_caption = service.caption_icons(images[:1])[0]
            repeated.append(repeated_caption.label if repeated_caption else None)
        rss_after = int(process.memory_info().rss)
        private_after = _private_bytes(process)
    top3 = _freeform_top3(service, images)
    if top3:
        predictions = [
            replace(
                caption,
                candidates=tuple((candidate, 0.0) for candidate in candidates),
            )
            for caption, candidates in zip(predictions, top3)
        ]
    result = {
        "backend": args.backend,
        "ready": True,
        "status": service.status,
        "dataset_samples": len(rows),
        "load_ms": load_ms,
        "first_inference_ms": first_ms,
        "stable_single_p50_ms": statistics.median(stable_times),
        "stable_single_p95_ms": sorted(stable_times)[
            max(0, int(len(stable_times) * 0.95) - 1)
        ],
        "batch_size": min(16, len(images)),
        "batch_inference_ms": batch_ms,
        "repeat_count": args.repeat_count,
        "repeated_unique_labels": sorted(set(repeated), key=lambda value: str(value)),
        "repeated_stable": len(set(repeated)) <= 1,
        "rss_before_bytes": rss_before,
        "rss_after_bytes": rss_after,
        "private_after_bytes": private_after,
        "peak_rss_bytes": sampler.peak_rss,
        "peak_private_bytes": sampler.peak_private,
        "accuracy": _evaluate(rows, predictions),
        "first_caption": first[0].label if first and first[0] else None,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _run_parent(args: argparse.Namespace) -> int:
    rows = _make_fixture(args.output_dir / "fixture")
    del rows
    results: dict[str, Any] = {}
    for backend in ("florence2", "quasivision", "florence2_int8"):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--backend",
            backend,
            "--manifest",
            str((args.output_dir / "fixture" / "manifest.json").resolve()),
            "--florence-model-dir",
            str(args.florence_model_dir.resolve()),
            "--quasivision-model",
            str(args.quasivision_model.resolve()),
            "--quasivision-labels",
            str(args.quasivision_labels.resolve()),
            "--florence-int8-model",
            str(args.florence_int8_model.resolve()),
            "--repeat-count",
            str(args.repeat_count),
        ]
        env = dict(os.environ)
        env["TRANSFORMERS_OFFLINE"] = "1"
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            print(
                f"[{backend}] worker failed\n{completed.stdout}\n{completed.stderr}",
                file=sys.stderr,
            )
            results[backend] = {
                "error": "worker_failed",
                "returncode": completed.returncode,
            }
            continue
        try:
            results[backend] = json.loads(completed.stdout.strip().splitlines()[-1])
        except Exception as exc:
            print(
                f"[{backend}] invalid worker output: {exc}\n{completed.stdout}",
                file=sys.stderr,
            )
            results[backend] = {"error": "invalid_worker_output"}
    report = {
        "fixture": str((args.output_dir / "fixture").resolve()),
        "models": {
            "florence2": str(args.florence_model_dir.resolve()),
            "quasivision": str(args.quasivision_model.resolve()),
            "florence2_int8": str(args.florence_int8_model.resolve()),
        },
        "results": results,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all("error" not in item for item in results.values()) else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument(
        "--backend",
        choices=("florence2", "quasivision", "florence2_int8"),
        default="florence2",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("artifacts/icon-semantic-poc/benchmark")
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--florence-model-dir",
        type=Path,
        default=Path("computer_server/models/omniparser_v2/icon_caption"),
    )
    parser.add_argument(
        "--quasivision-model",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/quasivision/icon_classifier.onnx"),
    )
    parser.add_argument(
        "--quasivision-labels",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/quasivision/labels.json"),
    )
    parser.add_argument(
        "--florence-int8-model",
        type=Path,
        default=Path("artifacts/icon-semantic-poc/florence2_int8/model.int8.onnx"),
    )
    parser.add_argument("--repeat-count", type=int, default=100)
    return parser.parse_args(argv)


if __name__ == "__main__":
    arguments = _parse_args()
    if arguments.worker:
        if arguments.manifest is None:
            raise SystemExit("--manifest is required in worker mode")
        raise SystemExit(_worker(arguments))
    raise SystemExit(_run_parent(arguments))
