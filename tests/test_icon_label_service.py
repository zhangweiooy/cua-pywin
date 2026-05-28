from __future__ import annotations

from PIL import Image

from computer_server.services.icon_label_service import (
    BUNDLED_TINYCLIP_MODEL_DIR,
    IconLabelCandidate,
    IconLabelService,
    default_model_name_or_path,
)


def test_icon_label_service_batches_images_once():
    import torch

    class FakeProcessor:
        def __init__(self):
            self.image_call_count = 0
            self.image_batch_size = 0

        def __call__(self, images=None, return_tensors="pt", **kwargs):
            assert images is not None
            self.image_call_count += 1
            self.image_batch_size = len(images)
            return {"pixel_values": torch.ones(len(images), 3, 2, 2)}

    class FakeModel:
        logit_scale = torch.tensor(1.0)

        def get_image_features(self, pixel_values):
            return torch.tensor([
                [1.0, 0.0],
                [0.0, 1.0],
                [1.0, 0.0],
            ])

    candidates = (
        IconLabelCandidate("send button", "send prompt"),
        IconLabelCandidate("settings icon", "settings prompt"),
    )
    processor = FakeProcessor()
    service = IconLabelService(
        candidates=candidates,
        min_score=0.0,
        min_margin=0.0,
        enabled=True,
    )
    service._state = "ready"
    service._processor = processor
    service._model = FakeModel()
    service._torch = torch
    service._device = torch.device("cpu")
    service._text_features = torch.tensor([
        [1.0, 0.0],
        [0.0, 1.0],
    ])

    images = [Image.new("RGB", (4, 4), "white") for _ in range(3)]

    labels = service.label_icons(images)

    assert processor.image_call_count == 1
    assert processor.image_batch_size == 3
    assert [label.label for label in labels if label is not None] == [
        "send button",
        "settings icon",
        "send button",
    ]


def test_default_model_path_uses_bundled_model(monkeypatch):
    monkeypatch.delenv("CUA_TINYCLIP_MODEL", raising=False)

    assert default_model_name_or_path() == str(BUNDLED_TINYCLIP_MODEL_DIR)
