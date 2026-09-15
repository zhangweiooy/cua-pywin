from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from computer_server.services.community_florence2_service import (
    CommunityFlorence2Runner,
)


def fake_runner(tmp_path, max_tokens=4):
    runner = CommunityFlorence2Runner(tmp_path, tmp_path, max_new_tokens=max_tokens)
    runner._processor = SimpleNamespace(
        batch_decode=lambda ids, **kwargs: [row.tolist() for row in ids],
    )

    class Processor:
        def __call__(self, **kwargs):
            return {
                "input_ids": np.zeros((2, 1), np.int64),
                "pixel_values": np.zeros((2, 3, 64, 64), np.float32),
            }

        def batch_decode(self, ids, **kwargs):
            return [str(row.tolist()) for row in ids]

    runner._processor = Processor()

    def logits(tokens):
        value = np.zeros((2, 1, 6), np.float32)
        for row, token in enumerate(tokens):
            value[row, 0, token] = 10
        return value

    runner._sessions = {
        "vision": SimpleNamespace(run=lambda *_: [np.zeros((2, 1, 4))]),
        "embed": SimpleNamespace(run=lambda *_: [np.zeros((2, 1, 4))]),
        "encoder": SimpleNamespace(run=lambda *_: [np.zeros((2, 2, 4))]),
        "decoder": SimpleNamespace(
            run=lambda *_: [logits([2, 3])],
            get_outputs=lambda: [SimpleNamespace(name="logits")],
        ),
    }
    calls = []

    def step(*_):
        calls.append(True)
        return [logits([4, 2])]

    runner._sessions["decoder_with_past"] = SimpleNamespace(
        run=step, get_outputs=lambda: [SimpleNamespace(name="logits")]
    )
    return runner, calls


def test_finished_batch_rows_do_not_emit_more_tokens(tmp_path):
    runner, calls = fake_runner(tmp_path)
    assert runner.caption_images([Image.new("RGB", (64, 64))] * 2) == [
        "[2, 2]",
        "[3, 2]",
    ]
    assert len(calls) == 1


def test_token_limit_does_not_run_unused_decoder_step(tmp_path):
    runner, calls = fake_runner(tmp_path, max_tokens=1)
    assert runner.caption_images([Image.new("RGB", (64, 64))] * 2) == ["[2]", "[3]"]
    assert calls == []


def test_nan_logits_fail_instead_of_becoming_semantics(tmp_path):
    runner, _ = fake_runner(tmp_path)
    runner._sessions["decoder"].run = lambda *_: [np.full((2, 1, 6), np.nan)]
    with pytest.raises(ValueError, match="non-finite"):
        runner.caption_images([Image.new("RGB", (64, 64))] * 2)


def test_kv_cache_uses_output_names_and_preserves_encoder():
    previous = {"past_key_values.0.encoder.key": "encoder"}
    session = SimpleNamespace(
        get_outputs=lambda: [
            SimpleNamespace(name=n)
            for n in ("logits", "present.1.decoder.value", "present.0.decoder.key")
        ]
    )
    assert CommunityFlorence2Runner._step_past(
        previous, [None, "value", "key"], session
    ) == {
        **previous,
        "past_key_values.1.decoder.value": "value",
        "past_key_values.0.decoder.key": "key",
    }


def test_session_uses_bounded_cpu_threading(monkeypatch, tmp_path):
    import computer_server.services.community_florence2_service as service_module

    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"model")
    captured = {}

    class Options:
        graph_optimization_level = None
        intra_op_num_threads = None
        inter_op_num_threads = None
        enable_cpu_mem_arena = None
        enable_mem_pattern = None

    class FakeOrt:
        SessionOptions = Options
        GraphOptimizationLevel = SimpleNamespace(ORT_ENABLE_ALL="all")

        @staticmethod
        def InferenceSession(path, *, sess_options, providers):
            captured.update(
                path=path,
                options=sess_options,
                providers=providers,
            )
            return object()

    monkeypatch.setattr(service_module, "ort", FakeOrt)

    CommunityFlorence2Runner._session(model_path)

    assert captured["options"].graph_optimization_level == "all"
    assert captured["options"].intra_op_num_threads == 1
    assert captured["options"].inter_op_num_threads == 1
    assert captured["options"].enable_cpu_mem_arena is False
    assert captured["options"].enable_mem_pattern is True
    assert captured["providers"] == ["CPUExecutionProvider"]


def test_quantized_load_uses_int8_vision_and_fp32_embeddings(monkeypatch, tmp_path):
    source_dir = tmp_path / "source"
    pipeline_dir = tmp_path / "pipeline"
    (source_dir / "onnx").mkdir(parents=True)
    (pipeline_dir / "int8").mkdir(parents=True)
    (source_dir / "generation_config.json").write_text("{}", encoding="utf-8")
    paths = []

    class Session:
        def get_inputs(self):
            return [SimpleNamespace(name="inputs_embeds")]

        def get_outputs(self):
            return []

    monkeypatch.setattr(
        CommunityFlorence2Runner,
        "_load_processor",
        lambda _runner: object(),
    )

    def fake_session(path):
        paths.append(path)
        return Session()

    monkeypatch.setattr(CommunityFlorence2Runner, "_session", staticmethod(fake_session))
    runner = CommunityFlorence2Runner(source_dir, pipeline_dir, quantized=True)
    runner.load()

    assert paths == [
        pipeline_dir / "int8/vision_encoder.int8.onnx",
        source_dir / "onnx/embed_tokens.onnx",
        pipeline_dir / "int8/encoder_model.int8.onnx",
        pipeline_dir / "int8/decoder_model.int8.onnx",
        pipeline_dir / "int8/decoder_with_past_model.int8.onnx",
    ]


def test_benchmark_override_can_use_fp32_vision_with_int8_transformers(tmp_path):
    from scripts.benchmark_onnx_community_florence2 import (
        VisionBenchmarkRunner,
        _fixture_coverage,
    )

    source_dir = tmp_path / "source"
    pipeline_dir = tmp_path / "pipeline"
    runner = VisionBenchmarkRunner(
        source_dir,
        pipeline_dir,
        int8_vision=False,
    )

    assert runner.quantized is True
    assert runner._vision_graph_path(source_dir / "onnx") == (
        source_dir / "onnx/vision_encoder.onnx"
    )
    assert _fixture_coverage([{"source": "wechat"}]) == {
        "source_counts": {"wechat": 1},
        "required_release_source_minimums": {"oryx": 8, "wechat": 8},
        "missing_release_sources": ["oryx", "wechat"],
        "release_ready": False,
    }
    complete = [{"source": "wechat"}] * 8 + [{"source": "oryx"}] * 8
    assert _fixture_coverage(complete)["release_ready"] is True


def test_release_fixture_includes_both_curated_desktop_sources(tmp_path):
    from scripts.benchmark_icon_semantics import _make_fixture
    from scripts.benchmark_onnx_community_florence2 import _fixture_coverage

    rows = _make_fixture(tmp_path)
    coverage = _fixture_coverage(rows)

    assert coverage["source_counts"] == {
        "synthetic": 140,
        "wechat": 11,
        "oryx": 16,
    }
    assert coverage["release_ready"] is True
    assert all((tmp_path / row["path"]).is_file() for row in rows)


def test_greedy_logits_settings_match_transformers(tmp_path):
    import torch
    from transformers.generation.logits_process import (
        NoRepeatNGramLogitsProcessor,
        ForcedBOSTokenLogitsProcessor,
        ForcedEOSTokenLogitsProcessor,
    )

    runner = CommunityFlorence2Runner(tmp_path, tmp_path, max_new_tokens=8)
    runner._generation_config = {
        "forced_bos_token_id": 0,
        "forced_eos_token_id": 2,
        "no_repeat_ngram_size": 3,
    }
    history = [2, 0, 3, 4, 3, 4, 3, 4]
    for step in range(8):
        ids = history[: step + 1]
        scores = np.arange(6, dtype=np.float32)[None, :]
        expected = torch.tensor(scores.copy())
        for processor in (
            NoRepeatNGramLogitsProcessor(3),
            ForcedBOSTokenLogitsProcessor(0),
            ForcedEOSTokenLogitsProcessor(9, 2),
        ):
            expected = processor(torch.tensor([ids]), expected)
        runner._process_logits(scores, [np.array([token]) for token in ids[1:]], step)
        np.testing.assert_array_equal(scores, expected.numpy())
