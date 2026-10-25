"""模型登记表：内置清单、自定义登记、安装/删除语义。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from flash_input import asr_catalog, config


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """每个测试用自己的 models.json，别读写真机 ~/.config。"""
    monkeypatch.setenv("FLASH_INPUT_CONFIG", str(tmp_path / "config.json"))


def _cfg(**asr) -> dict:
    cfg = config._deep_merge(config.DEFAULTS, {})
    cfg["asr"].update(asr)
    return cfg


def test_builtin_has_separate_variants_and_engines():
    ids = list(asr_catalog.BUILTIN)
    assert "qwen3-0.6b" in ids and "qwen3-1.7b" in ids
    assert {"sensevoice", "paraformer", "zipformer"} <= set(ids)
    assert asr_catalog.BUILTIN["qwen3-0.6b"]["variant"] == "0.6B"
    assert asr_catalog.BUILTIN["qwen3-1.7b"]["variant"] == "1.7B"


def test_resolve_uses_config_model():
    cfg = _cfg(model="sensevoice")
    entry = asr_catalog.resolve(cfg)
    assert entry["id"] == "sensevoice" and entry["kind"] == "ctc"
    assert entry["engine"] == "sensevoice"


def test_resolve_unknown_raises():
    cfg = _cfg(model="nope")
    with pytest.raises(asr_catalog.CatalogError):
        asr_catalog.resolve(cfg)


def test_catalog_includes_custom_entry():
    cfg = _cfg(
        model="mine",
        models=[{"id": "mine", "name": "我的", "kind": "ctc", "dir": "/tmp/mine", "files": ["a.onnx"]}],
    )
    entries = asr_catalog.catalog(cfg)
    assert entries["mine"]["custom"] is True
    assert entries["mine"]["name"] == "我的"


def test_custom_readiness_requires_declared_files(tmp_path):
    (tmp_path / "a.onnx").write_bytes(b"x")
    cfg = _cfg(
        model="mine",
        models=[
            {
                "id": "mine",
                "kind": "ctc",
                "dir": str(tmp_path),
                "files": ["a.onnx"],
            }
        ],
    )
    entry = asr_catalog.resolve(cfg)
    assert asr_catalog.is_ready(entry) is True
    assert asr_catalog.model_dir(entry) == tmp_path
    (tmp_path / "a.onnx").unlink()
    assert asr_catalog.is_ready(entry) is False
    assert asr_catalog.missing(entry) == ["a.onnx"]


def test_custom_install_rejects_incomplete(tmp_path):
    cfg = _cfg(
        model="mine",
        models=[{"id": "mine", "kind": "ctc", "dir": str(tmp_path), "files": ["a.onnx"]}],
    )
    entry = asr_catalog.resolve(cfg)
    with pytest.raises(asr_catalog.CatalogError):
        asr_catalog.install(entry)


def test_register_custom_dir(tmp_path, monkeypatch):
    for name in ("model.int8.onnx", "tokens.txt"):
        (tmp_path / name).write_bytes(b"x")
    monkeypatch.setattr(asr_catalog, "_check_loadable", lambda directory: None)
    monkeypatch.setattr(asr_catalog, "_metadata", lambda directory: {"lfr_window_size": "7"})
    cfg = _cfg()
    entry = asr_catalog.register(cfg, "我的 模型", str(tmp_path))
    assert entry["id"] == "我的-模型"
    assert entry["kind"] == "ctc" and entry["engine"] == "paraformer"
    assert entry["dir"] == str(tmp_path)
    assert "model.int8.onnx" in entry["files"]


def test_register_rejects_relative_path(tmp_path):
    with pytest.raises(asr_catalog.CatalogError):
        asr_catalog.register(_cfg(), "x", "relative/dir")


def test_register_rejects_dir_without_onnx(tmp_path):
    with pytest.raises(asr_catalog.CatalogError):
        asr_catalog.register(_cfg(), "x", str(tmp_path))


def test_register_avoids_builtin_and_existing_ids(tmp_path, monkeypatch):
    (tmp_path / "m.onnx").write_bytes(b"x")
    monkeypatch.setattr(asr_catalog, "_check_loadable", lambda directory: None)
    monkeypatch.setattr(asr_catalog, "_metadata", lambda directory: {"model_type": "paraformer"})
    cfg = _cfg(models=[{"id": "sensevoice", "kind": "ctc", "dir": "/tmp/x", "files": ["a.onnx"]}])
    entry = asr_catalog.register(cfg, "sensevoice", str(tmp_path))
    assert entry["id"] != "sensevoice"
    assert entry["id"].startswith("custom-sensevoice") or entry["id"].endswith("-2")


def test_remove_custom_only_unregisters(tmp_path):
    entry = {"id": "mine", "custom": True, "kind": "ctc", "dir": str(tmp_path)}
    assert "文件保留" in asr_catalog.remove(entry)
    assert tmp_path.exists()


def test_remove_builtin_deletes_engine_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASH_INPUT_MODEL_DIR", str(tmp_path))
    engine_dir = tmp_path / "sensevoice"
    engine_dir.mkdir()
    (engine_dir / "model.int8.onnx").write_bytes(b"x")
    (engine_dir / "tokens.txt").write_bytes(b"x")
    entry = asr_catalog.catalog(_cfg())["sensevoice"]
    assert asr_catalog.remove(entry)
    assert not engine_dir.exists()


def test_status_marks_current_and_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASH_INPUT_MODEL_DIR", str(tmp_path))
    cfg = _cfg(model="paraformer")
    rows = {row["id"]: row for row in asr_catalog.status(cfg)}
    assert rows["paraformer"]["current"] is True
    assert rows["sensevoice"]["current"] is False
    assert rows["paraformer"]["ready"] is False
    assert len([r for r in rows.values() if r["kind"] == "qwen3"]) == 2  # 两个规格各一条
