"""udev 规则：``/dev/uinput`` 可写 + ``/dev/input/event*`` 可读。

规则文件随 wheel 分发（``assets/99-flash-input.rules``），这里只负责「看」和「装」：

- 装：``sudo install`` 到 ``/etc/udev/rules.d/`` + ``udevadm`` reload/trigger，**不自己提权**、
  不改用户组、不调包管理器；
- 看：``status()`` 报告当前是否已经可用（即使没装规则，靠发行版默认权限也可能可用）。

uaccess 标签由 logind 在**登录时**换算成 ACL，所以装完要重新登录（切换 seat 也一样）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import env

RULES_PATH = Path("/etc/udev/rules.d/99-flash-input.rules")
ASSET = Path(__file__).parent / "assets" / "99-flash-input.rules"

#: 资产文件缺失时的内嵌兜底（wheel 里应当总是有 assets/，这里只防打包漏文件）
FALLBACK = """KERNEL=="uinput", TAG+="uaccess", OPTIONS+="static_node=uinput"
SUBSYSTEM=="input", ENV{ID_INPUT_KEYBOARD}=="1", TAG+="uaccess"
KERNEL=="uinput", GROUP="input", MODE="0660"
"""


def rule_text() -> str:
    """规则正文：优先读随包分发的 assets，缺失时用内嵌兜底。"""
    try:
        return ASSET.read_text(encoding="utf-8")
    except OSError:
        return FALLBACK


def installed() -> bool:
    """/etc 里那份是否就是当前版本（内容比较，忽略空白差异）。"""
    try:
        current = RULES_PATH.read_text(encoding="utf-8")
    except OSError:
        return False
    return _norm(current) == _norm(rule_text())


def _norm(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line and not line.startswith("#"))


def status() -> dict[str, object]:
    devices = env.keyboard_devices()
    return {
        "uinput_present": env.uinput_present(),
        "uinput_writable": env.uinput_writable(),
        "keyboard_devices": len(devices),
        "rule_installed": installed(),
        "rule_path": str(RULES_PATH),
    }


def usable(info: dict[str, object] | None = None) -> bool:
    """真正决定功能能不能用的两项：uinput 可写 + 至少一个可读键盘设备。"""
    info = info or status()
    return bool(info["uinput_writable"]) and int(info["keyboard_devices"]) > 0


def _sudo_prefix(sudo: str | None) -> list[str]:
    if sudo is not None:
        return [sudo] if sudo else []
    if os.geteuid() == 0:
        return []
    found = shutil.which("sudo")
    if not found:
        raise RuntimeError("需要 root 才能写 /etc/udev/rules.d/，但没找到 sudo")
    return [found]


def install(sudo: str | None = None, runner=subprocess.run) -> list[str]:
    """写入规则并让 udev 重新读。返回执行过的命令行，失败抛 RuntimeError。"""
    prefix = _sudo_prefix(sudo)
    text = rule_text()
    with tempfile.NamedTemporaryFile("w", suffix=".rules", delete=False, encoding="utf-8") as handle:
        handle.write(text)
        tmp = Path(handle.name)
    commands = [
        [*prefix, "install", "-Dm644", str(tmp), str(RULES_PATH)],
        [*prefix, "udevadm", "control", "--reload-rules"],
        [*prefix, "udevadm", "trigger", "--subsystem-match=input", "--action=change"],
    ]
    ran: list[str] = []
    try:
        for argv in commands:
            ran.append(" ".join(argv))
            proc = runner(argv, capture_output=True, text=True)
            if getattr(proc, "returncode", 0) != 0:
                detail = (getattr(proc, "stderr", "") or "").strip() or f"退出码 {proc.returncode}"
                raise RuntimeError(f"`{' '.join(argv)}` 失败：{detail}")
    finally:
        tmp.unlink(missing_ok=True)
    return ran
