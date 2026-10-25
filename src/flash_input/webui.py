"""本地 Web 设置页：单文件 HTML + 原生 JS，只监听回环地址。

- `GET  /`            设置页
- `GET  /api/config`  生效中的配置 + 配置文件路径 + 默认值
- `POST /api/config`  整份校验 → 原子写 → 回调热重载（daemon 用）
- `GET  /api/status`  环境自检（与 `finput doctor` 同一份数据）
- `GET  /api/models`  语音模型清单（含安装进度、内存里真实在跑的引擎）
- `POST /api/models/{install,select,remove,add,rebuild}`  安装 / 切换 / 删除 / 添加本地 ONNX 模型 / 重建引擎

两种用法：
- `finput web --serve`：前台起服务（M0 起就能用，不需要常驻进程）
- `finput run`：M4 起在后台线程里调用 `serve_background()`，随守护进程一起提供
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__, asr_catalog, env, keys
from . import config as config_mod
from .logging import get as _get_log

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flash Input · 设置</title>
<style>
 :root{--bg:#f6f8fa;--card:#fff;--line:#e2e6ea;--accent:#2f6feb;--ok:#1a7f37;--err:#cf222e}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:#1f2328;font:14px/1.6 system-ui,"Noto Sans CJK SC",sans-serif}
 .wrap{max-width:820px;margin:0 auto;padding:24px 16px 60px}
 h1{font-size:20px;display:flex;align-items:center;gap:10px}
 h1 small{color:#6e7781;font-weight:400;font-size:13px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}
 .card h2{margin:0 0 10px;font-size:15px}
 label{display:block;margin:10px 0 2px;font-weight:600}
 input,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;font:inherit;background:#fff}
 .row{display:flex;gap:14px;flex-wrap:wrap}.row>div{flex:1;min-width:200px}
 .checks{display:flex;flex-wrap:wrap;gap:6px 10px;margin-top:8px}
 .check{display:inline-flex;align-items:center;gap:8px;margin:0;padding:6px 10px;
        width:fit-content;max-width:100%;border-radius:6px;font-weight:400;cursor:pointer}
 .check:hover{background:#eef1f4}
 .check input{width:16px;height:16px;flex:none;accent-color:var(--accent)}
 button{border:0;border-radius:6px;padding:8px 16px;font-size:14px;cursor:pointer}
 .primary{background:var(--accent);color:#fff}.ghost{background:#eef1f4}
 .rec{display:flex;gap:8px}.rec input{flex:1;background:#f6f8fa;color:#1f2328}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px 16px}
 .grid b{display:inline-block;min-width:112px;color:#57606a}
 .ok{color:var(--ok);font-weight:600}.bad{color:var(--err);font-weight:600}
 #msg{display:none;margin:8px 0;padding:8px 12px;border-radius:6px}
 #msg.err{display:block;background:#ffebe9;color:var(--err)}
 #msg.ok{display:block;background:#dafbe1;color:var(--ok)}
 .hint{color:#6e7781;font-size:12.5px;margin-top:4px}
 .model{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
 .model:first-child{border-top:0}
 .model .info{flex:1;min-width:0}
 .model .name{font-weight:600}
 .model .meta{color:#6e7781;font-size:12px;overflow-wrap:anywhere}
 .model .tag{font-size:11px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:#6e7781}
 .model .tag.on{color:var(--ok);border-color:var(--ok)}
 footer{color:#6e7781;font-size:12px;text-align:center;margin-top:26px}
</style></head><body><div class="wrap">
<h1>⌨️ Flash Input 设置 <small>语音 / 抓屏 → 文字</small></h1>
<div id="msg"></div>

<div class="card"><h2>环境状态</h2><div class="grid" id="status"><span class="hint">加载中…</span></div></div>

<form id="form" autocomplete="off">
<div class="card"><h2>模型常驻（内存换延迟）</h2>
 <div class="row">
  <div><label for="asr_resident">ASR 模型常驻</label>
   <select id="asr_resident" data-path="asr.resident">
    <option value="none">不常驻（用完立即卸载）</option>
    <option value="10min">空闲 10 分钟后卸载</option>
    <option value="30min">空闲 30 分钟后卸载</option>
    <option value="forever">永久常驻（永不卸载）</option></select></div>
  <div><label for="ocr_resident">OCR 模型常驻</label>
   <select id="ocr_resident" data-path="ocr.resident">
    <option value="none">不常驻（用完立即卸载）</option>
    <option value="10min">空闲 10 分钟后卸载</option>
    <option value="30min">空闲 30 分钟后卸载</option>
    <option value="forever">永久常驻（永不卸载）</option></select></div>
 </div>
 <input type="hidden" id="asr_model" data-path="asr.model">
 <div class="row">
  <div><label for="asr_language">SenseVoice 语言</label>
   <select id="asr_language" data-path="asr.language">
    <option value="auto">自动</option>
    <option value="zh">中文</option>
    <option value="en">英文</option>
    <option value="ja">日文</option>
    <option value="ko">韩文</option>
    <option value="yue">粤语</option></select></div>
  <div><label for="asr_threads">ASR 线程数</label>
   <input type="number" id="asr_threads" data-path="asr.threads" min="1" max="256"></div>
  <div><label for="ocr_threads">OCR 线程数</label>
   <input type="number" id="ocr_threads" data-path="ocr.threads" min="1" max="256"></div>
 </div>
 <div class="hint">实测：ASR 4 线程、OCR 2 线程最快；交给默认（=核数）反而更慢、CPU 高数倍。</div>
</div>

<div class="card"><h2>语音模型</h2>
 <div class="hint">同一个模型的不同规格（如 Qwen3 的 0.6B / 1.7B）是不同条目。只支持 ONNX 模型；
   「删除」对内置模型是删缓存文件，对自定义模型只是取消登记（文件不动）。</div>
 <div class="hint">模型路径与说明都记在 <code id="registry_path">models.json</code>（和 config.json 同目录），
   想改说明、把模型文件搬到别处，直接编辑这个文件再刷新本页。</div>
 <div id="model_list"><span class="hint">加载中…</span></div>
 <div class="hint" id="engine_state">运行中的引擎：读取中…</div>
 <div class="row" style="margin-top:10px">
  <div style="flex:none"><button type="button" class="ghost" id="model_rebuild">重启模型</button></div>
  <div class="hint" style="flex:1;margin:10px 0 0">改完模型或线程数后点它立即重建内存里的引擎；
   不点也会在下次识别时按新配置自动换。</div>
 </div>
 <div class="row" style="margin-top:10px">
  <div><label for="model_name">添加本地 ONNX 模型 · 名称</label>
   <input type="text" id="model_name" placeholder="如 my-paraformer"></div>
  <div><label for="model_dir">模型目录（绝对路径）</label>
   <input type="text" id="model_dir" placeholder="/home/me/models/paraformer"></div>
  <div style="flex:none"><button type="button" class="ghost" id="model_add">添加并启用</button></div>
 </div>
 <div class="hint" id="model_hint"></div>
</div>

<div class="card"><h2>热键</h2>
 <div class="row">
  <div><label for="hotkey_mode">热键模式</label>
   <select id="hotkey_mode" data-path="hotkey.mode">
    <option value="evdev">evdev：进程内监听（按住说话 + 组合键抓屏）</option>
    <option value="off">off：不监听键盘（只保留抓屏与设置页）</option></select></div>
  <div><label for="voice_key">语音键（按住说话）</label>
   <div class="rec"><input type="text" id="voice_key" data-path="hotkey.voice_key" readonly placeholder="点右侧「录制」">
    <button type="button" class="ghost" id="voice_rec">⌨ 录制</button></div>
   <div class="hint" id="voice_tip">必须是单个修饰键（如右 Ctrl / 右 Alt），按住说话、松开识别。</div></div>
  <div><label for="ocr_key">抓屏组合键</label>
   <div class="rec"><input type="text" id="ocr_key" data-path="hotkey.ocr_key" readonly placeholder="点右侧「录制」">
    <button type="button" class="ghost" id="ocr_rec">⌨ 录制</button></div>
   <div class="hint" id="ocr_tip">按下组合键（如 Super+Shift+O）或单个功能键（如 F9），按下即抓屏。</div></div>
 </div>
 <div class="hint">键名是 Linux 键码（KEY_*），由浏览器按物理键位转换，与键盘布局无关。录制成功后自动保存。</div>
</div>
<div class="card"><h2>抓屏</h2>
 <div class="row">
  <div><label for="ocr_trigger">谁打开截屏窗口</label>
   <select id="ocr_trigger" data-path="ocr.trigger">
    <option value="inject">我们注入 Print（默认）</option>
    <option value="listen">GNOME 快捷键触发（保存时自动改绑）</option>
    <option value="spectacle">KDE · Spectacle 区域截图（自动，无需改桌面键）</option></select></div>
  <div><label for="ocr_timeout">截屏等待超时（秒）</label>
   <input type="number" id="ocr_timeout" data-path="ocr.timeout_s" min="5" max="600"></div>
 </div>
</div>


<div class="card"><h2>识别与粘贴</h2>
 <div class="row">
  <div><label for="layout">文本排版</label>
   <select id="layout" data-path="ocr.layout">
    <option value="reading">reading：按视觉行合并（推荐）</option>
    <option value="box">box：每个检测框一行</option></select></div>
  <div><label for="inject_mode">粘贴方式</label>
   <select id="inject_mode" data-path="inject.mode">
    <option value="auto">auto：能 uinput 就自动粘贴</option>
    <option value="uinput">uinput：只走虚拟键盘</option>
    <option value="clipboard">clipboard：只进剪贴板</option></select></div>
  <div><label for="paste_key">粘贴组合键</label>
   <select id="paste_key" data-path="inject.paste_key">
    <option value="shift+insert">Shift+Insert（终端/浏览器，默认）</option>
    <option value="ctrl+v">Ctrl+V</option>
    <option value="ctrl+shift+v">Ctrl+Shift+V（终端）</option>
    <option value="ctrl+alt+v">Ctrl+Alt+V</option>
    <option value="shift+insert">Shift+Insert（终端/浏览器）</option>
    <option value="none">不自动粘贴（只进剪贴板）</option></select></div>
 </div>
  <div class="checks">
   <label class="check"><input type="checkbox" id="voice_paste" data-path="inject.voice_paste">语音识别后自动粘贴</label>
   <label class="check"><input type="checkbox" id="ocr_paste" data-path="inject.ocr_paste">图片识别后自动粘贴</label>
  </div>
</div>

<div class="card"><h2>设置页本身</h2>
 <div class="row">
  <div><label for="web_host">监听地址</label>
   <input type="text" id="web_host" data-path="web.host" readonly title="只允许本机回环地址（127.0.0.1 / ::1 / localhost）"></div>
  <div><label for="web_port">端口（0 = 自动）</label>
   <input type="number" id="web_port" data-path="web.port" min="0" max="65535"></div>
 </div>
 <div class="hint">改地址或端口需要重启守护进程才生效。</div>
</div>

<div class="card" style="display:flex;gap:12px;align-items:center">
 <button type="submit" class="primary" id="save">保存</button>
 <button type="button" class="ghost" id="reset">恢复默认</button>
 <span class="hint" id="file"></span>
</div>
</form>
<footer>Flash Input <span id="ver"></span> · 只监听本机回环地址</footer>
</div>
<script>
let DEFAULTS = {};
const $ = id => document.getElementById(id);
function flash(text, kind){ const m = $("msg"); m.className = kind; m.textContent = text;
  setTimeout(()=>{ m.className = ""; m.style.display="none"; }, 4000); }
function setByPath(obj, path, value){
  const parts = path.split("."); let node = obj;
  for (const p of parts.slice(0,-1)) node = node[p] ||= {};
  node[parts.at(-1)] = value;
}
function getByPath(obj, path){ return path.split(".").reduce((n,p)=> n?.[p], obj); }
function fill(cfg){
  DEFAULTS = cfg.defaults || {};
  document.querySelectorAll("[data-path]").forEach(el=>{
    const v = getByPath(cfg, el.dataset.path);
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v ?? "";
  });
  $("file").textContent = cfg.config_path + (cfg.exists ? "" : "（尚未创建，保存后生成）");
  $("ver").textContent = cfg.version;
  REVISION = cfg.revision || "new";
}
let REVISION = "new";
async function loadAll(){
  const cfg = await (await fetch("/api/config")).json();
  fill(cfg);
  await loadModels();
  const st = await (await fetch("/api/status")).json();
  const g = $("status"); g.innerHTML = "";
  for (const [k, v] of Object.entries(st)) {
    const d = document.createElement("div");
    const b = document.createElement("b"); b.textContent = k;
    const s = document.createElement("span");
    s.textContent = Array.isArray(v) ? (v.length + " 个") : String(v);
    if (v === true) s.className = "ok";
    if (v === false || v === null) s.className = "bad";
    d.append(b, s); g.append(d);
  }
}
/* ---- 语音模型：列表 / 安装 / 删除 / 启用 / 添加本地目录 ---- */
let MODEL_POLL = null;
function modelRow(m){
  const row = document.createElement("div"); row.className = "model";
  const info = document.createElement("div"); info.className = "info";
  const name = document.createElement("div"); name.className = "name";
  name.textContent = m.name + " ";
  if (m.current) { const t = document.createElement("span"); t.className = "tag on"; t.textContent = "使用中"; name.append(t); }
  if (m.custom) { const t = document.createElement("span"); t.className = "tag"; t.textContent = "自定义"; name.append(t); }
  if (m.source) { const t = document.createElement("span"); t.className = "tag"; t.textContent = m.source; name.append(t); }
  const meta = document.createElement("div"); meta.className = "meta";
  const state = m.installing ? ("安装中 " + (m.progress == null ? "" : m.progress + "%"))
    : (m.ready ? "已就绪" : "未下载");
  meta.textContent = `${m.id} · ${m.kind} · ${m.size_mb} MB · ${state} · ${m.note}`;
  const dir = document.createElement("div"); dir.className = "meta"; dir.textContent = m.dir;
  info.append(name, meta, dir);
  row.append(info);
  const actions = document.createElement("div"); actions.style.cssText = "display:flex;gap:6px;flex:none";
  if (m.installing) {
    const b = document.createElement("button"); b.className = "ghost"; b.textContent = "安装中…"; b.disabled = true;
    actions.append(b);
  } else if (!m.ready) {
    const b = document.createElement("button"); b.className = "ghost"; b.textContent = "安装";
    b.onclick = ()=> modelAction("install", {id: m.id});
    actions.append(b);
  } else if (!m.current) {
    const b = document.createElement("button"); b.className = "ghost"; b.textContent = "启用";
    b.onclick = ()=> modelAction("select", {id: m.id});
    actions.append(b);
  }
  if (!m.current) {
    const b = document.createElement("button"); b.className = "ghost"; b.textContent = "删除";
    b.onclick = ()=> { if (confirm("删除 " + m.name + "？" + (m.custom ? "（只取消登记，文件不动）" : "（会删掉已下载的模型文件）"))) modelAction("remove", {id: m.id}); };
    actions.append(b);
  }
  row.append(actions);
  return row;
}
function renderEngine(e){
  const a = (e && e.asr) || {}, o = (e && e.ocr) || {};
  let asr;
  if (!a.model) asr = "未加载";
  else {
    asr = a.model + (a.loaded ? "（已加载" + (a.load_ms ? "，" + a.load_ms + " ms" : "") + "）" : "（未加载）");
    if (a.pending) asr += " · 配置已变，下次识别自动换";
  }
  const ocr = o.loaded ? "已加载（" + o.threads + " 线程）" : "未加载";
  $("engine_state").textContent = "运行中的引擎：语音 " + asr + "；OCR " + ocr;
}
function renderModels(data){
  if (data.registry) $("registry_path").textContent = data.registry;
  const box = $("model_list"); box.innerHTML = "";
  for (const m of data.models || []) box.append(modelRow(m));
  if (data.engine !== undefined) renderEngine(data.engine);
  if (data.message) flash(data.message, "ok");
  if (data.error) flash(data.error, "err");
  clearTimeout(MODEL_POLL);
  if ((data.models || []).some(m => m.installing)) MODEL_POLL = setTimeout(loadModels, 1200);
}
async function loadModels(){
  const r = await fetch("/api/models");
  const data = await r.json();
  renderModels(data);
  if (data.revision) REVISION = data.revision;
}
async function modelAction(action, body){
  const r = await fetch("/api/models/" + action, {method:"POST", headers:{"Content-Type":"application/json"},
                                                  body: JSON.stringify(body)});
  const data = await r.json();
  renderModels(data);
  if (!data.error) { const cfg = await (await fetch("/api/config")).json(); fill(cfg); }
  return data;
}
async function waitEngine(){
  // 重建在模型线程里跑；这里轮询真实引擎状态，别让“正在重建”变成又一次假成功
  for (let i = 0; i < 60; i++) {
    await new Promise(res => setTimeout(res, 500));
    const data = await (await fetch("/api/models")).json();
    const e = data.engine || {}, a = e.asr || {};
    if (a.busy) continue;
    renderModels(data);
    if (a.loaded) {
      flash("模型已重建：" + (a.model || "?") + (a.load_ms ? "（加载 " + a.load_ms + " ms）" : ""), "ok");
    } else {
      flash("重建失败：" + (e.last_error || "详见日志 finput log"), "err");
    }
    return;
  }
  flash("重建还没结束，稍后刷新本页看「运行中的引擎」", "err");
}
$("model_rebuild").addEventListener("click", ()=>{
  modelAction("rebuild", {}).then(data => { if (data.ok) waitEngine(); });
});
$("model_add").addEventListener("click", ()=>{
  const name = $("model_name").value.trim(), dir = $("model_dir").value.trim();
  if (!dir) { flash("请填模型目录的绝对路径", "err"); return; }
  modelAction("add", {name, dir}).then(()=>{ $("model_name").value = ""; $("model_dir").value = ""; });
});
/* ---- 热键录制：浏览器 KeyboardEvent.code → Linux KEY_*（映射表由服务端注入） ---- */
const KEYMAP_JS = __KEYMAP_JSON__;
const MOD_KEYS = new Set(["KEY_LEFTCTRL","KEY_RIGHTCTRL","KEY_LEFTALT","KEY_RIGHTALT",
                          "KEY_LEFTSHIFT","KEY_RIGHTSHIFT","KEY_LEFTMETA","KEY_RIGHTMETA"]);
let rec = null;
function keyName(ev){
  if (KEYMAP_JS[ev.code]) return KEYMAP_JS[ev.code];
  if (/^Key[A-Z]$/.test(ev.code)) return "KEY_" + ev.code.slice(3);
  if (/^Digit[0-9]$/.test(ev.code)) return "KEY_" + ev.code.slice(5);
  if (/^Numpad[0-9]$/.test(ev.code)) return "KEY_KP" + ev.code.slice(6);
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(ev.code)) return "KEY_" + ev.code;
  return null;
}
function pretty(combo){ return combo.replaceAll("KEY_", ""); }
function recordStart(kind){
  recordStop();
  const voice = kind === "voice";
  const tip = $(voice ? "voice_tip" : "ocr_tip");
  rec = { kind, input: $(voice ? "voice_key" : "ocr_key"), tip,
          defaultTip: tip.textContent, mods: [], pending: null };
  rec.input.value = "";
  rec.tip.textContent = "正在录制…… 按 Esc 取消";
  window.addEventListener("keydown", recordDown, true);
  window.addEventListener("keyup", recordUp, true);
}
function recordStop(){
  window.removeEventListener("keydown", recordDown, true);
  window.removeEventListener("keyup", recordUp, true);
  if (rec) rec.tip.textContent = rec.defaultTip;
  rec = null;
}
function recordHint(text){ if (rec) rec.tip.textContent = text; }
function recordCommit(value){
  const input = rec.input;
  recordStop();
  input.value = value;
  flash("已录制：" + (value ? pretty(value) : "（已清空）"), "ok");
  $("form").requestSubmit();
}
function recordDown(ev){
  if (!rec) return;
  ev.preventDefault(); ev.stopPropagation();
  if (ev.key === "Escape") { recordStop(); return; }
  if (rec.kind === "ocr" && ev.key === "Backspace") { recordStop(); return; }
  const name = keyName(ev);
  if (!name) { recordHint("这个键不支持录制（" + ev.code + "），换一个"); return; }
  if (MOD_KEYS.has(name)) {
    if (!rec.mods.includes(name)) rec.mods.push(name);
    if (rec.kind === "voice") { rec.pending = name; rec.tip.textContent = "松开即保存：" + pretty(name); }
    else { rec.tip.textContent = "正在录制…… " + pretty(rec.mods.join("+")); }
    return;
  }
  if (rec.kind === "voice") { recordHint("语音键只能是修饰键（Ctrl / Alt / Shift / Super）"); return; }
  recordCommit([...rec.mods, name].join("+"));
}
function recordUp(ev){
  if (!rec || rec.kind !== "voice" || !rec.pending) return;
  if (keyName(ev) === rec.pending) recordCommit(rec.pending);
}
$("voice_rec").addEventListener("click", ()=> recordStart("voice"));
$("ocr_rec").addEventListener("click", ()=> recordStart("ocr"));

$("form").addEventListener("submit", async ev=>{
  ev.preventDefault();
  const out = JSON.parse(JSON.stringify(DEFAULTS));
  document.querySelectorAll("[data-path]").forEach(el=>{
    let v = el.type === "checkbox" ? el.checked : el.value;
    if (el.type === "number") v = v === "" ? null : Number(v);
    setByPath(out, el.dataset.path, v);
  });
  // 自定义模型登记由 /api/models 管，不随这张表单提交（否则会覆盖成空列表）
  delete out.asr.models;
  out._revision = REVISION;
  const r = await fetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"},
                                       body: JSON.stringify(out)});
  const data = await r.json();
  if (r.ok) { fill(data); flash("已保存：" + data.config_path, "ok"); }
  else if (r.status === 409) {
    flash("配置已被别处修改，未保存。请按 F5 刷新后重试（" + (data.error || "") + "）", "err");
  }
  else { flash("保存失败：" + (data.error || r.status), "err"); }
});
$("reset").addEventListener("click", ()=> fill({defaults: DEFAULTS, ...DEFAULTS, config_path: $("file").textContent, version: $("ver").textContent}));
loadAll();
</script></body></html>
"""


def page_html() -> str:
    """页面里的键名映射由 Python 侧注入，保证「网页能录」与「后台校验」用同一张表。"""
    return PAGE.replace("__KEYMAP_JSON__", json.dumps(keys.KEYMAP, ensure_ascii=False))


class Handler(BaseHTTPRequestHandler):
    server_version = "flash-input-web/" + __version__

    def log_message(self, fmt: str, *args: Any) -> None:  # 保持安静
        pass

    # ---------- 响应 ----------
    def _send(self, payload: bytes, code: int, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"), code, "application/json; charset=utf-8"
        )

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise config_mod.ConfigError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise config_mod.ConfigError("请求体必须是 JSON 对象")
        return data

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        app: dict[str, Any] = self.server.app  # type: ignore[attr-defined]
        if self.path in ("/", "/index.html"):
            return self._send(page_html().encode("utf-8"), 200, "text/html; charset=utf-8")
        if self.path == "/api/config":
            return self._json(_config_payload(app))
        if self.path == "/api/status":
            return self._json(_status_payload(app))
        if self.path == "/api/models":
            return self._json(_models_payload(app))
        self._json({"error": "404 Not Found"}, 404)

    def do_POST(self) -> None:
        app: dict[str, Any] = self.server.app  # type: ignore[attr-defined]
        try:
            if self.path == "/api/config":
                body = self._read_json()
                expected = body.pop("_revision", None)
                current = _revision(app.get("explicit_config"))
                if expected is not None and expected != current:
                    return self._json(
                        {
                            "error": "页面打开后配置又被改过（可能来自另一个标签页或命令行）",
                            "stale": True,
                        },
                        409,
                    )
                # 页面只提交自己认识的字段；用当前配置做底，避免把 asr.models
                # 这类页面上没有的键重置成默认值
                old_cfg = app["config"]
                old_trigger = (old_cfg.get("ocr") or {}).get("trigger")
                merged = config_mod._deep_merge(old_cfg, body)
                config_mod.save(merged, app.get("explicit_config"))
                _reload_app(app)
                binding = _sync_capture_binding(app, old_trigger)
                payload = _config_payload(app)
                payload["capture_binding"] = binding
                return self._json(payload)
            if self.path.startswith("/api/models/"):
                action = self.path.rsplit("/", 1)[-1]
                body = self._read_json()
                return self._json(_models_action(app, action, body))
            self._json({"error": "404 Not Found"}, 404)
        except config_mod.ConfigError as exc:
            self._json({"error": str(exc)}, 400)
        except OSError as exc:
            self._json({"error": f"写入失败：{exc}"}, 500)
        except Exception as exc:  # 兜底：别让设置页把服务带崩
            _get_log().exception("设置页请求失败：%s %s", self.command, self.path)
            self._json({"error": f"内部错误：{exc}"}, 500)


def _sync_capture_binding(app: dict[str, Any], old_trigger: str | None) -> str:
    """按新的 `ocr.trigger` 同步桌面快捷键；失败只返回说明，不影响保存。"""
    from . import shortcut

    cfg = app["config"]
    try:
        return shortcut.apply_trigger(
            str(cfg["ocr"].get("trigger") or "inject"),
            str(cfg["hotkey"].get("ocr_key") or ""),
            old_trigger=old_trigger,
        )
    except Exception as exc:  # gsettings 缺失/被改坏都不该让保存失败
        return f"桌面快捷键同步失败：{exc}"


def _value(cfg: dict[str, Any], dotted: str) -> Any:
    """按 `a.b.c` 取配置值（设置页锁定时用来比较）。"""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _revision(explicit: str | None) -> str:
    """配置文件内容的短哈希：页面保存时带回来，防止旧标签页静默覆盖新值。"""
    target = config_mod.path(explicit)
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return "new"
    except OSError:
        return "unreadable"


def _reload_app(app: dict[str, Any]) -> None:
    app["config"] = config_mod.load(app.get("explicit_config"))
    callback = app.get("on_reload")
    if callback:
        # 不要在 HTTP 响应里做重活
        threading.Thread(target=callback, args=(app["config"],), daemon=True).start()


def _models_payload(app: dict[str, Any]) -> dict[str, Any]:
    cfg = app["config"]
    state = app.get("on_engine_state")
    return {
        "ok": True,
        "models": asr_catalog.status(cfg),
        "registry": str(asr_catalog.registry_path()),
        "revision": _revision(app.get("explicit_config")),
        "engine": state() if callable(state) else None,  # 内存里真实在跑的引擎
    }


def _models_action(app: dict[str, Any], action: str, body: dict[str, Any]) -> dict[str, Any]:
    """安装 / 删除 / 启用 / 添加自定义模型 / 重建引擎。出错回 ok=false + error。"""
    cfg = app["config"]
    entries = asr_catalog.catalog(cfg)
    model_id = str(body.get("id") or "")
    try:
        if action == "rebuild":
            callback = app.get("on_rebuild")
            if not callable(callback):
                return {
                    "ok": False,
                    "error": "当前设置页没有连接守护进程，无法重建模型（finput run 下才可用）",
                }
            try:
                result = callback()
            except Exception as exc:  # 兜底：重建失败也要把原因回给页面
                return {"ok": False, "error": f"重建失败：{exc}"}
            return {**_models_payload(app), **result}
        if action == "add":
            entry = asr_catalog.register(
                cfg,
                str(body.get("name") or ""),
                str(body.get("dir") or ""),
                kind=str(body.get("kind") or "auto"),
            )
            asr_catalog.add_entry(entry, cfg)
            if body.get("select", True):
                cfg["asr"]["model"] = entry["id"]
            config_mod.save(cfg, app.get("explicit_config"))
            _reload_app(app)
            return {**_models_payload(app), "message": f"已登记 {entry['name']}（{entry['id']}）"}
        if model_id not in entries:
            return {"ok": False, "error": f"未知模型 {model_id}"}
        entry = entries[model_id]
        if action == "install":
            if asr_catalog.is_ready(entry):
                return {**_models_payload(app), "message": f"{entry['name']} 已就绪"}
            if not asr_catalog.install_async(entry):
                return {"ok": False, "error": "这个模型正在安装"}
            return _models_payload(app)
        if action == "select":
            if not asr_catalog.is_ready(entry):
                return {"ok": False, "error": f"{entry['name']} 还没下载，先点「安装」"}
            cfg["asr"]["model"] = model_id
            config_mod.save(cfg, app.get("explicit_config"))
            _reload_app(app)
            return {
                **_models_payload(app),
                "message": f"已切换到 {entry['name']}，下次识别时自动生效（也可点「重启模型」立即重建）",
            }
        if action == "remove":
            message = asr_catalog.remove(entry)
            if entry.get("custom"):
                asr_catalog.remove_entry(model_id, cfg)
                if cfg["asr"].get("models"):
                    cfg["asr"]["models"] = [
                        item for item in cfg["asr"]["models"] if item.get("id") != model_id
                    ]
                if cfg["asr"].get("model") == model_id:
                    cfg["asr"]["model"] = asr_catalog.DEFAULT_MODEL
                config_mod.save(cfg, app.get("explicit_config"))
                _reload_app(app)
            return {**_models_payload(app), "message": message}
        return {"ok": False, "error": f"未知操作 {action}"}
    except asr_catalog.CatalogError as exc:
        return {"ok": False, "error": str(exc)}
    except config_mod.ConfigError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"写入失败：{exc}"}


def _config_payload(app: dict[str, Any]) -> dict[str, Any]:
    explicit = app.get("explicit_config")
    target = config_mod.path(explicit)
    return {
        **app["config"],
        "config_path": str(target),
        "exists": target.exists(),
        "defaults": config_mod.DEFAULTS,
        "revision": _revision(explicit),
    }


def _status_payload(app: dict[str, Any]) -> dict[str, Any]:
    cfg = app["config"]
    info = env.describe()
    return {
        "版本": __version__,
        "会话": info["session_type"],
        "桌面 / 发行版": f"{info['desktop']} / {info['distro']}",
        "键盘设备": len(info["keyboard_devices"]),  # type: ignore[arg-type]
        "uinput 可写": info["uinput_writable"],
        "剪贴板": info["clipboard_cmd"],
        "会话总线": info["session_bus"],
        "systemd 托管": info["systemd_managed"],
        "可用内存 GB": info["mem_available_gb"],
        "ASR 常驻": cfg["asr"]["resident"],
        "OCR 常驻": cfg["ocr"]["resident"],
    }


def make_server(
    host: str,
    port: int,
    explicit_config: str | None = None,
    on_reload: Callable[[dict[str, Any]], None] | None = None,
    on_rebuild: Callable[[], dict[str, Any]] | None = None,
    on_engine_state: Callable[[], dict[str, Any]] | None = None,
) -> ThreadingHTTPServer:
    app = {
        "config": config_mod.load(explicit_config),
        "explicit_config": explicit_config,
        "on_reload": on_reload,
        "on_rebuild": on_rebuild,
        "on_engine_state": on_engine_state,
    }
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def serve_background(
    cfg: dict[str, Any] | None = None,
    explicit_config: str | None = None,
    on_reload: Callable[[dict[str, Any]], None] | None = None,
    on_rebuild: Callable[[], dict[str, Any]] | None = None,
    on_engine_state: Callable[[], dict[str, Any]] | None = None,
    host: str | None = None,
    port: int | None = None,
    quiet: bool = False,
) -> ThreadingHTTPServer | None:
    """后台线程提供设置页；端口被占等失败返回 None（调用方继续跑主功能）。"""
    loaded = cfg or config_mod.load(explicit_config)
    use_host = host or str(loaded["web"]["host"])
    use_port = int(loaded["web"]["port"] if port is None else port)
    try:
        server = make_server(use_host, use_port, explicit_config, on_reload, on_rebuild, on_engine_state)
    except OSError as exc:
        if not quiet:
            print(
                f"提示：设置页未能监听 {use_host}:{use_port}（{exc}），其余功能不受影响。",
                file=sys.stderr,
                flush=True,
            )
        return None
    threading.Thread(target=server.serve_forever, name="flash-input-web", daemon=True).start()
    bound = server.server_address[1]
    print(f"设置页：http://{use_host}:{bound}/", flush=True)
    return server


def serve_foreground(host: str, port: int, explicit_config: str | None = None, quiet: bool = False) -> int:
    """前台阻塞提供设置页，Ctrl-C 退出。"""
    try:
        server = make_server(host, port, explicit_config)
    except OSError as exc:
        print(f"无法监听 {host}:{port}（{exc}）", file=sys.stderr, flush=True)
        return 2
    print(f"设置页：http://{host}:{server.server_address[1]}/   （Ctrl-C 退出）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


__all__: Sequence[str] = ("PAGE", "Handler", "make_server", "serve_background", "serve_foreground")
