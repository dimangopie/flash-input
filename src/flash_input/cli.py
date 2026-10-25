"""命令行入口：`finput <子命令>`。

子命令分两类：日常/设置用的（`run` `stop` `status` `doctor` `config` `web` `service`
`models` `setup-permissions` `completion`），以及排错用的（`doctor --test hotkey|inject`）。
命令行与设置页共存：热键、常驻、粘贴、抓屏方式都在设置页里改，命令只补它们覆盖不了的
（服务安装、模型下载、udev 规则、补全脚本）。抓屏与识别没有单独的子命令：它们只由热键驱动，
免得一次手动识别把本来就不多的模型/剪贴板资源占用住，挡住用户真正在用热键时的输入。
退出码：0 正常 · 1 未实现 / 一般失败 · 2 环境或依赖不满足 · 3 配置错误 · 4 运行时错误 · 5 uinput 强制要求但不可用
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, env, keys, shortcut, xdg
from . import config as config_mod

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ENV = 2
EXIT_CONFIG = 3
EXIT_RUNTIME = 4
EXIT_NO_UINPUT = 5

PROG = "finput"


# --------------------------------------------------------------------------- 工具


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _load(args: argparse.Namespace) -> dict[str, Any]:
    return config_mod.load(getattr(args, "config", None))


def _web_url(cfg: dict[str, Any]) -> str:
    web = cfg["web"]
    port = web["port"]
    if port == 0:
        return f"http://{web['host']}:<动态端口>/  （daemon 会打印实际端口）"
    return f"http://{web['host']}:{port}/"


# --------------------------------------------------------------------------- 子命令


def cmd_config(args: argparse.Namespace) -> int:
    action = args.action
    if action == "path":
        print(config_mod.path(getattr(args, "config", None)))
        return EXIT_OK

    if action == "show":
        cfg = _load(args)
        target = config_mod.path(getattr(args, "config", None))
        if not args.raw:
            state = (
                "存在"
                if config_mod.config_file_exists(getattr(args, "config", None))
                else "不存在，以下为默认值"
            )
            print(f"# {target}（{state}）")
        if args.key:
            value = config_mod.get_path(cfg, args.key)
            print(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value)
        else:
            print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return EXIT_OK

    if action == "get":
        cfg = _load(args)
        value = config_mod.get_path(cfg, args.key)
        print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        return EXIT_OK

    if action == "set":
        cfg = _load(args)
        new_cfg = config_mod.set_path(cfg, args.key, args.value)
        written = config_mod.save(new_cfg, getattr(args, "config", None))
        # 回读确认：写进去的和读出来的一致
        reread = config_mod.load(getattr(args, "config", None))
        now = config_mod.get_path(reread, args.key)
        print(
            f"{args.key} = {now if isinstance(now, str) else json.dumps(now, ensure_ascii=False)}  → {written}"
        )
        # 守护进程在跑就顺手热加载：否则内存里还是旧配置，改模型要等下次重启才生效
        reply = _daemon_request({"cmd": "reload"}, timeout=1.0)
        if reply and reply.get("ok"):
            print("（守护进程已重新加载配置）")
        return EXIT_OK

    _err(f"未知的 config 动作：{action}")
    return EXIT_FAIL


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load(args)
    info = env.describe()
    problems: list[str] = []

    def line(ok: bool | None, label: str, value: str) -> None:
        # ✓/✗ = 必过项；○ = 可选且未做（不算失败）；· = 纯信息
        mark = "✓" if ok else ("·" if ok is None else "✗")
        print(f"  {mark} {label:<20} {value}")

    def optional(ok: bool, label: str, value: str) -> None:
        print(f"  {'✓' if ok else '○'} {label:<20} {value}")

    print(f"{PROG} {__version__}  （Python {sys.version.split()[0]}）")
    print()
    print("运行环境")
    line(info["session_type"] != "unknown", "会话类型", str(info["session_type"]))
    line(None, "桌面 / 发行版", f"{info['desktop']}（{env.desktop_family()}） / {info['distro']}")
    line(True, "配置文件", str(info["config_path"]))
    state_writable = _writable_dir(xdg.state_dir())
    line(
        state_writable,
        "状态目录",
        str(info["state_dir"]) + ("" if state_writable else "（不可写，日志只进 stderr）"),
    )
    line(True, "运行目录", str(info["runtime_dir"]))
    print()
    print("热键与注入")
    line(
        bool(info["keyboard_devices"]),
        "可读输入设备",
        f"{len(info['keyboard_devices'])} 个" if info["keyboard_devices"] else "无（需 udev uaccess 规则）",
    )
    if not info["keyboard_devices"]:
        problems.append("读不到 /dev/input/event*：运行 `finput setup-permissions --install` 并重新登录")
    line(
        bool(info["uinput_present"]),
        "/dev/uinput",
        "存在" if info["uinput_present"] else "不存在（内核未加载 uinput）",
    )
    line(
        bool(info["uinput_writable"]),
        "uinput 可写",
        "是" if info["uinput_writable"] else "否（需 uaccess 规则或 input 组）",
    )
    from . import privileged

    perm = privileged.status()
    optional(
        perm["rule_installed"],
        "udev 规则",
        "已安装（uaccess）"
        if perm["rule_installed"]
        else f"未安装（靠默认组权限也能用；换机器建议 `{PROG} setup-permissions --install`）",
    )
    line(
        info["clipboard_cmd"] is not None,
        "剪贴板写入",
        info["clipboard_cmd"] or f"缺 wl-clipboard / xclip（{env.package_hint('wl-clipboard')}）",
    )
    if info["clipboard_cmd"] is None:
        problems.append("没有可用的剪贴板命令：Wayland 装 wl-clipboard，Xorg 装 xclip")
    paste_key = cfg["inject"]["paste_key"]
    inject_mode = cfg["inject"]["mode"]
    line(None, "粘贴组合键", f"{paste_key}（inject.mode={inject_mode}）")
    if inject_mode == "uinput" and not info["uinput_writable"]:
        problems.append("inject.mode = uinput 但 /dev/uinput 不可写：装 udev uaccess 规则或改回 auto")
    print()
    print("抓屏与识别")
    from . import ocr

    trigger = cfg["ocr"].get("trigger") or "inject"
    if trigger == "listen":
        family = env.desktop_family()
        expected = None
        try:
            expected = keys.to_accelerator(cfg["hotkey"]["ocr_key"])
            actual = shortcut.screenshot_bindings().get(shortcut.SCREENSHOT_KEY, "")
        except Exception:  # gsettings 缺失/读失败都不该让 doctor 崩
            actual = ""
        if family != "gnome":
            line(False, "抓屏方式", f"{family} 上 ocr.trigger=listen 不适用（GNOME 专用）")
            problems.append(f"当前桌面是 {env.desktop()}，到设置页把「谁打开截屏窗口」改成 Spectacle 并保存")
        else:
            mine = bool(expected) and expected in actual
            line(mine, "抓屏方式", f"系统截屏 UI（GNOME 触发；需要「截屏」= {expected}）")
            if not mine:
                problems.append(
                    f"ocr.trigger=listen 但 GNOME 的「截屏」不是 {expected}："
                    f"到设置页把「谁打开截屏窗口」重新保存一次（现在是 {actual or '读不到'}）"
                )
    elif trigger == "spectacle":
        has_spectacle = env.has_cmd("spectacle")
        line(
            has_spectacle,
            "抓屏方式",
            "系统截屏 UI（KDE · Spectacle 区域截图）"
            if has_spectacle
            else "缺 spectacle（sudo apt install spectacle / pacman -S spectacle）",
        )
        if not has_spectacle:
            problems.append("ocr.trigger=spectacle 但没装 spectacle：装上它，或在设置页改回注入 Print")
    else:
        can_capture = info["uinput_writable"]
        line(
            can_capture,
            "抓屏方式",
            ("系统截屏 UI（注入 Print）" if can_capture else "需要 /dev/uinput 可写才能按 Print"),
        )
        if not can_capture:
            problems.append(
                "抓屏要注入 Print 打开系统截屏窗口：/dev/uinput 不可写，"
                "先 `finput setup-permissions --install` 再重登"
            )
    line(
        ocr.models_ready(),
        "OCR 模型",
        f"就绪（{ocr.MODELS_DIR}）"
        if ocr.models_ready()
        else "缺失（wheel 应自带 models/，试试重装 finput）",
    )
    if not ocr.models_ready():
        problems.append("OCR 模型文件缺失：重装 finput 包")
    print()
    print("语音识别")
    from . import asr_catalog

    entry = asr_catalog.resolve(cfg)
    ready = asr_catalog.is_ready(entry)
    size = asr_catalog.qwen3_models.human_size(
        asr_catalog.qwen3_models.dir_size(asr_catalog.model_dir(entry))
    )
    line(ready, "ASR 模型", f"{entry['name']} {'就绪（' + size + '）' if ready else '未下载'}")
    if not ready:
        problems.append(f"ASR 模型未下载：`{PROG} models fetch`（{entry['name']}）")
    line(
        None,
        "模型列表",
        f"asr.model={entry['id']} · 共 {len(asr_catalog.catalog(cfg))} 个（`{PROG} models list`）",
    )
    mic_ok = _mic_available()
    line(mic_ok, "录音设备", "sounddevice 可用" if mic_ok else "打不开录音流（装 libasound2 / 检查麦克风）")
    if not mic_ok:
        problems.append("录音不可用：确认系统有 libasound2，且当前用户能访问麦克风")
    print()
    print("常驻与设置页")
    from . import ipc, service

    svc = service.status()
    owner = _socket_owner(ipc)
    if info["systemd_managed"]:
        line(True, "systemd 托管", "是（日志进 journald）")
    elif owner is not None and svc["installed"] and svc["active"] == "active":
        line(
            False,
            "systemd 托管",
            f"否：当前 daemon 是手动起的（pid={owner}），抢走了 unit 的 socket —— 关掉终端热键就没了",
        )
        problems.append(
            f"手动运行的 daemon（pid={owner}）占着 socket：先 `{PROG} stop` 再 `{PROG} service install`，"
            "让它由 systemd 托管（`systemctl --user status flash-input` 看 MainPID）"
        )
    else:
        line(False, "systemd 托管", "否（手动运行，关掉终端热键就没了）")
        problems.append(
            f"daemon 没交给 systemd 托管：`{PROG} service install`（崩溃自动拉起、日志进 journald）"
        )
    if svc["installed"]:
        line(
            svc["active"] == "active",
            "自启 unit",
            f"{svc['enabled'] or '未启用'} / {svc['active'] or '未运行'}",
        )
        if svc["active"] != "active":
            problems.append(
                f"flash-input.service 没在运行：`finput service logs` 看原因（{svc['active'] or 'unknown'}）"
            )
    else:
        line(None, "自启 unit", f"未安装（`{PROG} service install`）")
    line(
        bool(info["session_bus"]),
        "会话总线",
        "存在" if info["session_bus"] else "缺 $XDG_RUNTIME_DIR/bus（通知不可用）",
    )
    line(bool(cfg["web"]["enabled"]), "Web 设置页", _web_url(cfg))
    mem = info["mem_available_gb"]
    if mem is not None:
        asr_resident = cfg["asr"]["resident"]
        warn = mem < 4 and asr_resident != "none"
        line(
            not warn,
            "可用内存",
            f"{mem} GB" + ("（ASR 峰值约 3 GB，建议 asr.resident = none）" if warn else ""),
        )
        if warn:
            problems.append("可用内存 < 4 GB 且 ASR 设为常驻：建议 `finput config set asr.resident none`")
    print()
    print("模型常驻策略")
    line(
        None,
        "ASR",
        f"{cfg['asr'].get('model')} · resident={cfg['asr']['resident']} · threads={cfg['asr']['threads']}",
    )
    line(None, "OCR", f"resident={cfg['ocr']['resident']} · threads={cfg['ocr']['threads']}")
    print()
    if problems:
        print("需要处理：")
        for item in problems:
            print(f"  - {item}")
        code = EXIT_ENV
    else:
        print("环境检查通过。")
        code = EXIT_OK
    if getattr(args, "test", None):
        return _doctor_test(args, cfg)
    return code


def _socket_owner(ipc_mod) -> int | None:
    """问 socket 上的 daemon 要 pid；没 daemon 返回 None。"""
    try:
        reply = ipc_mod.request({"cmd": "status"}, timeout=1.5)
    except Exception:  # socket 坏了也不该让 doctor 崩
        return None
    if not reply or not reply.get("ok"):
        return None
    pid = (reply.get("data") or {}).get("pid")
    return int(pid) if isinstance(pid, int) else None


def _doctor_test(args: argparse.Namespace, cfg: dict) -> int:
    """`doctor --test hotkey|inject`：需要用户动手的深度自测，平时不占命令名。"""
    which = getattr(args, "test", None)
    if which == "hotkey":
        return _doctor_test_hotkey(args, cfg)
    if which == "inject":
        return _doctor_test_inject(args, cfg)
    _err(f"未知自测项：{which}（可选 hotkey / inject）")
    return EXIT_FAIL


def _doctor_test_inject(args: argparse.Namespace, cfg: dict) -> int:
    from . import inject
    from .clipboard import ClipboardError, copy_text

    paste_key = cfg["inject"]["paste_key"]
    print(f"\n注入自测（粘贴键 {paste_key}）")
    if not inject.available():
        _err(f"无法测试：{inject.requirement_hint()}")
        return EXIT_ENV
    if paste_key == "none":
        _err("paste_key = none，没有可发送的组合键；先 `finput config set inject.paste_key shift+insert`")
        return EXIT_CONFIG
    text = f"Flash Input 粘贴测试 {datetime.now():%H:%M:%S}"
    try:
        used = copy_text(text)
    except ClipboardError as exc:
        _err(f"剪贴板写入失败：{exc}")
        return EXIT_ENV
    print(f"  已写入剪贴板（{used}）：{text}")
    print("  把光标放到任意输入框，3 秒后发一次粘贴键……", flush=True)
    time.sleep(3)
    try:
        inject.paste(paste_key)
    except inject.InjectUnavailable as exc:
        _err(f"uinput 不可用：{exc}")
        return EXIT_NO_UINPUT
    except inject.InjectError as exc:
        _err(f"注入失败：{exc}")
        return EXIT_RUNTIME
    print("  已发送。目标窗口里出现上面的文字，说明粘贴链路可用。")
    return EXIT_OK


def _doctor_test_hotkey(args: argparse.Namespace, cfg: dict) -> int:
    from . import hotkey

    try:
        required = hotkey.required_codes(cfg["hotkey"]["voice_key"], cfg["hotkey"]["ocr_key"])
    except hotkey.keys.KeyError as exc:  # type: ignore[attr-defined]
        _err(f"热键配置不认识：{exc}")
        return EXIT_CONFIG
    devices, notes = hotkey.discover(cfg["hotkey"]["devices"], required)
    print(f"\n热键自测：语音={cfg['hotkey']['voice_key']}（按住）  抓屏={cfg['hotkey']['ocr_key']}（按下）")
    for note in notes:
        print(f"  ! {note}")
    if not devices:
        _err(
            "没有可读的键盘设备。检查 /dev/input 权限：\n"
            "  ls -l /dev/input/event*\n"
            f"  {PROG} setup-permissions --print   # 看要装什么规则，加 --install 装上后重新登录"
        )
        return EXIT_ENV
    for device in devices:
        print(f"  监听 {device.path}  {device.name}")
    print("  5 秒内按一下热键（Ctrl-C 提前结束）…", flush=True)
    matcher = hotkey.Matcher(
        voice_key=cfg["hotkey"]["voice_key"],
        ocr_key=cfg["hotkey"]["ocr_key"],
        guard_ms=int(cfg["hotkey"]["guard_ms"]),
        min_hold_ms=int(cfg["hotkey"]["min_hold_ms"]),
        max_hold_s=int(cfg["hotkey"]["max_hold_s"]),
    )
    hits: list[str] = []

    def on_action(name: str) -> None:
        hits.append(name)
        print(f"  命中 → {name}")

    def on_key(name: str, value: int) -> None:
        if value == 1:
            print(f"  按下 {name}")

    listener = hotkey.Listener(matcher, devices, on_action, on_key)
    try:
        time.sleep(5.0)
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()
    print("  命中：" + ("、".join(hits) if hits else "无（没识别到热键）"))
    return EXIT_OK if hits else EXIT_RUNTIME


def cmd_completion(args: argparse.Namespace) -> int:
    """打印 / 安装 / 卸载 shell 补全（脚本由 argparse 现生成，不会过期）。"""
    from . import completion

    target = getattr(args, "target", "print") or "print"
    shell = getattr(args, "shell", None)
    action = "print"
    if target in completion.SHELLS:
        shell, action = target, "print"
    elif target in ("print", "install", "uninstall"):
        action = target
    else:
        _err(f"不认识的目标：{target}（可用：print / install / uninstall / {' / '.join(completion.SHELLS)}）")
        return EXIT_CONFIG

    try:
        name = completion.detect_shell(shell)
        parser = build_parser()
        if action == "print":
            print(completion.script(parser, name))
            return EXIT_OK
        if action == "uninstall":
            removed = completion.uninstall(name)
            print(f"已删除 {name} 补全脚本" if removed else f"没有找到 {name} 补全脚本（未安装）")
            return EXIT_OK
        path = completion.install(parser, name)
    except completion.CompletionError as exc:
        _err(str(exc))
        return EXIT_ENV

    print(f"已安装 {name} 补全：{path}")
    print(completion.hint(name))
    print(f"临时试用：source <({PROG} completion {name})")
    return EXIT_OK


def _daemon_request(payload: dict, timeout: float = 3.0, explicit_config=None):
    from . import ipc

    return ipc.request(payload, timeout=timeout, runtime_dir=None)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load(args)
    sock = xdg.socket_path()
    info = _daemon_request({"cmd": "status"})
    print(f"{PROG} {__version__}")
    if info and info.get("ok"):
        data = info.get("data") or {}
        print(f"守护进程  运行中 · pid={data.get('pid')} · 已运行 {data.get('uptime_s')} 秒（{sock}）")
        asr_busy = (data.get("asr_task") or "忙") if data.get("asr_busy") else "空闲"
        ocr_busy = (data.get("ocr_task") or "忙") if data.get("ocr_busy") else "空闲"
        print(
            f"活动      voice={data.get('voice_state')} · ASR {asr_busy} · OCR {ocr_busy}"
            f" · 最近动作={data.get('last_action') or '-'}"
        )
        devices = data.get("devices") or []
        print(
            f"热键监听  mode={data.get('hotkey_mode')} · {len(devices)} 个键盘设备"
            + ("：" + ", ".join(d.get("name", "?") for d in devices) if devices else "")
        )
        for note in data.get("device_notes") or []:
            print(f"          ! {note}")
        asr_info, ocr_info = data.get("asr") or {}, data.get("ocr") or {}
        label = str(asr_info.get("model") or "?")
        print(
            f"模型      ASR {label} {'已加载' if asr_info.get('loaded') else '未加载'}"
            f" · resident={asr_info.get('resident')}"
        )
        print(
            f"          OCR {'已加载' if ocr_info.get('loaded') else '未加载'}"
            f" · resident={ocr_info.get('resident')}"
        )
        web = data.get("web")
        print(f"设置页    {('http://{}:{}'.format(web['host'], web['port'])) if web else '未启动'}")
        if data.get("last_error"):
            print(f"最近错误  {data['last_error']}")
        return EXIT_OK
    print(f"守护进程  未运行（{sock} 不存在；`finput service install` 常驻，或先 `finput run`）")
    print(
        f"热键配置  mode={cfg['hotkey']['mode']} 语音={cfg['hotkey']['voice_key']} 抓屏={cfg['hotkey']['ocr_key']}"
    )
    print(f"剪贴板    {env.first_cmd('wl-copy', 'xclip') or '缺失'}")
    from . import asr_catalog

    entry = asr_catalog.resolve(cfg)
    print(
        f"语音      {entry['id']}（{entry['name']}） · resident={cfg['asr']['resident']} · "
        f"模型 {'已就绪' if asr_catalog.is_ready(entry) else '未下载（finput models fetch）'}"
    )
    print(f"OCR       resident={cfg['ocr']['resident']} · threads={cfg['ocr']['threads']}")
    return EXIT_OK


def _wait_socket_gone(timeout: float = 5.0) -> bool:
    """等守护进程释放 socket（进程退出后由 atexit 清掉）。"""
    deadline = time.monotonic() + timeout
    path = xdg.socket_path()
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(0.1)
    return not path.exists()


def _mapping_hint() -> None:
    """停止 / 卸载都不会动桌面快捷键映射，显式提醒用户自己去查。"""
    print(
        "\n提示：停止运行或删除程序不会自动还原桌面按键映射，请自行检查当前桌面环境的按键映射：\n"
        "  GNOME: gsettings list-recursively org.gnome.shell.keybindings | grep show-screenshot-ui\n"
        "  KDE:   用 Spectacle，不改桌面按键\n"
        "  想还原 GNOME 的「截屏界面」：到设置页把「谁打开截屏窗口」改成「注入 Print」并保存"
    )


def cmd_stop(args: argparse.Namespace) -> int:
    """停掉守护进程：归 systemd 管的走 systemctl stop，手动跑的走 IPC。"""
    from . import ipc, service

    st = service.status()
    if st.get("active") == "active":
        try:
            service.stop()
        except RuntimeError as exc:
            _err(f"停止服务失败：{exc}")
            return EXIT_ENV
        _wait_socket_gone()
        print("已停止 systemd 服务 flash-input.service（enable 未变，下次登录仍会自启）")
        _mapping_hint()
        return EXIT_OK

    reply = ipc.request({"cmd": "shutdown"}, timeout=5.0)
    if reply is None:
        print("守护进程没有在运行")
        return EXIT_OK
    if not reply.get("ok"):
        _err(f"停止失败：{reply.get('msg') or reply}")
        return EXIT_RUNTIME
    _wait_socket_gone()
    print("已停止守护进程")
    _mapping_hint()
    return EXIT_OK


def cmd_web(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if not cfg["web"]["enabled"]:
        _err("Web 设置页已被配置关闭（web.enabled = false）")
        return EXIT_CONFIG
    url = _web_url(cfg)
    if cfg["web"]["port"] == 0:
        print(url)
        return EXIT_OK
    print(
        f"{url}  （随 `finput run` 启动；当前 daemon "
        + ("在跑" if xdg.socket_path().exists() else "未运行")
        + "）"
    )
    if args.open:
        webbrowser.open(url)
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from . import daemon as daemon_mod

    try:
        d = daemon_mod.Daemon(
            cfg,
            explicit_config=getattr(args, "config", None),
        )
        d.start()
    except daemon_mod.DaemonError as exc:
        _err(f"启动失败：{exc}")
        return EXIT_RUNTIME
    import signal

    def _stop(_signum, _frame):
        d.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    _err(f"daemon 已启动（pid={os.getpid()}），Ctrl-C 退出")
    return d.run_forever()


def _mic_available() -> bool:
    try:
        import sounddevice as sd

        return bool(sd.query_devices())
    except Exception:
        return False


def cmd_setup_permissions(args: argparse.Namespace) -> int:
    from . import privileged

    if getattr(args, "print_rule", False):
        print(privileged.rule_text().rstrip())
        return EXIT_OK
    if getattr(args, "install", False):
        try:
            ran = privileged.install()
        except RuntimeError as exc:
            _err(f"安装失败：{exc}")
            return EXIT_FAIL
        for command in ran:
            print(f"  $ {command}")
        info = privileged.status()
        print(
            "\n规则已写入。"
            + ("现在就能用。" if privileged.usable(info) else "重新登录（或重新插一次键盘）后生效。")
        )
        return EXIT_OK

    info = privileged.status()
    print(f"{PROG} {__version__}  权限")
    print(f"  {'✓' if info['uinput_present'] else '✗'} /dev/uinput 存在")
    print(f"  {'✓' if info['uinput_writable'] else '✗'} /dev/uinput 可写")
    print(f"  {'✓' if int(info['keyboard_devices']) else '✗'} 可读键盘设备 {info['keyboard_devices']} 个")
    rule = "已安装" if info["rule_installed"] else "未安装"
    print(f"  · udev 规则 {rule}（{info['rule_path']}）")
    if not privileged.usable(info):
        print("\n当前不可用。运行 `finput setup-permissions --install`（需要 sudo），装完重新登录。")
        print("规则内容：`finput setup-permissions --print`")
        return EXIT_ENV
    print("\n当前权限够用。")
    return EXIT_OK


def cmd_service(args: argparse.Namespace) -> int:
    from . import service

    action = getattr(args, "action", "status") or "status"
    if action == "print":
        print(service.unit_text(getattr(args, "config", None)))
        return EXIT_OK
    if action == "install":
        try:
            ran = service.install(getattr(args, "config", None))
        except (OSError, RuntimeError) as exc:
            _err(f"安装失败：{exc}")
            return EXIT_FAIL
        for command in ran:
            print(f"  $ {command}")
        print(f"\n已安装 {service.UNIT_PATH}")
        print(f"日志：`{PROG} service logs -f`   状态：`{PROG} service status`")
        return EXIT_OK
    if action == "uninstall":
        ran = service.uninstall()
        for command in ran:
            print(f"  $ {command}")
        print("已卸载 unit（配置、模型没动）。")
        _mapping_hint()
        return EXIT_OK
    if action == "logs":
        return service.logs(getattr(args, "lines", 30) or 30, bool(getattr(args, "follow", False)))

    info = service.status()
    if not info["installable"]:
        _err("这套系统没有 systemctl --user（不是 systemd），请用 `finput run` 或发行版的自启方式")
        return EXIT_ENV
    print(f"{PROG} {__version__}  systemd user service")
    print(f"  {'✓' if info['installed'] else '·'} unit      {info['path']}")
    print(f"  · 启用      {info['enabled'] or '未安装'}")
    print(f"  · 运行      {info['active'] or '未运行'}")
    return EXIT_OK if info["installed"] else EXIT_FAIL


def cmd_models(args: argparse.Namespace) -> int:
    from . import asr_catalog, ocr

    cfg = _load(args)
    if args.action == "list":
        rows = asr_catalog.status(cfg)
        print(f"{PROG} {__version__}  语音模型")
        print(f"  登记文件：{asr_catalog.registry_path()}（路径与说明都在这里，可直接编辑）")
        print("  ✓ 已下载就绪   ✗ 未下载（装好才能启用，见末尾）   … 安装中   * 当前使用")
        print()
        for row in rows:
            mark = "*" if row["current"] else " "
            if row["installing"]:
                state = f"安装中 {row['progress'] or 0}%"
                flag = "…"
            elif row["ready"]:
                state = "已就绪"
                flag = "✓"
            else:
                state = "未下载"
                flag = "✗"
            tag = "自定义" if row["custom"] else row["kind"]
            print(
                f" {mark}{flag} {row['id']:<14} {state:<8} {row['name']:<18} {row['size_mb']:>5} MB  {tag}/{row['source']}"
            )
            print(f"      {row['note']}  {row['dir']}")
            if row.get("install_error"):
                print(f"      上次安装失败：{row['install_error']}")
        ready = ocr.models_ready()
        print()
        print(f"   {'✓' if ready else '✗'} OCR（PP-OCRv6 small，随 wheel 分发）{ocr.MODELS_DIR}")
        missing = [row["id"] for row in rows if not row["ready"]]
        print()
        if not missing:
            print(
                "所有语音模型都已下载。切换：`finput config set asr.model <id>`，或设置页「语音模型」里点「启用」。"
            )
        else:
            print(f"缺 {len(missing)} 个：{'、'.join(missing)}")
            print("  装当前选中的模型：`finput models fetch`（按 asr.model 下载）")
            print("  装其它模型 / 删除 / 添加本地 ONNX：设置页「语音模型」卡片")
            print("  切换模型：`finput config set asr.model <id>`")
        return EXIT_OK

    entry = asr_catalog.resolve(cfg)
    if asr_catalog.is_ready(entry):
        print(f"✓ {entry['name']} 已就绪：{asr_catalog.model_dir(entry)}")
        return EXIT_OK
    print(f"下载 {entry['name']}（{entry['size_mb']} MB）…", file=sys.stderr, flush=True)
    try:
        directory = asr_catalog.install(entry)
    except (asr_catalog.CatalogError, asr_catalog.qwen3_models.ModelError) as exc:
        _err(str(exc))
        return EXIT_ENV
    print(f"✓ {entry['id']} → {directory}")
    return EXIT_OK


# --------------------------------------------------------------------------- 解析器


def completion_shells() -> tuple[str, ...]:
    from . import completion

    return completion.SHELLS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="语音 / 抓屏 → 文字 → 剪贴板（本地 ONNX 推理，Xorg / Wayland 通用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "日常只用热键：按住语音键说话、按抓屏键调系统截屏窗口。\n"
            "\n"
            "常用：\n"
            f"  {PROG} service install       装成 systemd 用户服务（登录自动起）\n"
            f"  {PROG} status                看热键设备 / 模型 / 最近动作\n"
            f"  {PROG} doctor                环境自检（权限、剪贴板、模型、内存）\n"
            f"  {PROG} doctor --test hotkey  看键盘设备、按键命中（选键/排错）\n"
            f"  {PROG} web                   设置页地址（随守护进程启动）\n"
            f"  {PROG} stop                  停掉守护进程（调试用）\n"
            f"  {PROG} completion install    给 bash/zsh/fish 装命令补全\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument(
        "--config", metavar="PATH", help="指定配置文件（默认 $XDG_CONFIG_HOME/flash-input/config.json）"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")

    sub = parser.add_subparsers(dest="command", metavar="<子命令>")

    p_run = sub.add_parser("run", help="常驻守护进程（热键 / 模型 / uinput / 设置页）")
    p_run.set_defaults(func=cmd_run)

    p_cfg = sub.add_parser("config", help="读写配置")
    cfg_sub = p_cfg.add_subparsers(dest="action", required=True, metavar="<动作>")
    p_show = cfg_sub.add_parser("show", help="打印生效中的配置")
    p_show.add_argument("key", nargs="?", help="只打印某一项（点号路径）")
    p_show.add_argument("--raw", action="store_true", help="不打印注释行，直接输出 JSON")
    p_show.set_defaults(func=cmd_config, action="show")
    p_get = cfg_sub.add_parser("get", help="读一项")
    p_get.add_argument("key")
    p_get.set_defaults(func=cmd_config, action="get")
    p_set = cfg_sub.add_parser("set", help="改一项（按默认值推断类型）")
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_set.set_defaults(func=cmd_config, action="set")
    p_path = cfg_sub.add_parser("path", help="打印配置文件路径")
    p_path.set_defaults(func=cmd_config, action="path")

    p_stop = sub.add_parser("stop", help="停止守护进程（或 systemd 服务）")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="守护进程 / 设置页 / 热键 的当前状态")
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="环境自检")
    p_doctor.add_argument(
        "--test", metavar="<项目>", help="深入自测：hotkey（列设备并监听 5 秒） / inject（发一次真实粘贴键）"
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_web = sub.add_parser("web", help="设置页地址")
    p_web.add_argument("--print", action="store_true", help="只打印地址（默认行为）")
    p_web.add_argument("--open", action="store_true", help="用默认浏览器打开")
    p_web.set_defaults(func=cmd_web)

    p_setup = sub.add_parser("setup-permissions", help="检查/安装 udev uaccess 规则（需 sudo 写入）")
    p_setup.add_argument("--print", dest="print_rule", action="store_true", help="只打印规则内容")
    p_setup.add_argument("--install", action="store_true", help="写入 /etc/udev/rules.d/ 并 reload")
    p_setup.set_defaults(func=cmd_setup_permissions)

    p_service = sub.add_parser("service", help="systemd user service")
    service_sub = p_service.add_subparsers(dest="action", required=True, metavar="<动作>")
    for name, text in (
        ("install", "写入并 enable --now"),
        ("uninstall", "停用并删除"),
        ("status", "is-enabled / is-active"),
        ("logs", "看 journal（默认 30 行）"),
        ("print", "只打印 unit 内容"),
    ):
        p = service_sub.add_parser(name, help=text)
        if name == "logs":
            p.add_argument("-f", "--follow", action="store_true", help="持续跟随")
            p.add_argument("-n", "--lines", type=int, default=30, help="看最近多少行（默认 30）")
        p.set_defaults(func=cmd_service, action=name)

    p_completion = sub.add_parser(
        "completion",
        help="生成/安装 shell 补全（bash / zsh / fish，脚本由命令面现生成）",
    )
    p_completion.add_argument(
        "target",
        nargs="?",
        default="print",
        metavar="print|install|uninstall|SHELL",
        help="print = 打印脚本（默认）；install / uninstall；也可以直接写 bash / zsh / fish",
    )
    p_completion.add_argument(
        "--shell", choices=list(completion_shells()), help="目标 shell（默认按 $SHELL 猜）"
    )
    p_completion.set_defaults(func=cmd_completion)

    p_models = sub.add_parser("models", help="模型管理")
    models_sub = p_models.add_subparsers(dest="action", required=True, metavar="<动作>")
    models_sub.add_parser("list", help="列出可用/已下载模型").set_defaults(func=cmd_models, action="list")
    models_sub.add_parser("fetch", help="下载配置里选定的语音模型（asr.model）").set_defaults(
        func=cmd_models, action="fetch"
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from . import logging as logging_mod

    parser = build_parser()
    args = parser.parse_args(argv)
    logging_mod.setup(verbose=getattr(args, "verbose", False))
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK
    try:
        return int(args.func(args))
    except config_mod.ConfigError as exc:
        _err(f"配置错误：{exc}")
        return EXIT_CONFIG
    except KeyboardInterrupt:
        _err("已中断")
        return EXIT_RUNTIME


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
