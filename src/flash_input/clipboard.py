"""剪贴板写入：Wayland 用 `wl-copy`，Xorg 用 `xclip`。

为什么不用 portal.Clipboard / Tkinter（OUTLINE §3 有完整理由）：前者要 RemoteDesktop
会话与常驻授权，后者要靠 XWayland + 常驻 Tk 线程，依赖一点没少、纯 Wayland 还不可用。
这两个命令是各自会话里最朴素、最被验证的做法。

文字永远走剪贴板再发粘贴键（OUTLINE §2.3）：uinput 直接打中文必须过输入法，做不到。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from . import env


class ClipboardError(RuntimeError):
    pass


def backend() -> str | None:
    """返回可用的后端名（`wl-copy` / `xclip`），都不可用返回 None。"""
    wayland = env.is_wayland()
    x11 = env.is_x11()
    if wayland and shutil.which("wl-copy"):
        return "wl-copy"
    if x11 and shutil.which("xclip"):
        return "xclip"
    # 会话类型判断不出来（比如在 tmux 里）时按实际存在的命令兜底
    if shutil.which("wl-copy") and (os.environ.get("WAYLAND_DISPLAY") or wayland):
        return "wl-copy"
    if shutil.which("xclip") and (os.environ.get("DISPLAY") or x11):
        return "xclip"
    return None


def package_hint() -> str:
    """按发行版给出安装提示（只打印）。"""
    return env.package_hint("wl-clipboard（Wayland）或 xclip（Xorg）")


def copy_text(text: str, *, timeout: float = 5.0) -> str:
    """把文字写进剪贴板，返回使用的后端名。失败抛 ClipboardError。"""
    data = text.encode("utf-8")
    name = backend()
    if name is None:
        raise ClipboardError(
            "没有可用的剪贴板命令：Wayland 需要 wl-clipboard（wl-copy），Xorg 需要 xclip；"
            f"可以装：{package_hint()}"
        )
    if name == "wl-copy":
        argv = ["wl-copy", "--type", "text/plain;charset=utf-8"]
    else:
        argv = ["xclip", "-selection", "clipboard", "-in"]
    try:
        # 关键：stderr 用临时文件而不是管道。wl-copy / xclip 会把服务选择区的子进程
        # fork 到后台，子进程继承 stderr 管道，pipe 不关闭 communicate() 就会一直等到
        # 超时（明明剪贴板已经写成功了）。文件描述符没有这个「EOF 才结束」语义。
        with tempfile.TemporaryFile() as err:
            proc = subprocess.run(argv, input=data, stdout=subprocess.DEVNULL, stderr=err, timeout=timeout)
            err.seek(0)
            stderr = err.read()
    except FileNotFoundError as exc:
        raise ClipboardError(f"找不到 {name}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipboardError(f"{name} 超时（{timeout:.0f}s）：剪贴板服务没有响应") from exc
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise ClipboardError(f"{name} 失败（退出码 {proc.returncode}）：{detail or '无输出'}")
    return name


def copy_image(path) -> str:
    """把图片文件放进剪贴板（`inject.mode=clipboard` 时可选用，M2 才需要）。"""
    name = backend()
    if name == "wl-copy":
        argv = ["wl-copy", "--type", "image/png"]
    elif name == "xclip":
        argv = ["xclip", "-selection", "clipboard", "-t", "image/png", "-in"]
    else:
        raise ClipboardError("没有可用的剪贴板命令")
    try:
        with open(path, "rb") as handle:
            proc = subprocess.run(
                argv, stdin=handle, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5.0
            )
    except FileNotFoundError as exc:
        raise ClipboardError(f"找不到 {name}") from exc
    except OSError as exc:
        raise ClipboardError(f"读不了图片 {path}：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise ClipboardError(f"{name} 失败：{detail or '无输出'}")
    return name


__all__ = ["ClipboardError", "backend", "copy_image", "copy_text", "package_hint"]
