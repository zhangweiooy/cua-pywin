"""Export the bundled icon detector to a controlled ONNX PoC artifact."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tensor_metadata(value: Any) -> dict[str, Any]:
    return {
        "name": value.name,
        "type": value.type,
        "shape": list(value.shape),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        type=Path,
        default=Path("som/weights/icon_detect/model.pt"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/yolo-onnx-poc/export"),
    )
    args = parser.parse_args()

    from ultralytics import YOLO, __version__ as ultralytics_version

    import onnx
    import onnxruntime as ort
    import torch

    source_model = args.model.resolve()
    output_dir = args.output_dir.resolve()
    controlled_source = output_dir / "source"
    controlled_source.mkdir(parents=True, exist_ok=True)
    copied_model = controlled_source / "model.pt"
    shutil.copy2(source_model, copied_model)

    model = YOLO(str(copied_model))
    stride = [float(value) for value in model.model.stride.tolist()]
    names = {str(key): value for key, value in model.names.items()}
    exported = Path(
        model.export(
            format="onnx",
            imgsz=1280,
            dynamic=True,
            simplify=True,
            opset=17,
            nms=False,
            batch=1,
            device="cpu",
        )
    ).resolve()
    output_model = output_dir / "model.onnx"
    if exported != output_model:
        shutil.move(str(exported), output_model)

    onnx_model = onnx.load(output_model)
    session = ort.InferenceSession(
        str(output_model), providers=["CPUExecutionProvider"]
    )
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": str(source_model),
            "sha256": _sha256(source_model),
            "bytes": source_model.stat().st_size,
            "task": model.task,
            "names": names,
            "stride": stride,
        },
        "export": {
            "ultralytics_version": ultralytics_version,
            "torch_version": torch.__version__,
            "onnx_version": onnx.__version__,
            "onnxslim_version": importlib.metadata.version("onnxslim"),
            "arguments": {
                "format": "onnx",
                "imgsz": 1280,
                "dynamic": True,
                "simplify": True,
                "opset": 17,
                "nms": False,
                "batch": 1,
                "device": "cpu",
            },
        },
        "onnx": {
            "path": str(output_model),
            "sha256": _sha256(output_model),
            "bytes": output_model.stat().st_size,
            "opsets": [
                {"domain": value.domain, "version": value.version}
                for value in onnx_model.opset_import
            ],
            "inputs": [_tensor_metadata(value) for value in session.get_inputs()],
            "outputs": [_tensor_metadata(value) for value in session.get_outputs()],
            "coordinate_format": "raw xywh plus one class score",
            "metadata": dict(session.get_modelmeta().custom_metadata_map),
        },
    }
    (output_dir / "export_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
