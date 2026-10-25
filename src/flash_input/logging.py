"""日志初始化：**任何失败都不能让主流程退出**。

两条路：

- systemd 托管（存在 `INVOCATION_ID`）→ 只写 stderr，journald 收走；不建文件、不轮转。
- 手动运行 → `$STATE_DIR/flash-input.log`（1 MB × 3 轮转）；目录不可写（本机 `/` 只读）
  → 退回 stderr，并打一行说明。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import env, xdg

LOGGER_NAME = "flash-input"  # 与 daemon.LOG 一致：附了 handler 的就是这个名字

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 3


def _stderr_handler(level: int) -> logging.Handler:
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    return handler


def _file_handler(path: Path, level: int) -> logging.Handler | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    except OSError:
        return None
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    return handler


def setup(verbose: bool = False, quiet: bool = False) -> logging.Logger:
    """配置根 logger 并返回本模块 logger。可重复调用（先清掉旧 handler）。"""
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(logging.DEBUG)  # 由 handler 过滤，保证文件里也能留 DEBUG
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.propagate = False

    if env.systemd_managed():
        root.addHandler(_stderr_handler(level))
        return root

    file_handler = _file_handler(xdg.log_path(), level)
    if file_handler is not None:
        root.addHandler(file_handler)
        # 手动运行时 stderr 也留一份：用户看得见，不必去翻文件
        stderr = _stderr_handler(level)
        stderr.setLevel(max(level, logging.WARNING))
        root.addHandler(stderr)
    else:
        root.addHandler(_stderr_handler(level))
        root.warning("日志目录不可写（%s），日志只输出到 stderr；主流程继续", xdg.log_path().parent)
    return root


def get(name: str = LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)
