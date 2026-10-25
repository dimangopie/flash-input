"""抓屏：按一下 Print 打开**系统自己的截屏 UI**，取走它存下的 PNG 交给 OCR。

为什么不调 portal 的 `interactive=true`（OUTLINE §2.2）：GNOME 46 的会话后端对它一律回
`response=2`（Shell 里 `screenshotUI.open()` 抓帧失败后立刻关窗），选区窗口起不来；但**同一个
系统截屏 UI 用 Print 键是好的**。所以这里注入 `Print`，等系统把用户框选的结果存进
`~/Pictures`（GNOME 46 实际落在 `~/Pictures/Screenshots/`，所以递归扫这个目录），
取走、删掉原件、复制一份到运行时目录再返回。

portal 的整屏路径（`fullscreen` 模式）已按用户要求删除：抓屏只有「系统截屏 UI」一条路，
因此抓屏不再依赖 `xdg-desktop-portal`，只依赖 `/dev/uinput`（注入 Print）。
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import select
import shutil
import struct
import subprocess
import time
from pathlib import Path

from PIL import Image

from . import xdg

# linux/input-event-codes.h
KEY_SYSRQ = 99  # Print


class CaptureError(RuntimeError):
    """抓屏失败：uinput 不可用、用户取消、等不到系统存图。"""


def _result_path(runtime_dir: Path | None = None) -> Path:
    root = runtime_dir or xdg.runtime_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"shot-{os.getpid()}.png"


def discard(path: Path) -> None:
    """识别完就把截图删掉（调用方负责；已经不存在也算成功）。"""
    with contextlib.suppress(OSError):
        Path(path).unlink()


def _complete_png(path: Path) -> bool:
    """系统是边写边落盘的：文件刚出现时可能只有半张图。

    判定「写完」= 大小连续两次不变、且能整张解码。否则 OCR 会报
    `image file is truncated`（曾经的真实故障）。
    """
    try:
        first = path.stat()
    except OSError:
        return False
    if first.st_size <= 0:
        return False
    time.sleep(0.15)
    try:
        second = path.stat()
    except OSError:
        return False
    if second.st_size != first.st_size:
        return False
    try:
        with Image.open(path) as image:
            image.load()
    except Exception:
        return False
    return True


# ---------------------------------------------------------------- inotify
_IN_CREATE = 0x00000100
_IN_MOVED_TO = 0x00000080
_IN_CLOSE_WRITE = 0x00000008
_IN_ISDIR = 0x40000000
_INOTIFY_EVENT = struct.Struct("iIII")  # wd, mask, cookie, len


class _Inotify:
    """最小 inotify 封装（ctypes 直接调 libc，不引入第三方依赖）。"""

    def __init__(self, paths: list[Path]) -> None:
        self._libc = ctypes.CDLL("libc.so.6", use_errno=True)
        fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 失败")
        self.fd = fd
        self._dirs: dict[int, Path] = {}
        for path in paths:
            self.add(path)

    def add(self, path: Path) -> None:
        mask = _IN_CREATE | _IN_MOVED_TO | _IN_CLOSE_WRITE
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(str(path)), mask)
        if wd >= 0:
            self._dirs[wd] = path

    def drain(self) -> None:
        """读空事件队列；新建的子目录补上 watch（截图可能落在新目录里）。"""
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            return
        offset = 0
        while offset + _INOTIFY_EVENT.size <= len(data):
            wd, mask, _cookie, name_len = _INOTIFY_EVENT.unpack_from(data, offset)
            offset += _INOTIFY_EVENT.size + name_len
            base = self._dirs.get(wd)
            if base is None or not name_len:
                continue
            name = data[offset - name_len : offset].split(b"\0", 1)[0].decode("utf-8", "replace")
            if mask & _IN_ISDIR and mask & (_IN_CREATE | _IN_MOVED_TO):
                self.add(base / name)

    def wait(self, timeout: float) -> None:
        try:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        except OSError:
            return
        if ready:
            self.drain()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.fd)

    def __enter__(self) -> _Inotify:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _watch_dirs(pictures: Path) -> list[Path]:
    dirs = [pictures]
    sub = pictures / "Screenshots"
    if sub.is_dir():
        dirs.append(sub)
    return dirs


def _new_pngs(pictures: Path, before: set[Path], started: float) -> list[Path]:
    if not pictures.is_dir():
        return []
    found = []
    for path in pictures.rglob("*.png"):
        if path in before:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime + 1.0 >= started:
            found.append(path)
    return found


def pictures_dir() -> Path:
    """系统截屏的**根**目录：$XDG_PICTURES_DIR，否则 ~/Pictures，再退 ~。

    真正落盘位置由 GNOME 决定，可能是根目录下的 `Screenshots/` 子目录
    （本机就是这个），所以查找要递归。
    """
    value = os.environ.get("XDG_PICTURES_DIR")
    if value:
        return Path(value)
    guess = Path.home() / "Pictures"
    return guess if guess.is_dir() else Path.home()


SPECTACLE = "spectacle"


def spectacle_capture(timeout: float = 60.0, *, runtime_dir: Path | None = None) -> Path:
    """KDE：``spectacle --region --output <file>`` 自己弹选区并写文件。

    没有 Spectacle 时给一句能照做的提示，而不是让用户对着「等不到图」发呆。
    """
    exe = shutil.which(SPECTACLE)
    if exe is None:
        raise CaptureError(
            "没找到 spectacle：KDE 上用它打开系统截屏窗口（Debian/Ubuntu: sudo apt install spectacle；"
            "Arch: sudo pacman -S spectacle）。也可以把 ocr.trigger 改回 inject，用 Print 键触发"
        )
    target = _result_path(runtime_dir)
    if target.exists():
        target.unlink(missing_ok=True)
    try:
        subprocess.run(
            [exe, "--region", "--output", str(target), "--nonnotify"],
            timeout=timeout,
            check=False,
            capture_output=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"Spectacle 等待选区超时（{timeout:.0f}s）") from exc
    except OSError as exc:
        raise CaptureError(f"启动 spectacle 失败：{exc}") from exc
    if not target.is_file():
        raise CaptureError("Spectacle 没有产出图片（多半是取消了选区，或它把图存到了别处）")
    if not _complete_png(target):
        raise CaptureError("Spectacle 写出的图片不完整，再试一次")
    return target


def system_capture(
    timeout: float = 60.0,
    *,
    runtime_dir: Path | None = None,
    pictures: Path | None = None,
    trigger: str = "inject",
) -> Path:
    """用「系统自己的截屏 UI」抓一块，返回运行时目录里的副本。

    三种触发方式（``ocr.trigger``）：

    - ``inject``（默认）：我们注入一下 ``Print``，系统 UI 随之打开；
    - ``listen``：由 GNOME 快捷键绑定（Mutter 侧）负责打开 UI，我们只监听文件；
    - ``spectacle``（KDE）：直接调 Spectacle 的区域模式，它自己弹选区并把 PNG
      写到指定路径（KDE 上没有 GNOME 那种「截屏后自动落进 ~/Pictures」的行为）。

    为什么要绕开 portal（OUTLINE §2.2）：GNOME 46 的 `interactive=true` 每回必
    `response=2`（Shell 里 `screenshotUI.open()` 抓帧失败就关窗），但系统截屏 UI
    本身是好的，只是入口得自己给。用户框选后系统把 PNG 存进 ``~/Pictures``
    （GNOME 46 实际落在 ``~/Pictures/Screenshots/``），这里用 inotify 等它写完，
    取走、删原件、复制到运行时目录。
    """
    if trigger == "spectacle":
        return spectacle_capture(timeout=timeout, runtime_dir=runtime_dir)
    if trigger not in ("inject", "listen"):
        raise CaptureError(f"未知的抓屏触发方式：{trigger}")
    pictures = pictures or pictures_dir()
    started = time.time()
    before = set(pictures.rglob("*.png")) if pictures.is_dir() else set()

    if trigger == "inject":
        from . import inject

        try:
            inject.tap_key(KEY_SYSRQ)
        except inject.InjectUnavailable as exc:
            raise CaptureError(f"触发系统截屏需要 uinput：{exc}") from exc
        except inject.InjectError as exc:
            raise CaptureError(f"触发系统截屏失败：{exc}") from exc

    deadline = time.monotonic() + timeout
    watcher: _Inotify | None = None
    try:
        watcher = _Inotify(_watch_dirs(pictures))
    except OSError:
        watcher = None
    try:
        while time.monotonic() < deadline:
            for path in _new_pngs(pictures, before, started):
                if not _complete_png(path):
                    continue  # 还在写：等下一个事件再看
                target = _result_path(runtime_dir)
                xdg.ensure(target.parent)
                try:
                    shutil.copyfile(path, target)
                except OSError as exc:
                    raise CaptureError(f"读取系统截屏失败：{exc}") from exc
                discard(path)  # 用户要的「识别完删掉照片」：系统原件立刻删
                return target
            remaining = deadline - time.monotonic()
            if watcher is not None:
                watcher.wait(min(0.5, remaining))
            else:
                time.sleep(min(0.1, max(0.0, remaining)))
    finally:
        if watcher is not None:
            watcher.close()
    raise CaptureError(
        f"等待系统截屏超时（{timeout:.0f}s）：在系统截屏窗口里选「Select Area to Grab」框选，"
        "再点「Take Screenshot」，会自动识别；按 Esc 取消"
    )


__all__ = [
    "CaptureError",
    "discard",
    "pictures_dir",
    "system_capture",
]
