"""daemon 与 CLI 之间的本地 IPC：`$XDG_RUNTIME_DIR/flash-input.sock` 上的一问一答。

协议刻意做小：每个连接发一行 JSON、收一行 JSON，然后关闭。这样命令行不用长连接，
daemon 也不用处理粘包；单飞保护在 daemon 里，不靠连接数。

请求：`{"cmd": "ping" | "status" | "voice" | "ocr" | "shutdown", ...}`
响应：`{"ok": bool, "busy": bool, "msg": str, "data": {...}}`
"""

from __future__ import annotations

import contextlib
import json
import os
import socketserver
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import xdg

MAX_BYTES = 64 * 1024


class IpcError(RuntimeError):
    pass


def socket_path(runtime_dir: str | os.PathLike | None = None) -> Path:
    if runtime_dir is None:
        return xdg.socket_path()
    return Path(runtime_dir) / "flash-input.sock"


def request(
    payload: dict[str, Any], timeout: float = 3.0, runtime_dir: str | os.PathLike | None = None
) -> dict[str, Any] | None:
    """发一条请求。daemon 不在（socket 不存在 / 连接被拒）时返回 None，不抛异常。"""
    import socket

    path = socket_path(runtime_dir)
    if not path.exists():
        return None
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(path))
        conn.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_BYTES:
                raise IpcError("响应过大")
    except (ConnectionRefusedError, FileNotFoundError):
        return None
    except (OSError, IpcError):
        return None
    finally:
        conn.close()
    line = buf.split(b"\n", 1)[0].strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_BYTES)
        if not raw:
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            reply = {"ok": False, "busy": False, "msg": "请求不是合法 JSON"}
        else:
            handler: Callable[[dict], dict] = self.server.handler  # type: ignore[attr-defined]
            try:
                reply = handler(payload)
            except Exception as exc:
                reply = {"ok": False, "busy": False, "msg": f"{type(exc).__name__}: {exc}"}
        self.wfile.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: Path, handler: Callable[[dict], dict]) -> None:
        self.path = Path(path)
        self.handler = handler  # type: ignore[assignment]
        super().__init__(str(self.path), _Handler)

    def server_close(self) -> None:
        super().server_close()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


def make_server(handler: Callable[[dict], dict], runtime_dir: str | os.PathLike | None = None) -> Server:
    """绑定 socket。已有实例时抛 `IpcError`；残留的陈旧 socket 会被清掉。"""
    path = socket_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if request({"cmd": "ping"}, timeout=0.5, runtime_dir=runtime_dir) is not None:
            raise IpcError(f"已有实例在运行（{path}）")
        # 上一个进程崩溃留下的
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    server = Server(path, handler)
    os.chmod(path, 0o600)
    return server


def serve_in_thread(server: Server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="flash-input-ipc", daemon=True)
    thread.start()
    return thread
