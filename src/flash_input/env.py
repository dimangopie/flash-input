"""环境探测：会话类型、工具、发行版、systemd、uinput。

只做「读」，不做任何有副作用的调用；每个函数都必须能在受限环境里失败而不抛。
`doctor` 与 `webui` 都从这里拿数据，保证命令行看到的和网页看到的是同一份事实。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import xdg


def session_type() -> str:
    """wayland / x11 / tty / unknown。"""
    value = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if value in ("wayland", "x11", "tty"):
        return value
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def is_wayland() -> bool:
    return session_type() == "wayland"


def is_x11() -> bool:
    return session_type() == "x11"


def desktop() -> str:
    return os.environ.get("XDG_CURRENT_DESKTOP", "") or "unknown"


def desktop_family() -> str:
    """``gnome`` / ``kde`` / ``other``。

    GNOME 与 KDE 在 Wayland 上共用同一套底层（evdev、uinput、wl-clipboard、systemd），
    差别只在「谁负责打开系统截屏窗口」：GNOME 用绑定到快捷键的截屏界面，
    KDE 直接用 Spectacle 的区域截图。
    """
    name = desktop().lower()
    if "gnome" in name or "unity" in name or "cinnamon" in name:
        return "gnome"
    if "kde" in name or "plasma" in name:
        return "kde"
    return "other"


def has_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def first_cmd(*names: str) -> str | None:
    for name in names:
        if has_cmd(name):
            return name
    return None


def os_release() -> dict[str, str]:
    """解析 /etc/os-release；失败返回空 dict。"""
    out: dict[str, str] = {}
    for candidate in (Path("/etc/os-release"), Path("/usr/lib/os-release")):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
        if out:
            break
    return out


def distro_id() -> str:
    data = os_release()
    return (data.get("ID") or "") + " " + (data.get("ID_LIKE") or "")


def package_hint(package: str) -> str:
    """按发行版给出安装提示（只打印，不执行任何包管理器）。"""
    ids = distro_id().lower()
    if "debian" in ids or "ubuntu" in ids:
        return f"sudo apt install {package}"
    if "fedora" in ids or "rhel" in ids:
        return f"sudo dnf install {package}"
    if "arch" in ids:
        return f"sudo pacman -S {package}"
    if "suse" in ids:
        return f"sudo zypper install {package}"
    return f"用你的包管理器安装 {package}"


def invocation_id() -> str | None:
    """systemd 拉起时一定有这个变量；有它就不写文件日志，交给 journald。"""
    return os.environ.get("INVOCATION_ID") or None


def systemd_managed() -> bool:
    return invocation_id() is not None


def session_bus_present() -> bool:
    path = os.environ.get("XDG_RUNTIME_DIR")
    return bool(path) and (Path(path) / "bus").exists()


def uinput_present() -> bool:
    return Path("/dev/uinput").exists()


def uinput_writable() -> bool:
    try:
        return os.access("/dev/uinput", os.W_OK)
    except OSError:
        return False


def keyboard_devices() -> list[Path]:
    """列出可读的 /dev/input/event*；读不了就返回空（daemon 用 evdev 自己再判断）。"""
    root = Path("/dev/input")
    if not root.is_dir():
        return []
    try:
        return [entry for entry in sorted(root.glob("event*")) if os.access(entry, os.R_OK)]
    except OSError:
        return []


def mem_available_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def describe() -> dict[str, object]:
    """doctor / /api/status 共用的一份快照。"""
    osr = os_release()
    return {
        "session_type": session_type(),
        "desktop": desktop(),
        "distro": osr.get("PRETTY_NAME", "unknown"),
        "distro_id": osr.get("ID", ""),
        "config_path": str(xdg.config_path()),
        "state_dir": str(xdg.state_dir()),
        "runtime_dir": str(xdg.runtime_dir()),
        "socket_path": str(xdg.socket_path()),
        "systemd_managed": systemd_managed(),
        "session_bus": session_bus_present(),
        "uinput_present": uinput_present(),
        "uinput_writable": uinput_writable(),
        "keyboard_devices": [str(p) for p in keyboard_devices()],
        "clipboard_cmd": first_cmd("wl-copy", "xclip"),
        "paste_reader": first_cmd("wl-paste", "xclip"),
        "mem_available_gb": mem_available_gb(),
        "in_wayland": is_wayland(),
        "in_x11": is_x11(),
    }
