"""OCR 面的测试：行合并算法、模型文件存在、真实引擎识别一张合成图。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from flash_input import ocr


def _box(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def test_assemble_lines_groups_by_vertical_overlap():
    boxes = [
        _box(100, 100, 300, 140),  # 第一行右半
        _box(10, 102, 90, 138),  # 第一行左半（略低 2px）
        _box(10, 200, 300, 240),  # 第二行
    ]
    texts = ["world", "hello", "second"]
    assert ocr.assemble_lines(boxes, texts) == ["hello world", "second"]


def test_assemble_lines_keeps_two_close_blocks_apart():
    boxes = [_box(10, 10, 100, 40), _box(10, 60, 100, 90)]
    assert ocr.assemble_lines(boxes, ["a", "b"]) == ["a", "b"]


def test_join_fragments_adds_space_only_between_non_cjk():
    assert ocr._join_fragments(["你好", "世界"]) == "你好世界"
    assert ocr._join_fragments(["hello", "world"]) == "hello world"
    assert ocr._join_fragments(["中文", "abc"]) == "中文 abc"


def test_assemble_lines_ignores_empty_text():
    assert ocr.assemble_lines([_box(0, 0, 10, 10)], ["  "]) == []


def test_models_are_vendored():
    assert ocr.models_ready(), f"缺少模型：{ocr.MODELS_DIR}"
    for name in ocr.MODEL_FILES.values():
        assert (ocr.MODELS_DIR / name).stat().st_size > 100_000


@pytest.fixture(scope="module")
def sample_image(tmp_path_factory):
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont

    path = tmp_path_factory.mktemp("img") / "sample.png"
    image = Image.new("RGB", (900, 320), "white")
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, 64)
            break
    if font is None:
        font = ImageFont.load_default()
    draw.text((40, 60), "HELLO 123", fill="black", font=font)
    draw.text((40, 180), "SECOND LINE", fill="black", font=font)
    image.save(path)
    return path


def test_engine_reads_synthetic_image(sample_image):
    engine = ocr.OcrEngine(threads=2, resident="none")
    try:
        text = engine.text(sample_image)
    finally:
        engine.unload()
    assert "HELLO" in text.upper()
    assert "123" in text
    assert len(text.splitlines()) >= 2


class _FakeResult:
    def __init__(self, boxes, txts):
        self.boxes = boxes
        self.txts = txts


def test_lines_from_result_reading_vs_box():
    boxes = [_box(100, 100, 300, 140), _box(10, 102, 90, 138)]
    result = _FakeResult(boxes, ["world", "hello"])
    assert ocr.lines_from_result(result, "reading") == ["hello world"]
    assert ocr.lines_from_result(result, "box") == ["world", "hello"]


def test_lines_from_result_skips_blank_lines():
    result = _FakeResult([_box(0, 0, 10, 10)], ["", "  "])
    assert ocr.lines_from_result(result) == []
