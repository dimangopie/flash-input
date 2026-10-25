"""语音链路测试：特征、分词器/提示词、音频解码、常驻策略、录音裁剪。

真正的端到端转写需要 ~1 GB 模型，不在单测里跑；这里把不依赖模型的纯逻辑钉住，
模型相关路径由 `tests` 之外的手工验证（`finput voice --file`）覆盖。
"""

from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pytest

from flash_input import asr, features, models, recorder
from flash_input import config as config_mod


@pytest.fixture()
def cfg(monkeypatch, tmp_path):
    data = config_mod.load()
    data["asr"]["resident"] = "none"
    data["asr"]["threads"] = 4
    return data


# --------------------------------------------------------------------------- 特征


def test_pad_or_trim_lengths():
    short = np.ones(1000, dtype=np.float32)
    assert features.pad_or_trim(short).shape == (features.CHUNK_SAMPLES,)
    long = np.ones(features.CHUNK_SAMPLES * 2, dtype=np.float32)
    assert features.pad_or_trim(long).shape == (features.CHUNK_SAMPLES,)


def test_log_mel_shape_and_range():
    rng = np.random.default_rng(0)
    audio = rng.standard_normal(features.SAMPLE_RATE).astype(np.float32) * 0.1
    mel = features.log_mel_spectrogram(features.pad_or_trim(audio))
    assert mel.shape == (features.N_MELS, 3000)
    assert mel.dtype == np.float32
    assert np.isfinite(mel).all()
    assert mel.min() >= -1.2 and mel.max() <= 1.5  # 归一化后的经验区间（尾部补零会到 -1.06）


def test_log_mel_rejects_stereo():
    with pytest.raises(ValueError):
        features.log_mel_spectrogram(np.zeros((2, 16000), dtype=np.float32))


# --------------------------------------------------------------------------- 提示词 / 输出清洗


def test_build_prompt_expands_audio_pads():
    prompt = asr.build_prompt(3)
    assert prompt.count(asr.AUDIO_PAD) == 3
    assert prompt.startswith(asr.IM_START + "system")
    assert prompt.endswith(asr.IM_START + "assistant\n")


def test_build_prompt_rejects_zero():
    with pytest.raises(ValueError):
        asr.build_prompt(0)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<asr_text>你好世界", "你好世界"),
        ("<asr_text>你好世界<|im_end|>", "你好世界"),
        ("你好世界<|endoftext|>", "你好世界"),
        ("  <asr_text>  hi  ", "hi"),
        ("", ""),
    ],
)
def test_parse_asr_output(raw, expected):
    assert asr.parse_asr_output(raw) == expected


# --------------------------------------------------------------------------- 音频解码


def _wav_bytes(samples: np.ndarray, rate: int = 16000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        data = np.clip(samples * 32767, -32768, 32767).astype(np.int16)
        wf.writeframes(data.tobytes())
    return buf.getvalue()


def test_wav_to_float32_roundtrip():
    samples = np.array([0.0, 0.5, -0.5, 0.25], dtype=np.float32)
    audio = asr.wav_to_float32(_wav_bytes(samples))
    assert audio.dtype == np.float32
    np.testing.assert_allclose(audio, samples, atol=1e-4)


def test_wav_to_float32_mixes_stereo():
    stereo = np.array([[0.5, -0.5], [1.0, 0.0]], dtype=np.float32)
    flatted = stereo.reshape(-1)
    audio = asr.wav_to_float32(_wav_bytes(flatted, channels=2))
    np.testing.assert_allclose(audio, [0.0, 0.5], atol=1e-4)


def test_wav_to_float32_rejects_wrong_rate():
    with pytest.raises(asr.AsrError):
        asr.wav_to_float32(_wav_bytes(np.zeros(100, dtype=np.float32), rate=44100))


def test_load_audio_file(tmp_path):
    path = tmp_path / "a.wav"
    path.write_bytes(_wav_bytes(np.full(1600, 0.1, dtype=np.float32)))
    audio = asr.load_audio_file(path)
    assert audio.shape == (1600,)


# --------------------------------------------------------------------------- 引擎与常驻


def test_transcribe_short_audio_returns_empty_without_loading(cfg):
    engine = asr.AsrEngine(warmup=False)
    assert engine.transcribe(np.zeros(10, dtype=np.float32)) == ""
    assert not engine.loaded


def test_engine_load_without_model_raises(monkeypatch):
    monkeypatch.setattr(models, "find_root", lambda variant=None: None)
    engine = asr.AsrEngine(warmup=False)
    with pytest.raises(asr.AsrError) as exc:
        engine.load()
    assert "models fetch" in str(exc.value)


def test_release_unloads_when_resident_none(cfg, monkeypatch):
    engine = asr.AsrEngine(warmup=False)
    monkeypatch.setattr(asr, "_ENGINE", engine)
    monkeypatch.setattr(asr, "_ENGINE_KEY", (cfg["asr"]["model"], "auto", cfg["asr"]["threads"]))
    assert asr.release(cfg) is True
    assert asr._ENGINE is None


def test_release_keeps_engine_when_resident_10min(cfg, monkeypatch):
    cfg["asr"]["resident"] = "10min"
    engine = asr.AsrEngine(warmup=False)
    monkeypatch.setattr(asr, "_ENGINE", engine)
    assert asr.release(cfg) is False
    assert asr._ENGINE is engine


def test_maybe_unload_respects_timeout(cfg, monkeypatch):
    cfg["asr"]["resident"] = "10min"
    engine = asr.AsrEngine(warmup=False)
    monkeypatch.setattr(asr, "_ENGINE", engine)
    monkeypatch.setattr(asr, "_LAST_USED", 1000.0)
    assert asr.maybe_unload(cfg, now=1000.0 + config_mod.RESIDENT_SECONDS["10min"] - 1) is False
    assert asr.maybe_unload(cfg, now=1000.0 + config_mod.RESIDENT_SECONDS["10min"] + 1) is True
    assert asr._ENGINE is None


def test_maybe_unload_forever_never():
    cfg = config_mod.load()
    cfg["asr"]["resident"] = "forever"
    engine = asr.AsrEngine(warmup=False)
    asr._ENGINE = engine
    assert asr.maybe_unload(cfg, now=10**9) is False
    asr._ENGINE = None


# --------------------------------------------------------------------------- 模型定位


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def _fake_cache_bases(tmp_path: Path):
    return [tmp_path / "modelscope"]


def test_models_find_root_detects_complete_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "_cache_bases", lambda: _fake_cache_bases(tmp_path))
    root = tmp_path / "modelscope" / "models" / models.REPO_ID.replace("/", "--") / "snapshots" / "master"
    for name in models.MODEL_FILES:
        _touch(root / "model_0.6B" / name)
    for name in models.TOKENIZER_FILES:
        _touch(root / "tokenizer" / name)

    assert models.find_root("0.6B") == root
    assert models.find_root("1.7B") is None
    info = models.describe("0.6B")
    assert info["present"] is True


def test_models_find_root_requires_tokenizer(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "_cache_bases", lambda: _fake_cache_bases(tmp_path))
    root = tmp_path / "modelscope" / "models" / models.REPO_ID.replace("/", "--") / "snapshots" / "master"
    for name in models.MODEL_FILES:
        _touch(root / "model_0.6B" / name)
    assert models.find_root("0.6B") is None  # tokenizer 缺 → 不算命中


# --------------------------------------------------------------------------- 录音工具


def test_trim_trailing_silence_removes_tail():
    rng = np.random.default_rng(1)
    speech = (rng.standard_normal(16000) * 0.2).astype(np.float32)
    silence = np.zeros(16000, dtype=np.float32)
    trimmed = recorder.trim_trailing_silence(np.concatenate([speech, silence]))
    assert trimmed.size < speech.size + silence.size
    assert trimmed.size >= speech.size


def test_trim_trailing_silence_all_silence_keeps_short_tail():
    trimmed = recorder.trim_trailing_silence(np.zeros(16000, dtype=np.float32))
    assert 0 < trimmed.size <= 16000


def test_recorder_duration():
    assert recorder.duration(np.zeros(16000, dtype=np.float32)) == 1.0


def test_recorder_rejects_bad_seconds():
    with pytest.raises(recorder.RecorderError):
        recorder.Recorder().record(0)
