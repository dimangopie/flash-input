"""日志初始化：daemon 用的 logger 名字必须真的挂上 handler。

踩过的坑：`logging.setup()` 配置的是 "finput"，而 `daemon.LOG` 是 "flash-input"，
结果 daemon 的 INFO 日志全被 stdlib 的 lastResort（只放 WARNING 以上）吞掉，
journalctl 里只剩警告，看起来像「什么都没发生」。
"""

from __future__ import annotations

import logging

from flash_input import daemon
from flash_input import logging as log_mod


def test_setup_attaches_handlers_to_the_logger_daemon_uses(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "test")  # 走 systemd 分支，不碰文件
    log_mod.setup()
    logger = log_mod.get(daemon.LOG)
    assert logger.name == log_mod.LOGGER_NAME
    assert logger.handlers, f"{daemon.LOG} 没有 handler，INFO 会被 lastResort 吞掉"
    assert logger.propagate is False
    assert logger.isEnabledFor(logging.INFO)
