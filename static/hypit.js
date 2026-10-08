/* HYPIT 工坊：Codex 驱动的本地出片模式（仅本机） */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const projList = $("proj-list"), chatLog = $("chat-log"), chatText = $("chat-text"),
    btnSend = $("btn-send"), fileList = $("file-list"), preview = $("preview"),
    statusEl = $("hp-status"), usageEl = $("chat-usage"), chatProj = $("chat-proj");

  let current = null;      // 当前项目 { pid, name, ... }
  let busy = false;        // 一轮对话进行中
  let roots = [];          // /api/hypit/projects 不带 root 列表，写死与后端一致
  roots = ["HYPIT 本地产线", "我的视频作品"];

  // ---------- 工具 ----------
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function fmtSize(n) {
    if (n > 1048576) return (n / 1048576).toFixed(1) + "M";
    if (n > 1024) return (n / 1024).toFixed(0) + "K";
    return n + "B";
  }
  function addMsg(cls, text) {
    const d = document.createElement("div");
    d.className = "hp-msg " + cls;
    d.textContent = text;
    chatLog.appendChild(d);
    chatLog.scrollTop = chatLog.scrollHeight;
    return d;
  }
  function setBusy(b, label) {
    busy = b;
    btnSend.disabled = b || !current;
    chatText.disabled = b || !current;
    statusEl.textContent = label || "";
  }

  // ---------- 项目列表 ----------
  async function loadProjects(selectPid) {
    try {
      const r = await fetch("/api/hypit/projects");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      projList.innerHTML = "";
      if (!data.projects.length) {
        projList.innerHTML = '<p style="padding:8px;font-size:12px;color:var(--text-dim)">未发现项目，可在下方新建</p>';
      }
      for (const p of data.projects) {
        const d = document.createElement("div");
        d.className = "hp-proj" + (current && current.pid === p.pid ? " active" : "");
        const pend = p.pending ? `<span class="pend">◔ ${p.pending} 待办</span>` : (p.done ? "✓ 已完成" : "");
        const fin = p.finals.length ? ` · 🎬 ${p.finals[p.finals.length - 1]}` : "";
        let dl = "";
        if (p.download) {
          if (p.download.status === "running") dl = ' · <span class="pend">⬇ 参考片下载中</span>';
          else if (p.download.status === "error") dl = ` · <span style="color:var(--danger)" title="${esc(p.download.error || "")}">✗ 参考片下载失败</span> <button type="button" class="dl-retry">重试</button>`;
        }
        d.innerHTML = `<div class="p-name">${esc(p.name)}</div>
          <div class="p-meta">${esc(p.root)}${pend ? " · " + pend : ""}${esc(fin)}${dl}</div>`;
        d.onclick = () => selectProject(p);
        const retryBtn = d.querySelector(".dl-retry");
        if (retryBtn) retryBtn.onclick = async (e) => {
          e.stopPropagation();
          await fetch("/api/hypit/refetch", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pid: p.pid, ref_link: (p.download && p.download.url) || "" }),
          });
          loadProjects();
        };
        projList.appendChild(d);
      }
      if (selectPid) {
        const hit = data.projects.find((p) => p.pid === selectPid);
        if (hit) selectProject(hit);
      }
      // 有进行中的参考片下载：5 秒后自动刷新状态
      if (data.projects.some((p) => p.download && p.download.status === "running")) {
        setTimeout(() => loadProjects(), 5000);
      }
    } catch (e) {
      projList.innerHTML = `<p style="padding:8px;font-size:12px;color:var(--danger)">加载失败：${esc(e.message)}</p>`;
    }
  }

  function selectProject(p) {
    current = p;
    document.querySelectorAll(".hp-proj").forEach((el) => el.classList.remove("active"));
    [...projList.children].forEach((el) => {
      if (el.querySelector(".p-name") && el.querySelector(".p-name").textContent === p.name) el.classList.add("active");
    });
    chatProj.textContent = " · " + p.name;
    usageEl.textContent = "";
    chatLog.innerHTML = "";
    addMsg("sys", `项目：${p.path}\n${p.has_session ? "已有导演会话，下一条消息自动接续。" : "首次对话会新建导演会话。"}对话记录不在此页保存，项目文件即记忆。`);
    setBusy(false);
    loadFiles();
    chatText.focus();
  }

  // ---------- 新建项目向导 ----------
  const newModal = $("new-modal"), newStatus = $("new-status");
  $("btn-new-open").onclick = () => {
    newStatus.textContent = "";
    newModal.classList.remove("hidden");
    $("new-name").focus();
  };
  $("btn-new-cancel").onclick = () => newModal.classList.add("hidden");
  newModal.addEventListener("click", (e) => {
    if (e.target === newModal) newModal.classList.add("hidden");
  });

  $("btn-new-ok").onclick = async () => {
    const name = $("new-name").value.trim();
    const link = $("new-link").value.trim();
    const req = $("new-req").value.trim();
    const file = $("new-file").files[0];
    if (!name) { newStatus.textContent = "先填项目名"; $("new-name").focus(); return; }
    const btn = $("btn-new-ok");
    btn.disabled = true;
    try {
      // 1 建项目文件夹（需求 + 参考链接写入 BRIEF，链接后台下载）
      newStatus.textContent = "创建项目文件夹…";
      const r = await fetch("/api/hypit/new", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          root_idx: $("new-root").selectedIndex,
          name, requirement: req, ref_link: link,
        }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
      // 2 上传参考视频
      let uploaded = "";
      if (file) {
        newStatus.textContent = `上传参考视频（${fmtSize(file.size)}）…`;
        const fd = new FormData();
        fd.append("pid", data.pid);
        fd.append("file", file);
        const ru = await fetch("/api/hypit/upload", { method: "POST", body: fd });
        const du = await ru.json();
        if (!ru.ok) throw new Error(du.detail || ("上传失败 HTTP " + ru.status));
        uploaded = du.path;
      }
      // 3 选中项目，预填第一条导演指令（用户确认后发送）
      newModal.classList.add("hidden");
      $("new-name").value = ""; $("new-link").value = ""; $("new-req").value = ""; $("new-file").value = "";
      await loadProjects(data.pid);
      const refs = [uploaded ? `references 里的 ${uploaded}` : "", link ? `参考链接 ${link}（后台下载中，落点 references/source.mp4）` : ""]
        .filter(Boolean).join("；");
      chatText.value =
        (req ? `需求：${req}\n` : "") +
        (refs ? `参考：${refs}\n` : "") +
        "请先读 BRIEF.md 和参考素材，告诉我你的制作思路（Treatment），我们再开始。";
      chatText.focus();
    } catch (e) {
      newStatus.textContent = "失败：" + e.message;
    } finally {
      btn.disabled = false;
    }
  };

  // ---------- 文件列表 / 预览 ----------
  let filesCache = [];
  async function loadFiles() {
    if (!current) return;
    try {
      const r = await fetch("/api/hypit/files?pid=" + encodeURIComponent(current.pid));
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      filesCache = data.files;
      fileList.innerHTML = "";
      for (const f of data.files) {
        const d = document.createElement("div");
        d.className = "hp-file";
        const icon = { md: "📄", text: "📄", image: "🖼", video: "🎬", audio: "🎵", other: "📦" }[f.kind] || "📦";
        d.innerHTML = `<span>${icon}</span><span>${esc(f.path)}</span><span class="f-size">${fmtSize(f.size)}</span>`;
        if (f.kind !== "other") d.onclick = () => previewFile(f, d);
        fileList.appendChild(d);
      }
    } catch (e) {
      fileList.innerHTML = `<p style="padding:8px;font-size:12px;color:var(--danger)">加载失败：${esc(e.message)}</p>`;
    }
  }

  async function previewFile(f, el) {
    document.querySelectorAll(".hp-file").forEach((x) => x.classList.remove("active"));
    if (el) el.classList.add("active");
    const url = "/api/hypit/file?pid=" + encodeURIComponent(current.pid) + "&path=" + encodeURIComponent(f.path);
    preview.innerHTML = `<span class="pv-name">${esc(f.path)}</span>`;
    if (f.kind === "image") {
      const img = document.createElement("img");
      img.src = url;
      preview.appendChild(img);
    } else if (f.kind === "video") {
      const v = document.createElement("video");
      v.src = url; v.controls = true;
      preview.appendChild(v);
    } else if (f.kind === "audio") {
      const a = document.createElement("audio");
      a.src = url; a.controls = true;
      preview.appendChild(a);
    } else {
      try {
        const r = await fetch(url);
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
        const pre = document.createElement("pre");
        pre.textContent = data.text;
        preview.appendChild(pre);
      } catch (e) {
        preview.innerHTML += `<span style="color:var(--danger);font-size:12px">${esc(e.message)}</span>`;
      }
    }
  }
  $("btn-files-refresh").onclick = loadFiles;
  $("btn-refresh").onclick = () => loadProjects();

  // ---------- 对话（SSE） ----------
  let filesDirty = false;
  function addCmdBlock(command) {
    const det = document.createElement("details");
    det.className = "hp-cmd";
    det.innerHTML = `<summary><span class="exit-ok">▶</span><span class="cmd-str">${esc(command)}</span></summary><pre>运行中…</pre>`;
    chatLog.appendChild(det);
    chatLog.scrollTop = chatLog.scrollHeight;
    return det;
  }
  function finishCmdBlock(det, output, exitCode) {
    const mark = det.querySelector("summary span");
    mark.className = exitCode === 0 ? "exit-ok" : "exit-bad";
    mark.textContent = exitCode === 0 ? "✓" : "✗ " + exitCode;
    det.querySelector("pre").textContent = output || "（无输出）";
    chatLog.scrollTop = chatLog.scrollHeight;
  }
  function addFileChips(changes) {
    filesDirty = true;
    for (const c of changes) {
      const chip = document.createElement("div");
      chip.className = "hp-filechip";
      const kind = { add: "＋", update: "✎", delete: "－" }[c.kind] || "✎";
      const rel = String(c.path || "");
      chip.textContent = `${kind} ${rel}`;
      const hit = filesCache.find((f) => rel.endsWith(f.path) || f.path.endsWith(rel));
      chip.onclick = () => {
        const p = hit ? hit.path : rel.replace(/^[A-Za-z]:[\\/]/, "").replace(/\\/g, "/");
        const f2 = filesCache.find((f) => f.path === p);
        if (f2) previewFile(f2, null);
      };
      chatLog.appendChild(chip);
    }
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  async function send() {
    const msg = chatText.value.trim();
    if (!msg || busy || !current) return;
    chatText.value = "";
    addMsg("user", msg);
    setBusy(true, "导演工作中…");
    const cmdBlocks = {};   // item id -> <details>
    try {
      const r = await fetch("/api/hypit/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pid: current.pid, message: msg }),
      });
      if (!r.ok || !r.body) {
        let detail = "HTTP " + r.status;
        try { detail = (await r.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(6)); } catch (_) { continue; }
          if (ev.ev === "msg") {
            addMsg("agent", ev.text);
          } else if (ev.ev === "cmd_start") {
            cmdBlocks[ev.id] = addCmdBlock(ev.command);
          } else if (ev.ev === "cmd") {
            if (cmdBlocks[ev.id]) finishCmdBlock(cmdBlocks[ev.id], ev.output, ev.exit_code);
            else { const d2 = addCmdBlock(ev.command); finishCmdBlock(d2, ev.output, ev.exit_code); }
          } else if (ev.ev === "file") {
            addFileChips(ev.changes || []);
          } else if (ev.ev === "usage") {
            const u = ev.usage || {};
            usageEl.textContent = `tokens ${u.input_tokens || 0}→${u.output_tokens || 0}`;
          } else if (ev.ev === "error") {
            addMsg("sys err", "⚠ " + ev.text);
          } else if (ev.ev === "done") {
            if (ev.code !== 0) addMsg("sys err", "会话异常结束（退出码 " + ev.code + "）");
          }
        }
      }
    } catch (e) {
      addMsg("sys err", "发送失败：" + e.message);
    }
    setBusy(false);
    if (filesDirty) { filesDirty = false; loadFiles(); }
    chatText.focus();
  }

  btnSend.onclick = send;
  chatText.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });

  // ---------- 初始化 ----------
  const rootSel = $("new-root");
  roots.forEach((label, i) => {
    const o = document.createElement("option");
    o.value = i; o.textContent = label;
    rootSel.appendChild(o);
  });
  loadProjects();
})();
