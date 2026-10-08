// PS 手柄图标秘密入口：正确顺序 ◯◯□✕ 进入管理员界面；错 3 次锁 30 分钟（服务端持久化）
(function () {
  var SEQ = ['circle', 'circle', 'square', 'x'];
  var pad = document.getElementById('secret-pad');
  if (!pad) return;
  var btns = pad.querySelectorAll('.secret-btn');
  var seq = [];
  var hintTimer = null;

  var hint = document.createElement('span');
  hint.className = 'secret-hint';
  hint.hidden = true;
  pad.appendChild(hint);

  function showHint(msg, ms) {
    hint.textContent = msg;
    hint.hidden = false;
    clearTimeout(hintTimer);
    hintTimer = setTimeout(function () { hint.hidden = true; }, ms || 2000);
  }
  function hide() { pad.classList.add('hidden'); }
  function show() { pad.classList.remove('hidden'); }

  function lockRemainMin(until) {
    return Math.max(1, Math.ceil((until - Date.now() / 1000) / 60));
  }

  // 锁定到期后自动重新检测
  function scheduleReveal(until) {
    var wait = Math.max(0, until * 1000 - Date.now()) + 1500;
    setTimeout(status, wait);
  }

  async function status() {
    try {
      var r = await fetch('/api/admin/secret/status');
      var d = await r.json();
      if (d.locked) {
        show();
        showHint('已锁定 ' + lockRemainMin(d.lock_until) + ' 分钟', 3500);
        scheduleReveal(d.lock_until);
      } else show();
    } catch (e) { show(); }
  }

  async function submit(finalSeq) {
    try {
      var r = await fetch('/api/admin/secret?seq=' + finalSeq.join(','), { method: 'POST' });
      var d = await r.json();
      if (d.ok) { location.href = '/admin.html'; return; }
      if (d.locked) {
        showHint('已锁定 ' + lockRemainMin(d.lock_until) + ' 分钟', 3000);
        scheduleReveal(d.lock_until);
        setTimeout(hide, 2800); // 提示显示完再隐藏
      } else {
        pad.classList.remove('shake');
        void pad.offsetWidth; // 重置动画
        pad.classList.add('shake');
        showHint('顺序不对，再试一次～');
      }
    } catch (e) {}
  }

  btns.forEach(function (b) {
    b.addEventListener('click', function () {
      if (pad.classList.contains('hidden')) return;
      var key = b.dataset.key;
      seq.push(key);
      var isPrefix = seq.every(function (k, i) { return k === SEQ[i]; });
      if (isPrefix && seq.length < SEQ.length) return; // 正确前缀，继续等
      var wasCorrect = seq.length === SEQ.length && seq.every(function (k, i) { return k === SEQ[i]; });
      submit(seq);
      seq = [];
    });
  });

  status();
})();
