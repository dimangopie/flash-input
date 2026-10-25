"""热键状态机与设备抽象的单测（不需要真键盘）。"""

from __future__ import annotations

import contextlib
import time

from flash_input import hotkey, keys

T0 = 1000.0


def matcher(**kwargs) -> hotkey.Matcher:
    defaults = {
        "voice_key": "KEY_RIGHTCTRL",
        "ocr_key": "KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O",
        "guard_ms": 300,
        "min_hold_ms": 300,
        "max_hold_s": 60,
    }
    defaults.update(kwargs)
    return hotkey.Matcher(**defaults)


def test_event_struct_is_24_bytes():
    assert hotkey._EVENT.size == 24


def test_key_name_table_covers_all_supported_keys():
    for name in keys.SUPPORTED_KEYS:
        code = keys.KEY_CODES[name]
        assert hotkey._KEY_NAME_BY_CODE[code] == name


def test_hold_records_only_after_min_hold():
    m = matcher()
    assert m.feed("KEY_RIGHTCTRL", 1, T0) == []
    assert m.tick(T0 + 0.1) == []  # 还没到 300 ms：不录音
    assert m.tick(T0 + 0.31) == ["voice_start"]
    assert m.feed("KEY_RIGHTCTRL", 0, T0 + 1.0) == ["voice_stop"]


def test_short_tap_records_nothing():
    m = matcher()
    m.feed("KEY_RIGHTCTRL", 1, T0)
    assert m.feed("KEY_RIGHTCTRL", 0, T0 + 0.1) == []
    assert m.tick(T0 + 0.5) == []


def test_other_key_within_guard_cancels():
    m = matcher()
    m.feed("KEY_RIGHTCTRL", 1, T0)
    assert m.feed("KEY_C", 1, T0 + 0.1) == []  # Ctrl+C：判为组合键
    assert m.tick(T0 + 0.31) == []  # 不录音
    assert m.feed("KEY_C", 0, T0 + 0.2) == []
    assert m.feed("KEY_RIGHTCTRL", 0, T0 + 0.4) == []


def test_other_key_after_recording_starts_does_not_stop_it():
    m = matcher()
    m.feed("KEY_RIGHTCTRL", 1, T0)
    assert m.tick(T0 + 0.31) == ["voice_start"]
    assert m.feed("KEY_J", 1, T0 + 0.5) == []
    assert m.feed("KEY_RIGHTCTRL", 0, T0 + 1.0) == ["voice_stop"]


def test_max_hold_forces_stop():
    m = matcher(max_hold_s=5)
    m.feed("KEY_RIGHTCTRL", 1, T0)
    assert m.tick(T0 + 0.31) == ["voice_start"]
    assert m.tick(T0 + 4.0) == []
    assert m.tick(T0 + 5.1) == ["voice_stop"]
    assert m.feed("KEY_RIGHTCTRL", 0, T0 + 5.2) == []


def test_ocr_combo_needs_all_modifiers():
    m = matcher()
    m.feed("KEY_LEFTMETA", 1, T0)
    m.feed("KEY_LEFTSHIFT", 1, T0 + 0.05)
    assert m.feed("KEY_O", 1, T0 + 0.1) == ["ocr"]
    assert m.feed("KEY_O", 2, T0 + 0.15) == []  # 自动重复不重复触发
    assert m.feed("KEY_O", 1, T0 + 0.2) == []  # 没松手不重复触发


def test_ocr_combo_retriggers_after_release():
    m = matcher()
    m.feed("KEY_LEFTMETA", 1, T0)
    m.feed("KEY_LEFTSHIFT", 1, T0)
    assert m.feed("KEY_O", 1, T0) == ["ocr"]
    m.feed("KEY_O", 0, T0 + 0.1)
    assert m.feed("KEY_O", 1, T0 + 1.0) == ["ocr"]


def test_ocr_combo_without_modifier_does_not_fire():
    m = matcher()
    assert m.feed("KEY_O", 1, T0) == []


def test_ocr_combo_with_extra_modifier_fires_everything_held():
    m = matcher()
    m.feed("KEY_LEFTMETA", 1, T0)
    m.feed("KEY_LEFTSHIFT", 1, T0)
    m.feed("KEY_LEFTCTRL", 1, T0)
    assert m.feed("KEY_O", 1, T0) == ["ocr"]


def test_ocr_combo_cancels_pending_voice():
    m = matcher()
    m.feed("KEY_RIGHTCTRL", 1, T0)
    # 按住语音键的同时按下抓屏组合：抓屏赢，语音不启动
    m.feed("KEY_LEFTMETA", 1, T0 + 0.1)
    m.feed("KEY_LEFTSHIFT", 1, T0 + 0.1)
    assert m.feed("KEY_O", 1, T0 + 0.1) == ["ocr"]
    assert m.tick(T0 + 0.5) == []


def test_cancel_from_ipc():
    m = matcher()
    m.feed("KEY_RIGHTCTRL", 1, T0)
    m.tick(T0 + 0.31)
    assert m.cancel() is True
    assert m.feed("KEY_RIGHTCTRL", 0, T0 + 1.0) == []


def test_unknown_key_ignored():
    m = matcher()
    assert m.feed("KEY_THIS_IS_NOT_A_KEY", 1, T0) == []


def test_required_codes():
    codes = hotkey.required_codes("KEY_RIGHTCTRL", "KEY_LEFTMETA+KEY_O")
    assert codes == [keys.KEY_CODES["KEY_RIGHTCTRL"], keys.KEY_CODES["KEY_LEFTMETA"], keys.KEY_CODES["KEY_O"]]


def test_device_has_key_bitmap():
    device = hotkey.Device(path="/dev/null", name="fake", fd=-1, keys=bytes([0, 0, 0x08]))
    assert device.has_key(19)  # 第 3 字节 bit3
    assert not device.has_key(0)
    assert not device.has_key(1000)


def test_discover_reports_missing_paths():
    devices, notes = hotkey.discover("/nonexistent/event0", [])
    assert devices == []
    assert any("nonexistent" in note for note in notes)


def test_listener_stops_cleanly_without_devices():
    m = matcher()
    listener = hotkey.Listener(m, [], lambda action: None)
    listener.start()
    time.sleep(0.05)
    listener.stop()
    listener.join(timeout=1.0)
    assert not listener.is_alive()


def test_listener_picks_up_hot_plugged_device(monkeypatch):
    """启动后才出现的键盘（USB 热插拔、别的工具的 uinput 设备）要被重扫接进来。"""
    import os

    monkeypatch.setattr(hotkey, "_RESCAN_S", 0.05)
    fd1 = os.pipe()[0]
    fd2 = os.pipe()[0]
    first = hotkey.Device(path="/dev/input/event90", name="first", fd=fd1)
    second = hotkey.Device(path="/dev/input/event91", name="second", fd=fd2)
    actions: list[str] = []
    listener = hotkey.Listener(
        matcher(),
        [first],
        actions.append,
        rescan=lambda existing: [] if second.path in existing else [second],
    )
    listener.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(listener.devices) < 2:
            time.sleep(0.02)
        assert [device.name for device in listener.devices] == ["first", "second"]
        # 已在列表里的设备不会重复注册
        assert listener.rescan({first.path, second.path}) == []
    finally:
        listener.stop()
        listener.join(timeout=2.0)
        listener.close()
        for fd in (fd1, fd2):
            with contextlib.suppress(OSError):
                os.close(fd)


def test_discover_skips_non_keyboard_without_note(monkeypatch):
    """鼠标/电源键这类没有 EV_KEY 的设备不是错误，不该出现在 status 的提示里。"""

    def fake_open(path):
        raise hotkey.HotkeyError(f"{path} 不产生按键事件")

    monkeypatch.setattr(hotkey, "open_device", fake_open)
    devices, notes = hotkey.discover("/dev/input/event1,/dev/input/event2")
    assert devices == []
    assert notes == []


def test_discover_reports_permission_error(monkeypatch):
    def fake_open(path):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(hotkey, "open_device", fake_open)
    devices, notes = hotkey.discover("/dev/input/event3")
    assert devices == []
    assert notes and "没权限读" in notes[0]
