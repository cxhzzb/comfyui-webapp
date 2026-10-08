# -*- coding: utf-8 -*-
"""集中配置：所有本机相关的路径、端口、凭据外观都从这里读。

解析优先级（高 → 低）：

1. 环境变量（``COMFYUI_ROOT`` / ``COMFY_SHARED`` / ``WEBAPP_PORT`` ... ）
2. 项目根目录下的 ``config.json``（不入库，参见 ``config.example.json``）
3. 代码内的通用默认值（不包含任何个人信息，适用于 ComfyUI Desktop 默认安装）

这样开源分发的代码里没有个人路径，而本机只需在 ``config.json`` 里覆盖少量字段。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "config.json"
EXAMPLE_FILE = BASE_DIR / "config.example.json"

# ComfyUI Desktop 的常见默认安装位置，仅作为"找不到配置"时的兜底
_DEFAULT_COMFYUI_ROOT = Path(r"C:\ComfyUI")
_DEFAULT_COMFY_SHARED = None  # 未配置时回落到 COMFYUI_ROOT 同级

# 键 -> 环境变量名
_ENV_KEYS = {
    "comfyui_root": "COMFYUI_ROOT",
    "comfy_shared_root": "COMFY_SHARED",
    "karaoke_python": "WEBAPP_KARAOKE_PYTHON",
    "hypit_root": "WEBAPP_HYPIT_ROOT",
    "codex_cmd": "CODEX_CMD",
    "hypit_cmd": "HYPIT_CMD",
    "host": "WEBAPP_HOST",
    "port": "WEBAPP_PORT",
}

_DEFAULTS = {
    "comfyui_root": _DEFAULT_COMFYUI_ROOT,
    "comfy_shared_root": None,
    "karaoke_python": None,
    "hypit_root": Path.home() / "HYPIT",
    "video_projects_root": Path.home() / "Videos",
    "codex_cmd": None,
    "hypit_cmd": None,
    "host": "0.0.0.0",
    "port": 8800,
}

# 与个人环境绑定的额外目录名（共享根下）
MODELS_DIRNAME = "models"
INPUT_DIRNAME = "input"
OUTPUT_DIRNAME = "output"
MODELS_TRASH_DIRNAME = ".model_trash"
FILE_TRASH_DIRNAME = ".file_trash"

_raw: dict = {}
_loaded_from_file = False


def _load():
    """读取 config.json；文件不存在或损坏都不影响启动。"""
    global _loaded_from_file
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _loaded_from_file = True
            return data
    except Exception as exc:  # 配置坏了也必须能起来
        print(f"[config] 读取 {CONFIG_FILE.name} 失败，改用默认值：{exc}", flush=True)
    return {}


_raw = _load()


def _pick(key):
    """按优先级取值：环境变量 > config.json > 默认值。返回 (值, 来源)。"""
    env = _ENV_KEYS.get(key)
    if env:
        v = os.environ.get(env)
        if v not in (None, ""):
            return v, f"env:{env}"
    if key in _raw and _raw[key] not in (None, ""):
        return _raw[key], CONFIG_FILE.name
    return _DEFAULTS.get(key), "default"


def _as_path(value: Path | str) -> Path:
    return Path(value).expanduser().resolve()


class Config:
    """启动时解析一次的运行时配置。"""

    def __init__(self):
        self.sources: dict[str, str] = {}

        self.comfyui_root = _as_path(self._get("comfyui_root"))

        shared, src = _pick("comfy_shared_root")
        self.sources["comfy_shared_root"] = src
        # 未配置共享根时，假定与安装目录同级（ComfyUI Desktop 常见布局）
        self.comfy_shared_root = (
            _as_path(shared) if shared else (self.comfyui_root.parent / "ComfyUI-Shared")
        )

        # 模型搜索根：安装目录内的 models + 共享目录内的 models
        self.models_root = self.comfyui_root / MODELS_DIRNAME
        self.shared_models_root = self.comfy_shared_root / MODELS_DIRNAME

        self.input_root = self.comfy_shared_root / INPUT_DIRNAME
        self.output_root = self.comfy_shared_root / OUTPUT_DIRNAME
        self.workflow_dir = self.comfyui_root / "user" / "default" / "workflows"

        self.models_trash_dir = self.comfy_shared_root / MODELS_TRASH_DIRNAME
        self.file_trash_dir = self.comfy_shared_root / FILE_TRASH_DIRNAME

        karaoke, src = _pick("karaoke_python")
        self.sources["karaoke_python"] = src
        # 未配置时按当前解释器所在目录推断，缺文件时上层会优雅报错
        self.karaoke_python = _as_path(karaoke) if karaoke else None

        self.hypit_root = _as_path(self._get("hypit_root"))
        self.video_projects_root = _as_path(self._get("video_projects_root"))

        # 外部 CLI：优先 config.json / 环境变量，其次 PATH，最后按用户目录推断
        self.codex_cmd = self._resolve_cmd("codex_cmd", "codex", Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd")
        self.hypit_cmd = self._resolve_cmd("hypit_cmd", "hypit", Path.home() / ".local" / "bin" / "hypit.cmd")

        self.host = str(self._get("host"))
        self.port = int(self._get("port"))

    def _resolve_cmd(self, key, name, fallback: Path) -> str:
        """定位外部命令：配置 > PATH > 常见用户目录。找不到也返回候选路径，由调用方报错。"""
        value, src = _pick(key)
        self.sources[key] = src
        if value:
            return str(value)
        found = shutil.which(name)
        if found:
            self.sources[key] = "PATH"
            return found
        self.sources[key] = "fallback"
        return str(fallback)

    def _get(self, key):
        value, src = _pick(key)
        self.sources[key] = src
        return value

    def is_customized(self) -> bool:
        """是否至少有一项来自 config.json / 环境变量（否则是纯默认布局）。"""
        return _loaded_from_file or any(
            s.startswith("env:") for s in self.sources.values()
        )

    def describe(self) -> str:
        return (
            f"ComfyUI={self.comfyui_root} | 共享={self.comfy_shared_root} | "
            f"监听={self.host}:{self.port}"
        )


cfg = Config()

__all__ = [
    "BASE_DIR",
    "CONFIG_FILE",
    "EXAMPLE_FILE",
    "Config",
    "cfg",
]
