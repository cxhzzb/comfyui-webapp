/* MV 工坊 —— 五步向导（选歌/规划/配图/生成/合成）
 * 状态存 localStorage（webapp_state_mv），刷新/中断后恢复。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const STATE_KEY = 'webapp_state_mv';
  const SLOT_LABELS = ['主角 1', '主角 2', '场景 1', '场景 2'];

  let modesList = [];
  let pollTimer = null;

  async function apiJson(url, opts) {
    const r = await fetch(url, opts);
    if (r.status === 401) { location.href = '/login'; throw new Error('未登录'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.error || ('HTTP ' + r.status));
    return data;
  }

  // ---------- 状态 ----------
  function loadState() {
    try { return JSON.parse(localStorage.getItem(STATE_KEY)) || {}; } catch (e) { return {}; }
  }
  function saveState(patch) {
    const s = Object.assign(loadState(), patch);
    try { localStorage.setItem(STATE_KEY, JSON.stringify(s)); } catch (e) {}
    return s;
  }

  // ---------- 步骤切换 ----------
  function gotoStep(n) {
    saveState({ step: n });
    for (let i = 1; i <= 5; i++) {
      $('panel-' + i).classList.toggle('hidden', i !== n);
    }
    document.querySelectorAll('.mv-step').forEach((el) => {
      const sn = parseInt(el.dataset.step, 10);
      el.classList.toggle('active', sn === n);
      el.classList.toggle('done', sn < n);
    });
    window.scrollTo({ top: 0, behavior: 'smooth' });
    if (n === 2) ensurePlan();
    if (n === 4) { ensureTask(); renderClipsGrid(); }
  }

  // ---------- 第 1 步：选歌 ----------
  async function loadSongs(preselect) {
    const box = $('song-list');
    try {
      const d = await apiJson('/api/mv/songs');
      const songs = d.songs || [];
      if (!songs.length) {
        box.innerHTML = '<p class="error">还没有可用的歌曲。先去「想把我唱你听」生成一首。</p>';
        return;
      }
      box.innerHTML = '';
      const st = loadState();
      songs.forEach((s) => {
        const card = document.createElement('div');
        card.className = 'song-card';
        const dur = s.duration ? s.duration.toFixed(0) + ' 秒' : '--';
        const date = new Date(s.mtime * 1000);
        const p = (x) => String(x).padStart(2, '0');
        card.innerHTML =
          '<div class="song-main"><div class="song-name">' + (s.style || s.filename) + '</div>' +
          '<div class="song-sub">' + dur + ' · ' + (p(date.getMonth() + 1) + '-' + p(date.getDate()) + ' ' +
            p(date.getHours()) + ':' + p(date.getMinutes())) + ' · ' + s.filename + '</div></div>';
        const play = document.createElement('audio');
        play.controls = true; play.preload = 'none';
        play.src = '/api/file?filename=' + encodeURIComponent(s.filename) +
          '&subfolder=' + encodeURIComponent(s.subfolder) + '&type=output';
        play.className = 'song-play';
        card.appendChild(play);
        if (!s.has_info) {
          const warn = document.createElement('div');
          warn.className = 'song-warn';
          warn.textContent = '缺参数留档，无法规划';
          card.appendChild(warn);
          card.classList.add('disabled');
        } else {
          card.addEventListener('click', (e) => {
            if (e.target.tagName === 'AUDIO') return;
            box.querySelectorAll('.song-card').forEach((c) => c.classList.remove('selected'));
            card.classList.add('selected');
            saveState({ song: { filename: s.filename, subfolder: s.subfolder, style: s.style }, plan: null, task_id: null });
            $('btn-to-2').disabled = false;
          });
          if (preselect && preselect === s.subfolder + '/' + s.filename) card.click();
          else if (!preselect && st.song && st.song.filename === s.filename && st.song.subfolder === s.subfolder) {
            card.classList.add('selected');
            $('btn-to-2').disabled = false;
          }
        }
        box.appendChild(card);
      });
    } catch (e) {
      box.innerHTML = '<p class="error">加载失败：' + e.message + '</p>';
    }
  }

  // ---------- 公共：AI 优化提示词按钮 ----------
  function makeOptimizeBtn(ta, getCtx) {
    const btn = document.createElement('button');
    btn.className = 'btn secondary btn-small';
    btn.textContent = '✨ AI 优化';
    btn.title = '润色这条提示词（线下本地或线上 API，由右上角切换）';
    btn.addEventListener('click', async () => {
      if (!ta.value.trim()) { alert('提示词为空，先写点内容再优化'); return; }
      btn.disabled = true;
      btn.textContent = '优化中…';
      try {
        const ctx = getCtx ? getCtx() : {};
        const r = await apiJson('/api/mv/prompt/optimize', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(Object.assign({ prompt: ta.value,
            route: ($('llm-route') || {}).value || 'local',
            llm_model: localStorage.getItem('hd_llm_model') || '' }, ctx)),
        });
        ta.value = r.prompt;
        ta.dispatchEvent(new Event('change'));
        ta.dispatchEvent(new Event('input'));
      } catch (e) { alert('AI 优化失败：' + e.message); }
      btn.disabled = false;
      btn.textContent = '✨ AI 优化';
    });
    return btn;
  }

  // ---------- 第 2 步：规划 ----------
  let planPolling = false;
  async function ensurePlan(force) {
    const st = loadState();
    if (!st.song) { gotoStep(1); return; }
    if (st.plan && !force) { renderPlan(st.plan); return; }
    if (planPolling) return;
    planPolling = true;
    $('plan-timeline').innerHTML = '';
    $('btn-to-3').disabled = true;
    $('plan-status').innerHTML = '<span class="spinner"></span>规划中：对齐人声时间轴 + AI 写段落提示词（约 30-60 秒）…';
    try {
      const r = await apiJson('/api/mv/plan', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(st.song),
      });
      for (let i = 0; i < 120; i++) {
        await new Promise((res) => setTimeout(res, 3000));
        const s = await apiJson('/api/mv/plan/' + r.task_id);
        if (s.state === 'done') {
          saveState({ plan: s.plan });
          renderPlan(s.plan);
          planPolling = false;
          return;
        }
        if (s.state === 'error') throw new Error(s.error || '规划失败');
      }
      throw new Error('规划超时');
    } catch (e) {
      $('plan-status').textContent = '❌ ' + e.message;
    }
    planPolling = false;
  }

  function renderPlan(plan) {
    $('plan-status').textContent =
      `共 ${plan.duration} 秒 · ${plan.sections.length} 个段落 · ` +
      (plan.aligned ? '已用人声真实时间轴' : '按结构均摊') +
      (plan.prompts_by_ai ? ' · 提示词由 AI 撰写（可改）' : '');
    const box = $('plan-timeline');
    box.innerHTML = '';
    plan.sections.forEach((sec, i) => {
      const card = document.createElement('div');
      card.className = 'plan-card';
      const lyrics = sec.lyrics.length ? sec.lyrics.join(' / ') : '（纯器乐）';
      card.innerHTML =
        '<div class="plan-head"><span class="plan-tag">' + sec.label + '</span>' +
        '<span class="plan-time"></span></div>';
      card.querySelector('.plan-time').textContent =
        sec.start.toFixed(0) + '–' + sec.end.toFixed(0) + ' 秒 · ' +
        sec.clips + ' 段 × ' + sec.clip_len.toFixed(1) + 's';
      const ly = document.createElement('div');
      ly.className = 'plan-lyrics';
      ly.textContent = lyrics;
      card.appendChild(ly);
      const ta = document.createElement('textarea');
      ta.className = 'plan-prompt';
      ta.rows = 3;
      ta.value = sec.prompt;
      ta.addEventListener('change', () => {
        const st = loadState();
        st.plan.sections[i].prompt = ta.value;
        saveState({ plan: st.plan });
      });
      card.appendChild(ta);
      const tools = document.createElement('div');
      tools.className = 'prompt-tools';
      tools.appendChild(makeOptimizeBtn(ta, () => {
        const p = loadState().plan || plan;
        const s2 = p.sections[i];
        return { style: p.style || '', label: s2.label,
                 lyrics: (s2.lyrics || []).join(' / '), lip_sync: !!s2.lip_sync };
      }));
      card.appendChild(tools);
      box.appendChild(card);
    });
    $('btn-to-3').disabled = false;
  }

  // ---------- 第 3 步：逐段配图 ----------
  function renderSectionImages() {
    const st = loadState();
    const box = $('section-images');
    box.innerHTML = '';
    if (!st.plan) return;
    const images = st.images || {};
    st.plan.sections.forEach((sec, si) => {
      const card = document.createElement('div');
      card.className = 'sec-img-card';
      const n = (images[si] || []).filter(Boolean).length;
      const modeName = n >= 2 ? '多参视频' : (n === 1 ? '图生视频' : '文生视频');
      card.innerHTML =
        '<div class="plan-head"><span class="plan-tag">' + sec.label + '</span>' +
        '<span class="plan-time">' + sec.start.toFixed(0) + '–' + sec.end.toFixed(0) + ' 秒 · ' +
        sec.clips + ' 段 · <b class="sec-mode">' + modeName + '</b></span></div>';
      const ta = document.createElement('textarea');
      ta.className = 'plan-prompt';
      ta.rows = 3;
      ta.value = sec.prompt || '';
      ta.addEventListener('input', () => {
        const st2 = loadState();
        st2.plan.sections[si].prompt = ta.value;
        saveState({ plan: st2.plan });
      });
      card.appendChild(ta);
      const tools = document.createElement('div');
      tools.className = 'prompt-tools';
      tools.appendChild(makeOptimizeBtn(ta, () => {
        const p = loadState().plan;
        const s2 = p.sections[si];
        return { style: p.style || '', label: s2.label,
                 lyrics: (s2.lyrics || []).join(' / '), lip_sync: !!s2.lip_sync };
      }));
      const lipLab = document.createElement('label');
      lipLab.className = 'check-row check-row-sm';
      const lipCb = document.createElement('input');
      lipCb.type = 'checkbox';
      lipCb.checked = !!sec.lip_sync;
      lipCb.addEventListener('change', () => {
        const st2 = loadState();
        st2.plan.sections[si].lip_sync = lipCb.checked;
        saveState({ plan: st2.plan });
      });
      lipLab.appendChild(lipCb);
      lipLab.appendChild(document.createTextNode('👄 对口型（演唱特写）'));
      tools.appendChild(lipLab);
      card.appendChild(tools);
      const slots = document.createElement('div');
      slots.className = 'img-slots';
      for (let k = 0; k < 4; k++) {
        slots.appendChild(makeImgSlot(si, k));
      }
      card.appendChild(slots);
      box.appendChild(card);
    });
  }

  function makeImgSlot(si, k) {
    const st = loadState();
    const images = (st.images || {})[si] || [];
    const slot = document.createElement('div');
    slot.className = 'img-slot';
    const fname = images[k];
    slot.innerHTML = '<span class="img-slot-label">' + SLOT_LABELS[k] + '</span>';
    if (fname) {
      slot.classList.add('filled');
      const img = document.createElement('img');
      img.src = '/api/file?filename=' + encodeURIComponent(fname) + '&type=input';
      slot.appendChild(img);
      const del = document.createElement('button');
      del.className = 'img-slot-del';
      del.textContent = '×';
      del.addEventListener('click', (e) => {
        e.stopPropagation();
        const st2 = loadState();
        const imgs = (st2.images || {});
        (imgs[si] = imgs[si] || [])[k] = null;
        saveState({ images: imgs });
        renderSectionImages();
      });
      slot.appendChild(del);
    } else {
      const plus = document.createElement('span');
      plus.className = 'img-slot-plus';
      plus.textContent = '+';
      slot.appendChild(plus);
    }
    slot.addEventListener('click', () => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = 'image/*';
      input.addEventListener('change', async () => {
        if (!input.files || !input.files[0]) return;
        slot.classList.add('uploading');
        try {
          const fd = new FormData();
          fd.append('file', input.files[0]);
          const r = await apiJson('/api/upload', { method: 'POST', body: fd });
          const st2 = loadState();
          const imgs = (st2.images || {});
          (imgs[si] = imgs[si] || [])[k] = r.name || r.filename;
          saveState({ images: imgs });
          renderSectionImages();
        } catch (e) {
          alert('上传失败：' + e.message);
          slot.classList.remove('uploading');
        }
      });
      input.click();
    });
    return slot;
  }

  // ---------- 第 4 步：批量生成 ----------
  let creatingTask = false;
  async function ensureTask() {
    // 进入第 4 步时若还没建任务：先静默创建（不启动），让用户逐条配置
    const st = loadState();
    if (!st.song || !st.plan || st.task_id || creatingTask) return;
    creatingTask = true;
    const images = st.images || {};
    const sections = st.plan.sections.map((sec, i) => ({
      tag: sec.tag, label: sec.label, start: sec.start, end: sec.end,
      clips: sec.clips, clip_len: sec.clip_len,
      prompt: sec.prompt,
      images: (images[i] || []).filter(Boolean),
      lip_sync: !!sec.lip_sync,
    }));
    try {
      const r = await apiJson('/api/mv/generate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: st.song.filename, subfolder: st.song.subfolder,
          sections, autostart: false,
        }),
      });
      saveState({ task_id: r.task_id });
      startTaskPolling();
    } catch (e) {
      $('gen-status').textContent = '创建任务失败：' + e.message;
    }
    creatingTask = false;
  }

  async function startGenerate() {
    // 「全部开始」：任务已在进入本步时建好，这里只是启动队列
    const st = loadState();
    if (!st.task_id) { await ensureTask(); }
    const st2 = loadState();
    if (!st2.task_id) return;
    $('btn-generate').disabled = true;
    try {
      await apiJson('/api/mv/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: st2.task_id }),
      });
      startTaskPolling();
    } catch (e) {
      $('btn-generate').disabled = false;
      $('gen-status').textContent = '启动失败：' + e.message;
    }
  }

  function startTaskPolling() {
    clearInterval(pollTimer);
    pollTimer = setInterval(pollTask, 3000);
    pollTask();
  }

  async function pollTask() {
    const st = loadState();
    if (!st.task_id) return;
    try {
      const t = await apiJson('/api/mv/task/' + st.task_id);
      renderTask(t);
      if (['created', 'generated', 'partial', 'done', 'assemble_error'].includes(t.state)) {
        clearInterval(pollTimer);
      }
    } catch (e) {
      // 任务可能已被清理
    }
  }

  function renderTask(t) {
    const total = t.sections.reduce((n, s) => n + s.clips.length, 0);
    const done = t.sections.reduce((n, s) => n + s.clips.filter((c) => c.state === 'done').length, 0);
    const errs = t.sections.reduce((n, s) => n + s.clips.filter((c) => c.state === 'error').length, 0);
    $('gen-status').textContent =
      t.state === 'created' ? `任务已就绪：${total} 段待生成，可逐条配置后「全部开始」或单独生成` :
      t.state === 'running' ? `生成中 ${done}/${total}…` :
      t.state === 'generated' ? `✅ 全部完成（${total} 段）` :
      t.state === 'partial' ? `⚠️ 完成 ${done}/${total}，${errs} 段失败（可点重试）` :
      t.state === 'assembling' ? '合成中…' :
      t.state === 'done' ? '✅ MV 已合成' :
      t.state === 'assemble_error' ? '❌ 合成失败：' + (t.error || '') : t.state;
    $('btn-generate').disabled = t.state === 'running' || t.state === 'assembling';
    $('btn-generate').textContent = t.state === 'created' ? '▶ 全部开始' : '▶ 继续/全部生成';
    $('btn-to-5').classList.toggle('hidden', done < total);
    renderClipsGrid(t);
  }

  function fmtEta(t, c) {
    // 预计时间：优先用本任务已完成段的平均耗时，否则按 ~24 秒/视频秒估算
    const avg = t.avg_clip_seconds || (c.seconds * 24);
    if (c.state === 'running') {
      const el = c.elapsed_live || 0;
      const remain = Math.max(0, avg - el);
      return `已用 ${el}s · 预计还剩 ${Math.round(remain)}s`;
    }
    return `预计约 ${Math.round(avg)}s`;
  }

  function renderClipsGrid(taskData) {
    const st = loadState();
    const t = taskData || st._task;
    const box = $('clips-grid');
    box.innerHTML = '';
    if (!t || !t.sections) {
      if (st.plan) {
        st.plan.sections.forEach((sec) => {
          const row = document.createElement('div');
          row.className = 'clip-row';
          row.innerHTML = '<span class="plan-tag">' + sec.label + '</span><span class="clip-pending">共 ' +
            sec.clips + ' 段待生成</span>';
          box.appendChild(row);
        });
      }
      return;
    }
    st._task = t;
    saveState({ _task: t });
    t.sections.forEach((sec, si) => {
      const wrap = document.createElement('div');
      wrap.className = 'clip-sec';
      const head = document.createElement('div');
      head.className = 'clip-sec-head';
      head.innerHTML = '<span class="plan-tag">' + sec.label + '</span>' +
        '<span class="plan-time">' + (sec.start != null ? sec.start.toFixed(0) + '–' + sec.end.toFixed(0) + ' 秒' : '') + '</span>';
      wrap.appendChild(head);
      sec.clips.forEach((c, ci) => {
        wrap.appendChild(clipCard(t, si, ci, c));
      });
      box.appendChild(wrap);
    });
  }

  const CLIP_STATE_TXT = { pending: '待生成', created: '待生成', running: '生成中', done: '已完成', error: '失败' };

  function clipCard(t, si, ci, c) {
    const card = document.createElement('div');
    card.className = 'clip-card st-' + c.state;
    const head = document.createElement('div');
    head.className = 'clip-card-head';
    head.innerHTML =
      '<span class="clip-name">#' + (ci + 1) + ' · ' + c.seconds.toFixed(1) + 's · ' +
      { t2v: '文生', i2v: '图生', r2v: '多参' }[c.mode] +
      (c.lip_sync ? ' · 👄对口型' : '') + '</span>' +
      '<span class="clip-state">' + (CLIP_STATE_TXT[c.state] || c.state) + '</span>' +
      '<span class="clip-eta">' + fmtEta(t, c) + '</span>';
    card.appendChild(head);

    // 进度条（生成中显示实时进度；完成显示满格）
    const bar = document.createElement('div');
    bar.className = 'clip-progress';
    const fill = document.createElement('div');
    fill.className = 'clip-progress-fill';
    const txt = document.createElement('span');
    txt.className = 'clip-progress-text';
    if (c.state === 'running') {
      const pct = c.progress && c.progress.percent != null ? c.progress.percent : null;
      fill.style.width = (pct == null ? 100 : pct) + '%';
      if (pct == null) fill.classList.add('indeterminate');
      txt.textContent = pct != null
        ? pct.toFixed(1) + '%（' + c.progress.value + ' / ' + c.progress.max + ' 步）'
        : '准备中（模型加载）…';
    } else if (c.state === 'done') {
      fill.style.width = '100%';
      txt.textContent = c.elapsed ? '用时 ' + c.elapsed + 's' : '';
    } else {
      fill.style.width = '0%';
      txt.textContent = c.state === 'error' ? (c.error || '失败') : '';
    }
    bar.appendChild(fill);
    bar.appendChild(txt);
    card.appendChild(bar);

    // 操作按钮
    const acts = document.createElement('div');
    acts.className = 'clip-actions';
    const btnEdit = document.createElement('button');
    btnEdit.className = 'btn secondary btn-small';
    btnEdit.textContent = '设置';
    btnEdit.addEventListener('click', () => {
      const ed = card.querySelector('.clip-editor');
      ed.classList.toggle('hidden');
    });
    acts.appendChild(btnEdit);
    if (c.state !== 'running') {
      const btnGen = document.createElement('button');
      btnGen.className = 'btn secondary btn-small';
      btnGen.textContent = '单独生成';
      btnGen.addEventListener('click', () => generateOne(t.id, si, ci, card));
      acts.appendChild(btnGen);
    }
    if (c.state === 'done') {
      const btnView = document.createElement('button');
      btnView.className = 'btn secondary btn-small';
      btnView.textContent = '预览';
      btnView.addEventListener('click', () => {
        let pv = card.querySelector('.clip-preview');
        if (pv) {
          const hiding = !pv.classList.contains('hidden');
          pv.classList.toggle('hidden');
          if (hiding) {
            const v = pv.querySelector('video');
            if (v) v.pause();
          }
          return;
        }
        pv = document.createElement('div');
        pv.className = 'clip-preview';
        const v = document.createElement('video');
        v.controls = true;
        v.preload = 'auto';
        v.src = '/api/mv/clip/' + t.id + '/' + encodeURIComponent(c.file);
        pv.appendChild(v);
        card.insertBefore(pv, card.querySelector('.clip-editor'));
        v.play().catch(() => {});
      });
      acts.appendChild(btnView);
    }
    card.appendChild(acts);

    // 编辑窗（默认收起）
    card.appendChild(clipEditor(t, si, ci, c));
    return card;
  }

  function clipEditor(t, si, ci, c) {
    const ed = document.createElement('div');
    ed.className = 'clip-editor hidden';
    const ta = document.createElement('textarea');
    ta.className = 'plan-prompt';
    ta.rows = 3;
    ta.value = c.prompt;
    ed.appendChild(ta);

    const tools = document.createElement('div');
    tools.className = 'prompt-tools';
    const lipCb = document.createElement('input');
    lipCb.type = 'checkbox';
    lipCb.checked = !!c.lip_sync;
    tools.appendChild(makeOptimizeBtn(ta, () => ({
      label: (t.sections[si] || {}).label || '', lip_sync: lipCb.checked,
    })));
    const lipLab = document.createElement('label');
    lipLab.className = 'check-row check-row-sm';
    lipLab.appendChild(lipCb);
    lipLab.appendChild(document.createTextNode('👄 对口型（演唱特写）'));
    tools.appendChild(lipLab);
    ed.appendChild(tools);

    const slots = document.createElement('div');
    slots.className = 'img-slots';
    const imgs = (c.images || []).slice();
    for (let k = 0; k < 4; k++) {
      slots.appendChild(makeClipImgSlot(imgs, k));
    }
    ed.appendChild(slots);

    const row = document.createElement('div');
    row.className = 'clip-actions';
    const btnSave = document.createElement('button');
    btnSave.className = 'btn secondary btn-small';
    btnSave.textContent = '保存设置';
    btnSave.addEventListener('click', async () => {
      try {
        const images = imgs.filter(Boolean);
        await apiJson('/api/mv/clip/update', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: t.id, section: si, clip: ci, prompt: ta.value, images, lip_sync: lipCb.checked }),
        });
        btnSave.textContent = '✓ 已保存';
        setTimeout(() => { btnSave.textContent = '保存设置'; }, 1500);
        pollTask();
      } catch (e) { alert('保存失败：' + e.message); }
    });
    row.appendChild(btnSave);
    const btnGen = document.createElement('button');
    btnGen.className = 'btn btn-primary btn-small';
    btnGen.textContent = '保存并单独生成';
    btnGen.addEventListener('click', async () => {
      try {
        const images = imgs.filter(Boolean);
        await apiJson('/api/mv/clip/update', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: t.id, section: si, clip: ci, prompt: ta.value, images, lip_sync: lipCb.checked }),
        });
        generateOne(t.id, si, ci, null);
      } catch (e) { alert('操作失败：' + e.message); }
    });
    row.appendChild(btnGen);
    ed.appendChild(row);
    return ed;
  }

  function makeClipImgSlot(imgs, k) {
    const slot = document.createElement('div');
    slot.className = 'img-slot img-slot-sm';
    const fname = imgs[k];
    slot.innerHTML = '<span class="img-slot-label">' + SLOT_LABELS[k] + '</span>';
    const render = () => {
      slot.querySelectorAll('img,.img-slot-del,.img-slot-plus').forEach((el) => el.remove());
      if (imgs[k]) {
        slot.classList.add('filled');
        const img = document.createElement('img');
        img.src = '/api/file?filename=' + encodeURIComponent(imgs[k]) + '&type=input';
        slot.appendChild(img);
        const del = document.createElement('button');
        del.className = 'img-slot-del';
        del.textContent = '×';
        del.addEventListener('click', (e) => {
          e.stopPropagation();
          imgs[k] = null;
          render();
        });
        slot.appendChild(del);
      } else {
        slot.classList.remove('filled');
        const plus = document.createElement('span');
        plus.className = 'img-slot-plus';
        plus.textContent = '+';
        slot.appendChild(plus);
      }
    };
    if (!fname) {
      const plus = document.createElement('span');
      plus.className = 'img-slot-plus';
      plus.textContent = '+';
      slot.appendChild(plus);
    } else {
      slot.classList.add('filled');
      const img = document.createElement('img');
      img.src = '/api/file?filename=' + encodeURIComponent(fname) + '&type=input';
      slot.appendChild(img);
    }
    slot.addEventListener('click', () => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = 'image/*';
      input.addEventListener('change', async () => {
        if (!input.files || !input.files[0]) return;
        try {
          const fd = new FormData();
          fd.append('file', input.files[0]);
          const r = await apiJson('/api/upload', { method: 'POST', body: fd });
          imgs[k] = r.name || r.filename;
          render();
        } catch (e) { alert('上传失败：' + e.message); }
      });
      input.click();
    });
    return slot;
  }

  async function generateOne(taskId, si, ci, card) {
    try {
      await apiJson('/api/mv/clip/generate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId, section: si, clip: ci }),
      });
      startTaskPolling();
    } catch (e) {
      alert('单独生成失败：' + e.message);
    }
  }

  async function retryClip(taskId, si, ci) {
    try {
      await apiJson('/api/mv/retry', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId, section: si, clip: ci }),
      });
      startTaskPolling();
    } catch (e) {
      alert('重试失败：' + e.message);
    }
  }

  // ---------- 第 5 步：合成 ----------
  async function startAssemble() {
    const st = loadState();
    if (!st.task_id) return;
    $('btn-assemble').disabled = true;
    $('assemble-status').innerHTML = '<span class="spinner"></span>合成中…';
    try {
      await apiJson('/api/mv/assemble', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: st.task_id, mix_ambient: $('mix-ambient').checked }),
      });
      const timer = setInterval(async () => {
        try {
          const t = await apiJson('/api/mv/task/' + st.task_id);
          if (t.state === 'done' && t.result) {
            clearInterval(timer);
            $('btn-assemble').disabled = false;
            $('assemble-status').textContent = '✅ 合成完成';
            showMvResult(t.result);
          } else if (t.state === 'assemble_error') {
            clearInterval(timer);
            $('btn-assemble').disabled = false;
            $('assemble-status').textContent = '❌ 合成失败：' + (t.error || '');
          }
        } catch (e) {}
      }, 3000);
    } catch (e) {
      $('btn-assemble').disabled = false;
      $('assemble-status').textContent = '提交失败：' + e.message;
    }
  }

  function showMvResult(result) {
    const box = $('mv-result');
    box.innerHTML = '';
    const url = '/api/file?filename=' + encodeURIComponent(result.filename) +
      '&subfolder=' + encodeURIComponent(result.subfolder) + '&type=output';
    const v = document.createElement('video');
    v.controls = true; v.src = url; v.className = 'mv-video';
    box.appendChild(v);
    const row = document.createElement('div');
    row.className = 'row';
    row.style.justifyContent = 'center';
    const a = document.createElement('a');
    a.href = url; a.textContent = '⬇ 下载 MV'; a.setAttribute('download', '');
    row.appendChild(a);
    box.appendChild(row);
  }

  // ---------- 顶部模式切换 ----------
  function renderModeSwitch() {
    const sw = $('mode-switch');
    if (!sw) return;
    for (const m of modesList) {
      const b = document.createElement('button');
      b.className = 'ms-btn' + (m.id === 'mv' ? ' active' : '');
      b.textContent = m.name;
      if (m.id === 'mv') b.disabled = true;
      else b.addEventListener('click', () => {
        location.href = m.id === 'music' ? '/static/music.html' : '/static/mode.html?m=' + encodeURIComponent(m.id);
      });
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

  // ---------- 初始化 ----------
  async function init() {
    try {
      const data = await apiJson('/api/modes');
      modesList = data.modes || [];
    } catch (e) {}
    renderModeSwitch();

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

    // 事件
    $('btn-to-2').addEventListener('click', () => gotoStep(2));
    $('btn-back-1').addEventListener('click', () => gotoStep(1));
    $('btn-replan').addEventListener('click', () => { saveState({ plan: null }); ensurePlan(true); });
    $('btn-to-3').addEventListener('click', () => { renderSectionImages(); gotoStep(3); });
    $('btn-back-2').addEventListener('click', () => gotoStep(2));
    $('btn-to-4').addEventListener('click', () => gotoStep(4));
    $('btn-back-3').addEventListener('click', () => { renderSectionImages(); gotoStep(3); });
    $('btn-generate').addEventListener('click', startGenerate);
    $('btn-to-5').addEventListener('click', () => gotoStep(5));
    $('btn-back-4').addEventListener('click', () => gotoStep(4));
    $('btn-assemble').addEventListener('click', startAssemble);
    $('btn-reset').addEventListener('click', () => {
      if (confirm('清空本页全部进度（选歌/规划/配图/任务）？')) {
        localStorage.removeItem(STATE_KEY);
        location.reload();
      }
    });
    document.querySelectorAll('.mv-step').forEach((el) => {
      el.addEventListener('click', () => {
        const n = parseInt(el.dataset.step, 10);
        const st = loadState();
        if (n < (st.step || 1)) gotoStep(n);
      });
    });

    pollPerf();
    setInterval(pollPerf, 5000);

    // 从音乐页带歌跳转：/static/mv.html?song=subfolder/filename
    const qs = new URLSearchParams(location.search);
    const preselect = qs.get('song');
    await loadSongs(preselect);

    // 恢复进度
    const st = loadState();
    if (st.task_id) startTaskPolling();
    if (st.plan && st.step >= 3) renderSectionImages();
    gotoStep(Math.min(st.step || 1, 5));
  }

  init();
})();
