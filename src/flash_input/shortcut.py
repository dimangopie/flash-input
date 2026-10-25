"""GNOME 截屏快捷键的读写。

设置页保存 ``ocr.trigger``、以及 daemon reload 时会调用 :func:`apply_trigger`：
``listen`` 需要把抓屏热键加进 GNOME 的「显示截屏界面」，其余触发方式则还原默认。


只碰 ``org.gnome.shell.keybindings`` 里的两个键：``show-screenshot-ui``（「显示截屏界面」，
GNOME 46 上就是抓屏对话框）与旧的 ``screenshot``（不绑它，只在我们改过时还原）。
与语音热键无关——语音热键由 ``hotkey.py`` 直接读 ``/dev/input``。
"""

from __future__ import annotations

import ast
import subprocess

SCREENSHOT_SCHEMA = "org.gnome.shell.keybindings"


def _run(runner, *args: str) -> str:
    result = runner(args, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip() or f"gsettings 失败：{' '.join(args)}")
    return result.stdout


def _get(runner, key: str, schema: str = SCREENSHOT_SCHEMA) -> str:
    return _run(runner, "gsettings", "get", schema, key)


def _set(runner, key: str, value: str, schema: str = SCREENSHOT_SCHEMA) -> str:
    _run(runner, "gsettings", "set", schema, key, value)
    return value


#: 我们改绑的是 ``show-screenshot-ui``（「显示截屏界面」，GNOME 46 上就是那个抓屏对话框）；
#: ``screenshot`` 是旧的「直接截屏」动作，Ubuntu 默认绑 ``<Shift>Print``，我们只在它被我们
#: 改过时顺手还原，别的一概不碰。
SCREENSHOT_KEY = "show-screenshot-ui"
SCREENSHOT_KEYS = ("screenshot", "show-screenshot-ui")
DEFAULT_SHOW_SCREENSHOT_UI = ["Print"]
DEFAULT_SCREENSHOT = ["<Shift>Print"]


def screenshot_bindings(runner=subprocess.run) -> dict[str, str]:
    """读 GNOME 的两个截屏快捷键当前值（原样字符串，便于打印）。"""
    return {key: _get(runner, key, schema=SCREENSHOT_SCHEMA).strip() for key in SCREENSHOT_KEYS}


def parse_accels(raw: str) -> list[str]:
    """解析 gsettings 的 ``['Print', '<Super><Shift>o']`` 字符串。"""
    text = raw.strip()
    if text.startswith("@as "):
        text = text[4:].strip()
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _literal(accels: list[str]) -> str:
    return "[" + ", ".join(f"'{accel}'" for accel in accels) + "]"


def bind_screenshot(accelerator: str, runner=subprocess.run) -> str:
    """把 ``accelerator`` **加进** GNOME 的「显示截屏界面」（``show-screenshot-ui``）绑定。

    保留已有绑定（默认的 ``Print`` 不动），所以 ``Print`` 与本程序热键同时可用。
    顺手做一件收尾：``screenshot`` 若还停在旧版我们改过的组合键上，还原成 ``<Shift>Print``。
    """
    current_plain = _get(runner, "screenshot", schema=SCREENSHOT_SCHEMA).strip()
    if accelerator in current_plain:
        _set(runner, "screenshot", f"['{DEFAULT_SCREENSHOT[0]}']", schema=SCREENSHOT_SCHEMA)
    accels = parse_accels(_get(runner, SCREENSHOT_KEY, schema=SCREENSHOT_SCHEMA))
    # 保证默认的 Print 还在（用户可能被我们之前「覆盖式」绑定弄丢过），再追加本程序热键
    if DEFAULT_SHOW_SCREENSHOT_UI[0] not in accels:
        accels.insert(0, DEFAULT_SHOW_SCREENSHOT_UI[0])
    if accelerator not in accels:
        accels.append(accelerator)
    return _set(runner, SCREENSHOT_KEY, _literal(accels), schema=SCREENSHOT_SCHEMA)


def unbind_screenshot(runner=subprocess.run) -> str:
    """把 ``show-screenshot-ui`` 还原成 GNOME 默认（``['Print']``）。"""
    return _set(runner, SCREENSHOT_KEY, f"['{DEFAULT_SHOW_SCREENSHOT_UI[0]}']", schema=SCREENSHOT_SCHEMA)


def apply_trigger(trigger: str, ocr_key: str, old_trigger: str | None = None, runner=subprocess.run) -> str:
    """按 ``ocr.trigger`` 同步桌面快捷键，返回一句可展示的说明。

    - ``listen``：把抓屏热键并进 ``show-screenshot-ui``（``Print`` 保留）；
    - 其他触发方式：若之前是 ``listen``，把 ``show-screenshot-ui`` 还原成 ``Print``；
    - 非 GNOME 桌面直接跳过（KDE 用 Spectacle，不需要改桌面按键）。
    """
    from . import env as env_mod
    from . import keys

    if env_mod.desktop_family() != "gnome":
        if trigger == "spectacle":
            return "KDE：用 Spectacle 区域截图，不改桌面快捷键"
        return f"{env_mod.desktop_family()}：不改桌面快捷键"
    if trigger == "listen":
        try:
            accelerator = keys.to_accelerator(ocr_key)
        except keys.KeyError as exc:
            raise RuntimeError(f"抓屏热键 {ocr_key} 无法转成 GNOME 加速键：{exc}") from exc
        bind_screenshot(accelerator, runner=runner)
        return f"已把 {accelerator} 加入 GNOME 的「截屏界面」（Print 保留）"
    if old_trigger == "listen":
        unbind_screenshot(runner=runner)
        return "已还原 GNOME 的「截屏界面」为 Print"
    return ""
