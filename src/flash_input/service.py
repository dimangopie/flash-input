"""systemd user service：``flash-input.service`` 的生成、安装、查询。

unit 只有一份，装在 ``$XDG_CONFIG_HOME/systemd/user/``。两条关键约束：

- ``After=`` + ``WantedBy=graphical-session.target``：等图形会话（wayland socket / 输入设备）就绪再起；
  会话没拉起 target 的桌面可以改成 ``WantedBy=default.target``（``doctor`` 会给提示）。
- ``Restart=on-failure``：崩了 2 s 后自己回来，配合 ``StartLimitBurst`` 防止无限重启刷屏。

``ExecStart`` 用绝对路径：systemd user service 的 PATH 不包含 ``~/.local/bin``。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from . import xdg

UNIT_NAME = "flash-input.service"
UNIT_DIR = xdg.config_home() / "systemd" / "user"
UNIT_PATH = UNIT_DIR / UNIT_NAME
JOURNAL_UNIT = UNIT_NAME


def executable() -> str:
    """优先用 PATH 里的 ``finput``（uv tool 安装后一定在），否则退回 ``python -m flash_input``。

    注意不要对 ``sys.executable`` 做 ``resolve()``：venv 里的 python 是指向 uv 解释器的软链，
    解析掉就丢了 venv 的 site-packages，``-m flash_input`` 立刻 ModuleNotFoundError。
    """
    found = shutil.which("finput")
    if found:
        return str(Path(found).resolve())
    return f"{Path(sys.executable)} -m flash_input"


def unit_text(config: Path | str | None = None) -> str:
    """生成 unit 正文。``config`` 用来把非默认配置路径钉进 unit。"""
    exec_start = f"{executable()} run"
    lines = [
        "[Unit]",
        "Description=Flash Input（语音 / 抓屏 → 文字）",
        "After=graphical-session.target",
        "PartOf=graphical-session.target",
        "StartLimitIntervalSec=60",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        "RestartSec=2",
        "StandardOutput=journal",
        "StandardError=journal",
        "SyslogIdentifier=flash-input",
    ]
    if config:
        lines.append(f"Environment=FLASH_INPUT_CONFIG={config}")
    lines += ["", "[Install]", "WantedBy=graphical-session.target", ""]
    return "\n".join(lines)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _journalctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["journalctl", "--user", *args], capture_output=True, text=True)


def install(config: Path | str | None = None) -> list[str]:
    """写 unit → daemon-reload → enable --now。返回执行过的命令。"""
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(unit_text(config), encoding="utf-8")
    ran = ["systemctl --user daemon-reload"]
    proc = _systemctl("daemon-reload")
    if proc.returncode != 0:
        raise RuntimeError(f"daemon-reload 失败：{proc.stderr.strip() or proc.returncode}")
    for args in (("enable", UNIT_NAME), ("restart", UNIT_NAME)):
        ran.append("systemctl --user " + " ".join(args))
        proc = _systemctl(*args)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"退出码 {proc.returncode}"
            raise RuntimeError(f"`systemctl --user {' '.join(args)}` 失败：{detail}")
    return ran


def stop() -> str:
    """停掉服务（不动 enable 状态：下次登录还会起）。返回执行过的命令。"""
    proc = _systemctl("stop", UNIT_NAME)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"退出码 {proc.returncode}"
        raise RuntimeError(f"停止服务失败：{detail}")
    return f"systemctl --user stop {UNIT_NAME}"


def uninstall() -> list[str]:
    """停掉、禁用、删 unit、daemon-reload；不动配置和模型。"""
    ran: list[str] = []
    if UNIT_PATH.exists():
        for args in (("disable", "--now", UNIT_NAME),):
            ran.append("systemctl --user " + " ".join(args))
            _systemctl(*args)  # 已经停了不算错
        UNIT_PATH.unlink(missing_ok=True)
        ran.append("systemctl --user daemon-reload")
        _systemctl("daemon-reload")
    return ran


def status() -> dict[str, object]:
    installed_now = UNIT_PATH.exists()
    enabled = active = None
    if installed_now and shutil.which("systemctl"):
        enabled = (_systemctl("is-enabled", UNIT_NAME).stdout or "").strip() or None
        active = (_systemctl("is-active", UNIT_NAME).stdout or "").strip() or None
    return {
        "installable": bool(shutil.which("systemctl")),
        "installed": installed_now,
        "path": str(UNIT_PATH),
        "enabled": enabled,
        "active": active,
    }


def logs(lines: int = 30, follow: bool = False) -> int:
    """把 `journalctl --user -u flash-input` 接到当前终端（不捕获输出）。"""
    argv = ["journalctl", "--user", "-u", UNIT_NAME, "-n", str(lines)]
    if follow:
        argv.append("-f")
    try:
        return subprocess.call(argv)
    except FileNotFoundError:
        print("没找到 journalctl（这套系统不是 systemd）", file=sys.stderr)
        return 1
