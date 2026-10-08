// 生成 README 用的界面截图：登录 → 首页 → 生成页 → 手机版首页
// 用法：node shot.mjs            （需要 webapp 已在 8800 运行，且 9224 有无头浏览器）
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';

const CDP_PORT = 9224;
const BASE = 'http://127.0.0.1:8800';
const OUT = new URL('./', import.meta.url);

const cfg = JSON.parse(readFileSync(new URL('../auth_config.json', import.meta.url), 'utf8'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && c.pending.has(msg.id)) {
        const { res, rej } = c.pending.get(msg.id);
        c.pending.delete(msg.id);
        msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
      }
    };
    return c;
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => this.pending.set(id, { res, rej }));
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('页面 JS 异常: ' + JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result?.value;
  }
  async waitFor(expr, label, timeout = 20000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      if (await this.eval(expr)) return true;
      await sleep(400);
    }
    throw new Error(`等待超时：${label}`);
  }
  async shot(name, { fullPage = false } = {}) {
    const params = { format: 'png' };
    if (fullPage) params.captureBeyondViewport = true;
    const { data } = await this.send('Page.captureScreenshot', params);
    const file = new URL(name, OUT);
    writeFileSync(file, Buffer.from(data, 'base64'));
    const kb = (Buffer.from(data, 'base64').length / 1024).toFixed(0);
    console.log(`  已保存 ${name} (${kb} KB)`);
  }
  close() { this.ws.close(); }
}

const dpr = 2;
const page = await fetch(`http://127.0.0.1:${CDP_PORT}/json/new?about:blank`, { method: 'PUT' }).then((r) => r.json());
const cdp = await CDP.connect(page.webSocketDebuggerUrl);
await cdp.send('Page.enable');
await cdp.send('Runtime.enable');
await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: dpr, mobile: false });

// ---- 1. 登录页 ----
await cdp.send('Page.navigate', { url: BASE + '/login' });
await cdp.waitFor(`!!document.querySelector('input[name=username]')`, '登录表单');
await sleep(900);
await cdp.shot('screenshot-login.png');

// ---- 登录 ----
await cdp.eval(`
  document.querySelector('input[name=username]').value = ${JSON.stringify(cfg.auth_user)};
  document.querySelector('input[name=password]').value = ${JSON.stringify(cfg.auth_pass)};
  document.querySelector('form').submit(); 'ok';
`);
await sleep(2000);

// ---- 2. 首页模式列表 ----
await cdp.send('Page.navigate', { url: BASE + '/' });
await cdp.waitFor(`document.querySelectorAll('#cards .card').length > 0`, '首页模式卡片');
await sleep(1200);
const modeCount = await cdp.eval(`document.querySelectorAll('#cards .card').length`);
console.log(`  首页渲染出 ${modeCount} 个模式卡片`);
await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: dpr, mobile: false });
await cdp.shot('screenshot-home.png');

// ---- 3. 生成页（视频模式）----
await cdp.send('Page.navigate', { url: BASE + '/static/mode.html?m=t2v' });
await cdp.waitFor(`document.querySelector('#mode-name') && document.querySelector('#mode-name').textContent.trim() !== '加载中…'`, '生成页模式名');
await cdp.waitFor(`document.querySelector('#lane-strip') && document.querySelector('#lane-strip').children.length > 0`, '车道状态条');
await sleep(2000);
const modeName = await cdp.eval(`document.querySelector('#mode-name').textContent.trim()`);
console.log(`  生成页模式名: ${modeName}`);
await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1100, deviceScaleFactor: dpr, mobile: false });
await sleep(800);
// 车道名含本机硬件型号（如「本地 4070S」）与云端实例名，公开截图前替换为通用文案
const laneText = await cdp.eval(`
  const strip = document.querySelector('#lane-strip');
  const before = strip.textContent.replace(/\\s+/g, ' ').trim();
  const walker = document.createTreeWalker(strip, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    n.nodeValue = n.nodeValue
      .replace(/本地[^·]*/, '本地实例')
      .replace(/云端[^·]*/, '云端实例');
  }
  ({ before, after: strip.textContent.replace(/\\s+/g, ' ').trim() });
`);
console.log(`  车道条文案: [${laneText.before}] -> [${laneText.after}]`);
await sleep(300);
await cdp.shot('screenshot-generate.png');

// ---- 4. 手机版首页 ----
await cdp.send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 3, mobile: true });
await cdp.send('Page.navigate', { url: BASE + '/' });
await cdp.waitFor(`document.querySelectorAll('#cards .card').length > 0`, '手机版模式卡片');
await sleep(1200);
await cdp.shot('screenshot-mobile.png');

cdp.close();
console.log('全部截图完成');
