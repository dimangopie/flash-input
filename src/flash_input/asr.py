"""Qwen3-ASR 0.6B/1.7B int8 的离线单句转写。

三段式 ONNX（与本项目其它部分一样只有 CPU 一个后端）：

    log-mel (128, n_frames)
      → conv_frontend.onnx   → (1, n_audio, 512)
      → encoder.int8.onnx    → audio_features (1, n_audio, 1024|2048)
      → decoder.int8.onnx    → 自回归，greedy 到 <|im_end|>

音频按实际长度送进 conv/encoder，不补齐到 30 s：短句的 prefill 成本随
时长线性下降，长句也不再被静默截断（KV cache 按「prompt + 最大生成」现分）。
一次只处理一句话（录音器上限 60 s，这里兜底 300 s）。
不做流式，也不做长音频切分——那是另一套复杂度和另一套 bug。

常驻策略不在这里实现，由模块级 `get_engine()` / `release()` 按
`asr.resident`（none / 10min / 30min / forever）决定什么时候 `unload()`。
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from . import asr_catalog, features, models
from . import config as config_mod

os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

SAMPLE_RATE = features.SAMPLE_RATE
MAX_NEW_TOKENS = 400
MIN_SAMPLES = int(SAMPLE_RATE * 0.1)  # 0.1 s 以下视为误触
MAX_TOTAL_LEN = 1200  # 未显式指定长度时的 KV cache 槽数
MAX_AUDIO_SECONDS = 300  # 单句音频硬上限，避免 KV cache 无界增长

AUDIO_START = "<|audio_start|>"
AUDIO_END = "<|audio_end|>"
AUDIO_PAD = "<|audio_pad|>"
IM_START = "<|im_start|>"
IM_END = "<|im_end|>"


class AsrError(RuntimeError):
    """模型缺失、图不完整或转写失败。"""


# --------------------------------------------------------------------------- 文本处理


def build_prompt(audio_token_count: int, system_prompt: str = "") -> str:
    """渲染 Qwen3-ASR 的 chat 模板，<|audio_pad|> 复制 N 份。"""
    if audio_token_count < 1:
        raise ValueError(f"audio_token_count 必须 >= 1，实际 {audio_token_count}")
    system = f"{IM_START}system\n{system_prompt}{IM_END}\n"
    audio = f"{AUDIO_START}{AUDIO_PAD * audio_token_count}{AUDIO_END}"
    user = f"{IM_START}user\n{audio}{IM_END}\n"
    return system + user + f"{IM_START}assistant\n"


_ASR_MARK = "<asr_text>"


def parse_asr_output(raw: str) -> str:
    """从 ``<asr_text>…<|im_end|>`` 里取出正文；没有标记就原样去空白。"""
    if not raw:
        return ""
    text = raw
    idx = text.rfind(_ASR_MARK)
    if idx >= 0:
        text = text[idx + len(_ASR_MARK) :]
    import re

    text = re.sub(r"<\|[a-zA-Z0-9_]+\|>", "", text)
    return text.replace(_ASR_MARK, "").strip()


# --------------------------------------------------------------------------- Tokenizer


class Qwen3Tokenizer:
    """用 HuggingFace ``tokenizers`` 重建 Qwen3-ASR 的 byte-level BPE。

    模型仓库只给 ``vocab.json`` + ``merges.txt`` + ``tokenizer_config.json``，
    没有 ``tokenizer.json``，所以这里按 Qwen2Tokenizer 的规则重建：
    ByteLevel 预分词 + ByteLevel 解码 + 62 个 added token。
    """

    def __init__(self, tokenizer_dir: Path):
        from tokenizers import AddedToken, Tokenizer, decoders, pre_tokenizers
        from tokenizers.models import BPE

        tokenizer_dir = Path(tokenizer_dir)
        paths = {name: tokenizer_dir / name for name in models.TOKENIZER_FILES}
        missing = [str(p) for p in paths.values() if not p.is_file()]
        if missing:
            raise AsrError("分词器文件缺失：" + "、".join(missing))

        tok = Tokenizer(BPE.from_file(str(paths["vocab.json"]), str(paths["merges.txt"])))
        tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=True)
        tok.decoder = decoders.ByteLevel()

        cfg = json.loads(paths["tokenizer_config.json"].read_text(encoding="utf-8"))
        added = cfg.get("added_tokens_decoder", {})
        tokens = [
            AddedToken(
                info["content"],
                lstrip=info.get("lstrip", False),
                rstrip=info.get("rstrip", False),
                normalized=info.get("normalized", False),
                single_word=info.get("single_word", False),
                special=info.get("special", False),
            )
            for _, info in sorted(added.items(), key=lambda kv: int(kv[0]))
        ]
        tok.add_tokens(tokens)

        self._tok = tok
        self._special_ids = {int(tid) for tid, info in added.items() if info.get("special", False)}
        self.eos_id = tok.token_to_id(IM_END)

    def encode(self, text: str) -> list[int]:
        return self._tok.encode(text).ids

    def decode(self, ids: list[int], *, skip_special: bool = True) -> str:
        kept = [i for i in ids if i not in self._special_ids] if skip_special else ids
        return self._tok.decode(kept, skip_special_tokens=False)


# --------------------------------------------------------------------------- ONNX runner


class _Runner:
    """三个 session + KV cache 的薄封装；常量都在这里，不散落。"""

    def __init__(self, model_dir: Path, *, threads: int = 4, max_total_len: int = MAX_TOTAL_LEN):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.intra_op_num_threads = max(1, int(threads))
        opts.inter_op_num_threads = 1

        files = {name: model_dir / name for name in models.MODEL_FILES}
        missing = [str(p) for p in files.values() if not p.is_file()]
        if missing:
            raise AsrError("模型文件缺失：" + "、".join(missing))

        providers = ["CPUExecutionProvider"]
        self.conv = ort.InferenceSession(
            str(files["conv_frontend.onnx"]), sess_options=opts, providers=providers
        )
        self.encoder = ort.InferenceSession(
            str(files["encoder.int8.onnx"]), sess_options=opts, providers=providers
        )
        self.decoder = ort.InferenceSession(
            str(files["decoder.int8.onnx"]), sess_options=opts, providers=providers
        )

        self.max_total_len = max_total_len
        self.num_layers, self.kv_heads, self.head_dim = self._inspect_decoder()
        self._out_names = ["logits"]
        for i in range(self.num_layers):
            self._out_names += [f"key_delta_{i}", f"value_delta_{i}"]

    def _inspect_decoder(self) -> tuple[int, int, int]:
        key_inputs = [i for i in self.decoder.get_inputs() if i.name.startswith("cache_key_")]
        if not key_inputs:
            raise AsrError("decoder.onnx 没有 cache_key_* 输入，图结构异常")
        shape = key_inputs[0].shape
        heads = shape[2] if isinstance(shape[2], int) else 8
        head_dim = shape[3] if isinstance(shape[3], int) else 128
        return len(key_inputs), heads, head_dim

    def encode_audio(self, mel: np.ndarray) -> np.ndarray:
        conv_out = self.conv.run(["conv_output"], {"input_features": mel.T[np.newaxis, ...]})[0]
        mask = np.ones((1, conv_out.shape[1]), dtype=bool)
        return self.encoder.run(
            ["audio_features"],
            {"input_features": conv_out, "feature_attention_mask": mask},
        )[0]

    def alloc_caches(self, total_len: int | None = None) -> list[np.ndarray]:
        """按需要的槽数分配 KV cache；长度由 prompt + 最大生成长度决定。"""
        length = int(total_len) if total_len else self.max_total_len
        return [
            np.zeros((1, length, self.kv_heads, self.head_dim), dtype=np.float32)
            for _ in range(2 * self.num_layers)
        ]

    def step(
        self,
        input_ids: np.ndarray,
        audio_features: np.ndarray,
        caches: list[np.ndarray],
        cur_len: int,
    ) -> np.ndarray:
        seq = input_ids.shape[1]
        limit = caches[0].shape[1]
        if cur_len + seq > limit:
            raise AsrError(f"KV cache 溢出：{cur_len} + {seq} > {limit}，这句太长，拆开发")
        feed: dict[str, Any] = {
            "input_ids": input_ids,
            "audio_features": audio_features,
            "attention_mask": np.ones(input_ids.shape, dtype=np.int64),
            "cache_position": np.arange(cur_len, cur_len + seq, dtype=np.int64),
        }
        for i in range(self.num_layers):
            feed[f"cache_key_{i}"] = caches[2 * i]
            feed[f"cache_value_{i}"] = caches[2 * i + 1]

        outputs = self.decoder.run(self._out_names, feed)
        end = cur_len + seq
        for i in range(self.num_layers):
            caches[2 * i][:, cur_len:end, :, :] = outputs[1 + 2 * i]
            caches[2 * i + 1][:, cur_len:end, :, :] = outputs[2 + 2 * i]
        return outputs[0]


# --------------------------------------------------------------------------- 引擎


class AsrEngine:
    """加载一次、转写多次；`load()` 幂等。"""

    def __init__(
        self,
        variant: str = models.DEFAULT_VARIANT,
        *,
        threads: int = 4,
        root: Path | None = None,
        warmup: bool = True,
    ):
        if variant not in models.VARIANTS:
            raise AsrError(f"未知规格 {variant!r}，可选：{' / '.join(models.VARIANTS)}")
        self.variant = variant
        self.threads = max(1, int(threads))
        self.warmup = warmup
        self.root = root
        self._runner: _Runner | None = None
        self._tokenizer: Qwen3Tokenizer | None = None
        self._load_ms = 0
        self._load_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._runner is not None and self._tokenizer is not None

    @property
    def load_ms(self) -> int:
        return self._load_ms

    def load(self) -> None:
        with self._load_lock:
            self._load_locked()

    def _load_locked(self) -> None:
        if self.loaded:
            return
        started = time.perf_counter()
        root = self.root or models.find_root(self.variant)
        if root is None:
            raise AsrError(
                f"没有找到 {self.variant} 模型。先跑一次：finput models fetch --variant {self.variant}\n"
                f"（仓库 {models.REPO_ID}，约 {'983 MB' if self.variant == '0.6B' else '2.6 GB'}）"
            )
        self._runner = _Runner(models.model_dir(root, self.variant), threads=self.threads)
        self._tokenizer = Qwen3Tokenizer(models.tokenizer_dir(root))
        if self.warmup:
            self._warmup()
        self._load_ms = int((time.perf_counter() - started) * 1000)

    def unload(self) -> None:
        self._runner = None
        self._tokenizer = None
        self._load_ms = 0
        import gc

        gc.collect()

    def _warmup(self) -> None:
        """1 s 噪声跑一次 prefill + 几步 greedy，坏图在加载期就暴露。"""
        assert self._runner is not None
        rng = np.random.default_rng(0)
        audio = rng.standard_normal(SAMPLE_RATE).astype(np.float32) * 0.05
        logits = self._prefill(audio)
        if not np.isfinite(logits).all() or not np.any(logits != 0):
            raise AsrError("模型自检失败：prefill 输出退化（logits 非有限或全 0），模型文件可能损坏")
        eos = self._tokenizer.eos_id if self._tokenizer else None
        for _ in range(3):
            next_id = int(np.argmax(logits[0, -1]))
            if next_id == eos:
                raise AsrError("模型自检失败：第一步就输出结束符，模型文件可能损坏")
            break

    def _prefill(self, audio: np.ndarray) -> np.ndarray:
        assert self._runner is not None and self._tokenizer is not None
        mel = features.log_mel_spectrogram(audio)
        audio_features = self._runner.encode_audio(mel)
        prompt_ids = self._tokenizer.encode(build_prompt(audio_features.shape[1]))
        self._caches = self._runner.alloc_caches(len(prompt_ids) + MAX_NEW_TOKENS)
        self._audio_features = audio_features
        logits = self._runner.step(
            np.array([prompt_ids], dtype=np.int64), audio_features, self._caches, cur_len=0
        )
        self._cur_len = len(prompt_ids)
        return logits

    def transcribe(self, audio: np.ndarray) -> str:
        """``audio``：float32 单声道 16 kHz，[-1, 1]。空/过短返回 ""。"""
        if audio is None or len(audio) < MIN_SAMPLES:
            return ""
        self.load()
        assert self._runner is not None and self._tokenizer is not None

        audio = np.asarray(audio, dtype=np.float32)
        limit = MAX_AUDIO_SECONDS * SAMPLE_RATE
        if len(audio) > limit:
            audio = audio[:limit]

        logits = self._prefill(audio)
        eos = self._tokenizer.eos_id
        generated: list[int] = []
        while len(generated) < MAX_NEW_TOKENS:
            next_id = int(np.argmax(logits[0, -1]))
            if next_id == eos:
                break
            generated.append(next_id)
            logits = self._runner.step(
                np.array([[next_id]], dtype=np.int64), self._audio_features, self._caches, self._cur_len
            )
            self._cur_len += 1
        raw = self._tokenizer.decode(generated)
        return parse_asr_output(raw)


# --------------------------------------------------------------------------- 音频解码


def wav_to_float32(data: bytes) -> np.ndarray:
    """16-bit PCM WAV → float32 单声道 [-1, 1]。多声道取均值，非 16 kHz 报错。"""
    with wave.open(io.BytesIO(data), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise AsrError(
            f"只支持 16-bit PCM WAV，实际 {width * 8}-bit；用 ffmpeg 转一下：ffmpeg -i in -ar 16000 -ac 1 -c:a pcm_s16le out.wav"
        )
    if rate != SAMPLE_RATE:
        raise AsrError(
            f"采样率需要 {SAMPLE_RATE} Hz，实际 {rate} Hz；用 ffmpeg 转一下：ffmpeg -i in -ar 16000 -ac 1 -c:a pcm_s16le out.wav"
        )
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


def load_audio_file(path: Path) -> np.ndarray:
    return wav_to_float32(Path(path).expanduser().read_bytes())


# --------------------------------------------------------------------------- 常驻管理

_ENGINE: AsrEngine | None = None
_ENGINE_KEY: tuple[str, str, int] | None = None
_LAST_USED = 0.0


def config_key(cfg: dict[str, Any]) -> tuple[str, str, int]:
    """决定引擎身份的配置三元组；变了就必须换引擎。

    daemon 的常驻缓存也用它比对，避免「网页里换了模型、内存里还是旧的」。
    """
    asr = cfg["asr"]
    return (
        str(asr.get("model") or asr_catalog.DEFAULT_MODEL),
        str(asr.get("language") or "auto"),
        int(asr["threads"]),
    )


def make_engine(cfg: dict[str, Any]):
    """按配置建一个引擎实例（daemon 自己管生命周期，也走这里）。"""
    entry = asr_catalog.resolve(cfg)
    threads = int(cfg["asr"]["threads"])
    if entry["kind"] == "qwen3":
        return AsrEngine(entry["variant"], threads=threads)
    if entry["kind"] == "zipformer":
        from .asr_stream import ZipformerEngine

        return ZipformerEngine(asr_catalog.model_dir(entry), threads=threads)
    from .asr_ctc import CtcEngine

    return CtcEngine(
        entry["engine"],
        asr_catalog.model_dir(entry),
        threads=threads,
        language=str(cfg["asr"].get("language") or "auto"),
    )


def get_engine(cfg: dict[str, Any]) -> AsrEngine:
    """取当前配置对应的引擎；配置变了就换一个（旧的直接丢给 GC）。"""
    global _ENGINE, _ENGINE_KEY
    key = config_key(cfg)
    if _ENGINE is not None and key == _ENGINE_KEY:
        return _ENGINE
    if _ENGINE is not None:
        _ENGINE.unload()
    _ENGINE = make_engine(cfg)
    _ENGINE_KEY = key
    return _ENGINE


def release(cfg: dict[str, Any], *, now: float | None = None) -> bool:
    """按 `asr.resident` 决定是否卸载；返回是否真的卸载了。"""
    global _ENGINE, _ENGINE_KEY, _LAST_USED
    if _ENGINE is None:
        return False
    _LAST_USED = time.monotonic() if now is None else now
    seconds = config_mod.RESIDENT_SECONDS.get(str(cfg["asr"]["resident"]))
    if seconds is None:  # forever
        return False
    if seconds == 0:  # none：每次用完就卸
        _ENGINE.unload()
        _ENGINE = None
        _ENGINE_KEY = None
        return True
    return False


def maybe_unload(cfg: dict[str, Any], *, now: float | None = None) -> bool:
    """常驻超时（10min/30min）；daemon 的定时器调它。"""
    global _ENGINE, _ENGINE_KEY
    if _ENGINE is None:
        return False
    seconds = config_mod.RESIDENT_SECONDS.get(str(cfg["asr"]["resident"]))
    if seconds is None or seconds == 0:
        return False
    if (time.monotonic() if now is None else now) - _LAST_USED < seconds:
        return False
    _ENGINE.unload()
    _ENGINE = None
    _ENGINE_KEY = None
    return True


def status() -> dict[str, Any]:
    return {
        "loaded": _ENGINE is not None and _ENGINE.loaded,
        "engine": _ENGINE_KEY[0] if _ENGINE_KEY else None,
        "variant": _ENGINE.variant if _ENGINE else None,
        "threads": _ENGINE.threads if _ENGINE else None,
        "load_ms": _ENGINE.load_ms if _ENGINE else None,
        "idle_seconds": round(time.monotonic() - _LAST_USED, 1) if _LAST_USED else None,
    }
