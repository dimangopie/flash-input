# Flash Input（`finput`）设计大纲

> 把 `daobidao`（按住热键说话 → 本地 ASR → 文字进剪贴板）和 `shotocr`（抓屏 → 本地 OCR → 文字进剪贴板）
> 合成一个 **Python 写的 uv tool**：两个热键，ONNX 本地推理，Xorg / Wayland 同一套代码。
>
> 产品名 Flash Input · 发行包 `flash-input` · 模块 `flash_input` · 命令 `finput`。
> 本文件描述**当前实现**（M0–M5 已完成），不含历史演进；运行流程图见 [`docs/runtime.md`](docs/runtime.md)。

---

## 1. 范围

### 1.1 两个动作

| 动作 | 触发 | 做什么 |
|---|---|---|
| **语音输入** | **按住**右 Ctrl（`KEY_RIGHTCTRL`）说话，松开结束 | 录音 16 kHz 单声道 → Qwen3-ASR 0.6B int8 转写 → 剪贴板 → 粘贴 |
| **抓屏 OCR** | **按下** `Super+Shift+O` | 弹出**系统截屏 UI** → 用户框选 → 取图 → RapidOCR → 剪贴板 → 粘贴 |

两个动作共用一套 evdev 监听器、一套「文字落地」逻辑（剪贴板 + 可选粘贴）。
语音与抓屏都**只由热键驱动**：不提供 `finput ocr` / `finput capture` 这类命令行识别入口，
因为同一时间模型与剪贴板只够服务一件事，命令行识别会正好挡住用户按热键的输入。

### 1.2 明确不做

| 不做 | 为什么 |
|---|---|
| X11 专用热键（`XGrabKey` / xbindkeys / sxhkd） | Wayland 下不生效；evdev 一套代码覆盖两种会话 |
| `GlobalShortcuts` portal | 语义最合适（有 `Deactivated`），但要 GNOME 48+，本机 46 没有；evdev 已覆盖 |
| 用 uinput 直接打中文 | 必须过输入法（IME），uinput 发的是键码，中文会变成无意义的键序列 |
| 自绘屏幕选区覆盖层 | 系统截屏 UI 本身就是选区窗口；Wayland 覆层不能自己定位，还会拖 GTK 依赖 |
| 外部抓屏命令（grim / slurp / maim / import） | 系统 UI 已含选区/整屏/窗口三种模式；外部命令每个发行版参数都不同，`import` 在 XWayland 还抓黑图 |
| 用 portal 写剪贴板（`org.freedesktop.portal.Clipboard`） | 不自己开会话，只能挂在 RemoteDesktop 会话上 → 弹「允许远程控制」、顶栏出现共享指示，比 fork 一次 `wl-copy` 复杂得多 |
| 用 Tkinter 当剪贴板后端 | 实测可行但**不减依赖**（`python3-tk` 要单独装、只有 X11 后端、要常驻 Tk 线程），等于拿 `wl-clipboard` 换 `python3-tk` |
| 流式 ASR | 单句离线足够；流式要处理回滚、分段拼接，收益抵不上兼容成本 |
| 读剪贴板图片做 OCR | 截图由自己触发，不需要猜用户剪贴板里有什么 |
| GUI 浮窗 / 托盘图标 / 开机自启用 XDG autostart | 托盘要 GTK；启动顺序与自愈用 systemd 才有（见 §8） |
| i18n / 自动更新 / 遥测 | 单人工具，中文界面足够；更新走 `uv tool upgrade` |

---

## 2. 交互细节

### 2.1 语音：按住说话

```
按下语音键 → (等 guard 窗口) → 开始录音 ──松开──> 截掉尾部静音 → ASR → 剪贴板 → 粘贴
```

三个守卫，避免抢占正常键盘用法：

| 守卫 | 行为 | 目的 |
|---|---|---|
| `guard_ms = 300` | 按住后 300 ms 内出现**其它按键** → 判为「Ctrl 组合键」，取消本次录音 | 右 Ctrl 也是常用修饰键，`Ctrl+C` 不能被当成说话 |
| `min_hold_ms = 300` | 按住不足 300 ms 就松开 → 视为误触，不录 | 键位轻碰不该触发模型 |
| `max_hold_s = 60` | 按住超过 60 s 自动停止 | 防止卡住一直录 |

- `tail_silence_s = 1.2`：松手后把尾部静音截掉，减少无谓 token
- 录音期间其它按键**不打断**（已经在说话，用户可能同时操作鼠标）
- 识别结果为空（没听到内容）→ 日志记一行，不写剪贴板
- 语音与 OCR 互斥：一次只处理一个任务，忙时再触发会得到「正在…（N 秒）」的原因（见 §3.3）

### 2.2 抓屏 OCR：用系统自己的截屏 UI

```
按下抓屏键 → uinput 按 Print → 系统截屏窗口（选 Select Area to Grab 框选，点 Take Screenshot）
           → 新 PNG 出现在 ~/Pictures → 取走并删掉原件
           → RapidOCR（det + rec ONNX）→ 行合并 → 剪贴板 → 粘贴
```

**为什么注入 `Print`，而不是调 portal 的 `interactive=true`**：GNOME 46 的会话后端对
`Screenshot(interactive=true)` 一律回 `response=2`（Shell 里 `screenshotUI.open()` 抓帧失败后立刻关窗），
选区窗口根本起不来；但**同一个系统截屏窗口，按 `Print` 打开是好的**。换个入口即可，且窗口、选区、
多屏、HiDPI、缩放全部由系统实现 —— 我们不自绘、不调外部命令、不依赖 `xdg-desktop-portal`。

取图规则：

- 只认 `$XDG_PICTURES_DIR` 或 `~/Pictures` 下**本次触发之后新出现的 `*.png`**；用户原有截图一概不动
- 取到后**立刻删掉 `~/Pictures` 里的原件**，复制一份到 `$XDG_RUNTIME_DIR/flash-input/shot-<pid>.png`
  交给 OCR；OCR 结束后（成功 / 失败 / 没识别到字 / 取消）在 `finally` 里删掉这份副本
- 取消交给用户：在系统窗口里按 `Esc` 或点 Cancel 即可（没有「再按一次热键取消」这类机制）；`ocr.timeout_s`（默认 60 s）内没等到新图报超时。
- 取图：GNOME 46 落在 `~/Pictures/Screenshots/`，所以对 `~/Pictures` 递归找、取最新一张；只认这次触发之后出现、且大小稳定、PIL 能完整解码的 PNG（避免读到半张图）。
  工具不提供「再按一次热键取消」之类脚本化开关 —— 抓屏进行中再按热键只会得到「忙」
- 需要 `/dev/uinput` 可写（与粘贴共用同一条 udev 规则）；不可写时直接报错并说明原因
- 谁负责按 `Print` 由 `ocr.trigger` 决定：
  - `inject`（默认）：本程序注入 `Print` 打开系统窗口，抓屏依赖 `/dev/uinput`；
  - `listen`：改用 GNOME 自己的快捷键绑定。在设置页把「谁打开截屏窗口」切到 `listen` 并保存时，
    程序自动把 `hotkey.ocr_key` **追加**进 `org.gnome.shell.keybindings show-screenshot-ui`
    （「显示截屏界面」）的绑定列表 —— 默认的 `Print` 保留，两个键都能开系统截屏界面；daemon 重读配置后
    **只监听文件、不注入**，抓屏不再依赖 uinput（粘贴仍需要）。切回 `inject` 保存时会还原成 `['Print']`。
    选 `show-screenshot-ui` 而不是 `screenshot`：后者是旧的「直接截屏」动作，绑它等于把用户的
    `<Shift>Print` 抢走；前者本来就是 `Print`，追加本程序热键后 `Print` 与 `<Super><Shift>o` 并存。
    旧版曾绑过 `screenshot`，改绑时检测到残留会自动把它还原成 `<Shift>Print`。
    注意 evdev 监听在内核层，所以即使按键被 GNOME 消费，程序照样能收到原始按键。
- 等待新图用 **inotify**（`ctypes` 调 libc，无额外依赖）：监听 `~/Pictures` 与 `~/Pictures/Screenshots`，
  新建子目录会自动补 watch；inotify 不可用时退回 0.1 s 轮询。
- 设备热插拔：监听线程每 0.5 s 重扫 `/dev/input/event*`，新插的键盘（含 uinput 虚拟键盘）立刻接上；
  设备消失时自动摘掉。

**没有「授权 token」这一层**：抓屏就是「按一次键 → 系统窗口 → 你框选、点 Take Screenshot → 程序取图」，
不加额外的授权记录、不锁设置页、也不记忆「上次已授权」。理由：那个 token 从一开始就不能跳过系统窗口，
它只额外制造了「设置为什么会是灰的」这种困惑；抓屏是否发生，本来就由用户那一下点击决定。

- **生命周期**：改绑写的是 GNOME 的用户设置，**停止运行或卸载程序都不会自动还原**。
  `finput stop` 与 `finput service uninstall` 会打印自查命令
  （`gsettings list-recursively org.gnome.shell.keybindings | grep show-screenshot-ui`）；
  想还原就在设置页把「谁打开截屏窗口」改回「注入 Print」并保存。KDE 用 Spectacle，不碰桌面按键。

### 2.3 文字落地

顺序：**剪贴板 → （可选）粘贴组合键**。

| 情况 | 行为 |
|---|---|
| `inject.mode = auto`（默认） | uinput 可用就发粘贴键；不可用 / 锁屏 / `paste_key=none` → 只写剪贴板，日志里说明 |
| `inject.mode = uinput` | 强制发键；不可用则命令报错（退出码 5） |
| `inject.mode = clipboard` | 只写剪贴板 |
| 语音 / OCR 的自动粘贴 | 两个**独立开关** `inject.voice_paste` / `inject.ocr_paste`，关掉只写剪贴板 |
| 会话锁屏 | 查 `loginctl show-session $XDG_SESSION_ID -p LockedHint`，`yes` 则跳过粘贴（否则会打进锁屏对话框） |

- 两个模型可能同时出结果，所以「写剪贴板 + 发粘贴键」整段用一把短锁串行，不会互相撕字
- 剪贴板后端：Wayland 用 `wl-copy`，Xorg 用 `xclip -selection clipboard`（都 fork 外部命令，
  不用 portal、不用 Tk）
- 粘贴键可选 `shift+insert`（默认）/ `ctrl+v` / `ctrl+shift+v` / `ctrl+alt+v` / `none`；`paste_delay_ms = 60`
  等剪贴板 owner 就绪后再发键
- **没有桌面通知**：一切状态写日志（`journalctl --user -u flash-input -f`），不弹任何气泡

### 2.4 热键

热键在**进程内读 `/dev/input/event*`**（原生 ioctl + `struct`，不引入 python-evdev）：

- 读的是**物理键码**，与键盘布局无关；Xorg / Wayland、GNOME / KDE / wlroots 同一套
- 设备枚举只保留「能同时提供语音键与抓屏键」的键盘；每 2 s 重扫一次，支持热插拔
- `hotkey.mode`：
  - `evdev`（默认）：上面的进程内监听，**需要 daemon**
    （GNOME 自定义快捷键在「设置 → 键盘 → 自定义快捷键」里手动加，不再提供 `shortcut` 子命令）
  - `off`：只留命令行
- 键位在**设置页录制**：浏览器按键 → `event.code` → `KEY_*` 名，前后端共用同一张映射表
  （`keys.KEYMAP`）；语音键必须是单个修饰键，抓屏键必须是「修饰键 + 恰好一个主键」

---

## 3. 架构

一句话版本：

- **输入注入**：走 `/dev/uinput`（内核级虚拟键盘，完全无视 GNOME 还是 KDE）。
- **热键监听**：原生读 `/dev/input/event*`（内核级，不抢桌面快捷键、不依赖辅助进程）。
- **抓屏**：借 GNOME 自己的截屏窗口（注入 `Print` 或用户按 GNOME 绑定），程序只负责收文件 ——
  不用 `xdg-desktop-portal`（GNOME 46 的交互式抓屏必回 `response=2`）。
- **剪贴板**：`wl-copy` / `xclip` 外部命令（Wayland / Xorg 各一条，跨桌面通用）。
- **语音 / OCR 推理**：`onnxruntime` CPU EP + `numpy`，模型本地，不联网。
- **启动方式**：走 systemd user service（`graphical-session.target` + `Restart=on-failure`）。

### 3.0 部件 × 机制 × 依赖

| 部件 | 走什么机制 | 运行时依赖 | 明确不用（原因见 §2/§9） |
|---|---|---|---|
| 文字注入（粘贴） | 建临时 `/dev/uinput` 虚拟键盘 → 发 `Shift+Insert`（可配 `ctrl+v` 等）→ 立即销毁 | Linux `uinput` 模块 + 写权限（udev `uaccess`，或 `input`/`plugdev` 组） | `ydotool`、`wtype`、`xdotool`、XTest、桌面快捷键 |
| 热键监听 | 原生读 `/dev/input/event*`：`struct` 解 24 字节事件 + `ioctl` 取设备名/能力位，进程内状态机做按住/组合/守卫 | 读 `/dev/input` 的权限（udev `uaccess`，或 `input` 组） | `XGrabKey`、libinput、`GlobalShortcuts` portal、`keyd`/`swhkd`、python-evdev |
| 抓屏 | 打开 GNOME 的截屏对话框：默认 `inject.tap_key(KEY_SYSRQ)` 注入 `Print`；`listen` 档由 GNOME 快捷键触发。程序用 inotify（`ctypes` 调 libc）或 0.2 s 轮询看 `~/Pictures` 下的新 PNG | GNOME 的截屏组件 + `show-screenshot-ui` 绑定（切到 `listen` 保存时代绑） | `xdg-desktop-portal`、`grim`/`slurp`、`maim`、`import`、自绘选区浮层 |
| 剪贴板 | fork 外部命令写选区：Wayland 用 `wl-copy`，Xorg 用 `xclip -selection clipboard` | `wl-clipboard` 或 `xclip`（二选一） | `org.freedesktop.portal.Clipboard`、tkinter、GTK/Qt |
| 语音识别 | `onnxruntime` CPU EP + `numpy`：Qwen3-ASR 三段式 / SenseVoice·Paraformer（CTC）/ Zipformer（流式），按 `asr.model` 选 | `onnxruntime`、模型文件（`models fetch` 下载） | 云端 API、PyTorch、`sherpa-onnx` 运行时 |
| 图片 OCR | `onnxruntime` + `rapidocr`，PP-OCRv6 small 三模型随 wheel（31 MB） | 同上 | Tesseract、外部 OCR 服务 |
| 常驻 / 自启 | systemd user service：`After=`/`PartOf=graphical-session.target`、`Restart=on-failure`、日志进 journald | `systemd` + `logind`（`systemctl --user`） | XDG autostart、cron、`nohup` 手动起 |
| 权限 | udev 规则 `TAG+="uaccess"` + `OPTIONS+="static_node=uinput"`，由 logind 动态给当前登录会话 ACL | `udev`、`logind`（`setup-permissions --install` 需 sudo 写一次规则） | 常驻 sudo/root helper、把用户永久加进 `input` 组 |
| 设置页 | 内置 `ThreadingHTTPServer` + 单文件 HTML + 原生 JS，读改 `config.json` | 无（Python 标准库） | 前端框架、Electron、托盘图标、GUI 工具包 |
| 进程间通信 | unix socket：`$XDG_RUNTIME_DIR/flash-input/flash-input.sock`，单行 JSON | 无 | D-Bus 服务、HTTP 端口、临时文件轮询 |
| 日志 | systemd 下 stderr → journald；否则写 `$XDG_STATE_HOME/flash-input/`，失败降级 stderr | journald 或可写状态目录 | 自建日志服务、syslog socket |
| 通知 | **没有**（弹窗已全部删除，结果进日志与剪贴板） | 无 | `org.freedesktop.Notifications`、`notify-send` |

两条硬边界（越界就会引入一整套兼容性分支）：

1. **凡是能由内核或桌面自己完成的，就不自己实现**：注入借 uinput、抓屏借 GNOME 截屏窗口、
   剪贴板借 `wl-copy`/`xclip`、自启借 systemd。
2. **凡是需要「再装一个 daemon / 再开一个会话 / 再要一次授权」的，就不做**：所以没有 portal 剪贴板
   （要挂 RemoteDesktop 会话）、没有 GlobalShortcuts portal（GNOME 48+ 才有）、没有 keyd。

### 3.1 进程与线程

```
finput run（systemd user service 托管，一个进程）
├─ 主线程          启动 → 就绪等待 → 起各线程 → 等 SIGTERM/SIGINT
├─ IPC 线程        unix socket $XDG_RUNTIME_DIR/flash-input/flash-input.sock（单行 JSON）
├─ 热键线程        selectors 轮询 /dev/input/event*，喂 Matcher；每 0.5 s 重扫设备
├─ ASR 模型线程    加载 / 转写 / 卸载，一条线程内严格串行
├─ OCR 模型线程    抓屏 / 识别 / 卸载，一条线程内严格串行
├─ 维护线程        0.5 s 周期：按 resident 策略卸载闲置模型
└─ Web 线程        设置页 http://127.0.0.1:7331（可关）
```

daemon 不在时：语音与抓屏都不可用（热键没有监听者）。

### 3.2 两条主链路

- 语音：`evdev 按下 → guard → LiveRecorder → （预）加载 ASR → 转写 → 剪贴板 → uinput 粘贴 → 卸载(按策略)`
- 抓屏：`evdev 按下 → GNOME 截屏 UI（或注入 Print）→ 取图删原件 → RapidOCR → 行合并 → 剪贴板 → uinput 粘贴 → 删副本`

### 3.3 IPC 与单飞

- 请求：`{"cmd": "status|ocr|reload|shutdown|ping", ...}`；响应：`{"ok": true, ...}`
- `ocr` 抓屏与状态查询由 daemon 执行，CLI 只做参数校验与输出；语音只由热键触发
- **两个模型线程互相独立**：ASR 转写时按抓屏键不会被挡；同一个模型内部不并发
  （load / infer / unload 都排在自己那条线程里，不会出现「加载到一半被卸载」）
- **同一模型不排队**：该模型忙时新任务直接拒绝，回复带原因（「正在抓屏 OCR（12 秒）」）
- **抓屏进行中再按热键**：只回「正在抓屏 OCR（N 秒）」；取消由用户在系统截屏窗口里自己按 Esc
- 启动即拒绝第二实例：socket 已存在且能连上 → `IpcError`

---

## 4. 模块

```
src/flash_input/
├── cli.py          # 命令行入口、子命令实现、退出码
├── daemon.py       # 常驻进程：IPC、热键、单飞任务、维护线程、状态聚合
├── hotkey.py       # /dev/input/event* 原生读取 + Matcher（按住/组合/守卫）
├── ipc.py          # unix socket 服务端与客户端
├── recorder.py     # sounddevice 录音（Recorder / LiveRecorder）、尾部静音截断
├── asr.py          # 引擎分发（qwen3/ctc/zipformer）、常驻管理、WAV 读取
├── asr_ctc.py      # SenseVoice / Paraformer：fbank→LFR→CMVN→CTC 贪心
├── asr_stream.py   # Zipformer 流式：39 帧窗口、32 帧步进、45 个状态张量
├── asr_models.py   # 三种引擎的下载清单（hf-mirror → huggingface）
├── asr_catalog.py  # 模型登记表：内置 + models.json（路径/说明可手改）
├── fbank.py        # Kaldi fbank 80（snip_edges/hamming/high_freq 可调）、LFR、CMVN、CTC
├── features.py     # Whisper 风格 log-mel（128 mel，16 kHz）
├── models.py       # ASR 模型定位（modelscope 缓存）、fetch、体积统计
├── ocr.py          # RapidOCR 封装、行合并（reading / box）
├── capture.py      # 抓屏：uinput 按 Print → 取系统落图 → 删原件
├── clipboard.py    # wl-copy / xclip 写入
├── inject.py       # /dev/uinput 虚拟键盘：粘贴组合键、tap_key
├── config.py       # 默认值、JSON 读写、校验、迁移
├── keys.py         # 浏览器 code ↔ evdev KEY_* 映射与组合校验
├── xdg.py          # 配置/状态/运行时路径
├── env.py          # 会话、发行版、命令探测
├── logging.py      # 日志（systemd 下进 journald，否则轮转文件）
├── privileged.py   # udev 规则检查/安装
├── service.py      # systemd user unit 生成/安装
├── shortcut.py     # GNOME 截屏快捷键读写（切到 trigger=listen 时自动改绑）
├── webui.py        # 单文件设置页（HTTP + 原生 JS）
├── models/         # 随包分发的 OCR 模型（PP-OCRv6 det/rec/cls，31 MB）
└── assets/         # 99-flash-input.rules
```

---

## 5. 配置

- 位置：`~/.config/flash-input/config.json`（`$XDG_CONFIG_HOME`），`--config PATH` 可覆盖
- 格式：JSON；`finput config get/set` 按默认值推断类型；改动经设置页或命令立即生效（daemon 热重载）
- 坏文件只警告并退回默认值，**不覆盖**用户文件

| 键 | 默认 | 说明 |
|---|---|---|
| `hotkey.mode` | `evdev` | `evdev` 进程内监听 / `off` 关闭（语音与抓屏都靠它） |
| `hotkey.voice_key` | `KEY_RIGHTCTRL` | 语音键（必须是单个修饰键） |
| `hotkey.ocr_key` | `KEY_LEFTMETA+KEY_LEFTSHIFT+KEY_O` | 抓屏键（修饰键 + 恰好一个主键） |
| `hotkey.guard_ms` | `300` | 按住后多久内出现其它键就判为组合键 |
| `hotkey.min_hold_ms` | `300` | 最短按住时长，低于它算误触 |
| `hotkey.max_hold_s` | `60` | 最长录音时间 |
| `hotkey.devices` | `auto` | `auto` 或显式设备路径列表 |
| `voice.tail_silence_s` | `1.2` | 尾部静音截断的保留时长 |
| `asr.model` | `qwen3-0.6b` | 当前模型 id（见 §5.1；同一模型的不同规格是不同 id） |
| `asr.models` | `[]` | 旧版的自定义模型登记（已迁到 `models.json`，保留只为兼容旧文件） |
| `asr.threads` | `4` | ONNX 线程数（**必须显式设**，见 §11） |
| `asr.resident` | `forever` | `none` / `10min` / `30min` / `forever`（默认永久常驻：只要 daemon 在就占 ~2.5 GB） |
| `asr.preload_on_record` | `true` | 按下热键就开始加载模型，把加载藏进说话时间 |
| `ocr.layout` | `reading` | `reading` 按视觉行合并 / `box` 每框一行 |
| `ocr.threads` | `2` | ONNX 线程数 |
| `ocr.resident` | `30min` | 常驻策略 |
| `ocr.timeout_s` | `60` | 等系统截屏落图的超时 |
| `ocr.trigger` | `inject` | 谁打开截屏窗口：`inject` 注入 Print / `listen` GNOME 快捷键触发（保存时自动改绑）/ `spectacle` KDE 调 Spectacle |
| `inject.mode` | `auto` | `auto` / `uinput` / `clipboard` |
| `inject.paste_key` | `shift+insert` | 见 §2.3（默认对终端与浏览器都通用） |
| `inject.paste_delay_ms` | `60` | 写剪贴板到发键的间隔 |
| `inject.voice_paste` | `true` | 语音识别后是否自动粘贴 |
| `inject.ocr_paste` | `false` | 抓屏识别后是否自动粘贴（默认只进剪贴板，按需再粘） |
| `inject.restore_clipboard` | `false` | 粘完不恢复旧剪贴板（还能自己再按一次粘贴） |
| `web.enabled` | `true` | 常驻进程是否起设置页 |
| `web.host` / `web.port` | `127.0.0.1` / `7331` | 监听地址与端口（只监听回环） |

设置页（`finput web --open`）能改上表的大部分项：热键录制、常驻策略、线程数、抓屏触发方式与超时、
排版、粘贴开关、监听端口；页面同时显示 `status` 摘要。表单提交走 `POST /api/config`，服务端复用同一份
`validate()` 校验，非法值返回 400。保存时以「当前配置」为底合并提交上来的字段，所以页面上没有的键
（例如 `asr.models`）不会被重置。

**模型路径与说明记在 `models.json`**：`~/.config/flash-input/models.json`（和 `config.json` 同目录，
`finput models list` 会在第一行打印这个路径）。文件形如：

```json
{"version": 1, "models": [
  {"id": "sensevoice", "note": "我改的说明", "dir": "/mnt/models/sensevoice"},
  {"id": "my-model", "name": "我的模型", "kind": "ctc", "engine": "paraformer",
   "dir": "/home/me/models/paraformer", "files": ["model.int8.onnx", "tokens.txt"]}
]}
```

- 第一次读取时自动播种：内置五个模型各写一条（已下载的连同当前路径）。之后**文件是唯一事实来源**，
  目录、说明都可以手改；同 id 条目覆盖内置的 `note` / `dir` / `size_mb`，新 id 就是自定义模型。
- 设置页添加/删除自定义模型时改的也是这个文件；`config.json` 只留 `asr.model`（当前用哪个）。
- `models.json` 读坏时不会崩：`finput models list` 明确报错，daemon 退回内置名单照常启动。

**模型管理也在设置页**（`GET /api/models` + `POST /api/models/{install,select,remove,add,rebuild}`）：

- 列表把每个模型（含同一模型的 0.6B / 1.7B 两个规格）作为**独立条目**展示，显示体积、状态（未下载 /
  安装中 N% / 已就绪 / 使用中）和落盘目录。
- 列表同时返回内存里真实在跑的引擎（`engine`）：模型名、是否已加载、加载耗时、`pending`
  （配置已变但还没重建）、是否忙。**「使用中」看的是配置，`engine` 看的是运行时**，两者不一致时页面明说。
- 「安装」后台下载，「删除」对内置模型删缓存文件、对自定义模型只取消登记。
- 切换/删除/添加模型只改配置并回报“已保存”；守护进程取引擎时比较配置指纹
  （ASR：model+language+threads；OCR：threads+resident），变了自动重建，不需要重启。
  「重启模型」（`POST /api/models/rebuild`）立即触发重建，页面轮询 `engine` 直到把新引擎的加载结果报出来。
- 「添加本地 ONNX 模型」填名称 + 目录绝对路径：程序先确认每个 `.onnx` 能被 onnxruntime 打开，
  再按文件名/元数据猜引擎（三段式 Qwen3 / encoder+decoder+joiner / `model_type=paraformer` /
  带 `lfr_window_size` 的 CTC），登记后可直接启用。**不支持非 ONNX 格式**。

### 5.1 语音模型清单（设置页 / `finput models list`）

| `asr.model` | 模型 | 体积 | 加载 | 5 s 音频 | 峰值 RSS | 特点 |
|---|---|---|---|---|---|---|
| `qwen3-0.6b`（默认） | Qwen3-ASR 0.6B int8 | 983 MB | 2.4 s | ≈2.1 s | ≈3.1 GB | 中英均衡，自回归最慢 |
| `qwen3-1.7b` | Qwen3-ASR 1.7B int8 | 2.6 GB | 5 s | ≈4 s | ≈5 GB | 更准，内存占用高 |
| `sensevoice` | SenseVoiceSmall int8 | 239 MB | 0.7 s | 0.17 s | ≈0.5 GB | 中英日韩粤，抗噪，带标点 |
| `paraformer` | Paraformer-zh int8 | 227 MB | 1.0 s | 0.15 s | ≈0.5 GB | 中文最准，无标点 |
| `zipformer` | Zipformer zh 14M int8 | 26 MB | 0.25 s | 0.15 s | ≈0.12 GB | 流式模型，延迟最低，准确率略低 |

- 模型不随 wheel 分发。Qwen3 走 ModelScope 缓存；其余下到
  `$XDG_CACHE_HOME/flash-input/models/<id>/`（或 `$FLASH_INPUT_MODEL_DIR`）。
- 自定义条目（`models.json` 里的新 id）可以指向任意本地目录，只要求是完整可用的 ONNX 模型。
- 声学前端按各模型元数据自动选：SenseVoice/Zipformer 用 `[-1,1]` 样本 + povey 窗 +
  `snip_edges=false` + `high_freq=-400`；Paraformer 用 int16 量级样本 + hamming 窗 +
  `snip_edges=true`。LFR/CMVN 参数直接读 ONNX 的 `custom_metadata_map`。
- `asr.language` 只对 SenseVoice 生效（auto/zh/en/ja/ko/yue）。
- 切换模型后第一次使用会加载新模型。

---

## 6. 命令行

| 命令 | 作用 |
|---|---|
| `finput stop` | 停掉守护进程：先 IPC 优雅退出，没有再停 `flash-input.service`；`enable` 状态不变 |
| `finput run` | 常驻进程（systemd 拉起的就是它） |
| `finput status` | 守护进程 / 热键 / 模型 / 设置页状态 |
| `finput doctor` | 环境自检（键盘设备、uinput、剪贴板、模型、内存、systemd） |
| `finput web [--open]` | 打印（或打开）设置页地址 |
| `finput config show\|get\|set\|path` | 读写配置 |
| `finput models list\|fetch [--variant 0.6B\|1.7B\|all]` | 模型清单与下载 |
| `finput setup-permissions [--print\|--install]` | 检查/安装 udev uaccess 规则（需 sudo） |
| `finput service install\|uninstall\|status\|logs\|print` | systemd user service |
| `finput doctor --test hotkey` | 设备枚举 + 5 s 按键命中测试（排错用，不占独立命令） |
| `finput doctor --test inject` | 写剪贴板并发一次粘贴键，验证 uinput |
| `finput completion [bash\|zsh\|fish] [install\|uninstall]` | 生成/安装/卸载 shell 补全（脚本由 argparse 现生成，不手写） |

命令行分两类，避免堆砌：**日常**（`run` `stop` `status` `config` `web` `service` `doctor` `models` `setup-permissions` `completion`）与**排错自测**（`doctor --test hotkey|inject`）。
所有设置（热键、常驻、粘贴、抓屏）都在 Web 设置页里，命令只补设置页做不到的事：服务安装、模型下载、udev 规则、截屏键改绑。

是否自动粘贴只由配置决定（`inject.voice_paste` / `inject.ocr_paste`），命令行不提供覆盖开关。

退出码：`0` 成功 · `1` 失败 · `2` 环境不满足 · `3` 配置错误 · `4` 运行时错误（抓屏超时 / 模型缺失）
· `5` 强制 uinput 但不可用。

---

## 7. 权限（udev + logind）

`finput setup-permissions --install` 写 `/etc/udev/rules.d/99-flash-input.rules`，共三条：

```udev
KERNEL=="uinput", TAG+="uaccess", OPTIONS+="static_node=uinput"
SUBSYSTEM=="input", ENV{ID_INPUT_KEYBOARD}=="1", TAG+="uaccess"
KERNEL=="uinput", GROUP="input", MODE="0660"
```

- `uaccess` 由 logind 在登录时按 seat 发 ACL（`user:<uid>:rw-`），注销/切 seat 自动收回；
  比把用户塞进 `input` 组更贴合「只给当前前台用户」
- 键盘这条必须自己加：系统自带 `/usr/lib/udev/rules.d/70-uaccess.rules` **只给 joystick 打
  uaccess 标签**，键盘/鼠标没有，默认是 `root:input 660`
- 第三行是兜底：没有 logind 的机器上 uaccess 不生效，按组授权
- 装完需要 `udevadm control --reload-rules && udevadm trigger`（命令已包含）**并重新登录**
- 检查：`finput doctor` 的「可读输入设备」「/dev/uinput」两行；`getfacl /dev/uinput` 看 ACL

---

## 8. 常驻：systemd user service

```ini
[Unit]
Description=finput（语音/抓屏 → 文字）
# 图形会话就绪后才启动：compositor 的 wayland socket、输入设备都在此之后可用
After=graphical-session.target
PartOf=graphical-session.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
ExecStart=/home/<user>/.local/bin/finput run
Restart=on-failure
RestartSec=2
StandardOutput=journal
StandardError=journal
SyslogIdentifier=finput
# 可选覆盖：# Environment=FLASH_INPUT_CONFIG=/path/to/config.json

[Install]
WantedBy=graphical-session.target
```

为什么不用 XDG autostart：`After=graphical-session.target` 表达启动顺序、`Restart=on-failure` 自愈、
日志进 journald 可直接排错 —— `.desktop` 三件都做不到（只能 `sleep`、不会重启、日志只能重定向）。

- `finput service install`：写 unit → `daemon-reload` → `enable` → `restart`；重复执行幂等
- `finput service logs [-f]`：`journalctl --user -u flash-input`
- `ExecStart` 用**绝对路径**（安装时由 `shutil.which` 决定，通常是 `~/.local/bin/finput` 软链到
  `~/.local/share/uv/tools/flash-input`），`uv tool upgrade` 不用改 unit
- 不靠 unit 顺序赌运气：`run` 自带 30 s 有界就绪等待（会话总线 / wayland socket），等不到也继续

---

## 9. 跨发行版

目标：任意 **systemd + GNOME / KDE Plasma**（或会拉起 `graphical-session.target` 的桌面）。

| 分类 | 内容 |
|---|---|
| **A. `uv` 自动装** | onnxruntime、numpy、sounddevice（自带 PortAudio，依赖系统 `libasound`）、rapidocr、pillow、tokenizers、jeepney、modelscope、`flash-input` 本体 |
| **B. 必须发行版装** | 剪贴板二选一：`wl-clipboard`（Wayland）/ `xclip`（Xorg）；`systemd`（`systemctl --user`、`udevadm`）；内核 `uinput`；logind 的 seat ACL；glibc（非 musl）；KDE 另加 `spectacle`（区域截图） |
| **C. 不需要** | `notify-send`/libnotify（不弹通知）、`ydotool`/`wtype`/`xdotool`（uinput 自己发键）、grim/maim/import（系统截屏 UI）、ffmpeg/sox、系统 python3、keyd/swhkd |

- 抓屏**不需要**任何额外包：GNOME 用系统截屏 UI（`Print` 绑定 `show-screenshot-ui`，默认如此）；
  KDE 用 `spectacle --region --output`（KDE 默认自带 spectacle）

### 9.1 KDE 兼容

GNOME 与 KDE 在 Wayland 上共用同一套底层，所以只有两处不同：

| 层 | GNOME | KDE | 是否已兼容 |
|---|---|---|---|
| 热键 | 读 `/dev/input/event*`（evdev + udev uaccess） | 同左，**不需要**改桌面快捷键 | ✅ 代码相同 |
| 按键注入 | uinput 发 `Shift+Insert` | 同左 | ✅ |
| 剪贴板 | `wl-copy` / `xclip` | 同左 | ✅ |
| 常驻 | systemd user service + `graphical-session.target` | Plasma 同样拉起该 target | ✅ |
| 抓屏 UI | 快捷键触发系统截屏界面（`ocr.trigger=listen`）或注入 Print | `spectacle --region --output`（`ocr.trigger=spectacle`） | ✅ 新增 |
| 桌面快捷键改写 | `gsettings`（`show-screenshot-ui`，切到 `listen` 保存时自动改绑） | 不改，程序直接调 Spectacle | ✅ 保存 `ocr.trigger` 时按桌面自动切换 |

- `env.desktop_family()` 从 `XDG_CURRENT_DESKTOP` 判 `gnome` / `kde` / `other`；`doctor` 打印家族。
- KDE 上只需要在设置页把「谁打开截屏窗口」改成 Spectacle；没装 `spectacle` 时
  `doctor` 直接给包名（Debian/Ubuntu `spectacle`，Arch `spectacle`）。
- `spectacle_capture()` 用 `--region --output <runtime>/shot-<pid>.png --nonnotify`，
  超时/取消/文件不完整都有独立错误文案。
- 未实测：本机只有 GNOME，KDE 分支靠 `subprocess` 假 runner 与 `desktop_family()` 单测覆盖。
- `doctor` 按 `/etc/os-release` 的 `ID`/`ID_LIKE` 打印对应包名；**不代跑** `apt`/`dnf`/`pacman`
- 不假设 `~/.local/bin` 在 `PATH`：需要时打印绝对路径
- 明确不在范围：musl 发行版（Alpine，onnxruntime 无 wheel）、无 systemd 的桌面、容器 / 无 logind
- 实测环境：Ubuntu 24.04 / GNOME 46 / systemd 255 / Wayland / 24 核；Xorg 与 KDE 结论来自
  「同一批上游组件」的结构性判断，未逐项实测

---

## 10. 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 骨架：pyproject、配置、日志、xdg、env、CLI 框架、udev 规则资产 | ✅ |
| M0b | Web 设置页（`/`、`/api/config`、`/api/status`）+ 单测 | ✅ |
| M1 | `capture` + `ocr`（RapidOCR、行合并、模型随包）+ `clipboard` + `notifier` | ✅（抓屏在 M5 后改为系统截屏 UI） |
| M2 | `inject`（uinput 虚拟键盘、粘贴组合键、降级语义）+ `doctor --test inject` | ✅ |
| M3 | `features` / `asr` / `models` / `recorder` + `models` 子命令（语音由热键触发） | ✅ 中文质量未验（缺真实语料） |
| M4 | `hotkey`（原生 evdev + 守卫）+ `ipc` + `daemon`（单飞、维护线程、状态）+ `doctor --test hotkey` | ✅ 桌面实测 |
| M5 | `privileged`（udev）+ `service`（systemd）+ `shortcut`（GNOME 快捷键） | ✅（`--install` 需 sudo，未跑） |
| M6 | 打包验证：干净环境 `uv tool install` 全链路 + README 收尾 | ⬜ 未开始 |
| M7 | 可选 ASR 引擎：`asr_ctc`（SenseVoice/Paraformer）+ `asr_stream`（Zipformer）+ `asr_models` 下载 | ✅ 2026-10-21 |

测试：`uv run pytest` → **183 passed**（配置、键位映射、OCR 行合并、注入事件序列、热键状态机、
IPC、daemon 单飞、设置页、服务/规则/快捷键生成）。

---

## 11. 实测数据（24 核 CPU，int8）

| 项目 | 数值 |
|---|---|
| ASR 0.6B 模型 | 983 MB（conv 44 + encoder 182 + decoder 756） |
| ASR 加载 | 2.4 s；首推后 RSS 2.4 GB；峰值 ≈ 3.1 GB；`unload` 后 ≈ 1.3 GB（ORT arena 不归 OS） |
| ASR 解码 | 35 ms/token @4 线程；10 s 中文 ≈ 40 token ≈ **3.2 s**（已加载），冷启动 +4.9 s |
| OCR 模型 | 31 MB（PP-OCRv6 det/rec + cls） |
| OCR 加载 / 常驻 | 0.4–1.3 s / RSS 169 MB |
| OCR 识别 | 5 行对话框 0.67 s；满屏 1920×1080（28 行）1.72 s @4 线程 |
| 空闲 | 空进程 43 MB / ~0% CPU；ASR 常驻时 2.4–3 GB |
| 新引擎实测（同一段 5.2 s 中文） | zipformer：load 0.25 s / 转写 0.15 s / 峰值 122 MB；sensevoice：0.68 s / 0.17 s / 493 MB；paraformer：1.0 s / 0.15 s / 493 MB |

**关键结论：ONNX 线程数必须显式设**。ORT 默认 `intra_op_num_threads` = 核数，24 核上同工作量
比 4 线程慢一倍、烧 6.7 倍 CPU（17.8 → 120 CPU·s）。所以 `asr.threads=4`、`ocr.threads=2` 是默认值，
不是随手填的。

---

## 12. 已知限制

1. **中文识别质量未用真实语料验证**：端到端只在 espeak 合成英文上验过（与旧 daobidao 逐字一致），
   合成中文两端一样乱，说明是合成音问题而非实现差异
2. **`resident` 不能真正归还内存**：`unload()` 释放 session 后 RSS 仍有 ~1.3 GB（ORT arena 留给进程）；
   想彻底还内存只能让进程退出（systemd 会拉回来）
3. **抓屏依赖 `Print` 绑定**：用户把系统截屏快捷键改掉后，`ocr` 会等到超时；`doctor` 与 README 给了检查方法
4. **Xorg / KDE / 1.7B 未实测**：Xorg 走 `xclip`、KDE 走同一套 evdev 与 `Print`，结构上应成立但没跑过；
   1.7B 模型未下载验证
5. **`docs/runtime.md` 的流程图为手绘 mermaid**，与代码同步靠人，不是自动生成
6. **zipformer 是 14M 小模型**：速度快、内存小，但准确率明显低于另外三个；同段话实测会出现「金融动荡 → 竞动」这类错字，适合要低延迟的场合
7. **新引擎的中文质量同样只用合成音与 sherpa-onnx 的公开测试音频验证过**，没跑真实麦克风语料

---

## 13. 下一步

- **M6**：干净环境 `uv tool install`（wheel + sdist）→ 从零跑通 `doctor` / `voice` / `ocr` / `service install`
- 真实中文语料测一轮 ASR 质量（按住热键说一段，看粘贴结果）
- Xorg 会话、KDE 各跑一次 `doctor --test hotkey` + `ocr`，把 §9 的「结构性判断」变成实测
- 可选：`asr.resident=none` 时的「空闲自杀 + systemd 拉起」实现，替代目前留着 1.3 GB 的做法
