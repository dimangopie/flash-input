"""注入层测试：事件序列、模式与降级、锁屏跳过、uinput 不可用时的行为。

真正的 `/dev/uinput` 写入只能在带权限的桌面会话里验证（`finput inject test`），
这里用替身把「决策逻辑」钉住，纯逻辑不留死角。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from flash_input import config as config_mod
from flash_input import inject


@pytest.fixture()
def cfg():
    return config_mod._deep_merge(config_mod.DEFAULTS, {"inject": {"paste_delay_ms": 0}})


def test_event_sequence_order():
    seq = inject.event_sequence(inject.modifier_keys("ctrl+v"))
    assert seq == [
        (inject.EV_KEY, inject.KEY_LEFTCTRL, 1),
        (inject.EV_KEY, inject.KEY_V, 1),
        (inject.EV_KEY, inject.KEY_V, 0),
        (inject.EV_KEY, inject.KEY_LEFTCTRL, 0),
    ]


def test_event_sequence_releases_modifiers_in_reverse():
    seq = inject.event_sequence(inject.modifier_keys("ctrl+shift+v"))
    assert [code for _, code, _ in seq] == [
        inject.KEY_LEFTCTRL,
        inject.KEY_LEFTSHIFT,
        inject.KEY_V,
        inject.KEY_V,
        inject.KEY_LEFTSHIFT,
        inject.KEY_LEFTCTRL,
    ]
    assert [value for _, _, value in seq] == [1, 1, 1, 0, 0, 0]


def test_paste_none_sends_nothing(monkeypatch):
    clock = {"slept": 0.0}
    monkeypatch.setattr(
        inject.time, "sleep", lambda seconds: clock.__setitem__("slept", clock["slept"] + seconds)
    )
    inject.paste("none")
    assert clock["slept"] == 0.0  # 没建设备也没等待


def test_unknown_paste_key():
    with pytest.raises(inject.InjectError):
        inject.modifier_keys("meta+v")


def test_config_and_inject_agree_on_paste_keys():
    """配置允许的组合键必须正好是注入层认识的那几个，避免两边漂移。"""
    allowed = set(inject.PASTE_COMBOS)
    assert allowed == {"ctrl+v", "ctrl+shift+v", "ctrl+alt+v", "shift+insert", "none"}


def test_input_event_struct_is_64bit_layout():
    assert inject._EVENT.size == 24


def test_deliver_clipboard_mode(monkeypatch, cfg):
    calls: dict[str, object] = {}

    def fake_copy(text: str) -> str:
        calls["text"] = text
        return "wl-copy"

    monkeypatch.setattr(inject.clipboard, "copy_text", fake_copy)
    monkeypatch.setattr(inject, "session_locked", lambda: False)
    result = inject.deliver("你好", cfg)
    assert result.mode == "clipboard"
    assert result.clipboard == "wl-copy"
    assert calls["text"] == "你好"


def test_deliver_auto_without_uinput_falls_back(monkeypatch, cfg):
    monkeypatch.setattr(inject.clipboard, "copy_text", lambda text: "xclip")
    monkeypatch.setattr(inject, "session_locked", lambda: False)
    monkeypatch.setattr(inject, "available", lambda: False)
    result = inject.deliver("hi", cfg)
    assert result.mode == "clipboard"
    assert "uinput" in result.note


def test_deliver_auto_with_uinput_pastes(monkeypatch, cfg):
    pasted: list[str] = []
    monkeypatch.setattr(inject.clipboard, "copy_text", lambda text: "wl-copy")
    monkeypatch.setattr(inject, "session_locked", lambda: False)
    monkeypatch.setattr(inject, "available", lambda: True)
    monkeypatch.setattr(inject, "paste", lambda key="shift+insert": pasted.append(key))
    result = inject.deliver("hi", cfg)
    assert result.mode == "uinput"
    assert pasted == ["shift+insert"]


def test_deliver_auto_degrades_when_paste_raises(monkeypatch, cfg):
    """auto 模式下真发键失败（如 uinput 建建设备报 EINVAL）不该丢掉转写结果。"""
    monkeypatch.setattr(inject.clipboard, "copy_text", lambda text: "wl-copy")
    monkeypatch.setattr(inject, "session_locked", lambda: False)
    monkeypatch.setattr(inject, "available", lambda: True)
    monkeypatch.setattr(
        inject, "paste", lambda key="ctrl+v": (_ for _ in ()).throw(inject.InjectError("EINVAL"))
    )
    result = inject.deliver("hi", cfg)
    assert result.mode == "clipboard"
    assert "粘贴失败" in result.note


def test_deliver_forced_uinput_keeps_error(monkeypatch, cfg):
    monkeypatch.setattr(inject.clipboard, "copy_text", lambda text: "wl-copy")
    monkeypatch.setattr(inject, "session_locked", lambda: False)
    monkeypatch.setattr(inject, "available", lambda: True)
    monkeypatch.setattr(
        inject, "paste", lambda key="ctrl+v": (_ for _ in ()).throw(inject.InjectError("EINVAL"))
    )
    import pytest

    with pytest.raises(inject.InjectError):
        inject.deliver("hi", cfg, force_mode="uinput")


def test_deliver_locked_skips_paste(monkeypatch, cfg):
    pasted: list[str] = []
    monkeypatch.setattr(inject.clipboard, "copy_text", lambda text: "wl-copy")
    monkeypatch.setattr(inject, "session_locked", lambda: True)
    monkeypatch.setattr(inject, "available", lambda: True)
    monkeypatch.setattr(inject, "paste", lambda key="ctrl+v": pasted.append(key))
    result = inject.deliver("hi", cfg)
    assert result.mode == "clipboard"
    assert "锁屏" in result.note
    assert pasted == []


def test_deliver_forced_uinput_raises_when_unavailable(monkeypatch, cfg):
    monkeypatch.setattr(inject.clipboard, "copy_text", lambda text: "wl-copy")
    monkeypatch.setattr(inject, "session_locked", lambda: False)
    monkeypatch.setattr(inject, "available", lambda: False)
    with pytest.raises(inject.InjectUnavailable):
        inject.deliver("hi", cfg, force_mode="uinput")


def test_deliver_paste_key_none_keeps_clipboard(monkeypatch, cfg):
    cfg["inject"]["paste_key"] = "none"
    monkeypatch.setattr(inject.clipboard, "copy_text", lambda text: "wl-copy")
    monkeypatch.setattr(inject, "session_locked", lambda: False)
    monkeypatch.setattr(inject, "available", lambda: True)
    result = inject.deliver("hi", cfg)
    assert result.mode == "clipboard"
    assert "none" in result.note


def test_deliver_rejects_unknown_mode(monkeypatch, cfg):
    monkeypatch.setattr(inject.clipboard, "copy_text", lambda text: "wl-copy")
    with pytest.raises(inject.InjectError):
        inject.deliver("hi", cfg, force_mode="magic")


def test_paste_without_uinput_gives_hint(monkeypatch):
    monkeypatch.setattr(inject.env, "uinput_present", lambda: False)
    with pytest.raises(inject.InjectUnavailable) as exc:
        inject.paste("ctrl+v")
    assert "/dev/uinput" in str(exc.value)


def test_deliver_paste_false_only_writes_clipboard(monkeypatch, cfg):
    """语音 / OCR 各自的「自动粘贴」关掉时，只写剪贴板、不发键。"""
    monkeypatch.setattr(inject.clipboard, "copy_text", lambda text: "wl-copy")
    monkeypatch.setattr(inject, "available", lambda: True)
    sent = []
    monkeypatch.setattr(inject, "paste", lambda key: sent.append(key))

    result = inject.deliver("你好", cfg, paste_enabled=False)

    assert result.mode == "clipboard"
    assert result.clipboard == "wl-copy"
    assert sent == []


def test_shift_insert_uses_insert_as_main_key():
    modifiers, key = inject.paste_combo("shift+insert")
    assert modifiers == (inject.KEY_LEFTSHIFT,)
    assert key == inject.KEY_INSERT
    assert inject.event_sequence(modifiers, key) == [
        (inject.EV_KEY, inject.KEY_LEFTSHIFT, 1),
        (inject.EV_KEY, inject.KEY_INSERT, 1),
        (inject.EV_KEY, inject.KEY_INSERT, 0),
        (inject.EV_KEY, inject.KEY_LEFTSHIFT, 0),
    ]


def test_shift_insert_is_a_valid_paste_key(cfg):
    cfg["inject"]["paste_key"] = "shift+insert"
    config_mod.validate(cfg)  # 不抛就是合法
