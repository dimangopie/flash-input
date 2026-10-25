"""OCR：RapidOCR（PP-OCRv6 small，ONNX），模型随 wheel 分发，不联网。

三个模型放在 `src/flash_input/models/`（约 31 MB），与 rapidocr 版本解耦：
升依赖不会悄悄换模型；换模型是一次显式的文件替换。

线程数必须显式设（OUTLINE §18）：ORT 默认 `intra_op_num_threads = 核数`，
在 24 核机器上比 4 线程慢一倍、CPU 时间高数倍。默认 2，够快且不抢整机。
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from .config import RESIDENT_SECONDS

MODELS_DIR = Path(__file__).resolve().parent / "models"
MODEL_FILES = {
    "Det": "PP-OCRv6_det_small.onnx",
    "Rec": "PP-OCRv6_rec_small.onnx",
    "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
}

_CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

# onnxruntime 首次构造会尝试写 ~/.onnxruntime/telemetry，受限环境里会刷一行警告
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")


class OcrError(RuntimeError):
    pass


def models_ready() -> bool:
    return all((MODELS_DIR / name).is_file() for name in MODEL_FILES.values())


def _model_params() -> dict[str, str]:
    if not models_ready():
        return {}
    return {f"{task}.model_path": str(MODELS_DIR / name) for task, name in MODEL_FILES.items()}


def _quiet_rapidocr() -> None:
    """rapidocr 构造时会往 stderr 刷 INFO，压掉（必须在 import / 构造之后各调一次）。"""
    for name in ("", "rapidocr", "RapidOCR", "onnxruntime"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL)
        for handler in list(logger.handlers):
            handler.setLevel(logging.CRITICAL)


def _join_fragments(parts: list[str]) -> str:
    """同一视觉行内的碎片拼接：中文紧邻中文不加空格，其余加空格。"""
    if not parts:
        return ""
    out = parts[0]
    for part in parts[1:]:
        if not out or not part or (_CJK_RE.search(out[-1]) and _CJK_RE.search(part[0])):
            out += part
        else:
            out += " " + part
    return out


def assemble_lines(boxes, texts: list[str], y_tol: float = 0.6) -> list[str]:
    """按垂直重叠把检测框聚成视觉行，行内按 x 排序；行间自上而下。"""
    if not boxes or not texts:
        return list(texts)
    items = []
    for box, text in zip(boxes, texts, strict=False):
        text = str(text).strip()
        if not text:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        items.append(
            {
                "x1": min(xs),
                "y2": max(ys),
                "y1": min(ys),
                "cy": (min(ys) + max(ys)) / 2.0,
                "text": text,
            }
        )
    if not items:
        return []
    items.sort(key=lambda item: item["cy"])

    lines: list[list[dict]] = []
    current: list[dict] = []
    for item in items:
        if not current:
            current = [item]
            continue
        top = min(entry["y1"] for entry in current)
        bottom = max(entry["y2"] for entry in current)
        overlap = min(bottom, item["y2"]) - max(top, item["y1"])
        ref_height = min(bottom - top, item["y2"] - item["y1"]) or 1.0
        if overlap > y_tol * ref_height:
            current.append(item)
        else:
            lines.append(current)
            current = [item]
    if current:
        lines.append(current)

    out: list[str] = []
    for line in lines:
        line.sort(key=lambda entry: entry["x1"])
        out.append(_join_fragments([entry["text"] for entry in line]))
    return out


def _as_list(value) -> list:
    """numpy 数组不能直接判真假（`array or []` 会抛），统一转 list。"""
    if value is None:
        return []
    if hasattr(value, "tolist"):
        return list(value.tolist())
    return list(value)


def lines_from_result(result, layout: str = "reading") -> list[str]:
    texts = [str(text).strip() for text in _as_list(getattr(result, "txts", None)) if str(text).strip()]
    boxes = _as_list(getattr(result, "boxes", None))
    if layout == "reading" and boxes and len(boxes) == len(texts):
        return assemble_lines(boxes, texts)
    if texts:
        return texts
    # 旧版 rapidocr_onnxruntime 的 (result, elapse) 形态
    if isinstance(result, tuple) and result and isinstance(result[0], (list, tuple)):
        out = []
        for item in result[0]:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text = str(item[1]).strip()
                if text:
                    out.append(text)
        return out
    return []


class OcrEngine:
    """懒加载 + 可选常驻；守护进程复用同一个实例。"""

    def __init__(self, threads: int = 2, resident: str = "30min") -> None:
        self.threads = max(1, int(threads))
        self.resident = resident
        self.idle_seconds = RESIDENT_SECONDS.get(resident, RESIDENT_SECONDS["30min"])
        self._engine = None
        self._load_ms: float | None = None
        self.last_used = 0.0

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    @property
    def load_ms(self) -> float | None:
        return self._load_ms

    def _load(self) -> None:
        if self._engine is not None:
            return
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:  # pragma: no cover
            raise OcrError("未安装 rapidocr：在项目目录跑 `uv sync`，或 `uv tool install finput`") from exc
        _quiet_rapidocr()
        params = _model_params()
        # 注意类型：rapidocr 会把这些值原样塞进配置，线程数必须是 int（给 str 会在比较时炸）
        params["EngineConfig.onnxruntime.intra_op_num_threads"] = int(self.threads)
        params["EngineConfig.onnxruntime.inter_op_num_threads"] = 1
        params["Global.log_level"] = "error"
        started = time.monotonic()
        try:
            self._engine = RapidOCR(params=params or None)
        except Exception as exc:  # pragma: no cover
            raise OcrError(f"OCR 引擎初始化失败：{exc}") from exc
        finally:
            _quiet_rapidocr()
        self._load_ms = (time.monotonic() - started) * 1000

    def recognize(self, image, layout: str = "reading") -> list[str]:
        self._load()
        assert self._engine is not None
        try:
            result = self._engine(str(image) if isinstance(image, Path) else image)
        except Exception as exc:
            raise OcrError(f"识别失败：{exc}") from exc
        finally:
            self.last_used = time.monotonic()
        return lines_from_result(result, layout)

    def text(self, image, layout: str = "reading") -> str:
        return "\n".join(line for line in self.recognize(image, layout) if line)

    def unload(self) -> None:
        if self._engine is not None:
            self._engine = None
            self._load_ms = None
            import gc

            gc.collect()

    def maybe_unload(self, now: float | None = None) -> bool:
        """空闲超过 resident 策略就卸载；返回是否卸载了。"""
        if not self.loaded or self.idle_seconds is None:
            return False
        current = time.monotonic() if now is None else now
        if current - self.last_used >= self.idle_seconds:
            self.unload()
            return True
        return False


__all__ = [
    "MODELS_DIR",
    "OcrEngine",
    "OcrError",
    "assemble_lines",
    "lines_from_result",
    "models_ready",
]
