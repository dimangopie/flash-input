"""键名解析与校验：网页录制与后台校验共用一份规则。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from flash_input import config as config_mod
from flash_input import keys


def test_parse_and_normalize_order():
    assert keys.parse("KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O") == [
        "KEY_LEFTMETA",
        "KEY_LEFTSHIFT",
        "KEY_O",
    ]
    assert keys.validate_combo("KEY_F9") == "KEY_F9"
    assert keys.validate_combo("KEY_RIGHTCTRL+KEY_V") == "KEY_RIGHTCTRL+KEY_V"


@pytest.mark.parametrize(
    "combo",
    ["", "KEY_O+", "+KEY_O", "KEY_FOO", "KEY_LEFTCTRL+KEY_LEFTCTRL+KEY_O", "O", "ctrl+v"],
)
def test_parse_rejects(combo):
    with pytest.raises(keys.KeyError):
        keys.parse(combo)


@pytest.mark.parametrize("combo", ["KEY_LEFTCTRL", "KEY_RIGHTMETA"])
def test_hold_key_accepts_modifiers(combo):
    assert keys.validate_hold_key(combo) == combo


@pytest.mark.parametrize("combo", ["KEY_O", "KEY_F9", "KEY_LEFTMETA+KEY_O"])
def test_hold_key_rejects_non_modifier_or_combo(combo):
    with pytest.raises(keys.KeyError):
        keys.validate_hold_key(combo)


@pytest.mark.parametrize(
    "combo", ["KEY_LEFTMETA+KEY_LEFTMETA", "KEY_O+KEY_LEFTMETA", "KEY_LEFTCTRL+KEY_LEFTSHIFT"]
)
def test_combo_rejects(combo):
    with pytest.raises(keys.KeyError):
        keys.validate_combo(combo)


def test_describe_is_readable():
    assert keys.describe("KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O") == "Super+Shift+O"
    assert keys.describe("KEY_RIGHTCTRL") == "RCtrl"


def test_config_validation_uses_keys():
    good = config_mod._deep_merge(config_mod.DEFAULTS, {})
    config_mod.validate(good)

    bad = config_mod._deep_merge(config_mod.DEFAULTS, {"hotkey": {"voice_key": "KEY_O"}})
    with pytest.raises(config_mod.ConfigError):
        config_mod.validate(bad)

    bad = config_mod._deep_merge(config_mod.DEFAULTS, {"hotkey": {"ocr_key": "KEY_LEFTMETA"}})
    with pytest.raises(config_mod.ConfigError):
        config_mod.validate(bad)


def test_set_path_validates_hotkey(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASH_INPUT_CONFIG", str(tmp_path / "config.json"))
    cfg = config_mod.load()
    cfg2 = config_mod.set_path(cfg, "hotkey.voice_key", "KEY_RIGHTALT")
    assert cfg2["hotkey"]["voice_key"] == "KEY_RIGHTALT"
    with pytest.raises(config_mod.ConfigError):
        config_mod.set_path(cfg, "hotkey.voice_key", "KEY_F9")


def test_to_accelerator_orders_modifiers_and_lowercases_main_key():
    keys = __import__("flash_input.keys", fromlist=["keys"])
    assert keys.to_accelerator("KEY_LEFTALT+KEY_LEFTCTRL+KEY_K") == "<Alt><Primary>k"
    assert keys.to_accelerator("KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O") == "<Super><Shift>o"


def test_to_accelerator_handles_function_and_special_keys():
    keys = __import__("flash_input.keys", fromlist=["keys"])
    assert keys.to_accelerator("KEY_F13") == "f13"
    assert keys.to_accelerator("KEY_SYSRQ") == "Print"
    assert keys.to_accelerator("KEY_LEFTSHIFT+KEY_INSERT") == "<Shift>Insert"


def test_to_accelerator_rejects_bare_modifier():
    """语音键那种「单个修饰键」转不成加速键（GNOME 只能绑组合键）。"""
    import pytest

    keys = __import__("flash_input.keys", fromlist=["keys"])
    with pytest.raises(keys.KeyError):
        keys.to_accelerator("KEY_RIGHTCTRL")


def test_to_accelerator_rejects_two_main_keys():
    import pytest

    keys = __import__("flash_input.keys", fromlist=["keys"])
    with pytest.raises(keys.KeyError):
        keys.to_accelerator("KEY_LEFTCTRL+KEY_A+KEY_B")
