"""RM38/RM41, AC29/AC32: native loading owns CPU after verified artifact I/O."""

import builtins
import hashlib
import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from relaymind.assets.embedding_manifest import VerifiedEmbeddingArtifact, load_embedding_manifest
from relaymind.assets.embedding_onnx import OnnxCpuBackend
from relaymind.assets.scheduler import AssetWorkScheduler
from relaymind.assets.sources import CaptureContext, SourceFailure
from relaymind.store.run_store import ForegroundWindow
from test_embedding_manifest import fixture


class Clock:
    def __init__(self):
        self.now = 10.0

    def monotonic(self):
        return self.now


def native_manifest(tmp_path):
    path, raw = fixture(tmp_path)
    tokenizer = {
        "post_processor": {
            "type": "TemplateProcessing",
            "single": [
                {"SpecialToken": {"id": "cls"}},
                {"Sequence": {"id": "A", "type_id": 0}},
                {"SpecialToken": {"id": "sep"}},
            ],
            "special_tokens": {"cls": {"ids": [1]}, "sep": {"ids": [2]}},
        }
    }
    files = {
        "tokenizer.json": json.dumps(tokenizer).encode(),
        "config.json": json.dumps(
            {"pad_token_id": 0, "max_position_embeddings": 512, "hidden_size": 2}
        ).encode(),
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
        if name == "tokenizer.json":
            entry = next(item for item in raw["files"] if item["path"] == name)
            entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        else:
            raw["files"].append(
                dict(
                    path=name,
                    roles=["configuration"],
                    bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
    path.write_text(json.dumps(raw))
    return load_embedding_manifest(path)


def control_for(clock, busy, cancelled):
    scheduler = AssetWorkScheduler(busy=busy)
    window = ForegroundWindow(clock.monotonic)
    context = CaptureContext(
        datetime.now(UTC),
        monotonic=clock.monotonic,
        cancelled=cancelled,
        observe_busy=busy,
        work_gate=scheduler.checkpoint,
        foreground_window=window,
        finish_cpu=scheduler.end_batch,
    )
    return context, window


def stub_runtime(monkeypatch, manifest, *, tokenizer_started, session_started):
    from relaymind.assets import embedding_onnx

    class Tokenizer:
        @staticmethod
        def from_str(_text):
            tokenizer_started()
            return Tokenizer()

        def no_truncation(self):
            pass

        def no_padding(self):
            pass

    class Session:
        def __init__(self, _model, **_options):
            session_started()

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

        def get_outputs(self):
            return [SimpleNamespace(name="last_hidden_state")]

    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=Tokenizer))
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(
            SessionOptions=lambda: SimpleNamespace(),
            ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
            InferenceSession=Session,
        ),
    )
    monkeypatch.setattr(embedding_onnx, "runtime_identity", lambda: dict(manifest.data["runtime"]))
    monkeypatch.setattr(embedding_onnx, "device_identity", lambda: manifest.data["device"])


def test_verified_read_then_native_load_records_foreground_overlap(tmp_path, monkeypatch):
    """A real verified read must re-enter CPU ownership for tokenizer and ORT load."""
    manifest = native_manifest(tmp_path)
    clock = Clock()
    busy = False
    context, window = control_for(clock, lambda: busy, lambda: False)
    active = []

    def tokenizer_started():
        active.append(window._active)

    def session_started():
        active.append(window._active)
        clock.now = 10.1
        window.record(clock.now)
        clock.now = 10.12

    stub_runtime(
        monkeypatch,
        manifest,
        tokenizer_started=tokenizer_started,
        session_started=session_started,
    )
    imports = []
    original_import = builtins.__import__

    def observed_import(name, *args, **kwargs):
        if name in {"numpy", "onnxruntime", "tokenizers"}:
            imports.append(window._active)
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", observed_import)
        with VerifiedEmbeddingArtifact(manifest, control=context) as artifact:
            OnnxCpuBackend(artifact)
    assert imports == [True, True, True]
    assert active == [True, True]
    assert context.busy_observed_at == 10.12
    assert round(context.drain_ms, 6) == 20

    # The last verified read closes CPU ownership. A foreground holder must
    # prevent the following native constructor, with no real-time sleep.
    clock = Clock()
    busy_calls = 0
    busy = False
    cancelled = False

    def holder():
        nonlocal busy_calls, cancelled
        if not busy:
            return False
        busy_calls += 1
        if busy_calls >= 2:
            cancelled = True
        return True

    context, _window = control_for(clock, holder, lambda: cancelled)
    starts = []
    stub_runtime(
        monkeypatch,
        manifest,
        tokenizer_started=lambda: starts.append("tokenizer"),
        session_started=lambda: starts.append("session"),
    )
    with pytest.raises(SourceFailure, match="cancelled"):
        with VerifiedEmbeddingArtifact(manifest, control=context) as artifact:
            original_read = artifact.read_bytes

            def after_weights(name, *, limit):
                nonlocal busy
                value = original_read(name, limit=limit)
                if name == "weights.bin":
                    busy = True
                return value

            artifact.read_bytes = after_weights
            OnnxCpuBackend(artifact)
    assert starts == ["tokenizer"]


def test_cancelled_native_constructor_preserves_source_failure_and_never_publishes(tmp_path):
    """RM38/RM41: cancellation after native CPU still owns the failed load."""
    from relaymind.assets.embedding_activation import BackendRegistration, EmbeddingLoader
    from relaymind.assets.embedding_qualification import qualification_key
    from test_embedding_activation import configured

    config = configured(tmp_path)
    clock = Clock()
    cancelled = False
    context, _window = control_for(clock, lambda: False, lambda: cancelled)
    started = []

    def construct(artifact):
        def native():
            nonlocal cancelled
            started.append("native")
            cancelled = True
            return object()

        return artifact.run_cpu(native)

    entry = BackendRegistration(
        lambda: dict(config.manifest.data["runtime"]),
        lambda: config.manifest.data["device"],
        construct,
    )
    loader = EmbeddingLoader(
        qualifications={qualification_key(config.manifest, config.digest)},
        backends={"fixture": entry},
    )
    with pytest.raises(SourceFailure, match="cancelled"):
        loader.load(config, control=context)
    assert started == ["native"]


def test_cancellation_after_native_guard_prevents_engine_publication(tmp_path):
    """RM38/RM41: final close cannot turn a cancelled load into a usable engine."""
    from relaymind.assets.embedding_activation import BackendRegistration, EmbeddingLoader
    from relaymind.assets.embedding_qualification import qualification_key
    from test_embedding_activation import configured
    from test_embedding_backend import NativeDouble

    config = configured(tmp_path)
    clock = Clock()
    cancelled = False
    context, _window = control_for(clock, lambda: False, lambda: cancelled)
    started = []

    def construct(artifact):
        nonlocal cancelled
        started.append("native")
        original_guard = artifact.guard

        def late_guard():
            nonlocal cancelled
            original_guard()
            cancelled = True

        artifact.guard = late_guard
        return NativeDouble()

    entry = BackendRegistration(
        lambda: dict(config.manifest.data["runtime"]),
        lambda: config.manifest.data["device"],
        construct,
    )
    loader = EmbeddingLoader(
        qualifications={qualification_key(config.manifest, config.digest)},
        backends={"fixture": entry},
    )
    with pytest.raises(SourceFailure, match="cancelled"):
        loader.load(config, control=context)
    assert started == ["native"]
