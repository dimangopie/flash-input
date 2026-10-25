"""sherpa-onnx 系模型的登记与下载（SenseVoice / Paraformer / Zipformer）。

这三个引擎来自 k2-fsa 的 sherpa-onnx 模型库：模型文件是普通 ONNX，
不需要 sherpa-onnx 运行时，本项目的 `asr_ctc.py` 自己算 fbank、LFR、CMVN
和 CTC 解码，所以这里只负责「放哪、怎么下」。

下载地址按「先镜像、后官方」排列：hf-mirror.com 在国内通常比
huggingface.co 快得多，官方地址作为镜像挂了时的退路。GitHub Releases
上的整包 tar.bz2 动辄 1 GB（含非 int8 图与测试音频），不采用。

模型不进 wheel（每个 200 MB 上下），放在：

    $FLASH_INPUT_MODEL_DIR/<engine>/         （默认 $XDG_CACHE_HOME/flash-input/models）
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

HF_MIRROR = "https://hf-mirror.com"
HF_OFFICIAL = "https://huggingface.co"

# engine -> (仓库, 需要的文件)
ENGINES: dict[str, dict[str, object]] = {
    "sensevoice": {
        "repo": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "files": ("model.int8.onnx", "tokens.txt"),
        "size_mb": 239,
        "note": "SenseVoiceSmall，中英日韩粤，抗噪",
    },
    "paraformer": {
        "repo": "csukuangfj/sherpa-onnx-paraformer-zh-2024-03-09",
        "files": ("model.int8.onnx", "tokens.txt"),
        "size_mb": 227,
        "note": "Paraformer 中文，准确率最高",
    },
    "zipformer": {
        "repo": "csukuangfj/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23",
        "files": (
            "encoder-epoch-99-avg-1.int8.onnx",
            "decoder-epoch-99-avg-1.int8.onnx",
            "joiner-epoch-99-avg-1.int8.onnx",
            "tokens.txt",
        ),
        "size_mb": 26,
        "note": "Zipformer 流式中文，14M 极快",
    },
}

CTC_ENGINES = tuple(ENGINES)


class AsrModelError(RuntimeError):
    """模型缺失或下载失败。"""


def model_root() -> Path:
    explicit = os.environ.get("FLASH_INPUT_MODEL_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "flash-input" / "models"


def engine_dir(engine: str) -> Path:
    if engine not in ENGINES:
        raise AsrModelError(f"未知引擎 {engine!r}，可选：{' / '.join(ENGINES)}")
    return model_root() / engine


def is_ready(engine: str) -> bool:
    directory = engine_dir(engine)
    return all((directory / name).is_file() for name in ENGINES[engine]["files"])  # type: ignore[union-attr]


def missing_files(engine: str) -> list[str]:
    directory = engine_dir(engine)
    return [n for n in ENGINES[engine]["files"] if not (directory / n).is_file()]  # type: ignore[union-attr]


def _urls(engine: str, name: str) -> list[str]:
    repo = ENGINES[engine]["repo"]
    suffix = f"/{repo}/resolve/main/{name}"
    return [HF_MIRROR + suffix, HF_OFFICIAL + suffix]


def _download(
    engine: str,
    name: str,
    dest: Path,
    *,
    quiet: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last: Exception | None = None
    for url in _urls(engine, name):
        try:
            if not quiet:
                print(f"下载 {engine}/{name} …", file=sys.stderr, flush=True)
            request = urllib.request.Request(url, headers={"User-Agent": "flash-input"})
            with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as out:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                while chunk := response.read(1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
                    elif not quiet and total:
                        print(f"\r  {done / 1e6:6.1f} / {total / 1e6:.1f} MB", end="", file=sys.stderr)
            if not quiet and total:
                print(file=sys.stderr)
            size = tmp.stat().st_size
            if size < 1024:
                raise AsrModelError(f"{name} 下载内容异常（{size} 字节）")
            tmp.replace(dest)
            return
        except (urllib.error.URLError, OSError, AsrModelError) as exc:
            last = exc
            tmp.unlink(missing_ok=True)
            if not quiet:
                print(f"  失败：{exc}，换下一个地址", file=sys.stderr)
    raise AsrModelError(f"{engine}/{name} 下载失败：{last}")


def fetch(engine: str, *, quiet: bool = False, on_progress: Callable[[int, int], None] | None = None) -> Path:
    """下载缺失文件；返回模型目录。已齐的直接返回，不重复下载。"""
    directory = engine_dir(engine)
    for name in missing_files(engine):
        _download(engine, name, directory / name, quiet=quiet, on_progress=on_progress)
    if not is_ready(engine):
        raise AsrModelError(f"{directory} 仍不完整")
    return directory


def describe(engine: str) -> str:
    info = ENGINES.get(engine)
    if info is None:
        return f"{engine}（未知引擎）"
    state = "已下载" if is_ready(engine) else "未下载"
    return f"{engine}（{info['note']}，约 {info['size_mb']} MB，{state}）"
