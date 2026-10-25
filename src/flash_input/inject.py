"""按键注入：自建 `/dev/uinput` 虚拟键盘，只发「粘贴」组合键。

为什么只发粘贴键（OUTLINE §2.3）：uinput 打中文必须经过输入法，直接打键位是英文；
所以文字永远先进剪贴板，再发 `Ctrl+V` 让目标应用自己粘贴。

依赖面刻意压到最小：
- 不依赖 ydotool / wtype / xdotool（它们要么自己也是 uinput 包装，要么只管一种会话）；
- 不依赖 `python-evdev`（只用 4 个 ioctl + 24 字节的 `input_event` 结构）；
- 不依赖 X11，也不依赖 Wayland 协议——内核接口在两种会话下完全一样。

`inject.mode`：
- `auto`（默认）：能写 uinput 就粘贴，不能就只进剪贴板（并说明原因）
- `uinput`：强制粘贴，uinput 不可用时报错（退出码 5）
- `clipboard`：只写剪贴板，不发键
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import struct
import subprocess
import time
from dataclasses import dataclass

from . import clipboard, env

# ---- 内核常量（linux/input-event-codes.h / linux/uinput.h，避免为此引入依赖） ----

EV_SYN = 0x00
EV_KEY = 0x01
SYN_REPORT = 0x00

KEY_LEFTCTRL = 29
KEY_LEFTSHIFT = 42
KEY_LEFTALT = 56
KEY_V = 47
KEY_INSERT = 110

# `_IOW('U', 100, int)` 等：x86_64 上是这些固定值；不依赖 C 头文件。
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_SETUP = 0x405C5503  # _IOW('U', 3, struct uinput_setup)：8 + 80 + 4 = 92 字节
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

# struct uinput_setup { struct input_id id; char name[80]; __u32 ff_effects_max; }
_INPUT_ID_BUS_USB = 0x03
_DEVICE_NAME = b"flash-input"

# 粘贴组合键：修饰键在前，主键固定为 V。配置里只允许这几种（config.validate 同一份列表）。
# 名字 →（修饰键序列, 主键）。主键不一定是 V：Shift+Insert 也是终端里常见的粘贴键。
PASTE_COMBOS: dict[str, tuple[tuple[int, ...], int]] = {
    "ctrl+v": ((KEY_LEFTCTRL,), KEY_V),
    "ctrl+shift+v": ((KEY_LEFTCTRL, KEY_LEFTSHIFT), KEY_V),
    "ctrl+alt+v": ((KEY_LEFTCTRL, KEY_LEFTALT), KEY_V),
    "shift+insert": ((KEY_LEFTSHIFT,), KEY_INSERT),
    "none": ((), KEY_V),
}

# 64 位 Linux 的 struct input_event：timeval(2×8) + type + code + value = 24 字节。
_EVENT = struct.Struct("qqHHi")

# 设备创建后内核/合成器需要一点点时间把设备登记进去，立刻写事件可能被丢弃。
_CREATE_SETTLE_S = 0.06
_DESTROY_SETTLE_S = 0.03


class InjectError(RuntimeError):
    """注入失败（设备打不开 / 写不进去 / 组合键不认识）。"""


class InjectUnavailable(InjectError):
    """uinput 不可用（设备不存在或没有写权限），且调用方要求必须粘贴。"""


@dataclass(frozen=True)
class DeliverResult:
    """一次「文字 → 目标应用」的结果，给命令行的提示与日志用。"""

    mode: str  # uinput / clipboard
    clipboard: str  # 实际使用的剪贴板后端名
    note: str = ""  # 降级原因或跳过原因


# --------------------------------------------------------------------------- 探测


def available() -> bool:
    """能写 /dev/uinput 就返回 True。"""
    return env.uinput_present() and env.uinput_writable()


def requirement_hint() -> str:
    """uinput 不可用时给人看的一句话。"""
    if not env.uinput_present():
        return (
            "/dev/uinput 不存在：确认内核编入了 uinput 模块"
            "（`sudo modprobe uinput` 可手动加载，重启后需要 udev 规则才会自动加载）"
        )
    if not env.uinput_writable():
        return (
            "/dev/uinput 没有写权限：运行 `finput setup-permissions --install`"
            "（uaccess 规则，M5 提供）后重新登录；临时可用 `sudo usermod -aG input $USER`"
        )
    return ""


def session_locked() -> bool | None:
    """当前会话是否锁屏；判断不出来返回 None（按未锁屏处理）。

    锁屏时不要往桌面上打字：输入会进入锁屏对话框。用 logind 的 `LockedHint`，
    这是 GNOME / KDE / 各会话统一的信号，不需要读屏或猜。
    """
    session_id = os.environ.get("XDG_SESSION_ID") or "auto"
    try:
        proc = subprocess.run(
            ["loginctl", "show-session", session_id, "--property=LockedHint", "--value"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (proc.stdout or "").strip().lower()
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    return None


# --------------------------------------------------------------------------- 事件序列


def paste_combo(paste_key: str) -> tuple[tuple[int, ...], int]:
    """把配置字符串翻译成（修饰键码, 主键码）；'none' 返回空修饰 + V。"""
    try:
        return PASTE_COMBOS[paste_key]
    except KeyError as exc:
        raise InjectError(f"不认识的粘贴组合键：{paste_key!r}") from exc


def modifier_keys(paste_key: str) -> tuple[int, ...]:
    """只要修饰键部分；'none' 返回空元组。"""
    return paste_combo(paste_key)[0]


def event_sequence(modifiers: tuple[int, ...], key: int = KEY_V) -> list[tuple[int, int, int]]:
    """一次粘贴的完整事件序列（type, code, value），不含 SYN。

    顺序：修饰键按下 → 主键按下 → 主键抬起 → 修饰键逆序抬起。
    每个键变化后紧跟一次 `SYN_REPORT`（由 `_emit` 补），否则应用可能只看到半个组合。
    """
    seq = [(EV_KEY, code, 1) for code in modifiers]
    seq += [(EV_KEY, key, 1), (EV_KEY, key, 0)]
    seq += [(EV_KEY, code, 0) for code in reversed(modifiers)]
    return seq


def _emit(fd: int, events: list[tuple[int, int, int]]) -> None:
    now = time.time()
    sec, usec = int(now), int((now % 1) * 1_000_000)
    for event_type, code, value in events:
        os.write(fd, _EVENT.pack(sec, usec, event_type, code, value))
        os.write(fd, _EVENT.pack(sec, usec, EV_SYN, SYN_REPORT, 0))


# --------------------------------------------------------------------------- 设备


def _create_device(keys: tuple[int, ...]) -> int:
    """建一个只有几个键的虚拟键盘，返回 fd。调用方负责 `_destroy_device`。"""
    try:
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise InjectError(f"打开 /dev/uinput 失败：{exc}（{requirement_hint()}）") from exc
    try:
        # 只声明 EV_KEY；EV_SYN 是内核隐式支持的，对它调 UI_SET_EVBIT 会 EINVAL。
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        for code in set(keys) | {KEY_V}:
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        setup = struct.pack("HHHH80sI", _INPUT_ID_BUS_USB, 0x1209, 0x0001, 0x0001, _DEVICE_NAME, 0)
        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE)
    except OSError:
        os.close(fd)
        raise
    time.sleep(_CREATE_SETTLE_S)
    return fd


def _destroy_device(fd: int) -> None:
    try:
        fcntl.ioctl(fd, UI_DEV_DESTROY)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def tap_key(code: int) -> None:
    """按一下某个键（如 Print 打开系统截屏 UI、Esc 关掉它）。

    只注册这一个键，按下后立刻抬起；建/销设备与 `paste()` 用同一套逻辑。
    """
    fd = _create_device((code,))
    try:
        _emit(fd, [(EV_KEY, code, 1), (EV_KEY, code, 0)])
    finally:
        _destroy_device(fd)


def paste(paste_key: str = "shift+insert") -> None:
    """发一次粘贴组合键。失败抛 InjectError（带权限提示）。"""
    modifiers, key = paste_combo(paste_key)
    if not modifiers:
        return  # paste_key = none：不建设备、不发键
    if not env.uinput_present():
        raise InjectUnavailable(requirement_hint())
    try:
        fd = _create_device((*modifiers, key))
    except PermissionError as exc:
        raise InjectUnavailable(requirement_hint()) from exc
    except OSError as exc:
        raise InjectError(f"创建 uinput 设备失败：{exc}（{requirement_hint()}）") from exc
    try:
        _emit(fd, event_sequence(modifiers, key))
        time.sleep(_DESTROY_SETTLE_S)
    except OSError as exc:
        raise InjectError(f"写 uinput 事件失败：{exc}") from exc
    finally:
        _destroy_device(fd)


# --------------------------------------------------------------------------- 完整投递


def deliver(
    text: str, cfg: dict, *, force_mode: str | None = None, paste_enabled: bool = True
) -> DeliverResult:
    """把文字送到目标应用：先写剪贴板，再按 `inject.mode` 决定是否发粘贴键。

    `paste_enabled=False` 只写剪贴板（语音 / OCR 各自的「是否粘贴」开关会传 False）。
    参数名不能叫 `paste`：会把模块里的 `paste()` 函数遮住。
    """
    inject_cfg = cfg["inject"]
    if not paste_enabled:
        backend = clipboard.copy_text(text)
        return DeliverResult(mode="clipboard", clipboard=backend)
    mode = force_mode or inject_cfg["mode"]
    if mode not in ("auto", "uinput", "clipboard"):
        raise InjectError(f"inject.mode 只能是 auto / uinput / clipboard，收到 {mode!r}")

    backend = clipboard.copy_text(text)

    if session_locked():
        return DeliverResult(mode="clipboard", clipboard=backend, note="会话已锁屏，跳过粘贴")
    if mode == "clipboard":
        return DeliverResult(mode="clipboard", clipboard=backend)
    if not available():
        if mode == "uinput":
            raise InjectUnavailable(requirement_hint())
        return DeliverResult(mode="clipboard", clipboard=backend, note="uinput 不可用，只写入剪贴板")

    paste_key = inject_cfg["paste_key"]
    if paste_key == "none":
        return DeliverResult(mode="clipboard", clipboard=backend, note="paste_key = none，不自动粘贴")
    time.sleep(max(0, int(inject_cfg["paste_delay_ms"])) / 1000)
    try:
        paste(paste_key)
    except InjectError as exc:
        # auto 的语义是「尽量粘贴」：真粘贴失败时文字已经在剪贴板里，
        # 不要因为发键失败就丢掉整句转写结果。强制 uinput 才升级为错误。
        if mode == "uinput":
            raise
        return DeliverResult(mode="clipboard", clipboard=backend, note=f"粘贴失败，文字在剪贴板：{exc}")
    return DeliverResult(mode="uinput", clipboard=backend)


__all__ = [
    "PASTE_COMBOS",
    "DeliverResult",
    "InjectError",
    "InjectUnavailable",
    "available",
    "deliver",
    "event_sequence",
    "modifier_keys",
    "paste",
    "paste_combo",
    "requirement_hint",
    "session_locked",
]
