"""模型线程：OCR 与 ASR 各用一条专属线程，互不阻塞。

为什么不是「一条公共工作线程 + 全局 busy 锁」：ASR 转写要 2–3 秒，OCR 要 0.7–2 秒，
共用一条线程时后到的那个连热键都按不动（只会得到「忙」）。而两个模型本来就是彼此独立的
ONNX session、独立的线程池（`asr.threads` / `ocr.threads`），没有共享的可变状态，
因此各自排队即可并发执行；同一个模型内部仍然严格串行 —— load / infer / unload 都在
它自己那条线程里排队，不会出现「加载到一半被维护线程卸载」这类竞态。

两条线程都只做三件事：加载、推理、卸载。跨模型的共享资源只有剪贴板与 uinput 粘贴键，
那一步由 `Daemon._deliver()` 用一把短锁串行化（毫秒级），不影响推理并行。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

LOG = "flash-input"


class ModelWorker:
    """单个模型专属的工作线程：一次只跑一个任务，后到的直接拒绝。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self._queue: queue.Queue[tuple[str, Callable[[], Any]] | None] = queue.Queue()
        self._busy = threading.Event()
        self._closed = threading.Event()
        self._state_lock = threading.Lock()
        self._task = ""
        self._since = 0.0
        self._thread = threading.Thread(
            target=self._loop,
            name=f"flash-input-{name}",
            daemon=True,
        )
        self._thread.start()

    # ---------------------------------------------------------------- 供调用方

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    @property
    def task(self) -> str:
        with self._state_lock:
            return self._task

    def reason(self) -> str:
        """忙的时候说清楚在忙什么、忙了多久，而不是只弹一句「忙」。"""
        if not self._busy.is_set():
            return ""
        with self._state_lock:
            task = self._task or "上一个任务"
            since = self._since
        elapsed = time.time() - since if since else 0.0
        return f"正在{task}（{elapsed:.0f} 秒）"

    def submit(self, label: str, work: Callable[[], Any]) -> bool:
        """排一个任务；已有任务在跑就返回 False（不排队，避免热键连按攒一堆）。"""
        with self._state_lock:
            if self._busy.is_set():
                return False
            self._busy.set()
            self._task = label
            self._since = time.time()
        self._queue.put((label, work))
        return True

    def stop(self, *, wait: bool = False, timeout: float = 5.0) -> None:
        self._closed.set()
        self._queue.put(None)
        if wait:
            self._thread.join(timeout)

    # ---------------------------------------------------------------- 线程主体

    def _loop(self) -> None:
        while not self._closed.is_set():
            item = self._queue.get()
            if item is None:
                break
            label, work = item
            try:
                work()
            except Exception:
                logging.getLogger(LOG).exception("%s失败", label)
            finally:
                with self._state_lock:
                    self._task = ""
                    self._since = 0.0
                self._busy.clear()
