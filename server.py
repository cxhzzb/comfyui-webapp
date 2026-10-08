# -*- coding: utf-8 -*-
"""ComfyUI Web 前端后端：FastAPI + uvicorn，端口 8800，代理本机 8188 的 ComfyUI。"""
import base64
import ctypes
import ctypes.wintypes
import gc
import hashlib
import hmac
import json
import os
import random
import re
import secrets as _secrets
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import requests
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import BASE_DIR, cfg

WF_DIR = BASE_DIR / "workflows"
COMFY = "http://127.0.0.1:8188"
CLIENT_ID = "webapp-" + uuid.uuid4().hex[:12]
# ComfyUI 输出根目录（图片直接在根下，视频在 video/ 等 subfolder 下）
OUTPUT_ROOT = cfg.output_root

MODES_JSON = json.loads((WF_DIR / "modes.json").read_text(encoding="utf-8"))
# ---------------- 车道池（多 ComfyUI 后端，v1：自动调度 + 手动选车道） ----------------
LANES_FILE = BASE_DIR / "lanes.json"
TASKS_FILE = BASE_DIR / "tasks.json"
SYNC_FILE = BASE_DIR / "lane_sync_state.json"
_lanes_lock = threading.Lock()


def _load_lanes():
    try:
        raw = json.loads(LANES_FILE.read_text(encoding="utf-8"))
        cfgs = raw.get("lanes") or []
    except Exception:
        cfgs = []
    if not any(c.get("id") == "local" for c in cfgs):
        cfgs.insert(0, {"id": "local", "name": "本地 4070S", "base_url": COMFY, "enabled": True, "models": {}, "params": {}})
    lanes = []
    for c in cfgs:
        lid = str(c.get("id", "lane"))
        lanes.append({
            "id": lid,
            "name": str(c.get("name", lid)),
            "base_url": str(c.get("base_url", COMFY)),
            "enabled": bool(c.get("enabled", True)),
            "models": dict(c.get("models") or {}),
            "model_options": dict(c.get("model_options") or {}),
            "params": dict(c.get("params") or {}),
            "client_id": f"webapp-lane-{lid}-" + uuid.uuid4().hex[:8],
            "online": False,
            "queue_running": 0,
            "queue_pending": 0,
        })
    return lanes


LANES = _load_lanes()


def _lane(lid):
    for l in LANES:
        if l["id"] == lid:
            return l
    return None


def _lane_health_loop():
    """每 5s 轮询各启用车道 /queue，维护在线状态与队列深度。"""
    while True:
        for l in LANES:
            if not l["enabled"]:
                continue
            try:
                r = requests.get(f"{l['base_url']}/queue", timeout=8)
                if r.status_code == 200:
                    q = r.json()
                    with _lanes_lock:
                        l["online"] = True
                        l["queue_running"] = len(q.get("queue_running") or [])
                        l["queue_pending"] = len(q.get("queue_pending") or [])
                else:
                    with _lanes_lock:
                        l["online"] = False
            except Exception:
                with _lanes_lock:
                    l["online"] = False
        time.sleep(5)


def _pick_lane():
    with _lanes_lock:
        cands = [l for l in LANES if l["enabled"] and l["online"]]
    if not cands:
        loc = _lane("local")
        if loc is not None:
            return loc
        raise HTTPException(503, "没有可用车道")
    order = {l["id"]: i for i, l in enumerate(LANES)}
    cands.sort(key=lambda l: (l["queue_running"] + l["queue_pending"], order.get(l["id"], 999)))
    return cands[0]


def _resolve_lane(choice):
    if not choice or choice == "auto":
        return _pick_lane()
    l = _lane(choice)
    if l is None:
        raise HTTPException(400, f"车道不存在：{choice}")
    if not l["enabled"]:
        raise HTTPException(400, f"车道已停用：{l['name']}")
    if not l["online"]:
        raise HTTPException(400, f"车道离线：{l['name']}（请检查实例与 SSH 隧道）")
    return l


def _load_tasks():
    try:
        return json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_tasks(tasks):
    tmp = TASKS_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(TASKS_FILE))
    except Exception:
        pass


_tasks = _load_tasks()
_tasks_lock = threading.Lock()


def _register_task(prompt_id, lane_id, mode):
    with _tasks_lock:
        _tasks[prompt_id] = {"lane": lane_id, "mode": mode, "created_at": time.time()}
        if len(_tasks) > 2000:
            keep = dict(list(_tasks.items())[-1000:])
            _tasks.clear()
            _tasks.update(keep)
        _save_tasks(_tasks)


def _task_lane(prompt_id):
    with _tasks_lock:
        t = _tasks.get(prompt_id)
    if t:
        l = _lane(t.get("lane"))
        if l is not None:
            return l
    return _lane("local")


def _load_sync_state():
    try:
        return json.loads(SYNC_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_sync_state(st):
    try:
        SYNC_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _sync_lane_outputs(lane, prompt_id=None):
    """把云端已完成任务输出下载到本地 OUTPUT_ROOT/{lane_id}/...（URL subfolder 前缀同）。"""
    if lane["id"] == "local":
        return None
    try:
        r = requests.get(f"{lane['base_url']}/history", timeout=20)
        if r.status_code != 200:
            return "云端 history 获取失败"
        history = r.json() or {}
    except Exception as e:
        return str(e)
    st = _load_sync_state()
    synced = set(st.get(lane["id"], []))
    err = None
    for pid, entry in history.items():
        if prompt_id is not None and pid != prompt_id:
            continue
        for node_out in (entry.get("outputs") or {}).values():
            for key in ("videos", "gifs", "images"):
                for item in node_out.get(key) or []:
                    if not isinstance(item, dict) or "filename" not in item:
                        continue
                    sub = item.get("subfolder", "")
                    dest = OUTPUT_ROOT / lane["id"] / sub
                    dest.mkdir(parents=True, exist_ok=True)
                    fp = dest / item["filename"]
                    try:
                        v = requests.get(
                            f"{lane['base_url']}/view",
                            params={"filename": item["filename"], "subfolder": sub, "type": item.get("type", "output")},
                            timeout=300,
                        )
                        if v.status_code == 200:
                            tmp = fp.with_suffix(fp.suffix + ".tmp")
                            tmp.write_bytes(v.content)
                            os.replace(str(tmp), str(fp))
                    except Exception as e:
                        err = err or f"{pid}: {e}"
        if prompt_id is None:
            synced.add(pid)
    st[lane["id"]] = list(synced)[-500:]
    _save_sync_state(st)
    return err


def _sync_loop(lane):
    while True:
        if lane["enabled"]:
            _sync_lane_outputs(lane)
        time.sleep(2)


RATIO_OPTIONS = [
    "1:1 (Square)",
    "2:3 (Portrait Photo)",
    "3:2 (Photo)",
    "3:4 (Portrait Standard)",
    "4:3 (Standard)",
    "9:16 (Portrait Widescreen)",
    "16:9 (Widescreen)",
    "21:9 (Ultrawide)",
]

# ---------------- 模型文件管理（管理员） ----------------
# 各分类的搜索根目录（ComfyUI 会同时扫描共享目录与安装目录）
MODEL_CATS = {
    "unet": {
        "label": "主模型 (UNET)",
        "roots": [
            cfg.shared_models_root / "diffusion_models",
            cfg.models_root / "diffusion_models",
        ],
        "exts": (".safetensors", ".ckpt", ".gguf", ".pt", ".pth", ".bin"),
    },
    "clip": {
        "label": "CLIP",
        "roots": [
            cfg.shared_models_root / "text_encoders",
            cfg.models_root / "text_encoders",
        ],
        "exts": (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"),
    },
    "lora": {
        "label": "LoRA",
        "roots": [
            cfg.models_root / "loras",
            cfg.shared_models_root / "loras",
        ],
        "exts": (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"),
    },
}
MODELS_STATE_FILE = BASE_DIR / "models_state.json"  # 启停名单（禁用列表）
MODELS_TRASH_DIR = cfg.models_trash_dir  # 回收站（可恢复）


def _load_models_state():
    try:
        return json.loads(MODELS_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_models_state(state):
    try:
        MODELS_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _scan_models(cat):
    """递归扫描分类下所有模型文件，返回相对路径（反斜杠，与 ComfyUI 模板一致）"""
    cfg = MODEL_CATS[cat]
    files = []
    for root in cfg["roots"]:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.name.startswith(".uploading-"):
                continue
            if p.suffix.lower() not in cfg["exts"]:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({
                "name": str(p.relative_to(root)),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "root": str(root),
            })
    files.sort(key=lambda f: f["name"].lower())
    return files


def _disabled_set(cat):
    st = _load_models_state()
    return set(st.get("disabled", {}).get(cat, []))


def _enabled_model_paths(cat):
    """启用的模型相对路径列表（生成下拉框/校验用）"""
    dis = _disabled_set(cat)
    return [f["name"] for f in _scan_models(cat) if f["name"] not in dis]


def _find_model_file(cat, name):
    cfg = MODEL_CATS[cat]
    for root in cfg["roots"]:
        p = root / name
        if p.is_file():
            return root, p
    return None, None


def _move_to_trash(cat, name, root):
    """把模型文件移入回收站（带原路径元数据，可恢复）"""
    d = MODELS_TRASH_DIR / cat / name
    d.parent.mkdir(parents=True, exist_ok=True)
    src = root / name
    if src.exists():
        os.replace(str(src), str(d))
    meta = Path(str(d) + ".meta.json")
    meta.write_text(json.dumps({"root": str(root), "name": name}, ensure_ascii=False), encoding="utf-8")

# 增强任务的「任务类型」映射（enhance.json 节点 253 的合法取值）
TASK_TYPE_MAP = {
    "t2v": "文生视频(T2VA)",
    "i2v": "首帧图生视频(I2VA)",
    "fl2v": "首尾帧视频(FL2VA)",
    "r2v": "全参考模式(Reference to Video)",
    "quadview": "首帧图生视频(I2VA)",
}

# 前端展示用模式配置（注入点取自 workflows/modes.json）
MODES = {
    "quadview": {
        "id": "quadview",
        "name": "四视图生成（Krea2）",
        "description": "根据人物参考图生成特写、正面、侧面、背面四视图角色设定图",
        "kind": "image",
        "prompt_mode": "readonly",
    },
    "qwen_t2i": {
        "id": "qwen_t2i",
        "name": "文生图（Qwen Image 2.1）",
        "description": "纯文本描述生成图片，支持画面比例与分辨率调节",
        "kind": "image",
        "prompt_mode": "editable",
        "local_only": True,  # 云端无 Qwen 模型，强制本地车道
    },
    "qwen_i2i": {
        "id": "qwen_i2i",
        "name": "图像编辑（Qwen Image 2.1）",
        "description": "按提示词编辑参考图（换装/改风格等），参考图可选、上传几张生效几张；不传图则退化为文生图",
        "kind": "image",
        "prompt_mode": "editable",
        "local_only": True,  # 云端无 Qwen 模型，强制本地车道
    },
    "t2v": {
        "id": "t2v",
        "name": "MiniMaxH3 文生视频",
        "description": "纯文本描述生成带音效的视频",
        "kind": "video",
        "prompt_mode": "editable",
    },
    "i2v": {
        "id": "i2v",
        "name": "图生视频",
        "description": "以首帧图片为起点，按提示词生成视频",
        "kind": "video",
        "prompt_mode": "editable",
    },
    "fl2v": {
        "id": "fl2v",
        "name": "首尾帧视频",
        "description": "给定首帧与尾帧图片，生成中间过渡视频",
        "kind": "video",
        "prompt_mode": "editable",
    },
    "r2v": {
        "id": "r2v",
        "name": "多参视频",
        "description": "参考多张图片生成视频（Reference to Video）",
        "kind": "video",
        "prompt_mode": "editable",
    },
    "music": {
        "id": "music",
        "name": "想把我唱你听",
        "description": "选曲风、AI 写词，生成带人声的完整歌曲（MiniMax Music 3）",
        "kind": "music",
        "prompt_mode": "music",
    },
    "mv": {
        "id": "mv",
        "name": "MV 工坊",
        "description": "五步向导：选歌 → 段落规划 → 逐段配图 → 批量生成 → 合成 MV",
        "kind": "mv",
        "prompt_mode": "mv",
    },
}


# ---------------- 音乐模式（MiniMax Music 3） ----------------

MUSIC_PRESET_DIR = (
    Path(os.environ.get("COMFY_HOME", str(cfg.comfyui_root)))
    / "custom_nodes" / "ComfyUI-Music3-Presets" / "presets"
)
MUSIC_GENDERS = ["自动（用预设原本人声）", "女声", "男声"]
MUSIC_LENGTHS = ["短 · 约 60 秒", "标准 · 约 120 秒", "完整 · 180 秒以上"]
MUSIC_CHORUS = ["末次变奏（推荐）", "完全相同", "每次不同"]
MUSIC_LANGS = ["中文", "英文", "中文为主·英文副歌"]


def _music_styles():
    """扫描曲风预设目录，返回下拉显示名列表（与节点包同一套规则：去掉数字前缀）。"""
    names = []
    try:
        for name in sorted(os.listdir(MUSIC_PRESET_DIR)):
            if not name.lower().endswith(".txt"):
                continue
            label = os.path.splitext(name)[0]
            if "_" in label:
                head, rest = label.split("_", 1)
                if head.isdigit():
                    label = rest
            names.append(label)
    except OSError:
        pass
    return names


def _load_template(mode):
    return json.loads((WF_DIR / MODES_JSON[mode]["file"]).read_text(encoding="utf-8"))


def _random_seed():
    return random.randint(0, 2**48 - 1)


# ---------------- ComfyUI 实时进度（WebSocket 长连，断线自动重连） ----------------

_progress = {}  # prompt_id -> {"value": int, "max": int}
_progress_lock = threading.Lock()
_task_start = {}  # prompt_id -> 首次观察到 running 的 epoch 秒（用于"已用时间"）

_WS_DEBUG = os.environ.get("WS_DEBUG") == "1"  # 调试用：记录 WS 消息类型到 _scratch/ws_debug.log


def _ws_debug_log(line):
    try:
        with open(BASE_DIR / "_scratch" / "ws_debug.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + line + "\n")
    except Exception:
        pass


def _progress_ws_loop(lane):
    """每个车道一条 WebSocket 长连，进度按 (lane_id, prompt_id) 键控。"""
    import websocket  # websocket-client

    base = lane["base_url"]
    cid = lane["client_id"]
    lid = lane["id"]
    # base_url 是 http(s)://，WebSocket 需要 ws(s)://
    url = base.replace("http", "ws", 1) + f"/ws?clientId={cid}"
    while True:
        try:
            ws = websocket.create_connection(url, timeout=30)
            if _WS_DEBUG:
                _ws_debug_log(f"lane={lid} WS connected")
            while True:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue  # 空闲超时不断线，继续等
                if not isinstance(raw, str):  # 预览图等二进制帧直接跳过
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                data = msg.get("data") or {}
                if _WS_DEBUG:
                    _ws_debug_log(f"type={msg.get('type')} keys={list(data.keys())}")
                pid = data.get("prompt_id")
                if not pid:
                    continue
                key = f"{lid}:{pid}"
                mtype = msg.get("type")
                if mtype == "progress":
                    with _progress_lock:
                        if len(_progress) > 200:  # 防止无限增长
                            _progress.clear()
                        _progress[key] = {
                            "value": data.get("value", 0),
                            "max": data.get("max", 0),
                        }
                elif mtype == "progress_state":
                    # ComfyUI 新版（>=0.2.x）改为整包推送节点进度：
                    # data = {"prompt_id": ..., "nodes": {node_id: {value, max, state, ...}}}
                    # 取正在运行且 max 最大的节点（采样步数节点）
                    best = None
                    for n in (data.get("nodes") or {}).values():
                        if n.get("state") != "running":
                            continue
                        mx = n.get("max") or 0
                        if mx > 1 and (best is None or mx > best[1]):
                            best = (n.get("value", 0), mx)
                    if best:
                        with _progress_lock:
                            if len(_progress) > 200:  # 防止无限增长
                                _progress.clear()
                            _progress[key] = {"value": best[0], "max": best[1]}
                elif mtype in ("execution_success", "execution_error", "execution_interrupted"):
                    with _progress_lock:
                        _progress.pop(key, None)
        except Exception as e:  # noqa: BLE001 - 断线重连
            if _WS_DEBUG:
                _ws_debug_log(f"lane={lid} WS error: {e!r}")
            time.sleep(3)


def _get_progress(lane, prompt_id):
    lid = lane["id"]
    with _progress_lock:
        p = _progress.get(f"{lid}:{prompt_id}")
    if not p or not p.get("max"):
        return None
    return {
        "value": p["value"],
        "max": p["max"],
        "percent": round(p["value"] / p["max"] * 100, 1),
    }


def _elapsed_seconds(entry):
    """从 history status.messages 计算执行耗时（execution_start→execution_success，不含排队）。"""
    start = end = None
    for name, payload in (entry.get("status") or {}).get("messages") or []:
        if name == "execution_start":
            start = payload.get("timestamp")
        elif name == "execution_success":
            end = payload.get("timestamp")
    if start and end:
        return round((end - start) / 1000.0, 1)
    return None


def _comfy_post_prompt(prompt, lane=None):
    """提交 prompt 到指定车道，校验失败时把 node_errors 透传为 400。"""
    lane = lane or _lane("local")
    base = lane["base_url"]
    cid = lane["client_id"]
    name = lane["name"]
    try:
        r = requests.post(
            f"{base}/prompt",
            json={"prompt": prompt, "client_id": cid},
            timeout=30,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"无法连接 ComfyUI（{name}）：{e}")
    if r.status_code != 200:
        try:
            detail = r.json()
        except ValueError:
            detail = r.text
        raise HTTPException(400, {"message": "ComfyUI 校验失败", "detail": detail})
    return r.json()["prompt_id"]


def _get_history(prompt_id, lane=None):
    lane = lane or _lane("local")
    base = lane["base_url"]
    name = lane["name"]
    try:
        r = requests.get(f"{base}/history/{prompt_id}", timeout=15)
    except requests.RequestException as e:
        raise HTTPException(502, f"无法连接 ComfyUI（{name}）：{e}")
    if r.status_code != 200:
        return None
    return (r.json() or {}).get(prompt_id)


_meta_cache = {}


def _probe_meta(filename, subfolder, kind):
    """探测输出文件元信息：视频有时长/宽高/帧率，图片只有宽高；外加大小和目录。

    用 imageio-ffmpeg 自带的 ffmpeg 二进制解析（`ffmpeg -i` 的 stderr）。
    """
    key = (subfolder or "") + "/" + filename
    if key in _meta_cache:
        return _meta_cache[key]
    path = (OUTPUT_ROOT / subfolder / filename) if subfolder else (OUTPUT_ROOT / filename)
    if not path.exists():
        return None
    meta = {"size_bytes": path.stat().st_size, "dir": str(path.parent)}
    try:
        import re
        import subprocess

        import imageio_ffmpeg

        r = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        out = r.stderr or ""
        v = re.search(r"Stream #\S*.*?Video:.*?(\d{2,5})x(\d{2,5})", out)
        if v:
            meta["width"], meta["height"] = int(v.group(1)), int(v.group(2))
        if kind in ("video", "audio"):
            m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
            if m:
                meta["duration"] = round(
                    int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 2)
            f = re.search(r"Video:.*?([\d.]+) fps", out)
            if f:
                meta["fps"] = float(f.group(1))
    except Exception:  # noqa: BLE001 - 探测失败不阻断主流程
        pass
    if len(_meta_cache) > 500:
        _meta_cache.clear()
    _meta_cache[key] = meta
    return meta


def _history_outputs(entry, lane=None):
    """从 history 条目提取输出文件，兼容 images/videos/gifs 等 key。

    注意：SaveVideo 的 mp4 也出现在 "images" key 下（带 animated 标记），
    因此按文件扩展名判断 kind，而不是按 key。
    云端车道（lane.id != local）输出已回传到 OUTPUT_ROOT/{lane_id}/...，
    这里把 URL 的 subfolder 前缀加上 lane_id 使 /api/file、/api/thumb、_probe_meta 复用本地逻辑。
    """
    results = []
    seen = set()
    prefix = (lane["id"] + "/") if lane and lane["id"] != "local" else ""
    for node_out in (entry.get("outputs") or {}).values():
        for key in ("videos", "gifs", "images", "audio"):
            for item in node_out.get(key) or []:
                if not isinstance(item, dict) or "filename" not in item:
                    continue
                if item["filename"] in seen:
                    continue
                seen.add(item["filename"])
                ext = item["filename"].rsplit(".", 1)[-1].lower()
                if ext in ("mp4", "webm", "mov", "mkv", "avi", "gif", "webp"):
                    kind = "video"
                elif ext in ("mp3", "flac", "wav", "opus", "m4a", "ogg"):
                    kind = "audio"
                else:
                    kind = "image"
                sub_in = item.get("subfolder", "").replace("\\", "/")
                sub_out = (prefix + sub_in).rstrip("/") if prefix else sub_in
                qs = urlencode(
                    {
                        "filename": item["filename"],
                        "subfolder": sub_out,
                        "type": item.get("type", "output"),
                    }
                )
                out_item = {"kind": kind, "url": f"/api/file?{qs}"}
                if kind != "audio":
                    tqs = urlencode(
                        {
                            "filename": item["filename"],
                            "subfolder": sub_out,
                        }
                    )
                    out_item["thumb"] = f"/api/thumb?{tqs}"
                if item.get("type", "output") == "output":
                    meta = _probe_meta(item["filename"], sub_out, kind)
                    if meta:
                        out_item["meta"] = meta
                results.append(out_item)
    return results


def _wait_done(prompt_id, timeout=300.0, interval=2.0):
    """轮询 history 直到完成，返回 (entry, error_msg)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = _get_history(prompt_id)
        if entry is not None:
            status = entry.get("status") or {}
            if status.get("completed") or status.get("status_str") == "success":
                return entry, None
            if status.get("status_str") == "error":
                msgs = [
                    m[1].get("exception_message", str(m[1]))
                    for m in status.get("messages", [])
                    if m[0] == "execution_error"
                ]
                return entry, "; ".join(msgs) or "执行出错"
        time.sleep(interval)
    return None, "等待超时"


def _get_queue_lane(lane):
    base = lane["base_url"]
    try:
        r = requests.get(f"{base}/queue", timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"queue_running": [], "queue_pending": []}


def _push_input_to_lane(lane, filename):
    """把已上传到本地 ComfyUI input 的文件字节，重推到目标车道。"""
    base = lane["base_url"]
    try:
        r = requests.get(f"{COMFY}/view", params={"filename": filename, "subfolder": "", "type": "input"}, timeout=120)
        if r.status_code != 200:
            return False
        up = requests.post(
            f"{base}/upload/image",
            files={"image": (filename, r.content, "application/octet-stream")},
            data={"overwrite": "true"},
            timeout=120,
        )
        return up.status_code == 200
    except Exception:
        return False


app = FastAPI(title="ComfyUI Web 前端")


# ---------------- 模式配置 ----------------

@app.get("/api/modes")
def api_modes(request: Request):
    out = []
    for mode_id in ("quadview", "qwen_t2i", "qwen_i2i", "t2v", "i2v", "fl2v", "r2v"):
        info = MODES[mode_id]
        spec = MODES_JSON[mode_id]
        tpl = _load_template(mode_id)
        entry = {
            "id": mode_id,
            "name": info["name"],
            "description": info["description"],
            "kind": info["kind"],
            "prompt_mode": info.get("prompt_mode", "editable"),
            "image_slots": [img["label"] for img in spec.get("images", [])],
        }
        if info.get("local_only"):
            entry["local_only"] = True
        if mode_id in QWEN_ENHANCE_MODES:
            # Qwen 生图模式：提示词优化可选「文生图/图生图」增强模式（决定用哪个 PE 模型）
            entry["enhance_modes"] = {
                "options": ["文生图", "图生图"],
                "default": QWEN_ENHANCE_MODES[mode_id],
            }
        if entry["prompt_mode"] == "readonly":
            # 只读模式：展示模板内置提示词，不接受用户输入
            entry["builtin_prompt"] = tpl[spec["prompt"]["node"]]["inputs"][spec["prompt"]["input"]]
        if "seconds" in spec:
            sec_node = spec["seconds"]
            entry["seconds"] = {
                "default": tpl[sec_node["node"]]["inputs"][sec_node["input"]],
                "min": 5,
                "max": 20,
            }
        if "ratio" in spec:
            ratio_node = spec["ratio"]
            entry["aspect_ratio"] = {
                "default": tpl[ratio_node["node"]]["inputs"][ratio_node["input"]],
                "options": RATIO_OPTIONS,
            }
        if spec.get("optional_slots"):
            # 可选上传槽：只暴露 label/kind，接线信息留在后端
            entry["optional_slots"] = [
                {"label": s["label"], "kind": s["kind"]} for s in spec["optional_slots"]
            ]
        adv = spec.get("advanced")
        if adv:
            # 高级参数控件配置，默认值从模板现读；inject_only 注入点只做车道预设替换，不暴露控件
            acfg = {}
            if "steps" in adv:
                s = adv["steps"]
                acfg["steps"] = {
                    "min": s["min"], "max": s["max"],
                    "default": tpl[s["node"]]["inputs"][s["input"]],
                }
            if "megapixels" in adv:
                s = adv["megapixels"]
                acfg["megapixels"] = {
                    "min": s["min"], "max": s["max"], "step": s["step"],
                    "default": tpl[s["node"]]["inputs"][s["input"]],
                }
            if "clip" in adv and not adv["clip"].get("inject_only"):
                s = adv["clip"]
                opts = _enabled_model_paths("clip")
                default = tpl[s["node"]]["inputs"][s["input"]]
                if default not in opts:
                    opts.insert(0, default)
                acfg["clip"] = {
                    "options": opts,
                    "default": default,
                }
            if "lora" in adv and not adv["lora"].get("inject_only"):
                s = adv["lora"]
                opts = _enabled_model_paths("lora")
                default = tpl[s["node"]]["inputs"][s["input"]]
                if default not in opts:
                    opts.insert(0, default)
                acfg["lora"] = {
                    "options": opts,
                    "default": default,
                    "strength": {
                        "min": 0, "max": 1.5, "step": 0.05,
                        "default": tpl[s["node"]]["inputs"][s["strength_input"]],
                    },
                }
            if "lora_strength" in adv:
                s = adv["lora_strength"]
                acfg["lora_strength"] = {
                    "min": 0, "max": 1.5, "step": 0.05,
                    "default": tpl[s["node"]]["inputs"][s["input"]],
                }
            if "unet" in adv and not adv["unet"].get("inject_only"):
                s = adv["unet"]
                opts = _enabled_model_paths("unet")
                default = tpl[s["node"]]["inputs"][s["input"]]
                if default not in opts:
                    opts.insert(0, default)
                acfg["unet"] = {
                    "options": opts,
                    "default": default,
                }
            entry["advanced"] = acfg
        out.append(entry)
    # 音乐模式：配置结构与视频模式差异大，单独组条目
    mspec = MODES_JSON["music"]
    mtpl = _load_template("music")
    out.append({
        "id": "music",
        "name": MODES["music"]["name"],
        "description": MODES["music"]["description"],
        "kind": "music",
        "prompt_mode": "music",
        "image_slots": [],
        "styles": _music_styles(),
        "genders": MUSIC_GENDERS,
        "duration": {
            "default": mtpl[mspec["duration"]["node"]]["inputs"][mspec["duration"]["input"]],
            "min": 30,
            "max": 300,
        },
        "lyrics_opts": {
            "lengths": MUSIC_LENGTHS,
            "chorus": MUSIC_CHORUS,
            "langs": MUSIC_LANGS,
        },
        "advanced": {
            "steps": {
                "min": mspec["advanced"]["steps"]["min"],
                "max": mspec["advanced"]["steps"]["max"],
                "default": mtpl[mspec["advanced"]["steps"]["node"]]["inputs"][mspec["advanced"]["steps"]["input"]],
            },
        },
    })
    out.append({
        "id": "mv",
        "name": MODES["mv"]["name"],
        "description": MODES["mv"]["description"],
        "kind": "mv",
        "prompt_mode": "mv",
        "image_slots": [],
    })
    # HYPIT 工坊：能驱动本机 agent，只对本机直连客户端展示卡片
    if _is_loopback_direct(request):
        out.append({
            "id": "hypit",
            "name": "HYPIT 工坊",
            "description": "Codex 导演驱动的本地出片工作台：对话即导演，项目文件即记忆（仅本机可用）",
            "kind": "hypit",
            "prompt_mode": "hypit",
            "image_slots": [],
        })
    return {"modes": out}


# ---------------- 上传 ----------------

INPUT_ROOT = cfg.input_root


class ToInputReq(BaseModel):
    filename: str
    subfolder: str = ""


@app.post("/api/file/to_input")
def api_file_to_input(req: ToInputReq):
    """把输出目录的文件复制进 ComfyUI input 目录（LoadImage/LoadVideo 只认 input）。
    供前端「右键 → 引用到…」使用；幂等（同名已存在则直接复用）。"""
    src = (OUTPUT_ROOT / req.subfolder / req.filename).resolve()
    if not str(src).startswith(str(OUTPUT_ROOT.resolve())) or not src.is_file():
        raise HTTPException(404, "输出文件不存在")
    INPUT_ROOT.mkdir(parents=True, exist_ok=True)
    dest = INPUT_ROOT / ("ref_" + os.path.basename(req.filename))
    if not dest.exists():
        shutil.copy2(str(src), str(dest))
    return {"name": dest.name}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    try:
        r = requests.post(
            f"{COMFY}/upload/image",
            files={"image": (file.filename or "upload.png", data, file.content_type or "application/octet-stream")},
            data={"overwrite": "true"},
            timeout=120,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"无法连接 ComfyUI：{e}")
    if r.status_code != 200:
        raise HTTPException(502, f"ComfyUI 上传失败：{r.text[:500]}")
    return r.json()


# ---------------- 提示词增强（异步任务） ----------------

class EnhanceReq(BaseModel):
    mode: str
    text: str
    seconds: float | None = None
    images: dict | None = None  # qwen_i2i 增强时附带的参考图（label -> ComfyUI 文件名）
    enhance_mode: str | None = None  # Qwen 增强的「任务模式」：文生图 / 图生图（None=按模式默认）
    route: str = "local"  # local=线下本地模型 / api=线上 API（llm_api_config.json）
    llm_model: str | None = None  # 线下时指定本地 GGUF（白名单校验，空=模板默认）


_enhance_tasks = {}
_enhance_lock = threading.Lock()


def _run_enhance(task_id, mode, text, seconds, route="local", llm_model=None):
    try:
        spec = MODES_JSON["enhance"]
        tpl = _load_template("enhance")
        tpl[spec["prompt"]["node"]]["inputs"][spec["prompt"]["input"]] = text
        tpl[spec["task_type"]["node"]]["inputs"][spec["task_type"]["input"]] = TASK_TYPE_MAP[mode]
        if route == "api":
            _apply_llm_route_to_node(tpl, spec["task_type"]["node"], "API增强")
        else:
            m = _llm_pick_local(llm_model)
            if m:  # 节点 254 是 QwenTE_ModelLoader
                tpl["254"]["inputs"]["主模型"] = m
                tpl["254"]["inputs"]["模型系列"] = _llm_series_for(m)
                # 主模型与 mmproj 必须同系配套；H3 增强是纯文本模式，无配套就置「无」
                tpl["254"]["inputs"]["视觉投影mmproj"] = _llm_mmproj_for(m) or "无"
        if seconds is not None and "seconds" in spec:
            # 节点 253「视频时长」：FLOAT, 1.0-150.0, step 0.5
            sec = min(150.0, max(1.0, float(seconds)))
            tpl[spec["seconds"]["node"]]["inputs"][spec["seconds"]["input"]] = sec
        tpl[spec["seed"]["node"]]["inputs"][spec["seed"]["input"]] = _random_seed()
        prompt_id = _comfy_post_prompt(tpl)
        entry, err = _wait_done(prompt_id, timeout=300.0)
        if err:
            raise RuntimeError(err)
        text_node = spec["outputs"]["text_node"]
        enhanced = entry["outputs"][text_node]["text"][0]
        with _enhance_lock:
            _enhance_tasks[task_id] = {"state": "done", "enhanced_text": enhanced}
    except HTTPException as e:
        with _enhance_lock:
            _enhance_tasks[task_id] = {"state": "error", "error": str(e.detail)}
    except Exception as e:  # noqa: BLE001
        with _enhance_lock:
            _enhance_tasks[task_id] = {"state": "error", "error": str(e)}


# Qwen Image 2.1 生图模式的增强（走 TE_Qwen_Image_2_1_Prompt_Enhancer，模板 workflows/qwen_enhance.json）
QWEN_ENHANCE_MODES = {"qwen_t2i": "文生图", "qwen_i2i": "图生图"}


def _run_qwen_enhance(task_id, mode, text, images, enhance_mode=None, route="local", llm_model=None):
    try:
        tpl = json.loads((WF_DIR / "qwen_enhance.json").read_text(encoding="utf-8"))
        enh = tpl["1"]["inputs"]
        enh["输入提示词"] = text
        # 任务模式：前端可覆盖（文生图/图生图），否则按模式默认
        enh["任务模式"] = (
            enhance_mode if enhance_mode in ("文生图", "图生图") else QWEN_ENHANCE_MODES[mode]
        )
        enh["seed"] = _random_seed()
        # 线上 API：增强器走 API，不看参考图（PE 视觉链是本地专属），跳过图片接线
        if route == "api":
            _apply_llm_route_to_node(tpl, "1", "API")
            images = None
        else:
            m = _llm_pick_local(llm_model)
            if m:
                enh["主模型"] = m
                mp = _llm_mmproj_for(m)
                if mp:  # 主模型与 mmproj 必须同系配套，否则 mtmd context 加载失败
                    enh["mmproj"] = mp
        # 参考图（可选）：LoadImage → ImageScaleToTotalPixels → 增强器的 图片/图片2
        # 顺序按 qwen_i2i 的 optional_slots 定义，保证 参考图1→图片、参考图2→图片2
        slot_labels = [
            s["label"] for s in MODES_JSON.get("qwen_i2i", {}).get("optional_slots", [])
        ]
        img_keys = ["图片", "图片2"]
        idx = 0
        for label in slot_labels:
            fname = (images or {}).get(label)
            if not fname or idx >= len(img_keys):
                continue
            lid, sid = str(10 + idx * 2), str(11 + idx * 2)
            tpl[lid] = {"class_type": "LoadImage", "inputs": {"image": fname}}
            tpl[sid] = {
                "class_type": "ImageScaleToTotalPixels",
                "inputs": {
                    "upscale_method": "lanczos",
                    "megapixels": 1.0,
                    "resolution_steps": 32,
                    "image": [lid, 0],
                },
            }
            enh[img_keys[idx]] = [sid, 0]
            idx += 1
        prompt_id = _comfy_post_prompt(tpl)
        entry, err = _wait_done(prompt_id, timeout=600.0)
        if err:
            raise RuntimeError(err)
        enhanced = entry["outputs"]["2"]["text"][0]
        with _enhance_lock:
            _enhance_tasks[task_id] = {"state": "done", "enhanced_text": enhanced}
    except HTTPException as e:
        with _enhance_lock:
            _enhance_tasks[task_id] = {"state": "error", "error": str(e.detail)}
    except Exception as e:  # noqa: BLE001
        with _enhance_lock:
            _enhance_tasks[task_id] = {"state": "error", "error": str(e)}


@app.post("/api/enhance")
def api_enhance(req: EnhanceReq):
    if req.mode in QWEN_ENHANCE_MODES:
        if not req.text.strip():
            raise HTTPException(400, "提示词不能为空")
        task_id = uuid.uuid4().hex
        with _enhance_lock:
            _enhance_tasks[task_id] = {"state": "running"}
        threading.Thread(
            target=_run_qwen_enhance,
            args=(task_id, req.mode, req.text, req.images or {}, req.enhance_mode, req.route, req.llm_model),
            daemon=True,
        ).start()
        return {"task_id": task_id}
    if req.mode not in TASK_TYPE_MAP:
        raise HTTPException(400, f"未知模式：{req.mode}")
    if not req.text.strip():
        raise HTTPException(400, "提示词不能为空")
    task_id = uuid.uuid4().hex
    with _enhance_lock:
        _enhance_tasks[task_id] = {"state": "running"}
    threading.Thread(target=_run_enhance, args=(task_id, req.mode, req.text, req.seconds, req.route, req.llm_model), daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/enhance/{task_id}")
def api_enhance_result(task_id: str):
    with _enhance_lock:
        task = _enhance_tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


# ---------------- AI 写词（异步任务，复用 enhance 的模式） ----------------

class LyricsReq(BaseModel):
    theme: str = ""
    style: str = ""
    language: str = "中文"
    length: str = "标准 · 约 120 秒"
    chorus: str = "末次变奏（推荐）"
    keywords: str = ""
    existing: str = ""  # 非空则走「补全/洗稿现有歌词」模式
    seed: int = -1


_lyrics_tasks = {}
_lyrics_lock = threading.Lock()


def _run_lyrics(task_id, req: LyricsReq):
    try:
        tpl = json.loads((WF_DIR / "music_lyrics.json").read_text(encoding="utf-8"))
        wb = tpl["2"]["inputs"]
        wb["模式"] = "补全/洗稿现有歌词" if req.existing.strip() else "从零写歌词"
        wb["主题"] = req.theme
        wb["曲风情绪"] = req.style
        wb["歌词语言"] = req.language if req.language in MUSIC_LANGS else "中文"
        wb["歌曲长度"] = req.length if req.length in MUSIC_LENGTHS else "标准 · 约 120 秒"
        wb["副歌写法"] = req.chorus if req.chorus in MUSIC_CHORUS else "末次变奏（推荐）"
        wb["已有歌词"] = req.existing
        wb["必须出现的词"] = req.keywords
        tpl["3"]["inputs"]["seed"] = _random_seed() if req.seed is None or req.seed < 0 else req.seed
        prompt_id = _comfy_post_prompt(tpl)
        entry, err = _wait_done(prompt_id, timeout=300.0)
        if err:
            raise RuntimeError(err)
        text = entry["outputs"]["5"]["text"][0]
        with _lyrics_lock:
            _lyrics_tasks[task_id] = {"state": "done", "lyrics": text}
    except HTTPException as e:
        with _lyrics_lock:
            _lyrics_tasks[task_id] = {"state": "error", "error": str(e.detail)}
    except Exception as e:  # noqa: BLE001
        with _lyrics_lock:
            _lyrics_tasks[task_id] = {"state": "error", "error": str(e)}


@app.post("/api/music/lyrics")
def api_music_lyrics(req: LyricsReq):
    if not req.theme.strip() and not req.existing.strip():
        raise HTTPException(400, "先写一句歌曲主题（或贴入已有歌词）")
    task_id = uuid.uuid4().hex
    with _lyrics_lock:
        _lyrics_tasks[task_id] = {"state": "running"}
    threading.Thread(target=_run_lyrics, args=(task_id, req), daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/music/lyrics/{task_id}")
def api_music_lyrics_result(task_id: str):
    with _lyrics_lock:
        task = _lyrics_tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


# ---------------- 卡拉OK歌词对齐（faster-whisper 逐字时间戳） ----------------

KARAOKE_PY = cfg.karaoke_python or (cfg.comfyui_root.parent / "karaoke_venv" / "Scripts" / "python.exe")
KARAOKE_SCRIPT = BASE_DIR / "karaoke_align.py"


class AlignReq(BaseModel):
    filename: str
    subfolder: str = ""
    lyrics: str


_align_tasks = {}
_align_lock = threading.Lock()
_align_cache = {}  # (subfolder/filename, mtime, lyrics哈希) -> timings


@app.post("/api/music/align")
def api_music_align(req: AlignReq):
    path = (OUTPUT_ROOT / req.subfolder / req.filename).resolve()
    if not str(path).startswith(str(OUTPUT_ROOT.resolve())) or not path.is_file():
        raise HTTPException(404, "音频文件不存在")
    if not KARAOKE_PY.is_file():
        raise HTTPException(503, "对齐环境未安装")
    if not req.lyrics.strip():
        raise HTTPException(400, "歌词不能为空")
    key = (str(path), path.stat().st_mtime_ns, hashlib.md5(req.lyrics.encode()).hexdigest())
    with _align_lock:
        if key in _align_cache:
            return {"state": "done", "timings": _align_cache[key]}
    task_id = uuid.uuid4().hex
    with _align_lock:
        _align_tasks[task_id] = {"state": "running"}
    threading.Thread(target=_run_align, args=(task_id, req, path, key), daemon=True).start()
    return {"task_id": task_id}


def _run_align(task_id, req: AlignReq, path: Path, key):
    try:
        r = subprocess.run(
            [str(KARAOKE_PY), str(KARAOKE_SCRIPT), "--audio", str(path)],
            input=req.lyrics.encode("utf-8"),
            capture_output=True, timeout=600,
        )
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        data = json.loads(out.splitlines()[-1]) if out else {"ok": False, "error": r.stderr.decode("utf-8", "replace")[-300:]}
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "对齐失败")
        timings = {"duration": data["duration"], "lines": data["lines"],
                   "match_ratio": data.get("match_ratio", 0)}
        with _align_lock:
            # 匹配率过低的结果多半是乱码/识别失败，不缓存，避免污染后续请求
            if timings["match_ratio"] > 0.3:
                _align_cache[key] = timings
                if len(_align_cache) > 50:
                    _align_cache.clear()
            _align_tasks[task_id] = {"state": "done", "timings": timings}
    except Exception as e:  # noqa: BLE001
        with _align_lock:
            _align_tasks[task_id] = {"state": "error", "error": str(e)[:300]}


@app.get("/api/music/align/{task_id}")
def api_music_align_result(task_id: str):
    with _align_lock:
        task = _align_tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


# ---------------- 音乐生成 ----------------

def _api_generate_music(req: "GenerateReq"):
    spec = MODES_JSON["music"]
    tpl = _load_template("music")
    styles = _music_styles()
    if not req.style or req.style not in styles:
        raise HTTPException(400, f"非法曲风：{req.style}")
    tpl[spec["style"]["node"]]["inputs"][spec["style"]["input"]] = req.style
    gender = req.gender if req.gender in MUSIC_GENDERS else MUSIC_GENDERS[0]
    tpl[spec["gender"]["node"]]["inputs"][spec["gender"]["input"]] = gender
    lyrics = (req.lyrics or "").strip()
    if not lyrics:
        raise HTTPException(400, "歌词不能为空：直接写词，或先点「AI 写词」")
    tpl[spec["lyrics"]["node"]]["inputs"][spec["lyrics"]["input"]] = lyrics
    dur = float(req.duration) if req.duration else 120.0
    if not (30.0 <= dur <= 300.0):
        raise HTTPException(400, "歌曲长度需在 30~300 秒之间")
    tpl[spec["duration"]["node"]]["inputs"][spec["duration"]["input"]] = dur
    seed_used = _random_seed() if req.seed is None or req.seed < 0 else req.seed
    tpl[spec["seed"]["node"]]["inputs"][spec["seed"]["input"]] = seed_used
    if req.steps is not None:
        s = spec["advanced"]["steps"]
        if not (s["min"] <= req.steps <= s["max"]):
            raise HTTPException(400, f"步数需在 {s['min']}~{s['max']} 之间")
        tpl[s["node"]]["inputs"][s["input"]] = int(req.steps)
    # 云端车道没有 Music3 模型，音乐只走本地
    lane = _lane("local") or _pick_lane()
    prompt_id = _comfy_post_prompt(tpl, lane)
    _register_task(prompt_id, lane["id"], req.mode)
    return {"prompt_id": prompt_id, "lane": {"id": lane["id"], "name": lane["name"]}, "seed": seed_used}


# ---------------- MV 工坊（五步向导：选歌/规划/配图/生成/合成） ----------------

MV_WORK_ROOT = BASE_DIR / "mv_work"
MV_TASKS_DIR = BASE_DIR / "mv_tasks"

_MV_TAG_CN = {
    "intro": "前奏", "verse": "主歌", "pre-chorus": "预副歌", "chorus": "副歌",
    "post-chorus": "后副歌", "bridge": "桥段", "instrumental": "间奏",
    "interlude": "间奏", "inst": "间奏", "solo": "独奏", "outro": "尾奏", "hook": "副歌",
}


def _mv_song_path(filename, subfolder=""):
    path = (OUTPUT_ROOT / subfolder / filename).resolve()
    if not str(path).startswith(str(OUTPUT_ROOT.resolve())) or not path.is_file():
        raise HTTPException(404, "歌曲文件不存在")
    return path


def _mv_parse_info_txt(path):
    text = path.read_text(encoding="utf-8")

    def section(name, nxt):
        m = re.search(rf"===== {name} =====\n(.*?)(?=\n===== {nxt} =====|\Z)", text, re.S)
        return m.group(1).strip() if m else ""

    m = re.search(r"^曲风: (.+)$", text, re.M)
    style = m.group(1).strip() if m else ""
    caption = section("Caption", "歌词")
    lyrics = section("歌词", "歌词（B 段）")
    m = re.search(r"===== 歌词（B 段） =====\n(.*)$", text, re.S)
    if m:
        lyrics = (lyrics + "\n\n" + m.group(1).strip()).strip()
    return style, caption, lyrics


def _mv_lyric_sections(lyrics):
    """把带结构标签的歌词拆成有序段落：[{tag, label, lines}]"""
    sections = []
    cur = None
    for ln in lyrics.splitlines():
        t = ln.strip()
        if not t:
            continue
        m = re.fullmatch(r"\[([^\]]*)\]", t)
        if m:
            cur = {"tag": m.group(1).strip(), "lines": []}
            sections.append(cur)
        elif cur is not None:
            cur["lines"].append(t)
        else:
            cur = {"tag": "Verse", "lines": [t]}
            sections.append(cur)
    for s in sections:
        s["label"] = _MV_TAG_CN.get(s["tag"].lower(), s["tag"])
    return sections


def _mv_assign_times(sections, timings, duration):
    """段落起止：带词段取首末行真实时间（卡拉OK对齐），纯器乐段吃相邻夹缝；
    对齐不可用时按行数权重均摊。"""
    line_times = (timings or {}).get("lines") or []
    li = 0
    for s in sections:
        n = len(s["lines"])
        if n and li + n <= len(line_times):
            s["start"] = line_times[li]["start"]
            s["end"] = line_times[li + n - 1]["end"]
            li += n
        else:
            s["start"] = s["end"] = None
    if all(s["start"] is None for s in sections):
        weights = [max(len(s["lines"]), 1) for s in sections]
        total = sum(weights)
        pos = 0.0
        for s, w in zip(sections, weights):
            s["start"] = round(pos, 2)
            pos += duration * w / total
            s["end"] = round(pos, 2)
        return
    for i, s in enumerate(sections):
        if s["start"] is not None:
            continue
        prev_end = next((sections[k]["end"] for k in range(i - 1, -1, -1)
                         if sections[k]["end"] is not None), 0.0)
        nxt = next((sections[k]["start"] for k in range(i + 1, len(sections))
                    if sections[k]["start"] is not None), duration)
        s["start"], s["end"] = round(prev_end, 2), round(nxt, 2)
    for s in sections:
        if s["end"] - s["start"] < 1.0:
            s["end"] = round(s["start"] + 1.0, 2)
    # 覆盖段间器乐空隙：每段的结束延伸到下一段的开始，保证全曲无视觉空窗
    for i in range(len(sections) - 1):
        if sections[i + 1]["start"] > sections[i]["end"]:
            sections[i]["end"] = sections[i + 1]["start"]
    if sections:
        sections[-1]["end"] = max(sections[-1]["end"], round(duration, 2))


def _mv_find_info_txt(audio_path: Path):
    """找与歌曲配套的留档 txt：同目录 *_info.txt 中修改时间最接近的
    （同一次运行里音频和留档几乎同时写盘，通常只差几秒）。"""
    infos = list(audio_path.parent.glob("*_info.txt"))
    if not infos:
        return None
    mt = audio_path.stat().st_mtime
    return min(infos, key=lambda p: abs(p.stat().st_mtime - mt))


@app.get("/api/mv/songs")
def api_mv_songs():
    """扫描 output/audio 下带留档 txt 的歌曲。"""
    out = []
    root = OUTPUT_ROOT / "audio"
    if root.is_dir():
        for dirpath in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not dirpath.is_dir():
                continue
            for aud in sorted(dirpath.iterdir()):
                if aud.suffix.lower() not in (".mp3", ".flac", ".wav", ".opus", ".m4a", ".ogg"):
                    continue
                info = _mv_find_info_txt(aud)
                entry = {
                    "filename": aud.name,
                    "subfolder": f"audio/{dirpath.name}",
                    "size_bytes": aud.stat().st_size,
                    "mtime": aud.stat().st_mtime,
                    "has_info": info is not None,
                }
                if info:
                    style, _, _ = _mv_parse_info_txt(info)
                    entry["style"] = style
                meta = _probe_meta(aud.name, entry["subfolder"], "audio")
                if meta and meta.get("duration"):
                    entry["duration"] = meta["duration"]
                out.append(entry)
    return {"songs": out}


class MvPlanReq(BaseModel):
    filename: str
    subfolder: str = ""


_mv_plan_tasks = {}
_mv_plan_lock = threading.Lock()


def _align_audio_blocking(path: Path, lyrics: str):
    """调 faster-whisper 对齐歌词行（同步），返回 timings dict 或 None。"""
    try:
        r = subprocess.run(
            [str(KARAOKE_PY), str(KARAOKE_SCRIPT), "--audio", str(path)],
            input=lyrics.encode("utf-8"), capture_output=True, timeout=600,
        )
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        data = json.loads(out.splitlines()[-1]) if out else {}
        if not data.get("ok"):
            return None
        return {"duration": data["duration"], "lines": data["lines"],
                "match_ratio": data.get("match_ratio", 0)}
    except Exception:  # noqa: BLE001
        return None


def _qwen_infer(system, task, max_tokens=2560, temperature=0.7,
                model="Qwen3.5-4B-Q4_K_M.gguf", series="Qwen3.5-VL"):
    """本地 Qwen 文本推理（经 ComfyUI，用完自动卸载）。失败返回 None。"""
    prompt = {
        "1": {"class_type": "QwenTE_ModelLoader", "inputs": {
            "模型系列": series, "主模型": model, "视觉投影mmproj": "无",
            "启用思考": False, "保留历史think": False, "上下文长度": 8192, "GPU层数": -1,
            "KV缓存K类型": "默认(F16)", "KV缓存V类型": "默认(F16)", "MoE专家上CPU": False, "前N层专家上CPU": 0,
            # 节点包 2026-09 升级后新增的必填项（缺了会被 ComfyUI 校验 400 拒收）
            "Qwen3.8推理强度": "xhigh", "MTP推测解码": "关闭", "MTP草稿token数": 2,
            "Flash Attention": "不开启", "MTP草稿模型": "无"}},
        "2": {"class_type": "QwenTE_ImageInfer", "inputs": {
            "qwen模型": ["1", 0], "输入模式": "文本", "提示词": task, "系统提示词": system,
            "最多帧数": 24, "最大边长": 1024, "最大生成token": max_tokens, "温度": temperature, "top_p": 0.9,
            "top_k": 20, "重复惩罚": 1.0, "频率惩罚": 0.0, "存在惩罚": 0.0,
            "seed": _random_seed(), "输出think块": False, "生成后自动卸载模型": True}},
        "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 0]}},
    }
    try:
        pid = _comfy_post_prompt(prompt)
        entry, err = _wait_done(pid, timeout=300.0)
        if err:
            return None
        return entry["outputs"]["3"]["text"][0]
    except Exception:  # noqa: BLE001
        return None


# ---------------- 线上 LLM API（可选：提示词增强/修正/优化走线上） ----------------
# 配置：webapp\llm_api_config.json（不入 git，模板见 llm_api_config.example.json），
# 或环境变量 LLM_API_BASE / LLM_API_KEY / LLM_API_MODEL 覆盖。

LLM_API_CFG_FILE = BASE_DIR / "llm_api_config.json"


def _llm_api_cfg():
    """读取线上 LLM API 配置；缺任一项返回 None。"""
    base = os.environ.get("LLM_API_BASE", "")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_API_MODEL", "")
    if LLM_API_CFG_FILE.is_file():
        try:
            cfg = json.loads(LLM_API_CFG_FILE.read_text(encoding="utf-8"))
            base = base or cfg.get("base_url", "")
            key = key or cfg.get("api_key", "")
            model = model or cfg.get("model", "")
        except Exception:  # noqa: BLE001
            pass
    if not key or not base or not model:
        return None
    return {"base_url": base.rstrip("/"), "api_key": key, "model": model}


_llm_api_last_error = ""  # 最近一次线上调用失败原因（供报错文案透出）


def _llm_api_chat(system, task, max_tokens=2560, temperature=0.7):
    """调用 OpenAI 兼容 chat/completions 接口；失败返回 None（原因记入 _llm_api_last_error）。"""
    global _llm_api_last_error
    _llm_api_last_error = ""
    cfg = _llm_api_cfg()
    if not cfg:
        return None
    url = cfg["base_url"]
    if not url.endswith("/chat/completions"):
        url += "" if url.endswith("/v1") else "/v1"
        url += "/chat/completions"
    msgs = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": task}
    ]
    try:
        r = requests.post(
            url,
            headers={"Authorization": "Bearer " + cfg["api_key"]},
            json={"model": cfg["model"], "messages": msgs,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=90,
        )
        if r.status_code != 200:
            _llm_api_last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            return None
        data = r.json()
        content = data["choices"][0]["message"].get("content")
        # content 可能是字符串或分块列表（部分服务商）；都兼容
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            # 200 但 content 为空：多为 reasoning 模型把 max_tokens 用光，或服务商结构不同
            msg = data["choices"][0].get("message", {})
            _llm_api_last_error = (
                f"content 为空（finish_reason={data['choices'][0].get('finish_reason')}，"
                f"reasoning={'有' if msg.get('reasoning_content') else '无'}）"
            )
            return None
        return content
    except Exception as e:  # noqa: BLE001
        _llm_api_last_error = str(e)[:200]
        return None


@app.get("/api/llm/route")
def api_llm_route():
    """前端据此决定是否启用「线上 API」选项（不回传 key）。"""
    cfg = _llm_api_cfg()
    return {"api_configured": bool(cfg), "model": (cfg or {}).get("model", "")}


# 本地 LLM 模型目录（QwenTE 节点只扫安装目录的 models\LLM，共享目录的同名目录为硬链备份）
LLM_DIRS = [
    Path(os.environ.get("COMFY_HOME", str(cfg.comfyui_root))) / "models" / "LLM",
    cfg.shared_models_root / "LLM",
]


def _llm_local_models():
    """可用于提示词功能的本地 GGUF 模型（相对路径，反斜杠风格与节点一致；排除 mmproj/mtp 附件）。"""
    out = []
    for d in LLM_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.gguf")):
            low = p.name.lower()
            if "mmproj" in low or "mtp" in low:
                continue
            rel = str(p.relative_to(d))
            if rel not in out:
                out.append(rel)
    return out


def _llm_series_for(model_name):
    """按文件名推 QwenTE_ModelLoader 的「模型系列」取值。"""
    low = model_name.lower()
    if "qwen3vl" in low or "qwen3-vl" in low:
        return "Qwen3-VL"
    if "3.8" in low:
        return "Qwen3.8-VL"
    if "3.6" in low:
        return "Qwen3.6-VL"
    return "Qwen3.5-VL"


# 线下模型使用建议（悬停图例用；usable 为 2026-09-29 逐个加载实测结果）
LLM_MODEL_GUIDE = {
    "qwen3vl_8b_heretic-Q4_K_M.gguf":
        "Qwen3-VL 8B · 默认推荐：长提示词单点修改最稳，速度也快（4~8s）",
    "Qwen3.5-4B-Q4_K_M.gguf":
        "Qwen3.5 4B 标准版 · 最快最省显存，适合短提示词；MV 优化/写词的默认",
    "Q35-4B-G-UD\\Qwen3.5-4B-UD-Q4_K_XL.gguf":
        "Qwen3.5 4B UD 动态量化 Q4 · 同尺寸里质量更好的量化方案",
    "Q35-4B-G-UD\\Qwen3.5-4B-UD-Q6_K_XL.gguf":
        "Qwen3.5 4B UD Q6 · 质量再高一档，略慢",
    "Q35-4B-G-UD\\Qwen3.5-4B-UD-Q8_K_XL.gguf":
        "Qwen3.5 4B UD Q8 · 4B 里最高精度，更慢更占显存",
    "Q35-4B-U-HauhauCS\\Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf":
        "Qwen3.5 4B 无审查版 Q4 · 敏感题材的增强/改写用它；注意它习惯先输出 Thinking 段再给正文",
    "Q35-4B-U-HauhauCS\\Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf":
        "Qwen3.5 4B 无审查版 Q6 · 同上，质量更好；也带 Thinking 前缀",
    "Q35-4B-U-HauhauCS\\Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf":
        "Qwen3.5 4B 无审查版 Q8 · 同上，最高精度（Qwen 生图增强的默认同系）；也带 Thinking 前缀",
    "pe_t2i_heretic-Q4_K_M.gguf":
        "文生图提示词扩写专训模型 · 只擅长扩写，修改/对话类任务不建议",
    "pe_i2i_heretic-Q4_K_M.gguf":
        "图生图提示词扩写专训模型 · 只擅长扩写，修改/对话类任务不建议",
}


def _llm_pick_local(name):
    """校验前端选的线下模型（白名单），非法/空返回 None（调用方用默认）。"""
    if name and name in _llm_local_models():
        return name
    return None


# 主模型 → 配套 mmproj 视觉投影（换模型不换 mmproj 会报 mtmd context 加载失败）
def _llm_mmproj_for(model_name):
    if "qwen3vl_8b" in model_name:
        return "mmproj-qwen3vl_8b_heretic-f16.gguf"
    if "Q35-4B-G-UD" in model_name:
        return "Q35-4B-G-UD\\mmproj-Qwen3.5-4B-BF16.gguf"
    if "Q35-4B-U-HauhauCS" in model_name:
        return "Q35-4B-U-HauhauCS\\mmproj-Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-BF16.gguf"
    if "pe_i2i" in model_name:
        return "pe_i2i_heretic.mmproj-bf16.gguf"
    if model_name == "Qwen3.5-4B-Q4_K_M.gguf":
        return "Qwen3.5-4B-mmproj-BF16.gguf"
    return None  # 无配套（如 pe_t2i）→ 调用方自行处理


@app.get("/api/llm/config")
def api_llm_config_get():
    """设置弹窗用：返回线上配置（key 不回传，只报有无）+ 线下模型清单。"""
    cfg = _llm_api_cfg()
    return {
        "base_url": (cfg or {}).get("base_url", ""),
        "model": (cfg or {}).get("model", ""),
        "has_key": bool(cfg and cfg.get("api_key")),
        "local_models": [
            {"name": m, "desc": LLM_MODEL_GUIDE.get(m, "本地 GGUF 模型")}
            for m in _llm_local_models()
        ],
    }


class LlmApiConfigReq(BaseModel):
    base_url: str = ""
    api_key: str = ""  # 留空 = 保持现有 key 不变
    model: str = ""


class LlmModelsReq(BaseModel):
    base_url: str = ""
    api_key: str = ""  # 留空 = 用已保存的 key


@app.post("/api/llm/models")
def api_llm_models(req: LlmModelsReq):
    """用（表单值优先、已保存兜底）的配置拉取服务商可用模型列表。"""
    saved = _llm_api_cfg() or {}
    base = (req.base_url.strip() or saved.get("base_url", "")).rstrip("/")
    key = req.api_key.strip() or saved.get("api_key", "")
    if not base or not key:
        raise HTTPException(400, "请先填写 base_url 和 API Key")
    url = base if base.endswith("/models") else base + ("" if base.endswith("/v1") else "/v1") + "/models"
    try:
        r = requests.get(url, headers={"Authorization": "Bearer " + key}, timeout=30)
    except requests.RequestException as e:
        raise HTTPException(502, f"无法连接 API：{e}")
    if r.status_code != 200:
        raise HTTPException(502, f"拉取模型列表失败 HTTP {r.status_code}：{r.text[:200]}")
    try:
        models = sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))
    except Exception:  # noqa: BLE001
        raise HTTPException(502, f"模型列表响应格式异常：{r.text[:200]}")
    if not models:
        raise HTTPException(502, "服务商返回了空的模型列表")
    return {"models": models}


@app.post("/api/llm/config")
def api_llm_config_save(req: LlmApiConfigReq):
    """保存线上 API 配置到 llm_api_config.json（不入 git）。base_url 和模型都留空 = 清除配置。"""
    existing = {}
    if LLM_API_CFG_FILE.is_file():
        try:
            existing = json.loads(LLM_API_CFG_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = {}
    if not req.base_url.strip() and not req.model.strip():
        LLM_API_CFG_FILE.unlink(missing_ok=True)
        return {"ok": True, "configured": False}
    cfg = {
        "base_url": req.base_url.strip(),
        "api_key": req.api_key.strip() or existing.get("api_key", ""),
        "model": req.model.strip(),
    }
    LLM_API_CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "configured": bool(_llm_api_cfg())}


def _apply_llm_route_to_node(tpl, node_id, api_value):
    """增强器节点切线上：写入 增强方式=API 类取值 + 注入凭据。未配置抛 400。"""
    cfg = _llm_api_cfg()
    if not cfg:
        raise HTTPException(400, "线上 API 未配置：请填写 webapp\\llm_api_config.json"
                                 "（模板 llm_api_config.example.json），或改用线下本地")
    inputs = tpl[node_id]["inputs"]
    inputs["增强方式"] = api_value
    inputs["api_key"] = cfg["api_key"]
    inputs["api_base_url"] = cfg["base_url"]
    inputs["model"] = cfg["model"]


def _mv_qwen_prompts(style, caption, sections, duration):
    """本地 Qwen 给每个段落写画面提示词（编号列表）。失败返回 None。"""
    brief = []
    for i, s in enumerate(sections, 1):
        excerpt = " / ".join(s["lines"][:2])[:60] if s["lines"] else "（纯器乐）"
        brief.append(f"{i}. [{s['label']}] {s['start']:.0f}s-{s['end']:.0f}s: {excerpt}")
    system = ("You are a music video director. You output ONLY a numbered list, "
              "one line per section. No markdown, no commentary.")
    task = f"""Song style: {style or 'unknown'}
Music caption (mood reference):
{(caption or '')[:1000]}

The song is {duration:.0f} seconds, sectioned as:
{chr(10).join(brief)}

Write ONE English visual prompt (25-50 words) for each of the {len(sections)} sections, as "1. <prompt>" ... "{len(sections)}. <prompt>".
Rules:
- Each prompt describes ONE cinematic music-video shot fitting that section's mood and lyrics.
- Invent ONE consistent visual world (same setting, palette, recurring main character or motif) and repeat its key descriptors in every prompt so all shots look like one film.
- Ambient environmental sound and natural sync audio are welcome.
- Strictly no on-screen text, no subtitles, no captions, no logos, no watermarks."""
    text = _qwen_infer(system, task)
    if not text:
        return None
    shots = [m.group(1).strip() for m in re.finditer(
        r"^\s*\d+\s*[.、)]:?\s*[\"\']?(.+?)[\"\']?\s*$", text, re.M)]
    shots = [s for s in shots if len(s) > 15]
    return shots[:len(sections)] if len(shots) >= len(sections) else None


def _run_mv_plan(task_id, req: MvPlanReq):
    try:
        path = _mv_song_path(req.filename, req.subfolder)
        info = _mv_find_info_txt(path)
        style, caption, lyrics = ("", "", "")
        if info:
            style, caption, lyrics = _mv_parse_info_txt(info)
        if not lyrics.strip():
            raise RuntimeError("这首歌没有参数留档 txt（含歌词），无法规划段落")
        meta = _probe_meta(path.name, req.subfolder, "audio") or {}
        duration = float(meta.get("duration") or 0)
        timings = _align_audio_blocking(path, lyrics)
        if timings and timings.get("match_ratio", 0) <= 0.3:
            timings = None  # 匹配率太低多半拿错了歌词，不如均摊
        if timings and not duration:
            duration = timings["duration"]
        if not duration:
            raise RuntimeError("无法读取歌曲时长")
        sections = _mv_lyric_sections(lyrics)
        if not sections:
            raise RuntimeError("歌词里没有可用段落")
        _mv_assign_times(sections, timings, duration)
        prompts = _mv_qwen_prompts(style, caption, sections, duration)
        suffix = " No text, no subtitles, no captions, no watermark. Ambient environmental sound and natural sync audio."
        for i, s in enumerate(sections):
            dur = s["end"] - s["start"]
            clips = max(1, math_ceil(dur / 15.0))
            s["duration"] = round(dur, 2)
            s["clips"] = clips
            s["clip_len"] = round(dur / clips, 2)
            s["prompt"] = ((prompts[i] if prompts and i < len(prompts)
                            else f"Cinematic music video shot, {style or 'emotional'} mood, {s['label']} section.")
                           + suffix)
        result = {
            "duration": round(duration, 2),
            "style": style,
            "aligned": bool(timings),
            "prompts_by_ai": bool(prompts),
            "sections": [
                {"tag": s["tag"], "label": s["label"], "start": s["start"], "end": s["end"],
                 "duration": s["duration"], "clips": s["clips"], "clip_len": s["clip_len"],
                 "lyrics": s["lines"], "prompt": s["prompt"]}
                for s in sections
            ],
        }
        with _mv_plan_lock:
            _mv_plan_tasks[task_id] = {"state": "done", "plan": result}
    except Exception as e:  # noqa: BLE001
        with _mv_plan_lock:
            _mv_plan_tasks[task_id] = {"state": "error", "error": str(e)[:300]}


def math_ceil(x):
    return int(-(-x // 1))


@app.post("/api/mv/plan")
def api_mv_plan(req: MvPlanReq):
    _mv_song_path(req.filename, req.subfolder)  # 校验存在
    task_id = uuid.uuid4().hex
    with _mv_plan_lock:
        _mv_plan_tasks[task_id] = {"state": "running"}
    threading.Thread(target=_run_mv_plan, args=(task_id, req), daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/mv/plan/{task_id}")
def api_mv_plan_result(task_id: str):
    with _mv_plan_lock:
        task = _mv_plan_tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


MV_NO_TEXT_SUFFIX = (" No text, no subtitles, no captions, no watermark."
                     " Ambient environmental sound and natural sync audio.")
MV_LIPSYNC_SUFFIX = (" Close-up of the singer facing the camera, singing with mouth"
                     " movements clearly synchronized to the vocal, natural facial expression.")


class MvPromptOptimizeReq(BaseModel):
    prompt: str
    style: str = ""
    label: str = ""
    lyrics: str = ""
    lip_sync: bool = False
    route: str = "local"  # local=线下本地模型 / api=线上 API（llm_api_config.json）
    llm_model: str | None = None  # 线下时指定本地 GGUF（白名单校验，空=默认 4B）


@app.post("/api/mv/prompt/optimize")
def api_mv_prompt_optimize(req: MvPromptOptimizeReq):
    """本地 Qwen 润色单条段落/片段提示词（同步调用，约 10-30 秒）。"""
    if not req.prompt.strip():
        raise HTTPException(400, "提示词为空，先写点内容再优化")
    system = ("You are a music video director and prompt engineer. You output ONLY the "
              "rewritten English prompt, one paragraph, no markdown, no commentary, no quotes.")
    task = f"""Song style: {req.style or 'unknown'}
Section: {req.label or 'unknown'}
Lyrics excerpt: {(req.lyrics or '')[:200] or '(instrumental)'}
{"This shot needs visible lip-sync: a close-up of the singer facing camera, mouth movements matching the vocal." if req.lip_sync else ""}

Rewrite and improve this music-video shot prompt (keep it 30-60 words, cinematic, concrete):
{req.prompt.strip()[:800]}

Rules:
- Keep the user's core idea, make it more visual and specific (lighting, camera, mood).
- Keep it consistent with the song style and section mood above.
- Strictly no on-screen text, no subtitles, no captions, no logos, no watermarks.
- Ambient environmental sound and natural sync audio are welcome."""
    if req.route == "api":
        if not _llm_api_cfg():
            raise HTTPException(400, "线上 API 未配置：请填写 webapp\\llm_api_config.json"
                                     "（模板 llm_api_config.example.json），或改用线下本地")
        text = _llm_api_chat(system, task, max_tokens=512, temperature=0.7)
    else:
        m = _llm_pick_local(req.llm_model)
        if m:
            text = _qwen_infer(system, task, max_tokens=512, temperature=0.7,
                               model=m, series=_llm_series_for(m))
        else:
            text = _qwen_infer(system, task, max_tokens=512, temperature=0.7)
    if not text:
        if req.route == "api":
            raise HTTPException(502, "AI 优化失败：线上 API 无响应（"
                                     + (_llm_api_last_error or "连接失败") + "），或改用线下本地")
        raise HTTPException(502, "AI 优化失败：本地 Qwen 无响应，稍后再试")
    # 取第一段非空文本，去掉可能的编号/引号
    line = ""
    for ln in text.strip().splitlines():
        ln = ln.strip().strip('"\'')
        if ln:
            line = re.sub(r"^\d+\s*[.、)]:?\s*", "", ln).strip()
            break
    if len(line) < 10:
        raise HTTPException(502, "AI 优化结果异常，请重试")
    if "no text" not in line.lower():
        line += MV_NO_TEXT_SUFFIX
    return {"prompt": line}


# ---------------- 提示词修正（通用，本地 Qwen 按修改要求改写） ----------------

class PromptReviseReq(BaseModel):
    text: str         # 当前提示词（通常是增强结果）
    instruction: str  # 用户的修改要求，如「改成夜晚」「去掉人物」
    route: str = "local"  # local=线下本地模型 / api=线上 API（llm_api_config.json）
    llm_model: str | None = None  # 线下时指定本地 GGUF（白名单校验，空=默认 8B）


@app.post("/api/prompt/revise")
def api_prompt_revise(req: PromptReviseReq):
    """按用户的修改要求改写提示词（同步调用本地 Qwen，约 10-30 秒）。"""
    if not req.text.strip():
        raise HTTPException(400, "提示词为空")
    if not req.instruction.strip():
        raise HTTPException(400, "请先输入修改要求")
    # 4B 小模型实测：few-shot 示例 + 补全式结尾成功率最高，但部分 seed 会整体复读原文
    # （输出与 seed 强相关、与温度关系不大），因此做回声检测 + 换 seed 重试，最多 3 次
    orig = req.text.strip()
    task_tpl = (
        "请把下面的提示词按要求修改。直接输出修改后的完整提示词，不要输出任何解释。\n\n"
        "原提示词：一只猫坐在沙发上看电视，客厅温馨明亮。\n"
        "修改要求：改成狗，场景换成厨房\n"
        "修改后的提示词：一只狗坐在厨房里看电视，厨房温馨明亮。\n\n"
        f"原提示词：{orig[:3000]}\n修改要求：{req.instruction.strip()[:500]}\n修改后的提示词："
    )
    # 修正用 Qwen3-VL 8B（models/LLM 里的硬链接，源文件在 text_encoders）：
    # 实测比 4B 更会做「长文里的单点修改」，且不复读原文；保留回声检测 + 换 seed 重试兜底
    # route=api 时改走线上 API（_llm_api_chat，OpenAI 兼容 chat/completions）
    if req.route == "api" and not _llm_api_cfg():
        raise HTTPException(400, "线上 API 未配置：请填写 webapp\\llm_api_config.json"
                                 "（模板 llm_api_config.example.json），或改用线下本地")
    text = None
    api_silent = True  # 线上线路下：LLM 是否一次都没应答（区别于「应答了但复读原文」）
    local_model = _llm_pick_local(req.llm_model) or "qwen3vl_8b_heretic-Q4_K_M.gguf"
    for _attempt in range(3):
        if req.route == "api":
            out = _llm_api_chat("", task_tpl, max_tokens=2560, temperature=0.7)
        else:
            out = _qwen_infer("", task_tpl, max_tokens=2560, temperature=0.7,
                              model=local_model, series=_llm_series_for(local_model))
        if not out:
            continue
        api_silent = False
        # 小模型有时先复述原文、再用「修改后的提示词：」引出修订版——取标记之后的内容
        if "修改后的提示词" in out:
            out = out.rsplit("修改后的提示词", 1)[-1]
        out = re.sub(r"^[：:\*\s]+", "", out).replace("**", "").strip().strip('"“”')
        # 尾注清理：模型偶尔在修订版后追加「（注：…）」说明段（可能同行、可能带 markdown 残留星号）
        out = re.split(r"[（(]注", out, maxsplit=1)[0].rstrip().rstrip("*").rstrip()
        if out and out != orig:
            text = out
            break
    if not text:
        if req.route == "api" and api_silent:
            raise HTTPException(502, "线上 API 无响应：" + (_llm_api_last_error or "连接失败")
                                     + "。请检查 ⚙️ 里的配置，或改用线下本地")
        raise HTTPException(502, "模型未做修改（连续 3 次原样返回），请换个说法重试")
    return {"text": text}


# ---------- MV 生成任务队列 ----------

class MvGenerateReq(BaseModel):
    filename: str
    subfolder: str = ""
    sections: list  # [{tag,label,start,end,clips,clip_len,prompt,images:[ComfyUI文件名],lip_sync:bool}]
    autostart: bool = True  # False = 只建任务不启动（先逐条配置）


_mv_tasks = {}
_mv_lock = threading.Lock()


def _mv_task_file(task_id):
    MV_TASKS_DIR.mkdir(parents=True, exist_ok=True)
    return MV_TASKS_DIR / f"{task_id}.json"


def _mv_save_task(t):
    try:
        _mv_task_file(t["id"]).write_text(
            json.dumps(_mv_task_public(t), ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _mv_task_public(t):
    return {k: v for k, v in t.items() if k != "thread"}


def _mv_pick_mode(n_images):
    return "r2v" if n_images >= 2 else ("i2v" if n_images == 1 else "t2v")


def _mv_build_clip_tpl(mode, prompt_text, seconds, seed, images, steps=8):
    tpl = _load_template(mode)
    spec = MODES_JSON[mode]
    tpl[spec["prompt"]["node"]]["inputs"][spec["prompt"]["input"]] = prompt_text
    tpl[spec["seed"]["node"]]["inputs"][spec["seed"]["input"]] = seed
    tpl[spec["seconds"]["node"]]["inputs"][spec["seconds"]["input"]] = float(seconds)
    if "ratio" in spec:
        tpl[spec["ratio"]["node"]]["inputs"][spec["ratio"]["input"]] = "16:9 (Widescreen)"
    s = spec.get("advanced", {}).get("steps")
    if s:
        tpl[s["node"]]["inputs"][s["input"]] = int(steps)
    if mode == "i2v":
        img = spec["images"][0]
        tpl[img["node"]]["inputs"][img["input"]] = images[0]
    elif mode == "r2v":
        imgs = spec["images"]
        tpl[imgs[0]["node"]]["inputs"][imgs[0]["input"]] = images[0]
        tpl[imgs[1]["node"]]["inputs"][imgs[1]["input"]] = images[1]
        for idx, fname in enumerate(images[2:4]):
            opts = spec.get("optional_slots", [])
            if idx >= len(opts):
                break
            wire = opts[idx]["wire"]
            nid = str(max(int(k) for k in tpl.keys()) + 1)
            tpl[nid] = {"class_type": "LoadImage", "inputs": {"image": fname}}
            tpl[wire["node"]]["inputs"][wire["input"]] = [nid, 0]
    return tpl


def _mv_fetch_video(entry):
    for node_out in (entry.get("outputs") or {}).values():
        for key in ("videos", "gifs", "images"):
            for item in node_out.get(key) or []:
                fn = (item.get("filename") or "").lower()
                if fn.endswith((".mp4", ".webm", ".mov")):
                    return item
    return None


def _mv_exec_clip(t, clip, workdir: Path, lane):
    """执行单条视频段生成（抛异常表示失败）。供主队列 worker 与单段生成复用。"""
    clip["started_at"] = time.time()
    prompt_text = clip["prompt"]
    if clip.get("lip_sync"):
        prompt_text += MV_LIPSYNC_SUFFIX
    tpl = _mv_build_clip_tpl(clip["mode"], prompt_text, clip["seconds"],
                             clip["seed"], clip["images"])
    pid = _comfy_post_prompt(tpl, lane)
    clip["prompt_id"] = pid
    with _mv_lock:
        _mv_save_task(t)
    entry, err = _wait_done(pid, timeout=1200.0)
    if err:
        raise RuntimeError(err)
    vid = _mv_fetch_video(entry)
    if not vid:
        raise RuntimeError("ComfyUI 未产出视频")
    with requests.get(f"{COMFY}/view", params={
            "filename": vid["filename"], "subfolder": vid.get("subfolder", ""),
            "type": "output"}, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(workdir / clip["file"], "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                f.write(chunk)
    clip["elapsed"] = round(time.time() - clip["started_at"], 1)


def _mv_worker(task_id):
    with _mv_lock:
        t = _mv_tasks.get(task_id)
    if t is None:
        return
    lane = _lane("local") or _pick_lane()
    workdir = Path(t["workdir"])
    workdir.mkdir(parents=True, exist_ok=True)
    any_error = False
    for sec in t["sections"]:
        for clip in sec["clips"]:
            if clip["state"] == "running" or (clip["state"] == "done" and (workdir / clip["file"]).exists()):
                continue
            clip["state"] = "running"
            clip.pop("error", None)
            with _mv_lock:
                _mv_save_task(t)
            try:
                _mv_exec_clip(t, clip, workdir, lane)
                clip["state"] = "done"
            except Exception as e:  # noqa: BLE001
                clip["state"] = "error"
                clip["error"] = str(e)[:200]
                any_error = True
            with _mv_lock:
                _mv_save_task(t)
    t["state"] = "partial" if any_error else "generated"
    with _mv_lock:
        _mv_save_task(t)


def _mv_single_clip_worker(task_id, si, ci):
    with _mv_lock:
        t = _mv_tasks.get(task_id)
    if t is None:
        return
    lane = _lane("local") or _pick_lane()
    workdir = Path(t["workdir"])
    workdir.mkdir(parents=True, exist_ok=True)
    clip = t["sections"][si]["clips"][ci]
    clip["state"] = "running"
    clip.pop("error", None)
    with _mv_lock:
        _mv_save_task(t)
    try:
        _mv_exec_clip(t, clip, workdir, lane)
        clip["state"] = "done"
    except Exception as e:  # noqa: BLE001
        clip["state"] = "error"
        clip["error"] = str(e)[:200]
    done = sum(1 for s in t["sections"] for c in s["clips"] if c["state"] == "done")
    total = sum(len(s["clips"]) for s in t["sections"])
    if done == total:
        t["state"] = "generated"
    elif t["state"] not in ("created",):
        t["state"] = "partial" if clip["state"] == "error" else t["state"]
    with _mv_lock:
        _mv_save_task(t)


@app.post("/api/mv/generate")
def api_mv_generate(req: MvGenerateReq):
    path = _mv_song_path(req.filename, req.subfolder)
    if not req.sections:
        raise HTTPException(400, "段落为空")
    task_id = uuid.uuid4().hex[:12]
    workdir = MV_WORK_ROOT / task_id
    t = {
        "id": task_id,
        "state": "running" if req.autostart else "created",
        "song": {"filename": req.filename, "subfolder": req.subfolder},
        "workdir": str(workdir),
        "created_at": time.time(),
        "sections": [],
    }
    for si, sec in enumerate(req.sections):
        images = [f for f in (sec.get("images") or []) if f][:4]
        mode = _mv_pick_mode(len(images))
        lip_sync = bool(sec.get("lip_sync"))
        clips = []
        n = max(1, int(sec.get("clips") or 1))
        clip_len = float(sec.get("clip_len") or 15)
        if clip_len > 15 or clip_len < 1:
            raise HTTPException(400, "每段视频需在 1~15 秒之间")
        for ci in range(n):
            clips.append({
                "idx": ci, "state": "pending", "mode": mode,
                "prompt": (sec.get("prompt") or "").strip(),
                "seconds": round(clip_len, 2),
                "seed": _random_seed(),
                "images": images,
                "lip_sync": lip_sync,
                "file": f"s{si:02d}_c{ci:02d}.mp4",
            })
        t["sections"].append({
            "tag": sec.get("tag"), "label": sec.get("label"),
            "start": sec.get("start"), "end": sec.get("end"),
            "clips": clips,
        })
    with _mv_lock:
        _mv_tasks[task_id] = t
        _mv_save_task(t)
    if req.autostart:
        th = threading.Thread(target=_mv_worker, args=(task_id,), daemon=True)
        t["thread"] = th
        th.start()
    return {"task_id": task_id}


class MvTaskIdReq(BaseModel):
    task_id: str


def _mv_worker_alive(t):
    th = t.get("thread")
    return bool(th and th.is_alive())


@app.post("/api/mv/start")
def api_mv_start(req: MvTaskIdReq):
    """启动/继续整个任务队列（此前可能只建了任务没启动）。"""
    with _mv_lock:
        t = _mv_tasks.get(req.task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    if _mv_worker_alive(t):
        return {"ok": True, "already": True}
    t["state"] = "running"
    _mv_save_task(t)
    th = threading.Thread(target=_mv_worker, args=(req.task_id,), daemon=True)
    t["thread"] = th
    th.start()
    return {"ok": True}


class MvClipReq(BaseModel):
    task_id: str
    section: int
    clip: int
    prompt: str | None = None
    images: list | None = None
    lip_sync: bool | None = None


def _mv_get_clip(task_id, si, ci):
    with _mv_lock:
        t = _mv_tasks.get(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    try:
        return t, t["sections"][si]["clips"][ci]
    except (IndexError, KeyError):
        raise HTTPException(400, "段落或视频段不存在")


@app.post("/api/mv/clip/update")
def api_mv_clip_update(req: MvClipReq):
    """改单条的提示词/参考图/对口型（未在生成的才能改，图数变化会重算生成模式）。"""
    t, clip = _mv_get_clip(req.task_id, req.section, req.clip)
    if clip["state"] == "running":
        raise HTTPException(400, "该段正在生成中，等完成后再改")
    if req.prompt is not None:
        if not req.prompt.strip():
            raise HTTPException(400, "提示词不能为空")
        clip["prompt"] = req.prompt.strip()
    if req.images is not None:
        imgs = [f for f in req.images if f][:4]
        clip["images"] = imgs
        clip["mode"] = _mv_pick_mode(len(imgs))
    if req.lip_sync is not None:
        clip["lip_sync"] = bool(req.lip_sync)
    _mv_save_task(t)
    return {"ok": True, "mode": clip["mode"]}


@app.post("/api/mv/clip/generate")
def api_mv_clip_generate(req: MvClipReq):
    """单独生成某一条（用该条自己的提示词和参考图）。"""
    t, clip = _mv_get_clip(req.task_id, req.section, req.clip)
    if clip["state"] == "running":
        raise HTTPException(400, "该段正在生成中")
    if not clip.get("prompt", "").strip():
        raise HTTPException(400, "该段提示词为空，先点「设置」填提示词")
    if _mv_worker_alive(t) and clip["state"] == "pending":
        # 主队列正在跑：直接把这条交给主队列优先处理即可，避免双 worker 抢显存
        clip["seed"] = _random_seed()
        _mv_save_task(t)
        return {"ok": True, "via": "queue"}
    clip["seed"] = _random_seed()
    _mv_save_task(t)
    th = threading.Thread(target=_mv_single_clip_worker,
                          args=(req.task_id, req.section, req.clip), daemon=True)
    t["thread"] = th
    th.start()
    return {"ok": True, "via": "single"}


@app.get("/api/mv/task/{task_id}")
def api_mv_task(task_id: str):
    with _mv_lock:
        t = _mv_tasks.get(task_id)
    if t is None:
        f = _mv_task_file(task_id)
        if f.is_file():
            t = json.loads(f.read_text(encoding="utf-8"))
            with _mv_lock:
                _mv_tasks[task_id] = t
    if t is None:
        raise HTTPException(404, "任务不存在")
    # 自愈：有 clip 处于 running 但 worker 线程已不在（webapp 重启/进程崩溃导致孤儿）——
    # 重置回 pending 并自动拉起 worker 续跑，用户页面轮询到即恢复
    if not _mv_worker_alive(t):
        orphans = [c for sec in t.get("sections", []) for c in sec.get("clips", [])
                   if c.get("state") == "running"]
        if orphans:
            for c in orphans:
                c["state"] = "pending"
                c.pop("prompt_id", None)
                c.pop("started_at", None)
            t["state"] = "running"
            with _mv_lock:
                _mv_save_task(t)
            th = threading.Thread(target=_mv_worker, args=(task_id,), daemon=True)
            t["thread"] = th
            th.start()
    # 运行中的视频段附上实时进度（WS 长连按 prompt_id 跟踪）与已用时间
    for sec in t.get("sections", []):
        for c in sec.get("clips", []):
            if c.get("state") == "running":
                pid = c.get("prompt_id")
                if pid:
                    with _progress_lock:
                        p = _progress.get(f"local:{pid}")
                    if p and p.get("max"):
                        c["progress"] = {
                            "value": p["value"], "max": p["max"],
                            "percent": round(p["value"] / p["max"] * 100, 1),
                        }
                if c.get("started_at"):
                    c["elapsed_live"] = round(time.time() - c["started_at"])
    # 预计时间：已完成段的平均耗时（没有就给 None，前端用经验值）
    done_elapsed = [c["elapsed"] for sec in t.get("sections", []) for c in sec.get("clips", [])
                    if c.get("state") == "done" and c.get("elapsed")]
    out = _mv_task_public(t)
    out["avg_clip_seconds"] = round(sum(done_elapsed) / len(done_elapsed), 1) if done_elapsed else None
    return out


class MvRetryReq(BaseModel):
    task_id: str
    section: int
    clip: int


@app.post("/api/mv/retry")
def api_mv_retry(req: MvRetryReq):
    with _mv_lock:
        t = _mv_tasks.get(req.task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    try:
        clip = t["sections"][req.section]["clips"][req.clip]
    except (IndexError, KeyError):
        raise HTTPException(400, "段落或视频段不存在")
    if clip["state"] == "running":
        raise HTTPException(400, "该段正在生成中")
    clip["state"] = "pending"
    clip["seed"] = _random_seed()
    clip.pop("error", None)
    if t["state"] in ("generated", "partial", "assembled", "done"):
        t["state"] = "running"
    _mv_save_task(t)
    if not any(th.is_alive() for th in [t.get("thread")] if th):
        th = threading.Thread(target=_mv_worker, args=(req.task_id,), daemon=True)
        t["thread"] = th
        th.start()
    return {"ok": True}


# ---------- MV 合成 ----------

class MvAssembleReq(BaseModel):
    task_id: str
    mix_ambient: bool = True


def _ff_duration(path):
    try:
        import imageio_ffmpeg
        r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, timeout=30)
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _run_mv_assemble(task_id, req: MvAssembleReq):
    with _mv_lock:
        t = _mv_tasks.get(task_id)
    try:
        t["state"] = "assembling"
        _mv_save_task(t)
        workdir = Path(t["workdir"])
        clips = [workdir / c["file"] for sec in t["sections"] for c in sec["clips"]]
        if not all(p.exists() for p in clips):
            raise RuntimeError("还有视频段未生成完成")
        song = _mv_song_path(t["song"]["filename"], t["song"]["subfolder"])
        duration = _ff_duration(song)
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        joined = workdir / "joined.mp4"

        # 段间 0.5 秒交叉淡化（xfade/acrossfade 链）；失败退回直接拼接
        try:
            durs = [_ff_duration(p) for p in clips]
            inputs = []
            for p in clips:
                inputs += ["-i", str(p)]
            parts, offset = [], 0.0
            last_v, last_a = "0:v", "0:a"
            for i in range(1, len(clips)):
                offset += durs[i - 1] - (0.5 if i > 0 else 0)
                parts.append(f"[{last_v}][{i}:v]xfade=transition=fade:duration=0.5:offset={offset:.2f}[v{i}]")
                parts.append(f"[{last_a}][{i}:a]acrossfade=d=0.5[a{i}]")
                last_v, last_a = f"v{i}", f"a{i}"
            filtergraph = ";".join(parts)
            subprocess.run([ff, "-y", *inputs, "-filter_complex", filtergraph,
                            "-map", f"[{last_v}]", "-map", f"[{last_a}]",
                            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                            "-pix_fmt", "yuv420p", "-r", "24", "-c:a", "aac", joined],
                           check=True, capture_output=True, timeout=1800)
        except Exception:  # noqa: BLE001
            lst = workdir / "concat.txt"
            with open(lst, "w", encoding="utf-8") as f:
                for p in clips:
                    f.write(f"file '{p.name}'\n")
            subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                            "-pix_fmt", "yuv420p", "-r", "24", "-c:a", "aac", joined],
                           check=True, capture_output=True, timeout=1800, cwd=str(workdir))

        out_dir = OUTPUT_ROOT / "mv"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r'[\\/:*?"<>|]+', "_", Path(t["song"]["filename"]).stem)
        out_path = out_dir / f"{stem}_MV.mp4"
        if req.mix_ambient:
            subprocess.run([ff, "-y", "-i", str(joined), "-i", str(song),
                            "-filter_complex", "[1:a]volume=1.0[song];[0:a]volume=0.2[amb];"
                                               "[song][amb]amix=inputs=2:duration=first[a]",
                            "-map", "0:v", "-map", "[a]", "-t", f"{duration:.2f}",
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out_path)],
                           check=True, capture_output=True, timeout=1800)
        else:
            subprocess.run([ff, "-y", "-i", str(joined), "-i", str(song),
                            "-t", f"{duration:.2f}", "-c:v", "copy", "-c:a", "aac",
                            "-b:a", "192k", "-shortest", str(out_path)],
                           check=True, capture_output=True, timeout=1800)
        t["state"] = "done"
        t["result"] = {"filename": out_path.name, "subfolder": "mv"}
        _mv_save_task(t)
    except Exception as e:  # noqa: BLE001
        t["state"] = "assemble_error"
        t["error"] = str(e)[:300]
        _mv_save_task(t)


@app.post("/api/mv/assemble")
def api_mv_assemble(req: MvAssembleReq):
    with _mv_lock:
        t = _mv_tasks.get(req.task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    if t["state"] in ("assembling",):
        raise HTTPException(400, "正在合成中")
    threading.Thread(target=_run_mv_assemble, args=(req.task_id, req), daemon=True).start()
    return {"ok": True}


@app.get("/api/mv/clip/{task_id}/{filename}")
def api_mv_clip(task_id: str, filename: str):
    """预览已生成的视频段（存在任务工作目录，不在 ComfyUI output 里）。"""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    path = (MV_WORK_ROOT / task_id / filename).resolve()
    if not str(path).startswith(str(MV_WORK_ROOT.resolve())) or not path.is_file():
        raise HTTPException(404, "视频段不存在")
    return FileResponse(path, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})


# ---------------- 生成 ----------------

class GenerateReq(BaseModel):
    mode: str
    prompt: str = ""  # readonly 模式（quadview）不需要
    images: dict = {}
    optional: dict = {}  # 可选槽位：label -> ComfyUI 文件名
    seed: int = -1
    seconds: float | None = None
    aspect_ratio: str | None = None
    # 高级参数（可选，按模式注入点生效）
    steps: int | None = None
    megapixels: float | None = None
    clip_name: str | None = None
    lora_name: str | None = None
    lora_strength: float | None = None
    unet_name: str | None = None
    lane: str = "auto"  # auto | 车道 id
    # 音乐模式专用
    style: str | None = None    # 曲风名（预设目录里的显示名）
    gender: str | None = None   # 人声性别
    lyrics: str | None = None   # 歌词（带结构标签）
    duration: float | None = None  # 歌曲长度（秒）


@app.post("/api/generate")
def api_generate(req: GenerateReq):
    if req.mode == "music":
        return _api_generate_music(req)
    if req.mode not in MODES:
        raise HTTPException(400, f"未知模式：{req.mode}")
    spec = MODES_JSON[req.mode]
    tpl = _load_template(req.mode)

    # 提示词（只读模式忽略用户输入，保留模板内置值）
    if MODES[req.mode].get("prompt_mode") != "readonly":
        tpl[spec["prompt"]["node"]]["inputs"][spec["prompt"]["input"]] = req.prompt

    # 图片槽位：label -> node/input
    label_map = {img["label"]: img for img in spec.get("images", [])}
    for label, img_spec in label_map.items():
        filename = (req.images or {}).get(label)
        if not filename:
            raise HTTPException(400, f"缺少图片：{label}")
        tpl[img_spec["node"]]["inputs"][img_spec["input"]] = filename

    # seed
    seed_used = None
    if "seed" in spec:
        seed_used = _random_seed() if req.seed is None or req.seed < 0 else req.seed
        tpl[spec["seed"]["node"]]["inputs"][spec["seed"]["input"]] = seed_used

    # 可选上传槽：上传了才动态加节点并接线，不传则完全不参与工作流
    # slot 可带 "via"：在 LoadImage 与目标输入之间插入一个中间节点（如 ImageScaleToTotalPixels）
    for slot in spec.get("optional_slots", []):
        filename = (req.optional or {}).get(slot["label"])
        if not filename:
            continue
        next_id = max(int(k) for k in tpl.keys()) + 1
        target = slot["wire"]
        if slot["kind"] == "image":
            nid = str(next_id)
            tpl[nid] = {"class_type": "LoadImage", "inputs": {"image": filename}}
            src = [nid, 0]
            via = slot.get("via")
            if via:
                vid = str(next_id + 1)
                via_inputs = dict(via.get("inputs") or {})
                # "$xxx" 占位：取请求里的同名高级参数（如 "$megapixels" → req.megapixels），
                # 未提供时回退到 advanced 同名注入点的模板默认值
                for k, v in list(via_inputs.items()):
                    if isinstance(v, str) and v.startswith("$"):
                        req_val = getattr(req, v[1:], None)
                        if req_val is None:
                            a = spec.get("advanced", {}).get(v[1:])
                            req_val = tpl[a["node"]]["inputs"][a["input"]] if a else None
                        via_inputs[k] = req_val
                via_inputs[via.get("image_input", "image")] = src
                tpl[vid] = {"class_type": via["class_type"], "inputs": via_inputs}
                src = [vid, 0]
            tpl[target["node"]]["inputs"][target["input"]] = src
        elif slot["kind"] == "video":
            # LoadVideo 输出 VIDEO，需经 GetVideoComponents 取帧（IMAGE）
            lv_id, gv_id = str(next_id), str(next_id + 1)
            tpl[lv_id] = {"class_type": "LoadVideo", "inputs": {"file": filename}}
            tpl[gv_id] = {"class_type": "GetVideoComponents", "inputs": {"video": [lv_id, 0]}}
            tpl[target["node"]]["inputs"][target["input"]] = [gv_id, 0]

    # 编辑开关（如 qwen_i2i 的 ComfySwitchNode）：有可选图上传走图生图支路，否则走空 latent（退化为文生图）
    if "edit_switch" in spec:
        es = spec["edit_switch"]
        has_img = any(
            (req.optional or {}).get(s["label"])
            for s in spec.get("optional_slots", [])
            if s.get("kind") == "image"
        )
        tpl[es["node"]]["inputs"][es["input"]] = bool(has_img)

    # 秒数 / 比例
    if req.seconds is not None and "seconds" in spec:
        if not (1 <= req.seconds <= 60):
            raise HTTPException(400, "秒数超出范围")
        tpl[spec["seconds"]["node"]]["inputs"][spec["seconds"]["input"]] = req.seconds
    if req.aspect_ratio is not None and "ratio" in spec:
        if req.aspect_ratio not in RATIO_OPTIONS:
            raise HTTPException(400, f"非法画面比例：{req.aspect_ratio}")
        tpl[spec["ratio"]["node"]]["inputs"][spec["ratio"]["input"]] = req.aspect_ratio

    # 高级参数注入（白名单/范围校验，注入点见 modes.json advanced 段）
    adv = spec.get("advanced", {})
    # ---- 车道：自动 / 指定；云端先重推输入文件，再套车道模型/参数预设（用户显式参数随后覆盖） ----
    # local_only 模式（如 Qwen 生图，云端无模型）强制本地车道
    if MODES[req.mode].get("local_only"):
        lane = _lane("local") or _pick_lane()
    else:
        lane = _resolve_lane(req.lane)
    lname = lane["name"]
    if lane["id"] != "local":
        for fname in list((req.images or {}).values()) + list((req.optional or {}).values()):
            if fname and not _push_input_to_lane(lane, fname):
                raise HTTPException(502, f"输入文件推送失败（车道 {lname}）")
    lm = lane.get("models") or {}
    lp = lane.get("params") or {}
    # 车道预设值 + 车道候选清单都视为合法模型名（云端文件名与本地清单不同）
    _lm_all = list(lm.values()) + [
        v for vals in (lane.get("model_options") or {}).values()
        for v in (vals if isinstance(vals, list) else [vals])
    ]
    if adv and "unet" in adv and lm.get("unet_" + req.mode):
        s = adv["unet"]
        tpl[s["node"]]["inputs"][s["input"]] = lm["unet_" + req.mode]
    if adv and "clip" in adv and lm.get("clip"):
        s = adv["clip"]
        tpl[s["node"]]["inputs"][s["input"]] = lm["clip"]
    for _role in ("video_vae", "audio_vae"):
        if adv and _role in adv and lm.get(_role):
            s = adv[_role]
            tpl[s["node"]]["inputs"][s["input"]] = lm[_role]
    _lora_name = lm.get("lora_" + req.mode) or lm.get("lora")
    if adv and "lora" in adv and _lora_name:
        s = adv["lora"]
        tpl[s["node"]]["inputs"][s["input"]] = _lora_name
    # quadview 的 LoRA 是四视图效果本体，强度保持模板默认，不适用车道全局 lora_strength
    if lp.get("lora_strength") is not None and req.mode != "quadview":
        if adv and "lora" in adv:
            s = adv["lora"]
            tpl[s["node"]]["inputs"][s.get("strength_input") or s["input"]] = float(lp["lora_strength"])
        elif adv and "lora_strength" in adv:
            s = adv["lora_strength"]
            tpl[s["node"]]["inputs"][s["input"]] = float(lp["lora_strength"])
    steps_map = lp.get("steps")
    if adv and "steps" in adv and steps_map:
        step_val = steps_map.get(req.mode) if isinstance(steps_map, dict) else steps_map
        if step_val is None and isinstance(steps_map, dict):
            step_val = steps_map.get("default")
        if step_val is not None:
            s = adv["steps"]
            tpl[s["node"]]["inputs"][s["input"]] = int(step_val)

    if req.steps is not None and "steps" in adv:
        s = adv["steps"]
        if not (s["min"] <= req.steps <= s["max"]):
            raise HTTPException(400, f"步数需在 {s['min']}~{s['max']} 之间")
        tpl[s["node"]]["inputs"][s["input"]] = int(req.steps)
    if req.megapixels is not None and "megapixels" in adv:
        s = adv["megapixels"]
        if not (s["min"] <= req.megapixels <= s["max"]):
            raise HTTPException(400, f"分辨率需在 {s['min']}~{s['max']} MP 之间")
        tpl[s["node"]]["inputs"][s["input"]] = float(req.megapixels)
    if req.clip_name is not None and "clip" in adv:
        _allowed = _enabled_model_paths("clip") + [tpl[adv["clip"]["node"]]["inputs"][adv["clip"]["input"]]] + _lm_all
        if req.clip_name not in _allowed:
            raise HTTPException(400, f"非法 CLIP 模型：{req.clip_name}")
        s = adv["clip"]
        tpl[s["node"]]["inputs"][s["input"]] = req.clip_name
    if req.lora_name is not None and "lora" in adv:
        _allowed = _enabled_model_paths("lora") + [tpl[adv["lora"]["node"]]["inputs"][adv["lora"]["input"]]] + _lm_all
        if req.lora_name not in _allowed:
            raise HTTPException(400, f"非法 LoRA：{req.lora_name}")
        s = adv["lora"]
        tpl[s["node"]]["inputs"][s["input"]] = req.lora_name
    if req.lora_strength is not None:
        if not (0 <= req.lora_strength <= 1.5):
            raise HTTPException(400, "LoRA 强度需在 0~1.5 之间")
        if "lora" in adv:
            s = adv["lora"]
            tpl[s["node"]]["inputs"][s["strength_input"]] = float(req.lora_strength)
        elif "lora_strength" in adv:
            s = adv["lora_strength"]
            tpl[s["node"]]["inputs"][s["input"]] = float(req.lora_strength)
    if req.unet_name is not None and "unet" in adv:
        s = adv["unet"]
        _allowed = _enabled_model_paths("unet") + [tpl[s["node"]]["inputs"][s["input"]]] + _lm_all
        if req.unet_name not in _allowed:
            raise HTTPException(400, f"非法 UNET 模型：{req.unet_name}")
        tpl[s["node"]]["inputs"][s["input"]] = req.unet_name

    prompt_id = _comfy_post_prompt(tpl, lane)
    _register_task(prompt_id, lane["id"], req.mode)
    return {"prompt_id": prompt_id, "lane": {"id": lane["id"], "name": lane["name"]}, "seed": seed_used}


# ---------------- 状态查询 ----------------

@app.get("/api/status/{prompt_id}")
def api_status(prompt_id: str):
    lane = _task_lane(prompt_id)
    lane_info = {"id": lane["id"], "name": lane["name"]}
    entry = _get_history(prompt_id, lane)
    if entry is not None:
        with _progress_lock:
            _task_start.pop(prompt_id, None)  # 任务已结束，清理计时
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            msgs = [
                m[1].get("exception_message", str(m[1]))
                for m in status.get("messages", [])
                if m[0] == "execution_error"
            ]
            return {"state": "error", "error": "; ".join(msgs) or "执行出错", "outputs": [], "lane": lane_info}
        if lane["id"] != "local":
            _sync_lane_outputs(lane, prompt_id)  # 云端任务先同步回传再组输出
        return {
            "state": "done",
            "outputs": _history_outputs(entry, lane),
            "elapsed_seconds": _elapsed_seconds(entry),
            "lane": lane_info,
        }

    # 不在 history，查该车道队列
    q = _get_queue_lane(lane)
    for item in q.get("queue_running", []):
        if len(item) > 1 and item[1] == prompt_id:
            with _progress_lock:
                if len(_task_start) > 500:  # 防止无限增长
                    _task_start.clear()
                started = _task_start.setdefault(prompt_id, time.time())
            return {
                "state": "running",
                "outputs": [],
                "progress": _get_progress(lane, prompt_id),
                "elapsed_running": round(time.time() - started, 1),  # 已花费的生成时间（秒）
                "lane": lane_info,
            }
    for idx, item in enumerate(q.get("queue_pending", [])):
        if len(item) > 1 and item[1] == prompt_id:
            return {"state": "queued", "queue_position": idx + 1, "outputs": [], "lane": lane_info}
    with _progress_lock:
        _task_start.pop(prompt_id, None)
    return {"state": "error", "error": "任务不存在（可能已被清除）", "outputs": [], "lane": lane_info}


# ---------------- 车道状态 ----------------

@app.get("/api/lanes")
def api_lanes():
    out = []
    for l in LANES:
        st = "offline"
        if l["online"]:
            if l["queue_running"] > 0:
                st = "generating"
            elif l["queue_pending"] > 0:
                st = "queued"
            else:
                st = "idle"
        out.append({
            "id": l["id"],
            "name": l["name"],
            "online": l["online"],
            "queue_running": l["queue_running"],
            "queue_pending": l["queue_pending"],
            "status": st,
            "enabled": l["enabled"],
            "models": {k: v for k, v in (l.get("models") or {}).items() if v},
            "model_options": l.get("model_options") or {},
        })
    return {"lanes": out}


@app.get("/api/admin/lanes")
def admin_lanes():
    out = api_lanes()["lanes"]
    for l in LANES:
        for o in out:
            if o["id"] == l["id"]:
                o["enabled"] = l["enabled"]
                o["base_url"] = l["base_url"]
                break
    return {"lanes": out}


# ---------------- 文件代理 ----------------

@app.get("/api/file")
def api_file(request: Request, filename: str, subfolder: str = "", type: str = "output"):
    # 输出文件直接读本地磁盘并支持 Range（可拖动进度条）；其余类型仍走 ComfyUI 代理
    if type == "output":
        path = (OUTPUT_ROOT / subfolder / filename).resolve()
        if not str(path).startswith(str(OUTPUT_ROOT.resolve())) or not path.is_file():
            raise HTTPException(404, "文件不存在")
        size = path.stat().st_size
        ext = path.suffix.lower()
        mime = {
            ".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav",
            ".opus": "audio/ogg", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
            ".mp4": "video/mp4", ".webm": "video/webm",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
        }.get(ext, "application/octet-stream")
        range_h = request.headers.get("range")
        if range_h:
            import re as _re
            m = _re.match(r"bytes=(\d*)-(\d*)", range_h.strip())
            if m and (m.group(1) or m.group(2)):
                start = int(m.group(1)) if m.group(1) else 0
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                if start >= size or start > end:
                    raise HTTPException(416, "Range Not Satisfiable")
                length = end - start + 1

                def _iter():
                    with open(path, "rb") as f:
                        f.seek(start)
                        left = length
                        while left > 0:
                            chunk = f.read(min(256 * 1024, left))
                            if not chunk:
                                break
                            left -= len(chunk)
                            yield chunk

                return StreamingResponse(
                    _iter(),
                    status_code=206,
                    media_type=mime,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{size}",
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(length),
                    },
                )
        return FileResponse(path, media_type=mime, headers={"Accept-Ranges": "bytes"})
    try:
        r = requests.get(
            f"{COMFY}/view",
            params={"filename": filename, "subfolder": subfolder, "type": type},
            stream=True,
            timeout=300,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"无法连接 ComfyUI：{e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, "文件不存在或无法读取")
    headers = {}
    if r.headers.get("Content-Length"):
        headers["Content-Length"] = r.headers["Content-Length"]
    return StreamingResponse(
        r.iter_content(chunk_size=256 * 1024),
        media_type=r.headers.get("Content-Type", "application/octet-stream"),
        headers=headers,
    )


# ---------------- 缩略图（ffmpeg 抽帧/缩放，磁盘缓存） ----------------
_thumb_dir = None


def _get_thumb_dir():
    global _thumb_dir
    if _thumb_dir is None:
        _thumb_dir = OUTPUT_ROOT.parent / ".thumbs"
        _thumb_dir.mkdir(parents=True, exist_ok=True)
    return _thumb_dir


@app.get("/api/thumb")
def api_thumb(filename: str, subfolder: str = "", width: int = 320):
    path = (OUTPUT_ROOT / subfolder / filename) if subfolder else (OUTPUT_ROOT / filename)
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    mtime = int(path.stat().st_mtime_ns)
    d = _get_thumb_dir()
    cache_path = d / (
        hashlib.md5(f"{subfolder}/{filename}".encode()).hexdigest() + f"_{mtime}.jpg"
    )
    if not cache_path.exists():
        import subprocess

        import imageio_ffmpeg

        ext = path.suffix.lower()
        is_video = ext in (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gif", ".webp")
        # 视频: 快速 seek 到 0.2s 抽帧; 图片: 不能 seek, 用 yuvj420p 规避 PNG 色彩空间问题
        args = [imageio_ffmpeg.get_ffmpeg_exe(), "-y"]
        if is_video:
            args += ["-ss", "0.2"]
        args += ["-i", str(path), "-frames:v", "1", "-vf", f"scale={width}:-2"]
        if not is_video:
            args += ["-pix_fmt", "yuvj420p"]
        args += ["-q:v", "6", str(cache_path)]
        try:
            r = subprocess.run(
                args,
                capture_output=True,
                timeout=60,
            )
            ok = r.returncode == 0 and cache_path.exists()
        except Exception:
            ok = False
        if not ok:
            return FileResponse(path)  # 生成失败时回退原文件
        # 简单清理：超过 1000 个缩略图时清空（源文件删了也会重新生成）
        try:
            files = list(d.glob("*.jpg"))
            if len(files) > 1000:
                for f in files:
                    f.unlink(missing_ok=True)
        except Exception:
            pass
    return FileResponse(cache_path, media_type="image/jpeg")


# ---------------- 静态页面 ----------------

@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.exception_handler(HTTPException)
async def http_exc_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.middleware("http")
async def no_cache_static(request, call_next):
    # 静态页面/脚本禁止缓存，避免浏览器混用新旧版本导致页面渲染失败
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# 凭据配置（2026-09-25 起从源码移出，明文不再进 git）
# 优先级：环境变量 WEBAPP_AUTH_PASS 等 > auth_config.json > 首次运行自动生成随机密码
AUTH_CONFIG_FILE = BASE_DIR / "auth_config.json"


def _load_auth_config():
    """读取凭据配置；缺失字段自动补齐生成（密码随机），并落盘到 auth_config.json。"""
    cfg = {}
    if AUTH_CONFIG_FILE.exists():
        try:
            cfg = json.loads(AUTH_CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # 配置坏了也要能起来，用环境变量兜底
            print(f"[auth] 读取 {AUTH_CONFIG_FILE.name} 失败：{exc}", flush=True)
    generated = not (cfg.get("auth_pass") and cfg.get("admin_pass") and cfg.get("session_secret"))
    if generated:
        cfg.setdefault("auth_user", os.environ.get("WEBAPP_AUTH_USER", "admin"))
        cfg.setdefault("admin_user", os.environ.get("WEBAPP_ADMIN_USER", "admin"))
        cfg["auth_pass"] = cfg.get("auth_pass") or _secrets.token_urlsafe(9)
        cfg["admin_pass"] = cfg.get("admin_pass") or cfg["auth_pass"]
        cfg["session_secret"] = cfg.get("session_secret") or _secrets.token_hex(16)
        cfg.setdefault("secret_seq", ["circle", "circle", "square", "x"])
        try:
            AUTH_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[auth] 写入 {AUTH_CONFIG_FILE.name} 失败：{exc}", flush=True)
    for _k in ("auth_user", "auth_pass", "admin_user", "admin_pass", "session_secret"):
        _env = os.environ.get("WEBAPP_" + _k.upper())
        if _env:
            cfg[_k] = _env
    if generated:
        print("[auth] 已生成 {} —— 登录：{} / {}".format(
            AUTH_CONFIG_FILE, cfg["auth_user"], cfg["auth_pass"]), flush=True)
    return cfg


_AUTH_CFG = _load_auth_config()

# ---------------- 登录鉴权（表单 + Cookie 会话） ----------------
AUTH_USER = _AUTH_CFG["auth_user"]
AUTH_PASS = _AUTH_CFG["auth_pass"]            # 改密码改 auth_config.json，勿写回源码
ADMIN_USER = _AUTH_CFG["admin_user"]
ADMIN_PASS = _AUTH_CFG["admin_pass"]
SESSION_SECRET = _AUTH_CFG["session_secret"]  # 会话签名密钥，随配置持久化
SECRET_SEQ = list(_AUTH_CFG.get("secret_seq") or ["circle", "circle", "square", "x"])
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 天

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 · ComfyUI 工作台</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    background: #0a0a0c;
    color: #e8e9ec;
    font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; overflow: hidden;
  }
  .deco { position: fixed; border: 2px solid rgba(255,255,255,.05); border-radius: 50%; pointer-events: none; }
  .d1 { width: 480px; height: 480px; right: -100px; top: 8%; }
  .d2 { width: 300px; height: 300px; right: 160px; top: 30%; }
  .d3 { position: fixed; left: 0; right: 0; top: 58%; height: 2px;
        background: linear-gradient(90deg, rgba(134,239,172,0) 0%, rgba(134,239,172,.8) 35%, rgba(134,239,172,.8) 100%);
        opacity: .6; pointer-events: none; }
  .card {
    position: relative; z-index: 1;
    width: 340px; max-width: 90vw; padding: 38px 32px;
    background: rgba(255,255,255,.035);
    border: 1px solid rgba(255,255,255,.09);
    border-radius: 14px;
    backdrop-filter: blur(14px) saturate(1.2);
    -webkit-backdrop-filter: blur(14px) saturate(1.2);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.06);
  }
  h1 { font-size: 20px; letter-spacing: .12em; margin-bottom: 6px; }
  .sub { color: #8b8f99; font-size: 12px; letter-spacing: .2em; margin-bottom: 22px; }
  label { display: block; font-size: 12px; color: #aab0c0; margin: 14px 0 6px; }
  input {
    width: 100%; padding: 11px 12px; border-radius: 9px;
    border: 1px solid rgba(255,255,255,.12); background: rgba(255,255,255,.04);
    color: #e8e9ec; font-size: 15px; outline: none;
  }
  input:focus { border-color: #86efac; }
  button {
    width: 100%; margin-top: 24px; padding: 12px; border: none; border-radius: 9px;
    background: linear-gradient(135deg, #a7f3d0, #86efac); color: #0a0a0c;
    font-size: 15px; font-weight: 600; cursor: pointer;
  }
  button:hover { box-shadow: 0 4px 18px rgba(134,239,172,.25); }
  .err { color: #ff6b62; font-size: 13px; margin-top: 14px; min-height: 18px; }
</style>
</head>
<body>
  <span class="deco d1"></span>
  <span class="deco d2"></span>
  <span class="d3"></span>
  <form class="card" method="post" action="/login">
    <h1>ComfyUI 工作台</h1>
    <p class="sub">请登录后使用</p>
    <label>用户名</label>
    <input name="username" autocomplete="username" required autofocus>
    <label>密码</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <div class="err">__ERR__</div>
    <button type="submit">登 录</button>
  </form>
</body>
</html>"""


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_session() -> str:
    payload = f"{AUTH_USER}:{int(time.time())}"
    sig = _sign(payload)
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def _check_session(token: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user, ts, sig = raw.rsplit(":", 2)
        if not _secrets.compare_digest(sig, _sign(f"{user}:{ts}")):
            return False
        if user != AUTH_USER:
            return False
        return (time.time() - int(ts)) < SESSION_MAX_AGE
    except Exception:
        return False


@app.get("/login")
def login_page():
    return HTMLResponse(LOGIN_PAGE.replace("__ERR__", ""))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    user = form.get("username", "")
    pw = form.get("password", "")
    if _secrets.compare_digest(user, AUTH_USER) and _secrets.compare_digest(pw, AUTH_PASS):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("session", _make_session(), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
        return resp
    return HTMLResponse(LOGIN_PAGE.replace("__ERR__", "用户名或密码错误"), status_code=401)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# ---------------- 管理员界面（独立账号 + 独立会话） ----------------
# ADMIN_USER / ADMIN_PASS 来自 auth_config.json（见文件上部 _load_auth_config）

ADMIN_LOGIN_PAGE = LOGIN_PAGE.replace(
    '<title>登录 · ComfyUI 工作台</title>', '<title>管理员登录</title>'
).replace(
    '<h1>ComfyUI 工作台</h1>', '<h1>管理员界面</h1>'
).replace(
    '<p class="sub">请登录后使用</p>', '<p class="sub">管理员专用 · 请登录</p>'
).replace(
    'action="/login"', 'action="/admin/login"'
)


def _make_admin_session() -> str:
    payload = f"admin:{ADMIN_USER}:{int(time.time())}"
    sig = _sign(payload)
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def _check_admin_session(token: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        kind, user, ts, sig = raw.rsplit(":", 3)
        if kind != "admin" or user != ADMIN_USER:
            return False
        if not _secrets.compare_digest(sig, _sign(f"{kind}:{user}:{ts}")):
            return False
        return (time.time() - int(ts)) < SESSION_MAX_AGE
    except Exception:
        return False


@app.get("/admin.html")
def admin_page():
    return FileResponse(BASE_DIR / "static" / "admin.html")


@app.get("/admin/login")
def admin_login_page():
    return HTMLResponse(ADMIN_LOGIN_PAGE.replace("__ERR__", ""))


@app.post("/admin/login")
async def admin_login_submit(request: Request):
    form = await request.form()
    user = form.get("username", "")
    pw = form.get("password", "")
    if _secrets.compare_digest(user, ADMIN_USER) and _secrets.compare_digest(pw, ADMIN_PASS):
        resp = RedirectResponse("/admin.html", status_code=303)
        resp.set_cookie("admin_session", _make_admin_session(), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
        return resp
    return HTMLResponse(ADMIN_LOGIN_PAGE.replace("__ERR__", "用户名或密码错误"), status_code=401)


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin.html", status_code=303)
    resp.delete_cookie("admin_session")
    return resp


@app.get("/api/admin/check")
def admin_check(request: Request):
    return JSONResponse({"ok": _check_admin_session(request.cookies.get("admin_session", ""))})


@app.get("/api/admin/status")
def admin_status(request: Request):
    if not _check_admin_session(request.cookies.get("admin_session", "")):
        return JSONResponse(status_code=401, content={"error": "未登录"})
    comfy_up = False
    try:
        r = requests.get(f"{COMFY}/system_stats", timeout=3)
        comfy_up = r.status_code == 200
    except Exception:
        pass
    return JSONResponse({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "comfy_up": comfy_up,
        "version": "HorseDance 1.0",
    })


# ---------------- 硬件状态（CPU / 内存 / GPU / 显存） ----------------
def _perf_stats():
    perf = {"cpu": None, "mem": None, "gpu": None}
    try:  # 内存（Windows API）
        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.wintypes.DWORD),
                ("dwMemoryLoad", ctypes.wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        m = _MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        total_gb = m.ullTotalPhys / 1073741824.0
        perf["mem"] = {
            "used_gb": round(total_gb * m.dwMemoryLoad / 100.0, 1),
            "total_gb": round(total_gb, 1),
            "percent": m.dwMemoryLoad,
        }
    except Exception:
        pass
    try:  # CPU（GetSystemTimes 采样 0.2 秒）
        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.wintypes.DWORD), ("dwHighDateTime", ctypes.wintypes.DWORD)]

        def _times():
            idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
            def to_int(ft): return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
            return to_int(idle), to_int(kernel), to_int(user)
        i1, k1, u1 = _times()
        time.sleep(0.2)
        i2, k2, u2 = _times()
        idle_d = i2 - i1
        total_d = (k2 - k1) + (u2 - u1)
        perf["cpu"] = round((1 - idle_d / total_d) * 100, 1) if total_d else 0.0
    except Exception:
        pass
    try:  # GPU（nvidia-smi）
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        parts = [p.strip() for p in out.stdout.strip().split(",")]
        if len(parts) == 4:
            perf["gpu"] = {
                "name": parts[0],
                "util": float(parts[1]),
                "vram_used_gb": round(float(parts[2]) / 1024, 1),
                "vram_total_gb": round(float(parts[3]) / 1024, 1),
            }
    except Exception:
        pass
    return perf


@app.get("/api/admin/perf")
def admin_perf():
    return JSONResponse(_perf_stats())


@app.post("/api/admin/free")
def admin_free():
    """释放内存与显存：调用 ComfyUI /free 卸载模型+清缓存；有任务生成时自动跳过（保护运行中的任务）"""
    busy = False
    try:
        q = requests.get(f"{COMFY}/queue", timeout=5).json()
        busy = bool(q.get("queue_running")) or bool(q.get("queue_pending"))
    except Exception:
        pass
    result = {"busy": busy}
    if busy:
        result["message"] = "有任务生成中，已跳过释放（避免打断）"
    else:
        try:
            r = requests.post(f"{COMFY}/free", json={"unload_models": "true", "free_memory": "true"}, timeout=30)
            result["comfy_free"] = r.status_code
        except Exception as e:
            result["comfy_free"] = f"error: {e}"
        gc.collect()
        try:
            ctypes.windll.kernel32.SetProcessWorkingSetSize(-1, -1, -1)  # 裁剪进程工作集
        except Exception:
            pass
        result["message"] = "已释放内存与显存"
    result["perf"] = _perf_stats()
    return JSONResponse(result)


# ---------------- 取消任务（中断当前 + 从队列删除） ----------------
@app.post("/api/cancel")
def api_cancel(pid: str = ""):
    lane = _task_lane(pid)
    base = lane["base_url"]
    try:
        requests.post(f"{base}/interrupt", timeout=5)  # 中断正在运行的任务
    except Exception:
        pass
    if pid:
        try:
            requests.post(f"{base}/queue", json={"delete": [pid]}, timeout=5)  # 删除排队中的任务
        except Exception:
            pass
        with _progress_lock:
            _task_start.pop(pid, None)
    return {"ok": True, "lane": {"id": lane["id"], "name": lane["name"]}}


# ---------------- 秘密入口（PS 手柄图标组合 ◯◯□✕） ----------------
# SECRET_SEQ 来自 auth_config.json（见文件上部 _load_auth_config）
SECRET_LOCK_MIN = 30
SECRET_STATE_FILE = BASE_DIR / "admin_secret_state.json"


def _secret_state():
    try:
        return json.loads(SECRET_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"wrong": 0, "lock_until": 0}


def _save_secret_state(st):
    try:
        SECRET_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _secret_effective_state():
    """锁定过期则自动解锁并清零错误次数"""
    st = _secret_state()
    if st.get("lock_until", 0) and time.time() > st["lock_until"]:
        st = {"wrong": 0, "lock_until": 0}
        _save_secret_state(st)
    return st


@app.get("/api/admin/secret/status")
def admin_secret_status(request: Request):
    st = _secret_effective_state()
    return {
        "locked": bool(st.get("lock_until")),
        "lock_until": st.get("lock_until", 0),
        "is_admin": _check_admin_session(request.cookies.get("admin_session", "")),
    }


@app.post("/api/admin/secret")
def admin_secret(request: Request, seq: str = ""):
    st = _secret_effective_state()
    if st.get("lock_until"):  # 锁定中：一律拒绝（正确序列也无效）
        return JSONResponse({"ok": False, "locked": True, "lock_until": st["lock_until"]})
    seq_list = [s.strip() for s in seq.split(",") if s.strip()]
    if seq_list == SECRET_SEQ:
        resp = JSONResponse({"ok": True})
        resp.set_cookie("admin_session", _make_admin_session(), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
        _save_secret_state({"wrong": 0, "lock_until": 0})
        return resp
    st["wrong"] = st.get("wrong", 0) + 1
    locked = False
    if st["wrong"] >= 3:
        st["lock_until"] = int(time.time()) + SECRET_LOCK_MIN * 60
        st["wrong"] = 0
        locked = True
    _save_secret_state(st)
    return JSONResponse({"ok": False, "wrong": st["wrong"], "locked": locked, "lock_until": st.get("lock_until", 0)})


# ---------------- 模型文件管理（管理员） ----------------
@app.get("/api/admin/models")
def admin_models_list(cat: str):
    if cat not in MODEL_CATS:
        raise HTTPException(400, "未知分类")
    files = _scan_models(cat)
    dis = _disabled_set(cat)
    for f in files:
        f["enabled"] = f["name"] not in dis
    free_gb = None
    try:
        free_gb = round(shutil.disk_usage(MODEL_CATS[cat]["roots"][0]).free / 1e9, 1)
    except Exception:
        pass
    return {
        "label": MODEL_CATS[cat]["label"],
        "roots": [str(r) for r in MODEL_CATS[cat]["roots"]],
        "files": files,
        "free_gb": free_gb,
    }


@app.get("/api/admin/models/check")
def admin_models_check():
    """手动检查：汇总三类模型状态 + 隐患告警"""
    out = []
    warns = []
    for cat in MODEL_CATS:
        files = _scan_models(cat)
        dis = _disabled_set(cat)
        total = sum(f["size"] for f in files)
        free_gb = None
        try:
            free_gb = round(shutil.disk_usage(MODEL_CATS[cat]["roots"][0]).free / 1e9, 1)
        except Exception:
            pass
        out.append({
            "cat": cat,
            "label": MODEL_CATS[cat]["label"],
            "count": len(files),
            "enabled_count": sum(1 for f in files if f["name"] not in dis),
            "total_bytes": total,
            "free_gb": free_gb,
        })
        # 模板默认模型是否缺失
        for mode_id, spec in MODES_JSON.items():
            adv = spec.get("advanced", {})
            for key, cat_key in (("clip", "clip"), ("lora", "lora"), ("unet", "unet")):
                if cat_key != cat or key not in adv:
                    continue
                try:
                    tpl = _load_template(mode_id)
                    s = adv[key]
                    default = tpl[s["node"]]["inputs"][s["input"]]
                    if not _find_model_file(cat, default)[1]:
                        warns.append(f"模式[{mode_id}] 默认 {key.upper()} 模型缺失：{default}")
                except Exception:
                    pass
        # 禁用名单里已不存在的文件
        for name in dis:
            if not _find_model_file(cat, name)[1]:
                warns.append(f"[{cat}] 禁用名单含已不存在的文件：{name}")
    return {"categories": out, "warnings": warns}


@app.post("/api/admin/models/open")
def admin_models_open(cat: str, root: str = ""):
    """在服务器上打开模型所在文件夹（资源管理器）"""
    if cat not in MODEL_CATS:
        raise HTTPException(400, "未知分类")
    if root and root in [str(r) for r in MODEL_CATS[cat]["roots"]]:
        folder = Path(root)
    else:
        folder = MODEL_CATS[cat]["roots"][0]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(str(folder))
    except Exception as e:
        raise HTTPException(500, f"无法打开文件夹：{e}")
    return {"ok": True, "folder": str(folder)}


@app.post("/api/admin/models/upload")
async def admin_models_upload(request: Request, cat: str, name: str, subfolder: str = "", root: str = "", overwrite: str = "false"):
    if cat not in MODEL_CATS:
        raise HTTPException(400, "未知分类")
    name = Path(name).name  # 只取文件名，防路径穿越
    if not name or name.startswith(".uploading-"):
        raise HTTPException(400, "非法文件名")
    # 子目录净化：仅允许普通层级，拒绝 .. 与绝对路径
    subfolder = subfolder.replace("\\", "/").strip("/")
    parts = [x for x in subfolder.split("/") if x and x not in (".", "..")]
    subfolder = "/".join(parts)
    # 目标根目录：优先取已包含该子目录的根（让文件与同目录其它文件放一起），否则默认第一个根
    if root and root in [str(r) for r in MODEL_CATS[cat]["roots"]]:
        dest_root = Path(root)
    else:
        dest_root = MODEL_CATS[cat]["roots"][0]
        if subfolder:
            for r in MODEL_CATS[cat]["roots"]:
                if (r / subfolder).is_dir():
                    dest_root = r
                    break
    dest_dir = dest_root / subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)
    rel = ((subfolder + "/" + name) if subfolder else name).replace("/", "\\")
    final = dest_dir / name
    if final.exists() and overwrite != "true":
        raise HTTPException(409, "文件已存在（勾选“覆盖同名”可替换，旧文件进回收站）")
    tmp = dest_dir / f".uploading-{uuid.uuid4().hex[:8]}-{name}"
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        if final.exists():
            _move_to_trash(cat, rel, dest_root)
        os.replace(str(tmp), str(final))
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(500, f"上传失败：{e}")
    return {"ok": True, "name": rel, "root": str(dest_root)}


@app.post("/api/admin/models/delete")
def admin_models_delete(cat: str, name: str):
    if cat not in MODEL_CATS:
        raise HTTPException(400, "未知分类")
    root, p = _find_model_file(cat, name)
    if not p:
        raise HTTPException(404, "文件不存在")
    _move_to_trash(cat, name, root)
    return {"ok": True}


@app.post("/api/admin/models/toggle")
def admin_models_toggle(cat: str, name: str, enabled: str):
    if cat not in MODEL_CATS:
        raise HTTPException(400, "未知分类")
    st = _load_models_state()
    dis = set(st.setdefault("disabled", {}).setdefault(cat, []))
    if enabled == "true":
        dis.discard(name)
    else:
        dis.add(name)
    st["disabled"][cat] = sorted(dis)
    _save_models_state(st)
    return {"ok": True, "enabled": enabled == "true"}


@app.get("/api/admin/models/trash")
def admin_models_trash(cat: str):
    if cat not in MODEL_CATS:
        raise HTTPException(400, "未知分类")
    d = MODELS_TRASH_DIR / cat
    out = []
    if d.is_dir():
        for p in d.rglob("*"):
            if p.is_file() and not p.name.endswith(".meta.json"):
                try:
                    meta = json.loads(Path(str(p) + ".meta.json").read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
                out.append({
                    "name": str(p.relative_to(d)),
                    "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                    "root": meta.get("root", ""),
                })
    out.sort(key=lambda f: f["name"].lower())
    return {"files": out}


@app.post("/api/admin/models/trash/restore")
def admin_models_trash_restore(cat: str, name: str):
    if cat not in MODEL_CATS:
        raise HTTPException(400, "未知分类")
    d = MODELS_TRASH_DIR / cat / name
    if not d.is_file():
        raise HTTPException(404, "回收站中不存在该文件")
    meta = {}
    try:
        meta = json.loads(Path(str(d) + ".meta.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    dest_root = Path(meta.get("root", "")) if meta.get("root") else MODEL_CATS[cat]["roots"][0]
    dest = dest_root / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(d), str(dest))
    try:
        Path(str(d) + ".meta.json").unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/admin/models/trash/clear")
def admin_models_trash_clear(cat: str):
    if cat not in MODEL_CATS:
        raise HTTPException(400, "未知分类")
    d = MODELS_TRASH_DIR / cat
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


# ---------------- 输入/输出文件管理（管理员，可远程拷贝） ----------------
FILE_ROOTS = {
    "input": cfg.input_root,
    "output": cfg.output_root,
    "workflow": cfg.workflow_dir,
}
FILE_TRASH_DIR = cfg.file_trash_dir


def _safe_file_path(root_key: str, rel: str):
    """解析 root_key + 相对路径，拒绝越出根目录（防路径穿越）"""
    if root_key not in FILE_ROOTS:
        raise HTTPException(400, "未知根目录")
    root = FILE_ROOTS[root_key]
    rel = rel.replace("\\", "/").strip("/")
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise HTTPException(400, "非法路径")
    return root, p


def _file_rel(root_key: str, p: Path):
    return str(p.relative_to(FILE_ROOTS[root_key])).replace("\\", "/")


def _file_trash_move(root_key: str, rel: str):
    """把文件/文件夹移入回收站（扁平存储，元数据记录原路径，可恢复）"""
    d = FILE_TRASH_DIR / root_key / f"{Path(rel).name}__{uuid.uuid4().hex[:6]}"
    d.parent.mkdir(parents=True, exist_ok=True)
    src = FILE_ROOTS[root_key] / rel
    if src.exists():
        shutil.move(str(src), str(d))
    meta = Path(str(d) + ".meta.json")
    meta.write_text(json.dumps({"root": root_key, "path": rel}, ensure_ascii=False), encoding="utf-8")


@app.get("/api/admin/files")
def admin_files_list(root: str, path: str = ""):
    root_p, p = _safe_file_path(root, path)
    if not p.is_dir():
        raise HTTPException(404, "目录不存在")
    items = []
    for c in p.iterdir():
        if c.name.startswith("."):  # 隐藏 .thumbs 等缓存目录
            continue
        try:
            st = c.stat()
        except OSError:
            continue
        items.append({
            "name": c.name,
            "is_dir": c.is_dir(),
            "size": st.st_size if c.is_file() else None,
            "mtime": st.st_mtime,
        })
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    try:
        free_gb = round(shutil.disk_usage(root_p).free / 1e9, 1)
    except Exception:
        free_gb = None
    return {"root": root, "path": path, "items": items, "free_gb": free_gb}


@app.get("/api/admin/files/file")
def admin_files_file(root: str, path: str, download: str = "0"):
    _, p = _safe_file_path(root, path)
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(
        p,
        filename=p.name,
        content_disposition_type="attachment" if download == "1" else "inline",
    )


@app.post("/api/admin/files/upload")
async def admin_files_upload(request: Request, root: str, path: str = "", name: str = "", overwrite: str = "false"):
    _, dir_p = _safe_file_path(root, path)
    if not dir_p.is_dir():
        raise HTTPException(404, "目录不存在")
    name = Path(name).name
    if not name:
        raise HTTPException(400, "非法文件名")
    final = dir_p / name
    if final.exists() and overwrite != "true":
        raise HTTPException(409, "文件已存在（勾选覆盖可替换）")
    tmp = dir_p / f".up-{uuid.uuid4().hex[:8]}-{name}"
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        if final.exists():
            final.unlink()
        os.replace(str(tmp), str(final))
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(500, f"上传失败：{e}")
    return {"ok": True, "name": name}


@app.post("/api/admin/files/mkdir")
def admin_files_mkdir(root: str, path: str = "", name: str = ""):
    _, dir_p = _safe_file_path(root, path)
    name = Path(name).name
    if not name:
        raise HTTPException(400, "非法目录名")
    (dir_p / name).mkdir(parents=True, exist_ok=True)
    return {"ok": True}


@app.post("/api/admin/files/rename")
def admin_files_rename(root: str, path: str = "", old: str = "", new: str = ""):
    _, dir_p = _safe_file_path(root, path)
    src = dir_p / Path(old).name
    dst = dir_p / Path(new).name
    if not src.exists():
        raise HTTPException(404, "文件不存在")
    if dst.exists():
        raise HTTPException(409, "目标已存在")
    os.rename(str(src), str(dst))
    return {"ok": True}


@app.post("/api/admin/files/copy")
def admin_files_copy(root: str, path: str = "", name: str = "", target_root: str = "", target_path: str = ""):
    _, src_dir = _safe_file_path(root, path)
    src = src_dir / Path(name).name
    if not src.exists():
        raise HTTPException(404, "文件不存在")
    _, t_dir = _safe_file_path(target_root, target_path)
    dst = t_dir / src.name
    if dst.exists():
        raise HTTPException(409, "目标已存在")
    if src.is_dir():
        shutil.copytree(str(src), str(dst))
    else:
        shutil.copy2(str(src), str(dst))
    return {"ok": True, "target": str(dst)}


@app.post("/api/admin/files/delete")
def admin_files_delete(root: str, path: str = "", name: str = ""):
    rel = (path + "/" + name) if path else name
    _, p = _safe_file_path(root, rel)
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    _file_trash_move(root, rel)
    return {"ok": True}


@app.get("/api/admin/files/trash")
def admin_files_trash(root: str):
    d = FILE_TRASH_DIR / root
    out = []
    if d.is_dir():
        for c in d.iterdir():
            if c.name.endswith(".meta.json"):
                continue
            try:
                meta = json.loads(Path(str(c) + ".meta.json").read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            try:
                st = c.stat()
            except OSError:
                continue
            out.append({
                "key": c.name,
                "name": meta.get("path", c.name),  # 显示原相对路径
                "is_dir": c.is_dir(),
                "size": st.st_size if c.is_file() else None,
                "mtime": st.st_mtime,
            })
    out.sort(key=lambda f: f["name"].lower())
    return {"files": out}


@app.post("/api/admin/files/trash/restore")
def admin_files_trash_restore(root: str, key: str):
    d = FILE_TRASH_DIR / root / Path(key).name
    if not d.exists():
        raise HTTPException(404, "回收站中不存在该文件")
    meta = {}
    try:
        meta = json.loads(Path(str(d) + ".meta.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    dest_root = meta.get("root", root)
    dest = FILE_ROOTS[dest_root] / meta.get("path", key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d), str(dest))
    try:
        Path(str(d) + ".meta.json").unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/admin/files/trash/clear")
def admin_files_trash_clear(root: str):
    d = FILE_TRASH_DIR / root
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


# ---------------- HYPIT 工坊（Codex 驱动的本地出片模式，仅本机可用） ----------------
# 设计：浏览器对话 → 每条消息 spawn 一次 `codex exec --json`（首轮）或
# `codex exec resume <session_id> --json`（后续），stdout 的 JSONL 事件流经 SSE 推给前端。
# 安全（2026-09-29 实测）：
#   1. 仅回环直连可用 —— cpolar 隧道流量也来自 127.0.0.1，靠转发头识别并拒绝（见中间件）；
#   2. codex 以 `-s workspace-write` 运行，可写范围只有项目目录，越界写入被拒；
#   3. exec 模式审批策略为 never，agent 不会发起提权。
HYPIT_ROOTS = [
    {"label": "HYPIT 本地产线", "path": Path(cfg.hypit_root), "new_under": "productions"},
    {"label": "我的视频作品", "path": Path(cfg.video_projects_root), "new_under": ""},
]
HYPIT_STATE_FILE = BASE_DIR / "hypit_state.json"
CODEX_CMD = cfg.codex_cmd
HYPIT_CMD = cfg.hypit_cmd
_hypit_chat_locks = {}
_hypit_chat_locks_guard = threading.Lock()
# 出现任一转发头即视为经过代理（隧道），不按本机直连对待
_FORWARDED_HEADERS = ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "forwarded", "x-forwarded-host")


def _is_loopback_direct(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1"):
        return False
    return not any(request.headers.get(h) for h in _FORWARDED_HEADERS)


def _hypit_require_local(request: Request):
    if not _is_loopback_direct(request):
        raise HTTPException(403, "HYPIT 工坊仅本机可用")


def _hypit_state():
    try:
        return json.loads(HYPIT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_hypit_state(st):
    HYPIT_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def _hypit_scan_projects():
    """项目 = 根目录自身或下一级子目录中，含 BRIEF.md 或 *.svrun/*.svml 的目录；
    productions\\ 子目录再多扫一层（作品集默认收在该子目录下）。"""
    out = []
    for ridx, root in enumerate(HYPIT_ROOTS):
        rpath = root["path"]
        if not rpath.is_dir():
            continue
        candidates = [rpath] + [d for d in sorted(rpath.iterdir()) if d.is_dir()]
        for d in list(candidates):
            sub = d / "productions"
            if sub.is_dir():
                candidates += [s for s in sorted(sub.iterdir()) if s.is_dir()]
        for d in candidates:
            try:
                has_brief = (d / "BRIEF.md").is_file()
                has_run = any(d.glob("*.svrun")) or any(d.glob("*.svml"))
                if not has_brief and not has_run:
                    continue
                rel = "." if d == rpath else d.relative_to(rpath).as_posix()
                pending = done = 0
                prog = d / "PROGRESS.md"
                if prog.is_file():
                    txt = prog.read_text(encoding="utf-8", errors="replace")
                    pending = len(re.findall(r"- \[ \]", txt))
                    done = len(re.findall(r"- \[x\]", txt, re.I))
                finals = [p.name for p in sorted(d.glob("final*.mp4"))]
                rd = d / "renders"
                if rd.is_dir():
                    finals += [f"renders/{p.name}" for p in sorted(rd.glob("*.mp4"))]
                out.append({
                    "pid": f"{ridx}:{rel}",
                    "name": root["label"] if rel == "." else d.name,
                    "path": str(d), "root": root["label"],
                    "pending": pending, "done": done, "finals": finals,
                    "mtime": d.stat().st_mtime,
                })
            except Exception:
                continue
    out.sort(key=lambda p: p["mtime"], reverse=True)
    return out


def _hypit_project_dir(pid: str) -> Path:
    try:
        ridx_s, rel = pid.split(":", 1)
        root = HYPIT_ROOTS[int(ridx_s)]["path"].resolve()
    except Exception:
        raise HTTPException(400, "pid 格式错误")
    d = root if rel == "." else (root / rel).resolve()
    if d != root and root not in d.parents:
        raise HTTPException(403, "越界路径")
    if not d.is_dir():
        raise HTTPException(404, "项目不存在")
    return d


@app.get("/api/hypit/projects")
def api_hypit_projects(request: Request):
    _hypit_require_local(request)
    state = _hypit_state()
    sessions = state.get("sessions", {})
    downloads = state.get("downloads", {})
    projs = _hypit_scan_projects()
    for p in projs:
        p["has_session"] = p["pid"] in sessions
        if p["pid"] in downloads:
            p["download"] = downloads[p["pid"]]
    return {"projects": projs}


class HypitNewReq(BaseModel):
    root_idx: int
    name: str
    requirement: str = ""
    ref_link: str = ""


_HYPIT_BRIEF_TEMPLATE = """# Brief · {name}

## 委托

{requirement}

## 交付


## 参考档案
{ref_section}

## 服务与付费约定

- 路线：本地 ComfyUI（零 API 费用）/ HypiHub（付费；付费生成前必须先确认）

## 权威关系

- 本 Brief 是创作决定；有改动先更新这里
"""


def _hypit_brief_add_ref(proj: Path, bullet: str):
    """往 BRIEF.md 的「参考档案」小节追加一条；没有该小节则补在文末。"""
    brief = proj / "BRIEF.md"
    line = f"- {bullet}"
    try:
        txt = brief.read_text(encoding="utf-8") if brief.is_file() else ""
        if "## 参考档案" in txt:
            txt = txt.replace("## 参考档案", "## 参考档案\n" + line, 1)
        else:
            txt = txt.rstrip() + "\n\n## 参考档案\n" + line + "\n"
        brief.write_text(txt if txt.endswith("\n") else txt + "\n", encoding="utf-8")
    except Exception:
        pass


def _hypit_dl_set(pid, status, error=None, file=None, url=None):
    st = _hypit_state()
    entry = {"status": status, "error": error, "file": file}
    if url:
        entry["url"] = url
    st.setdefault("downloads", {})[pid] = entry
    _save_hypit_state(st)


def _hypit_fetch_link(pid, proj: Path, url: str):
    """后台线程：用 hypit media fetch（yt-dlp）把参考链接下载到项目 references\\source.mp4。"""
    def work():
        refs = proj / "references"
        refs.mkdir(exist_ok=True)
        dest = refs / "source.mp4"
        cmd = f'"{HYPIT_CMD}" media fetch "{url}" --to "{dest}"'
        try:
            r = subprocess.run(
                cmd, cwd=str(HYPIT_ROOTS[0]["path"]), shell=True,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=1800,
            )
            if r.returncode == 0 and dest.is_file():
                _hypit_dl_set(pid, "done", file="references/source.mp4")
                _hypit_brief_add_ref(proj, f"链接已下载：`references/source.mp4`（{url}）")
            else:
                tail = ((r.stderr or "") + (r.stdout or "")).strip()[-300:]
                _hypit_dl_set(pid, "error", error=tail or f"退出码 {r.returncode}")
        except Exception as exc:
            _hypit_dl_set(pid, "error", error=str(exc)[:300])
    threading.Thread(target=work, daemon=True).start()


@app.post("/api/hypit/new")
def api_hypit_new(req: HypitNewReq, request: Request):
    _hypit_require_local(request)
    name = re.sub(r'[\\/:*?"<>|]', "", req.name).strip().strip(".")
    if not name:
        raise HTTPException(400, "项目名不能为空")
    if not (0 <= req.root_idx < len(HYPIT_ROOTS)):
        raise HTTPException(400, "root_idx 无效")
    ref_link = (req.ref_link or "").strip()
    if ref_link and not ref_link.startswith(("http://", "https://")):
        raise HTTPException(400, "参考链接需以 http(s):// 开头")
    root = HYPIT_ROOTS[req.root_idx]
    parent = root["path"] / root["new_under"] if root["new_under"] else root["path"]
    d = parent / name
    if d.exists():
        raise HTTPException(400, "同名项目已存在")
    d.mkdir(parents=True)
    # 项目级 AGENTS.md：codex 进目录自动读，环境速查（ffmpeg / ComfyUI / 模型 / 现成工作流）
    agents_tpl = BASE_DIR / "hypit_agents.md"
    if agents_tpl.is_file():
        shutil.copy(str(agents_tpl), str(d / "AGENTS.md"))
    ref_section = f"- 链接参考：{ref_link}（下载中，落点 `references/source.mp4`）" if ref_link else "（暂无，可后补）"
    (d / "BRIEF.md").write_text(
        _HYPIT_BRIEF_TEMPLATE.format(
            name=name,
            requirement=(req.requirement or "").strip() or "（待补充）",
            ref_section=ref_section,
        ),
        encoding="utf-8",
    )
    pid = f"{req.root_idx}:{d.relative_to(root['path']).as_posix()}"
    if ref_link:
        _hypit_dl_set(pid, "running", url=ref_link)
        _hypit_fetch_link(pid, d, ref_link)
    return {"ok": True, "pid": pid}


class HypitRefetchReq(BaseModel):
    pid: str
    ref_link: str


@app.post("/api/hypit/refetch")
def api_hypit_refetch(req: HypitRefetchReq, request: Request):
    """参考链接下载失败后的重试。"""
    _hypit_require_local(request)
    proj = _hypit_project_dir(req.pid)
    url = (req.ref_link or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "参考链接需以 http(s):// 开头")
    cur = _hypit_state().get("downloads", {}).get(req.pid, {})
    if cur.get("status") == "running":
        raise HTTPException(409, "已有下载进行中")
    _hypit_dl_set(req.pid, "running", url=url)
    _hypit_fetch_link(req.pid, proj, url)
    return {"ok": True}


@app.post("/api/hypit/upload")
async def api_hypit_upload(request: Request, pid: str = Form(...), file: UploadFile = File(...)):
    """上传参考视频到项目 references\\；重名自动加序号。"""
    _hypit_require_local(request)
    base = _hypit_project_dir(pid)
    name = re.sub(r'[\\/:*?"<>|]', "_", Path(file.filename or "ref.bin").name).strip() or "ref.bin"
    refs = base / "references"
    refs.mkdir(exist_ok=True)
    dest = refs / name
    if dest.exists():
        i = 1
        while (refs / f"{dest.stem}-{i}{dest.suffix}").exists():
            i += 1
        dest = refs / f"{dest.stem}-{i}{dest.suffix}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    rel = f"references/{dest.name}"
    _hypit_brief_add_ref(base, f"上传参考：`{rel}`")
    return {"ok": True, "path": rel}


_HYPIT_SKIP_DIRS = {"node_modules", ".git", ".hypit", "__pycache__", ".venv", "venv"}
_HYPIT_KIND = {
    ".md": "md", ".txt": "text", ".json": "text", ".svml": "text", ".svrun": "text",
    ".svs": "text", ".ps1": "text", ".bat": "text", ".js": "text", ".mjs": "text",
    ".py": "text", ".log": "text", ".toml": "text", ".yaml": "text", ".yml": "text",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
    ".mp4": "video", ".webm": "video", ".mov": "video",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio",
}
_HYPIT_MIME = {
    "image": {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "webp": "image/webp", "gif": "image/gif"},
    "video": {"mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime"},
    "audio": {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "flac": "audio/flac"},
}


@app.get("/api/hypit/files")
def api_hypit_files(request: Request, pid: str):
    _hypit_require_local(request)
    base = _hypit_project_dir(pid)
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [x for x in dirnames if x not in _HYPIT_SKIP_DIRS and not x.startswith(".")]
        if len(Path(dirpath).relative_to(base).parts) >= 4:
            dirnames[:] = []
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            try:
                fst = p.stat()
            except Exception:
                continue
            out.append({
                "path": p.relative_to(base).as_posix(),
                "size": fst.st_size, "mtime": fst.st_mtime,
                "kind": _HYPIT_KIND.get(p.suffix.lower(), "other"),
            })
            if len(out) >= 800:
                break
        if len(out) >= 800:
            break
    out.sort(key=lambda f: f["path"])
    return {"files": out}


@app.get("/api/hypit/file")
def api_hypit_file(request: Request, pid: str, path: str):
    _hypit_require_local(request)
    base = _hypit_project_dir(pid)
    p = (base / path).resolve()
    if p != base and base not in p.parents:
        raise HTTPException(403, "越界路径")
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    kind = _HYPIT_KIND.get(p.suffix.lower(), "other")
    if kind in ("md", "text"):
        if p.stat().st_size > 512 * 1024:
            raise HTTPException(413, "文件过大，不提供预览")
        return {"kind": kind, "name": p.name, "text": p.read_text(encoding="utf-8", errors="replace")}
    mime = _HYPIT_MIME.get(kind, {}).get(p.suffix.lower().lstrip("."))
    if not mime:
        raise HTTPException(415, "不支持预览的文件类型")
    return FileResponse(p, media_type=mime, headers={"Accept-Ranges": "bytes"})


class HypitChatReq(BaseModel):
    pid: str
    message: str


def _hypit_sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _hypit_run_turn(pid, proj: Path, msg: str):
    """跑一轮 codex exec，把 JSONL 事件翻译成 SSE。消息经 stdin 传入（避免命令行转义问题）。"""
    sid = _hypit_state().get("sessions", {}).get(pid, {}).get("session_id")
    if sid:
        # resume 子命令没有 -s 选项；sandbox 随原会话继承（2026-09-29 实测越界写入仍被拒）
        cmd = f'"{CODEX_CMD}" exec resume --json --skip-git-repo-check {sid} -'
    else:
        cmd = f'"{CODEX_CMD}" exec --json --skip-git-repo-check -s workspace-write -'
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(proj), shell=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except Exception as exc:
        yield _hypit_sse({"ev": "error", "text": f"codex 启动失败：{exc}"})
        return
    try:
        proc.stdin.write(msg)
        proc.stdin.close()
    except Exception:
        pass
    new_sid = None
    code = -1
    err_lines = []  # 非 JSONL 输出（启动报错等），退出码非 0 时透出
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                if line:
                    err_lines.append(line)
                    err_lines = err_lines[-8:]
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            t = ev.get("type")
            if t == "thread.started":
                new_sid = ev.get("thread_id")
                yield _hypit_sse({"ev": "session", "session_id": new_sid})
            elif t == "item.started":
                item = ev.get("item", {})
                if item.get("type") == "command_execution":
                    yield _hypit_sse({"ev": "cmd_start", "id": item.get("id"),
                                      "command": item.get("command", "")})
            elif t == "item.completed":
                item = ev.get("item", {})
                it = item.get("type")
                if it == "agent_message":
                    yield _hypit_sse({"ev": "msg", "text": item.get("text", "")})
                elif it == "command_execution":
                    yield _hypit_sse({"ev": "cmd", "id": item.get("id"),
                                      "command": item.get("command", ""),
                                      "output": (item.get("aggregated_output") or "")[-4000:],
                                      "exit_code": item.get("exit_code")})
                elif it == "file_change":
                    yield _hypit_sse({"ev": "file", "changes": item.get("changes", [])})
            elif t == "turn.completed":
                yield _hypit_sse({"ev": "usage", "usage": ev.get("usage", {})})
            elif t == "error":
                yield _hypit_sse({"ev": "error", "text": ev.get("message") or "未知错误"})
        code = proc.wait()
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        yield _hypit_sse({"ev": "error", "text": f"会话中断：{exc}"})
        return
    if new_sid:
        st = _hypit_state()
        st.setdefault("sessions", {})[pid] = {"session_id": new_sid, "updated": int(time.time())}
        _save_hypit_state(st)
    if code != 0:
        detail = "；".join(err_lines[-3:])
        yield _hypit_sse({"ev": "error", "text": f"codex 退出码 {code}" + (f"：{detail}" if detail else "")})
    yield _hypit_sse({"ev": "done", "code": code})


@app.post("/api/hypit/chat")
def api_hypit_chat(req: HypitChatReq, request: Request):
    _hypit_require_local(request)
    proj = _hypit_project_dir(req.pid)
    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(400, "消息不能为空")
    with _hypit_chat_locks_guard:
        lock = _hypit_chat_locks.setdefault(req.pid, threading.Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "该项目有对话正在进行，请等上一轮结束")

    def stream():
        try:
            yield from _hypit_run_turn(req.pid, proj, msg)
        finally:
            lock.release()

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.middleware("http")
async def session_auth(request, call_next):
    path = request.url.path
    # HYPIT 工坊（能驱动本机 agent 跑 shell）：仅回环直连，隧道/公网一律拒绝，
    # 且先于会话检查 —— 非本机连这个模式的存在都不暴露
    if path.startswith("/api/hypit") or path.startswith("/static/hypit"):
        if not _is_loopback_direct(request):
            if path.startswith("/api/"):
                return JSONResponse(status_code=403, content={"error": "HYPIT 工坊仅本机可用"})
            return RedirectResponse("/", status_code=303)
    # 公开路径：主登录、管理员登录
    if path.startswith("/login") or path.startswith("/admin/login"):
        return await call_next(request)
    # 管理员数据接口：必须管理员会话
    if (path.startswith("/api/admin")
            and not path.startswith("/api/admin/secret")
            and path not in ("/api/admin/perf", "/api/admin/free")):
        if not _check_admin_session(request.cookies.get("admin_session", "")):
            return JSONResponse(status_code=401, content={"error": "未登录"})
        return await call_next(request)
    # 管理员页面本身可访问（页面内部 JS 门控）
    if path == "/admin.html" or path.startswith("/admin/"):
        return await call_next(request)
    # 主会话
    if not _check_session(request.cookies.get("session", "")):
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": "未登录"})
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

if __name__ == "__main__":
    threading.Thread(target=_lane_health_loop, daemon=True).start()
    for _l in LANES:
        threading.Thread(target=_progress_ws_loop, args=(_l,), daemon=True).start()
        if _l["id"] != "local" and _l["enabled"]:
            threading.Thread(target=_sync_loop, args=(_l,), daemon=True).start()
    if cfg.is_customized():
        print(f"[config] {cfg.describe()}", flush=True)
    else:
        print(f"[config] 未找到 config.json，使用默认布局（{cfg.describe()}）；"
              f"可复制 config.example.json 为 config.json 覆盖", flush=True)
    uvicorn.run(app, host=cfg.host, port=cfg.port)
