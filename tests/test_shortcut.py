"""GNOME 截屏快捷键：gsettings 参数拼装（用假 runner，不改真实设置）。"""

from __future__ import annotations

from types import SimpleNamespace

from flash_input import shortcut


class FakeGSettings:
    """记录命令，按 key 返回预设值。"""

    def __init__(self, values: dict[str, str] | None = None):
        self.store = dict(values or {})
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        if argv[1] == "get":
            return SimpleNamespace(returncode=0, stdout=self.store.get(argv[3], "@as []"), stderr="")
        self.store[argv[3]] = argv[4]
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_parse_accels_handles_gvariant_prefix_and_empty():
    assert shortcut.parse_accels("@as []") == []
    assert shortcut.parse_accels("['Print', '<Super><Shift>o']") == ["Print", "<Super><Shift>o"]
    assert shortcut.parse_accels("") == []


def test_bind_appends_hotkey_and_keeps_print():
    gs = FakeGSettings({"show-screenshot-ui": "['Print']", "screenshot": "['<Shift>Print']"})
    shortcut.bind_screenshot("<Super><Shift>o", runner=gs)
    assert gs.store["show-screenshot-ui"] == "['Print', '<Super><Shift>o']"


def test_bind_is_idempotent():
    gs = FakeGSettings(
        {"show-screenshot-ui": "['Print', '<Super><Shift>o']", "screenshot": "['<Shift>Print']"}
    )
    shortcut.bind_screenshot("<Super><Shift>o", runner=gs)
    assert gs.store["show-screenshot-ui"] == "['Print', '<Super><Shift>o']"


def test_bind_restores_print_if_it_went_missing():
    gs = FakeGSettings({"show-screenshot-ui": "['<Super><Shift>o']", "screenshot": "['<Shift>Print']"})
    shortcut.bind_screenshot("<Super><Shift>o", runner=gs)
    assert gs.store["show-screenshot-ui"] == "['Print', '<Super><Shift>o']"


def test_bind_restores_legacy_screenshot_binding():
    gs = FakeGSettings({"show-screenshot-ui": "['Print']", "screenshot": "['<Super><Shift>o']"})
    shortcut.bind_screenshot("<Super><Shift>o", runner=gs)
    assert gs.store["screenshot"] == "['<Shift>Print']"


def test_unbind_puts_show_screenshot_ui_back_to_print():
    gs = FakeGSettings({"show-screenshot-ui": "['Print', '<Super><Shift>o']"})
    shortcut.unbind_screenshot(runner=gs)
    assert gs.store["show-screenshot-ui"] == "['Print']"


def test_screenshot_bindings_reads_both_keys():
    gs = FakeGSettings({"show-screenshot-ui": "['Print']", "screenshot": "['<Shift>Print']"})
    assert shortcut.screenshot_bindings(runner=gs) == {
        "screenshot": "['<Shift>Print']",
        "show-screenshot-ui": "['Print']",
    }


def test_gsettings_failure_is_reported():
    def boom(argv, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="No such schema")

    try:
        shortcut.bind_screenshot("<Super><Shift>o", runner=boom)
    except RuntimeError as exc:
        assert "No such schema" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当抛 RuntimeError")
