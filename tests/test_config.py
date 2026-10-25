"""配置面的单元测试：默认值、点号路径、类型推断、校验、损坏文件的降级。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from flash_input import config as config_mod


@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASH_INPUT_CONFIG", str(tmp_path / "config.json"))
    return tmp_path / "config.json"


def test_defaults_are_valid():
    config_mod.validate(config_mod.DEFAULTS)


def test_load_missing_returns_defaults(cfg_file):
    cfg = config_mod.load()
    assert cfg["asr"]["resident"] == "forever"
    assert cfg["ocr"]["resident"] == "30min"
    assert cfg["web"]["port"] == 7331
    assert not cfg_file.exists()


def test_save_and_reload(cfg_file):
    cfg = config_mod.load()
    config_mod.save(config_mod.set_path(cfg, "asr.resident", "30min"))
    assert config_mod.load()["asr"]["resident"] == "30min"
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["asr"]["resident"] == "30min"


def test_resident_choices():
    cfg = config_mod.load()
    for choice in config_mod.RESIDENT_CHOICES:
        config_mod.validate(config_mod.set_path(cfg, "ocr.resident", choice))
    with pytest.raises(config_mod.ConfigError):
        config_mod.set_path(cfg, "ocr.resident", "5min")


def test_coerce_types():
    cfg = config_mod.load()
    assert config_mod.set_path(cfg, "asr.threads", "8")["asr"]["threads"] == 8
    with pytest.raises(config_mod.ConfigError):
        config_mod.set_path(cfg, "asr.threads", "many")
    with pytest.raises(config_mod.ConfigError):
        config_mod.set_path(cfg, "asr.threads", "0")


def test_unknown_key():
    with pytest.raises(config_mod.ConfigError):
        config_mod.set_path(config_mod.load(), "asr.nope", "1")


def test_web_host_must_be_loopback():
    cfg = config_mod.load()
    for host in ("127.0.0.1", "::1", "localhost"):
        config_mod.validate(config_mod.set_path(cfg, "web.host", host))
    for bad in ("0.0.0.0", "", "192.168.1.10", "::"):
        with pytest.raises(config_mod.ConfigError):
            config_mod.set_path(cfg, "web.host", bad)


def test_broken_file_falls_back(cfg_file):
    cfg_file.write_text("{ not json", encoding="utf-8")
    cfg = config_mod.load()
    assert cfg["hotkey"]["mode"] == "evdev"
    # 不覆盖用户文件
    assert cfg_file.read_text(encoding="utf-8") == "{ not json"


def test_invalid_value_falls_back(cfg_file):
    cfg_file.write_text(json.dumps({"hotkey": {"mode": "xlib"}}), encoding="utf-8")
    cfg = config_mod.load()
    assert cfg["hotkey"]["mode"] == "evdev"
