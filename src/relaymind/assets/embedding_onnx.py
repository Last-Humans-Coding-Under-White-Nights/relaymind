"""Optional local ONNX/fast-tokenizer backend, verified bytes only (RM41).

Dependencies are imported only by this implemented registry entry. No model URL,
filename search, remote code, package installation or external inference is used.
"""

import json
import platform
from pathlib import Path

from relaymind.assets.embedding_manifest import VerifiedEmbeddingArtifact
from relaymind.assets.embeddings import EmbeddingError
from relaymind.canonical import canonical_digest


def _implementation_identity():
    from relaymind.assets.embedding_bundle import implementation_identity

    return implementation_identity()


def runtime_identity() -> dict[str, str]:
    try:
        import numpy
        import onnxruntime
        import tokenizers
    except ImportError:
        raise EmbeddingError("embedding_runtime_unavailable") from None
    return dict(
        backend="onnx_e5_cpu",
        version=onnxruntime.__version__,
        build=canonical_digest(
            dict(
                onnx=onnxruntime.get_build_info(),
                numpy=numpy.__version__,
                tokenizers=tokenizers.__version__,
                implementation=_implementation_identity(),
                threads=1,
                execution="sequential",
                pooling="masked_mean_float64.v1",
            )
        ),
    )


def device_identity() -> str:
    processor = platform.processor()
    if not processor and platform.system() == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    processor = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
    if not processor:
        raise EmbeddingError("embedding_device_unknown")
    value = f"cpu:{platform.system()}:{platform.machine()}:{processor}"
    if len(value.encode()) > 256:
        raise EmbeddingError("embedding_device_unknown")
    return value


class OnnxCpuBackend:
    def __init__(self, artifact: VerifiedEmbeddingArtifact):
        import numpy
        import onnxruntime
        from tokenizers import Tokenizer

        raw = artifact.manifest.data
        if dict(raw["runtime"]) != runtime_identity() or raw["device"] != device_identity():
            raise EmbeddingError("embedding_runtime_mismatch")
        self._numpy, self._ort = numpy, onnxruntime
        self._options = None

        def one(role, limit):
            def name():
                entries = [f for f in raw["files"] if role in f["roles"]]
                if len(entries) != 1:
                    raise EmbeddingError("unsupported_backend_files")
                return entries[0]["path"]

            return artifact.read_bytes(artifact.run_cpu(name), limit=limit)

        tokenizer_data = one("tokenizer", 64 * 1024 * 1024)
        configuration_data = one("configuration", 65536)

        def configure():
            config = json.loads(configuration_data)
            token_config = json.loads(tokenizer_data)
            processor = token_config.get("post_processor", {})
            if processor.get("type") != "TemplateProcessing":
                raise EmbeddingError("unsupported_tokenizer_policy")
            single = processor.get("single")
            if (
                not isinstance(single, list)
                or len(single) != 3
                or single[1] != {"Sequence": {"id": "A", "type_id": 0}}
                or set(single[0]) != {"SpecialToken"}
                or set(single[2]) != {"SpecialToken"}
            ):
                raise EmbeddingError("unsupported_tokenizer_policy")
            try:
                prefix = tuple(processor["special_tokens"][single[0]["SpecialToken"]["id"]]["ids"])
                suffix = tuple(processor["special_tokens"][single[2]["SpecialToken"]["id"]]["ids"])
                pad = config["pad_token_id"]
                max_tokens = config["max_position_embeddings"]
                if config["hidden_size"] != raw["dimensions"]:
                    raise EmbeddingError("embedding_dimension_mismatch")
            except (KeyError, TypeError):
                raise EmbeddingError("unsupported_tokenizer_policy") from None
            if (
                any(type(x) is not int or x < 0 for x in (*prefix, *suffix, pad))
                or type(max_tokens) is not int
                or not 1 <= max_tokens <= 8192
            ):
                raise EmbeddingError("unsupported_tokenizer_policy")
            tokenizer = Tokenizer.from_str(tokenizer_data.decode("utf-8"))
            tokenizer.no_truncation()
            tokenizer.no_padding()
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            return prefix, suffix, pad, max_tokens, tokenizer, options

        (
            self._prefix,
            self._suffix,
            self._pad,
            self._max_tokens,
            self._tokenizer,
            options,
        ) = artifact.run_cpu(configure)
        self._dimensions = raw["dimensions"]
        model = one("weights", 512 * 1024 * 1024)

        # A serialized model has no filesystem base for unlisted external data.
        # No custom operator library or external model resolver is registered.
        def construct_session():
            try:
                return onnxruntime.InferenceSession(
                    model, sess_options=options, providers=["CPUExecutionProvider"]
                )
            except Exception:
                raise EmbeddingError("embedding_model_unavailable") from None

        self._session = artifact.run_cpu(construct_session)
        artifact.guard()

        def inspect_session():
            inputs = {v.name for v in self._session.get_inputs()}
            if (
                not {"input_ids", "attention_mask"}
                <= inputs
                <= {
                    "input_ids",
                    "attention_mask",
                    "token_type_ids",
                }
            ):
                raise EmbeddingError("unsupported_model_inputs")
            return inputs, self._session.get_outputs()[0].name

        self._inputs, self._output = artifact.run_cpu(inspect_session)

    def encode_segment(self, value: str) -> list[int]:
        return self._tokenizer.encode(value, add_special_tokens=False).ids

    def special_tokens(self, tokens) -> tuple[int, ...]:
        return (*self._prefix, *tokens, *self._suffix)

    def cancel(self) -> None:
        options = self._options
        if options is not None:
            options.terminate = True

    def infer(self, token_batches, invoked):
        np = self._numpy
        if not 1 <= len(token_batches) <= 25 or any(
            not 1 <= len(tokens) <= self._max_tokens for tokens in token_batches
        ):
            raise EmbeddingError("input_overflow")
        length = max(map(len, token_batches))
        ids = np.full((len(token_batches), length), self._pad, dtype=np.int64)
        mask = np.zeros_like(ids)
        for row, tokens in enumerate(token_batches):
            ids[row, : len(tokens)] = tokens
            mask[row, : len(tokens)] = 1
        values = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            values["token_type_ids"] = np.zeros_like(ids)
        register = getattr(invoked, "register_buffer", None)
        if register is not None:
            for array in values.values():
                register(array)
        options = self._ort.RunOptions()
        self._options = options
        try:
            invoked()
            output = self._session.run([self._output], values, options)[0]
            if output.shape != (len(token_batches), length, self._dimensions):
                raise EmbeddingError("invalid_vectors")
            weighted = output.astype(np.float64) * mask[:, :, None]
            return (weighted.sum(axis=1) / mask.sum(axis=1)[:, None]).tolist()
        finally:
            self._options = None
            ids.fill(0)
            mask.fill(0)
            for array in values.values():
                array.fill(0)
