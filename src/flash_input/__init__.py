"""Flash Input —— 语音 / 抓屏 → 文字 → 剪贴板。

版本号只有一个来源：`pyproject.toml`，这里通过 importlib.metadata 读取。
以源码目录直接运行时（没有装成包）metadata 取不到，退回 "0.0.0+dev"。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version


def _read_version() -> str:
    try:
        return _version("flash-input")
    except PackageNotFoundError:  # 源码树里 `python -m flash_input` 的情况
        return "0.0.0+dev"


__version__ = _read_version()
__all__ = ["__version__"]
