"""XDG 路径解析。

所有路径都可以被环境变量覆盖，方便测试与本机 `/` 只读的受限环境：

- `FLASH_INPUT_CONFIG`      配置文件绝对路径
- `FLASH_INPUT_STATE_DIR`   日志与运行状态目录
- `FLASH_INPUT_RUNTIME_DIR` socket 与 last.png 所在目录

任何目录都**不在这里创建**，需要写的模块自己 `ensure()` 并容忍失败。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

APP = "flash-input"


def _home() -> Path:
    return Path.home()


def config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    return Path(raw).expanduser() if raw else _home() / ".config"


def config_path() -> Path:
    raw = os.environ.get("FLASH_INPUT_CONFIG")
    if raw:
        return Path(raw).expanduser()
    return config_home() / APP / "config.json"


def state_home() -> Path:
    raw = os.environ.get("XDG_STATE_HOME")
    return Path(raw).expanduser() if raw else _home() / ".local" / "state"


def state_dir() -> Path:
    raw = os.environ.get("FLASH_INPUT_STATE_DIR")
    if raw:
        return Path(raw).expanduser()
    return state_home() / APP


def data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    return Path(raw).expanduser() if raw else _home() / ".local" / "share"


def data_dir() -> Path:
    return data_home() / APP


def runtime_dir() -> Path:
    """socket 与临时产物目录；没有 XDG_RUNTIME_DIR 时退到临时目录。"""
    raw = os.environ.get("FLASH_INPUT_RUNTIME_DIR")
    if raw:
        return Path(raw).expanduser()
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / APP
    return Path(tempfile.gettempdir()) / f"{APP}-{os.getuid()}"


def socket_path() -> Path:
    return runtime_dir() / "flash-input.sock"


def log_path() -> Path:
    return state_dir() / "flash-input.log"


def ensure(path: Path, mode: int = 0o700) -> Path | None:
    """尽力创建目录；失败返回 None（调用方负责降级，绝不抛）。"""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        return path
    except OSError:
        return None
