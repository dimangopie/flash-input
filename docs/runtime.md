# Flash Input 运行流程

守护进程 `finput run` 是唯一常驻的进程，所有入口（热键、命令行、设置页）最终都汇到它的
`handle()` 上。本文按「谁在什么时候做什么」画成图，尺寸、时序都对应
`src/flash_input/service.py` / `daemon.py` / `hotkey.py` / `inject.py` 里的实现。

## 1. 开机：systemd 拉起守护进程

```mermaid
flowchart TD
    A["开机 / 登录"] --> B["graphical-session.target<br/>（由 GNOME / KDE 会话拉起）"]
    B --> C["flash-input.service<br/>WantedBy=graphical-session.target<br/>After=graphical-session.target"]
    C --> D["ExecStart=…/finput run<br/>Type=simple · Restart=on-failure · RestartSec=2"]
    D --> E["Daemon.start()"]

    E --> F["日志初始化<br/>有 INVOCATION_ID → stderr 进 journald"]
    F --> G{"就绪等待（最多 30 s，指数退避）"}
    G -->|"$XDG_RUNTIME_DIR/bus"| G1["会话总线"]
    G -->|"wayland socket / DISPLAY"| G2["图形会话"]
    G1 --> H
    G2 --> H
    G -->|"等不到也不退出"| H["继续启动<br/>（热键与 IPC 不依赖它们）"]

    H --> I["ipc.make_server()<br/>$XDG_RUNTIME_DIR/flash-input/flash-input.sock<br/>已有实例 → IpcError 退出"]
    I --> J["webui.serve_background()<br/>http://127.0.0.1:7331/（可关）"]
    J --> K["_start_hotkey()<br/>枚举 /dev/input/event* → Listener 线程"]
    K --> L["维护线程（0.5 s 周期）"]
    L --> M["run_forever()：等 SIGTERM / SIGINT / shutdown"]
```

关键点：

- unit 里的 `After=` 只是「优选」：真正保证可用的是 `start()` 里的**有界就绪等待**，
  所以合成器没正确拉起 target 时也不会死循环重启。
- socket 在建好之后才接热键，客户端看到「socket 存在」就等于「能收命令」。
- `Restart=on-failure` + `StartLimitBurst=5`：崩了 2 s 回来，反复崩 5 次后停下，不会刷屏。

## 2. 语音：按住说话

```mermaid
sequenceDiagram
    autonumber
    participant K as 键盘设备
    participant M as hotkey.Matcher
    participant D as Daemon
    participant C as capture
    participant G as GNOME 截屏对话框
    participant O as OcrEngine
    participant I as inject.deliver

    K->>M: Super 按下 → Shift 按下 → O 按下
    M-->>M: 修饰键全部按住 + 主键命中<br/>_ocr_latch 防长按连发，400 ms 去抖
    M->>D: ocr
    D->>C: system_capture(timeout, trigger)
    alt trigger = inject（默认）
        C->>G: inject.tap_key(KEY_SYSRQ) 注入 Print
    else trigger = listen
        Note over G: 用户按 GNOME 快捷键<br/>（设置页切到 listen 保存时自动改绑 show-screenshot-ui）
    end
    G-->>用户: 系统截屏窗口（选框 / 整屏 / 窗口）
    用户->>G: Take Screenshot
    G->>C: ~/Pictures/Screenshots/*.png 落盘
    C->>C: inotify/rglob 发现新图 + _complete_png 等写完<br/>复制到 $XDG_RUNTIME_DIR/flash-input/shot-<pid>.png，删原件
    C-->>D: Path
    D->>O: recognize(path, layout)
    O-->>D: text（行合并后的多行文本）
    D->>I: deliver(text)
    I-->>D: DeliverResult
    D->>C: discard(path)（finally 里必删副本）
```

抓屏没有第二条通道：没有 `xdg-desktop-portal`、没有 `grim`/`maim`/`import`。窗口打不开就等到
`ocr.timeout_s`（默认 60 s）超时，随后这次抓屏失败、单飞锁释放。

## 4. 文字落地：剪贴板 + uinput 粘贴

```mermaid
flowchart TD
    A["deliver(text, cfg)"] --> B["先写剪贴板<br/>Wayland: wl-copy · Xorg: xclip"]
    B --> C{"会话来锁屏？<br/>loginctl LockedHint"}
    C -->|"是"| Z["只复制，不注入<br/>note=已锁屏"]
    C -->|否| D{"inject.mode"}
    D -->|"clipboard"| Z
    D -->|"uinput"| E
    D -->|"auto"| F{"/dev/uinput 可写<br/>且 paste_key≠none"}
    F -->|否| Z
    F -->|是| E["自建 uinput 虚拟键盘<br/>发 ctrl+v / ctrl+shift+v<br/>发完立即销毁"]
    E --> G{"发键成功？"}
    G -->|是| Y["note=已粘贴到当前窗口"]
    G -->|"否（auto）"| Z
    G -->|"否（强制 uinput）"| X["抛 InjectUnavailable<br/>CLI 退出码 5"]
    Z --> W["只写剪贴板，日志记一行"]
```

要点：

- 中文**永远走剪贴板**：uinput 只发粘贴组合键，不逐字打码点（否则要过 IME，必坏）。
- 每次粘贴临时 `UI_DEV_CREATE` → 发键 → `UI_DEV_DESTROY`，不长期占一个虚拟键盘。
- `EV_SYN` 不能显式 `UI_SET_EVBIT`（内核返 `EINVAL`），这是踩过的坑。

## 5. 命令行 / 设置页入口

```mermaid
flowchart LR
    subgraph CLI["finput 子命令（只管配置与运维）"]
        S["status"] --> Q1["ipc.request({cmd:status})"]
        R["service / stop / config / models"] --> Q5["ipc.request({cmd:reload})"]
    end
    subgraph WEB["Web 设置页 :7331"]
        W1["GET /api/config · POST /api/config"] --> F["config.load/save（原子写）"]
        W2["GET /api/status"] --> Q4["daemon.status()"]
    end
    Q1 --> H["Daemon.handle(request)<br/>单行 JSON 请求/响应"]
    Q3 --> H
    Q4 --> H
    Q5 --> H
    H --> R["reply：ok / busy / msg / state"]
    R -.->|"daemon 不在"| FALLBACK["socket 不存在 → ipc.request 返回 None<br/>CLI 报错：语音/抓屏都需要常驻进程；ocr --image 仍可用"]
```

`ocr --image` 这条带显式输入的路径**不需要 daemon**（只加载模型），直接在 CLI 进程里跑完
（用的还是同一套 ocr/inject 代码）。语音与抓屏没有 CLI 入口：它们由热键直接驱动 daemon，
daemon 不在就没有监听者。

## 6. 常驻与内存回收

```mermaid
stateDiagram-v2
    [*] --> 未加载
    未加载 --> 加载中: 热键 / CLI 用到 / preload_on_record
    加载中 --> 已加载: load() 完成（ASR 约 4 s，1.3 GB）
    已加载 --> 使用中: transcribe / recognize
    使用中 --> 已加载: 记 last_used
    已加载 --> 未加载: resident=none 立即卸载，或维护线程发现超时
    已加载 --> 未加载: 守护进程退出（systemd 再拉起）
```

| 模型 | `resident` 取值 | 行为 |
|---|---|---|
| ASR | `none` / `10min` / `30min` / `forever` | 默认 `10min`；超时后 `unload()`，RSS 回落到 ~1.3 GB |
| OCR | 同上 | 默认 `30min`；加载后约 170 MB |

维护线程每 0.5 s 跑一次，`busy` 时不卸载，避免把正在推理的模型抽走。

## 7. 异常与退出码

```mermaid
flowchart TD
    A["进程退出"] --> B{"退出码"}
    B -->|"0"| C["成功（含「只复制没粘贴」的降级）"]
    B -->|"1"| D["一般失败（参数/未知命令/内部错误）"]
    B -->|"2"| E["环境不满足：没剪贴板命令 / 没有可读键盘设备 / /dev/uinput 不可写"]
    B -->|"3"| F["配置错误（JSON 坏、取值越界、键名不合法）"]
    B -->|"4"| G["运行期失败：抓屏超时、转写失败、IPC 超时"]
    B -->|"5"| H["强制 inject.mode=uinput 但 /dev/uinput 不可用"]
```

守护进程侧：`Restart=on-failure` 只处理**非 0 退出**；正常 `systemctl --user stop`
是 `SIGTERM`，`run_forever()` 收到后走 `shutdown()`，退出码 0，不会触发重启。

## 相关文件

| 文件 | 内容 |
|---|---|
| `src/flash_input/service.py` | unit 模板、`install/uninstall/status/logs` |
| `src/flash_input/daemon.py` | 就绪等待、IPC、单飞、常驻维护 |
| `src/flash_input/hotkey.py` | evdev 读取、组合匹配、守卫 |
| `src/flash_input/inject.py` | uinput 粘贴与降级 |
| `src/flash_input/ipc.py` | unix socket 单行 JSON 协议 |
