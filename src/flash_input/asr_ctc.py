"""SenseVoice / Paraformer 的 CTC 转写（纯 onnxruntime，不依赖 sherpa-onnx）。

两个模型的输入都是「Kaldi fbank 80 维 → LFR(7,6) → CMVN」后的 560 维特征，
输出是 CTC logits；差别只在模型接口：

    SenseVoice:  x, x_length, language, text_norm        → logits (N, T, 25055)
    Paraformer:  speech, speech_lengths                  → logits (batch, T, 8359)

预处理参数（LFR、CMVN、语言 id、ITN id）都写在这两个 ONNX 的
`custom_metadata_map` 里，这里直接读，不把任何数字写死——换了模型版本，
只要元数据还在就照样能跑。
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import asr_models
from . import fbank as fbank_mod

os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

SENSEVOICE_SKIP = 4  # <lang> <emotion> <event> <itn> 四个前置 token
BLANK_ID = 0


class CtcAsrError(RuntimeError):
    """模型加载或推理失败。"""


def _session(path: Path, threads: int):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, int(threads))
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


def _floats(meta: dict[str, str], key: str) -> np.ndarray:
    raw = meta.get(key)
    if not raw:
        raise CtcAsrError(f"模型缺少元数据 {key}")
    return np.asarray([float(x) for x in raw.split(",")], dtype=np.float32)


def load_tokens(path: Path) -> list[str]:
    """tokens.txt：每行「符号 空格 id」，符号里可能含空格，所以从右边切。"""
    table: dict[int, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            symbol, _, index = line.rpartition(" ")
            if not index.isdigit():
                continue
            table[int(index)] = symbol
    size = max(table) + 1 if table else 0
    return [table.get(i, "") for i in range(size)]


def detokenize(ids, tokens: list[str], *, drop: int = 0, min_id: int = 0) -> str:
    """ids → 文本：丢掉前 ``drop`` 个（SenseVoice 的 4 个前置 token），
    并跳过 ``min_id`` 以下的特殊符号（blank / <s> / </s> / <OOV>）。"""
    pieces = []
    for index in list(ids)[drop:]:
        index = int(index)
        if index < min_id or index >= len(tokens):
            continue
        pieces.append(tokens[index])
    text = "".join(pieces).replace("\u2581", " ").strip()
    return " ".join(text.split()) if text else ""


class CtcEngine:
    """一个 CTC 引擎实例；`load()` 幂等，`transcribe()` 可反复调用。"""

    def __init__(
        self,
        engine: str,
        model_dir: Path | None = None,
        *,
        threads: int = 4,
        language: str = "auto",
        warmup: bool = True,
    ):
        if engine not in asr_models.ENGINES:
            raise CtcAsrError(f"未知引擎 {engine!r}")
        self.engine = engine
        self.variant = engine  # 给 status 展示用，与 Qwen3 的 variant 同义
        self.threads = max(1, int(threads))
        self.language = language
        self.warmup = warmup
        self.model_dir = Path(model_dir) if model_dir else asr_models.engine_dir(engine)
        self._session = None
        self._tokens: list[str] = []
        self._meta: dict[str, str] = {}
        self._neg = np.zeros(0, dtype=np.float32)
        self._istd = np.zeros(0, dtype=np.float32)
        self._window = 7
        self._shift = 6
        self._load_ms = 0
        self._load_lock = threading.Lock()

    # ---- 生命周期 -------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self._session is not None

    @property
    def load_ms(self) -> int:
        return self._load_ms

    def load(self) -> None:
        with self._load_lock:
            if self._session is not None:
                return
            self._load_impl()

    def _load_impl(self) -> None:
        started = time.monotonic()
        model = self.model_dir / "model.int8.onnx"
        tokens = self.model_dir / "tokens.txt"
        if not model.is_file() or not tokens.is_file():
            raise CtcAsrError(f"{self.engine} 模型不完整（{self.model_dir}）：先运行 `finput models fetch`")
        session = _session(model, self.threads)
        meta = dict(session.get_modelmeta().custom_metadata_map)
        self._neg = _floats(meta, "neg_mean")
        self._istd = _floats(meta, "inv_stddev")
        self._window = int(meta.get("lfr_window_size") or 7)
        self._shift = int(meta.get("lfr_window_shift") or 6)
        self._tokens = load_tokens(tokens)
        self._meta = meta
        self._session = session
        if self.warmup:
            self._warmup()
        self._load_ms = int((time.monotonic() - started) * 1000)

    def _warmup(self) -> None:
        silence = np.zeros(fbank_mod.SAMPLE_RATE, dtype=np.float32)
        with contextlib.suppress(Exception):
            self.transcribe(silence)

    def unload(self) -> None:
        with self._load_lock:
            self._session = None
            self._tokens = []
            self._meta = {}
            self._neg = np.zeros(0, dtype=np.float32)
            self._istd = np.zeros(0, dtype=np.float32)
            self._load_ms = 0

    # ---- 推理 -----------------------------------------------------
    def _features(self, audio: np.ndarray) -> np.ndarray:
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        frame_length = fbank_mod.FRAME_LENGTH_MS * fbank_mod.SAMPLE_RATE // 1000
        if audio.size < frame_length:
            audio = np.pad(audio, (0, frame_length - audio.size))
        # 两个模型的声学前端不同（与 sherpa-onnx 的选择一致）：
        #   SenseVoice  [-1,1] 样本、povey 窗、snip_edges=false
        #   Paraformer  int16 量级样本、hamming 窗、snip_edges=true
        if self.engine == "paraformer":
            mel = fbank_mod.fbank(audio * 32768.0, snip_edges=True, window="hamming")
        else:
            mel = fbank_mod.fbank(audio, snip_edges=False, window="povey")
        feats = fbank_mod.apply_lfr(mel, self._window, self._shift)
        return fbank_mod.apply_cmvn(feats, self._neg, self._istd).astype(np.float32)

    def _language_id(self) -> int:
        if self.engine != "sensevoice":
            return 0
        name = f"lang_{self.language or 'auto'}"
        return int(self._meta.get(name) or self._meta.get("lang_auto") or 0)

    def transcribe(self, audio: np.ndarray) -> str:
        if self._session is None:
            self.load()
        assert self._session is not None
        feats = self._features(audio)
        if feats.shape[0] == 0:
            return ""
        batch = feats[None, :, :]
        if self.engine == "sensevoice":
            inputs = {
                "x": batch,
                "x_length": np.asarray([feats.shape[0]], dtype=np.int32),
                "language": np.asarray([self._language_id()], dtype=np.int32),
                "text_norm": np.asarray([int(self._meta.get("with_itn") or 14)], dtype=np.int32),
            }
        else:
            inputs = {
                "speech": batch,
                "speech_lengths": np.asarray([feats.shape[0]], dtype=np.int32),
            }
        logits = self._session.run(None, inputs)[0]
        ids = fbank_mod.ctc_greedy(np.asarray(logits)[0], blank_id=BLANK_ID)
        if self.engine == "sensevoice":
            # SenseVoice 每次输出都以 <lang> <emotion> <event> <itn> 开头
            return detokenize(ids, self._tokens, drop=SENSEVOICE_SKIP, min_id=SENSEVOICE_SKIP)
        return detokenize(ids, self._tokens, drop=0, min_id=SENSEVOICE_SKIP)

    def status(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "engine": self.engine,
            "dir": str(self.model_dir),
            "threads": self.threads,
            "load_ms": self._load_ms,
        }
