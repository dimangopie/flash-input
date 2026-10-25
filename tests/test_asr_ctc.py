"""CTC 引擎（SenseVoice / Paraformer）的预处理、解码与登记表。"""

from __future__ import annotations

import numpy as np
import pytest

from flash_input import asr_ctc, asr_models, config, fbank


def test_fbank_snip_edges_off_centers_first_frame_on_sample_zero():
    audio = np.zeros(16000, dtype=np.float32)
    snip = fbank.fbank(audio, snip_edges=True)
    centered = fbank.fbank(audio, snip_edges=False)
    assert snip.shape[1] == 80 and centered.shape[1] == 80
    # 不切边时帧数按 round(样本数 / shift) 算，比切边模式多
    assert centered.shape[0] > snip.shape[0]


def test_fbank_hamming_window_is_selectable():
    assert fbank.fbank(np.zeros(1600, np.float32)).shape[1] == 80
    assert fbank.fbank(np.zeros(1600, np.float32), window="hamming").shape[1] == 80


def test_lfr_is_centered_on_frame_centers():
    frames = np.arange(10, dtype=np.float32)[:, None]
    out = fbank.apply_lfr(frames, 7, 6)
    assert out.shape == (1 + (10 - 1) // 6, 7)
    assert out[0].tolist() == [0.0, 0.0, 0.0, 0.0, 1.0, 2.0, 3.0]


def test_lfr_repeats_last_frame_at_the_tail():
    frames = np.arange(8, dtype=np.float32)[:, None]
    out = fbank.apply_lfr(frames, 7, 6)
    assert out[-1].tolist() == [3.0, 4.0, 5.0, 6.0, 7.0, 7.0, 7.0]


def test_detokenize_drops_sensevoice_prefix_and_specials():
    tokens = ["<unk>", "<s>", "</s>", "<|zh|>", "你", "好", "▁world"]
    ids = [3, 1, 2, 3, 4, 5, 6]
    assert asr_ctc.detokenize(ids, tokens, drop=4, min_id=4) == "你好 world"


def test_detokenize_paraformer_keeps_first_real_token():
    tokens = ["<blank>", "<s>", "</s>", "<OOV>", "对", "我"]
    assert asr_ctc.detokenize([4, 5], tokens, drop=0, min_id=4) == "对我"


def test_load_tokens_keeps_symbols_with_spaces(tmp_path):
    path = tmp_path / "tokens.txt"
    path.write_text("a b 0\n你 1\n", encoding="utf-8")
    assert asr_ctc.load_tokens(path) == ["a b", "你"]


def test_ctc_engine_rejects_unknown_engine():
    with pytest.raises(asr_ctc.CtcAsrError):
        asr_ctc.CtcEngine("whisper")


def test_ctc_engine_reports_missing_model_files(tmp_path):
    engine = asr_ctc.CtcEngine("sensevoice", tmp_path)
    with pytest.raises(asr_ctc.CtcAsrError) as exc:
        engine.load()
    assert "models fetch" in str(exc.value)


def test_models_registry_exposes_three_engines():
    assert set(asr_models.ENGINES) == {"sensevoice", "paraformer", "zipformer"}
    for info in asr_models.ENGINES.values():
        assert any(f.endswith(".onnx") for f in info["files"])
        assert "tokens.txt" in info["files"]
        assert info["repo"].startswith("csukuangfj/")


def test_engine_dir_follows_flash_input_model_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASH_INPUT_MODEL_DIR", str(tmp_path))
    assert asr_models.engine_dir("sensevoice") == tmp_path / "sensevoice"
    assert asr_models.is_ready("sensevoice") is False
    (tmp_path / "sensevoice").mkdir()
    for name in asr_models.ENGINES["sensevoice"]["files"]:
        (tmp_path / "sensevoice" / name).write_bytes(b"x")
    assert asr_models.is_ready("sensevoice") is True


def test_config_validates_model_and_language():
    cfg = config.DEFAULTS.copy()
    cfg["asr"] = dict(cfg["asr"])
    cfg["asr"]["model"] = "whisper"
    with pytest.raises(config.ConfigError):
        config.validate(cfg)
    cfg["asr"]["model"] = "sensevoice"
    cfg["asr"]["language"] = "fr"
    with pytest.raises(config.ConfigError):
        config.validate(cfg)
    cfg["asr"]["language"] = "zh"
    config.validate(cfg)
