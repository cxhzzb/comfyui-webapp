# -*- coding: utf-8 -*-
"""卡拉OK歌词对齐：faster-whisper 逐字时间戳 × 已知歌词 → 每行真实起止时间。

用法：python karaoke_align.py --audio <音频路径>
歌词从 stdin 读（UTF-8）。结果 JSON 写 stdout：
{"ok": true, "duration": 秒, "lines": [{"start": 秒, "end": 秒}, ...]}
lines 顺序 = 歌词中非标签行的顺序。失败时 {"ok": false, "error": "..."}。
"""
import json
import os
import re
import sys

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # 国内镜像，首次下载模型用

MODEL_SIZE = os.environ.get("KARAOKE_MODEL", "small")

# 歌词从 stdin 读：Windows 默认 GBK，必须显式指定 UTF-8，否则中文全乱码对不上
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")


def norm(s):
    """归一化：去标点空白，只留文字（中日韩/字母/数字），小写。"""
    return re.sub(r"[^\w一-鿿]+", "", s, flags=re.UNICODE).lower()


def main():
    audio = None
    for i, a in enumerate(sys.argv):
        if a == "--audio" and i + 1 < len(sys.argv):
            audio = sys.argv[i + 1]
    if not audio or not os.path.isfile(audio):
        print(json.dumps({"ok": False, "error": f"音频不存在: {audio}"}))
        return
    lyrics = sys.stdin.read()
    lines = [ln.strip() for ln in lyrics.splitlines()
             if ln.strip() and not re.fullmatch(r"\[[^\]]*\]", ln.strip())]
    if not lines:
        print(json.dumps({"ok": False, "error": "歌词为空"}))
        return

    from faster_whisper import WhisperModel
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=4)
    cjk = sum(1 for c in lyrics if "一" <= c <= "鿿")
    lang = "zh" if cjk >= 4 else "en"
    # vad_filter 会把带伴奏的唱段误判为非语音，导致整首被过滤，这里必须关闭
    segments, info = model.transcribe(audio, language=lang, word_timestamps=True, vad_filter=False)
    duration = float(info.duration or 0.0)

    # 铺平成逐字时间流：[(char, start, end)]
    stream = []
    for seg in segments:
        for w in (seg.words or []):
            text = norm(w.word)
            if not text:
                continue
            span = max(0.01, (w.end - w.start) / len(text))
            for k, ch in enumerate(text):
                stream.append((ch, w.start + k * span, w.start + (k + 1) * span))
    if not stream:
        print(json.dumps({"ok": False, "error": "识别不到人声内容"}))
        return

    # 全部歌词行拼成参考序列，与识别流做字符级对齐
    import difflib
    ref = "".join(norm(ln) for ln in lines)
    hyp = "".join(c for c, _, _ in stream)
    sm = difflib.SequenceMatcher(a=ref, b=hyp, autojunk=False)
    ref_time = {}  # ref 下标 -> (start, end)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("equal", "replace") and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                ref_time[i1 + k] = (stream[j1 + k][1], stream[j1 + k][2])

    # 每行取首末已匹配字符的时间；没匹配上的行用相邻行插值
    out = []
    pos = 0
    for ln in lines:
        n = len(norm(ln))
        times = [ref_time.get(pos + k) for k in range(n)]
        times = [t for t in times if t]
        out.append({"start": round(times[0][0], 2), "end": round(times[-1][1], 2)} if times else None)
        pos += n
    # 插值：用前后已知行均分空隙
    for i, t in enumerate(out):
        if t is not None:
            continue
        prev_end = next((out[k]["end"] for k in range(i - 1, -1, -1) if out[k]), 0.0)
        nxt = next((out[k]["start"] for k in range(i + 1, len(out)) if out[k]), None)
        if nxt is None:
            nxt = prev_end + 3.0
        mid = (prev_end + nxt) / 2
        out[i] = {"start": round(prev_end, 2), "end": round(nxt, 2)} if prev_end < nxt else {"start": round(mid, 2), "end": round(mid + 0.5, 2)}

    # 插值可能超出实际时长（歌词被模型截短等情况），钳到 [0, duration]
    dur = duration or 1e9
    for t in out:
        t["start"] = max(0.0, min(t["start"], dur))
        t["end"] = max(t["start"], min(t["end"], dur))

    matched = len(ref_time)
    print(json.dumps({
        "ok": True,
        "duration": round(duration, 2),
        "match_ratio": round(matched / max(1, len(ref)), 3),
        "lines": out,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)[:300]}))
