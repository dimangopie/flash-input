"""Kaldi 风格的 80 维 log-mel fbank + LFR（低帧率）+ CMVN。

三件事都和 k2-fsa/sherpa-onnx 保持一致（它们决定了 ONNX 模型能不能认对字）：

1. **fbank**：25 ms 窗 / 10 ms 移、Povey 窗、去直流、预加重 0.97、512 点 FFT、
   80 个 mel 滤波器（20 Hz ~ 8 kHz）、取自然对数、`snip_edges=True`。
2. **LFR**（Low Frame Rate）：把相邻 ``window_size`` 帧拼成一帧、每次跳
   ``window_shift`` 帧，给 SenseVoice / Paraformer 用。
3. **CMVN**：``(x + mean) * istd``，其中 mean 取模型的负均值矩阵、istd 取倒标准差。

只有 numpy，不用 kaldi-native-fbank。
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000
FRAME_LENGTH_MS = 25
FRAME_SHIFT_MS = 10
N_MELS = 80
FFT_SIZE = 512
PREEMPHASIS = 0.97
LOW_FREQ = 20.0
HIGH_FREQ = -400.0  # Kaldi 约定：0 表示 Nyquist，负数表示 Nyquist+该值
_EPS = np.finfo(np.float32).eps


def _mel(freq: np.ndarray) -> np.ndarray:
    return 1127.0 * np.log1p(freq / 700.0)


def _mel_banks(high_freq: float = HIGH_FREQ) -> np.ndarray:
    """(N_MELS, FFT_SIZE//2+1) 的三角滤波器组（每行一个滤波器）。

    ``high_freq`` 沿用 Kaldi 语义：正数 = 该 Hz；0 = Nyquist；
    负数 = Nyquist + high_freq（-400 → 7600 Hz，sherpa 的默认值）。
    """
    nyquist = SAMPLE_RATE / 2.0
    high = high_freq if high_freq > 0 else (nyquist + high_freq if high_freq < 0 else nyquist)
    mel_low, mel_high = _mel(np.array(LOW_FREQ)), _mel(np.array(high))
    mel_points = np.linspace(mel_low, mel_high, N_MELS + 2)
    hz_points = 700.0 * np.expm1(mel_points / 1127.0)
    bin_width = SAMPLE_RATE / FFT_SIZE
    bins = hz_points / bin_width

    banks = np.zeros((N_MELS, FFT_SIZE // 2 + 1), dtype=np.float64)
    for i in range(N_MELS):
        left, center, right = bins[i], bins[i + 1], bins[i + 2]
        for k in range(int(np.floor(left)), int(np.ceil(right)) + 1):
            if k < 0 or k > FFT_SIZE // 2:
                continue
            if k <= center:
                weight = (k - left) / max(center - left, 1e-12)
            else:
                weight = (right - k) / max(right - center, 1e-12)
            banks[i, k] = max(0.0, weight)
    return banks


_BANKS: dict[float, np.ndarray] = {}


def _banks(high_freq: float) -> np.ndarray:
    if high_freq not in _BANKS:
        _BANKS[high_freq] = _mel_banks(high_freq)
    return _BANKS[high_freq]


_FRAME_LEN = FRAME_LENGTH_MS * SAMPLE_RATE // 1000


def _window(name: str) -> np.ndarray:
    """Kaldi 的窗函数：povey 是默认，Paraformer 要求 hamming。"""
    n = np.arange(_FRAME_LEN, dtype=np.float64)
    if name == "hamming":
        return (0.54 - 0.46 * np.cos(2 * np.pi * n / (_FRAME_LEN - 1))).astype(np.float64)
    povey = np.power(0.5 - 0.5 * np.cos(2 * np.pi * n / (_FRAME_LEN - 1)), 0.85)
    return povey.astype(np.float64)


_WINDOWS = {name: _window(name) for name in ("povey", "hamming")}


def _reflect(signal: np.ndarray, start: int, length: int) -> np.ndarray:
    """按 Kaldi ``snip_edges=false`` 的方式取窗：越界处镜像反射。"""
    size = signal.size
    positions = np.arange(start, start + length)
    # 反射：-1 -> 1, -2 -> 2, n -> n-2 …（Kaldi 的 Reflect 不翻转符号）
    period = max(size - 1, 1)
    positions = np.abs(positions)
    positions = np.mod(positions, 2 * period)
    positions = np.where(positions > period, 2 * period - positions, positions)
    return signal[positions]


def fbank(
    audio: np.ndarray,
    *,
    snip_edges: bool = False,
    window: str = "povey",
    high_freq: float = HIGH_FREQ,
) -> np.ndarray:
    """16 kHz 单声道 float32 → ``(T, 80)`` log-mel。

    ``snip_edges=False`` 与 sherpa-onnx / Kaldi 默认一致：帧中心落在
    ``i * shift``，两端用镜像补；``True`` 则首帧从采样点 0 开始（老实现）。
    """
    signal = np.asarray(audio, dtype=np.float64).reshape(-1)
    if signal.size == 0:
        return np.zeros((0, N_MELS), dtype=np.float32)

    # 波形级预加重（Kaldi 的顺序：dither → preemphasis → 分帧）
    preemphasized = np.empty_like(signal)
    preemphasized[0] = signal[0]
    preemphasized[1:] = signal[1:] - PREEMPHASIS * signal[:-1]

    frame_len = FRAME_LENGTH_MS * SAMPLE_RATE // 1000  # 400
    frame_shift = FRAME_SHIFT_MS * SAMPLE_RATE // 1000  # 160
    if snip_edges:
        if preemphasized.size < frame_len:
            return np.zeros((0, N_MELS), dtype=np.float32)
        num_frames = 1 + (preemphasized.size - frame_len) // frame_shift
        idx = np.arange(frame_len)[None, :] + frame_shift * np.arange(num_frames)[:, None]
        frames = preemphasized[idx]
    else:
        num_frames = (preemphasized.size + frame_shift // 2) // frame_shift
        if num_frames <= 0:
            return np.zeros((0, N_MELS), dtype=np.float32)
        first = -frame_len // 2
        frames = np.stack(
            [_reflect(preemphasized, first + i * frame_shift, frame_len) for i in range(num_frames)]
        )
    frames = frames - frames.mean(axis=1, keepdims=True)  # remove_dc_offset
    frames *= _WINDOWS.get(window, _WINDOWS["povey"])

    spectrum = np.fft.rfft(frames, n=FFT_SIZE, axis=1)
    power = spectrum.real**2 + spectrum.imag**2
    mel = power @ _banks(high_freq).T
    return np.log(np.maximum(mel, _EPS)).astype(np.float32)


def apply_lfr(features: np.ndarray, window_size: int, window_shift: int) -> np.ndarray:
    """``(T, D)`` → ``(T', D*window_size)``，与 sherpa-onnx 的 ``ApplyLfr`` 一致。

    第 i 帧以原始第 ``i * window_shift`` 帧为中心，左右各取 ``window_size/2``，
    越界处重复首/末帧；输出帧数 ``1 + (T - 1) // window_shift``。
    """
    frames = np.asarray(features, dtype=np.float32)
    if window_size <= 1:
        return frames
    if frames.shape[0] == 0:
        return np.zeros((0, frames.shape[1] * window_size), dtype=np.float32)
    total = frames.shape[0]
    dim = frames.shape[1]
    out_frames = 1 + (total - 1) // window_shift
    left = (window_size - 1) // 2
    out = np.empty((out_frames, dim * window_size), dtype=np.float32)
    for i in range(out_frames):
        center = i * window_shift
        left_pad = left - center if center < left else 0
        first = 0 if center < left else center - left
        max_offset = total - 1 - first
        cursor = 0
        for j in range(window_size):
            if j < left_pad:
                index = 0
            else:
                offset = j - left_pad
                index = total - 1 if offset > max_offset else first + offset
            out[i, cursor : cursor + dim] = frames[index]
            cursor += dim
    return out


def apply_cmvn(features: np.ndarray, neg_mean: np.ndarray, inv_stddev: np.ndarray) -> np.ndarray:
    """Kaldi CMVN：``(x + neg_mean) * inv_stddev``（逐维）。"""
    return ((features + neg_mean) * inv_stddev).astype(np.float32)


def load_tokens(path) -> list[str]:
    """读 ``tokens.txt``：每行 ``<符号> <id>``（符号可能是空格本身）。"""
    tokens: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            symbol, _, _idx = line.rpartition(" ")
            tokens.append(symbol)
    return tokens


def ctc_greedy(logits: np.ndarray, blank_id: int = 0) -> list[int]:
    """``(T, V)`` logits → 去重、去 blank 的 token 序列。"""
    ids = logits.argmax(axis=-1)
    out: list[int] = []
    prev = -1
    for idx in ids.tolist():
        if idx != prev and idx != blank_id:
            out.append(idx)
        prev = idx
    return out
