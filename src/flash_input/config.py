"""配置：JSON 持久化 + 默认值 + 校验，供命令行与 Web 设置页共用。

- 文件位置：`$FLASH_INPUT_CONFIG`，否则 `$XDG_CONFIG_HOME/flash-input/config.json`
- 读：文件缺失 / 损坏 / 字段非法 → 打警告并用默认值（**不覆盖用户文件**）
- 写：整份校验通过后才原子替换（临时文件 + `Path.replace`）
- 用 JSON 而不是 TOML：`tomllib` 只能读不能写，JSON 零依赖且 Web 页天然对接

字段语义见 OUTLINE.md §11；`resident` 是模型常驻策略：
`none`（用完立即卸载） / `10min` / `30min` / `forever`（永不卸载）。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from . import keys, xdg
from .inject import PASTE_COMBOS

CONFIG_VERSION = 1

# 谁负责打开系统截屏窗口：GNOME 注入 Print / GNOME 快捷键触发 / KDE 调 Spectacle
CAPTURE_TRIGGERS = ("inject", "listen", "spectacle")
# 语音模型列表见 asr_catalog.BUILTIN；这里只校验「选中的 id 在登记表里」
ASR_LANGUAGES = ("auto", "zh", "en", "ja", "ko", "yue")
RESIDENT_CHOICES = ("none", "10min", "30min", "forever")
RESIDENT_SECONDS: dict[str, int | None] = {"none": 0, "10min": 600, "30min": 1800, "forever": None}
# 设置页没有鉴权，只允许绑在本机回环：0.0.0.0 / 局域网地址一律拒绝
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

DEFAULTS: dict[str, Any] = {
    "hotkey": {
        "mode": "evdev",  # evdev / off
        "voice_key": "KEY_RIGHTCTRL",
        "ocr_key": "KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O",
        "guard_ms": 300,
        "min_hold_ms": 300,
        "max_hold_s": 60,
        "devices": "auto",
    },
    "voice": {"tail_silence_s": 1.2},
    "asr": {
        "model": "qwen3-0.6b",  # asr_catalog 里的 id（0.6B/1.7B 算两个模型）
        "models": [],  # 用户添加的本地 ONNX 模型（{id, name, kind, dir, files, ...}）
        "language": "auto",  # 仅 sensevoice：auto / zh / en / ja / ko / yue
        "threads": 4,
        "resident": "forever",
        "preload_on_record": True,
    },
    "ocr": {
        "layout": "reading",  # reading / box
        "threads": 2,
        "resident": "30min",
        "timeout_s": 60,
        "trigger": "inject",  # inject=注入 Print；listen=GNOME 快捷键触发；spectacle=KDE 调 Spectacle
    },
    "inject": {
        "mode": "auto",  # auto / uinput / clipboard
        "paste_key": "shift+insert",
        "paste_delay_ms": 60,
        "voice_paste": True,  # 语音识别完是否自动粘贴（False = 只进剪贴板）
        "ocr_paste": False,  # 抓屏识别完是否自动粘贴（默认只进剪贴板）
    },
    "web": {"enabled": True, "host": "127.0.0.1", "port": 7331},
    "version": CONFIG_VERSION,
}


class ConfigError(RuntimeError):
    """配置非法（命令行参数错误 / Web 提交错误 / 文件内容错误都用它）。"""


# --------------------------------------------------------------------------- 读取


def path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return xdg.config_path()


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if key == "version":
            continue
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load(explicit: str | None = None) -> dict[str, Any]:
    """读配置；任何问题都退化为默认值并打一行警告（绝不抛）。"""
    target = path(explicit)
    raw: dict[str, Any] = {}
    if target.exists():
        try:
            parsed = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                raw = parsed
            else:
                _warn(f"配置文件 {target} 顶层不是对象，已忽略")
        except (OSError, json.JSONDecodeError) as exc:
            _warn(f"配置文件 {target} 无法解析，使用默认配置（{exc}）")
    merged = _deep_merge(DEFAULTS, raw)
    merged["version"] = CONFIG_VERSION
    # v30 删掉了 ocr.capture_mode（抓屏只剩系统截屏 UI）。老配置里的残留键会被 _deep_merge
    # 带进来，页面上看得见但没有任何作用 —— 这里顺手摘掉。
    stale = merged.get("ocr")
    if isinstance(stale, dict):
        stale.pop("capture_mode", None)
    # v33：asr.engine + asr.variant 合并成 asr.model（一个规格一个 id）
    asr = merged.get("asr")
    if isinstance(asr, dict):
        old_engine = asr.pop("engine", None)
        old_variant = asr.pop("variant", None)
        asr.pop("model_dir", None)
        if old_engine and "model" not in raw.get("asr", {}):
            if old_engine == "qwen3":
                asr["model"] = f"qwen3-{str(old_variant or '0.6B').lower()}"
            else:
                asr["model"] = str(old_engine)
            try:
                save(merged, explicit)
            except OSError as exc:
                _warn(f"迁移 asr.model 失败（不影响运行）：{exc}")
    # v31 删掉了整个 [notify] 段（不再弹桌面通知）
    stale_notify = "notify" in merged
    merged.pop("notify", None)
    if stale_notify:
        # 老文件里还留着这个键：原地清一次，免得用户打开 config.json 以为还能配
        try:
            save(merged, explicit)
        except OSError as exc:
            _warn(f"清理旧配置键失败（不影响运行）：{exc}")
    try:
        validate(merged)
    except ConfigError as exc:
        _warn(f"配置校验失败，使用默认配置（{exc}）")
        merged = copy.deepcopy(DEFAULTS)
    return merged


def save(cfg: dict[str, Any], explicit: str | None = None) -> Path:
    """校验并原子写回。校验失败抛 ConfigError，不碰原文件。"""
    merged = _deep_merge(DEFAULTS, cfg)
    merged["version"] = CONFIG_VERSION
    validate(merged)
    target = path(explicit)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


# --------------------------------------------------------------------------- 校验


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate(cfg: dict[str, Any]) -> None:
    hotkey = cfg.get("hotkey") or {}
    _expect(isinstance(hotkey, dict), "hotkey 必须是对象")
    _expect(hotkey.get("mode") in ("evdev", "off"), "hotkey.mode 只能是 evdev / off")
    for key in ("voice_key", "ocr_key"):
        _expect(isinstance(hotkey.get(key), str) and hotkey[key].strip(), f"hotkey.{key} 不能为空")
    try:
        keys.validate_hold_key(hotkey["voice_key"])
        keys.validate_combo(hotkey["ocr_key"])
    except keys.KeyError as exc:
        raise ConfigError(str(exc)) from exc
    _expect(
        _is_int(hotkey.get("guard_ms")) and 0 <= hotkey["guard_ms"] <= 5000, "hotkey.guard_ms 应在 0~5000"
    )
    _expect(
        _is_int(hotkey.get("min_hold_ms")) and 0 <= hotkey["min_hold_ms"] <= 5000,
        "hotkey.min_hold_ms 应在 0~5000",
    )
    _expect(
        _is_int(hotkey.get("max_hold_s")) and 1 <= hotkey["max_hold_s"] <= 600, "hotkey.max_hold_s 应在 1~600"
    )
    _expect(isinstance(hotkey.get("devices"), str), "hotkey.devices 应为字符串（auto 或 /dev/input/eventN）")

    voice = cfg.get("voice") or {}
    _expect(
        isinstance(voice.get("tail_silence_s"), (int, float)) and voice["tail_silence_s"] >= 0,
        "voice.tail_silence_s 应 >= 0",
    )

    asr = cfg.get("asr") or {}
    model_id = asr.get("model")
    _expect(isinstance(model_id, str) and bool(model_id), "asr.model 应为非空字符串")
    _expect(isinstance(asr.get("models"), list), "asr.models 应为列表")
    for item in asr.get("models") or []:
        _expect(isinstance(item, dict), "asr.models 的每一项应为对象")
        _expect(bool(item.get("id")), "asr.models 里的模型缺少 id")
        _expect(
            item.get("kind") in ("qwen3", "ctc", "zipformer"),
            "自定义模型的 kind 只能是 qwen3 / ctc / zipformer",
        )
        _expect(
            isinstance(item.get("dir"), str) and item["dir"].startswith("/"),
            f"自定义模型 {item.get('id')} 的 dir 应为绝对路径",
        )
    known = {"qwen3-0.6b", "qwen3-1.7b", "sensevoice", "paraformer", "zipformer"}
    try:  # 登记文件（models.json）里的自定义模型也算已登记
        from . import asr_catalog

        known |= set(asr_catalog.catalog(cfg, create=False))
    except Exception:  # 文件损坏/读不到时只校验内置名单，真正的解析错误由 models list 报
        pass
    known |= {str(item.get("id")) for item in asr.get("models") or []}
    _expect(model_id in known, f"asr.model 只能是已登记的模型：{' / '.join(sorted(known))}")
    _expect(asr.get("language") in ASR_LANGUAGES, "asr.language 只能是 " + " / ".join(ASR_LANGUAGES))
    _expect(_is_int(asr.get("threads")) and 1 <= asr["threads"] <= 256, "asr.threads 应在 1~256")
    _expect(asr.get("resident") in RESIDENT_CHOICES, f"asr.resident 只能是 {' / '.join(RESIDENT_CHOICES)}")
    _expect(isinstance(asr.get("preload_on_record"), bool), "asr.preload_on_record 应为布尔")

    ocr = cfg.get("ocr") or {}
    _expect(ocr.get("layout") in ("reading", "box"), "ocr.layout 只能是 reading / box")
    _expect(_is_int(ocr.get("threads")) and 1 <= ocr["threads"] <= 256, "ocr.threads 应在 1~256")
    _expect(ocr.get("resident") in RESIDENT_CHOICES, f"ocr.resident 只能是 {' / '.join(RESIDENT_CHOICES)}")
    _expect(_is_int(ocr.get("timeout_s")) and 1 <= ocr["timeout_s"] <= 3600, "ocr.timeout_s 应在 1~3600")

    inject = cfg.get("inject") or {}
    _expect(
        inject.get("mode") in ("auto", "uinput", "clipboard"), "inject.mode 只能是 auto / uinput / clipboard"
    )
    _expect(inject.get("paste_key") in PASTE_COMBOS, "inject.paste_key 只能是 " + " / ".join(PASTE_COMBOS))
    _expect(ocr.get("trigger") in CAPTURE_TRIGGERS, "ocr.trigger 只能是 " + " / ".join(CAPTURE_TRIGGERS))
    for flag in ("voice_paste", "ocr_paste"):
        _expect(isinstance(inject.get(flag), bool), f"inject.{flag} 应为布尔")
    _expect(
        _is_int(inject.get("paste_delay_ms")) and 0 <= inject["paste_delay_ms"] <= 5000,
        "inject.paste_delay_ms 应在 0~5000",
    )

    web = cfg.get("web") or {}
    _expect(isinstance(web.get("enabled"), bool), "web.enabled 应为布尔")
    # 设置页没有任何鉴权，还能改配置、注册任意路径的 ONNX 模型：只许绑回环。
    _expect(
        web.get("host") in LOOPBACK_HOSTS,
        "web.host 只能是回环地址（" + " / ".join(LOOPBACK_HOSTS) + "），设置页没有鉴权",
    )
    _expect(_is_int(web.get("port")) and 0 <= web["port"] <= 65535, "web.port 应在 0~65535")


# --------------------------------------------------------------------------- 点号路径读写


def get_path(cfg: dict[str, Any], dotted: str) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"没有这个配置项：{dotted}")
        node = node[part]
    return node


def coerce(default: Any, raw: str) -> Any:
    """把命令行/表单里的字符串按默认值的类型解释。"""
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"需要布尔值（true/false），收到 {raw!r}")
    if isinstance(default, int):
        try:
            return int(raw, 10)
        except ValueError as exc:
            raise ConfigError(f"需要整数，收到 {raw!r}") from exc
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"需要数字，收到 {raw!r}") from exc
    return raw


def set_path(cfg: dict[str, Any], dotted: str, raw: Any) -> dict[str, Any]:
    """返回改过的新配置（不改原对象），类型按默认值推断。"""
    parts = dotted.split(".")
    default = get_path(DEFAULTS, dotted)  # 不存在的键在这里就报错
    value = raw if not isinstance(raw, str) else coerce(default, raw)
    new = copy.deepcopy(cfg)
    node = new
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    validate(new)
    return new


def _warn(msg: str) -> None:
    print(f"警告：{msg}", file=sys.stderr, flush=True)


def config_file_exists(explicit: str | None = None) -> bool:
    return path(explicit).exists()


__all__ = [
    "CONFIG_VERSION",
    "DEFAULTS",
    "RESIDENT_CHOICES",
    "RESIDENT_SECONDS",
    "ConfigError",
    "coerce",
    "config_file_exists",
    "get_path",
    "load",
    "path",
    "save",
    "set_path",
    "validate",
]
