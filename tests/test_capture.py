"""抓屏模块的纯逻辑测试（真抓屏要桌面会话，见 README 手工验收）。"""

from __future__ import annotations

import pytest

from flash_input import capture


def test_discard_ignores_missing_file(tmp_path):
    capture.discard(tmp_path / "never-existed.png")  # 不抛就是通过


def test_discard_removes_file(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"x")
    capture.discard(shot)
    assert not shot.exists()


def test_pictures_dir_prefers_xdg_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_PICTURES_DIR", str(tmp_path / "pics"))
    assert capture.pictures_dir() == tmp_path / "pics"


# ------------------------------------------------------------------ system_capture
def test_system_capture_taps_print_and_takes_the_new_picture(tmp_path, monkeypatch):
    from PIL import Image

    from flash_input import inject

    pics = tmp_path / "pics"
    pics.mkdir()
    taps: list[int] = []

    def tap(code):
        taps.append(code)
        if len(taps) == 1:  # 模拟用户框选后系统存图
            Image.new("RGB", (30, 20), (200, 10, 10)).save(pics / "Screenshot from 2026.png")

    monkeypatch.setattr(inject, "tap_key", tap)
    shot = capture.system_capture(timeout=5.0, runtime_dir=tmp_path, pictures=pics)

    assert taps == [capture.KEY_SYSRQ]
    assert shot.exists()
    assert Image.open(shot).size == (30, 20)
    assert not list(pics.iterdir()), "系统存的原件要删掉"


def test_system_capture_finds_picture_in_screenshots_subdir(tmp_path, monkeypatch):
    """GNOME 46 默认存到 ~/Pictures/Screenshots/，只扫根目录会永远等不到（真实故障）。"""
    from PIL import Image

    from flash_input import inject

    pics = tmp_path / "pics"
    (pics / "Screenshots").mkdir(parents=True)

    def tap(code):
        if code == capture.KEY_SYSRQ:
            Image.new("RGB", (12, 8), "white").save(
                pics / "Screenshots" / "Screenshot from 2026-01-01 00-00-00.png"
            )

    monkeypatch.setattr(inject, "tap_key", tap)
    shot = capture.system_capture(timeout=5.0, runtime_dir=tmp_path, pictures=pics)
    assert Image.open(shot).size == (12, 8)
    assert not list((pics / "Screenshots").iterdir())


def test_complete_png_rejects_half_written_file(tmp_path):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buffer, "PNG")
    data = buffer.getvalue()

    path = tmp_path / "Screenshot.png"
    path.write_bytes(data[: len(data) // 2])
    assert capture._complete_png(path) is False

    path.write_bytes(data)
    assert capture._complete_png(path) is True


def test_system_capture_times_out_without_a_picture(tmp_path, monkeypatch):
    from flash_input import inject

    pics = tmp_path / "pics"
    pics.mkdir()
    monkeypatch.setattr(inject, "tap_key", lambda code: None)
    with pytest.raises(capture.CaptureError):
        capture.system_capture(timeout=0.2, runtime_dir=tmp_path, pictures=pics)


def test_system_capture_needs_uinput(tmp_path, monkeypatch):
    from flash_input import inject

    pics = tmp_path / "pics"
    pics.mkdir()

    def no_uinput(code):
        raise inject.InjectUnavailable("/dev/uinput 不存在")

    monkeypatch.setattr(inject, "tap_key", no_uinput)
    with pytest.raises(capture.CaptureError) as exc:
        capture.system_capture(timeout=1.0, runtime_dir=tmp_path, pictures=pics)
    assert "uinput" in str(exc.value)


def test_system_capture_listen_mode_does_not_tap_and_still_gets_the_file(tmp_path, monkeypatch):
    """listen 模式：GNOME 自己开窗，我们只等文件（不再注入 Print）。"""
    import threading
    import time

    from PIL import Image

    from flash_input import inject

    pics = tmp_path / "pics"
    (pics / "Screenshots").mkdir(parents=True)
    taps: list[int] = []
    monkeypatch.setattr(inject, "tap_key", lambda code: taps.append(code))

    def save_later():
        time.sleep(0.4)
        Image.new("RGB", (40, 30), (10, 10, 200)).save(pics / "Screenshots" / "Screenshot-x.png")

    threading.Thread(target=save_later, daemon=True).start()
    shot = capture.system_capture(timeout=5.0, runtime_dir=tmp_path, pictures=pics, trigger="listen")

    assert taps == [], "listen 模式不该注入 Print"
    assert Image.open(shot).size == (40, 30)
    assert not list(pics.rglob("*.png")), "系统原件要删掉"


def test_system_capture_rejects_unknown_trigger(tmp_path):
    with pytest.raises(capture.CaptureError):
        capture.system_capture(timeout=0.1, runtime_dir=tmp_path, pictures=tmp_path, trigger="magic")


# ------------------------------------------------------------------ KDE / Spectacle
def test_spectacle_capture_runs_region_mode_and_takes_the_file(tmp_path, monkeypatch):
    from PIL import Image

    def which(name):
        return "/usr/bin/spectacle" if name == "spectacle" else None

    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        Image.new("RGB", (12, 10), (1, 2, 3)).save(argv[argv.index("--output") + 1])

    monkeypatch.setattr(capture.shutil, "which", which)
    monkeypatch.setattr(capture.subprocess, "run", run)
    path = capture.spectacle_capture(timeout=5, runtime_dir=tmp_path)
    assert path.is_file() and path.name.startswith("shot-")
    assert calls[0][1:3] == ["--region", "--output"]


def test_spectacle_missing_gives_install_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)
    with pytest.raises(capture.CaptureError) as exc:
        capture.spectacle_capture(timeout=1, runtime_dir=tmp_path)
    assert "spectacle" in str(exc.value)


def test_spectacle_cancelled_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.shutil, "which", lambda name: "/usr/bin/spectacle")
    monkeypatch.setattr(capture.subprocess, "run", lambda argv, **kwargs: None)
    with pytest.raises(capture.CaptureError):
        capture.spectacle_capture(timeout=1, runtime_dir=tmp_path)


def test_system_capture_dispatches_spectacle_trigger(tmp_path, monkeypatch):
    called = {}

    def fake(timeout=60.0, *, runtime_dir=None):
        called["hit"] = True
        return tmp_path / "x.png"

    monkeypatch.setattr(capture, "spectacle_capture", fake)
    assert capture.system_capture(timeout=1, trigger="spectacle", runtime_dir=tmp_path) == tmp_path / "x.png"
    assert called["hit"] is True
