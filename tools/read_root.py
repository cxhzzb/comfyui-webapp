#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 config.json 里的 comfyui_root，供 setup.bat 复用。

- 找到则打印路径（单行，无多余字符）
- 找不到则打印空行并以退出码 1 结束

把这段逻辑放在 .py 而不是写在 .bat 里，是为了避开 cmd 对括号/引号/管道的解析陷阱。
"""
import json
import sys
from pathlib import Path

cfg = Path(__file__).resolve().parent.parent / "config.json"
if not cfg.exists():
    sys.exit(1)
try:
    data = json.loads(cfg.read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)
root = (data or {}).get("comfyui_root") or ""
if not root:
    sys.exit(1)
print(root)
