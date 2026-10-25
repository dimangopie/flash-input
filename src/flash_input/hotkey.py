"""evdev 热键：直接读 `/dev/input/event*` 的按下/松开事件。

为什么不用 `python-evdev`：读 `struct input_event` 只有二十来行，少一个依赖就少一层
版本约束；键码表来自 `keys.KEY_CODES`（生成自 `linux/input-event-codes.h`）。

两条触发规则（见 OUTLINE §2.1 / §2.4）：

- **语音键**（默认右 Ctrl）是「按住说话」：按下后先等一个守护窗口（`guard_ms`），
  窗口内按了其它键 → 判为组合键（Ctrl+C 之类），不录音；窗口内没按其它键 → 开始录音，
  松手结束。短于 `min_hold_ms` 的轻点不录音、不通知。
- **抓屏键**（默认 `Super+Shift+O`）是「按下即抓」：修饰键全部按住 + 主键按下才算命中，
  按住不放只触发一次。

设备权限：`/dev/input/event*` 默认 `root:input 660`，而 udev 的 `70-uaccess.rules`
只给摇杆打了 `uaccess`。`setup-permissions` 会补一条
`SUBSYSTEM=="input", ENV{ID_INPUT_KEYBOARD}=="1", TAG+="uaccess"`。
"""

from __future__ import annotations

import contextlib
import os
import selectors
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import keys

EV_SYN = 0
EV_KEY = 1
KEY_PRESS = 1
KEY_RELEASE = 0
KEY_REPEAT = 2

# 热插拔重扫间隔：USB 键盘、别的工具建的 uinput 设备都靠它接进来。
_RESCAN_S = 0.5

_EVENT = struct.Struct("llHHi")  # struct input_event（64 位：timeval 2×8 + u16 + u16 + s32）

_KEY_NAME_BY_CODE = {code: name for name, code in keys.KEY_CODES.items()}


def _ioc(direction: int, type_: str, nr: int, size: int) -> int:
    return direction << 30 | size << 16 | ord(type_) << 8 | nr


def _eviocgbit(ev_type: int, size: int) -> int:
    return _ioc(2, "E", 0x20 + ev_type, size)


def _eviocgname(size: int) -> int:
    return _ioc(2, "E", 0x06, size)


EVIOCGBIT_EV = _eviocgbit(0, 8)
EVIOCGBIT_KEY = _eviocgbit(EV_KEY, 96)  # KEY_MAX 767 -> 96 字节
EVIOCGNAME = _eviocgname(256)


class HotkeyError(RuntimeError):
    """读不了 dev/input（权限、设备消失等）。"""


@dataclass
class Device:
    path: str
    name: str
    fd: int
    keys: bytes = b""
    events: int = 0

    def has_key(self, code: int) -> bool:
        index = code // 8
        return index < len(self.keys) and bool(self.keys[index] & (1 << (code % 8)))

    def read(self) -> list[tuple[int, int]]:
        """读当前可读的事件；返回 `(code, value)`，只保留 EV_KEY 的按下/松开。"""
        out: list[tuple[int, int]] = []
        while True:
            try:
                chunk = os.read(self.fd, _EVENT.size * 64)
            except BlockingIOError:
                break
            except OSError as exc:
                raise HotkeyError(f"读 {self.path} 失败：{exc}") from exc
            if not chunk:
                break
            usable = len(chunk) - len(chunk) % _EVENT.size
            for offset in range(0, usable, _EVENT.size):
                _sec, _usec, etype, code, value = _EVENT.unpack_from(chunk, offset)
                if etype == EV_KEY:
                    out.append((code, value))
        return out

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.fd)

    def describe(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "keys": len(self.keys),
        }


def open_device(path: str) -> Device:
    import fcntl

    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    name_buf = bytearray(256)
    try:
        fcntl.ioctl(fd, EVIOCGNAME, name_buf, True)
    except OSError:
        name_buf = bytearray(b"unknown")
    name = name_buf.split(b"\x00", 1)[0].decode("utf-8", "replace") or os.path.basename(path)
    ev_buf = bytearray(8)
    try:
        fcntl.ioctl(fd, EVIOCGBIT_EV, ev_buf, True)
    except OSError as exc:
        os.close(fd)
        raise HotkeyError(f"不是输入设备：{path}（{exc}）") from exc
    if not ev_buf[EV_KEY // 8] & (1 << (EV_KEY % 8)):
        os.close(fd)
        raise HotkeyError(f"{path} 不产生按键事件")
    key_buf = bytearray(96)
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, EVIOCGBIT_KEY, key_buf, True)
    return Device(path=path, name=name, fd=fd, keys=bytes(key_buf))


def required_codes(voice_key: str, ocr_key: str) -> list[int]:
    names = keys.parse(voice_key) + keys.parse(ocr_key)
    return [keys.KEY_CODES[name] for name in names]


def discover(devices: str = "auto", required: list[int] | None = None) -> tuple[list[Device], list[str]]:
    """打开可读的键盘设备。

    返回 `(devices, notes)`：`notes` 是给人看的失败原因，`doctor` / `hotkey test` 会打印。
    一个设备都打不开时返回空列表（调用方决定是报错还是降级）。
    """
    required = required or []
    notes: list[str] = []
    paths: list[str] = []
    if devices.strip() == "auto":
        paths = sorted(str(p) for p in Path("/dev/input").glob("event*"))
        if not paths:
            notes.append("没有 /dev/input/event*（容器/沙箱里很常见，桌面会话里才有）")
    else:
        paths = [p.strip() for p in devices.split(",") if p.strip()]

    opened: list[Device] = []
    for path in paths:
        try:
            device = open_device(path)
        except PermissionError as exc:
            notes.append(
                f"{path}: 没权限读（{exc.strerror}；跑 `finput setup-permissions --install` 后重新登录）"
            )
            continue
        except HotkeyError as exc:
            # 鼠标、电源键、摄像头这类设备本来就没有 EV_KEY 键盘能力：不是错误，静默跳过。
            if "不产生按键事件" in str(exc):
                continue
            notes.append(f"{path}: {exc}")
            continue
        except OSError as exc:
            notes.append(f"{path}: {exc}")
            continue
        if required and not all(device.has_key(code) for code in required):
            device.close()
            continue
        opened.append(device)

    if not opened and not notes and paths and required:
        notes.append("没有能同时提供语音键与抓屏键的键盘设备")
    return opened, notes


class Matcher:
    """纯逻辑状态机：喂键事件，吐动作。时间由调用方传入，方便单测。"""

    def __init__(
        self,
        voice_key: str = "KEY_RIGHTCTRL",
        ocr_key: str = "KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O",
        guard_ms: int = 300,
        min_hold_ms: int = 300,
        max_hold_s: int = 60,
    ) -> None:
        self.voice_key = voice_key
        tokens = keys.parse(ocr_key)
        self.ocr_mods = frozenset(tokens[:-1])
        self.ocr_main = tokens[-1]
        self.guard_s = guard_ms / 1000.0
        self.start_s = max(guard_ms, min_hold_ms) / 1000.0
        self.max_hold_s = float(max_hold_s)

        self._held: set[str] = set()
        self._press_at: float | None = None
        self._recording = False
        self._aborted = False
        self._ocr_latch = False
        self._last_ocr_at = -1e9

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def pending(self) -> bool:
        return self._press_at is not None

    def reset(self) -> None:
        self._held.clear()
        self._press_at = None
        self._recording = False
        self._aborted = False
        self._ocr_latch = False

    def feed(self, name: str, value: int, now: float) -> list[str]:
        if value == KEY_REPEAT:
            return []
        if name not in keys.SUPPORTED_KEYS:
            return []

        actions: list[str] = []
        if value == KEY_PRESS:
            if name == self.ocr_main and not self._ocr_latch:
                if self.ocr_mods and self.ocr_mods <= self._held and now - self._last_ocr_at > 0.4:
                    self._ocr_latch = True
                    self._last_ocr_at = now
                    self._abort_voice()
                    actions.append("ocr")
                elif not self.ocr_mods:
                    self._ocr_latch = True
                    self._last_ocr_at = now
                    actions.append("ocr")
            if name in keys.MODIFIER_SET:
                self._held.add(name)
            if name == self.voice_key:
                if self._press_at is None:
                    self._press_at = now
                    self._recording = False
                    self._aborted = False
            elif self.pending and not self._aborted and now - self._press_at <= self.guard_s:
                self._abort_voice()
        else:  # KEY_RELEASE
            if name in keys.MODIFIER_SET:
                self._held.discard(name)
            if name == self.ocr_main:
                self._ocr_latch = False
            if name == self.voice_key:
                if self._recording:
                    self._recording = False
                    self._press_at = None
                    actions.append("voice_stop")
                else:
                    self._press_at = None
                    self._aborted = False
        return actions

    def _abort_voice(self) -> None:
        if self._press_at is not None:
            self._aborted = True
            self._recording = False
            self._press_at = None

    def cancel(self) -> bool:
        """外部取消（`finput voice cancel`）。返回当时是否在录音。"""
        was = self._recording
        self._press_at = None
        self._recording = False
        self._aborted = False
        return was

    def tick(self, now: float) -> list[str]:
        if self._press_at is None:
            return []
        elapsed = now - self._press_at
        if not self._recording and not self._aborted:
            if elapsed >= self.start_s:
                self._recording = True
                return ["voice_start"]
            return []
        if self._recording and elapsed >= self.max_hold_s:
            self._recording = False
            return ["voice_stop"]
        return []


class Listener(threading.Thread):
    """把设备事件喂给 `Matcher`，把动作交给回调。daemon 里只跑一个。"""

    def __init__(self, matcher: Matcher, devices: list[Device], on_action, on_key=None, rescan=None) -> None:
        super().__init__(name="flash-input-hotkey", daemon=True)
        self.matcher = matcher
        self.devices = devices
        self.on_action = on_action
        self.on_key = on_key
        # `rescan(existing_paths) -> list[Device]`：每 `_RESCAN_S` 秒问一次有没有新键盘。
        # 热插拔很常见（USB 键盘、别的工具建的 uinput 设备），只在启动时枚举一次不够。
        self.rescan = rescan
        self._paths = {device.path for device in devices}
        self._halt = threading.Event()  # 不能叫 _stop：那是 Thread 的方法

    def stop(self) -> None:
        self._halt.set()

    def close(self) -> None:
        for device in self.devices:
            device.close()

    def _rescan(self, selector: selectors.BaseSelector, now: float) -> list[Device]:
        try:
            fresh = self.rescan(self._paths) or []
        except Exception:
            return []
        added: list[Device] = []
        for device in fresh:
            if device.path in self._paths:
                device.close()
                continue
            self._paths.add(device.path)
            self.devices.append(device)
            selector.register(device.fd, selectors.EVENT_READ, device)
            added.append(device)
        return added

    def run(self) -> None:
        selector = selectors.DefaultSelector()
        for device in self.devices:
            selector.register(device.fd, selectors.EVENT_READ, device)
        next_scan = time.monotonic() + _RESCAN_S
        try:
            while not self._halt.is_set():
                for key, _mask in selector.select(timeout=0.05):
                    device = key.data
                    try:
                        events = device.read()
                    except HotkeyError:
                        selector.unregister(device.fd)
                        self._paths.discard(device.path)  # 拔掉的设备以后还能重新加回来
                        # status 里也别再挂着已消失的设备
                        with contextlib.suppress(ValueError):
                            self.devices.remove(device)
                        continue
                    for code, value in events:
                        name = _KEY_NAME_BY_CODE.get(code)
                        if not name:
                            continue
                        if self.on_key is not None:
                            self.on_key(name, value)
                        actions = self.matcher.feed(name, value, time.monotonic())
                        for action in actions:
                            self.on_action(action)
                now = time.monotonic()
                if self.rescan is not None and now >= next_scan:
                    next_scan = now + _RESCAN_S
                    fresh = self._rescan(selector, now)
                    _ = fresh
                for action in self.matcher.tick(time.monotonic()):
                    self.on_action(action)
        finally:
            selector.close()
            self.close()


def describe_devices(
    devices_cfg: str = "auto",
    voice_key: str = "KEY_RIGHTCTRL",
    ocr_key: str = "KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O",
) -> tuple[list[dict], list[str]]:
    try:
        required = required_codes(voice_key, ocr_key)
    except keys.KeyError as exc:
        raise HotkeyError(str(exc)) from exc
    devices, notes = discover(devices_cfg, required)
    described = [device.describe() for device in devices]
    for device in devices:
        device.close()
    return described, notes
