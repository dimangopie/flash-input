"""udev 规则：内容、状态、安装命令（不执行真实 sudo）。"""

from __future__ import annotations

from types import SimpleNamespace

from flash_input import privileged


def test_rule_text_grants_uinput_and_keyboard():
    text = privileged.rule_text()
    assert 'KERNEL=="uinput"' in text and 'TAG+="uaccess"' in text
    assert 'OPTIONS+="static_node=uinput"' in text
    assert 'ENV{ID_INPUT_KEYBOARD}=="1"' in text
    body = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert len(body) == 3


def test_installed_false_when_rule_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(privileged, "RULES_PATH", tmp_path / "99-flash-input.rules")
    assert privileged.installed() is False


def test_installed_true_when_same_content(tmp_path, monkeypatch):
    target = tmp_path / "99-flash-input.rules"
    target.write_text("# 注释不同不影响\n" + privileged.rule_text(), encoding="utf-8")
    monkeypatch.setattr(privileged, "RULES_PATH", target)
    assert privileged.installed() is True


def test_install_runs_install_and_udevadm(tmp_path, monkeypatch):
    monkeypatch.setattr(privileged, "RULES_PATH", tmp_path / "99-flash-input.rules")
    seen: list[list[str]] = []

    def fake_runner(argv, capture_output=True, text=True):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ran = privileged.install(sudo="sudo", runner=fake_runner)
    assert len(seen) == 3
    assert seen[0][:3] == ["sudo", "install", "-Dm644"]
    assert seen[0][-1] == str(tmp_path / "99-flash-input.rules")
    assert "--reload-rules" in seen[1]
    assert "--subsystem-match=input" in seen[2]
    assert len(ran) == 3


def test_install_surfaces_udev_error(tmp_path, monkeypatch):
    monkeypatch.setattr(privileged, "RULES_PATH", tmp_path / "99-flash-input.rules")

    def fake_runner(argv, capture_output=True, text=True):
        return SimpleNamespace(returncode=1, stdout="", stderr="permission denied")

    try:
        privileged.install(sudo="sudo", runner=fake_runner)
    except RuntimeError as exc:
        assert "permission denied" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("失败时应当抛 RuntimeError")


def test_status_shape():
    info = privileged.status()
    assert set(info) >= {"uinput_present", "uinput_writable", "keyboard_devices", "rule_installed"}
    assert isinstance(privileged.usable(info), bool)
