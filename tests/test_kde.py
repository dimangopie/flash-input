"""KDE 兼容：桌面识别 + `capture bind` 在 KDE 上改成 Spectacle 区域截图。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from flash_input import config, env, shortcut


@pytest.mark.parametrize(
    "desktop,family",
    [
        ("ubuntu:GNOME", "gnome"),
        ("GNOME", "gnome"),
        ("KDE", "kde"),
        ("KDE:Plasma", "kde"),
        ("sway", "other"),
        ("", "other"),
    ],
)
def test_desktop_family(monkeypatch, desktop, family):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    assert env.desktop_family() == family


def test_apply_trigger_on_kde_does_not_touch_desktop(monkeypatch):
    """KDE：apply_trigger 不碰 gsettings，只回一句说明。"""
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    calls: list[str] = []
    note = shortcut.apply_trigger(
        "spectacle", "KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O", runner=lambda *a, **k: calls.append(str(a))
    )
    assert "Spectacle" in note
    assert calls == []


def test_apply_trigger_on_gnome_binds_hotkey(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    written: dict[str, str] = {}

    class Result:
        returncode = 0
        stdout = "['Print']"
        stderr = ""

    def runner(argv, **kwargs):
        if argv[1] == "get":
            return Result()
        written[argv[3]] = argv[4]
        return Result()

    note = shortcut.apply_trigger("listen", "KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O", runner=runner)
    assert "<Super><Shift>o" in written["show-screenshot-ui"]
    assert "Print" in written["show-screenshot-ui"]
    assert "<Super><Shift>o" in note


def test_apply_trigger_leaving_listen_restores_default(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    written: dict[str, str] = {}

    class Result:
        returncode = 0
        stdout = "['Print']"
        stderr = ""

    def runner(argv, **kwargs):
        if argv[1] == "set":
            written[argv[3]] = argv[4]
        return Result()

    note = shortcut.apply_trigger(
        "inject", "KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O", old_trigger="listen", runner=runner
    )
    assert written["show-screenshot-ui"] == "['Print']"
    assert "还原" in note


def test_webui_save_syncs_capture_binding(tmp_path, monkeypatch):
    """设置页保存 ocr.trigger=listen 时会自动改绑（不再依赖命令行）。"""
    from flash_input import webui

    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.setenv("FLASH_INPUT_CONFIG", str(tmp_path / "config.json"))
    seen: list[tuple[str, str | None]] = []

    def fake_apply(trigger, ocr_key, old_trigger=None, **kwargs):
        seen.append((trigger, old_trigger))
        return "已改绑"

    monkeypatch.setattr(shortcut, "apply_trigger", fake_apply)
    app = {"config": config.load(), "explicit_config": None}
    note = webui._sync_capture_binding(app, old_trigger="inject")
    assert note == "已改绑"
    assert seen == [(str(app["config"]["ocr"]["trigger"]), "inject")]


def test_spectacle_trigger_is_valid():
    cfg = config._deep_merge(config.DEFAULTS, {"ocr": {"trigger": "spectacle"}})
    config.validate(cfg)  # 不抛就是通过
