"""Whisper 风格 log-mel 频谱（纯 numpy）。

Qwen3-ASR 的训练输入就是 Whisper 的特征，所以这里必须和
``transformers.WhisperFeatureExtractor`` 保持一致：
``n_fft=400, hop=160, n_mels=128, 16 kHz, slaney mel, 周期 Hann, center=True,
reflect pad, 30 s 对齐``。

参数来自模型仓库里的 ``preprocessor_config.json``；写死成常量是因为这份
ONNX 模型只有这一种输入规格，读配置只会多一个漂移点。
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000
N_FFT = 400
HOP_LENGTH = 160
N_MELS = 128
CHUNK_SECONDS = 30
CHUNK_SAMPLES = CHUNK_SECONDS * SAMPLE_RATE  # 480000


def _hz_to_mel_slaney(hz: np.ndarray) -> np.ndarray:
    hz = np.asarray(hz, dtype=np.float64)
    f_sp = 200.0 / 3.0
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    log_mask = hz >= min_log_hz
    safe_hz = np.where(log_mask, hz, min_log_hz)
    log_part = min_log_mel + np.log(safe_hz / min_log_hz) / logstep
    return np.where(log_mask, log_part, hz / f_sp)


def _mel_to_hz_slaney(mels: np.ndarray) -> np.ndarray:
    mels = np.asarray(mels, dtype=np.float64)
    f_sp = 200.0 / 3.0
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    log_mask = mels >= min_log_mel
    safe_mels = np.where(log_mask, mels, min_log_mel)
    log_part = min_log_hz * np.exp((safe_mels - min_log_mel) * logstep)
    return np.where(log_mask, log_part, mels * f_sp)


def _mel_filter_bank() -> np.ndarray:
    """Slaney 三角滤波器组 + Slaney 面积归一化，形状 (n_fft//2+1, n_mels)。"""
    num_bins = N_FFT // 2 + 1
    mel_freqs = np.linspace(
        _hz_to_mel_slaney(np.array(0.0)),
        _hz_to_mel_slaney(np.array(SAMPLE_RATE / 2.0)),
        N_MELS + 2,
    )
    filter_freqs = _mel_to_hz_slaney(mel_freqs)
    fft_freqs = np.linspace(0.0, SAMPLE_RATE / 2.0, num_bins)

    diff = np.diff(filter_freqs)
    slopes = filter_freqs[np.newaxis, :] - fft_freqs[:, np.newaxis]
    down = -slopes[:, :-2] / diff[:-1]
    up = slopes[:, 2:] / diff[1:]
    filters = np.maximum(0.0, np.minimum(down, up))
    enorm = 2.0 / (filter_freqs[2 : N_MELS + 2] - filter_freqs[:N_MELS])
    filters = filters * enorm[np.newaxis, :]
    return filters.astype(np.float32)


def _hann_window() -> np.ndarray:
    """周期 Hann 窗（torch.hann_window(periodic=True) 的语义）。"""
    n = np.arange(N_FFT, dtype=np.float64)
    return (0.5 - 0.5 * np.cos(2.0 * np.pi * n / N_FFT)).astype(np.float32)


_MEL_FILTERS = _mel_filter_bank()
_WINDOW = _hann_window()


def pad_or_trim(audio: np.ndarray, length: int = CHUNK_SAMPLES) -> np.ndarray:
    """尾部补零或截断到恰好 ``length`` 个采样点（30 s）。"""
    if audio.shape[-1] > length:
        return audio[..., :length]
    if audio.shape[-1] < length:
        pad = [(0, 0)] * (audio.ndim - 1) + [(0, length - audio.shape[-1])]
        return np.pad(audio, pad, mode="constant", constant_values=0.0)
    return audio


def log_mel_spectrogram(audio: np.ndarray) -> np.ndarray:
    """``(N_MELS, n_frames)`` float32；30 s 输入得到 3000 帧。"""
    if audio.ndim != 1:
        raise ValueError(f"log_mel_spectrogram 只接受一维音频，实际 {audio.shape}")
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    pad = N_FFT // 2
    padded = np.pad(audio, pad, mode="reflect")
    n_frames = 1 + (padded.shape[0] - N_FFT) // HOP_LENGTH
    stride = padded.strides[0]
    frames = np.lib.stride_tricks.as_strided(
        padded, shape=(n_frames, N_FFT), strides=(stride * HOP_LENGTH, stride)
    )
    frames = frames * _WINDOW

    stft = np.fft.rfft(frames, n=N_FFT, axis=1)
    power = stft.real.astype(np.float32) ** 2 + stft.imag.astype(np.float32) ** 2
    mel = power @ _MEL_FILTERS
    mel = mel[:-1, :]  # 丢掉最后一帧，与 whisper 对齐

    log_spec = np.log10(np.maximum(mel, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec.T.astype(np.float32)
