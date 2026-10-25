"""剪贴板后端选择与错误路径（不真的写系统剪贴板）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from flash_input import clipboard, env


@pytest.fixture()
def fake_which(monkeypatch):
    available: set[str] = set()

    def which(name):
        return f"/usr/bin/{name}" if name in available else None

    monkeypatch.setattr(clipboard.shutil, "which", which)
    return available


def test_backend_prefers_wl_copy_on_wayland(monkeypatch, fake_which):
    monkeypatch.setattr(env, "is_wayland", lambda: True)
    monkeypatch.setattr(env, "is_x11", lambda: False)
    fake_which.update({"wl-copy", "xclip"})
    assert clipboard.backend() == "wl-copy"


def test_backend_uses_xclip_on_x11(monkeypatch, fake_which):
    monkeypatch.setattr(env, "is_wayland", lambda: False)
    monkeypatch.setattr(env, "is_x11", lambda: True)
    fake_which.update({"xclip"})
    assert clipboard.backend() == "xclip"


def test_backend_none_when_nothing_installed(monkeypatch, fake_which):
    monkeypatch.setattr(env, "is_wayland", lambda: True)
    monkeypatch.setattr(env, "is_x11", lambda: False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert clipboard.backend() is None


def test_copy_text_without_backend_raises(monkeypatch, fake_which):
    monkeypatch.setattr(env, "is_wayland", lambda: True)
    monkeypatch.setattr(env, "is_x11", lambda: False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(clipboard.ClipboardError, match=r"wl-copy|xclip"):
        clipboard.copy_text("hi")


def test_copy_text_reports_nonzero_exit(monkeypatch):
    monkeypatch.setattr(clipboard, "backend", lambda: "wl-copy")

    class Proc:
        returncode = 1

    def fake_run(*_args, **kwargs):
        # 现在 stderr 传进来的是临时文件，失败信息写进去再读出来
        kwargs["stderr"].write(b"selection owner died")
        return Proc()

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    with pytest.raises(clipboard.ClipboardError, match="selection owner died"):
        clipboard.copy_text("hi")
