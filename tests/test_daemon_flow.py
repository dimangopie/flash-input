"""daemon 单飞流程：抓屏 → OCR → 注入，全部用假实现，不碰真实设备/模型。"""

from __future__ import annotations

import time

from flash_input import capture, config, inject


def _wait_idle(daemon, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while daemon.busy and time.monotonic() < deadline:
        time.sleep(0.02)
    assert daemon.busy is False


def _fake_ocr(text: str = "识别出来的字"):
    class FakeOcr:
        def text(self, path, layout="reading"):
            return text

        def maybe_unload(self, now=None):
            return False

    return FakeOcr()


def test_ocr_flow_captures_then_pastes_and_deletes_shot(tmp_path, monkeypatch):
    from flash_input import daemon as daemon_mod

    monkeypatch.setenv("FLASH_INPUT_STATE_DIR", str(tmp_path / "state"))

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n")

    monkeypatch.setattr(capture, "system_capture", lambda **kw: shot)
    shot.write_bytes(b"\x89PNG\r\n")

    delivered: list[str] = []

    def fake_deliver(text, cfg, **kwargs):
        delivered.append(text)
        return inject.DeliverResult(mode="clipboard", clipboard=text, note="")

    monkeypatch.setattr(inject, "deliver", fake_deliver)

    d = daemon_mod.Daemon(config.DEFAULTS, no_web=True)
    try:
        monkeypatch.setattr(d, "_get_ocr", lambda: _fake_ocr())

        assert "抓屏" in d.ocr()
        _wait_idle(d)
        assert delivered == ["识别出来的字"]
        assert d.status()["last_action"] == "OCR:clipboard"
        assert d.status()["busy_reason"] == ""
        assert not shot.exists(), "识别完要删掉截图"
    finally:
        d.stop_event.set()


def test_capture_failure_is_reported_and_keeps_daemon_alive(tmp_path, monkeypatch):
    from flash_input import daemon as daemon_mod

    def boom(**kwargs):
        raise capture.CaptureError("系统截屏窗口被拒绝")

    monkeypatch.setattr(capture, "system_capture", boom)

    d = daemon_mod.Daemon(config.DEFAULTS, no_web=True)
    try:
        d.ocr()
        _wait_idle(d)
        assert "系统截屏窗口被拒绝" in d.status()["last_error"]
        assert d.busy is False  # 失败也要放开单飞锁
    finally:
        d.stop_event.set()


def test_ocr_busy_reason_names_the_running_task(tmp_path, monkeypatch):
    from flash_input import daemon as daemon_mod

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n")
    release = []

    class SlowOcr:
        def text(self, path, layout="reading"):
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not release:
                time.sleep(0.02)
            return "慢"

        def maybe_unload(self, now=None):
            return False

    monkeypatch.setattr(capture, "system_capture", lambda **kw: shot)
    monkeypatch.setattr(
        inject, "deliver", lambda text, cfg: inject.DeliverResult(mode="clipboard", clipboard=text, note="")
    )

    d = daemon_mod.Daemon(config.DEFAULTS, no_web=True)
    try:
        monkeypatch.setattr(d, "_get_ocr", lambda: SlowOcr())
        d.ocr()
        time.sleep(0.15)
        assert d.busy is True
        assert "抓屏 OCR" in d.busy_reason()
        # 抓屏进行中再按热键：只报忙，不搞「再按一次取消」这种脚本化开关
        assert d.ocr().startswith("忙：") and "抓屏 OCR" in d.ocr()
        release.append(1)
        _wait_idle(d)
    finally:
        d.stop_event.set()


def test_voice_and_ocr_paste_switches_are_independent(monkeypatch):
    """两个粘贴开关互不影响：daemon 按任务类型分别传 paste=。"""
    from flash_input import daemon, inject

    cfg = config._deep_merge(
        config.DEFAULTS, {"inject": {"voice_paste": False, "ocr_paste": True, "paste_delay_ms": 0}}
    )
    d = daemon.Daemon(cfg=cfg, no_web=True)
    calls = []
    monkeypatch.setattr(
        inject,
        "deliver",
        lambda text, cfg, **kw: (
            calls.append(kw.get("paste_enabled"))
            or inject.DeliverResult(mode="clipboard", clipboard="wl-copy")
        ),
    )

    d._deliver("语音", "语音输入", 1.0, paste=bool(cfg["inject"]["voice_paste"]))
    d._deliver("文字", "OCR", 1.0, paste=bool(cfg["inject"]["ocr_paste"]))
    assert calls == [False, True]
