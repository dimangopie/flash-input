# Flash Input

语音 / 抓屏 → 文字 → 剪贴板。本地 ONNX 推理，Xorg 与 Wayland 通用，不联网。

> 状态：**M5（权限 + systemd 常驻 + 桌面快捷键已接上）**。已能：`finput service install` 装成
> systemd user service（崩溃自动拉起、日志进 journal，随图形会话启动），按住右 Ctrl 说话、松开出字，
> `Super+Shift+O` 抓屏 OCR；录音 → Qwen3-ASR int8 → 剪贴板 → uinput 粘贴；系统截屏 UI → RapidOCR →
> 同样的注入链路；`doctor`（含 `--test hotkey|inject` 自测）/ `config` / `status` / Web 设置页（含键位录制）。
> 完整设计见 [OUTLINE.md](OUTLINE.md)，运行流程（mermaid 图）见 [docs/runtime.md](docs/runtime.md)。

## 安装

```bash
uv tool install /path/to/flash-input            # 从源码装到全局（尚未发布 PyPI）
# 开发时想要「改代码即生效」：uv tool install --editable /path/to/flash-input
finput models fetch                  # 下载当前 asr.model 选中的模型
finput setup-permissions --install   # udev uaccess 规则（本机权限已够用时可不装）
finput service install               # systemd user service 常驻（崩溃自愈 + 随图形会话启动）
```

装好后 `finput` 在 `~/.local/bin`（uv 会把可执行文件软链到 `~/.local/share/uv/tools/flash-input`）。
`finput service install` 会把 **当前这个 finput 的绝对路径**写进 unit，之后 `uv tool upgrade` 不用改 unit。

OCR 模型随 wheel 分发（31 MB）；ASR 模型不进 wheel，首次用前跑一次 `finput models fetch`。

模型路径与说明记在 `~/.config/flash-input/models.json`（`finput models list` 第一行会打印它；改说明、搬模型目录都改这个文件）。
语音模型五条（`asr.model`，设置页里可装/删/启用，也能添加本地 ONNX 模型）：`qwen3`（默认，中英均衡，约 1 GB / 3 GB 内存）、`qwen3-1.7b`（2.6 GB，更准）、`sensevoice`（239 MB，抗噪带标点）、`paraformer`（227 MB，中文最准）、`zipformer`（26 MB，流式低延迟）。设置页「语音模型」里点安装/启用即可。

## 常用命令

```bash
finput run                          # 常驻：读 /dev/input 热键 + 模型常驻 + 设置页
finput stop                          # 停守护进程（service 的 enable 状态不变）
finput status                       # 守护进程状态、最近动作、模型是否加载
finput service status               # is-enabled / is-active、unit 路径
finput service logs -f              # journalctl --user -u flash-input -f
finput doctor --test hotkey          # 看键盘设备、权限与按键命中（选键/排错）
finput doctor                       # 环境自检（会话/键盘设备/uinput/剪贴板/模型/内存）
finput models list                  # 四种引擎的模型是否已下载（* 为当前引擎）
finput config set asr.model sensevoice    # 切换语音模型，然后 finput models fetch
finput doctor --test inject          # 写测试文字并发一次粘贴键，验证 uinput 可用
finput completion install           # 装 bash/zsh/fish 命令补全（新终端生效）
finput config show                  # 打印生效中的配置
finput config set asr.resident 30min
finput config set ocr.resident forever
finput web                          # 打印设置页地址（随 daemon 启动）
```

## 并发

ASR 与 OCR 各跑一条**独立模型线程**：语音转写时按抓屏键照样能用，反之亦然；
同一个模型内部严格串行（不会出现加载到一半被卸载）。两个结果同时到达时，
「写剪贴板 + 发粘贴键」这一步串行，不会互相撕字。

## 配置

`~/.config/flash-input/config.json`（可用 `FLASH_INPUT_CONFIG` 覆盖）。默认值见 `config show`，
字段说明见 OUTLINE.md §11。模型常驻策略：

| `resident` | 含义 |
|---|---|
| `none` | 用完立即卸载（内存最省，每次要重新加载） |
| `10min` / `30min` | 空闲这么久后卸载（默认 ASR `forever`、OCR `30min`） |
| `forever` | 永不卸载（拿内存换首次延迟） |

切换模型（`asr.model` / `asr.language` / `asr.threads`）或改 `ocr.threads` / `ocr.resident` 后
**不需要手动重启**：守护进程下次取引擎时会比较「配置指纹」，发现变了就卸掉旧引擎、按新配置重建，
现有常驻的旧引擎不会继续冒充新模型。设置页「语音模型」里的「重启模型」按钮可以立刻完成这次重建
（页面会轮询显示内存里真实在跑的模型与加载耗时）；不点也不影响正确性，只是第一次识别会多一次加载。

## 目录

```
src/flash_input/   cli / config / logging / xdg / env / webui / keys（M0）
                  capture / ocr / clipboard（M1）
                  inject（M2）
                  features / asr / models / recorder（M3）
                  asr_ctc / asr_stream / asr_models / fbank（多引擎，M7）
                  hotkey / ipc / daemon（M4，已完成）
                  privileged / service / shortcut（M5，已完成）
                  models/  OCR 三个 ONNX 模型（PP-OCRv6 small，随 wheel 分发）
tests/            pytest
```

## 热键

| 动作 | 默认键 | 语义 |
|---|---|---|
| 语音 | **按住右 Ctrl**（`KEY_RIGHTCTRL`） | 按下开始录，松开转写；按住不足 300 ms 视为误触 |
| 抓屏 OCR | `Super+Shift+O` | 按下即抓屏，不需要松开 |

- 键位在设置页里点「录制」录入（`finput web`），存 `hotkey.voice_key` / `hotkey.ocr_key`
- **修饰键守卫**：按住语音键后 300 ms 内按了别的键（如 Ctrl+C）→ 判为组合键，不录音
- 必须能读 `/dev/input/event*`：`finput setup-permissions --install` 后重新登录；
  没有权限时 `finput doctor --test hotkey` 会说明原因

## 抓屏怎么选区

程序**不自己画选区**，也不调 `grim`/`slurp` 这类外部命令 —— 选区界面就是系统截屏窗口本身：

1. 按下抓屏热键（默认 `Super+Shift+O`）。
2. 系统截屏窗口弹出（GNOME 原生截屏界面），拖框选 `Select Area to Grab`；
   框选 / 整屏 / 选窗口三种模式、多屏、HiDPI、缩放全部由桌面负责。
3. 桌面生成 PNG 之后，程序只认「这次触发之后新出现、且写完整的 PNG」，取走 → OCR → 剪贴板，然后把原图删掉。

KDE 上 `ocr.trigger=spectacle`：由 `spectacle --region` 弹自己的选区并直接写文件，流程一样。

**没有系统截屏窗口的桌面（sway / Hyprland / i3 这类只把按键映射到命令的 WM）不支持**：
`inject` 注入的 `Print` 没有窗口响应，`listen` 也没有 GNOME 快捷键可改绑，结果只会等到
`ocr.timeout_s` 超时。本程序只借系统窗口选区，不内置选区覆盖层，也不接管外部截图命令。

## 开发

```bash
uv sync                       # 装依赖（含 pytest / ruff）
uv run pytest -q              # 测试（227 条）
uv run ruff check src tests   # 静态检查
uv run ruff format src tests  # 格式化
uv build                      # 出 wheel / sdist
uv tool install --force .     # 装到全局（改完代码重装一次）
```

规范写在 `pyproject.toml` 的 `[tool.ruff]`：行长 110、目标 py3.12、规则集
`E F W I UP B C4 SIM PERF RUF`；刻意保留的降级写法（宽泛 `except Exception`、
`try/except/pass`）已连原因列进 `ignore`。

## 故障排查

| 现象 | 先看什么 |
|---|---|
| 语音识别出字了但没粘贴进窗口 | `finput doctor --test inject`：`/dev/uinput` 可写吗；`inject.mode=auto` 下粘贴失败会退化成「只写剪贴板」，文字还在剪贴板里，手动 Ctrl+V 即可 |
| 常驻时按热键没反应 | `finput status` 看监听到了几个键盘设备；新插的 USB 键盘 2 秒内会自动接入，`finput doctor --test hotkey` 能看原始按键与命中判定 |
| 没有任何日志 | 由 systemd 启动时日志只进 journal：`journalctl --user -u flash-input -f`；手动 `finput run` 时写 `~/.local/state/flash-input/flash-input.log`（目录不可写则退回 stderr） |
| KDE 上按抓屏热键没弹选区 | 需要 `spectacle`：`finput doctor` 看「抓屏方式」；没装就 `sudo apt install spectacle`（Arch: `pacman -S spectacle`），「谁打开截屏窗口」选 Spectacle 即可 |
| 按抓屏热键没弹出系统截屏窗口 | 默认靠 uinput 按一下 `Print`：`finput doctor` 看「/dev/uinput」是否可写；也可在设置页把「谁打开截屏窗口」改成 `listen`（保存时自动把热键加进 GNOME 的「截屏界面」） |
| 系统截屏窗口开着，不想截了 | 在窗口里按 Esc 关掉；daemon 会等超时（`ocr.timeout_s`，默认 60 秒）后自动空出 |
| 抓屏超时（`等待系统截屏超时`） | 要在系统窗口里完成框选、让桌面生成图片；程序只认这次触发后新出现且写完整的 PNG。如果是 sway/Hyprland/i3 这类**没有系统截屏窗口**的桌面，注定超时——本程序不支持这类桌面（见上文「抓屏怎么选区」） |
| 粘贴键不对（终端里按了没反应） | 默认 `shift+insert` 终端与浏览器都通用；个别应用要 `ctrl+v` 或 `ctrl+shift+v`，在设置页「粘贴组合键」里换 |
| 按 `Print` 没反应、抓屏一直等到超时 | 桌面把系统截屏快捷键改掉了：`gsettings list-recursively org.gnome.shell.keybindings \| grep show-screenshot-ui` 看绑的是什么；到设置页把「谁打开截屏窗口」重新保存一次会重新改绑 |
| 停止/卸载后快捷键还绑着 | 程序不会自动还原映射：设置页把「谁打开截屏窗口」改回「注入 Print」并保存即可还原 `show-screenshot-ui`；KDE 用 Spectacle，不碰桌面按键 |
| 抓的图去哪了 | 识别完就删了（`$XDG_RUNTIME_DIR/flash-input/shot-<pid>.png` 只活到 OCR 结束） |
| 守护进程说「已有实例在运行」 | 单实例锁在 `$XDG_RUNTIME_DIR/flash-input/flash-input.sock`；先 `finput status`，确实残留再删 socket |
