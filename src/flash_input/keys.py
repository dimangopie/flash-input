"""键名：浏览器 `KeyboardEvent.code` ↔ Linux evdev 键名（`KEY_*`）。

设置页里录快捷键靠这张表：网页只能拿到 `event.code`（物理按键位置，与键盘布局无关），
evdev 只认 `KEY_*`。两边用同一份映射（`KEYMAP` 会被序列化进页面），
所以不会出现「网页能录、后台不认」的错位。

约定（与 evdev 的语义一致）：

- **修饰键**：`KEY_LEFTCTRL` / `KEY_RIGHTCTRL` / `KEY_LEFTALT` / `KEY_RIGHTALT` /
  `KEY_LEFTSHIFT` / `KEY_RIGHTSHIFT` / `KEY_LEFTMETA` / `KEY_RIGHTMETA`。
- **按住说话的键**必须是**单个修饰键**：普通键按下会被当前窗口收到（我们只是旁听，
  不拦截），拿字母当按住说话键等于一边说话一边往文档里打字。
- **组合键**（抓屏）：若干修饰键 + 恰好一个主键，顺序必须「修饰键在前、主键在后」，
  如 `KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O`；单个主键（如 `KEY_F9`）也允许。
"""

from __future__ import annotations

MODIFIERS = (
    "KEY_LEFTCTRL",
    "KEY_LEFTALT",
    "KEY_LEFTSHIFT",
    "KEY_LEFTMETA",
    "KEY_RIGHTCTRL",
    "KEY_RIGHTALT",
    "KEY_RIGHTSHIFT",
    "KEY_RIGHTMETA",
)
MODIFIER_SET = frozenset(MODIFIERS)

# Linux evdev 键码（取自 `linux/input-event-codes.h`），热键监听直接读 /dev/input 时需要。
KEY_CODES: dict[str, int] = {
    "KEY_ESC": 1,
    "KEY_1": 2,
    "KEY_2": 3,
    "KEY_3": 4,
    "KEY_4": 5,
    "KEY_5": 6,
    "KEY_6": 7,
    "KEY_7": 8,
    "KEY_8": 9,
    "KEY_9": 10,
    "KEY_0": 11,
    "KEY_MINUS": 12,
    "KEY_EQUAL": 13,
    "KEY_BACKSPACE": 14,
    "KEY_TAB": 15,
    "KEY_Q": 16,
    "KEY_W": 17,
    "KEY_E": 18,
    "KEY_R": 19,
    "KEY_T": 20,
    "KEY_Y": 21,
    "KEY_U": 22,
    "KEY_I": 23,
    "KEY_O": 24,
    "KEY_P": 25,
    "KEY_LEFTBRACE": 26,
    "KEY_RIGHTBRACE": 27,
    "KEY_ENTER": 28,
    "KEY_LEFTCTRL": 29,
    "KEY_A": 30,
    "KEY_S": 31,
    "KEY_D": 32,
    "KEY_F": 33,
    "KEY_G": 34,
    "KEY_H": 35,
    "KEY_J": 36,
    "KEY_K": 37,
    "KEY_L": 38,
    "KEY_SEMICOLON": 39,
    "KEY_APOSTROPHE": 40,
    "KEY_GRAVE": 41,
    "KEY_LEFTSHIFT": 42,
    "KEY_BACKSLASH": 43,
    "KEY_Z": 44,
    "KEY_X": 45,
    "KEY_C": 46,
    "KEY_V": 47,
    "KEY_B": 48,
    "KEY_N": 49,
    "KEY_M": 50,
    "KEY_COMMA": 51,
    "KEY_DOT": 52,
    "KEY_SLASH": 53,
    "KEY_RIGHTSHIFT": 54,
    "KEY_KPASTERISK": 55,
    "KEY_LEFTALT": 56,
    "KEY_SPACE": 57,
    "KEY_CAPSLOCK": 58,
    "KEY_F1": 59,
    "KEY_F2": 60,
    "KEY_F3": 61,
    "KEY_F4": 62,
    "KEY_F5": 63,
    "KEY_F6": 64,
    "KEY_F7": 65,
    "KEY_F8": 66,
    "KEY_F9": 67,
    "KEY_F10": 68,
    "KEY_SCROLLLOCK": 70,
    "KEY_KP7": 71,
    "KEY_KP8": 72,
    "KEY_KP9": 73,
    "KEY_KPMINUS": 74,
    "KEY_KP4": 75,
    "KEY_KP5": 76,
    "KEY_KP6": 77,
    "KEY_KPPLUS": 78,
    "KEY_KP1": 79,
    "KEY_KP2": 80,
    "KEY_KP3": 81,
    "KEY_KP0": 82,
    "KEY_KPDOT": 83,
    "KEY_F11": 87,
    "KEY_F12": 88,
    "KEY_KPENTER": 96,
    "KEY_RIGHTCTRL": 97,
    "KEY_KPSLASH": 98,
    "KEY_SYSRQ": 99,
    "KEY_RIGHTALT": 100,
    "KEY_HOME": 102,
    "KEY_UP": 103,
    "KEY_PAGEUP": 104,
    "KEY_LEFT": 105,
    "KEY_RIGHT": 106,
    "KEY_END": 107,
    "KEY_DOWN": 108,
    "KEY_PAGEDOWN": 109,
    "KEY_INSERT": 110,
    "KEY_DELETE": 111,
    "KEY_MUTE": 113,
    "KEY_VOLUMEDOWN": 114,
    "KEY_VOLUMEUP": 115,
    "KEY_KPEQUAL": 117,
    "KEY_PAUSE": 119,
    "KEY_LEFTMETA": 125,
    "KEY_RIGHTMETA": 126,
    "KEY_COMPOSE": 127,
    "KEY_NEXTSONG": 163,
    "KEY_PLAYPAUSE": 164,
    "KEY_PREVIOUSSONG": 165,
    "KEY_STOPCD": 166,
    "KEY_F13": 183,
    "KEY_F14": 184,
    "KEY_F15": 185,
    "KEY_F16": 186,
    "KEY_F17": 187,
    "KEY_F18": 188,
    "KEY_F19": 189,
    "KEY_F20": 190,
    "KEY_F21": 191,
    "KEY_F22": 192,
    "KEY_F23": 193,
    "KEY_F24": 194,
}

# 浏览器 KeyboardEvent.code -> evdev 键名。字母 / 数字 / F 键 / 小键盘数字在下面按规律补全。
KEYMAP: dict[str, str] = {
    "ShiftLeft": "KEY_LEFTSHIFT",
    "ShiftRight": "KEY_RIGHTSHIFT",
    "ControlLeft": "KEY_LEFTCTRL",
    "ControlRight": "KEY_RIGHTCTRL",
    "AltLeft": "KEY_LEFTALT",
    "AltRight": "KEY_RIGHTALT",
    "MetaLeft": "KEY_LEFTMETA",
    "MetaRight": "KEY_RIGHTMETA",
    "Escape": "KEY_ESC",
    "Tab": "KEY_TAB",
    "CapsLock": "KEY_CAPSLOCK",
    "Enter": "KEY_ENTER",
    "Space": "KEY_SPACE",
    "Backspace": "KEY_BACKSPACE",
    "Insert": "KEY_INSERT",
    "Delete": "KEY_DELETE",
    "Home": "KEY_HOME",
    "End": "KEY_END",
    "PageUp": "KEY_PAGEUP",
    "PageDown": "KEY_PAGEDOWN",
    "ArrowUp": "KEY_UP",
    "ArrowDown": "KEY_DOWN",
    "ArrowLeft": "KEY_LEFT",
    "ArrowRight": "KEY_RIGHT",
    "PrintScreen": "KEY_SYSRQ",
    "ScrollLock": "KEY_SCROLLLOCK",
    "Pause": "KEY_PAUSE",
    "ContextMenu": "KEY_COMPOSE",
    "Minus": "KEY_MINUS",
    "Equal": "KEY_EQUAL",
    "BracketLeft": "KEY_LEFTBRACE",
    "BracketRight": "KEY_RIGHTBRACE",
    "Backslash": "KEY_BACKSLASH",
    "Semicolon": "KEY_SEMICOLON",
    "Quote": "KEY_APOSTROPHE",
    "Backquote": "KEY_GRAVE",
    "Comma": "KEY_COMMA",
    "Period": "KEY_DOT",
    "Slash": "KEY_SLASH",
    "NumpadAdd": "KEY_KPPLUS",
    "NumpadSubtract": "KEY_KPMINUS",
    "NumpadMultiply": "KEY_KPASTERISK",
    "NumpadDivide": "KEY_KPSLASH",
    "NumpadDecimal": "KEY_KPDOT",
    "NumpadEnter": "KEY_KPENTER",
    "NumpadEqual": "KEY_KPEQUAL",
    "AudioVolumeMute": "KEY_MUTE",
    "AudioVolumeDown": "KEY_VOLUMEDOWN",
    "AudioVolumeUp": "KEY_VOLUMEUP",
    "MediaPlayPause": "KEY_PLAYPAUSE",
    "MediaStop": "KEY_STOPCD",
    "MediaTrackNext": "KEY_NEXTSONG",
    "MediaTrackPrevious": "KEY_PREVIOUSSONG",
}

_LETTERS = {f"KEY{chr(code)}": f"KEY_{chr(code)}" for code in range(ord("A"), ord("Z") + 1)}
_DIGITS = {f"KEY{digit}": f"KEY_{digit}" for digit in range(10)}
_FKEYS = {f"KEY_F{n}": f"KEY_F{n}" for n in range(1, 25)}
_NUMPAD = {f"KEY_KP{digit}": f"KEY_KP{digit}" for digit in range(10)}

SUPPORTED_KEYS = frozenset(
    list(KEYMAP.values())
    + list(_LETTERS.values())
    + list(_DIGITS.values())
    + list(_FKEYS.values())
    + list(_NUMPAD.values())
)
MAIN_KEYS = SUPPORTED_KEYS - MODIFIER_SET


class KeyError(ValueError):
    """键名不合法。"""


def parse(combo: str) -> list[str]:
    """拆成键名列表；只做语法层校验（不判断是不是修饰键）。"""
    if not isinstance(combo, str):
        raise KeyError("快捷键必须是字符串")
    tokens = [token.strip() for token in combo.split("+")]
    if not all(tokens) or any(not token for token in tokens):
        raise KeyError(f"快捷键格式不对：{combo!r}（用 + 连接，如 KEY_LEFTMETA+KEY_O）")
    unknown = [token for token in tokens if token not in SUPPORTED_KEYS]
    if unknown:
        raise KeyError(f"不认识的键 {', '.join(unknown)}；只接受 Linux KEY_* 键名（可以在设置页里录制）")
    if len(set(tokens)) != len(tokens):
        raise KeyError(f"快捷键里有重复的键：{combo!r}")
    return tokens


def validate_hold_key(combo: str) -> str:
    """校验「按住说话」键：必须是单个修饰键。返回规范化后的字符串。"""
    tokens = parse(combo)
    if len(tokens) != 1:
        raise KeyError(f"语音键必须是单个修饰键（如 KEY_RIGHTCTRL），不能是组合键：{combo!r}")
    if tokens[0] not in MODIFIER_SET:
        raise KeyError(
            f"语音键必须是修饰键（Ctrl / Alt / Shift / Super），{tokens[0]} 会在按住时被当前窗口收到"
        )
    return tokens[0]


def validate_combo(combo: str) -> str:
    """校验组合键：修饰键在前、恰好一个主键在后。返回原文（顺序由录制端保证）。"""
    tokens = parse(combo)
    main = [token for token in tokens if token not in MODIFIER_SET]
    if len(main) != 1:
        raise KeyError(f"组合键里要恰好有一个非修饰键（主键）：{combo!r}")
    if tokens[-1] not in MAIN_KEYS:
        raise KeyError(f"组合键的主键要放在最后：{combo!r}")
    return "+".join(tokens)


_ACCEL_MODS = {
    "KEY_LEFTCTRL": "<Primary>",
    "KEY_RIGHTCTRL": "<Primary>",
    "KEY_LEFTSHIFT": "<Shift>",
    "KEY_RIGHTSHIFT": "<Shift>",
    "KEY_LEFTALT": "<Alt>",
    "KEY_RIGHTALT": "<Alt>",
    "KEY_LEFTMETA": "<Super>",
    "KEY_RIGHTMETA": "<Super>",
}

_ACCEL_NAMES = {
    "KEY_SPACE": "space",
    "KEY_TAB": "Tab",
    "KEY_ENTER": "Return",
    "KEY_ESC": "Escape",
    "KEY_BACKSPACE": "BackSpace",
    "KEY_INSERT": "Insert",
    "KEY_DELETE": "Delete",
    "KEY_HOME": "Home",
    "KEY_END": "End",
    "KEY_PAGEUP": "Page_Up",
    "KEY_PAGEDOWN": "Page_Down",
    "KEY_UP": "Up",
    "KEY_DOWN": "Down",
    "KEY_LEFT": "Left",
    "KEY_RIGHT": "Right",
    "KEY_SYSRQ": "Print",
    "KEY_MINUS": "minus",
    "KEY_EQUAL": "equal",
    "KEY_COMMA": "comma",
    "KEY_DOT": "period",
    "KEY_SLASH": "slash",
    "KEY_SEMICOLON": "semicolon",
    "KEY_APOSTROPHE": "apostrophe",
    "KEY_LEFTBRACE": "bracketleft",
    "KEY_RIGHTBRACE": "bracketright",
    "KEY_BACKSLASH": "backslash",
    "KEY_GRAVE": "grave",
}


def to_accelerator(combo: str) -> str:
    """``KEY_LEFTCTRL+KEY_LEFTALT+KEY_K`` → ``<Primary><Alt>k``（GNOME/gsettings 的写法）。"""
    mods = []
    main = ""
    for name in parse(combo):
        if name in _ACCEL_MODS:
            token = _ACCEL_MODS[name]
            if token not in mods:
                mods.append(token)
            continue
        if main:
            raise KeyError(f"组合键只能有一个主键：{combo}")
        if name in _ACCEL_NAMES:
            main = _ACCEL_NAMES[name]
        elif name.startswith("KEY_F") and name[5:].isdigit():
            main = name[4:].lower()
        elif name.startswith("KEY_KP") and name[6:].isdigit():
            main = "KP_" + name[6:]
        elif len(name) == 5 and name.startswith("KEY_"):
            main = name[4:].lower()
        else:
            raise KeyError(f"不知道 {name} 对应的 GTK 键名")
    if not main:
        raise KeyError(f"组合键缺少主键：{combo}")
    return "".join(mods) + main


def describe(combo: str) -> str:
    """把 KEY_* 组合转成人看得懂的形式（Ctrl+Shift+V），给日志与 CLI 用。"""
    names = {
        "KEY_LEFTCTRL": "Ctrl",
        "KEY_RIGHTCTRL": "RCtrl",
        "KEY_LEFTALT": "Alt",
        "KEY_RIGHTALT": "RAlt",
        "KEY_LEFTSHIFT": "Shift",
        "KEY_RIGHTSHIFT": "RShift",
        "KEY_LEFTMETA": "Super",
        "KEY_RIGHTMETA": "RSuper",
    }
    parts: list[str] = []
    for token in combo.split("+"):
        token = token.strip()
        if token in names:
            parts.append(names[token])
        elif token.startswith("KEY_"):
            parts.append(token[4:])
        elif token:
            parts.append(token)
    return "+".join(parts)


__all__ = [
    "KEYMAP",
    "MAIN_KEYS",
    "MODIFIERS",
    "MODIFIER_SET",
    "SUPPORTED_KEYS",
    "KeyError",
    "describe",
    "parse",
    "validate_combo",
    "validate_hold_key",
]
