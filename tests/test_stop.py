"""`finput stop`：优先 IPC 优雅退出，兜底停 systemd 服务。"""

from __future__ import annotations

import argparse

from flash_input import cli, ipc, service


def _args() -> argparse.Namespace:
    return argparse.Namespace()


def test_stop_via_ipc_shuts_down_daemon(monkeypatch, tmp_path, capsys):
    """手动跑的守护进程（不归 systemd）走 IPC。"""
    calls: list[dict] = []
    monkeypatch.setattr(ipc, "request", lambda payload, timeout=5.0: calls.append(payload) or {"ok": True})
    monkeypatch.setattr(service, "status", lambda: {"installed": False, "active": None})
    monkeypatch.setattr(cli.xdg, "socket_path", lambda: tmp_path / "flash-input.sock")
    assert cli.cmd_stop(_args()) == cli.EXIT_OK
    assert calls == [{"cmd": "shutdown"}]
    assert "已停止守护进程" in capsys.readouterr().out


def test_stop_prefers_systemd_when_unit_active(monkeypatch, tmp_path, capsys):
    """systemd 管的服务走 systemctl stop，不用 IPC 抢着退。"""
    monkeypatch.setattr(
        ipc, "request", lambda payload, timeout=5.0: (_ for _ in ()).throw(AssertionError("不该走 IPC"))
    )
    monkeypatch.setattr(service, "status", lambda: {"installed": True, "active": "active"})
    stopped: list[str] = []
    monkeypatch.setattr(
        service, "stop", lambda: stopped.append("stop") or "systemctl --user stop flash-input.service"
    )
    monkeypatch.setattr(cli.xdg, "socket_path", lambda: tmp_path / "flash-input.sock")
    assert cli.cmd_stop(_args()) == cli.EXIT_OK
    assert stopped == ["stop"]
    assert "已停止 systemd 服务" in capsys.readouterr().out


def test_stop_when_nothing_runs_is_not_an_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ipc, "request", lambda payload, timeout=5.0: None)
    monkeypatch.setattr(service, "status", lambda: {"installed": False, "active": None})
    monkeypatch.setattr(cli.xdg, "socket_path", lambda: tmp_path / "flash-input.sock")
    assert cli.cmd_stop(_args()) == cli.EXIT_OK
    assert "没有在运行" in capsys.readouterr().out


def test_stop_reports_daemon_refusal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ipc, "request", lambda payload, timeout=5.0: {"ok": False, "msg": "正在录音"})
    monkeypatch.setattr(cli.xdg, "socket_path", lambda: tmp_path / "flash-input.sock")
    assert cli.cmd_stop(_args()) == cli.EXIT_RUNTIME
    assert "正在录音" in capsys.readouterr().err


def test_service_stop_reports_failure(monkeypatch):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "Unit not loaded"

    monkeypatch.setattr(service, "_systemctl", lambda *args: Proc())
    try:
        service.stop()
    except RuntimeError as exc:
        assert "Unit not loaded" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当抛 RuntimeError")
