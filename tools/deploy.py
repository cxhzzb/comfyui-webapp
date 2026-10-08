#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键部署：克隆 ComfyUI 自定义节点 + 下载模型权重。

用法（在项目根目录执行）：

    python tools/deploy.py --comfy-root D:\\ComfyUI            # 全部：节点 + 模型
    python tools/deploy.py --comfy-root D:\\ComfyUI --nodes    # 只装自定义节点
    python tools/deploy.py --comfy-root D:\\ComfyUI --models   # 只下模型
    python tools/deploy.py --comfy-root D:\\ComfyUI --dry-run  # 只看计划，不动磁盘
    python tools/deploy.py --check                             # 校验：还缺哪些文件

未指定 --comfy-root 时，会尝试读取项目根 config.json 的 comfyui_root。

下载特性：断点续传（.part 临时文件）、失败重试、镜像加速、已存在则跳过。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MANIFEST = BASE_DIR / "deploy.manifest.json"

# 国内网络可用的镜像（可用环境变量覆盖）
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
GITHUB_PROXY = os.environ.get("GITHUB_PROXY", "https://gh-proxy.com/").rstrip("/") + "/"
USE_PROXY = os.environ.get("DEPLOY_NO_PROXY", "") not in ("1", "true", "yes")

UA = {"User-Agent": "Mozilla/5.0 (comfyui-webapp-deploy)"}


def log(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # 某些 Windows 控制台编码旧
        print(msg.encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def git_url(repo):
    """可选走 GitHub 加速前缀。"""
    if USE_PROXY and repo.startswith("https://github.com/"):
        return GITHUB_PROXY + repo
    return repo


def hf_urls(url):
    """返回候选下载地址：镜像优先，原始地址兜底。"""
    if not url.startswith("https://huggingface.co/"):
        return [url]
    mirrored = HF_ENDPOINT + url[len("https://huggingface.co"):]
    return [mirrored] if mirrored == url else [mirrored, url]


def resolve_comfy_root(args) -> Path:
    if args.comfy_root:
        return Path(args.comfy_root).expanduser().resolve()
    cfg_file = BASE_DIR / "config.json"
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            if data.get("comfyui_root"):
                return Path(data["comfyui_root"]).expanduser().resolve()
        except Exception:
            pass
    log("!! 未指定 --comfy-root，且 config.json 里没有 comfyui_root。")
    log("   请用 --comfy-root <你的 ComfyUI 安装目录> 明确指定。")
    sys.exit(2)


def resolve_models_roots(args, comfy_root: Path) -> list[Path]:
    """模型根目录列表（可以有多个）。

    ComfyUI 常见两种布局：
      1) 全部在安装目录下                   -> <comfy_root>/models
      2) 部分/全部放在共享目录（extra_model_paths）-> <shared>/models

    两者可以并存（同一台机器上模型分散在两处很常见），所以这里返回列表：
    查找时会遍历所有根；下载时写入第一个根。
    """
    if args.models_root:
        return [Path(p).expanduser().resolve() for p in args.models_root]

    roots: list[Path] = []
    shared = os.environ.get("COMFY_SHARED")
    if not shared:
        cfg_file = BASE_DIR / "config.json"
        if cfg_file.exists():
            try:
                shared = json.loads(cfg_file.read_text(encoding="utf-8")).get("comfy_shared_root")
            except Exception:
                shared = None
    if shared:
        roots.append(Path(shared).expanduser().resolve() / "models")

    roots.append(comfy_root / "models")

    # 常见布局自动探测：ComfyUI Desktop 把共享模型目录放在安装目录上方的某一级
    # （例如 ...\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI 对应 ...\Comfy-Desktop\ComfyUI-Shared）
    ancestor = comfy_root
    for _ in range(4):
        ancestor = ancestor.parent
        if ancestor == ancestor.parent:
            break
        guess = ancestor / "ComfyUI-Shared" / "models"
        if guess.is_dir():
            roots.append(guess.resolve())
            break

    # 去重，保持顺序
    seen, uniq = set(), []
    for r in roots:
        if str(r).lower() not in seen:
            seen.add(str(r).lower())
            uniq.append(r)
    return uniq


def load_manifest():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return data.get("nodes", []), data.get("models", [])


# ---------------- 自定义节点 ----------------

def install_nodes(comfy_root: Path, nodes, dry_run=False):
    target_root = comfy_root / "custom_nodes"
    log("=" * 78)
    log(f"自定义节点 -> {target_root}")
    log("=" * 78)

    ok = skipped = failed = needs_manual = 0

    if not dry_run:
        target_root.mkdir(parents=True, exist_ok=True)

    for n in nodes:
        name, repo = n["dir"], (n.get("repo") or "").strip()
        dest = target_root / name

        if not repo:
            log(f"\n[需手动] {name}")
            log(f"         {n.get('note', '无公开来源')}")
            log(f"         需要的节点: {', '.join(n.get('provides', []))}")
            needs_manual += 1
            continue

        if dest.exists() and any(dest.iterdir()):
            log(f"\n[已存在] {name}")
            skipped += 1
            continue

        if dry_run:
            log(f"\n[待克隆] {name}  <- {git_url(repo)}")
            ok += 1
            continue

        log(f"\n[克隆中] {name}  <- {repo}")
        for attempt, url in enumerate([git_url(repo), repo], 1):
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", url, str(dest)],
                    check=True, capture_output=True, text=True, timeout=1800,
                )
                log(f"         完成")
                ok += 1
                break
            except subprocess.CalledProcessError as exc:
                err = (exc.stderr or "").strip().splitlines()
                log(f"         尝试 {attempt} 失败：{err[-1] if err else exc}")
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
            except Exception as exc:
                log(f"         尝试 {attempt} 异常：{exc}")
        else:
            log(f"         放弃（可稍后重跑，脚本会跳过已完成的）")
            failed += 1

        # 若该节点自带 requirements.txt，提示安装（不自动装，避免污染环境）
        req = dest / "requirements.txt"
        if req.exists():
            log(f"         注意：该节点带 requirements.txt，需在 ComfyUI 的 Python 环境执行：")
            log(f"               pip install -r \"{req}\"")

    log("")
    log(f"节点小结：新建 {ok}，已存在 {skipped}，失败 {failed}，需手动 {needs_manual}")
    return failed


# ---------------- 模型 ----------------

def download(urls, dest: Path, expect_gb: float | None, retries=4):
    """带断点续传与重试的下载。返回 True 表示成功。"""
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    for url in urls:
        for attempt in range(1, retries + 1):
            have = part.stat().st_size if part.exists() else 0
            headers = dict(UA)
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as r:
                    total = None
                    if r.headers.get("Content-Length"):
                        total = int(r.headers["Content-Length"]) + (have if r.status == 206 else 0)
                    if r.status == 200 and have:
                        have = 0  # 服务端不支持续传，从头写
                        part.unlink(missing_ok=True)
                    mode = "ab" if have else "wb"
                    done = have
                    t0 = time.time()
                    with open(part, mode) as f:
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                pct = done * 100 / total
                                sp = done / max(time.time() - t0, 0.001)
                                bar = "#" * int(pct / 3)
                                print(
                                    f"\r         [{bar:<33}] {pct:5.1f}%  "
                                    f"{human(done)}/{human(total)}  {human(sp)}/s",
                                    end="", flush=True,
                                )
                    print()
                if expect_gb:
                    got = part.stat().st_size / 2**30
                    if got < expect_gb * 0.9:
                        log(f"         警告：大小 {got:.2f}GB 明显小于预期 {expect_gb}GB，重试")
                        part.unlink(missing_ok=True)
                        continue
                part.replace(dest)
                return True
            except urllib.error.HTTPError as exc:
                print()
                if exc.code in (403, 404):
                    log(f"         {exc.code} {url}")
                    break  # 换下一个镜像
                log(f"         重试 {attempt}/{retries}（HTTP {exc.code}）")
                time.sleep(3 * attempt)
            except Exception as exc:
                print()
                log(f"         重试 {attempt}/{retries}（{type(exc).__name__}: {exc}）")
                time.sleep(3 * attempt)
    return False


def find_model(models_roots, m) -> Path | None:
    """在任意一个模型根下定位文件。

    先看清单里的预期路径；找不到就按文件名全树查找——ComfyUI 本身是递归扫描模型目录的，
    只要文件在某个根的树里就能被加载，不必强求某个子目录名。
    """
    rel = m["target"].split("models/", 1)[-1]
    name = Path(rel).name
    for root in models_roots:
        direct = root / rel
        if direct.exists() and direct.stat().st_size > 0:
            return direct
        if root.is_dir():
            for p in root.rglob(name):
                if p.is_file() and p.stat().st_size > 0:
                    return p
    return None


def install_models(models_roots, models, dry_run=False):
    target_root = models_roots[0]
    log("")
    log("=" * 78)
    log("模型权重")
    for r in models_roots:
        log(f"  查找目录：{r}")
    log(f"  下载写入：{target_root}")
    log("=" * 78)

    todo = [m for m in models if not m.get("status") == "manual"]
    manual = [m for m in models if m.get("status") == "manual"]

    total_gb = sum(m.get("size_gb", 0) for m in todo)
    have_gb = 0.0
    for m in todo:
        p = find_model(models_roots, m)
        if p:
            have_gb += p.stat().st_size / 2**30

    log(f"计划下载 {len(todo)} 个文件，合计约 {total_gb:.1f} GB"
        f"（已有 {have_gb:.1f} GB 会被跳过）")
    if USE_PROXY:
        log(f"镜像：HF={HF_ENDPOINT}  GitHub={GITHUB_PROXY}")
    log("")

    ok = skipped = failed = 0
    for i, m in enumerate(todo, 1):
        rel = m["target"].split("models/", 1)[-1]
        dest = target_root / rel
        size = m.get("size_gb", 0)
        log(f"[{i}/{len(todo)}] {m['file']}  ({size} GB)")

        existing = find_model(models_roots, m)
        if existing:
            log(f"        [跳过] 已存在：{existing}")
            skipped += 1
            continue

        log(f"        -> {rel}")
        if dry_run:
            log(f"        [待下载] {hf_urls(m['url'])[0]}")
            ok += 1
            continue

        if download(hf_urls(m["url"]), dest, size):
            log(f"        [完成]")
            ok += 1
        else:
            log(f"        [失败] 可重跑本脚本续传：{m['url']}")
            failed += 1
        log("")

    if manual:
        log("=" * 78)
        log(f"需要你自行准备的模型（{len(manual)} 个）")
        log("=" * 78)
        for m in manual:
            log(f"\n  {m['file']}  ({m.get('size_gb')} GB)")
            log(f"    目标路径：models/{m['target'].split('models/', 1)[-1]}")
            log(f"    用途：{m['used_by']}")
            log(f"    说明：{m.get('note', '')}")

    log("")
    log(f"模型小结：下载 {ok}，跳过 {skipped}，失败 {failed}，需手动 {len(manual)}")
    return failed


# ---------------- 校验 ----------------

def check(comfy_root: Path, models_roots):
    nodes, models = load_manifest()
    log("=" * 78)
    log("部署校验")
    log(f"  节点目录：{comfy_root / 'custom_nodes'}")
    for r in models_roots:
        log(f"  模型目录：{r}")
    log("=" * 78)

    log("\n自定义节点：")
    missing_nodes = []
    for n in nodes:
        dest = comfy_root / "custom_nodes" / n["dir"]
        exists = dest.is_dir() and any(dest.iterdir())
        log(f"  [{'有' if exists else '缺'}] {n['dir']:<26} {', '.join(n.get('provides', []))[:44]}")
        if not exists:
            missing_nodes.append(n["dir"])

    log("\n模型文件：")
    missing_models, manual_missing = [], []
    for m in models:
        p = find_model(models_roots, m)
        exists = p is not None
        tag = "有" if exists else ("缺(需手动)" if m.get("status") == "manual" else "缺")
        where = ""
        if exists:
            for r in models_roots:
                try:
                    where = f"  ({p.parent.relative_to(r)})"
                    break
                except ValueError:
                    continue
        log(f"  [{tag:<9}] {m['file']}{where}")
        if not exists:
            (manual_missing if m.get("status") == "manual" else missing_models).append(m["file"])

    log("")
    log("=" * 78)
    if not missing_nodes and not missing_models and not manual_missing:
        log("全部就绪，可以启动 server.py 了。")
    else:
        if missing_nodes:
            log(f"缺 {len(missing_nodes)} 个节点包 -> 跑 python tools/deploy.py --nodes")
        if missing_models:
            log(f"缺 {len(missing_models)} 个模型 -> 跑 python tools/deploy.py --models")
        if manual_missing:
            log(f"{len(manual_missing)} 个模型需自行准备（见 deploy.manifest.json 的 note 字段）")
    log("=" * 78)
    return 1 if (missing_nodes or missing_models) else 0


def main():
    ap = argparse.ArgumentParser(
        description="ComfyUI Webapp 一键部署（节点 + 模型）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--comfy-root", help="ComfyUI 安装目录（含 custom_nodes）")
    ap.add_argument("--models-root", action="append",
                    help="模型根目录，可重复指定多个。默认探测 <comfy-root>/models 与 "
                         "config.json 的 comfy_shared_root/models（两者可并存）")
    ap.add_argument("--nodes", action="store_true", help="只装自定义节点")
    ap.add_argument("--models", action="store_true", help="只下模型")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不实际改动")
    ap.add_argument("--check", action="store_true", help="只校验当前缺什么")
    args = ap.parse_args()

    if not MANIFEST.exists():
        log(f"找不到清单文件：{MANIFEST}")
        sys.exit(2)

    comfy_root = resolve_comfy_root(args)
    models_roots = resolve_models_roots(args, comfy_root)

    if args.check:
        sys.exit(check(comfy_root, models_roots))

    nodes, models = load_manifest()
    log(f"ComfyUI 根目录：{comfy_root}")
    for r in models_roots:
        log(f"模型根目录：  {r}")
    if not comfy_root.is_dir() and not args.dry_run:
        log("!! 该目录不存在。请确认 --comfy-root 指向 ComfyUI 安装目录。")
        sys.exit(2)
    if not comfy_root.is_dir():
        log("(注意：该目录当前不存在，以下是 --dry-run 的计划预览)")

    do_nodes = args.nodes or not (args.nodes or args.models)
    do_models = args.models or not (args.nodes or args.models)

    failed = 0
    if do_nodes:
        failed += install_nodes(comfy_root, nodes, args.dry_run)
    if do_models:
        failed += install_models(models_roots, models, args.dry_run)

    log("")
    if args.dry_run:
        log("这是 --dry-run，未做任何改动。去掉该参数即真正执行。")
        sys.exit(0)
    log("下一步：")
    log("  1) 对带 requirements.txt 的节点，在 ComfyUI 的 Python 环境里 pip install")
    log("  2) 重启 ComfyUI")
    log(f"  3) 校验：python tools/deploy.py --comfy-root \"{comfy_root}\" --check")
    log("  4) 启动 webapp：python server.py")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
