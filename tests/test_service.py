"""systemd user service：unit 文本与安装流程（不碰真实 systemctl）。"""

from __future__ import annotations

from types import SimpleNamespace

from flash_input import service


def test_unit_text_has_startup_contract():
    text = service.unit_text()
    assert "After=graphical-session.target" in text
    assert "PartOf=graphical-session.target" in text or "WantedBy=graphical-session.target" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=2" in text
    assert "StartLimitBurst=5" in text
    assert "StandardError=journal" in text
    assert "WantedBy=graphical-session.target" in text
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            target = line.split("=", 1)[1]
            assert target.startswith("/"), "ExecStart 必须是绝对路径，user service 的 PATH 没有 ~/.local/bin"
            assert target.endswith(" run")


def test_unit_text_embeds_explicit_config(tmp_path):
    text = service.unit_text(tmp_path / "config.json")
    assert f"Environment=FLASH_INPUT_CONFIG={tmp_path / 'config.json'}" in text


def test_install_and_uninstall(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "UNIT_DIR", tmp_path)
    monkeypatch.setattr(service, "UNIT_PATH", tmp_path / service.UNIT_NAME)

    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(service, "_systemctl", fake_systemctl)

    ran = service.install()
    assert (tmp_path / service.UNIT_NAME).exists()
    assert ("daemon-reload",) in calls
    assert ("enable", service.UNIT_NAME) in calls
    assert ("restart", service.UNIT_NAME) in calls
    assert any("daemon-reload" in item for item in ran)

    calls.clear()
    service.uninstall()
    assert not (tmp_path / service.UNIT_NAME).exists()
    assert ("disable", "--now", service.UNIT_NAME) in calls


def test_install_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "UNIT_DIR", tmp_path)
    monkeypatch.setattr(service, "UNIT_PATH", tmp_path / service.UNIT_NAME)

    def broken(*args):
        return SimpleNamespace(returncode=1, stdout="", stderr="Failed to connect to bus")

    monkeypatch.setattr(service, "_systemctl", broken)
    try:
        service.install()
    except RuntimeError as exc:
        assert "Failed to connect to bus" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("安装失败时应当抛 RuntimeError")


def test_status_for_missing_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "UNIT_PATH", tmp_path / service.UNIT_NAME)
    info = service.status()
    assert info["installed"] is False
    assert info["path"].endswith(service.UNIT_NAME)
