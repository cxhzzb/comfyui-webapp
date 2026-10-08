/* 想把我唱你听 —— 音乐模式页（MiniMax Music 3）
 * 布局/交互沿用视频模式页约定：三栏、状态持久化、进度轮询、催更弹幕、生成历史。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const STATE_KEY = 'webapp_state_music';

  let modeCfg = null;
  let modesList = [];
  let genTimer = null;
  let lyricsTimer = null;
  let currentPid = null;

  async function apiJson(url, opts) {
    const r = await fetch(url, opts);
    if (r.status === 401) { location.href = '/login'; throw new Error('未登录'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.error || ('HTTP ' + r.status));
    return data;
  }

  // ---------- 状态持久化 ----------
  function loadState() {
    try { return JSON.parse(localStorage.getItem(STATE_KEY)) || {}; } catch (e) { return {}; }
  }
  function saveState(patch) {
    const s = Object.assign(loadState(), patch);
    try { localStorage.setItem(STATE_KEY, JSON.stringify(s)); } catch (e) {}
  }
  function clearSavedPromptId() {
    const s = loadState(); delete s.prompt_id; saveState(s);
  }

  // ---------- 种子模式：每次随机 / 随机抽取 / 固定上次 ----------
  let lastSeed = null; // 最近一次出歌实际使用的种子（服务端返回）
  function drawSeed() {
    $('seed').value = Math.floor(Math.random() * 2 ** 48);
    saveState({ seed: parseInt($('seed').value, 10) });
  }
  function applySeedMode() {
    const m = $('seed-mode').value;
    const inp = $('seed');
    const drawBtn = $('btn-seed-draw');
    if (m === 'random') {
      inp.value = -1;
      inp.disabled = true;
      drawBtn.classList.add('hidden');
    } else if (m === 'draw') {
      inp.disabled = false;
      drawBtn.classList.remove('hidden');
      if (isNaN(parseInt(inp.value, 10)) || parseInt(inp.value, 10) < 0) { drawSeed(); return; }
    } else { // last：固定为上次实际使用的种子；还没有过生成则先随机一次
      inp.disabled = true;
      drawBtn.classList.add('hidden');
      inp.value = lastSeed != null ? lastSeed : -1;
    }
    saveState(collectState());
  }

  function setStatus(el, html, kind) {
    el.classList.remove('hidden');
    el.className = 'status-line' + (kind ? ' ' + kind : '');
    el.innerHTML = html;
  }

  function showEmpty(show) { $('preview-empty').classList.toggle('hidden', !show); }

  function setProgress(p, elapsed) {
    const wrap = $('progress-wrap');
    if (!p && elapsed == null) { wrap.classList.add('hidden'); return; }
    wrap.classList.remove('hidden');
    const fill = $('progress-fill');
    const text = $('progress-text');
    if (p && p.max > 0) {
      const pct = Math.min(100, (p.value / p.max) * 100);
      fill.style.width = pct.toFixed(1) + '%';
      text.textContent = pct.toFixed(1) + '%（' + p.value + ' / ' + p.max + ' 步）' +
        (elapsed != null ? ' · 已用 ' + elapsed + ' 秒' : '');
    } else {
      fill.style.width = '100%';
      text.textContent = '准备中（模型加载/文本编码）…' + (elapsed != null ? ' · 已用 ' + elapsed + ' 秒' : '');
    }
  }

  function showElapsed(sec) {
    const el = $('elapsed-line');
    if (sec == null) { el.classList.add('hidden'); return; }
    el.classList.remove('hidden');
    el.textContent = '总用时 ' + Number(sec).toFixed(1) + ' 秒（仅执行时间，不含排队）';
  }

  // ---------- 催更弹幕 ----------
  let danmakuLines = null;
  let danmakuTimer = null;
  async function loadDanmakuLines() {
    if (danmakuLines) return;
    try {
      const d = await apiJson('/static/fun_lines.json');
      danmakuLines = d;
    } catch (e) { danmakuLines = { urge: [], reply: [] }; }
  }
  function spawnDanmaku() {
    if (!danmakuLines) return;
    const urge = danmakuLines.urge || [];
    const reply = danmakuLines.reply || [];
    if (!urge.length && !reply.length) return;
    const box = $('danmaku');
    const isUrge = Math.random() < 0.5;
    const pool = isUrge ? urge : reply;
    const text = pool[Math.floor(Math.random() * pool.length)];
    if (!text) return;
    const item = document.createElement('div');
    item.className = 'danmaku-item' + (isUrge ? '' : ' reply');
    item.textContent = text;
    const track = Math.floor(Math.random() * 3);
    item.style.top = (track * 34 + 8) + 'px';
    box.appendChild(item);
    setTimeout(() => item.remove(), 14000);
  }
  async function startDanmaku() {
    await loadDanmakuLines();
    stopDanmaku();
    spawnDanmaku();
    danmakuTimer = setInterval(spawnDanmaku, 3500);
  }
  function stopDanmaku() {
    clearInterval(danmakuTimer);
    danmakuTimer = null;
    $('danmaku').innerHTML = '';
  }

  // ---------- 顶部模式切换 ----------
  function renderModeSwitch() {
    const sw = $('mode-switch');
    if (!sw) return;
    for (const m of modesList) {
      const b = document.createElement('button');
      b.className = 'ms-btn' + (m.id === 'music' ? ' active' : '');
      b.textContent = m.name;
      if (m.id === 'music') {
        b.disabled = true;
      } else {
        b.addEventListener('click', () => {
          location.href = m.id === 'mv'
            ? '/static/mv.html'
            : '/static/mode.html?m=' + encodeURIComponent(m.id);
        });
      }
      sw.appendChild(b);
    }
  }

  // ---------- 硬件状态条 ----------
  async function pollPerf() {
    try {
      const d = await apiJson('/api/admin/perf');
      const mem = d.mem && d.mem.percent;
      const gpu = d.gpu && d.gpu.util;
      const vram = d.gpu && d.gpu.vram_total_gb ? (d.gpu.vram_used_gb / d.gpu.vram_total_gb) * 100 : null;
      const set = (id, v) => {
        const el = $(id);
        el.textContent = v == null ? '--' : Math.round(v) + '%';
        el.parentElement.classList.toggle('warn', v != null && v > 70);
        el.parentElement.classList.toggle('crit', v != null && v > 90);
      };
      set('ps-cpu', d.cpu); set('ps-mem', mem); set('ps-gpu', gpu); set('ps-vram', vram);
    } catch (e) {}
  }

  // ---------- 表单恢复/收集 ----------
  function applyState(s) {
    if (s.style) $('style').value = s.style;
    if (s.gender) $('gender').value = s.gender;
    if (s.theme) $('theme').value = s.theme;
    if (s.lyrics) $('lyrics').value = s.lyrics;
    if (s.duration != null) { $('duration').value = s.duration; }
    if (s.seed != null) $('seed').value = s.seed;
    if (s.last_seed != null) lastSeed = s.last_seed;
    if (s.seed_mode && [...$('seed-mode').options].some((o) => o.value === s.seed_mode)) {
      $('seed-mode').value = s.seed_mode;
    }
    if (s.steps != null) $('adv-steps').value = s.steps;
    const lo = s.lyrics_opts || {};
    if (lo.lang) $('opt-lang').value = lo.lang;
    if (lo.length) $('opt-length').value = lo.length;
    if (lo.chorus) $('opt-chorus').value = lo.chorus;
    if (lo.keywords) $('opt-keywords').value = lo.keywords;
    syncRangeLabels();
  }
  function collectState() {
    return {
      style: $('style').value,
      gender: $('gender').value,
      theme: $('theme').value,
      lyrics: $('lyrics').value,
      duration: parseFloat($('duration').value),
      seed: parseInt($('seed').value, 10),
      seed_mode: $('seed-mode').value,
      last_seed: lastSeed,
      steps: parseInt($('adv-steps').value, 10),
      lyrics_opts: {
        lang: $('opt-lang').value,
        length: $('opt-length').value,
        chorus: $('opt-chorus').value,
        keywords: $('opt-keywords').value,
      },
    };
  }
  function syncRangeLabels() {
    $('duration-val').textContent = $('duration').value + ' 秒';
    $('adv-steps-val').textContent = $('adv-steps').value;
  }

  // ---------- AI 写词 ----------
  async function startLyrics() {
    const st = $('lyrics-status');
    const theme = $('theme').value.trim();
    if (!theme) { setStatus(st, '先写一句歌曲主题', 'err'); return; }
    $('btn-lyrics').disabled = true;
    setStatus(st, '<span class="spinner"></span>AI 写词中（首次需加载模型）…', 'busy');
    try {
      const { task_id } = await apiJson('/api/music/lyrics', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          theme,
          style: $('style').value,
          language: $('opt-lang').value,
          length: $('opt-length').value,
          chorus: $('opt-chorus').value,
          keywords: $('opt-keywords').value.trim(),
          seed: -1,
        }),
      });
      clearInterval(lyricsTimer);
      lyricsTimer = setInterval(() => pollLyrics(task_id), 2000);
    } catch (e) {
      $('btn-lyrics').disabled = false;
      setStatus(st, '写词失败：' + e.message, 'err');
    }
  }
  async function pollLyrics(taskId) {
    const st = $('lyrics-status');
    try {
      const res = await apiJson('/api/music/lyrics/' + taskId);
      if (res.state === 'running') return;
      clearInterval(lyricsTimer);
      $('btn-lyrics').disabled = false;
      if (res.state === 'done' && res.lyrics) {
        $('lyrics').value = res.lyrics.trim();
        saveState({ lyrics: $('lyrics').value });
        setStatus(st, '✅ 歌词已填入，可直接改', 'ok');
      } else {
        setStatus(st, '❌ 写词失败：' + (res.error || '未知错误'), 'err');
      }
    } catch (e) {
      clearInterval(lyricsTimer);
      $('btn-lyrics').disabled = false;
      setStatus(st, '写词查询失败：' + e.message, 'err');
    }
  }

  // ---------- 生成 ----------
  async function startGenerate() {
    const st = $('gen-status');
    const lyrics = $('lyrics').value.trim();
    if (!lyrics) { setStatus(st, '请先写词，或点「AI 写词」', 'err'); return; }
    // 种子：每次随机=-1；随机抽取=当前输入框值；固定上次=上次实际使用的种子（无则先随机）
    const seedMode = $('seed-mode').value;
    let seedVal = -1;
    if (seedMode === 'draw') {
      seedVal = parseInt($('seed').value, 10);
      if (isNaN(seedVal) || seedVal < 0) { drawSeed(); seedVal = parseInt($('seed').value, 10); }
    } else if (seedMode === 'last' && lastSeed != null) {
      seedVal = lastSeed;
    }
    const body = Object.assign({
      mode: 'music',
      duration: parseFloat($('duration').value),
      steps: parseInt($('adv-steps').value, 10),
    }, collectState());
    body.seed = seedVal; // collectState 里也有 seed，以种子模式计算结果为准

    const btn = $('btn-generate');
    btn.disabled = true;
    stopKaraoke();
    $('result').innerHTML = '';
    $('result').classList.remove('has-lyrics');
    showElapsed(null);
    showEmpty(false);
    setProgress(null);
    stopDanmaku();
    setStatus(st, '<span class="spinner"></span>提交中…', 'busy');
    try {
      const genRes = await apiJson('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const { prompt_id } = genRes;
      // 记录本次实际种子（服务端 -1 时已替换成真随机值），供「固定上次」使用
      if (typeof genRes.seed === 'number') {
        lastSeed = genRes.seed;
        if (seedMode === 'last') $('seed').value = lastSeed;
      }
      saveState({ prompt_id, last_seed: lastSeed });
      startPolling(prompt_id);
    } catch (e) {
      btn.disabled = false;
      setStatus(st, '提交失败：' + e.message, 'err');
    }
  }

  function startPolling(pid) {
    const btn = $('btn-generate');
    const st = $('gen-status');
    currentPid = pid;
    btn.disabled = true;
    startDanmaku();
    $('btn-cancel').classList.remove('hidden');
    clearInterval(genTimer);
    genTimer = setInterval(() => pollStatus(pid, btn, st), 2000);
    pollStatus(pid, btn, st);
  }

  async function pollStatus(pid, btn, st) {
    try {
      const res = await apiJson('/api/status/' + pid);
      if (res.state === 'queued') {
        setStatus(st, '<span class="spinner"></span>排队中（第 ' + (res.queue_position || '?') + ' 位）…', 'busy');
      } else if (res.state === 'running') {
        const elapsed = res.elapsed_running != null ? Math.round(res.elapsed_running) : null;
        setStatus(st, '<span class="spinner"></span>生成中' + (elapsed != null ? '（已用 ' + elapsed + ' 秒）' : '') + '…', 'busy');
        setProgress(res.progress || null, elapsed);
      } else if (res.state === 'done') {
        clearInterval(genTimer);
        btn.disabled = false;
        currentPid = null;
        $('btn-cancel').classList.add('hidden');
        stopDanmaku();
        setProgress(null);
        setStatus(st, '✅ 完成', 'ok');
        showElapsed(res.elapsed_seconds);
        addHistory(pid, res);
        showResults(res.outputs);
      } else {
        clearInterval(genTimer);
        btn.disabled = false;
        currentPid = null;
        $('btn-cancel').classList.add('hidden');
        stopDanmaku();
        setProgress(null);
        setStatus(st, '❌ 失败：' + (res.error || '未知错误'), 'err');
      }
    } catch (e) {
      clearInterval(genTimer);
      btn.disabled = false;
      currentPid = null;
      $('btn-cancel').classList.add('hidden');
      stopDanmaku();
      setProgress(null);
      setStatus(st, '状态查询失败：' + e.message, 'err');
    }
  }

  async function cancelGen() {
    const pid = currentPid;
    clearInterval(genTimer);
    currentPid = null;
    $('btn-generate').disabled = false;
    $('btn-cancel').classList.add('hidden');
    stopDanmaku();
    setProgress(null);
    clearSavedPromptId();
    setStatus($('gen-status'), '已取消', 'err');
    if (pid) {
      try { await apiJson('/api/cancel?pid=' + encodeURIComponent(pid), { method: 'POST' }); } catch (e) {}
    }
  }

  // ---------- 结果展示 ----------
  function fmtSize(n) {
    if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
    return n + ' B';
  }
  function escapeHtml(t) {
    const d = document.createElement('div');
    d.textContent = t;
    return d.innerHTML;
  }

  // ---------- 卡拉OK歌词墙 ----------
  let karaoke = null; // {audio, lines:[{el, chars:[span...]}], raf, per, lastIdx, lastCount}

  function stopKaraoke() {
    if (karaoke && karaoke.raf) cancelAnimationFrame(karaoke.raf);
    karaoke = null;
  }

  // 把歌词渲染成歌词墙：结构标签做成小节标记，歌词行逐字包 span（供逐字变色）
  function buildLyricsStage(lyricsText) {
    const stage = document.createElement('div');
    stage.className = 'lyrics-stage';
    const lines = [];
    const raw = (lyricsText || '').split('\n');
    for (const ln of raw) {
      const t = ln.trim();
      if (!t) continue;
      if (/^\[[^\]]*\]$/.test(t)) {
        const tag = document.createElement('div');
        tag.className = 'k-tag';
        tag.textContent = t.replace(/[\[\]]/g, '');
        stage.appendChild(tag);
        continue;
      }
      const div = document.createElement('div');
      div.className = 'k-line';
      const chars = [];
      for (const ch of Array.from(t)) {
        const sp = document.createElement('span');
        sp.textContent = ch;
        div.appendChild(sp);
        chars.push(sp);
      }
      stage.appendChild(div);
      lines.push({ el: div, chars });
    }
    return { stage, lines };
  }

  function bindKaraoke(audio, lines, fallbackDuration, box) {
    stopKaraoke();
    karaoke = { audio, lines, raf: null, lastIdx: -2, lastCount: -1, per: 0, timings: null };
    const setPer = () => {
      const dur = (audio.duration && isFinite(audio.duration)) ? audio.duration : (fallbackDuration || 0);
      karaoke.per = lines.length ? dur / lines.length : 0;
    };
    setPer();
    audio.addEventListener('loadedmetadata', setPer);

    const tick = () => {
      if (!karaoke) return;
      const { audio: a, lines: ls, timings } = karaoke;
      if (!ls.length) { karaoke.raf = requestAnimationFrame(tick); return; }
      const t = a.currentTime;
      let idx, within;
      if (timings && timings.lines && timings.lines.length === ls.length) {
        // 真实时间轴（faster-whisper 对齐）：前奏无人声时 idx=-1，全部待唱
        idx = -1;
        for (let i = 0; i < ls.length; i++) {
          if (t >= timings.lines[i].start - 0.05) idx = i;
        }
        if (idx >= 0) {
          const st = timings.lines[idx].start;
          const en = Math.max(st + 0.01, timings.lines[idx].end);
          within = Math.min(1, Math.max(0, (t - st) / (en - st)));
        }
      } else {
        // 兜底：均摊
        if (!karaoke.per) { karaoke.raf = requestAnimationFrame(tick); return; }
        idx = Math.min(ls.length - 1, Math.floor(t / karaoke.per));
        within = Math.min(1, Math.max(0, (t - idx * karaoke.per) / karaoke.per));
      }
      const count = idx >= 0 ? Math.floor(within * ls[idx].chars.length) : 0;
      if (idx !== karaoke.lastIdx || count !== karaoke.lastCount) {
        ls.forEach((ln, i) => {
          ln.el.classList.toggle('k-past', i < idx);
          ln.el.classList.toggle('k-cur', i === idx);
          ln.el.classList.toggle('k-next', i > idx);
          if (i === idx) {
            ln.chars.forEach((sp, ci) => sp.classList.toggle('k-on', ci < count));
          } else if (i < idx) {
            ln.chars.forEach((sp) => sp.classList.add('k-on'));
          } else {
            ln.chars.forEach((sp) => sp.classList.remove('k-on'));
          }
        });
        if (idx !== karaoke.lastIdx && idx >= 0) {
          ls[idx].el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        karaoke.lastIdx = idx;
        karaoke.lastCount = count;
      }
      karaoke.raf = requestAnimationFrame(tick);
    };
    audio.addEventListener('play', () => {
      if (karaoke && !karaoke.raf) karaoke.raf = requestAnimationFrame(tick);
    });
    audio.addEventListener('pause', () => {
      if (karaoke && karaoke.raf) { cancelAnimationFrame(karaoke.raf); karaoke.raf = null; }
    });
    audio.addEventListener('ended', () => {
      if (!karaoke) return;
      karaoke.lines.forEach((ln) => {
        ln.el.classList.add('k-past');
        ln.el.classList.remove('k-cur', 'k-next');
        ln.chars.forEach((sp) => sp.classList.add('k-on'));
      });
    });

    // 后台请求真实时间轴，到了就无缝切换
    requestAlign(box, audio);
  }

  // ---------- 歌词对齐（faster-whisper） ----------
  let alignSeq = 0;
  async function requestAlign(box, audio) {
    const hint = box.querySelector('.align-hint');
    const mySeq = ++alignSeq;
    try {
      const audioEl = box.querySelector('.audio-card audio');
      if (!audioEl) return;
      const u = new URL(audioEl.src, location.origin);
      const filename = u.searchParams.get('filename');
      const subfolder = u.searchParams.get('subfolder') || '';
      const lyrics = box.dataset.lyrics || '';
      if (!filename || !lyrics.trim()) return;
      if (hint) { hint.textContent = '🎯 歌词对齐中…'; hint.classList.remove('hidden'); }
      const r = await apiJson('/api/music/align', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename, subfolder, lyrics }),
      });
      let timings = r.timings;
      if (!timings && r.task_id) {
        for (let i = 0; i < 150; i++) {
          await new Promise((res) => setTimeout(res, 2000));
          const s = await apiJson('/api/music/align/' + r.task_id);
          if (s.state === 'done') { timings = s.timings; break; }
          if (s.state === 'error') throw new Error(s.error || '对齐失败');
        }
      }
      if (mySeq !== alignSeq || !karaoke) return; // 已切到别的结果
      if (timings && timings.lines && timings.lines.length === karaoke.lines.length) {
        karaoke.timings = timings;
        karaoke.lastIdx = -2; // 强制重绘
        if (hint) hint.textContent = '🎯 已精准对齐';
      } else if (hint) {
        hint.textContent = '';
      }
    } catch (e) {
      if (hint) hint.textContent = ''; // 对齐失败静默退回均摊
    }
  }

  function audioCard(o) {
    const card = document.createElement('div');
    card.className = 'audio-card result-media';
    const head = document.createElement('div');
    head.className = 'audio-head';
    head.innerHTML = '<span class="audio-icon">🎵</span><span class="audio-title">' +
      escapeHtml($('style').value || '歌曲') + '</span>';
    card.appendChild(head);
    const player = document.createElement('audio');
    player.controls = true;
    player.preload = 'metadata';
    player.src = o.url;
    card.appendChild(player);
    if (o.meta) {
      const bar = document.createElement('div');
      bar.className = 'media-meta';
      const parts = [];
      if (o.meta.duration != null) parts.push('<span>时长 <b>' + o.meta.duration + ' 秒</b></span>');
      if (o.meta.size_bytes != null) parts.push('<span>大小 <b>' + fmtSize(o.meta.size_bytes) + '</b></span>');
      if (o.meta.dir) parts.push('<span class="mm-dir">目录 <b>' + escapeHtml(o.meta.dir) + '</b></span>');
      parts.push('<span class="align-hint" style="color:var(--accent)"></span>');
      bar.innerHTML = parts.join('');
      card.appendChild(bar);
    } else {
      const bar = document.createElement('div');
      bar.className = 'media-meta';
      bar.innerHTML = '<span class="align-hint" style="color:var(--accent)"></span>';
      card.appendChild(bar);
    }
    const row = document.createElement('div');
    row.className = 'row';
    row.style.justifyContent = 'center';
    const a = document.createElement('a');
    a.href = o.url;
    a.textContent = '⬇ 下载';
    a.setAttribute('download', '');
    row.appendChild(a);
    // 去制作 MV：带着这首歌跳转 MV 工坊
    try {
      const u = new URL(o.url, location.origin);
      const fn = u.searchParams.get('filename');
      const sf = u.searchParams.get('subfolder') || '';
      if (fn) {
        const mv = document.createElement('a');
        mv.href = '/static/mv.html?song=' + encodeURIComponent((sf ? sf + '/' : '') + fn);
        mv.textContent = '🎬 去制作 MV';
        mv.style.marginLeft = '18px';
        row.appendChild(mv);
      }
    } catch (e) {}
    card.appendChild(row);
    return card;
  }

  function showResults(outputs, lyricsText) {
    stopKaraoke();
    const box = $('result');
    box.innerHTML = '';
    if (!outputs || !outputs.length) { showEmpty(true); return; }
    showEmpty(false);
    for (const o of outputs) {
      if (o.kind === 'audio') {
        const text = lyricsText != null ? lyricsText : $('lyrics').value;
        box.dataset.lyrics = text;
        const { stage, lines } = buildLyricsStage(text);
        if (lines.length) {
          box.appendChild(stage);
          box.classList.add('has-lyrics');
        }
        const card = audioCard(o);
        box.appendChild(card);
        const player = card.querySelector('audio');
        if (lines.length && player) {
          bindKaraoke(player, lines, o.meta && o.meta.duration, box);
        }
      } else {
        const wrap = document.createElement('div');
        wrap.className = 'result-media';
        const media = document.createElement(o.kind === 'video' ? 'video' : 'img');
        if (o.kind === 'video') media.controls = true;
        media.src = o.url;
        wrap.appendChild(media);
        box.appendChild(wrap);
      }
    }
  }

  // ---------- 生成历史 ----------
  function fmtTime(ts) {
    const d = new Date(ts);
    const p = (n) => String(n).padStart(2, '0');
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function addHistory(promptId, res) {
    const s = loadState();
    const arr = Array.isArray(s.history) ? s.history : [];
    if (arr.some((h) => h.pid === promptId)) return;
    const lyricsHead = ($('lyrics').value.trim().split('\n').find((l) => l.trim() && !l.trim().startsWith('[')) || '').trim();
    arr.unshift({
      pid: promptId,
      time: Date.now(),
      style: $('style').value,
      theme: $('theme').value.trim() || lyricsHead,
      lyrics: $('lyrics').value,
      outputs: res.outputs || [],
      elapsed: res.elapsed_seconds,
    });
    if (arr.length > 20) arr.length = 20;
    saveState({ history: arr });
    renderHistory();
  }

  function renderHistory() {
    const s = loadState();
    const arr = Array.isArray(s.history) ? s.history : [];
    const list = $('history-list');
    list.innerHTML = '';
    $('history-empty').classList.toggle('hidden', arr.length > 0);
    for (const h of arr) {
      const item = document.createElement('div');
      item.className = 'hist-item';
      const del = document.createElement('button');
      del.className = 'hist-del';
      del.textContent = '×';
      del.title = '删除此条';
      del.addEventListener('click', (e) => {
        e.stopPropagation();
        const s2 = loadState();
        const arr2 = Array.isArray(s2.history) ? s2.history : [];
        saveState({ history: arr2.filter((x) => x.pid !== h.pid) });
        renderHistory();
      });
      item.appendChild(del);
      const thumb = document.createElement('div');
      thumb.className = 'hist-thumb hist-thumb-audio';
      thumb.innerHTML = '<span class="hist-note">🎵</span>';
      item.appendChild(thumb);
      const info = document.createElement('div');
      info.className = 'hist-info';
      const dur = h.outputs && h.outputs[0] && h.outputs[0].meta && h.outputs[0].meta.duration;
      info.innerHTML =
        '<div class="hist-title">' + escapeHtml(h.style || '歌曲') + (dur ? ' · ' + dur + '秒' : '') + '</div>' +
        '<div class="hist-sub">' + fmtTime(h.time) + (h.elapsed ? ' · 用时 ' + Number(h.elapsed).toFixed(0) + 's' : '') + '</div>' +
        (h.theme ? '<div class="hist-prompt">' + escapeHtml(h.theme.slice(0, 40)) + '</div>' : '');
      item.appendChild(info);
      item.addEventListener('click', () => {
        if (h.outputs && h.outputs.length) showResults(h.outputs, h.lyrics || '');
      });
      list.appendChild(item);
    }
  }

  // ---------- 初始化 ----------
  async function init() {
    try {
      const data = await apiJson('/api/modes');
      modesList = data.modes || [];
      modeCfg = modesList.find((m) => m.id === 'music');
      if (!modeCfg) throw new Error('后端没有 music 模式');
    } catch (e) {
      $('mode-name').textContent = '加载失败';
      $('mode-desc').textContent = e.message;
      return;
    }
    document.title = 'HorseDance 1.0 · ' + modeCfg.name;
    $('mode-name').textContent = modeCfg.name;
    $('mode-desc').textContent = modeCfg.description;
    renderModeSwitch();

    // 下拉选项
    const fill = (id, options, def) => {
      const sel = $(id);
      for (const o of options) {
        const opt = document.createElement('option');
        opt.value = o; opt.textContent = o;
        sel.appendChild(opt);
      }
      if (def != null) sel.value = def;
    };
    fill('style', modeCfg.styles || [], (modeCfg.styles || [])[0]);
    fill('gender', modeCfg.genders || [], (modeCfg.genders || [])[0]);
    const lo = modeCfg.lyrics_opts || {};
    fill('opt-lang', lo.langs || ['中文'], '中文');
    fill('opt-length', lo.lengths || [], lo.lengths && lo.lengths[1]);
    fill('opt-chorus', lo.chorus || [], lo.chorus && lo.chorus[0]);

    // 参数控件
    const dur = modeCfg.duration || { default: 120, min: 30, max: 300 };
    $('duration').min = dur.min; $('duration').max = dur.max; $('duration').value = dur.default;
    const st = (modeCfg.advanced || {}).steps || { min: 4, max: 50, default: 30 };
    $('adv-steps').min = st.min; $('adv-steps').max = st.max; $('adv-steps').value = st.default;
    syncRangeLabels();

    // 恢复上次状态
    applyState(loadState());
    applySeedMode(); // 恢复种子模式（lastSeed 已在 applyState 就位）
    renderHistory();

    // 有未完成任务则恢复进度显示
    const saved = loadState();
    if (saved.prompt_id) {
      startPolling(saved.prompt_id);
    }

    // 事件
    $('duration').addEventListener('input', () => { syncRangeLabels(); saveState({ duration: parseFloat($('duration').value) }); });
    $('adv-steps').addEventListener('input', () => { syncRangeLabels(); saveState({ steps: parseInt($('adv-steps').value, 10) }); });
    for (const [id, key] of [['style', 'style'], ['gender', 'gender'], ['theme', 'theme'], ['lyrics', 'lyrics'], ['seed', 'seed']]) {
      $(id).addEventListener('change', () => saveState(collectState()));
    }
    for (const id of ['opt-lang', 'opt-length', 'opt-chorus', 'opt-keywords']) {
      $(id).addEventListener('change', () => saveState(collectState()));
    }
    $('seed-mode').addEventListener('change', applySeedMode);
    $('btn-seed-draw').addEventListener('click', drawSeed);
    $('btn-lyrics').addEventListener('click', startLyrics);
    $('btn-generate').addEventListener('click', startGenerate);
    $('btn-cancel').addEventListener('click', cancelGen);
    $('btn-reset').addEventListener('click', () => {
      if (confirm('清空本页的填写内容和生成历史？')) {
        localStorage.removeItem(STATE_KEY);
        location.reload();
      }
    });
    $('btn-clear-history').addEventListener('click', () => {
      saveState({ history: [] });
      renderHistory();
    });
    // 折叠面板
    const bindAdv = (headId, bodyId, arrowId) => {
      $(headId).addEventListener('click', () => {
        const body = $(bodyId);
        const open = body.classList.toggle('hidden');
        $(arrowId).textContent = open ? '▾' : '▴';
      });
    };
    bindAdv('adv-head', 'adv-body', 'adv-arrow');
    bindAdv('lyrics-opts-head', 'lyrics-opts-body', 'lyrics-opts-arrow');

    // 硬件状态条
    pollPerf();
    setInterval(pollPerf, 5000);
    $('perf-free').addEventListener('click', async () => {
      try {
        const r = await apiJson('/api/admin/free', { method: 'POST' });
        alert(r.message || '已释放');
      } catch (e) { alert(e.message); }
    });
  }

  init();
})();
