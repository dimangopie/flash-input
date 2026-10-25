"""IPC 与 daemon 的轻量单测：不真的开热键/模型/麦克风。"""

from __future__ import annotations

import time

import pytest

from flash_input import config, ipc


def test_request_returns_none_without_daemon(tmp_path):
    assert ipc.request({"cmd": "ping"}, timeout=0.2, runtime_dir=tmp_path) is None


def test_roundtrip_and_second_instance_rejected(tmp_path):
    def handler(payload: dict) -> dict:
        return {"ok": True, "busy": False, "msg": payload.get("cmd", ""), "data": {"n": 1}}

    server = ipc.make_server(handler, runtime_dir=tmp_path)
    thread = ipc.serve_in_thread(server)
    try:
        reply = ipc.request({"cmd": "ping"}, timeout=2.0, runtime_dir=tmp_path)
        assert reply == {"ok": True, "busy": False, "msg": "ping", "data": {"n": 1}}
        with pytest.raises(ipc.IpcError):
            ipc.make_server(handler, runtime_dir=tmp_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    assert ipc.request({"cmd": "ping"}, timeout=0.2, runtime_dir=tmp_path) is None


def test_stale_socket_is_replaced(tmp_path):
    path = ipc.socket_path(tmp_path)
    path.write_text("stale")
    server = ipc.make_server(lambda payload: {"ok": True}, runtime_dir=tmp_path)
    thread = ipc.serve_in_thread(server)
    try:
        assert ipc.request({"cmd": "ping"}, timeout=2.0, runtime_dir=tmp_path) == {"ok": True}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_bad_json_gets_error_reply(tmp_path):
    import socket

    server = ipc.make_server(lambda payload: {"ok": True}, runtime_dir=tmp_path)
    thread = ipc.serve_in_thread(server)
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(2.0)
        conn.connect(str(ipc.socket_path(tmp_path)))
        conn.sendall(b"not json\n")
        data = conn.recv(4096)
        conn.close()
        assert b'"ok": false' in data
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_daemon_status_and_actions_without_start(tmp_path, monkeypatch):
    from flash_input import daemon as daemon_mod

    cfg = config.DEFAULTS
    d = daemon_mod.Daemon(cfg, no_web=True)
    try:
        reply = d.handle({"cmd": "ping"})
        assert reply["ok"] and reply["msg"] == "pong"

        status = d.handle({"cmd": "status"})["data"]
        assert status["pid"] > 0
        assert status["voice_state"] == "idle"
        assert status["hotkey_mode"] == cfg["hotkey"]["mode"]
        assert status["asr"]["loaded"] is False

        assert d.handle({"cmd": "bogus"})["ok"] is False
        assert d.handle({"cmd": "voice", "action": "cancel"})["ok"] is False
    finally:
        d.stop_event.set()


def test_reload_keeps_running_but_updates_matcher(tmp_path):
    from flash_input import daemon as daemon_mod

    d = daemon_mod.Daemon(config.DEFAULTS, no_web=True)
    new_cfg = config.load()
    new_cfg["hotkey"]["voice_key"] = "KEY_RIGHTALT"
    new_cfg["hotkey"]["ocr_key"] = "KEY_LEFTCTRL+KEY_P"
    d.reload(new_cfg)
    assert d._matcher.voice_key == "KEY_RIGHTALT"
    assert d._matcher.ocr_main == "KEY_P"
    d.stop_event.set()


def _fake_engines(monkeypatch):
    """把 ASR / OCR 换成记账的假引擎，避免加载真 ONNX。"""
    from flash_input import asr
    from flash_input import daemon as daemon_mod

    built: list = []

    class FakeAsr:
        def __init__(self, tag):
            self.tag, self.loaded, self.load_ms = tag, False, 0

        def load(self):
            self.loaded = True

        def unload(self):
            self.loaded = False

    class FakeOcr:
        def __init__(self, threads=2, resident="30min"):
            self.threads, self.resident, self.loaded = threads, resident, False

        def unload(self):
            self.loaded = False

    def fake_asr(cfg):
        engine = FakeAsr(cfg["asr"]["model"])
        built.append(engine)
        return engine

    def fake_ocr(threads=2, resident="30min"):
        engine = FakeOcr(threads, resident)
        built.append(engine)
        return engine

    monkeypatch.setattr(asr, "make_engine", fake_asr)
    monkeypatch.setattr(daemon_mod, "OcrEngine", fake_ocr)
    return built


def test_reload_switching_model_rebuilds_engine(monkeypatch):
    """换模型后必须重建引擎：旧代码会继续返回缓存的旧实例。"""
    from flash_input import daemon as daemon_mod

    _fake_engines(monkeypatch)
    d = daemon_mod.Daemon(config.DEFAULTS, no_web=True)
    try:
        assert d._get_asr().tag == config.DEFAULTS["asr"]["model"]
        new_cfg = config.load()
        new_cfg["asr"]["model"] = "sensevoice"
        d.reload(new_cfg)
        assert d._get_asr().tag == "sensevoice"  # 没有 key 比对就会拿到旧引擎
        assert d.status()["asr"]["engine_model"] == "sensevoice"
    finally:
        d.stop_event.set()


def test_reload_changing_ocr_threads_rebuilds_engine(monkeypatch):
    from flash_input import daemon as daemon_mod

    _fake_engines(monkeypatch)
    d = daemon_mod.Daemon(config.DEFAULTS, no_web=True)
    try:
        assert d._get_ocr().threads == 2
        new_cfg = config.load()
        new_cfg["ocr"]["threads"] = 4
        d.reload(new_cfg)
        assert d._get_ocr().threads == 4
    finally:
        d.stop_event.set()


def test_rebuild_models_immediately_rebuilds_asr(monkeypatch):
    """网页「重启模型」的后端：投递任务后，内存里立刻换成当前配置的引擎。"""
    from flash_input import daemon as daemon_mod

    _fake_engines(monkeypatch)
    d = daemon_mod.Daemon(config.DEFAULTS, no_web=True)
    try:
        d._get_asr()
        new_cfg = config.load()
        new_cfg["asr"]["model"] = "sensevoice"
        d.reload(new_cfg)

        assert d.engine_state()["asr"]["pending"] is True  # 配置变了但还没重建
        result = d.rebuild_models()
        assert result["ok"] is True and "重建" in result["message"]
        for _ in range(200):
            if not d._asr_worker.busy:
                break
            time.sleep(0.01)
        state = d.engine_state()["asr"]
        assert state["model"] == "sensevoice"
        assert state["loaded"] is True
        assert state["pending"] is False
    finally:
        d.stop_event.set()


def test_busy_reason_names_the_running_task():
    """忙的时候要能说出在忙什么，而不是一句「忙，忽略」。"""
    from flash_input import daemon as daemon_mod

    d = daemon_mod.Daemon(config.DEFAULTS, no_web=True)
    try:
        assert d.busy_reason() == ""
        assert d._ocr_worker.submit("抓屏 OCR", lambda: time.sleep(0.4)) is True
        time.sleep(0.1)
        reason = d.busy_reason()
        assert "抓屏 OCR" in reason and "秒" in reason
        # 同一个模型上再提交一次：拒绝（不排队）
        assert d._ocr_worker.submit("抓屏 OCR", lambda: None) is False
        assert "抓屏 OCR" in d.busy_reason()
        for _ in range(50):
            if not d.busy:
                break
            time.sleep(0.05)
        assert d.busy is False
        assert d.busy_reason() == ""
    finally:
        d.stop_event.set()


def test_ocr_is_not_blocked_by_voice_work():
    """ASR 正在转写时按抓屏键不该得到「忙」：两个模型线程互相独立。"""
    from flash_input import daemon as daemon_mod

    d = daemon_mod.Daemon(config.DEFAULTS, no_web=True)
    d._ocr_and_deliver = lambda: time.sleep(0.3)  # 跳过真实抓屏
    try:
        assert d._asr_worker.submit("语音转写", lambda: time.sleep(0.3)) is True
        time.sleep(0.05)
        assert d._asr_worker.busy is True
        assert d.ocr() == "抓屏中…"
        assert d._ocr_worker.busy is True
        assert d.busy is True
        # 同一个 OCR 任务还没结束：只报忙（没有「再按一次取消」）
        assert d.ocr().startswith("忙：")
    finally:
        d._ocr_worker.stop(wait=True)
        d._asr_worker.stop(wait=True)
        d.stop_event.set()
