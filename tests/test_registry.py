"""模型登记文件 models.json：内置播种、说明/路径覆盖、自定义增删。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from flash_input import asr_catalog, config


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASH_INPUT_CONFIG", str(tmp_path / "config.json"))
    return tmp_path


def _cfg(**asr):
    return config._deep_merge(config.DEFAULTS, {"asr": asr})


def test_registry_seeds_builtin_entries_on_first_read(home, monkeypatch):
    monkeypatch.setattr(asr_catalog, "is_ready", lambda entry: False)
    rows = asr_catalog.registry_items(_cfg())
    assert [row["id"] for row in rows] == list(asr_catalog.BUILTIN)
    saved = json.loads((home / "models.json").read_text(encoding="utf-8"))
    assert [m["id"] for m in saved["models"]] == list(asr_catalog.BUILTIN)
    # 未下载的内置模型不写死路径，等装好再补
    assert all("dir" not in row for row in saved["models"])


def test_registry_override_changes_note_and_dir(home, monkeypatch):
    monkeypatch.setattr(asr_catalog, "is_ready", lambda entry: False)
    (home / "models.json").write_text(
        json.dumps(
            {
                "version": 1,
                "models": [
                    {"id": "sensevoice", "note": "我改的说明", "dir": "/mnt/models/sv"},
                ],
            }
        ),
        encoding="utf-8",
    )
    entries = asr_catalog.catalog(_cfg())
    assert entries["sensevoice"]["note"] == "我改的说明"
    assert entries["sensevoice"]["dir"] == "/mnt/models/sv"
    assert entries["sensevoice"]["overridden"] is True
    rows = {row["id"]: row for row in asr_catalog.status(_cfg())}
    assert rows["sensevoice"]["source"] == "内置·已定制"
    assert rows["paraformer"]["source"] == "内置"


def test_registry_custom_entry_without_kind_defaults_to_ctc(home):
    (home / "models.json").write_text(
        json.dumps(
            {
                "version": 1,
                "models": [
                    {"id": "mine", "name": "我的模型", "dir": "/tmp/mine", "files": ["a.onnx"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    entry = asr_catalog.catalog(_cfg())["mine"]
    assert entry["kind"] == "ctc" and entry["custom"] is True
    assert str(asr_catalog.model_dir(entry)) == "/tmp/mine"


def test_add_and_remove_entry_roundtrip(home):
    entry = {
        "id": "mine",
        "name": "我的",
        "kind": "ctc",
        "engine": "paraformer",
        "dir": "/tmp/mine",
        "files": ["a.onnx"],
        "note": "测试",
    }
    asr_catalog.add_entry(entry, _cfg())
    assert "mine" in asr_catalog.catalog(_cfg())
    assert asr_catalog.remove_entry("mine", _cfg()) is True
    assert "mine" not in asr_catalog.catalog(_cfg())
    assert asr_catalog.remove_entry("mine", _cfg()) is False


def test_broken_registry_falls_back_to_builtin(home):
    (home / "models.json").write_text("{ 不是 json", encoding="utf-8")
    assert set(asr_catalog.catalog(_cfg())) == set(asr_catalog.BUILTIN)
    with pytest.raises(asr_catalog.CatalogError):
        asr_catalog.registry_items(_cfg())


def test_legacy_config_models_migrate_once(home, monkeypatch):
    monkeypatch.setattr(asr_catalog, "is_ready", lambda entry: False)
    cfg = _cfg(models=[{"id": "old", "name": "旧", "kind": "ctc", "dir": "/tmp/old", "files": ["a.onnx"]}])
    assert "old" in asr_catalog.catalog(cfg)
    saved = json.loads((home / "models.json").read_text(encoding="utf-8"))
    assert any(m["id"] == "old" for m in saved["models"])
    # 配置文件里的条目始终有效：要两处都清掉才真正消失（webui 删除时就是这么做的）
    cfg["asr"]["models"] = []
    assert "old" in asr_catalog.catalog(cfg)
    asr_catalog.remove_entry("old", cfg)
    assert "old" not in asr_catalog.catalog(cfg)
