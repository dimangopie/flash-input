"""Zipformer 流式转写（14M 中文模型，纯 onnxruntime）。

Zipformer 是流式模型：编码器每次只吃 ``T`` 帧（本模型 39 帧 = 390 ms），
并靠 45 个状态张量（每层 9 个）在块之间传递上下文；解码器是 stateless 的
2 帧上下文，joiner 输出 5537 维 logits。

这里按 sherpa-onnx 的顺序实现「喂块 → 编码 → 逐帧贪心 → 用新 token 更新
上下文」，最后补零块把尾巴冲出来。状态张量的形状全部从 ONNX 输入签名里读，
不写死层数与维度。
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np

os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

BLANK_ID = 0
TAIL_CHUNKS = 1  # 音频结束后再喂几个全零块，把最后一个字的输出冲出来


class StreamZipformerError(RuntimeError):
    """模型加载或推理失败。"""


def load_tokens(path: Path) -> list[str]:
    from .asr_ctc import load_tokens as _load

    return _load(path)


def _session(path: Path, threads: int):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, int(threads))
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


def _state_zeros(shape: list[Any], kind: str) -> np.ndarray:
    concrete = tuple(1 if not isinstance(d, int) else d for d in shape)
    if kind == "tensor(int64)":
        return np.zeros(concrete, dtype=np.int64)
    return np.zeros(concrete, dtype=np.float32)


class ZipformerEngine:
    """流式 zipformer；`transcribe()` 内部按块喂，不需要调用方分块。"""

    def __init__(
        self,
        model_dir: Path | None = None,
        *,
        threads: int = 4,
        warmup: bool = True,
    ):
        from . import asr_models

        self.engine = "zipformer"
        self.variant = "zipformer"
        self.threads = max(1, int(threads))
        self.warmup = warmup
        self.model_dir = Path(model_dir) if model_dir else asr_models.engine_dir("zipformer")
        self._encoder = None
        self._decoder = None
        self._joiner = None
        self._tokens: list[str] = []
        self._chunk = 39
        self._stride = 32
        self._context = 2
        self._load_ms = 0
        self._load_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._encoder is not None

    @property
    def load_ms(self) -> int:
        return self._load_ms

    def _find(self, prefix: str) -> Path:
        for path in sorted(self.model_dir.glob(f"{prefix}*.onnx")):
            if "int8" in path.name:
                return path
        matches = sorted(self.model_dir.glob(f"{prefix}*.onnx"))
        if not matches:
            raise StreamZipformerError(f"{self.model_dir} 里找不到 {prefix}*.onnx")
        return matches[0]

    def load(self) -> None:
        with self._load_lock:
            if self._encoder is not None:
                return
            if not self.model_dir.is_dir():
                raise StreamZipformerError(
                    f"zipformer 模型目录不存在：{self.model_dir}（先运行 `finput models fetch`）"
                )
            import time

            started = time.monotonic()
            self._encoder = _session(self._find("encoder"), self.threads)
            self._decoder = _session(self._find("decoder"), self.threads)
            self._joiner = _session(self._find("joiner"), self.threads)
            meta = self._encoder.get_modelmeta().custom_metadata_map
            self._chunk = int(meta.get("T") or 39)
            self._stride = int(meta.get("decode_chunk_len") or max(1, self._chunk - 7))
            dec_meta = self._decoder.get_modelmeta().custom_metadata_map
            self._context = int(dec_meta.get("context_size") or 2)
            self._tokens = load_tokens(self.model_dir / "tokens.txt")
            if self.warmup:
                with contextlib.suppress(Exception):
                    self.transcribe(np.zeros(16000, dtype=np.float32))
            self._load_ms = int((time.monotonic() - started) * 1000)

    def unload(self) -> None:
        with self._load_lock:
            self._encoder = self._decoder = self._joiner = None
            self._tokens = []
            self._load_ms = 0

    # ---- 内部 -----------------------------------------------------
    def _initial_states(self) -> dict[str, np.ndarray]:
        assert self._encoder is not None
        return {
            item.name: _state_zeros(item.shape, item.type)  # type: ignore[arg-type]
            for item in self._encoder.get_inputs()
            if item.name != "x"
        }

    def _encode(self, chunk: np.ndarray, states: dict[str, np.ndarray]):
        assert self._encoder is not None
        feeds: dict[str, np.ndarray] = {"x": chunk.astype(np.float32)[None, :, :]}
        feeds.update(states)
        outputs = self._encoder.run(None, feeds)
        names = [o.name for o in self._encoder.get_outputs()]
        result = dict(zip(names, outputs, strict=False))
        new_states = {name[len("new_") :]: value for name, value in result.items() if name.startswith("new_")}
        return result["encoder_out"], new_states

    def _decode_chunk(self, encoder_out: np.ndarray, context: list[int]):
        assert self._decoder is not None and self._joiner is not None
        emitted: list[int] = []
        decoder_out = self._decoder.run(None, {"y": np.asarray([context], dtype=np.int64)})[0]
        frames = encoder_out[0] if encoder_out.ndim == 3 else encoder_out
        for frame in range(frames.shape[0]):
            encoder_frame = frames[frame][None, :]
            logits = self._joiner.run(None, {"encoder_out": encoder_frame, "decoder_out": decoder_out})[0]
            token = int(np.argmax(logits[0]))
            if token == BLANK_ID:
                continue
            emitted.append(token)
            context = [*context[1:], token]
            decoder_out = self._decoder.run(None, {"y": np.asarray([context], dtype=np.int64)})[0]
        return emitted, context

    # ---- 对外 -----------------------------------------------------
    def transcribe(self, audio: np.ndarray) -> str:
        if self._encoder is None:
            self.load()
        from . import fbank as fbank_mod

        signal = np.asarray(audio, dtype=np.float32).reshape(-1)
        frame_len = fbank_mod.FRAME_LENGTH_MS * fbank_mod.SAMPLE_RATE // 1000
        if signal.size < frame_len:
            signal = np.pad(signal, (0, frame_len - signal.size))
        features = fbank_mod.fbank(signal, snip_edges=False, window="povey")
        if features.shape[0] == 0:
            return ""
        chunk = self._chunk
        stride = self._stride
        states = self._initial_states()
        context = [BLANK_ID] * self._context
        tokens: list[int] = []
        total = features.shape[0]
        # 每次喂 39 帧、前进 32 帧（重叠 7 帧），这是 zipformer 流式的正确切法
        chunks = []
        start = 0
        while start + chunk <= total:
            chunks.append(features[start : start + chunk])
            start += stride
        if start < total:
            tail = features[start:]
            chunks.append(np.pad(tail, ((0, chunk - tail.shape[0]), (0, 0))))
        for block in chunks + [np.zeros((chunk, features.shape[1]), np.float32)] * TAIL_CHUNKS:
            encoder_out, states = self._encode(block, states)
            emitted, context = self._decode_chunk(encoder_out, context)
            tokens.extend(emitted)
        return "".join(self._tokens[t] for t in tokens if 0 <= t < len(self._tokens)).strip()

    def status(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "engine": self.engine,
            "dir": str(self.model_dir),
            "threads": self.threads,
            "load_ms": self._load_ms,
        }
