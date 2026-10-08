/* 模式页面逻辑：按 /api/modes 配置动态渲染 */
(function () {
  const params = new URLSearchParams(location.search);
  const modeId = params.get('m');
  const $ = (id) => document.getElementById(id);

  let modeCfg = null;
  let modesList = []; // 全部模式配置（右键「引用到…」菜单要用，提升到外层作用域）
  const uploaded = {}; // 槽位 label -> ComfyUI 文件名
  const uploadedMeta = {}; // 槽位 label -> {name, subfolder, type}（用于恢复预览）
  const slotDz = {}; // 槽位 label -> dropzone 元素
  const slotOpts = {}; // 槽位 label -> {optional, kind}（删除后重置用）

  // ---------- 状态持久化（localStorage，按模式隔离） ----------
  const STATE_KEY = 'webapp_state_' + modeId;

  function loadState() {
    try { return JSON.parse(localStorage.getItem(STATE_KEY)) || {}; }
    catch (e) { return {}; }
  }

  function saveState(extra) {
    if (!modeCfg) return;
    const s = loadState();
    if (modeCfg.prompt_mode !== 'readonly') {
      s.prompt = $('prompt').value;
      s.enhanced_text = $('enhanced-wrap').classList.contains('hidden')
        ? '' : $('enhanced-text').textContent;
    }
    s.images = {};
    for (const slot of allSlots()) {
      if (uploaded[slot.label]) {
        s.images[slot.label] = uploadedMeta[slot.label] || { name: uploaded[slot.label] };
      }
    }
    if (modeCfg.seconds) s.seconds = parseFloat($('seconds').value);
    if (modeCfg.aspect_ratio) s.aspect_ratio = $('aspect-ratio').value;
    s.seed = parseInt($('seed').value, 10);
    s.seed_mode = $('seed-mode').value;
    if (lastSeed != null) s.last_seed = lastSeed;
    const laneSel = $('lane-select');
    if (laneSel && laneSel.value) s.lane = laneSel.value;
    const adv = collectAdv();
    if (adv) s.adv = adv;
    Object.assign(s, extra || {});
    try { localStorage.setItem(STATE_KEY, JSON.stringify(s)); } catch (e) { /* 存储满等情况忽略 */ }
  }

  function clearSavedPromptId() {
    const s = loadState();
    if (s.prompt_id) {
      delete s.prompt_id;
      try { localStorage.setItem(STATE_KEY, JSON.stringify(s)); } catch (e) { /* ignore */ }
    }
  }

  // 表单变化即时保存
  $('prompt').addEventListener('input', () => saveState());
  $('seed').addEventListener('change', () => saveState());

  // ---------- 种子模式：每次随机 / 随机抽取 / 固定上次 ----------
  let lastSeed = null; // 最近一次生成实际使用的种子（服务端返回）
  function drawSeed() {
    $('seed').value = Math.floor(Math.random() * 2 ** 48);
    saveState();
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
    saveState();
  }
  $('seed-mode').addEventListener('change', applySeedMode);
  $('btn-seed-draw').addEventListener('click', drawSeed);
  $('btn-reset').addEventListener('click', () => {
    localStorage.removeItem(STATE_KEY);
    location.reload();
  });

  function setStatus(el, msg, cls) {
    el.className = 'status-line' + (cls ? ' ' + cls : '');
    el.innerHTML = msg;
    el.classList.remove('hidden');
  }

  function errText(detail) {
    if (typeof detail === 'string') return detail;
    try { return JSON.stringify(detail, null, 2); } catch (e) { return String(detail); }
  }

  async function apiJson(url, opts) {
    const r = await fetch(url, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(errText(data.error || data.detail || ('HTTP ' + r.status)));
    return data;
  }

  // ---------- 图片/视频上传 ----------
  // opts: {optional: bool（可选槽，未上传时不参与生成）, kind: 'image'|'video'}
  function makeSlot(label, opts) {
    opts = opts || {};
    const kind = opts.kind || 'image';
    const wrap = document.createElement('div');
    wrap.className = 'slot';
    if (kind === 'video') wrap.classList.add('slot-video'); // 视频槽标签标红
    wrap.innerHTML =
      '<div class="slot-label">' + label + '</div>' +
      '<div class="dropzone">' + (opts.optional ? '未激活（点击或拖入上传）' : '点击或拖入上传') + '</div>';
    const dz = wrap.querySelector('.dropzone');
    if (opts.optional) dz.classList.add('inactive');
    slotDz[label] = dz;
    slotOpts[label] = { optional: !!opts.optional, kind: kind };

    async function doUpload(file) {
      // 手机端 file.type 可能为空（尤其从文件管理器选图），用扩展名兜底
      const exts = kind === 'video'
        ? ['mp4', 'webm', 'mov', 'mkv', 'avi', 'm4v']
        : ['jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'heic', 'heif'];
      const ext = (file && file.name || '').split('.').pop().toLowerCase();
      const typeOk = file && (file.type.startsWith(kind + '/') || exts.includes(ext));
      if (!file || !typeOk) {
        alert(kind === 'video' ? '请选择视频文件' : '请选择图片文件');
        return;
      }
      dz.innerHTML = '<span class="dz-busy"><span class="spinner"></span>上传中…</span>';
      try {
        const fd = new FormData();
        fd.append('file', file);
        const data = await apiJson('/api/upload', { method: 'POST', body: fd });
        uploaded[label] = data.name;
        uploadedMeta[label] = {
          name: data.name,
          subfolder: data.subfolder || '',
          type: data.type || 'input',
          kind: kind,
        };
        dz.classList.remove('inactive'); // 上传后激活
        showPreview(label);
        saveState();
      } catch (e) {
        dz.innerHTML = '上传失败：' + e.message;
      }
    }

    dz.addEventListener('click', () => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = kind + '/*';
      input.onchange = () => doUpload(input.files[0]);
      input.click();
    });
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => {
      e.preventDefault();
      dz.classList.remove('dragover');
      doUpload(e.dataTransfer.files[0]);
    });
    return wrap;
  }

  // 删除槽位已上传的文件：清空记录、可选槽退回"未激活"
  function resetSlot(label) {
    delete uploaded[label];
    delete uploadedMeta[label];
    const dz = slotDz[label];
    const o = slotOpts[label] || {};
    if (dz) {
      dz.innerHTML = o.optional ? '未激活（点击或拖入上传）' : '点击或拖入上传';
      dz.classList.toggle('inactive', !!o.optional);
    }
    saveState();
  }

  // 显示槽位预览；加载失败（文件已被 ComfyUI 清理）时显示"已失效"占位并清除记录
  function showPreview(label) {
    const dz = slotDz[label];
    const meta = uploadedMeta[label];
    if (!dz || !meta) return;
    const qs = new URLSearchParams({
      filename: meta.name,
      subfolder: meta.subfolder || '',
      type: meta.type || 'input',
    });
    const url = '/api/file?' + qs;
    const onBad = () => {
      delete uploaded[label];
      delete uploadedMeta[label];
      dz.classList.add('inactive');
      dz.innerHTML = '文件已失效，请重新上传';
      saveState();
    };
    let media;
    if (meta.kind === 'video') {
      media = document.createElement('video');
      media.controls = true;
      media.muted = true;
      media.preload = 'metadata';
      media.playsInline = true;
      media.setAttribute('playsinline', '');
      media.setAttribute('webkit-playsinline', '');
    } else {
      media = new Image();
      media.alt = label;
    }
    media.onerror = onBad;
    media.src = url;
    dz.innerHTML = '';
    dz.appendChild(media);
    // 删除按钮（右上角 ×，不触发文件选择）
    const del = document.createElement('button');
    del.className = 'slot-del';
    del.title = '删除已上传文件';
    del.textContent = '×';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      resetSlot(label);
    });
    dz.appendChild(del);
  }

  // 全部槽位（必填 + 可选）
  function allSlots() {
    const req = (modeCfg.image_slots || []).map((l) => ({ label: l, kind: 'image', optional: false }));
    const opt = (modeCfg.optional_slots || [])
      .map((s) => ({ label: s.label, kind: s.kind, optional: true }))
      .sort((a, b) => Number(a.kind === 'video') - Number(b.kind === 'video')); // 视频槽固定排最后
    return req.concat(opt);
  }

  // ---------- 提示词增强 ----------
  let enhanceTimer = null;
  $('btn-enhance').addEventListener('click', async () => {
    const text = $('prompt').value.trim();
    if (!text) { alert('请先输入提示词'); return; }
    const btn = $('btn-enhance');
    const st = $('enhance-status');
    btn.disabled = true;
    $('enhanced-wrap').classList.add('hidden');
    setStatus(st, '<span class="spinner"></span>增强中（约 10-60 秒）…', 'busy');
    try {
      const body = { mode: modeId, text };
      if (modeCfg.seconds) body.seconds = parseFloat($('seconds').value); // 与时长滑块同步
      // 带上已上传的图片（qwen_i2i 的增强需要看到参考图；其他模式后端会忽略）
      const imgs = {};
      for (const slot of allSlots()) {
        if (uploaded[slot.label]) imgs[slot.label] = uploaded[slot.label];
      }
      if (Object.keys(imgs).length) body.images = imgs;
      if (modeCfg.enhance_modes) body.enhance_mode = $('enhance-mode').value; // 提示词优化模式
      body.route = ($('llm-route') || {}).value || 'local'; // 线下本地 / 线上 API
      body.llm_model = localStorage.getItem('hd_llm_model') || ''; // 线下模型选择（⚙️ 里设置）
      const { task_id } = await apiJson('/api/enhance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      clearInterval(enhanceTimer);
      enhanceTimer = setInterval(async () => {
        try {
          const res = await apiJson('/api/enhance/' + task_id);
          if (res.state === 'running') return;
          clearInterval(enhanceTimer);
          btn.disabled = false;
          if (res.state === 'done') {
            setStatus(st, '增强完成', 'ok');
            $('enhanced-text').textContent = res.enhanced_text;
            $('enhanced-wrap').classList.remove('hidden');
            saveState();
          } else {
            setStatus(st, '增强失败：' + (res.error || '未知错误'), 'err');
          }
        } catch (e) {
          clearInterval(enhanceTimer);
          btn.disabled = false;
          setStatus(st, '查询失败：' + e.message, 'err');
        }
      }, 2000);
    } catch (e) {
      btn.disabled = false;
      setStatus(st, '增强请求失败：' + e.message, 'err');
    }
  });

  $('btn-use-enhanced').addEventListener('click', () => {
    $('prompt').value = $('enhanced-text').textContent;
    $('enhanced-wrap').classList.add('hidden');
    $('enhance-status').classList.add('hidden');
    saveState();
  });

  // ---------- 提示词修正：按修改要求改写（增强结果框 或 主提示词窗，均支持） ----------
  async function doRevise(getText, setText, inputEl, stEl, btnEl) {
    const text = getText().trim();
    const instruction = inputEl.value.trim();
    if (!text) { alert('提示词为空'); return; }
    if (!instruction) { alert('请先输入修改要求'); return; }
    btnEl.disabled = true;
    setStatus(stEl, '<span class="spinner"></span>修正中（约 5-30 秒）…', 'busy');
    stEl.classList.remove('hidden');
    try {
      const res = await apiJson('/api/prompt/revise', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, instruction, route: ($('llm-route') || {}).value || 'local',
          llm_model: localStorage.getItem('hd_llm_model') || '' }),
      });
      setText(res.text);
      inputEl.value = '';
      setStatus(stEl, '✅ 已按修改要求修正', 'ok');
      saveState();
    } catch (e) {
      setStatus(stEl, '修正失败：' + e.message, 'err');
    }
    btnEl.disabled = false;
  }
  // 增强结果框里的修正
  const revEnh = () => doRevise(
    () => $('enhanced-text').textContent,
    (t) => { $('enhanced-text').textContent = t; },
    $('revise-input'), $('revise-status'), $('btn-revise'));
  $('btn-revise').addEventListener('click', revEnh);
  $('revise-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); revEnh(); }
  });
  // 主提示词窗的二次修正（直接改写输入框内容）
  const revMain = () => doRevise(
    () => $('prompt').value,
    (t) => { $('prompt').value = t; },
    $('revise-main-input'), $('revise-main-status'), $('btn-revise-main'));
  $('btn-revise-main').addEventListener('click', revMain);
  $('revise-main-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); revMain(); }
  });

  // ---------- 生成 ----------
  let genTimer = null;
  let currentPid = null;

  // ---------- 等待时的弹幕（一问一答 · 催更对话） ----------
  let urgeLines = [];
  let replyLines = [];
  let danmakuTimer = null;
  let danmakuSeq = 0; // 偶数=催，奇数=答，交替出现
  let lastUrgeIdx = -1, lastReplyIdx = -1;

  async function loadDanmakuLines() {
    try {
      const r = await fetch('/static/fun_lines.json?v=20260813f');
      const d = await r.json();
      urgeLines = Array.isArray(d.urge) ? d.urge : [];
      replyLines = Array.isArray(d.reply) ? d.reply : [];
    } catch (e) {
      urgeLines = [];
      replyLines = [];
    }
  }

  function pickLine(arr, lastKey) {
    if (!arr.length) return { text: '', idx: -1 };
    let idx;
    do { idx = Math.floor(Math.random() * arr.length); } while (idx === lastKey && arr.length > 1);
    return { text: arr[idx], idx };
  }

  function spawnDanmaku() {
    const box = $('danmaku');
    const isUrge = (danmakuSeq++ % 2) === 0;
    const u = pickLine(urgeLines, lastUrgeIdx);
    const r = pickLine(replyLines, lastReplyIdx);
    lastUrgeIdx = u.idx;
    lastReplyIdx = r.idx;
    const text = isUrge ? u.text : r.text;
    if (!text) return;
    const el = document.createElement('div');
    el.className = 'danmaku-item ' + (isUrge ? 'urge' : 'reply');
    const lanes = ['6%', '28%', '50%']; // 顶部三条轨道，B站式弹幕
    el.style.setProperty('--y', lanes[Math.floor(Math.random() * lanes.length)]);
    el.appendChild(document.createTextNode(text));
    box.appendChild(el);
    el.addEventListener('animationend', () => el.remove());
    while (box.children.length > 6) box.firstChild.remove(); // 防止积攒过多
  }

  function startDanmaku() {
    clearInterval(danmakuTimer);
    spawnDanmaku();
    danmakuTimer = setInterval(spawnDanmaku, 3500); // 每 3.5 秒弹一条
  }

  function stopDanmaku() {
    clearInterval(danmakuTimer);
    const box = $('danmaku');
    if (box) box.innerHTML = '';
  }

  // ---------- 顶部硬件状态条（CPU/内存/GPU/显存） ----------
  function psColor(p) {
    return p == null ? '' : (p >= 90 ? 'ps-bad' : (p >= 70 ? 'ps-warn' : 'ps-ok'));
  }

  async function loadPerfStrip() {
    try {
      const r = await fetch('/api/admin/perf');
      if (!r.ok) return;
      const d = await r.json();
      const cpu = d.cpu;
      $('ps-cpu').textContent = cpu == null ? '--' : cpu + '%';
      $('ps-cpu').className = psColor(cpu);
      if (d.mem) {
        $('ps-mem').textContent = d.mem.used_gb + 'G';
        $('ps-mem').className = psColor(d.mem.percent);
      }
      const gpu = d.gpu;
      if (gpu) {
        $('ps-gpu').textContent = gpu.util + '%';
        $('ps-gpu').className = psColor(gpu.util);
        $('ps-vram').textContent = gpu.vram_used_gb + 'G';
        $('ps-vram').className = psColor(gpu.vram_total_gb ? gpu.vram_used_gb / gpu.vram_total_gb * 100 : null);
      }
    } catch (e) {}
  }

  $('perf-free').addEventListener('click', async () => {
    const btn = $('perf-free');
    btn.disabled = true;
    btn.textContent = '⏳ 释放中…';
    try {
      const r = await fetch('/api/admin/free', { method: 'POST' });
      const d = await r.json();
      loadPerfStrip();
      btn.textContent = d.busy ? '⏳ 生成中' : '✅ 已释放';
    } catch (e) {
      btn.textContent = '♻️ 释放';
    }
    setTimeout(() => { btn.textContent = '♻️ 释放'; btn.disabled = false; }, 2500);
  });

  // ---------- 算力车道（车道池：自动 / 本地 / 云端） ----------
  let lanesCache = [];
  let laneSelectSig = ''; // 下拉选项签名，没变不重建（避免打断正在操作的用户）
  const LANE_STATUS_TEXT = { idle: '空闲', generating: '生成中', offline: '离线' };

  async function loadLanes() {
    try {
      const d = await apiJson('/api/lanes');
      lanesCache = d.lanes || [];
      renderLaneStrip();
      renderLaneSelect();
    } catch (e) {}
  }

  function renderLaneStrip() {
    const strip = $('lane-strip');
    if (!strip) return;
    const show = lanesCache.filter((l) => l.enabled !== false);
    if (!show.length) { strip.classList.add('hidden'); return; }
    strip.classList.remove('hidden');
    strip.innerHTML = '<span class="ls-label">车道</span>';
    for (const l of show) {
      const chip = document.createElement('span');
      chip.className = 'lane-chip lc-' + l.status;
      const txt = l.status === 'queued'
        ? '排队' + (l.queue_pending || 1)
        : (LANE_STATUS_TEXT[l.status] || l.status);
      chip.innerHTML = '<span class="lc-dot"></span>' + escapeHtml(l.name) + ' · ' + txt;
      strip.appendChild(chip);
    }
  }

  function renderLaneSelect() {
    const sel = $('lane-select');
    if (!sel) return;
    if (modeCfg.local_only) { $('lane-opt').classList.add('hidden'); return; } // 仅本地车道模式不展示选择
    const enabled = lanesCache.filter((l) => l.enabled !== false);
    if (enabled.length <= 1) { $('lane-opt').classList.add('hidden'); return; }
    $('lane-opt').classList.remove('hidden');
    const sig = enabled.map((l) => l.id + ':' + l.status).join('|');
    if (sig === laneSelectSig) return; // 选项没变，不动下拉
    laneSelectSig = sig;
    const prev = sel.value || 'auto';
    sel.innerHTML = '';
    const auto = document.createElement('option');
    auto.value = 'auto';
    auto.textContent = '自动（推荐）';
    sel.appendChild(auto);
    for (const l of enabled) {
      const o = document.createElement('option');
      o.value = l.id;
      o.textContent = l.name + (l.status === 'offline' ? '（离线）' : '');
      sel.appendChild(o);
    }
    sel.value = [...sel.options].some((o) => o.value === prev) ? prev : 'auto';
  }

  function selectedLane() {
    const v = ($('lane-select') || {}).value;
    if (!v || v === 'auto') return null;
    return lanesCache.find((l) => l.id === v) || null;
  }

  $('lane-select').addEventListener('change', () => {
    laneSelectSig = ''; // 用户主动切换后允许重建
    saveState();
    refreshAdvForLane();
  });

  function setProgress(p, elapsed) {
    const wrap = $('progress-wrap');
    if (!p && elapsed == null) { wrap.classList.add('hidden'); return; }
    showEmpty(false);
    wrap.classList.remove('hidden');
    $('progress-fill').style.width = (p ? p.percent : 0) + '%';
    let txt = p ? (p.percent + '%（' + p.value + ' / ' + p.max + ' 步）') : '准备中…';
    if (elapsed != null) txt += ' · 已用 ' + elapsed + ' 秒';
    $('progress-text').textContent = txt;
  }

  function showEmpty(on) {
    $('preview-empty').classList.toggle('hidden', !on);
  }

  function showElapsed(seconds) {
    const el = $('elapsed-line');
    if (seconds == null) { el.classList.add('hidden'); return; }
    el.textContent = '总用时 ' + seconds + ' 秒';
    el.classList.remove('hidden');
  }

  $('btn-generate').addEventListener('click', async () => {
    const editable = modeCfg.prompt_mode !== 'readonly';
    const prompt = editable ? $('prompt').value.trim() : '';
    if (editable && !prompt) { alert('请先输入提示词'); return; }
    const images = {};
    const optional = {};
    for (const slot of allSlots()) {
      if (slot.optional) {
        if (uploaded[slot.label]) optional[slot.label] = uploaded[slot.label]; // 可选槽：传了才带
      } else {
        if (!uploaded[slot.label]) { alert('请上传图片：' + slot.label); return; }
        images[slot.label] = uploaded[slot.label];
      }
    }
    // 种子：每次随机=-1；随机抽取=当前输入框值；固定上次=上次实际使用的种子（无则先随机）
    const seedMode = $('seed-mode').value;
    let seedVal = -1;
    if (seedMode === 'draw') {
      seedVal = parseInt($('seed').value, 10);
      if (isNaN(seedVal) || seedVal < 0) { drawSeed(); seedVal = parseInt($('seed').value, 10); }
    } else if (seedMode === 'last' && lastSeed != null) {
      seedVal = lastSeed;
    }
    const body = {
      mode: modeId,
      prompt,
      images,
      optional,
      seed: seedVal,
      lane: ($('lane-select') || {}).value || 'auto',
    };
    if (modeCfg.seconds) body.seconds = parseFloat($('seconds').value);
    if (modeCfg.aspect_ratio) body.aspect_ratio = $('aspect-ratio').value;
    const adv = collectAdv();
    if (adv) Object.assign(body, adv); // 高级参数随请求提交

    const btn = $('btn-generate');
    const st = $('gen-status');
    btn.disabled = true;
    $('result').innerHTML = '';
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
      const { prompt_id, lane } = genRes;
      // 记录本次实际种子（服务端 -1 时已替换成真随机值），供「固定上次」使用
      if (typeof genRes.seed === 'number') {
        lastSeed = genRes.seed;
        if (seedMode === 'last') $('seed').value = lastSeed;
      }
      saveState({ prompt_id }); // 保存任务 id，离开页面后可恢复
      if (lane && lane.name) setStatus(st, '<span class="spinner"></span>已派发 → ' + lane.name, 'busy');
      startPolling(prompt_id);
    } catch (e) {
      btn.disabled = false;
      setStatus(st, '提交失败：' + e.message, 'err');
    }
  });

  $('btn-cancel').addEventListener('click', async () => {
    const pid = currentPid;
    clearInterval(genTimer);
    currentPid = null;
    const btn = $('btn-generate');
    btn.disabled = false;
    $('btn-cancel').classList.add('hidden');
    stopDanmaku();
    setProgress(null);
    clearSavedPromptId();
    setStatus($('gen-status'), '已取消', 'err');
    if (pid) {
      try { await apiJson('/api/cancel?pid=' + encodeURIComponent(pid), { method: 'POST' }); } catch (e) {}
    }
  });

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
      const laneTag = res.lane && res.lane.name ? '（' + res.lane.name + '）' : '';
      if (res.state === 'queued') {
        setStatus(st, '<span class="spinner"></span>排队中（第 ' + (res.queue_position || '?') + ' 位）' + laneTag + '…', 'busy');
      } else if (res.state === 'running') {
        const elapsed = res.elapsed_running != null ? Math.round(res.elapsed_running) : null;
        setStatus(st, '<span class="spinner"></span>生成中' + laneTag + (elapsed != null ? '（已用 ' + elapsed + ' 秒）' : '') + '…', 'busy');
        setProgress(res.progress || null, elapsed);
      } else if (res.state === 'done') {
        clearInterval(genTimer);
        btn.disabled = false;
        currentPid = null;
        $('btn-cancel').classList.add('hidden');
        stopDanmaku();
        setProgress(null);
        setStatus(st, '✅ 完成' + laneTag, 'ok');
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

  function fmtSize(n) {
    if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
    return n + ' B';
  }

  // 视频/图片参数条（时长/分辨率/帧率/大小/目录，按可用字段渲染）
  function metaBar(meta) {
    const bar = document.createElement('div');
    bar.className = 'media-meta';
    const parts = [];
    if (meta.duration != null) parts.push('<span>时长 <b>' + meta.duration + ' 秒</b></span>');
    if (meta.width) parts.push('<span>分辨率 <b>' + meta.width + '×' + meta.height + '</b></span>');
    if (meta.fps != null) parts.push('<span>帧率 <b>' + meta.fps + ' fps</b></span>');
    if (meta.size_bytes != null) parts.push('<span>大小 <b>' + fmtSize(meta.size_bytes) + '</b></span>');
    if (meta.dir) parts.push('<span class="mm-dir">目录 <b>' + escapeHtml(meta.dir) + '</b></span>');
    bar.innerHTML = parts.join('');
    return bar;
  }

  function showResults(outputs) {
    const box = $('result');
    box.innerHTML = '';
    if (!outputs || !outputs.length) {
      showEmpty(true);
      return;
    }
    showEmpty(false);
    for (const o of outputs) {
      if (o.meta) box.appendChild(metaBar(o.meta));
      let media;
      if (o.kind === 'video') {
        if (isMobile()) {
          // 手机端：大幅缩小的首帧缩略图 + 播放按钮，点播放才进全屏播放器（不再用原生控件盖屏）
          const thumb = document.createElement('video');
          thumb.preload = 'metadata'; // 只加载首帧显示静态画面，不自动播放
          thumb.muted = true;
          thumb.playsInline = true;
          thumb.setAttribute('playsinline', '');
          thumb.setAttribute('webkit-playsinline', '');
          thumb.src = o.url;
          const btn = document.createElement('button');
          btn.className = 'vid-play';
          btn.type = 'button';
          btn.setAttribute('aria-label', '全屏播放');
          btn.innerHTML = '&#9654;';
          const wrap = document.createElement('div');
          wrap.className = 'result-media vid-thumb';
          wrap.appendChild(thumb);
          wrap.appendChild(btn);
          wrap.addEventListener('click', () => openFullscreenPlayer(o.url));
          box.appendChild(wrap);
          const row = document.createElement('div');
          row.className = 'row';
          row.style.justifyContent = 'center';
          const a = document.createElement('a');
          a.href = o.url;
          a.textContent = '⬇ 下载';
          a.setAttribute('download', '');
          row.appendChild(a);
          box.appendChild(row);
          continue;
        }
        media = document.createElement('video');
        media.controls = true;
        media.preload = 'metadata'; // 只加载首帧显示静态画面，不自动播放
        media.playsInline = true;
        media.setAttribute('playsinline', '');
        media.setAttribute('webkit-playsinline', '');
        media.src = o.url;
      } else {
        media = document.createElement('img');
        media.src = o.url;
      }
      const wrap = document.createElement('div');
      wrap.className = 'result-media';
      wrap.appendChild(media);
      box.appendChild(wrap);
      const row = document.createElement('div');
      row.className = 'row';
      row.style.justifyContent = 'center';
      const a = document.createElement('a');
      a.href = o.url;
      a.textContent = '⬇ 下载';
      a.setAttribute('download', '');
      row.appendChild(a);
      box.appendChild(row);
    }
  }

  // 手机端：视频结果点播放 -> 全屏覆盖层播放器，结束按钮直接返回原界面
  function isMobile() {
    return window.matchMedia('(max-width: 899px)').matches;
  }

  function openFullscreenPlayer(url) {
    const ov = document.createElement('div');
    ov.className = 'fs-player';
    const v = document.createElement('video');
    v.src = url;
    v.controls = true;
    v.autoplay = true;
    v.playsInline = true;
    v.setAttribute('playsinline', '');
    v.setAttribute('webkit-playsinline', '');
    const close = document.createElement('button');
    close.className = 'fs-close';
    close.type = 'button';
    close.textContent = '✕ 结束播放';
    const closeFn = () => {
      v.pause();
      v.removeAttribute('src');
      v.load();
      ov.remove();
      document.body.classList.remove('fs-open');
      document.removeEventListener('keydown', onKey);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') closeFn();
    };
    close.addEventListener('click', closeFn);
    ov.appendChild(close);
    ov.appendChild(v);
    document.body.classList.add('fs-open');
    document.body.appendChild(ov);
    document.addEventListener('keydown', onKey);
    const p = v.play();
    if (p && p.catch) p.catch(() => {}); // 某些浏览器阻止自动播放时静默
  }

  // ---------- 生成历史（存于 webapp_state_<modeId>.history，上限 20 条，新→旧） ----------
  function escapeHtml(t) {
    const d = document.createElement('div');
    d.textContent = t;
    return d.innerHTML;
  }

  function fmtTime(ts) {
    const d = new Date(ts);
    const p = (n) => String(n).padStart(2, '0');
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function promptSummary() {
    if (modeCfg.prompt_mode === 'readonly') return '内置提示词';
    const t = $('prompt').value.trim();
    if (!t) return '（空）';
    return t.length > 40 ? t.slice(0, 40) + '…' : t;
  }

  function collectParams() {
    const parts = [];
    if (modeCfg.seconds) parts.push($('seconds').value + ' 秒');
    if (modeCfg.aspect_ratio) parts.push($('aspect-ratio').value.split(' ')[0]);
    return parts.join(' · ');
  }

  function addHistory(promptId, res) {
    const s = loadState();
    const arr = Array.isArray(s.history) ? s.history : [];
    if (arr.length && arr[0].prompt_id === promptId) return; // 同一任务不重复记
    arr.unshift({
      prompt_id: promptId,
      ts: Date.now(),
      prompt: promptSummary(),
      params: collectParams(),
      lane: res.lane && res.lane.name ? res.lane.name : '',
      elapsed: res.elapsed_seconds != null ? res.elapsed_seconds : null,
      outputs: res.outputs || [],
    });
    while (arr.length > 20) arr.pop();
    saveState({ history: arr });
    renderHistory();
  }

  function renderHistory() {
    const s = loadState();
    const arr = Array.isArray(s.history) ? s.history : [];
    const list = $('history-list');
    list.innerHTML = '';
    $('history-empty').classList.toggle('hidden', arr.length > 0);
    for (let i = 0; i < arr.length; i++) {
      const entry = arr[i];
      const item = document.createElement('div');
      item.className = 'hist-item';
      // 单条删除按钮（不触发条目点击）
      const del = document.createElement('button');
      del.className = 'hist-del';
      del.title = '删除此记录';
      del.textContent = '×';
      del.addEventListener('click', (e) => {
        e.stopPropagation();
        const s2 = loadState();
        const arr2 = Array.isArray(s2.history) ? s2.history : [];
        arr2.splice(i, 1);
        saveState({ history: arr2 });
        renderHistory();
      });
      item.appendChild(del);
      const thumb = document.createElement('div');
      thumb.className = 'hist-thumb';
      const o = (entry.outputs || [])[0];
      if (o) {
        // 历史栏只用小缩略图（/api/thumb 生成的小图），懒加载，避免手机端拉整个视频文件
        const media = new Image();
        media.loading = 'lazy';
        media.decoding = 'async';
        media.src = o.thumb || o.url;
        media.onerror = () => {
          entry.dead = true; // 文件已被 ComfyUI 清理
          thumb.innerHTML = '<span class="hist-dead">已失效</span>';
        };
        thumb.appendChild(media);
      } else {
        thumb.innerHTML = '<span class="hist-dead">无文件</span>';
      }
      const meta = document.createElement('div');
      meta.className = 'hist-meta';
      const metaParams = [entry.params || '', entry.lane || ''].filter(Boolean).join(' · ');
      meta.innerHTML =
        '<div class="hist-line1"><span>' + fmtTime(entry.ts) + '</span>' +
        (entry.elapsed != null ? '<span>' + entry.elapsed + ' 秒</span>' : '<span></span>') + '</div>' +
        '<div class="hist-prompt">' + escapeHtml(entry.prompt || '') + '</div>' +
        (metaParams ? '<div class="hist-params">' + escapeHtml(metaParams) + '</div>' : '');
      item.appendChild(thumb);
      item.appendChild(meta);
      item.addEventListener('click', () => showHistoryEntry(entry));
      // 右键历史条目：引用到其它模式
      item.addEventListener('contextmenu', (e) => {
        if (entry.dead) return;
        const o0 = (entry.outputs || [])[0];
        const info = o0 && parseFileUrl(o0.url);
        if (!info) return;
        e.preventDefault();
        openRefMenu(e.clientX, e.clientY, info);
      });
      list.appendChild(item);
    }
  }

  // 点击历史条目：只把媒体加载到预览区，不影响任务状态
  function showHistoryEntry(entry) {
    setProgress(null);
    showElapsed(entry.elapsed);
    if (entry.dead) {
      showEmpty(false);
      $('result').innerHTML = '<p class="status-line err">文件已被 ComfyUI 清理，无法预览</p>';
      return;
    }
    showResults(entry.outputs);
  }

  $('btn-clear-history').addEventListener('click', () => {
    saveState({ history: [] });
    renderHistory();
  });

  // ---------- 右键「引用到…」菜单：把结果文件复制进 input 并带到目标模式的槽位 ----------
  function parseFileUrl(url) {
    try {
      const u = new URL(url, location.origin);
      const filename = u.searchParams.get('filename') || '';
      if (!filename) return null;
      const ext = filename.split('.').pop().toLowerCase();
      return {
        filename,
        subfolder: u.searchParams.get('subfolder') || '',
        kind: ['mp4', 'webm', 'mov', 'mkv', 'avi', 'gif', 'webp'].includes(ext) ? 'video' : 'image',
      };
    } catch (e) { return null; }
  }

  function refTargets(kind) {
    // 图片结果 → 各模式的图片槽（qwen_i2i 全部 8 个可选槽、fl2v 首/尾帧都列出）；
    // 视频结果 → r2v 的参考视频槽
    const find = (id) => (modesList || []).find((m) => m.id === id);
    if (kind === 'video') {
      const m = find('r2v');
      const slot = m && ((m.optional_slots || []).find((s) => s.kind === 'video') || {});
      return slot.label ? [{ mode: 'r2v', name: m.name, slots: [{ label: slot.label, kind: 'video' }] }] : [];
    }
    const imgSlots = (m) => (m.image_slots || []).map((l) => ({ label: l, kind: 'image' }));
    const out = [];
    const mI2i = find('qwen_i2i');
    if (mI2i) {
      const slots = (mI2i.optional_slots || []).filter((s) => s.kind === 'image')
        .map((s) => ({ label: s.label, kind: 'image' }));
      if (slots.length) out.push({ mode: 'qwen_i2i', name: mI2i.name, slots });
    }
    for (const id of ['i2v', 'fl2v', 'r2v', 'quadview']) {
      const m = find(id);
      if (!m) continue;
      let slots = imgSlots(m);
      if (!slots.length) {
        const s = ((m.optional_slots || []).find((x) => x.kind === 'image') || {});
        if (s.label) slots = [{ label: s.label, kind: 'image' }];
      }
      if (slots.length) out.push({ mode: id, name: m.name, slots });
    }
    return out;
  }

  let refMenu = null;
  function closeRefMenu() { if (refMenu) { refMenu.remove(); refMenu = null; } }
  document.addEventListener('click', closeRefMenu);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeRefMenu(); });
  window.addEventListener('blur', closeRefMenu);

  async function referenceTo(t, slot, fileInfo) {
    const r = await apiJson('/api/file/to_input', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: fileInfo.filename, subfolder: fileInfo.subfolder }),
    });
    if (t.mode === modeId) {
      // 目标就是本页：直接进槽位，不用跳转
      uploaded[slot.label] = r.name;
      uploadedMeta[slot.label] = { name: r.name, kind: slot.kind };
      if (slotDz[slot.label]) {
        slotDz[slot.label].classList.remove('inactive');
        showPreview(slot.label);
      }
      saveState();
      return;
    }
    // 跨页：写入目标模式的 localStorage 存档，跳转后由目标页恢复进槽位
    const key = 'webapp_state_' + t.mode;
    let st = {};
    try { st = JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) {}
    st.images = st.images || {};
    st.images[slot.label] = { name: r.name, kind: slot.kind };
    localStorage.setItem(key, JSON.stringify(st));
    location.href = '/static/mode.html?m=' + encodeURIComponent(t.mode);
  }

  function openRefMenu(x, y, fileInfo) {
    closeRefMenu();
    const targets = refTargets(fileInfo.kind);
    if (!targets.length) return;
    refMenu = document.createElement('div');
    refMenu.className = 'ref-menu';
    const head = document.createElement('div');
    head.className = 'ref-menu-head';
    head.textContent = '引用到…';
    refMenu.appendChild(head);
    for (const t of targets) {
      if (t.slots.length === 1) {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = t.name;
        b.title = '进槽位：' + t.slots[0].label;
        b.addEventListener('click', async (e) => {
          e.stopPropagation();
          b.textContent = '引用中…';
          try { await referenceTo(t, t.slots[0], fileInfo); } catch (err) { alert('引用失败：' + err.message); }
          closeRefMenu();
        });
        refMenu.appendChild(b);
      } else {
        // 多槽位：一级模式名 + 悬停展开二级槽位列表
        const item = document.createElement('div');
        item.className = 'ref-item';
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = t.name + ' ▸';
        item.appendChild(b);
        const sub = document.createElement('div');
        sub.className = 'ref-sub';
        for (const slot of t.slots) {
          const sb = document.createElement('button');
          sb.type = 'button';
          sb.textContent = slot.label.replace('（可选）', '');
          sb.addEventListener('click', async (e) => {
            e.stopPropagation();
            sb.textContent = '引用中…';
            try { await referenceTo(t, slot, fileInfo); } catch (err) { alert('引用失败：' + err.message); }
            closeRefMenu();
          });
          sub.appendChild(sb);
        }
        item.appendChild(sub);
        refMenu.appendChild(item);
      }
    }
    document.body.appendChild(refMenu);
    // 防溢出：贴边时收回
    const r = refMenu.getBoundingClientRect();
    refMenu.style.left = Math.min(x, window.innerWidth - r.width - 8) + 'px';
    refMenu.style.top = Math.min(y, window.innerHeight - r.height - 8) + 'px';
  }

  // 结果预览区：右键图片/视频
  $('result').addEventListener('contextmenu', (e) => {
    const m = e.target.closest('.result-media img, .result-media video');
    if (!m || !m.src) return;
    const info = parseFileUrl(m.src);
    if (!info) return;
    e.preventDefault();
    openRefMenu(e.clientX, e.clientY, info);
  });

  // ---------- 高级参数（预览区面板，按 /api/modes advanced 配置渲染） ----------
  const advEls = {
    steps: $('adv-steps'), mp: $('adv-mp'), clip: $('adv-clip'),
    lora: $('adv-lora'), str: $('adv-lora-str'), unet: $('adv-unet'),
  };
  const advOrig = {}; // role -> {options, default}（/api/modes 原始清单，车道切换时恢复用）

  // 模型下拉选项渲染（label 逻辑与初始渲染一致）
  function fillModelOptions(sel, role, opts, defVal) {
    sel.innerHTML = '';
    for (const opt of opts) {
      const o = document.createElement('option');
      o.value = opt;
      if (role === 'unet') {
        // 显示完整相对路径（含子目录），不同目录/同族模型才不会混成一行
        o.textContent = opt + (opt === defVal ? '（默认）' : '');
      } else if (role === 'lora') {
        o.textContent = opt.includes('lightx2v') ? 'lightx2v 4 步加速'
          : (opt.includes('turbo') ? 'turbo_v4 8 步加速（默认）' : opt);
      } else {
        o.textContent = opt;
      }
      if (opt === defVal) o.selected = true;
      sel.appendChild(o);
    }
  }

  // 选中具体车道时，unet/clip/lora 下拉切换为该车道模型清单；auto 档保持本地清单。
  // 车道有 model_options（候选清单）时列出全部候选，否则只列车道预设单值。
  // 用户已手选的值尽量保留（作为额外选项带过去）。
  function refreshAdvForLane() {
    if (!modeCfg || !modeCfg.advanced) return;
    const a = modeCfg.advanced;
    const lane = selectedLane();
    const lm = (lane && lane.models) || {};
    const lmo = (lane && lane.model_options) || {};
    for (const role of ['unet', 'clip', 'lora']) {
      if (!a[role] || !advOrig[role]) continue;
      const sel = advEls[role];
      const ov = role === 'unet' ? (lm['unet_' + modeId] || null)
        : role === 'lora' ? (lm['lora_' + modeId] || lm.lora || null)
        : (lm.clip || null);
      const ol = role === 'unet' ? (lmo['unet_' + modeId] || null) : (lmo[role] || null);
      const prev = sel.value;
      let opts, def;
      if (Array.isArray(ol) && ol.length) {
        opts = ol.slice();
        if (prev && !opts.includes(prev) && prev !== advOrig[role].default) opts.push(prev);
        def = ov || ol[0];
      } else if (ov) {
        opts = [ov];
        if (prev && prev !== ov && prev !== advOrig[role].default) opts.push(prev);
        def = ov;
      } else {
        opts = advOrig[role].options;
        def = advOrig[role].default;
      }
      fillModelOptions(sel, role, opts, def);
      if (opts.includes(prev)) sel.value = prev;
    }
  }

  function collectAdv() {
    if (!modeCfg || !modeCfg.advanced) return undefined;
    const a = modeCfg.advanced, out = {};
    if (a.steps) out.steps = parseInt(advEls.steps.value, 10);
    if (a.megapixels) out.megapixels = parseFloat(advEls.mp.value);
    if (a.clip) out.clip_name = advEls.clip.value;
    if (a.lora) out.lora_name = advEls.lora.value;
    if (a.lora || a.lora_strength) out.lora_strength = parseFloat(advEls.str.value);
    if (a.unet) out.unet_name = advEls.unet.value;
    return out;
  }

  function initAdvanced() {
    const a = modeCfg.advanced;
    if (!a) return;
    $('adv-panel').classList.remove('hidden');
    $('adv-head').addEventListener('click', () => {
      $('adv-body').classList.toggle('hidden');
      $('adv-arrow').classList.toggle('collapsed');
    });
    const onInput = () => saveState();
    if (a.steps) {
      $('adv-steps-wrap').classList.remove('hidden');
      const el = advEls.steps;
      el.min = a.steps.min; el.max = a.steps.max; el.step = 1; el.value = a.steps.default;
      $('adv-steps-val').textContent = el.value;
      el.addEventListener('input', () => { $('adv-steps-val').textContent = el.value; onInput(); });
    }
    if (a.megapixels) {
      $('adv-mp-wrap').classList.remove('hidden');
      const el = advEls.mp;
      el.min = a.megapixels.min; el.max = a.megapixels.max; el.step = a.megapixels.step;
      el.value = a.megapixels.default;
      $('adv-mp-val').textContent = el.value;
      el.addEventListener('input', () => { $('adv-mp-val').textContent = el.value; onInput(); });
    }
    if (a.clip) {
      $('adv-clip-wrap').classList.remove('hidden');
      advOrig.clip = { options: a.clip.options.slice(), default: a.clip.default };
      fillModelOptions(advEls.clip, 'clip', a.clip.options, a.clip.default);
      advEls.clip.addEventListener('change', onInput);
    }
    if (a.unet) {
      $('adv-unet-wrap').classList.remove('hidden');
      advOrig.unet = { options: a.unet.options.slice(), default: a.unet.default };
      fillModelOptions(advEls.unet, 'unet', a.unet.options, a.unet.default);
      advEls.unet.addEventListener('change', onInput);
    }
    if (a.lora) {
      $('adv-lora-wrap').classList.remove('hidden');
      advOrig.lora = { options: a.lora.options.slice(), default: a.lora.default };
      fillModelOptions(advEls.lora, 'lora', a.lora.options, a.lora.default);
      advEls.lora.addEventListener('change', () => {
        // 联动：lightx2v→4 步、turbo_v4→8 步（之后仍可手动改）
        if (a.steps) {
          advEls.steps.value = advEls.lora.value.includes('lightx2v') ? 4 : 8;
          $('adv-steps-val').textContent = advEls.steps.value;
        }
        onInput();
      });
    }
    const strCfg = a.lora ? a.lora.strength : a.lora_strength;
    if (strCfg) {
      $('adv-lora-str-wrap').classList.remove('hidden');
      const el = advEls.str;
      el.min = strCfg.min; el.max = strCfg.max; el.step = strCfg.step; el.value = strCfg.default;
      $('adv-lora-str-val').textContent = el.value;
      el.addEventListener('input', () => { $('adv-lora-str-val').textContent = el.value; onInput(); });
    }
  }

  // 恢复保存的高级参数（先 lora 后 steps，避免联动覆盖保存的步数）
  function restoreAdv(saved) {
    if (!saved || !modeCfg.advanced) return;
    const a = modeCfg.advanced;
    if (a.lora && saved.lora_name) advEls.lora.value = saved.lora_name;
    if (a.steps && saved.steps != null) {
      advEls.steps.value = saved.steps;
      $('adv-steps-val').textContent = saved.steps;
    }
    if (a.megapixels && saved.megapixels != null) {
      advEls.mp.value = saved.megapixels;
      $('adv-mp-val').textContent = saved.megapixels;
    }
    if (a.clip && saved.clip_name) advEls.clip.value = saved.clip_name;
    if (a.unet && saved.unet_name) advEls.unet.value = saved.unet_name;
    if ((a.lora || a.lora_strength) && saved.lora_strength != null) {
      advEls.str.value = saved.lora_strength;
      $('adv-lora-str-val').textContent = saved.lora_strength;
    }
  }

  // ---------- 初始化 ----------
  async function init() {
    if (!modeId) { location.href = '/'; return; }
    loadDanmakuLines(); // 等待时催更弹幕，不阻塞初始化
    loadPerfStrip();
    setInterval(loadPerfStrip, 5000); // 硬件状态每 5 秒刷新
    try {
      const data = await apiJson('/api/modes');
      modesList = data.modes;
      modeCfg = data.modes.find((m) => m.id === modeId);
      if (!modeCfg) throw new Error('未知模式：' + modeId);
    } catch (e) {
      $('mode-name').textContent = '加载失败';
      $('mode-desc').textContent = e.message;
      return;
    }
    document.title = 'HorseDance 1.0 · ' + modeCfg.name;
    $('mode-name').textContent = modeCfg.name;
    $('mode-desc').textContent = modeCfg.description;

    // 顶部模式快速切换（sw 为 null 说明浏览器缓存了旧版 HTML，不能让整页初始化失败）
    const sw = $('mode-switch');
    if (sw) {
      for (const m of modesList) {
        const b = document.createElement('button');
        b.className = 'ms-btn' + (m.id === modeId ? ' active' : '');
        b.textContent = m.name;
        if (m.id === modeId) {
          b.disabled = true;
        } else {
          b.addEventListener('click', () => {
            location.href = m.id === 'music'
              ? '/static/music.html'
              : (m.id === 'mv' ? '/static/mv.html' : '/static/mode.html?m=' + encodeURIComponent(m.id));
          });
        }
        sw.appendChild(b);
      }
    }

    if (modeCfg.prompt_mode === 'readonly') {
      // 只读提示词：展示工作流内置值，隐藏输入框与增强按钮
      $('editable-prompt-wrap').classList.add('hidden');
      $('builtin-prompt-wrap').classList.remove('hidden');
      $('builtin-prompt').textContent = modeCfg.builtin_prompt || '';
    }

    const slots = allSlots();
    if (slots.length) {
      $('images-panel').classList.remove('hidden');
      for (const slot of slots) {
        $('slots').appendChild(makeSlot(slot.label, { optional: slot.optional, kind: slot.kind }));
      }
    }
    if (modeCfg.seconds) {
      $('opt-seconds').classList.remove('hidden');
      const s = $('seconds');
      s.min = modeCfg.seconds.min;
      s.max = modeCfg.seconds.max;
      s.value = modeCfg.seconds.default;
      $('seconds-val').textContent = s.value + ' 秒';
      s.addEventListener('input', () => {
        $('seconds-val').textContent = s.value + ' 秒';
        saveState();
      });
    }
    if (modeCfg.aspect_ratio) {
      $('opt-ratio').classList.remove('hidden');
      const sel = $('aspect-ratio');
      for (const opt of modeCfg.aspect_ratio.options) {
        const o = document.createElement('option');
        o.value = opt;
        o.textContent = opt;
        if (opt === modeCfg.aspect_ratio.default) o.selected = true;
        sel.appendChild(o);
      }
      sel.addEventListener('change', () => saveState());
    }
    if (modeCfg.enhance_modes) {
      // 提示词优化模式选择（如 Qwen 生图的 文生图/图生图，决定增强用哪个 PE 模型）
      const sel = $('enhance-mode');
      for (const opt of modeCfg.enhance_modes.options) {
        const o = document.createElement('option');
        o.value = opt;
        o.textContent = opt;
        if (opt === modeCfg.enhance_modes.default) o.selected = true;
        sel.appendChild(o);
      }
      $('enhance-mode-row').classList.remove('hidden');
    }
    // 提示词 AI 线路：线下本地 / 线上 API（全站共用 localStorage 键 hd_llm_route）
    const routeSel = $('llm-route');
    function refreshLlmRoute() {
      if (!routeSel) return;
      apiJson('/api/llm/route').then((r) => {
        const opt = routeSel.querySelector('option[value=api]');
        if (r.api_configured) {
          opt.disabled = false;
          opt.textContent = '☁️ 线上 API';
          routeSel.title = '提示词 AI 走本地模型还是线上 API';
        } else {
          opt.disabled = true;
          opt.textContent = '☁️ 线上 API（未配置）';
          routeSel.title = '线上 API 需点 ⚙️ 配置后可用';
          if (routeSel.value === 'api') { routeSel.value = 'local'; localStorage.setItem('hd_llm_route', 'local'); }
        }
      }).catch(() => {});
    }
    if (routeSel) {
      routeSel.value = localStorage.getItem('hd_llm_route') || 'local';
      routeSel.addEventListener('change', () => localStorage.setItem('hd_llm_route', routeSel.value));
      refreshLlmRoute();
    }
    // ⚙️ 提示词 AI 设置弹窗：线上 API 三件套 + 线下模型选择（线下模型存 hd_llm_model，全站共用）
    (function initLlmCfg() {
      const pop = $('llm-cfg-pop');
      const btn = $('btn-llm-cfg');
      if (!pop || !btn) return;
      const st = $('llm-cfg-status');
      btn.addEventListener('click', async () => {
        pop.classList.toggle('hidden');
        if (pop.classList.contains('hidden')) return;
        try {
          const c = await apiJson('/api/llm/config');
          $('llm-cfg-base').value = c.base_url || '';
          $('llm-cfg-model').value = c.model || '';
          $('llm-cfg-key').value = '';
          $('llm-cfg-key').placeholder = c.has_key ? '已保存（输入新 key 覆盖）' : 'API Key';
          const sel = $('llm-cfg-local');
          sel.innerHTML = '';
          const cur = localStorage.getItem('hd_llm_model') || '';
          const guide = $('llm-guide-pop');
          guide.innerHTML = '';
          for (const m of c.local_models || []) {
            const o = document.createElement('option');
            o.value = m.name; o.textContent = m.name; o.title = m.desc;
            if (m.name === cur) o.selected = true;
            sel.appendChild(o);
            const row = document.createElement('div');
            const b = document.createElement('b');
            b.textContent = m.name;
            row.appendChild(b);
            row.appendChild(document.createTextNode(' — ' + m.desc));
            guide.appendChild(row);
          }
        } catch (e) {}
      });
      // ⓘ 点击查看各模型用途图例（不悬停触发）
      const tag = $('llm-guide-tag');
      if (tag) {
        tag.addEventListener('click', (e) => { e.stopPropagation(); $('llm-guide-pop').classList.toggle('hidden'); });
      }
      // 点击弹窗以外的区域：关闭弹窗与图例
      document.addEventListener('click', (e) => {
        if (pop.classList.contains('hidden')) return;
        if (pop.contains(e.target) || btn.contains(e.target)) return;
        pop.classList.add('hidden');
        const g = $('llm-guide-pop');
        if (g) g.classList.add('hidden');
      });
      $('llm-cfg-local').addEventListener('change', () => {
        localStorage.setItem('hd_llm_model', $('llm-cfg-local').value);
        setStatus(st, '已记住线下模型选择', 'ok');
        st.classList.remove('hidden');
      });
      // 🔁 拉取可用模型（表单值优先，key 留空用已保存的）
      $('btn-llm-models').addEventListener('click', async () => {
        const b = $('btn-llm-models');
        b.disabled = true;
        setStatus(st, '拉取模型列表…', 'busy');
        st.classList.remove('hidden');
        try {
          const r = await apiJson('/api/llm/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ base_url: $('llm-cfg-base').value, api_key: $('llm-cfg-key').value }),
          });
          const dl = $('llm-model-list');
          dl.innerHTML = '';
          for (const m of r.models) {
            const o = document.createElement('option');
            o.value = m;
            dl.appendChild(o);
          }
          setStatus(st, `✅ 拉到 ${r.models.length} 个模型，点模型名输入框即可选择`, 'ok');
          if (r.models.length && !$('llm-cfg-model').value) $('llm-cfg-model').value = r.models[0];
        } catch (e) {
          setStatus(st, '拉取失败：' + e.message, 'err');
        }
        b.disabled = false;
      });
      $('btn-llm-cfg-save').addEventListener('click', async () => {
        try {
          const r = await apiJson('/api/llm/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              base_url: $('llm-cfg-base').value,
              api_key: $('llm-cfg-key').value,
              model: $('llm-cfg-model').value,
            }),
          });
          setStatus(st, r.configured ? '✅ 已保存，线上 API 可用' : '⚠️ 已保存，但配置不完整（线上仍不可用）',
                    r.configured ? 'ok' : 'err');
          st.classList.remove('hidden');
          refreshLlmRoute(); // 重新探测，启用/禁用「线上 API」选项
        } catch (e) {
          setStatus(st, '保存失败：' + e.message, 'err');
          st.classList.remove('hidden');
        }
      });
    })();

    initAdvanced();

    // ---------- 恢复上次保存的页面状态 ----------
    const saved = loadState();
    if (modeCfg.prompt_mode !== 'readonly') {
      if (saved.prompt) $('prompt').value = saved.prompt;
      if (saved.enhanced_text) {
        $('enhanced-text').textContent = saved.enhanced_text;
        $('enhanced-wrap').classList.remove('hidden');
      }
    }
    if (saved.images) {
      for (const slot of slots) {
        const meta = saved.images[slot.label];
        if (meta && meta.name && slotDz[slot.label]) {
          if (!meta.kind) meta.kind = slot.kind; // 旧存档没有 kind 字段
          uploaded[slot.label] = meta.name;
          uploadedMeta[slot.label] = meta;
          slotDz[slot.label].classList.remove('inactive');
          showPreview(slot.label); // 文件已失效时 onerror 自动显示占位
        }
      }
    }
    if (saved.seconds != null && modeCfg.seconds) {
      $('seconds').value = saved.seconds;
      $('seconds-val').textContent = saved.seconds + ' 秒';
    }
    if (saved.aspect_ratio && modeCfg.aspect_ratio) {
      $('aspect-ratio').value = saved.aspect_ratio;
    }
    if (saved.seed != null && !isNaN(saved.seed)) $('seed').value = saved.seed;
    // 种子模式恢复（lastSeed 要先于 applySeedMode 就位）
    if (saved.last_seed != null) lastSeed = saved.last_seed;
    if (saved.seed_mode && [...$('seed-mode').options].some((o) => o.value === saved.seed_mode)) {
      $('seed-mode').value = saved.seed_mode;
    }
    applySeedMode();
    // 车道：先拉一次车道列表建下拉，再恢复上次选择，最后按车道刷新高级参数
    await loadLanes();
    setInterval(loadLanes, 5000); // 车道状态每 5 秒刷新
    if (saved.lane) {
      const sel = $('lane-select');
      if (sel && [...sel.options].some((o) => o.value === saved.lane)) sel.value = saved.lane;
    }
    refreshAdvForLane();
    restoreAdv(saved.adv);

    // 恢复上次任务：进行中则继续轮询，已完成则直接显示结果
    if (saved.prompt_id) {
      try {
        const res = await apiJson('/api/status/' + saved.prompt_id);
        if (res.state === 'queued' || res.state === 'running') {
          setStatus($('gen-status'), '<span class="spinner"></span>恢复上次任务…', 'busy');
          startPolling(saved.prompt_id);
        } else if (res.state === 'done') {
          setStatus($('gen-status'), '✅ 完成', 'ok');
          showElapsed(res.elapsed_seconds);
          addHistory(saved.prompt_id, res); // 去重：已在历史里则跳过
          showResults(res.outputs);
        } else {
          clearSavedPromptId(); // 历史丢失/出错：静默丢弃，只保留表单
        }
      } catch (e) {
        clearSavedPromptId();
      }
    }

    renderHistory();
  }
  init();
})();
