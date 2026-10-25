"""设置页的接口测试：起一个真的 HTTP server（端口 0），走一遍读写与校验失败。"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from flash_input import config as config_mod
from flash_input import webui


@pytest.fixture()
def server(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("FLASH_INPUT_CONFIG", str(cfg_path))
    srv = webui.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", cfg_path
    finally:
        srv.shutdown()
        srv.server_close()


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_page_and_config(server):
    base, cfg_path = server
    with urllib.request.urlopen(base + "/", timeout=5) as resp:
        html = resp.read().decode("utf-8")
    assert "Flash Input 设置" in html
    assert "asr.resident" in html
    # 热键是录制式输入，不是手填键名
    assert 'id="voice_rec"' in html and 'id="ocr_rec"' in html
    assert "readonly" in html
    # 键名映射表被注入页面：网页录到的就是后台认的
    assert "KEY_RIGHTCTRL" in html
    assert '"ControlRight": "KEY_RIGHTCTRL"' in html
    assert "__KEYMAP_JSON__" not in html

    cfg = _get(base + "/api/config")
    assert cfg["asr"]["resident"] == "forever"
    assert cfg["exists"] is False
    assert "defaults" in cfg
    assert not cfg_path.exists()


def test_save_and_validation(server):
    base, cfg_path = server
    code, err = _post(base + "/api/config", {"ocr": {"resident": "5min"}})
    assert code == 400
    assert "resident" in err["error"]
    assert not cfg_path.exists()

    # 手填错键名（比如网页录到别的键）在保存时就被挡住
    code, err = _post(base + "/api/config", {"hotkey": {"voice_key": "KEY_O"}})
    assert code == 400
    assert "语音键" in err["error"]
    code, err = _post(base + "/api/config", {"hotkey": {"ocr_key": "KEY_LEFTMETA"}})
    assert code == 400
    assert "组合键" in err["error"]
    assert not cfg_path.exists()

    code, saved = _post(
        base + "/api/config",
        {"hotkey": {"voice_key": "KEY_RIGHTALT", "ocr_key": "KEY_F9"}, "asr": {"resident": "none"}},
    )
    assert code == 200
    assert saved["hotkey"]["ocr_key"] == "KEY_F9"

    code, saved = _post(base + "/api/config", {"asr": {"resident": "none"}, "ocr": {"resident": "forever"}})
    assert code == 200
    assert saved["asr"]["resident"] == "none"
    assert saved["exists"] is True
    assert config_mod.load()["ocr"]["resident"] == "forever"


def test_status(server):
    base, _ = server
    status = _get(base + "/api/status")
    assert "会话" in status
    assert status["ASR 常驻"] == "forever"


def test_stale_revision_is_rejected(server):
    """旧标签页保存时带着过期版本：409 拒绝，不能静默覆盖别处刚改的值。"""
    base, _cfg_path = server
    first = _get(base + "/api/config")
    assert first["revision"]

    # 第一个窗口保存成功（版本随文件内容变化）
    code, saved = _post(
        base + "/api/config", {**first, "ocr": {"resident": "30min"}, "_revision": first["revision"]}
    )
    assert code == 200
    assert saved["revision"] != first["revision"]

    # 第二个窗口还拿着旧版本：必须被拒绝
    code, err = _post(
        base + "/api/config", {**first, "ocr": {"resident": "none"}, "_revision": first["revision"]}
    )
    assert code == 409
    assert err.get("stale") is True
    # 被拒绝的写入没有落盘
    current = _get(base + "/api/config")
    assert current["ocr"]["resident"] == "30min"


def test_missing_revision_still_allowed(server):
    """没带版本的旧客户端（比如 curl）保持兼容：照常保存，随后版本刷新。"""
    base, _cfg_path = server
    code, data = _post(base + "/api/config", {"ocr": {"resident": "10min"}})
    assert code == 200
    assert data["revision"]


# --------------------------------------------------------------------------- 模型管理


def _serve(tmp_path, cfg: dict | None = None):
    """起一个用指定配置的 server（返回 (base, cfg_path, srv)）。"""
    cfg_path = tmp_path / "config.json"
    if cfg is not None:
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    import os

    os.environ["FLASH_INPUT_CONFIG"] = str(cfg_path)
    srv = webui.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{srv.server_address[1]}", cfg_path, srv


def test_models_endpoint_lists_builtin(server):
    base, _ = server
    data = _get(base + "/api/models")
    ids = [row["id"] for row in data["models"]]
    assert ids[:2] == ["qwen3-0.6b", "qwen3-1.7b"]  # 同一模型的两个规格各一条
    assert {"sensevoice", "paraformer", "zipformer"} <= set(ids)
    assert sum(1 for row in data["models"] if row["current"]) == 1


def test_models_install_unknown_id(server):
    base, _ = server
    status, data = _post(base + "/api/models/install", {"id": "nope"})
    assert status == 200 and data["ok"] is False and "未知模型" in data["error"]


def test_models_select_requires_download(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(webui.asr_catalog, "is_ready", lambda entry: False)
    _status, data = _post(base + "/api/models/select", {"id": "sensevoice"})
    assert data["ok"] is False and "还没下载" in data["error"]


def test_models_add_registers_and_selects(tmp_path, monkeypatch):
    monkeypatch.setattr(
        webui.asr_catalog,
        "register",
        lambda cfg, name, directory, kind="auto": {
            "id": "my-model",
            "name": name or "my",
            "kind": "ctc",
            "engine": "paraformer",
            "dir": directory,
            "files": ["a.onnx"],
            "size_mb": 1,
            "note": "测试",
        },
    )
    base, cfg_path, srv = _serve(tmp_path)
    try:
        status, data = _post(base + "/api/models/add", {"name": "my", "dir": "/tmp/my-model"})
        assert status == 200 and data["ok"] is True
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["asr"]["model"] == "my-model"
        registry = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
        assert any(m["id"] == "my-model" and m["dir"] == "/tmp/my-model" for m in registry["models"])
        assert data["registry"].endswith("models.json")
        assert any(row["id"] == "my-model" and row["current"] for row in data["models"])
    finally:
        srv.shutdown()
        srv.server_close()


def test_models_remove_custom_switches_back(tmp_path):
    cfg = {
        "asr": {
            "model": "my-model",
            "models": [
                {"id": "my-model", "name": "我的", "kind": "ctc", "dir": "/tmp/my-model", "files": ["a.onnx"]}
            ],
        }
    }
    base, cfg_path, srv = _serve(tmp_path, cfg)
    try:
        status, data = _post(base + "/api/models/remove", {"id": "my-model"})
        assert status == 200 and data["ok"] is True
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["asr"]["model"] == "qwen3-0.6b"
        registry = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
        assert not any(m["id"] == "my-model" for m in registry["models"])
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_save_keeps_registry_entries(tmp_path):
    """页面表单里没有 asr.models：保存配置不能把自定义登记清空。"""
    cfg = {
        "asr": {
            "model": "my-model",
            "models": [
                {"id": "my-model", "name": "我的", "kind": "ctc", "dir": "/tmp/my-model", "files": ["a.onnx"]}
            ],
        }
    }
    base, cfg_path, srv = _serve(tmp_path, cfg)
    try:
        status, _data = _post(base + "/api/config", {"asr": {"resident": "none"}})
        assert status == 200
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["asr"]["model"] == "my-model"
        assert saved["asr"]["resident"] == "none"
        # 登记文件是懒创建的；旧配置里的 asr.models 仍然有效，也不会被表单覆盖
        assert [m["id"] for m in saved["asr"]["models"]] == ["my-model"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_models_select_message_points_to_rebuild(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(webui.asr_catalog, "is_ready", lambda entry: True)
    _status, data = _post(base + "/api/models/select", {"id": "sensevoice"})
    assert data["ok"] is True
    assert "重启模型" in data["message"]


def test_models_rebuild_without_daemon_is_explicit(server):
    base, _ = server
    status, data = _post(base + "/api/models/rebuild", {})
    assert status == 200
    assert data["ok"] is False
    assert "守护进程" in data["error"]


def test_models_rebuild_calls_daemon_and_reports_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASH_INPUT_CONFIG", str(tmp_path / "config.json"))
    calls: list[bool] = []
    engine = {
        "asr": {"model": "sensevoice", "loaded": True, "load_ms": 12, "pending": False, "busy": False},
        "ocr": {"loaded": False, "threads": 2, "busy": False},
        "last_error": "",
    }
    srv = webui.make_server(
        "127.0.0.1",
        0,
        on_rebuild=lambda: calls.append(True) or {"ok": True, "message": "正在重建模型…"},
        on_engine_state=lambda: engine,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, data = _post(base + "/api/models/rebuild", {})
        assert status == 200 and data["ok"] is True
        assert calls == [True]
        assert data["engine"]["asr"]["model"] == "sensevoice"
        # 列表接口也要给出真实引擎，页面据此判断重建是否完成
        assert _get(base + "/api/models")["engine"]["asr"]["loaded"] is True
    finally:
        srv.shutdown()
        srv.server_close()
