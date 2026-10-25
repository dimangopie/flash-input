"""录音：16 kHz 单声道 float32。

只用 `sounddevice`（wheel 自带 PortAudio，系统只需要 `libasound`），
不落临时文件、不调 ffmpeg/arecord。两条路径：

    record(seconds)        —— `finput voice --seconds 5`，固定时长
    record_hold(stop)      —— 按住说话，松手停（`stop()` 由热键层调）
    LiveRecorder           —— daemon 用的非阻塞 start/stop 形态

`min_seconds` 以下视为误触返回空数组；`max_seconds` 兜底防止一直录。
"""

from __future__ import annotations

import sys
import time

import numpy as np

SAMPLE_RATE = 16000


class RecorderError(RuntimeError):
    """没有可用的录音设备或 PortAudio 打不开流。"""


class Recorder:
    def __init__(self, samplerate: int = SAMPLE_RATE, channels: int = 1, device: int | None = None):
        self.samplerate = samplerate
        self.channels = channels
        self.device = device

    # ------------------------------------------------------------------ 底层

    def _stream(self):
        try:
            import sounddevice as sd
        except Exception as exc:
            raise RecorderError(
                "缺少 sounddevice / PortAudio：装 `libasound2` 后重装 flash-input，"
                f"或在设置页确认麦克风可用（{exc}）"
            ) from exc

        blocks: list[np.ndarray] = []

        def callback(indata, frames, _time, status):
            if status:  # 溢出/欠载不致命，记一句就继续
                print(f"录音提示：{status}", file=sys.stderr, flush=True)
            blocks.append(indata[:, 0].copy() if indata.ndim > 1 else indata.copy())

        try:
            stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="float32",
                blocksize=1600,  # 100 ms
                device=self.device,
                callback=callback,
            )
        except Exception as exc:
            raise RecorderError(f"打不开麦克风：{exc}") from exc
        return stream, blocks

    @staticmethod
    def _concat(blocks: list[np.ndarray]) -> np.ndarray:
        return np.concatenate(blocks).astype(np.float32) if blocks else np.zeros(0, dtype=np.float32)

    # ------------------------------------------------------------------ 两条路径

    def record(self, seconds: float) -> np.ndarray:
        """录固定时长。"""
        if seconds <= 0:
            raise RecorderError("录音时长必须 > 0")
        stream, blocks = self._stream()
        with stream:
            stream.start()
            time.sleep(seconds)
        audio = self._concat(blocks)
        return audio[: int(seconds * self.samplerate)]

    def record_hold(
        self,
        stop,
        *,
        max_seconds: float = 60.0,
        min_seconds: float = 0.3,
        on_start=None,
    ) -> np.ndarray:
        """一直录到 `stop()` 返回 True（热键松手）或 `max_seconds`。

        `on_start` 在流真正打开后调用一次（daemon 用它做「开始说话」提示音）。
        """
        stream, blocks = self._stream()
        started = time.monotonic()
        with stream:
            stream.start()
            if on_start is not None:
                on_start()
            while not stop():
                if time.monotonic() - started >= max_seconds:
                    break
                time.sleep(0.05)
        audio = self._concat(blocks)
        if audio.size < int(min_seconds * self.samplerate):
            return np.zeros(0, dtype=np.float32)  # 误触
        return audio[: int(max_seconds * self.samplerate)]


class LiveRecorder:
    """常驻形态：`start()` 开流、`stop()` 收尾。daemon 收到「按住/松开」时用它。

    与 `record_hold` 的区别是不阻塞：daemon 的 evdev 线程不能卡在录音循环里。
    最长录音由调用方（热键状态机）保证；这里再兜一层 `max_seconds`。
    """

    def __init__(
        self,
        samplerate: int = SAMPLE_RATE,
        channels: int = 1,
        device: int | None = None,
        max_seconds: float = 60.0,
    ) -> None:
        self.max_seconds = float(max_seconds)
        self._recorder = Recorder(samplerate=samplerate, channels=channels, device=device)
        self._samplerate = samplerate
        self._stream = None
        self._blocks: list[np.ndarray] = []
        self._started_at = 0.0

    @property
    def active(self) -> bool:
        return self._stream is not None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at if self.active else 0.0

    def start(self) -> bool:
        if self.active:
            return False
        stream, blocks = self._recorder._stream()
        try:
            stream.start()
        except Exception as exc:
            stream.close()
            raise RecorderError(f"麦克风启动失败：{exc}") from exc
        self._stream = stream
        self._blocks = blocks
        self._started_at = time.monotonic()
        return True

    def stop(self, *, min_seconds: float = 0.3) -> np.ndarray:
        stream, blocks = self._stream, self._blocks
        self._stream, self._blocks = None, []
        if stream is None:
            return np.zeros(0, dtype=np.float32)
        try:
            stream.stop()
        finally:
            stream.close()
        audio = Recorder._concat(blocks)
        if audio.size < int(min_seconds * self._samplerate):
            return np.zeros(0, dtype=np.float32)
        return audio[: int(self.max_seconds * self._samplerate)]

    def cancel(self) -> float:
        """丢弃当前录音，返回已录秒数。"""
        elapsed = self.elapsed
        self.stop(min_seconds=0)
        return elapsed


def trim_trailing_silence(
    audio: np.ndarray,
    *,
    samplerate: int = SAMPLE_RATE,
    window: float = 0.1,
    threshold: float = 0.008,
    keep: float = 0.3,
) -> np.ndarray:
    """去掉结尾的静音（保留 `keep` 秒尾巴），避免把「松手后的空白」也送进模型。

    只看尾部：开头的静音不影响识别，还会让 ASR 有一点上下文。
    """
    if audio.size == 0:
        return audio
    step = max(1, int(window * samplerate))
    end = audio.size
    floor = int(keep * samplerate)
    while end > floor:
        chunk = audio[max(floor, end - step) : end]
        if float(np.sqrt(np.mean(np.square(chunk)))) >= threshold:
            break
        end = max(floor, end - step)
    return audio[:end]


def duration(audio: np.ndarray, *, samplerate: int = SAMPLE_RATE) -> float:
    return round(audio.size / samplerate, 2)
