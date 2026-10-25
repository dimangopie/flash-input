"""常驻守护进程：一个进程同时持有热键监听、录音、模型、uinput 与设置页。

线程模型（全部很短的活）：

    IPC 服务线程   —— 一行 JSON 一问一答，命令只改状态/投递任务
    热键线程       —— epoll 读 /dev/input，喂状态机，动作丢给本类
    维护线程       —— 每 0.5 s 检查「按住超时」、按 resident 策略卸载模型
    ASR 模型线程   —— 加载 / 转写 / 卸载，一条线程内严格串行
    OCR 模型线程   —— 抓屏 / 识别 / 卸载，一条线程内严格串行
    Web 线程       —— 设置页（与 CLI 共用 webui）

两个模型**互不阻塞**：ASR 正在转写时按抓屏键照样能抓图识别，反之亦然。同一个模型
内部不并发（load / infer / unload 都排在自己那条线程里，不会出现「加载到一半被卸载」）。
跨模型唯一共享的是剪贴板 + uinput 粘贴键，那一步用 `_deliver_lock` 串行，毫秒级。
热键是唯一入口：daemon 不在时按键不会被处理（`finput doctor --test hotkey` 可自检）。
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__, asr, capture, config, env, hotkey, inject, ipc, runtime, xdg
from . import logging as log
from .asr import AsrEngine, AsrError
from .clipboard import ClipboardError
from .config import RESIDENT_SECONDS
from .ocr import OcrEngine, OcrError
from .recorder import LiveRecorder, RecorderError, duration, trim_trailing_silence

LOG = "flash-input"


class DaemonError(RuntimeError):
    pass


class Daemon:
    def __init__(
        self,
        cfg: dict[str, Any] | None = None,
        *,
        no_web: bool = False,
        web_port: int | None = None,
        explicit_config: str | None = None,
    ) -> None:
        self.cfg = cfg or config.load()
        self.no_web = no_web
        self.web_port = web_port
        self.explicit_config = explicit_config
        self.started_at = time.time()
        self.stop_event = threading.Event()
        self._state_lock = threading.Lock()
        # 两个模型各一条专属线程：模型内部串行，模型之间并行
        self._asr_worker = runtime.ModelWorker("asr")
        self._ocr_worker = runtime.ModelWorker("ocr")
        self._deliver_lock = threading.Lock()  # 只锁「写剪贴板 + 发粘贴键」这一步
        self._voice_state = "idle"  # idle / recording / working
        self._last_action = ""
        self._last_error = ""
        self._asr: AsrEngine | None = None
        self._asr_key: tuple[str, str, int] | None = None  # 建这个引擎时的配置身份
        self._asr_last_used = 0.0
        self._ocr: OcrEngine | None = None
        self._ocr_key: tuple[int, str] | None = None
        self._asr_lock = threading.Lock()
        self._ocr_lock = threading.Lock()
        self._recorder = LiveRecorder(max_seconds=float(self.cfg["hotkey"]["max_hold_s"]))
        hk = self.cfg["hotkey"]
        self._matcher = hotkey.Matcher(
            voice_key=hk["voice_key"],
            ocr_key=hk["ocr_key"],
            guard_ms=int(hk["guard_ms"]),
            min_hold_ms=int(hk["min_hold_ms"]),
            max_hold_s=int(hk["max_hold_s"]),
        )
        self._listener: hotkey.Listener | None = None
        self._ipc_server: ipc.Server | None = None
        self._web_server: Any = None
        self._maintenance: threading.Thread | None = None
        self.device_notes: list[str] = []
        self.hotkey_devices: list[dict] = []

    # ------------------------------------------------------------------ 生命周期

    @property
    def busy(self) -> bool:
        return self._asr_worker.busy or self._ocr_worker.busy

    @property
    def busy_task(self) -> str:
        return self._asr_worker.task or self._ocr_worker.task

    def busy_reason(self) -> str:
        """忙的时候说清楚在忙什么、忙了多久，而不是只弹一句「忙」。"""
        return self._asr_worker.reason() or self._ocr_worker.reason()

    def start(self) -> None:
        logger = log.get(LOG)
        logger.info(
            "启动 daemon pid=%s session=%s desktop=%s", os.getpid(), env.session_type(), env.desktop()
        )

        # 有界就绪等待：等不到也继续（抓屏走 uinput，不依赖会话总线）
        deadline = time.monotonic() + 30
        delay = 0.2
        while time.monotonic() < deadline:
            if env.session_bus_present() and xdg.runtime_dir().exists():
                break
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
        if not env.session_bus_present():
            logger.warning("会话总线还没就绪；热键、抓屏与粘贴都不依赖它，可以继续")

        try:
            self._ipc_server = ipc.make_server(self.handle)
        except ipc.IpcError as exc:
            raise DaemonError(str(exc)) from exc
        ipc.serve_in_thread(self._ipc_server)
        logger.info("IPC 就绪：%s", xdg.socket_path())

        if not self.no_web and self.cfg["web"]["enabled"]:
            from . import webui

            port = self.web_port if self.web_port is not None else int(self.cfg["web"]["port"])
            self._web_server = webui.serve_background(
                self.cfg,
                explicit_config=self.explicit_config,
                on_reload=self.reload,
                on_rebuild=self.rebuild_models,
                on_engine_state=self.engine_state,
                host=str(self.cfg["web"]["host"]),
                port=port,
                quiet=False,
            )

        self._start_hotkey()
        self._maintenance = threading.Thread(target=self._maintain, name="flash-input-maintain", daemon=True)
        self._maintenance.start()

    def _start_hotkey(self) -> None:
        logger = log.get(LOG)
        mode = self.cfg["hotkey"]["mode"]
        if mode != "evdev":
            logger.info("热键模式 off：不监听 /dev/input，只响应设置页与 CLI")
            return
        required = hotkey.required_codes(self.cfg["hotkey"]["voice_key"], self.cfg["hotkey"]["ocr_key"])
        devices, notes = hotkey.discover(self.cfg["hotkey"]["devices"], required)
        self.device_notes = notes
        self.hotkey_devices = [device.describe() for device in devices]
        for note in notes:
            logger.warning("热键：%s", note)
        if not devices:
            print(
                "热键不可用：没有可读的键盘设备。跑 `finput hotkey test` 看原因，"
                "或 `finput setup-permissions --install` 后再登录一次。",
                flush=True,
            )
            return
        logger.info("热键监听 %s 个设备：%s", len(devices), ", ".join(d["name"] for d in self.hotkey_devices))
        self._listener = hotkey.Listener(self._matcher, devices, self._on_action, rescan=self._rescan_devices)
        self._listener.start()

    def _rescan_devices(self, existing: set[str]) -> list:
        """热插拔：每 2 秒找一次新键盘，已打开的不重复开。"""
        required = hotkey.required_codes(self.cfg["hotkey"]["voice_key"], self.cfg["hotkey"]["ocr_key"])
        devices, notes = hotkey.discover(self.cfg["hotkey"]["devices"], required)
        fresh = []
        for device in devices:
            if device.path in existing:
                device.close()
                continue
            fresh.append(device)
        if fresh:
            log.get(LOG).info("热键：接入新设备 %s", ", ".join(d.name for d in fresh))
            self.hotkey_devices.extend(device.describe() for device in fresh)
        for note in notes:
            # 设备刚被拔掉时 open 会 ENOENT，每次重扫都报一条太吵；只记首次的权限/类型问题。
            if "No such file or directory" in note:
                continue
            if note not in self.device_notes:
                self.device_notes.append(note)
                log.get(LOG).warning("热键：%s", note)
        return fresh

    def _live_devices(self) -> list[dict]:
        """当前真正在监听的设备（拔掉的设备会从列表里消失）。"""
        listener = self._listener
        if listener is not None:
            return [device.describe() for device in listener.devices]
        return self.hotkey_devices

    def run_forever(self) -> int:
        try:
            while not self.stop_event.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        logger = log.get(LOG)
        logger.info("停止 daemon")
        self.stop_event.set()
        if self._listener is not None:
            self._listener.stop()
        if self._ipc_server is not None:
            self._ipc_server.shutdown()
            self._ipc_server.server_close()
        if self._web_server is not None:
            with contextlib.suppress(Exception):
                self._web_server.shutdown()
        self._recorder.cancel()
        self._asr_worker.stop(wait=True)
        self._ocr_worker.stop(wait=True)
        with self._asr_lock:
            if self._asr is not None:
                self._asr.unload()
                self._asr = None
        if self._ocr is not None:
            self._ocr.unload()

    def reload(self, cfg: dict[str, Any] | None = None) -> None:
        """设置页保存后热更新（热键键位/常驻策略等；线程与设备不重建）。"""
        self.cfg = cfg or config.load()
        hk = self.cfg["hotkey"]
        self._matcher = hotkey.Matcher(
            voice_key=hk["voice_key"],
            ocr_key=hk["ocr_key"],
            guard_ms=int(hk["guard_ms"]),
            min_hold_ms=int(hk["min_hold_ms"]),
            max_hold_s=int(hk["max_hold_s"]),
        )
        if self._listener is not None:
            self._listener.matcher = self._matcher
        self._sync_capture_binding()
        log.get(LOG).info("配置已热更新")

    def _sync_capture_binding(self) -> None:
        """`ocr.trigger` 变了就顺手同步 GNOME 的截屏键（设置页之外的入口也不落空）。"""
        from . import shortcut

        try:
            note = shortcut.apply_trigger(
                str(self.cfg["ocr"].get("trigger") or "inject"),
                str(self.cfg["hotkey"].get("ocr_key") or ""),
            )
        except Exception as exc:  # gsettings 缺失/被改坏都不该影响 daemon
            log.get(LOG).debug("同步桌面截屏快捷键失败：%s", exc)
            return
        if note:
            log.get(LOG).info("桌面截屏快捷键：%s", note)

    # ------------------------------------------------------------------ IPC

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd")
        logger = log.get(LOG)
        logger.debug("IPC %s", request)
        if cmd == "ping":
            return self._reply(msg="pong")
        if cmd == "status":
            return self._reply(data=self.status())
        if cmd == "shutdown":
            self.stop_event.set()
            return self._reply(msg="正在退出")
        if cmd == "reload":
            self.reload()
            return self._reply(msg="配置已重新加载")
        # 没有 "ocr" / "voice" 这类手动触发：抓屏与识别只由热键驱动，
        # 命令行抢跑会和用户正在按的热键抢同一个模型
        return self._reply(ok=False, msg=f"未知命令：{cmd}")

    @staticmethod
    def _reply(
        *, ok: bool = True, busy: bool = False, msg: str = "", data: dict | None = None
    ) -> dict[str, Any]:
        reply: dict[str, Any] = {"ok": ok, "busy": busy, "msg": msg}
        if data is not None:
            reply["data"] = data
        return reply

    # ------------------------------------------------------------------ 动作

    def _on_action(self, action: str) -> None:
        if action == "voice_start":
            self.voice_start()
        elif action == "voice_stop":
            self.voice_stop()
        elif action == "voice_cancel":
            self.voice_cancel()
        elif action == "ocr":
            self.ocr()

    def voice_start(self) -> str:
        with self._state_lock:
            if self._voice_state == "recording":
                return "已经在录音"
            if self._asr_worker.busy:
                self._last_action = "voice_start:busy"
                reason = self._asr_worker.reason()
                log.get(LOG).info("语音输入忙，忽略本次热键：%s", reason)
                return f"忙：{reason}"
            try:
                self._recorder.start()
            except RecorderError as exc:
                self._last_error = str(exc)
                log.get(LOG).error("录音失败：%s", exc)
                return f"录音失败：{exc}"
            self._voice_state = "recording"
            self._last_action = "voice_start"
        log.get(LOG).info("开始录音")
        if self.cfg["asr"]["preload_on_record"]:
            self._asr_worker.submit("语音模型加载", self._preload_asr)
        return "开始录音"

    def voice_stop(self) -> str:
        with self._state_lock:
            if self._voice_state != "recording":
                return "当前没有在录音"
            self._voice_state = "idle"
            audio = self._recorder.stop()
        self._last_action = "voice_stop"
        if audio.size == 0:
            log.get(LOG).info("没有录到声音：按键时间太短，或麦克风不可用")
            return "录音太短，已丢弃"
        audio = trim_trailing_silence(audio, keep=float(self.cfg["voice"]["tail_silence_s"]))
        seconds = duration(audio)
        log.get(LOG).info("录音结束：%.1f 秒", seconds)
        job = self._guard("语音转写", lambda: self._transcribe_and_deliver(audio, seconds), reset_voice=True)
        if not self._asr_worker.submit("语音转写", job):
            return f"忙：{self._asr_worker.reason()}，录音已丢弃"
        return f"录音 {seconds:g} 秒，转写中"

    def voice_cancel(self) -> str:
        with self._state_lock:
            if self._voice_state != "recording":
                self._matcher.cancel()
                return "当前没有在录音"
            self._voice_state = "idle"
            elapsed = self._recorder.cancel()
        self._matcher.cancel()
        self._last_action = "voice_cancel"
        return f"已取消（{elapsed:.1f} 秒录音丢弃）"

    def ocr(self) -> str:
        if self._ocr_worker.busy:
            reason = self._ocr_worker.reason()
            log.get(LOG).info("抓屏 OCR 忙，忽略本次热键：%s", reason)
            log.get(LOG).info(
                "抓屏 OCR 忙，忽略本次热键：%s（截屏窗口已关的话最多等 %s 秒）",
                reason,
                self.cfg["ocr"]["timeout_s"],
            )
            return f"忙：{reason}"
        if not self._ocr_worker.submit("抓屏 OCR", self._guard("抓屏 OCR", self._ocr_and_deliver)):
            return f"忙：{self._ocr_worker.reason()}"
        return "抓屏中…"

    # ------------------------------------------------------------------ 工作线程

    def _guard(self, name: str, work, *, reset_voice: bool = False):
        """给任务套一层错误处理：记最近错误、发通知、必要时复位语音状态。"""

        def run() -> None:
            try:
                work()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.get(LOG).exception("%s失败", name)
                log.get(LOG).error("%s失败：%s", name, exc)
            finally:
                if reset_voice:
                    with self._state_lock:
                        self._voice_state = "idle"

        return run

    def _preload_asr(self) -> None:
        engine = self._get_asr(preload=True)
        log.get(LOG).info("ASR 预加载完成（%s ms）", engine.load_ms if engine else 0)

    def _get_asr(self, *, preload: bool = False) -> AsrEngine | None:
        with self._asr_lock:
            key = asr.config_key(self.cfg)
            if self._asr is None or self._asr_key != key:
                if self._asr is not None:
                    # 网页/命令行换了模型或线程数：旧引擎不能继续用
                    self._asr.unload()
                    log.get(LOG).info("ASR 配置已变（%s → %s），重建引擎", self._asr_key, key)
                self._asr = asr.make_engine(self.cfg)
                self._asr_key = key
            engine = self._asr
        self._asr_last_used = time.time()
        engine.load()
        self._asr_last_used = time.time()
        return engine

    def _get_ocr(self) -> OcrEngine:
        key = (int(self.cfg["ocr"]["threads"]), str(self.cfg["ocr"]["resident"]))
        with self._ocr_lock:
            if self._ocr is None or self._ocr_key != key:
                if self._ocr is not None:
                    self._ocr.unload()
                    log.get(LOG).info("OCR 配置已变（%s → %s），重建引擎", self._ocr_key, key)
                self._ocr = OcrEngine(threads=key[0], resident=key[1])
                self._ocr_key = key
            return self._ocr

    def _transcribe_and_deliver(self, audio, seconds: float) -> None:
        with self._state_lock:
            self._voice_state = "working"
        try:
            engine = self._get_asr()
            assert engine is not None
            started = time.perf_counter()
            text = engine.transcribe(audio)
        except AsrError as exc:
            raise RuntimeError(f"转写失败：{exc}") from exc
        elapsed = time.perf_counter() - started
        text = text.strip()
        log.get(LOG).info("转写完成：%d 字符，%.1f 秒", len(text), elapsed)
        if not text:
            log.get(LOG).info("没有识别到语音：%g 秒音频，%.1f 秒；可能是噪音", seconds, elapsed)
            return
        self._deliver(text, "语音输入", elapsed, paste=bool(self.cfg["inject"].get("voice_paste", True)))

    def _capture_screen(self) -> Path:
        """抓一张 PNG：打开系统截屏 UI（用户框选），取走它存下的文件。"""
        timeout = float(self.cfg["ocr"].get("timeout_s") or 60)
        trigger = self.cfg["ocr"].get("trigger") or "inject"
        if trigger == "listen":
            log.get(LOG).info("抓屏：等系统截屏窗口（由 GNOME 快捷键触发，最多等 %.0f 秒）", timeout)
        else:
            log.get(LOG).info("抓屏：注入 Print 打开系统截屏窗口（最多等 %.0f 秒）", timeout)
        path = capture.system_capture(timeout=timeout, trigger=trigger)
        log.get(LOG).info("抓屏完成：%s（%d 字节）", path.name, path.stat().st_size)
        return path

    def _ocr_and_deliver(self) -> None:
        path = self._capture_screen()
        try:
            started = time.perf_counter()
            engine = self._get_ocr()
            try:
                text = engine.text(path, self.cfg["ocr"]["layout"]).strip()
            except OcrError as exc:
                raise RuntimeError(str(exc)) from exc
            elapsed = time.perf_counter() - started
            if not text:
                log.get(LOG).info("没有识别到文字")
                return
            self._deliver(text, "OCR", elapsed, paste=bool(self.cfg["inject"].get("ocr_paste", False)))
        finally:
            # 截图只是中间产物：识别完（成功、失败、取消都算）立刻删掉
            capture.discard(path)

    def _deliver(self, text: str, title: str, elapsed: float, *, paste: bool = True) -> None:
        # 两个模型线程可能同时出结果：写剪贴板 + 发粘贴键必须整段串行，否则会互相撕字
        with self._deliver_lock:
            try:
                result = inject.deliver(text, self.cfg, paste_enabled=paste)
            except (inject.InjectUnavailable, inject.InjectError, ClipboardError) as exc:
                log.get(LOG).error("%s：写入失败：%s", title, exc)
                return
            where = "已粘贴" if result.mode == "uinput" else "已复制"
            self._last_action = f"{title}:{result.mode}"
        log.get(LOG).info(
            "%s完成：%s（%s，%d 字符，%.1f 秒）",
            title,
            text.replace("\n", " ")[:80],
            where,
            len(text),
            elapsed,
        )

    # ------------------------------------------------------------------ 维护

    def _maintain(self) -> None:
        logger = log.get(LOG)
        while not self.stop_event.wait(0.5):
            now = time.time()
            resident = RESIDENT_SECONDS.get(self.cfg["asr"]["resident"])
            with self._asr_lock:
                asr_engine = self._asr
            if (
                asr_engine is not None
                and resident is not None
                and asr_engine.loaded
                and not self._asr_worker.busy
                and now - self._asr_last_used > resident
            ):
                self._asr_worker.submit("卸载语音模型", self._unload_asr)
            with self._ocr_lock:
                ocr_engine = self._ocr
            if ocr_engine is not None and not self._ocr_worker.busy and ocr_engine.maybe_unload(now):
                logger.info("OCR 已按 resident=%s 卸载", self.cfg["ocr"]["resident"])

    def _unload_asr(self) -> None:
        with self._asr_lock:
            if self._asr is not None:
                self._asr.unload()
                self._asr = None
                self._asr_key = None
                log.get(LOG).info("ASR 已按 resident=%s 卸载", self.cfg["asr"]["resident"])

    # ------------------------------------------------------------------ 重建 / 真实状态

    def _rebuild_asr(self) -> None:
        """丢掉内存里的引擎，按当前配置重建并预加载；失败由 _guard 记 last_error。"""
        with self._asr_lock:
            if self._asr is not None:
                self._asr.unload()
            self._asr = None
            self._asr_key = None
        self._get_asr()
        log.get(LOG).info("ASR 已重建：%s", self._asr_key[0] if self._asr_key else "?")

    def _rebuild_ocr(self) -> None:
        """丢掉 OCR 引擎；下一次抓屏识别时按当前配置懒加载。"""
        with self._ocr_lock:
            if self._ocr is not None:
                self._ocr.unload()
            self._ocr = None
            self._ocr_key = None

    def rebuild_models(self) -> dict[str, Any]:
        """网页「重启模型」：按当前配置重建 ASR / OCR 引擎。

        引擎只在自己的模型线程里加载/卸载，所以这里只投递任务，不等结果；
        页面轮询 `engine_state()` 看真实引擎是否换好。
        """
        started: list[str] = []
        blocked: list[str] = []
        for name, worker, work in (
            ("语音", self._asr_worker, self._rebuild_asr),
            ("OCR", self._ocr_worker, self._rebuild_ocr),
        ):
            if worker.submit("重建模型", self._guard("重建模型", work)):
                started.append(name)
            else:
                blocked.append(worker.reason() or name)
        if not started:
            return {"ok": False, "error": "；".join(blocked) + "，稍后再试"}
        message = "正在重建" + "+".join(started) + "模型…"
        if blocked:
            message += "；" + "；".join(blocked) + "，未重建"
        return {"ok": True, "message": message}

    def engine_state(self) -> dict[str, Any]:
        """内存里真实在跑的引擎（不是配置里想要的）。"""
        with self._asr_lock:
            asr_engine = self._asr
            asr_key = self._asr_key
            asr_loaded = bool(asr_engine and asr_engine.loaded)
            asr_pending = asr_engine is not None and asr_key != asr.config_key(self.cfg)
            asr_load_ms = asr_engine.load_ms if asr_engine else None
        with self._ocr_lock:
            ocr_engine = self._ocr
            ocr_loaded = bool(ocr_engine and ocr_engine.loaded)
            ocr_threads = ocr_engine.threads if ocr_engine else None
        return {
            "asr": {
                "model": asr_key[0] if asr_key else None,
                "loaded": asr_loaded,
                "load_ms": asr_load_ms,
                "pending": asr_pending,
                "busy": self._asr_worker.busy,
            },
            "ocr": {
                "loaded": ocr_loaded,
                "threads": ocr_threads,
                "busy": self._ocr_worker.busy,
            },
            "last_error": self._last_error,
        }

    def _matcher_summary(self) -> dict[str, Any]:
        return {
            "voice_key": self._matcher.voice_key,
            "ocr_key": "+".join(sorted(self._matcher.ocr_mods)) + "+" + self._matcher.ocr_main
            if self._matcher.ocr_mods
            else self._matcher.ocr_main,
            "recording": self._matcher.recording,
            "pending": self._matcher.pending,
        }

    def status(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self.started_at, 1),
            "session": env.session_type(),
            "desktop": env.desktop(),
            "voice_state": self._voice_state,
            "busy": self.busy,
            "busy_task": self.busy_task,
            "busy_reason": self.busy_reason(),
            "asr_busy": self._asr_worker.busy,
            "asr_task": self._asr_worker.task,
            "ocr_busy": self._ocr_worker.busy,
            "ocr_task": self._ocr_worker.task,
            "last_action": self._last_action,
            "last_error": self._last_error,
            "hotkey_mode": self.cfg["hotkey"]["mode"],
            "hotkey": self._matcher_summary(),
            "devices": self._live_devices(),
            "device_notes": self.device_notes,
            "asr": {
                "model": self.cfg["asr"].get("model"),
                "engine_model": self._asr_key[0] if self._asr_key else None,  # 内存里真正的
                "loaded": bool(self._asr and self._asr.loaded),
                "busy": self._asr_worker.busy,
                "resident": self.cfg["asr"]["resident"],
            },
            "ocr": {
                "loaded": bool(self._ocr and self._ocr.loaded),
                "busy": self._ocr_worker.busy,
                "resident": self.cfg["ocr"]["resident"],
            },
            "web": None
            if self._web_server is None
            else {
                "host": self.cfg["web"]["host"],
                "port": getattr(self._web_server, "server_port", self.cfg["web"]["port"]),
            },
        }
