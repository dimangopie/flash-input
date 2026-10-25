"""`finput config set`：写盘之外，还要让在跑的守护进程热加载（否则模型切换静默失效）。"""

from __future__ import annotations

import argparse
import json

from flash_input import cli, ipc


def _args(tmp_path, key: str, value: str) -> argparse.Namespace:
    return argparse.Namespace(
        action="set", key=key, value=value, config=str(tmp_path / "config.json"), raw=False
    )


def test_config_set_notifies_running_daemon(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(
        ipc, "request", lambda payload, timeout=3.0, runtime_dir=None: calls.append(payload) or {"ok": True}
    )
    assert cli.cmd_config(_args(tmp_path, "asr.model", "sensevoice")) == cli.EXIT_OK
    assert calls == [{"cmd": "reload"}]
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["asr"]["model"] == "sensevoice"
    assert "已重新加载配置" in capsys.readouterr().out


def test_config_set_quiet_without_daemon(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ipc, "request", lambda payload, timeout=3.0, runtime_dir=None: None)
    assert cli.cmd_config(_args(tmp_path, "asr.resident", "none")) == cli.EXIT_OK
    assert "已重新加载配置" not in capsys.readouterr().out
