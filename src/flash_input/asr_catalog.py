"""语音模型的统一登记表：内置模型 + 用户自己添加的 ONNX 模型。

一个「模型」在配置里就是一个 id（如 ``qwen3-0.6b``、``sensevoice``）。
同一个模型的不同规格（Qwen3 的 0.6B / 1.7B）**算两个不同的条目**，因为体积、
速度和准确率都不一样，放在同一个列表里用户才好挑。

来源三类：

- ``qwen3``：ModelScope 仓库 ``zengshuishui/Qwen3-ASR-onnx``，由 `models.py` 管缓存。
- ``ctc`` / ``zipformer``：k2-fsa 的 sherpa-onnx 模型库，由 `asr_models.py` 管下载。
- 自定义：用户填一个**本地目录**，里面必须是完整可用的 ONNX 模型；程序只登记，
  不搬文件，删除时只取消登记。

本项目只认 ONNX：不调用 sherpa-onnx 运行时，也不支持 PyTorch / TensorFlow 等格式。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import threading
from pathlib import Path

from . import asr_models, xdg
from . import models as qwen3_models

#: 引擎大类：决定用哪套解码器。自定义模型必须归到这三类之一。
KINDS = ("qwen3", "ctc", "zipformer")

#: id -> 内置模型定义。ctc/zipformer 的下载细节在 `asr_models.ENGINES` 里。
BUILTIN: dict[str, dict] = {
    "qwen3-0.6b": {
        "name": "Qwen3-ASR 0.6B",
        "kind": "qwen3",
        "variant": "0.6B",
        "size_mb": 941,
        "note": "中英均衡，自回归较慢",
    },
    "qwen3-1.7b": {
        "name": "Qwen3-ASR 1.7B",
        "kind": "qwen3",
        "variant": "1.7B",
        "size_mb": 2600,
        "note": "更大更准，内存占用高",
    },
    "sensevoice": {
        "name": "SenseVoiceSmall",
        "kind": "ctc",
        "engine": "sensevoice",
        "size_mb": 239,
        "note": "中英日韩粤，抗噪，带标点",
    },
    "paraformer": {
        "name": "Paraformer-zh",
        "kind": "ctc",
        "engine": "paraformer",
        "size_mb": 227,
        "note": "中文最准，无标点",
    },
    "zipformer": {
        "name": "Zipformer zh 14M",
        "kind": "zipformer",
        "engine": "zipformer",
        "size_mb": 26,
        "note": "流式中文，延迟最低，准确率略低",
    },
}

DEFAULT_MODEL = "qwen3-0.6b"

#: 模型路径与说明的登记文件（和 config.json 放在同一目录）。
REGISTRY_FILE = "models.json"

#: 登记文件里允许写的字段。路径（dir）与说明（note）都可以在这里改，
#: 内置模型的默认值只在文件里缺这一项时才用。
_ENTRY_FIELDS = ("id", "name", "kind", "engine", "variant", "dir", "files", "size_mb", "note")

#: 安装进度（进程内，给设置页轮询）
INSTALLS: dict[str, dict] = {}
_LOCK = threading.Lock()


class CatalogError(RuntimeError):
    """模型缺失、下载失败、格式不对或登记信息有误。"""


# --------------------------------------------------------------------------- 登记文件


def registry_path() -> Path:
    """`models.json` 的位置：和配置文件同目录，可用 `FLASH_INPUT_CONFIG` 改变父目录。"""
    return xdg.config_path().parent / REGISTRY_FILE


def load_registry() -> list[dict]:
    """读登记文件。文件不存在算空表；内容坏了明确报错，不静默丢配置。"""
    path = registry_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CatalogError(f"模型登记文件读不了：{path}（{exc}）") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise CatalogError(f"模型登记文件不是合法 JSON：{path}（{exc}）") from exc
    items = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise CatalogError(f"模型登记文件格式不对（期望 {{'models': [...]}}）：{path}")
    return [item for item in items if isinstance(item, dict) and item.get("id")]


def save_registry(items: list[dict]) -> Path:
    """原子写登记文件；只保留已知字段，避免把内部状态写进去。"""
    path = registry_path()
    cleaned = [{k: item[k] for k in _ENTRY_FIELDS if k in item} for item in items]
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"version": 1, "models": cleaned}, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def _seed(items: list[dict], cfg: dict | None, *, migrate_legacy: bool) -> tuple[list[dict], bool]:
    """补全登记文件：内置模型写一条（含默认说明与当前路径），再并进旧配置里的自定义模型。"""
    known = {str(item.get("id")) for item in items}
    changed = False
    for model_id in BUILTIN:
        if model_id in known:
            continue
        base = {"id": model_id, **BUILTIN[model_id], "note": BUILTIN[model_id]["note"]}
        entry = _entry_from(base, fallback_kind=BUILTIN[model_id]["kind"])
        if is_ready(entry):
            entry["dir"] = str(model_dir(entry))
        items.append({k: entry[k] for k in _ENTRY_FIELDS if k in entry})
        known.add(model_id)
        changed = True
    # 配置文件里的 asr.models 始终有效（旧版本写在这），与登记文件按 id 取并集。
    # 删除自定义模型时两边都要清，否则它会被这里重新补回来。
    for item in (cfg or {}).get("asr", {}).get("models", []) or []:
        model_id = str(item.get("id") or "")
        if model_id and model_id not in known:
            items.append({k: item.get(k) for k in _ENTRY_FIELDS if k in item})
            known.add(model_id)
            changed = True
    return items, changed


def registry_items(cfg: dict | None = None, *, create: bool = True) -> list[dict]:
    """登记表内容（必要时补种并落盘）；`create=False` 时只读不写。"""
    started_empty = not registry_path().exists()
    items = load_registry()
    seeded, changed = _seed(list(items), cfg, migrate_legacy=started_empty)
    if changed and create:
        # 只读环境里照样能列出模型，只是改动不会持久化
        with contextlib.suppress(OSError):
            save_registry(seeded)
    return seeded


def add_entry(entry: dict, cfg: dict | None = None) -> dict:
    """把一条自定义模型写进登记文件（同 id 覆盖）。"""
    items = registry_items(cfg)
    items = [item for item in items if str(item.get("id")) != str(entry.get("id"))]
    items.append({k: entry[k] for k in _ENTRY_FIELDS if k in entry})
    save_registry(items)
    return entry


def remove_entry(model_id: str, cfg: dict | None = None) -> bool:
    items = registry_items(cfg)
    kept = [item for item in items if str(item.get("id")) != str(model_id)]
    if len(kept) == len(items):
        return False
    save_registry(kept)
    return True


def _entry_from(item: dict, *, fallback_kind: str = "ctc") -> dict:
    """登记文件里的一条 → 内部条目（缺字段用内置默认补齐）。"""
    model_id = str(item.get("id"))
    base = dict(BUILTIN.get(model_id, {}))
    merged = {**base, **{k: item[k] for k in _ENTRY_FIELDS if k in item}}
    merged["id"] = model_id
    if not merged.get("kind"):
        merged["kind"] = fallback_kind
    merged.setdefault("engine", merged.get("engine") or merged["kind"])
    merged.setdefault("name", model_id)
    merged.setdefault("note", "自定义模型" if model_id not in BUILTIN else "")
    merged.setdefault("size_mb", 0)
    merged["files"] = tuple(merged.get("files") or ())
    merged["custom"] = model_id not in BUILTIN
    merged["_explicit_dir"] = "dir" in item
    return merged


# --------------------------------------------------------------------------- 条目


def _builtin(cfg: dict | None, model_id: str) -> dict:
    return {"id": model_id, **BUILTIN[model_id], "custom": False}


def _custom(cfg: dict | None, item: dict) -> dict:
    return {
        "id": item.get("id"),
        "name": item.get("name") or item.get("id"),
        "kind": item.get("kind"),
        "engine": item.get("engine") or item.get("kind"),
        "size_mb": item.get("size_mb") or 0,
        "note": item.get("note") or "自定义模型",
        "dir": str(item.get("dir") or ""),
        "files": tuple(item.get("files") or ()),
        "custom": True,
    }


def _is_overridden(model_id: str, item: dict) -> bool:
    """登记文件里的同 id 条目是否真的改了内置默认值（播种写入的默认值不算）。"""
    base = BUILTIN[model_id]
    if "dir" in item:
        default = str(model_dir(_builtin(None, model_id)))
        given = str(Path(str(item["dir"])).expanduser())
        if given.rstrip("/") != default.rstrip("/"):
            return True
    for key in ("note", "name"):
        if key in item and item[key] != base.get(key):
            return True
    if "files" in item and tuple(item["files"]) != tuple(base.get("files") or ()):
        return True
    return bool("size_mb" in item and item["size_mb"] != base.get("size_mb"))


def catalog(cfg: dict | None = None, *, create: bool = True) -> dict[str, dict]:
    """登记文件里的条目，id -> 内部条目；内置模型的说明/路径可被文件里的同 id 条目覆盖。"""
    try:
        items = registry_items(cfg, create=create)
    except CatalogError:
        items = []  # 文件坏了也要能启动 daemon；状态页 / models list 会单独报出来
    result: dict[str, dict] = {}
    for item in items:
        entry = _entry_from(item)
        if entry["id"] in result:
            continue
        if entry["id"] in BUILTIN:
            entry["custom"] = False
            entry["overridden"] = _is_overridden(entry["id"], item)
        result[entry["id"]] = entry
    for model_id in BUILTIN:  # 文件被手改到缺条目时，内置项仍然可用
        result.setdefault(model_id, _builtin(cfg, model_id))
    return result


def resolve(cfg: dict) -> dict:
    """配置里选中的模型条目。"""
    model_id = cfg["asr"].get("model") or DEFAULT_MODEL
    entries = catalog(cfg)
    if model_id not in entries:
        raise CatalogError(f"未知模型 {model_id!r}（finput models list 看已登记的模型）")
    return entries[model_id]


def model_dir(entry: dict) -> Path:
    """模型文件所在目录。登记文件里写了 dir 就以它为准（含内置模型搬家），否则按引擎默认位置。"""
    if entry.get("dir") and (entry.get("custom") or entry.get("overridden") or entry.get("_explicit_dir")):
        return Path(str(entry["dir"])).expanduser()
    if entry["kind"] == "qwen3":
        root = qwen3_models.find_root(entry["variant"])
        if root is not None:
            return qwen3_models.model_dir(root, entry["variant"])
        return (
            Path.home()
            / ".cache"
            / "modelscope"
            / "models"
            / qwen3_models.REPO_ID.replace("/", "--")
            / "snapshots"
            / "master"
            / f"model_{entry['variant']}"
        )
    if entry.get("custom"):
        return Path(entry["dir"]).expanduser()
    return asr_models.engine_dir(entry["engine"])


def required_files(entry: dict) -> tuple[str, ...]:
    if entry["kind"] == "qwen3":
        return qwen3_models.MODEL_FILES
    if entry.get("custom"):
        return tuple(entry.get("files") or ())
    return tuple(asr_models.ENGINES[entry["engine"]]["files"])  # type: ignore[arg-type]


def is_ready(entry: dict) -> bool:
    if entry.get("dir") and (entry.get("custom") or entry.get("overridden") or entry.get("_explicit_dir")):
        directory = model_dir(entry)
        return all((directory / name).is_file() for name in required_files(entry))
    if entry["kind"] == "qwen3":
        return qwen3_models.find_root(entry["variant"]) is not None
    if entry.get("custom"):
        if not entry.get("files"):
            return False
    else:
        return asr_models.is_ready(entry["engine"])
    directory = model_dir(entry)
    return all((directory / name).is_file() for name in required_files(entry))


def missing(entry: dict) -> list[str]:
    if entry["kind"] == "qwen3":
        return [] if is_ready(entry) else list(qwen3_models.MODEL_FILES)
    if not entry.get("custom"):
        return asr_models.missing_files(entry["engine"])
    directory = model_dir(entry)
    return [name for name in required_files(entry) if not (directory / name).is_file()]


# --------------------------------------------------------------------------- 安装


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _progress(model_id: str, done: int, total: int) -> None:
    with _LOCK:
        state = INSTALLS.setdefault(model_id, {})
        state["done"] = done
        state["total"] = total


def install(entry: dict, *, quiet: bool = False) -> Path:
    """下载内置模型；自定义模型只做完整性校验。"""
    if entry.get("custom"):
        if not is_ready(entry):
            raise CatalogError(f"自定义模型不完整：{model_dir(entry)} 缺少 {missing(entry)}")
        return model_dir(entry)
    if is_ready(entry):
        return model_dir(entry)
    if entry.get("_explicit_dir") and not entry.get("custom"):
        raise CatalogError(
            f"models.json 给 {entry['id']} 指定了目录 {model_dir(entry)}，但里面缺 "
            f"{missing(entry)}；删掉这条的 dir 字段就会下到默认位置"
        )
    model_id = entry["id"]
    if entry["kind"] == "qwen3":
        root = qwen3_models.fetch(entry["variant"], quiet=quiet)
        return qwen3_models.model_dir(root, entry["variant"])
    return asr_models.fetch(
        entry["engine"],
        quiet=quiet,
        on_progress=lambda done, total: _progress(model_id, done, total),
    )


def install_async(entry: dict) -> bool:
    """后台线程安装，状态见 `install_state()`；已在装则返回 False。"""
    model_id = entry["id"]
    with _LOCK:
        if INSTALLS.get(model_id, {}).get("status") == "installing":
            return False
        INSTALLS[model_id] = {"status": "installing", "done": 0, "total": 0, "error": ""}

    def run() -> None:
        try:
            install(entry, quiet=True)
        except Exception as exc:
            with _LOCK:
                INSTALLS[model_id] = {"status": "failed", "done": 0, "total": 0, "error": str(exc)}
            return
        with _LOCK:
            state = INSTALLS[model_id]
            state.update({"status": "done", "error": ""})

    threading.Thread(target=run, name=f"flash-input-install-{model_id}", daemon=True).start()
    return True


def install_state(model_id: str) -> dict | None:
    with _LOCK:
        state = INSTALLS.get(model_id)
        return dict(state) if state else None


def remove(entry: dict) -> str:
    """内置模型删缓存；自定义模型只取消登记（文件是用户自己的）。"""
    if entry.get("custom"):
        return "已取消登记（文件保留在原处）"
    if entry["kind"] == "qwen3":
        root = qwen3_models.find_root(entry["variant"])
        if root is None:
            return "未下载，无需删除"
        target = qwen3_models.model_dir(root, entry["variant"])
        shared = any(
            qwen3_models.find_root(v) == root for v in qwen3_models.VARIANTS if v != entry["variant"]
        )
        if shared:
            for name in qwen3_models.MODEL_FILES:
                (target / name).unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                target.rmdir()
            return f"已删除 {entry['name']} 的模型文件（同一快照里还有别的规格）"
        _remove_tree(root)
        return f"已删除 {root}"
    directory = asr_models.engine_dir(entry["engine"])
    if directory.is_dir():
        _remove_tree(directory)
        return f"已删除 {directory}"
    return "未下载，无需删除"


# --------------------------------------------------------------------------- 自定义模型


def _onnx_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".onnx")


def _minimal_options():
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    return options


def _check_loadable(directory: Path) -> None:
    import onnxruntime as ort

    for path in _onnx_files(directory):
        try:
            session = ort.InferenceSession(
                str(path), providers=["CPUExecutionProvider"], sess_options=_minimal_options()
            )
            del session
        except Exception as exc:
            raise CatalogError(f"{path.name} 不是可用的 ONNX 模型：{exc}") from exc


def _metadata(directory: Path) -> dict:
    path = _onnx_files(directory)[0]
    try:
        import onnxruntime as ort

        session = ort.InferenceSession(
            str(path), providers=["CPUExecutionProvider"], sess_options=_minimal_options()
        )
        meta = dict(session.get_modelmeta().custom_metadata_map or {})
        del session
        return meta
    except Exception:
        return {}


def detect(directory: Path) -> dict:
    """猜目录属于哪类模型；只做保守判断，猜不出时按 Paraformer 解码。"""
    if not directory.is_dir():
        raise CatalogError(f"目录不存在：{directory}")
    names = {p.name for p in _onnx_files(directory)}
    if not names:
        raise CatalogError(f"{directory} 里没有 .onnx 文件")
    _check_loadable(directory)
    if {"conv_frontend.onnx", "encoder.int8.onnx", "decoder.int8.onnx"} & names:
        return {"kind": "qwen3", "engine": "qwen3", "note": "三段式 Qwen3-ASR 图"}
    if (
        any(n.startswith("encoder") for n in names)
        and any(n.startswith("decoder") for n in names)
        and any(n.startswith("joiner") for n in names)
    ):
        return {"kind": "zipformer", "engine": "zipformer", "note": "encoder/decoder/joiner 流式图"}
    meta = _metadata(directory)
    if str(meta.get("model_type", "")).lower() == "paraformer":
        return {"kind": "ctc", "engine": "paraformer", "note": "meta model_type=paraformer"}
    if "lfr_window_size" in meta and ("with_itn" in meta or "text_norm" in meta):
        return {"kind": "ctc", "engine": "sensevoice", "note": "meta 带 LFR + text_norm，按 SenseVoice 处理"}
    if "lfr_window_size" in meta:
        return {"kind": "ctc", "engine": "paraformer", "note": "meta 带 LFR，按 Paraformer 处理"}
    return {"kind": "ctc", "engine": "paraformer", "note": "未识别出元数据，默认按 Paraformer 解码"}


def _slug(text: str) -> str:
    raw = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in text.strip().lower())
    return "-".join(part for part in raw.split("-") if part)[:40]


def register(cfg: dict, name: str, directory: str, *, kind: str = "auto") -> dict:
    """校验目录并生成一条自定义模型登记；调用方负责写进 cfg["asr"]["models"]。"""
    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise CatalogError("请填绝对路径（可以以 ~ 开头），相对路径会随工作目录变")
    info = detect(path)
    if kind not in ("auto", *KINDS):
        raise CatalogError(f"kind 只能是 auto / {' / '.join(KINDS)}")
    chosen_kind = info["kind"] if kind == "auto" else kind
    model_id = _slug(name) or _slug(path.name) or "custom"
    while model_id in BUILTIN:
        model_id = f"custom-{model_id}"
    taken = {item.get("id") for item in (cfg or {}).get("asr", {}).get("models", []) or []}
    base = model_id
    counter = 2
    while model_id in taken:
        model_id = f"{base}-{counter}"
        counter += 1
    files = tuple(p.name for p in _onnx_files(path))
    return {
        "id": model_id,
        "name": name.strip() or path.name,
        "kind": chosen_kind,
        "engine": info["engine"] if chosen_kind == "ctc" else chosen_kind,
        "dir": str(path),
        "files": list(files),
        "size_mb": round(qwen3_models.dir_size(path) / 1e6),
        "note": info["note"],
    }


def status(cfg: dict) -> list[dict]:
    """给设置页 / `finput models list` 用的完整列表。"""
    current = cfg["asr"].get("model") or DEFAULT_MODEL
    rows = []
    for model_id, entry in catalog(cfg).items():
        state = install_state(model_id) or {}
        total = state.get("total") or 0
        rows.append(
            {
                "id": model_id,
                "name": entry["name"],
                "kind": entry["kind"],
                "engine": entry.get("engine"),
                "size_mb": entry.get("size_mb", 0),
                "note": entry["note"],
                "custom": bool(entry.get("custom")),
                "source": (
                    "配置文件"
                    if entry.get("custom")
                    else ("内置·已定制" if entry.get("overridden") else "内置")
                ),
                "dir": str(model_dir(entry)),
                "ready": is_ready(entry),
                "current": model_id == current,
                "installing": state.get("status") == "installing",
                "install_error": state.get("error", ""),
                "progress": round(100.0 * (state.get("done") or 0) / total, 1) if total else None,
            }
        )
    return rows
