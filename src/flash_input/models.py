"""Qwen3-ASR 模型的定位与下载（ModelScope 缓存）。

模型不进 wheel：三个 ONNX 合计约 983 MB（0.6B）/ ~2.6 GB（1.7B）。仓库里只放
「怎么找、怎么下」的代码，文件交给 ModelScope 的缓存目录管理。

查找顺序与 modelscope>=1.0 的缓存布局一致：

    $XDG_CACHE_HOME/modelscope/models/<repo_id 的 / 换成 -->/snapshots/<rev>/
    ~/.cache/modelscope/models/<...>/snapshots/<rev>/
    ~/.cache/modelscope/hub/<repo_id>/                 （旧版布局）

``model_0.6B/`` 与 ``tokenizer/`` 都必须齐全才算命中；缺一个就当没缓存，
避免加载到一半才报错。
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ID = "zengshuishui/Qwen3-ASR-onnx"
VARIANTS = ("0.6B", "1.7B")
DEFAULT_VARIANT = "0.6B"

MODEL_FILES = ("conv_frontend.onnx", "encoder.int8.onnx", "decoder.int8.onnx")
TOKENIZER_FILES = ("vocab.json", "merges.txt", "tokenizer_config.json")


class ModelError(RuntimeError):
    """模型缺失、下载失败或目录不完整。"""


def _cache_bases() -> list[Path]:
    bases: list[Path] = []
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        bases.append(Path(xdg) / "modelscope")
    bases.append(Path.home() / ".cache" / "modelscope")
    return bases


def _has_root(root: Path, variant: str) -> bool:
    model_dir = root / f"model_{variant}"
    tokenizer_dir = root / "tokenizer"
    return all((model_dir / name).is_file() for name in MODEL_FILES) and all(
        (tokenizer_dir / name).is_file() for name in TOKENIZER_FILES
    )


def find_root(variant: str = DEFAULT_VARIANT) -> Path | None:
    """返回包含该 variant 与 tokenizer 的缓存快照目录；没有则 None。"""
    repo_dir = REPO_ID.replace("/", "--")
    for base in _cache_bases():
        snapshots = base / "models" / repo_dir / "snapshots"
        if snapshots.is_dir():
            preferred = snapshots / "master"
            if preferred.is_dir() and _has_root(preferred, variant):
                return preferred
            for rev in sorted(snapshots.iterdir()):
                if rev.is_dir() and _has_root(rev, variant):
                    return rev
        legacy = base / "hub" / REPO_ID
        if legacy.is_dir() and _has_root(legacy, variant):
            return legacy
    return None


def model_dir(root: Path, variant: str) -> Path:
    return root / f"model_{variant}"


def tokenizer_dir(root: Path) -> Path:
    return root / "tokenizer"


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def describe(variant: str = DEFAULT_VARIANT) -> dict[str, object]:
    """给 `models list` / `doctor` 用的一行信息。"""
    root = find_root(variant)
    if root is None:
        return {"variant": variant, "present": False, "path": None, "size": 0}
    return {
        "variant": variant,
        "present": True,
        "path": model_dir(root, variant),
        "size": dir_size(root),
    }


def fetch(variant: str = DEFAULT_VARIANT, *, quiet: bool = False) -> Path:
    """缺什么补什么；返回快照目录。已完整命中时直接返回，不碰网络。"""
    if variant not in VARIANTS:
        raise ModelError(f"未知的模型规格 {variant!r}，可选：{' / '.join(VARIANTS)}")
    root = find_root(variant)
    if root is not None:
        return root

    try:
        from modelscope import snapshot_download
    except Exception as exc:
        raise ModelError(
            "缺少 modelscope 依赖，无法自动下载；请重装：uv tool install --force "
            "flash-input（或 `uv sync` 后用 `uv run finput models fetch`）"
        ) from exc

    allow_patterns = [
        f"model_{variant}/conv_frontend.onnx",
        f"model_{variant}/encoder.int8.onnx",
        f"model_{variant}/decoder.int8.onnx",
        "tokenizer/*",
    ]
    try:
        path = snapshot_download(
            REPO_ID,
            allow_patterns=allow_patterns,
            cache_dir=str(_cache_bases()[-1]),
        )
    except Exception as exc:
        raise ModelError(f"下载 {REPO_ID} 失败：{exc}") from exc

    root = Path(path)
    if not _has_root(root, variant):
        raise ModelError(f"下载完成但目录不完整：{root}")
    return root
