"""模型线程：两个模型各跑各的，同一个模型内部串行。"""

from __future__ import annotations

import time

from flash_input import runtime


def _wait_idle(worker, timeout=2.0):
    deadline = time.monotonic() + timeout
    while worker.busy and time.monotonic() < deadline:
        time.sleep(0.02)
    return not worker.busy


def test_two_workers_run_in_parallel():
    """ASR 与 OCR 各自一条线程：一个在跑不会挡住另一个。"""
    asr = runtime.ModelWorker("t-asr")
    ocr = runtime.ModelWorker("t-ocr")
    try:
        assert asr.submit("语音转写", lambda: time.sleep(0.3)) is True
        assert ocr.submit("抓屏 OCR", lambda: time.sleep(0.3)) is True
        time.sleep(0.05)
        assert asr.busy and ocr.busy
        assert _wait_idle(asr) and _wait_idle(ocr)
    finally:
        asr.stop(wait=True)
        ocr.stop(wait=True)


def test_same_worker_serialises_and_rejects_second_task():
    worker = runtime.ModelWorker("t-one")
    order: list[str] = []
    try:
        assert worker.submit("第一个", lambda: (time.sleep(0.2), order.append("a"))) is True
        time.sleep(0.05)
        # 不排队：模型线程忙时直接拒绝，避免热键连按攒任务
        assert worker.submit("第二个", lambda: order.append("b")) is False
        assert "第一个" in worker.reason()
        assert _wait_idle(worker)
        assert order == ["a"]
        # 空下来以后可以接新任务，而且任务问不会互相穿插
        assert worker.submit("第三个", lambda: order.append("c")) is True
        assert _wait_idle(worker)
        assert order == ["a", "c"]
    finally:
        worker.stop(wait=True)


def test_worker_survives_failing_task_and_clears_busy():
    worker = runtime.ModelWorker("t-fail")

    def boom():
        raise RuntimeError("炸了")

    try:
        assert worker.submit("会炸的任务", boom) is True
        assert _wait_idle(worker)
        assert worker.reason() == ""
        assert worker.submit("后续任务", lambda: None) is True
        assert _wait_idle(worker)
    finally:
        worker.stop(wait=True)


def test_reason_reports_task_and_elapsed():
    worker = runtime.ModelWorker("t-reason")
    try:
        assert worker.reason() == ""
        assert worker.submit("抓屏 OCR", lambda: time.sleep(0.3)) is True
        time.sleep(0.1)
        reason = worker.reason()
        assert "抓屏 OCR" in reason and "秒" in reason and "正在" in reason
    finally:
        worker.stop(wait=True)
