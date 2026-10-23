'use strict';
// LAN Cache GOOEY front end. Instruments (seven-segment readouts, dials, LED meters, charts) come from Perf-Deck.

// ================================================================ helpers
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const C = { s: [1, 2, 3, 4, 5, 6, 7, 8].map(i => css('--s' + i)), grid: css('--grid'), axis: css('--axis'), muted: css('--muted'), panel: css('--panel'), text: css('--text'), hit: css('--hit'), miss: css('--miss') };
const sig = x => x >= 100 ? x.toFixed(0) : x >= 10 ? x.toFixed(1) : x.toFixed(2);
function splitBps(v) {
  if (v == null) return ['—', ''];
  for (const [u, d] of [['Gbps', 1e9], ['Mbps', 1e6], ['Kbps', 1e3]]) if (v >= d * .9995) return [sig(v / d), u];
  return [String(Math.round(v)), 'bps'];
}
const fmtBps = v => splitBps(v).join(' ').trim();
const rateParts = Bps => splitBps((Bps || 0) * 8);         // bytes/s in, bits/s out
const fmtRate = Bps => fmtBps((Bps || 0) * 8);
function axisBps(v) {
  for (const [u, d] of [['G', 1e9], ['M', 1e6], ['K', 1e3]]) if (v >= d) return parseFloat((v / d).toPrecision(3)) + u;
  return String(Math.round(v));
}
function splitBytes(b) {
  if (b == null) return ['—', ''];
  for (const [u, d] of [['TiB', 2 ** 40], ['GiB', 2 ** 30], ['MiB', 2 ** 20], ['KiB', 2 ** 10]]) if (b >= d) return [sig(b / d), u];
  return [String(Math.round(b)), 'B'];
}
const fmtBytes = b => splitBytes(b).join(' ').trim();
const pct = (a, b) => b > 0 ? Math.round(100 * a / b) : null;
function fmtWhen(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000), now = new Date();
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return d.toDateString() === now.toDateString() ? time : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ', ' + time;
}
function fmtAgo(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 5) return 'now';
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return fmtWhen(ts);
}
const fmtDur = s => s < 3600 ? Math.round(s / 60) + ' min' : s < 86400 ? (s / 3600).toFixed(1) + ' h' : (s / 86400).toFixed(1) + ' days';
const clamp01 = v => Math.max(0, Math.min(1, v || 0));
function ladder(max, floor) {
  const v = Math.max(max || 0, floor), p = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 5, 10].map(m => m * p).find(x => x >= v * .9999);
}
const short = v => axisBps(v);

// ================================================================ instruments (from Perf-Deck)
const SEG = { 0: 'abcdef', 1: 'bc', 2: 'abged', 3: 'abgcd', 4: 'fgbc', 5: 'afgcd', 6: 'afgecd', 7: 'abc', 8: 'abcdefg', 9: 'abcdfg', '-': 'g', ' ': '' };
const SEGP = (() => {
  const h = 1.6, o = .7;
  const H = y => `${2 + o},${y} ${2 + o + h},${y - h} ${14 - o - h},${y - h} ${14 - o},${y} ${14 - o - h},${y + h} ${2 + o + h},${y + h}`;
  const V = (x, y) => `${x},${y + o} ${x + h},${y + o + h} ${x + h},${y + 15 - o - h} ${x},${y + 15 - o} ${x - h},${y + 15 - o - h} ${x - h},${y + o + h}`;
  return { a: H(2), g: H(17), d: H(32), f: V(2, 2), b: V(14, 2), e: V(2, 17), c: V(14, 17) };
})();
function seg7(text, cells) {
  const glyphs = [];
  for (const ch of String(text)) ch === '.' && glyphs.length ? glyphs.at(-1).dot = true : glyphs.push({ ch });
  while (glyphs.length < cells) glyphs.unshift({ ch: ' ' });
  let x = 0, out = '';
  for (const g of glyphs) {
    if (g.ch === ':') { out += `<circle class="on" cx="${x + 2}" cy="11" r="1.7"/><circle class="on" cx="${x + 2}" cy="24" r="1.7"/>`; x += 8; continue; }
    const on = SEG[g.ch] ?? '';
    for (const k in SEGP) out += `<polygon class="${on.includes(k) ? 'on' : 'off'}" points="${SEGP[k]}" transform="translate(${x})"/>`;
    out += `<circle class="${g.dot ? 'on' : 'off'}" cx="${x + 17.8}" cy="32" r="1.5"/>`;
    x += 21;
  }
  return `<svg class="seg7" viewBox="-5 -1 ${x + 5} 36" role="img" aria-label="${esc(String(text).trim())}"><g transform="skewX(-6)">${out}</g></svg>`;
}
function mkDial(host, o) {
  const N = o.big ? 48 : 30, R = 84, gap = 1.4, step = 270 / N, nl = o.big ? 5 : 3;
  const pt = (r, deg) => [100 + r * Math.cos(deg * Math.PI / 180), 100 + r * Math.sin(deg * Math.PI / 180)].map(v => v.toFixed(2));
  let segs = '', labels = '';
  for (let i = 0; i < N; i++) {
    const a = pt(R, 135 + i * step + gap / 2), b = pt(R, 135 + (i + 1) * step - gap / 2);
    segs += `<path class="sg" stroke-width="${o.big ? 9 : 12}" d="M${a[0]} ${a[1]}A${R} ${R} 0 0 1 ${b[0]} ${b[1]}"/>`;
  }
  for (let j = 0; j < nl; j++) {
    const [x, y] = pt(o.big ? 57 : 59, 135 + 270 * j / (nl - 1));
    labels += `<text class="sl" x="${x}" y="${y}" text-anchor="middle" dominant-baseline="middle"></text>`;
  }
  host = typeof host === 'string' ? $(host) : host;
  host.innerHTML = `<div class="dial ${o.big ? 'big' : 'mini'}" style="--c:${o.color}"><svg class="face" viewBox="0 0 200 200" aria-hidden="true">${segs}${labels}
    <g class="pk"><path d="M${100 + R + 7} 100l8 -4.5v9z"/></g>${o.big ? `<g class="nd"><path d="M${100 + R - 8} 100l-11 -4.5v9z"/></g>` : ''}
    <text class="ttl" x="100" y="${o.big ? 180 : 186}" text-anchor="middle">${o.title}</text></svg>
    <div class="ctr"><div class="rd"></div><div class="un"></div></div></div>`;
  const root = host.firstChild, sg = [...root.querySelectorAll('.sg')], sl = [...root.querySelectorAll('.sl')];
  const pk = $('.pk', root), nd = $('.nd', root), rd = $('.rd', root), un = $('.un', root);
  return v => {
    root.classList.toggle('off', !!v.off);
    const f = clamp01(v.f), lit = v.f > 0 ? Math.max(1, Math.round(f * N)) : 0;
    sg.forEach((e, i) => e.classList.toggle('on', i < lit));
    pk.style.transform = `rotate(${135 + 270 * clamp01(v.pf)}deg)`; pk.style.opacity = v.pf > 0 ? 1 : 0;
    if (nd) nd.style.transform = `rotate(${135 + 270 * f}deg)`;
    sl.forEach((e, i) => e.textContent = v.labels[i] ?? '');
    rd.innerHTML = seg7(v.text, 4); un.textContent = v.unit;
  };
}
function mkMeter(host, o) {
  const N = o.n || 30;
  host = typeof host === 'string' ? $(host) : host;
  host.innerHTML = `<div class="meter" style="${o.color ? '--c:' + o.color : ''}"><div class="mh"><span class="k">${o.title}</span><span class="k mx"></span></div>
    <div class="mrow"><div class="leds${o.color ? '' : ' zones'}">${'<i></i>'.repeat(N)}</div><div class="rd"></div><span class="un"></span></div></div>`;
  const root = host.firstChild, leds = [...root.querySelectorAll('.leds i')], rd = $('.rd', root), un = $('.un', root), mx = $('.mx', root);
  return v => {
    root.classList.toggle('off', !!v.off);
    const lit = v.f > 0 ? Math.max(1, Math.round(clamp01(v.f) * N)) : 0, peak = v.pf > 0 ? Math.max(1, Math.round(clamp01(v.pf) * N)) - 1 : -1;
    leds.forEach((e, i) => { e.classList.toggle('on', i < lit); e.classList.toggle('peak', i === peak); });
    rd.innerHTML = seg7(v.text, o.cells || 4); un.textContent = v.unit; mx.textContent = v.scale || '';
  };
}
function mkCount(host, title) {
  host = typeof host === 'string' ? $(host) : host;
  host.innerHTML = `<div class="count"><span class="k">${title}</span><div class="rd"></div><span class="un"></span></div>`;
  const root = host.firstChild, rd = $('.rd', root), un = $('.un', root);
  return (value, unit, cells = 6) => {
    root.classList.toggle('off', value == null);
    rd.innerHTML = seg7(value == null ? '-'.repeat(cells) : String(value), cells); un.textContent = unit || '';
  };
}
function subReadouts(host, defs) {
  host = $(host);
  host.innerHTML = defs.map(d => `<div class="sr"><span class="k">${d}</span><div class="rd"></div><span class="un"></span></div>`).join('');
  const cells = $$('.sr', host);
  return vals => vals.forEach((v, i) => { $('.rd', cells[i]).innerHTML = seg7(v[0], 4); $('.un', cells[i]).textContent = v[1]; });
}
function leds(f, n, cls = '') {
  const lit = f > 0 ? Math.max(1, Math.round(clamp01(f) * n)) : 0;
  return `<div class="leds ${cls}">${Array.from({ length: n }, (_, i) => `<i class="${i < lit ? 'on' : ''}"></i>`).join('')}</div>`;
}
const splitBar = (hit, miss) => {
  const t = hit + miss;
  return `<div class="split" title="${fmtBytes(hit)} from cache · ${fmtBytes(miss)} from internet">${t ? `<b style="width:${100 * hit / t}%"></b><s style="width:${100 * miss / t}%"></s>` : ''}</div>`;
};

// ---------- charts (adapted from Perf-Deck: x is seconds from t0, labels show clock time)
const charts = [];
const tip = $('#tip');
function niceScale(max, n) {
  if (!(max > 0)) max = 1;
  const raw = max / n, p = 10 ** Math.floor(Math.log10(raw)), f = raw / p;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  return { step, top: Math.ceil(max / step - 1e-9) * step };
}
const clock = t => new Date(t * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
class Chart {
  constructor(id, o) {
    this.host = $(id); this.o = o; this.series = []; this.xMax = 10; this.t0 = 0; this.hover = null;
    this.cv = document.createElement('canvas'); this.host.appendChild(this.cv);
    this.ctx = this.cv.getContext('2d');
    new ResizeObserver(() => this.draw()).observe(this.host);
    this.host.addEventListener('pointermove', e => this.move(e));
    this.host.addEventListener('pointerleave', () => { this.hover = null; tip.hidden = true; this.draw(); });
    charts.push(this);
  }
  set(series, t0, xMax) { this.series = series; this.t0 = t0; this.xMax = Math.max(xMax, 1); this.draw(); if (this.hover != null) this.showTip(); }
  geom() { const w = this.host.clientWidth, h = this.host.clientHeight, l = 54, r = 16, t = 10, b = 24; return { w, h, l, t, pw: w - l - r, ph: h - t - b }; }
  x(g, t) { return g.l + t / this.xMax * g.pw; }
  draw() {
    const g = this.geom(), ctx = this.ctx, dpr = window.devicePixelRatio || 1;
    if (g.w < 40 || g.h < 40) return;
    if (this.cv.width !== Math.round(g.w * dpr) || this.cv.height !== Math.round(g.h * dpr)) { this.cv.width = Math.round(g.w * dpr); this.cv.height = Math.round(g.h * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, g.w, g.h);
    let max = 0;
    for (const s of this.series) for (const p of s.pts) if (p[1] > max) max = p[1];
    const sc = niceScale(Math.max(max * 1.04, this.o.minTop || 0), g.h > 220 ? 4 : 3);
    const y = v => g.t + g.ph - v / sc.top * g.ph;
    ctx.font = '11px system-ui, sans-serif'; ctx.fillStyle = C.muted; ctx.lineWidth = 1; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let v = 0; v <= sc.top + sc.step / 2; v += sc.step) {
      const yy = Math.round(y(v)) + .5;
      ctx.strokeStyle = v === 0 ? C.axis : C.grid;
      ctx.beginPath(); ctx.moveTo(g.l, yy); ctx.lineTo(g.l + g.pw, yy); ctx.stroke();
      ctx.fillText(this.o.axis(v), g.l - 8, yy);
    }
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const xs = [60, 120, 300, 600, 900, 1800, 3600].find(s => this.xMax / s <= Math.max(2, Math.floor(g.pw / 90))) || 3600;
    for (let t = Math.ceil(this.t0 / xs) * xs - this.t0; t <= this.xMax + 1e-6; t += xs) ctx.fillText(clock(this.t0 + t), this.x(g, t), g.t + g.ph + 7);
    ctx.save(); ctx.beginPath(); ctx.rect(g.l - 6, 0, g.pw + 12, g.t + g.ph + .5); ctx.clip();
    for (const s of this.series) this.line(g, s, y);
    if (this.hover != null) {
      const hx = Math.round(this.x(g, this.hover)) + .5;
      ctx.strokeStyle = C.axis; ctx.beginPath(); ctx.moveTo(hx, g.t); ctx.lineTo(hx, g.t + g.ph); ctx.stroke();
      for (const s of this.series) { const p = s.pts.find(p => p[0] === this.hover); if (p) this.marker(hx, y(p[1]), s.color); }
    } else for (const s of this.series) { const p = s.pts.at(-1); if (p) this.marker(this.x(g, p[0]), y(p[1]), s.color); }
    ctx.restore();
  }
  marker(x, y, color) { const c = this.ctx; c.beginPath(); c.arc(x, y, 5, 0, 7); c.fillStyle = C.panel; c.fill(); c.beginPath(); c.arc(x, y, 3.5, 0, 7); c.fillStyle = color; c.fill(); }
  line(g, s, y) {
    const ctx = this.ctx, pts = s.pts.filter(p => p[1] != null);
    if (pts.length < 2) return;
    ctx.beginPath(); pts.forEach((p, i) => ctx[i ? 'lineTo' : 'moveTo'](this.x(g, p[0]), y(p[1])));
    if (this.o.area) {
      const fill = new Path2D(); pts.forEach((p, i) => fill[i ? 'lineTo' : 'moveTo'](this.x(g, p[0]), y(p[1])));
      fill.lineTo(this.x(g, pts.at(-1)[0]), y(0)); fill.lineTo(this.x(g, pts[0][0]), y(0)); fill.closePath();
      const grad = ctx.createLinearGradient(0, g.t, 0, g.t + g.ph); grad.addColorStop(0, s.color + '38'); grad.addColorStop(1, s.color + '00');
      ctx.fillStyle = grad; ctx.fill(fill);
    }
    ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.stroke();
  }
  move(e) {
    const g = this.geom(), rect = this.host.getBoundingClientRect();
    const ref = this.series.reduce((a, s) => s.pts.length > a.length ? s.pts : a, []);
    if (!ref.length) return;
    const t = (e.clientX - rect.left - g.l) / g.pw * this.xMax;
    this.hover = ref.reduce((a, p) => Math.abs(p[0] - t) < Math.abs(a[0] - t) ? p : a)[0];
    this.ex = e.clientX; this.ey = e.clientY;
    this.draw(); this.showTip();
  }
  showTip() {
    const rows = this.series.map(s => { const p = s.pts.find(p => p[0] === this.hover); return p && p[1] != null ? `<div class="r"><i class="sw" style="background:${s.color}"></i>${esc(s.name)}<b>${esc((s.tip || this.o.tip)(p[1]))}</b></div>` : ''; }).join('');
    tip.innerHTML = `<div class="t">${new Date((this.t0 + this.hover) * 1000).toLocaleTimeString()}</div>${rows}`;
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let tx = this.ex + 16, ty = this.ey - th - 12;
    if (tx + tw > innerWidth - 8) tx = this.ex - tw - 16;
    if (ty < 8) ty = this.ey + 16;
    tip.style.left = tx + 'px'; tip.style.top = ty + 'px';
  }
}

// ================================================================ API, toasts, dialogs
const ME = { user: null, role: null, csrf: null };
const isAdmin = () => ME.role === 'admin';
async function api(method, path, body) {
  const opt = { method, headers: {}, credentials: 'same-origin' };
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  if (method !== 'GET') opt.headers['X-CSRF-Token'] = ME.csrf || '';
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (r.status === 401 && path !== '/api/login') { showLogin(); throw new Error('Please log in.'); }
  if (!r.ok) { const err = new Error((data && data.error) || r.statusText); err.status = r.status; throw err; }
  return data;
}
function toast(msg, kind = '') {
  const d = document.createElement('div'); d.className = kind; d.textContent = msg;
  $('#toast').appendChild(d); setTimeout(() => d.remove(), kind === 'bad' ? 9000 : 4500);
}
// ask({title, html, ok}) -> Promise<FormData|null>
function ask(o) {
  const dlg = $('#dlg');
  $('#dlg-title').textContent = o.title; $('#dlg-body').innerHTML = o.html || ''; $('#dlg-ok').textContent = o.ok || 'OK';
  $('#dlg-ok').hidden = o.ok === false;
  dlg.style.width = o.wide ? 'min(980px, calc(100vw - 32px))' : '';
  return new Promise(res => {
    dlg.onclose = () => res(dlg.returnValue === 'ok' ? new FormData($('#dlg-form')) : null);
    dlg.returnValue = ''; dlg.showModal();
    const f = $('input, textarea, select', $('#dlg-body')); if (f) f.focus();
  });
}
const confirmBox = (title, text, ok = 'OK') => ask({ title, html: `<p class="sub" style="margin:0">${text}</p>`, ok }).then(Boolean);
// Covers that fail to load fall back to a placeholder (CSP blocks inline onerror handlers).
document.addEventListener('error', e => {
  const t = e.target;
  if (t.tagName === 'IMG' && t.dataset.fallback !== undefined) { t.dataset.fallback === 'hide' ? t.style.visibility = 'hidden' : t.remove(); }
}, true);

// ================================================================ login / boot
function showLogin() {
  $('#app').hidden = true; $('#login').hidden = false; stopTimers();
  if (window._es) { window._es.close(); window._es = null; }
}
$('#login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  $('#login-err').textContent = '';
  try {
    await api('POST', '/api/login', { username: f.get('username'), password: f.get('password') });
    e.target.reset(); boot();
  } catch (err) { $('#login-err').textContent = err.message; }
});
$('#logout').onclick = async () => { try { await api('POST', '/api/logout', {}); } catch (e) { /* ignore */ } showLogin(); };

async function boot() {
  let me;
  try { me = await api('GET', '/api/me'); } catch (e) { return showLogin(); }
  Object.assign(ME, me);
  $('#login').hidden = true; $('#app').hidden = false;
  document.body.classList.toggle('viewer', !isAdmin());
  $('#me-name').textContent = me.user; $('#me-role').textContent = me.role; $('#me-role').className = 'tag-role ' + me.role;
  $('#hostline').textContent = `${me.event || ''} · ${me.hostname} · https://${location.host}`;
  document.title = (me.event || 'LAN Cache') + ' · Deck';
  let want = (location.hash || '#deck').slice(1);
  if (want === 'server') want = 'service';   // old bookmarks
  showTab($(`#tabs [data-tab="${want}"]`) ? want : 'deck');
  startTimers();
}

// ================================================================ tabs + timers
let TAB = 'deck';
const timers = [];
function showTab(t) {
  TAB = t;
  $$('#tabs button').forEach(b => b.classList.toggle('act', b.dataset.tab === t));
  $$('main.page').forEach(p => p.hidden = p.dataset.page !== t);
  history.replaceState(null, '', '#' + t);
  const fn = { preload: loadPreload, hosts: loadHosts, games: loadGames, logs: openLogs, health: loadHealth, service: () => loadServer(true), config: loadConfig, users: loadUsers }[t];
  if (fn) fn(true);
  if (t !== 'logs' && window._es) { window._es.close(); window._es = null; }
  charts.forEach(c => c.draw());
}
$('#tabs').onclick = e => { const b = e.target.closest('button'); if (b) showTab(b.dataset.tab); };
document.addEventListener('click', e => { const g = e.target.closest('[data-goto]'); if (g) showTab(g.dataset.goto); });
function every(ms, fn) { timers.push(setInterval(() => { if (!document.hidden) fn(); }, ms)); }
function startTimers() {
  stopTimers();
  loadOverview();
  every(2000, loadOverview);
  every(2000, () => { if (TAB === 'hosts') loadHosts(); if (TAB === 'health') loadHealth(); });
  every(5000, () => { if (TAB === 'service') loadServer(false); });
  every(1000, () => { if (TAB === 'preload' && (PF.running || ++PF.tick % 5 === 0)) loadPreload(); });
  every(15000, () => { if (TAB === 'games' && $('#game-detail').hidden) loadGames(); if (TAB === 'deck') loadDeckGames(); });
}
function stopTimers() { timers.splice(0).forEach(clearInterval); }

// ================================================================ front panel (every tab)
const LAMPS = [['dns', 'DNS'], ['cache', 'CACHE'], ['logs', 'LOGS'], ['hit', 'HIT'], ['miss', 'MISS'], ['clients', 'CLNT'], 'sep', ['upd', 'UPD'], ['pre', 'PRE'], 'sep', ['cpu', 'CPU'], ['temp', 'TEMP'], ['disk', 'DISK']];
$('#lamps').addEventListener('click', e => { if (e.target.closest('#l-upd, #l-pre')) showTab('preload'); });
$('#lamps').innerHTML = LAMPS.map(l => l === 'sep' ? '<span class="sep"></span>' : `<div class="lamp2" id="l-${l[0]}" title=""><i></i><span>${l[1]}</span></div>`).join('');
function lamp(id, state, blink, title) {
  const e = $('#l-' + id);
  e.className = 'lamp2' + (state ? ' on ' + (state === true ? '' : state) : '') + (blink ? ' blink' : '');
  if (title) e.title = title;
}
$('#sm-leds').innerHTML = '<i></i>'.repeat(40);
function sMeter(bps) {
  // log scale: 1 Mbps .. 10 Gbps over 40 LEDs
  const f = bps > 1e6 ? Math.log10(bps / 1e6) / 4 : bps > 0 ? .01 : 0;
  $$('#sm-leds i').forEach((e, i) => e.classList.toggle('on', i < Math.round(clamp01(f) * 40) || (i === 0 && bps > 0)));
  const [n, u] = splitBps(bps);
  $('#sm-rd').innerHTML = seg7(bps ? n : '0', 4);
  $('#sm-un').textContent = (bps ? u : 'Mbps').toUpperCase();
}

// ================================================================ DECK
const D = {
  hit: mkDial('#g-hit', { big: true, title: 'FROM CACHE', color: css('--hit') }),
  miss: mkDial('#g-miss', { big: true, title: 'FROM INTERNET', color: css('--miss') }),
  ratio: mkMeter('#m-ratio', { title: 'Hit ratio · today', color: css('--hit') }),
  cpu: mkMeter('#m-cpu', { title: 'CPU' }),
  temp: mkMeter('#m-temp', { title: 'CPU temperature' }),
  disk: mkMeter('#m-disk', { title: 'Cache disk' }),
  active: mkCount('#n-active', 'Downloading now'), clients: mkCount('#n-clients', 'Clients today'),
  games: mkCount('#n-games', 'Games cached'), reqs: mkCount('#n-reqs', 'Requests today'),
  subsHit: subReadouts('#subs-hit', ['Today', 'All time', 'Peak 10m', 'Hit %']),
  subsMiss: subReadouts('#subs-miss', ['Today', 'All time', 'Peak 10m', 'Saved']),
};
const cTput = new Chart('#c-tput', { area: true, axis: v => axisBps(v), tip: fmtBps });
let OV = null;
async function loadOverview() {
  let o;
  try { o = await api('GET', '/api/overview'); } catch (e) { return; }
  OV = o;
  const run = new Set(o.containers.filter(c => c.state === 'running').map(c => c.service));
  const anyCtr = o.containers.length > 0;
  lamp('dns', run.has('dns') ? true : anyCtr ? 'crit' : false, false, 'DNS container ' + (run.has('dns') ? 'running' : 'stopped'));
  lamp('cache', run.has('monolithic') ? true : anyCtr ? 'crit' : false, false, 'Cache container ' + (run.has('monolithic') ? 'running' : 'stopped'));
  lamp('logs', o.log_status === 'following' ? true : o.log_status.startsWith('no permission') ? 'crit' : false, false, o.log_status);
  lamp('hit', o.rate_hit > 0, o.rate_hit > 0, fmtRate(o.rate_hit) + ' from cache');
  lamp('miss', o.rate_miss > 0 ? 'warn' : false, o.rate_miss > 0, fmtRate(o.rate_miss) + ' from internet');
  lamp('clients', o.active > 0, false, o.active + ' downloading');
  lamp('cpu', o.cpu > 90 ? 'crit' : o.cpu > 70 ? 'warn' : true, false, `CPU ${o.cpu ?? '?'}%`);
  lamp('temp', o.temp == null ? false : o.temp > 85 ? 'crit' : o.temp > 75 ? 'warn' : true, o.temp > 85, o.temp == null ? 'no sensor' : `CPU ${o.temp} °C`);
  lamp('upd', o.prefill.update.length ? 'warn' : false, o.prefill.update.length > 0, o.prefill.update.length ? 'New game build on Steam: preload it (click)' : 'Cache has the newest build');
  lamp('pre', o.prefill.running ? 'crit' : false, o.prefill.running, o.prefill.running ? 'Preload running (click)' : 'No preload running');
  const pill = $('#srv-pill');
  const up = run.has('monolithic') && run.has('dns');
  pill.innerHTML = `<i class="dot ${up ? 'on' : anyCtr ? 'err' : ''}"></i><span>${!o.docker ? 'Docker not installed' : up ? 'Cache running' : run.size ? 'Cache partly running' : 'Cache stopped'}</span>`;
  sMeter((o.rate_hit + o.rate_miss) * 8);

  if (TAB !== 'deck') return;
  const pts = o.series, t0 = pts.length ? pts[0][0] : 0;
  const peakHit = Math.max(0, ...pts.map(p => p[1])) * 8, peakMiss = Math.max(0, ...pts.map(p => p[2])) * 8;
  const dial = (dialFn, Bps, peak) => {
    const bps = Bps * 8, top = ladder(Math.max(bps, peak), 1e8), [n, u] = splitBps(bps);
    dialFn({ f: bps / top, pf: peak / top, text: bps ? n : '0', unit: bps ? u.toUpperCase() : 'MBPS', labels: [0, .25, .5, .75, 1].map(x => x ? short(top * x) : '0') });
  };
  dial(D.hit, o.rate_hit, peakHit);
  dial(D.miss, o.rate_miss, peakMiss);
  const tb = o.today, ab = o.all;
  D.subsHit([splitBytes(tb.hit), splitBytes(ab.hit), splitBps(peakHit), [String(pct(tb.hit, tb.hit + tb.miss) ?? '-'), '%']]);
  D.subsMiss([splitBytes(tb.miss), splitBytes(ab.miss), splitBps(peakMiss), splitBytes(ab.hit)]);
  const hr = pct(tb.hit, tb.hit + tb.miss);
  D.ratio({ f: (hr || 0) / 100, text: hr == null ? '--' : String(hr), unit: '%', off: hr == null });
  D.cpu({ f: (o.cpu || 0) / 100, text: String(Math.round(o.cpu || 0)), unit: '%' });
  D.temp({ f: (o.temp || 0) / 95, text: o.temp == null ? '--' : String(Math.round(o.temp)), unit: '°C', off: o.temp == null });
  D.active(o.active, 'clients', 4); D.clients(tb.clients, 'unique IPs', 4); D.reqs(tb.reqs, 'requests');
  $('#deck-live').innerHTML = `<i class="dot ${o.active ? 'live' : ''}"></i>${o.active ? o.active + ' downloading' : 'idle'}`;
  cTput.set([{ name: 'From cache', color: C.hit, pts: pts.map(p => [p[0] - t0, p[1] * 8]) },
             { name: 'From internet', color: C.miss, pts: pts.map(p => [p[0] - t0, p[2] * 8]) }], t0, pts.length ? pts.at(-1)[0] - t0 : 600);
  loadDeckHosts();
  if (!DECK_GAMES_LOADED) loadDeckGames();
}
async function loadDeckHosts() {
  const r = await api('GET', '/api/clients/active');
  $('#deck-hosts').innerHTML = r.clients.length ? r.clients.slice(0, 8).map(hostRow).join('') : '<div class="empty">Nobody is downloading right now.</div>';
}
let DECK_GAMES_LOADED = false;
async function loadDeckGames() {
  DECK_GAMES_LOADED = true;
  const r = await api('GET', '/api/games');
  D.games(r.games.filter(g => g.miss > 0).length, 'titles', 4);
  const lim = sizeBytes(r.limit), used = r.disk.bytes;
  D.disk({ f: lim && used != null ? used / lim : 0, text: used != null ? splitBytes(used)[0] : '--', unit: used != null ? splitBytes(used)[1] : '', scale: r.limit ? 'of ' + r.limit : '', off: used == null });
  const top = r.games.slice(0, 6);
  $('#deck-games').innerHTML = top.length ? `<table><tr><th>Game</th><th>Size</th><th>Served</th><th>Clients</th></tr>${top.map(g => `<tr class="click" data-game="${esc(g.id)}"><td>${esc(g.name)}</td><td>${fmtBytes(g.miss)}</td><td>${fmtBytes(g.hit)}</td><td>${g.clients}</td></tr>`).join('')}</table>` : '<div class="empty">Nothing cached yet.</div>';
}
document.addEventListener('click', e => { const r = e.target.closest('[data-game]'); if (r) { showTab('games'); openGame(r.dataset.game); } });
function sizeBytes(s) { const m = /^(\d+(?:\.\d+)?)([kmgt])$/i.exec(s || ''); return m ? m[1] * 1024 ** ('kmgt'.indexOf(m[2].toLowerCase()) + 1) : null; }

// ================================================================ DOWNLOADS
const coverImg = (appid, cls = 'thumb') => appid ? `<img class="${cls}" src="/api/cover/${appid}" alt="" loading="lazy" data-fallback="hide">` : `<span class="${cls}"></span>`;
const SRC_TXT = { cache: 'CACHE', internet: 'INTERNET', mixed: 'MIXED', idle: 'IDLE' };
function hostRow(c) {
  const g = c.games[0] || {};
  const more = c.games.length > 1 ? ` +${c.games.length - 1} more` : '';
  const total = c.hit + c.miss, [rn, ru] = rateParts(c.rate_hit + c.rate_miss);
  return `<div class="host">
    <span class="sled ${c.source}"></span>
    <div class="who"><b>${esc(c.label || c.hostname || c.ip)}</b><span class="k">${esc(c.label || c.hostname ? c.ip + ' · ' : '')}${esc(c.platform || c.svc)}</span></div>
    <div class="gm">${coverImg(g.appid)}<div><b title="${esc(c.games.map(x => x.name).join('\n'))}">${esc(g.name || '—')}${esc(more)}</b><span class="k">${esc(g.service || c.svc)}</span></div></div>
    <span class="src ${c.source}"><i></i>${SRC_TXT[c.source]}</span>
    <div class="ratio"><span class="k"><span>${pct(c.hit, total) ?? '—'}% cache</span><span>${fmtBytes(total)}</span></span>${splitBar(c.hit, c.miss)}</div>
    <div class="spd tot">${seg7(c.rate_hit + c.rate_miss ? rn : '0', 4)}<span class="k">${(c.rate_hit + c.rate_miss ? ru : 'Mbps').toUpperCase()}</span></div>
    ${isAdmin() ? `<button class="iconbtn" data-label="${esc(c.ip)}" data-cur="${esc(c.label || '')}" title="Name this machine (e.g. seat number)">✎</button>` : '<span></span>'}
  </div>`;
}
async function loadHosts(first) {
  const win = $('#win').value;
  const r = await api('GET', '/api/clients/active?window=' + win);
  $('#hosts-n').textContent = r.clients.length;
  $('#hosts').innerHTML = r.clients.length ? r.clients.map(hostRow).join('') : '<div class="empty">Nobody is downloading right now. Machines appear here as soon as they pull game data through the cache.</div>';
  if (first || !loadHosts.n || ++loadHosts.n % 5 === 0) { loadHosts.n = loadHosts.n || 1; loadAllClients(); }
}
$('#win').onchange = () => loadHosts(true);
let ALLCL = [];
async function loadAllClients() { ALLCL = (await api('GET', '/api/clients')).clients; renderAllClients(); }
function renderAllClients() {
  const f = $('#cl-filter').value.toLowerCase();
  const rows = ALLCL.filter(c => !f || [c.ip, c.label, c.hostname, c.top, c.platform].join(' ').toLowerCase().includes(f));
  $('#clients-all').innerHTML = rows.length ? `<table><tr><th>Machine</th><th class="l">IP</th><th class="l">Platform</th><th class="l">Biggest download</th><th>From cache</th><th>From internet</th><th>Hit %</th><th>Last seen</th></tr>
    ${rows.map(c => `<tr class="click" data-client="${esc(c.ip)}"><td>${esc(c.label || c.hostname || '—')}</td><td class="l mono">${esc(c.ip)}</td><td class="l">${esc(c.platform || c.svc || '')}</td><td class="l">${esc(c.top || '')}</td>
    <td>${fmtBytes(c.hit)}</td><td>${fmtBytes(c.miss)}</td><td>${pct(c.hit, c.hit + c.miss) ?? '—'}%</td><td>${fmtAgo(c.last)}</td></tr>`).join('')}</table>` : '<div class="empty">No clients yet.</div>';
}
$('#cl-filter').oninput = renderAllClients;
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-label]');
  if (b) {
    const f = await ask({ title: 'Name this machine', html: `<label class="f">Name for ${esc(b.dataset.label)} (blank to clear)<input type="text" name="label" value="${esc(b.dataset.cur)}" maxlength="40" placeholder="e.g. Seat 12 / Team Red #3"></label>`, ok: 'Save' });
    if (!f) return;
    try { await api('POST', '/api/labels', { ip: b.dataset.label, label: f.get('label') }); loadHosts(true); } catch (err) { toast(err.message, 'bad'); }
    return;
  }
  const row = e.target.closest('[data-client]');
  if (row) {
    const r = await api('GET', '/api/clients/' + encodeURIComponent(row.dataset.client));
    ask({ title: `${r.label || r.hostname || r.ip} (${r.ip})`, ok: false, wide: true,
      html: `<div class="tbl" style="max-height:60vh"><table><tr><th>Game</th><th class="l">Service</th><th>From cache</th><th>From internet</th><th>Hit %</th><th>First</th><th>Last</th></tr>
      ${r.games.map(g => `<tr><td>${esc(g.name)}</td><td class="l">${esc(g.service_name)}</td><td>${fmtBytes(g.hit)}</td><td>${fmtBytes(g.miss)}</td><td>${pct(g.hit, g.hit + g.miss) ?? '—'}%</td><td>${fmtWhen(g.first)}</td><td>${fmtWhen(g.last)}</td></tr>`).join('')}</table></div>` });
  }
});

// ================================================================ CACHE BROWSER
const G = {
  cache: mkMeter('#m-cache', { title: 'Cache size vs limit (CACHE_DISK_SIZE)' }),
  fs: mkMeter('#m-fs', { title: 'Cache drive' }),
  count: mkCount('#n-gcount', 'Games'), stored: mkCount('#n-stored', 'Fetched from internet'),
  served: mkCount('#n-served', 'Served from cache'), saved: mkCount('#n-saved', 'Hit ratio'),
};
let GAMES = [];
async function loadGames() {
  const r = await api('GET', '/api/games');
  GAMES = r.games;
  const lim = sizeBytes(r.limit), used = r.disk.bytes;
  G.cache({ f: lim && used != null ? used / lim : 0, text: used != null ? splitBytes(used)[0] : '--', unit: used != null ? splitBytes(used)[1] : '', scale: r.limit ? 'limit ' + r.limit : '', off: used == null });
  const fu = r.fs.total ? (r.fs.total - r.fs.free) / r.fs.total : 0;
  G.fs({ f: fu, text: String(Math.round(fu * 100)), unit: '% used', scale: `${fmtBytes(r.fs.free)} free of ${fmtBytes(r.fs.total)}` });
  $('#disk-note').textContent = r.disk.at ? `Folder size measured ${fmtAgo(r.disk.at)}${r.disk.error ? ' · ' + r.disk.error : ''}` : 'Measuring folder size…';
  const real = GAMES.filter(g => g.miss > 0 || g.hit > 0);
  const hit = real.reduce((a, g) => a + g.hit, 0), miss = real.reduce((a, g) => a + g.miss, 0);
  G.count(real.length, 'titles', 4);
  const sb = splitBytes(miss), hb = splitBytes(hit);
  G.stored(sb[0], sb[1], 5); G.served(hb[0], hb[1], 5); G.saved(pct(hit, hit + miss) ?? '--', '%', 4);
  renderGames();
}
function renderGames() {
  const f = $('#g-filter').value.toLowerCase(), s = $('#g-sort').value;
  const list = GAMES.filter(g => !f || (g.name + ' ' + g.service_name).toLowerCase().includes(f));
  list.sort({ size: (a, b) => b.miss - a.miss, served: (a, b) => b.hit - a.hit, recent: (a, b) => b.last - a.last, name: (a, b) => a.name.localeCompare(b.name) }[s]);
  $('#games').innerHTML = list.length ? list.map(g => {
    const sb = splitBytes(g.miss), hb = splitBytes(g.hit);
    return `<div class="game" tabindex="0" data-gid="${esc(g.id)}">
      <div class="art"><span class="ph">${esc((g.name.match(/[A-Z0-9]/g) || ['?']).slice(0, 3).join(''))}</span>${g.appid ? `<img src="/api/cover/${g.appid}" alt="" loading="lazy" data-fallback>` : ''}<span class="svc svcb">${esc(g.service_name)}</span></div>
      <div class="body"><div class="ttl" title="${esc(g.name)}">${esc(g.name)}</div>
        <div class="figs"><div class="fig"><span class="k">Size</span>${seg7(sb[0], 4)}<span class="un">${sb[1]}</span></div>
          <div class="fig"><span class="k">Served</span>${seg7(hb[0], 4)}<span class="un">${hb[1]}</span></div>
          <div class="fig"><span class="k">Clients</span>${seg7(String(g.clients), 4)}<span class="un">machines</span></div></div>
        ${splitBar(g.hit, g.miss)}
        <span class="sub">Last download ${fmtAgo(g.last)}</span></div></div>`;
  }).join('') : '<div class="empty">Nothing in the cache yet. Games show up here after the first machine downloads them through the cache.</div>';
}
$('#g-filter').oninput = renderGames; $('#g-sort').onchange = renderGames;
$('#games').addEventListener('click', e => { const c = e.target.closest('[data-gid]'); if (c) openGame(c.dataset.gid); });
$('#games').addEventListener('keydown', e => { const c = e.target.closest('[data-gid]'); if (c && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openGame(c.dataset.gid); } });
async function openGame(gid) {
  let g;
  try { g = await api('GET', '/api/games/' + encodeURIComponent(gid)); } catch (e) { return toast(e.message, 'bad'); }
  const hit = g.clients.reduce((a, c) => a + c.hit, 0), miss = g.clients.reduce((a, c) => a + c.miss, 0);
  $('#games-list').hidden = true;
  const box = $('#game-detail'); box.hidden = false;
  box.innerHTML = `<div class="hrow" style="margin-bottom:14px"><button class="btn" id="g-back">← All games</button></div>
    <div class="detail-head">${g.appid ? `<img src="/api/cover/${g.appid}" alt="" data-fallback>` : ''}
      <div><h1>${esc(g.name)}</h1><div class="sub">${esc(g.service_name)}${g.appid ? ` · Steam app ${g.appid}` : ''}</div>
      <div class="hrow" style="margin-top:12px;gap:22px">
        <div class="fig"><span class="k">Size in cache</span>${seg7(splitBytes(miss)[0], 4)}<span class="un">${splitBytes(miss)[1]}</span></div>
        <div class="fig"><span class="k">Served</span>${seg7(splitBytes(hit)[0], 4)}<span class="un">${splitBytes(hit)[1]}</span></div>
        <div class="fig"><span class="k">Hit ratio</span>${seg7(String(pct(hit, hit + miss) ?? '-'), 3)}<span class="un">%</span></div>
        <div class="fig"><span class="k">Machines</span>${seg7(String(g.clients.length), 3)}<span class="un">clients</span></div></div></div></div>
    <h2 style="margin-top:22px">Download history by machine</h2>
    <div class="tbl"><table><tr><th>Machine</th><th class="l">IP</th><th>From cache</th><th>From internet</th><th class="l">Source</th><th>Started</th><th>Last activity</th></tr>
    ${g.clients.map(c => `<tr><td>${esc(c.label || c.hostname || '—')}</td><td class="l mono">${esc(c.ip)}</td><td>${fmtBytes(c.hit)}</td><td>${fmtBytes(c.miss)}</td>
      <td class="l">${splitBar(c.hit, c.miss)}</td><td>${fmtWhen(c.first)}</td><td>${fmtWhen(c.last)} <span class="sub">(${fmtDur(Math.max(60, c.last - c.first))})</span></td></tr>`).join('')}</table></div>
    <h2 style="margin-top:22px">By hour</h2>
    <div class="tbl" style="max-height:260px"><table><tr><th>Hour</th><th>From cache</th><th>From internet</th></tr>${g.hours.slice().reverse().map(h => `<tr><td>${fmtWhen(h[0])}</td><td>${fmtBytes(h[1])}</td><td>${fmtBytes(h[2])}</td></tr>`).join('')}</table></div>
    ${g.service === 'steam' ? `<h2 style="margin-top:22px">Steam depots</h2><div class="tbl"><table><tr><th>Depot</th><th>From cache</th><th>From internet</th><th></th></tr>
      ${g.parts.map(p => `<tr><td class="mono">${esc(p.key.split('/')[1])}</td><td>${fmtBytes(p.hit)}</td><td>${fmtBytes(p.miss)}</td><td>${isAdmin() && /^\d+$/.test(p.key.split('/')[1]) ? `<button class="btn ghost" data-depot="${esc(p.key.split('/')[1])}">Set game…</button>` : ''}</td></tr>`).join('')}</table></div>
      <p class="sub">Depots are matched to games automatically using the Steam API (needs internet). CS2 is built in.</p>` : ''}`;
  $('#g-back').onclick = () => { box.hidden = true; $('#games-list').hidden = false; loadGames(); };
}
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-depot]');
  if (!b) return;
  const f = await ask({ title: `Which game is depot ${b.dataset.depot}?`, html: `<label class="f">Steam app ID (the number in the store URL; blank = automatic)<input type="text" name="appid" inputmode="numeric" pattern="\\d*"></label>`, ok: 'Save' });
  if (!f) return;
  try { await api('POST', '/api/depots', { depot: b.dataset.depot, appid: f.get('appid') }); toast('Saved'); $('#g-back').click(); } catch (err) { toast(err.message, 'bad'); }
});

// ================================================================ LIVE LOGS
const LOG = { src: 'access', kind: '', paused: false, buf: [], filter: '' };
function lineClass(l) {
  if (/"(HIT|STALE|UPDATING|REVALIDATED)"/.test(l)) return 'hit';
  if (/"(MISS|EXPIRED|BYPASS)"/.test(l)) return 'miss';
  if (/\b(error|crit|emerg|alert|fail(ed)?|denied)\b/i.test(l)) return 'err';
  if (/\bwarn(ing)?\b/i.test(l)) return 'warn';
  return '';
}
function logMatch(l) {
  if (LOG.kind === 'HIT' && lineClass(l) !== 'hit') return false;
  if (LOG.kind === 'MISS' && lineClass(l) !== 'miss') return false;
  return !LOG.filter || l.toLowerCase().includes(LOG.filter);
}
function logHtml(l) {
  let h = esc(l).replace(/^\[([^\]]+)\]/, '<span class="tag">[$1]</span>')
    .replace(/&quot;(HIT|STALE|UPDATING|REVALIDATED)&quot;/, '&quot;<b class="h">$1</b>&quot;').replace(/&quot;(MISS|EXPIRED|BYPASS)&quot;/, '&quot;<b class="m">$1</b>&quot;');
  if (LOG.filter) { const f = esc(LOG.filter).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); h = h.replace(new RegExp(f, 'gi'), m => `<mark>${m}</mark>`); }
  return `<div class="${lineClass(l)}">${h}</div>`;
}
function logRender() {
  const lines = LOG.buf.filter(logMatch).slice(-2500);
  $('#console').innerHTML = lines.map(logHtml).join('') || '<div class="note">No lines yet.</div>';
  logCount(lines.length);
  if ($('#log-follow').checked) $('#console').scrollTop = 1e9;
}
function logCount(n) { $('#log-count').textContent = `${n} lines shown · ${LOG.buf.length} kept${LOG.paused ? ' · paused' : ''}`; }
function logAppend(lines) {
  LOG.buf.push(...lines);
  if (LOG.buf.length > 6000) LOG.buf.splice(0, LOG.buf.length - 5000);
  if (LOG.paused) return logCount($('#console').children.length);
  const con = $('#console');
  if (con.firstChild && con.firstChild.classList.contains('note')) con.innerHTML = '';
  const add = lines.filter(logMatch);
  if (add.length) con.insertAdjacentHTML('beforeend', add.map(logHtml).join(''));
  while (con.children.length > 2500) con.firstChild.remove();
  logCount(con.children.length);
  if ($('#log-follow').checked) con.scrollTop = 1e9;
}
function openLogs() {
  if (window._es) window._es.close();
  LOG.buf = []; $('#console').innerHTML = '<div class="note">Connecting…</div>';
  const es = new EventSource('/api/logs/stream?source=' + LOG.src);
  window._es = es;
  const st = $('#log-state');
  es.onopen = () => st.innerHTML = '<i class="dot live"></i>streaming';
  es.onerror = () => { st.innerHTML = '<i class="dot err"></i>reconnecting…'; };
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.status && d.status !== 'following') logAppend(['— ' + d.status + ' —']);
    if (d.lines) logAppend(d.lines);
    if (d.ended) { es.close(); st.innerHTML = '<i class="dot"></i>ended'; }
  };
}
$('#log-src').onclick = e => { const b = e.target.closest('button'); if (!b) return; $$('#log-src button').forEach(x => x.classList.toggle('act', x === b)); LOG.src = b.dataset.src; openLogs(); };
$('#log-kind').onclick = e => { const b = e.target.closest('button'); if (!b) return; $$('#log-kind button').forEach(x => x.classList.toggle('act', x === b)); LOG.kind = b.dataset.k; logRender(); };
let _lf; $('#log-filter').oninput = e => { clearTimeout(_lf); _lf = setTimeout(() => { LOG.filter = e.target.value.toLowerCase(); logRender(); }, 200); };
$('#log-pause').onclick = () => { LOG.paused = !LOG.paused; $('#log-pause').textContent = LOG.paused ? 'Resume' : 'Pause'; if (!LOG.paused) logRender(); else logCount($('#console').children.length); };
$('#log-clear').onclick = () => { LOG.buf = []; logRender(); };
$('#console').addEventListener('wheel', e => { if (e.deltaY < 0) $('#log-follow').checked = false; });

// ================================================================ HEALTH
const H = {
  cpu: mkDial('#h-cpu', { title: 'CPU %', color: C.s[0] }), temp: mkDial('#h-temp', { title: 'TEMP °C', color: C.s[1] }),
  freq: mkDial('#h-freq', { title: 'CLOCK GHZ', color: C.s[2] }), mem: mkDial('#h-mem', { title: 'MEMORY %', color: C.s[6] }),
};
$('#h-meters').innerHTML = '<div class="well" id="hm-cache"></div><div class="well" id="hm-root"></div><div class="well" id="hm-load"></div><div class="well" id="hm-swap"></div>';
Object.assign(H, { mCache: mkMeter('#hm-cache', { title: 'Cache drive' }), mRoot: mkMeter('#hm-root', { title: 'System drive' }), mLoad: mkMeter('#hm-load', { title: 'Load average (1 min)' }), mSwap: mkMeter('#hm-swap', { title: 'Swap used' }) });
const cCpu = new Chart('#c-cpu', { axis: v => String(Math.round(v)), tip: v => v.toFixed(1), minTop: 100 });
const cNet = new Chart('#c-net', { area: true, axis: axisBps, tip: fmtBps });
let healthN = 0;
async function loadHealth() {
  const r = await api('GET', '/api/system' + (healthN++ % 5 === 0 ? '?containers=1' : ''));
  const n = r.now, info = r.info;
  if (!n.mem) return;
  $('#h-info').textContent = `${info.hostname} · ${info.cpu_model} · ${info.cores} threads · up ${fmtDur(r.uptime)}`;
  H.cpu({ f: n.cpu / 100, text: String(Math.round(n.cpu)), unit: 'PERCENT', labels: ['0', '50', '100'] });
  H.temp({ f: (n.temp || 0) / 100, text: n.temp == null ? '--' : String(Math.round(n.temp)), unit: 'TCTL', labels: ['0', '50', '100'], off: n.temp == null });
  const ghz = (n.freq.avg || 0) / 1000, mx = Math.max(5, Math.ceil((n.freq.rated || 5000) / 1000));
  H.freq({ f: ghz / mx, pf: (n.freq.max_now || 0) / 1000 / mx, text: ghz.toFixed(2), unit: 'AVG · ' + ((n.freq.max_now || 0) / 1000).toFixed(1) + ' MAX', labels: ['0', String(mx / 2), String(mx)] });
  const mu = 1 - n.mem.available / n.mem.total;
  H.mem({ f: mu, text: String(Math.round(mu * 100)), unit: fmtBytes(n.mem.total - n.mem.available) + ' USED', labels: ['0', '50', '100'] });
  const fsm = (m, fs) => m({ f: (fs.total - fs.free) / fs.total, text: String(Math.round(100 * (fs.total - fs.free) / fs.total)), unit: '% used', scale: `${fmtBytes(fs.free)} free · ${fs.path}` });
  fsm(H.mCache, r.fs); fsm(H.mRoot, r.rootfs);
  H.mLoad({ f: n.load[0] / info.cores, text: n.load[0].toFixed(1), unit: `of ${info.cores}`, scale: n.load.map(x => x.toFixed(2)).join(' · ') });
  const sw = n.mem.swap_total ? 1 - n.mem.swap_free / n.mem.swap_total : 0;
  H.mSwap({ f: sw, text: String(Math.round(sw * 100)), unit: '%', scale: fmtBytes(n.mem.swap_total) + ' total' });
  const h = r.hist, t0 = h.length ? h[0].t : 0, span = h.length ? h.at(-1).t - t0 : 900;
  cCpu.set([{ name: 'CPU %', color: C.s[0], pts: h.map(p => [p.t - t0, p.cpu]) }, { name: 'CPU °C', color: C.s[1], pts: h.map(p => [p.t - t0, p.temp]) }], t0, span);
  cNet.set([{ name: 'Sent to players', color: C.s[2], pts: h.map(p => [p.t - t0, p.tx * 8]) }, { name: 'Received', color: C.s[1], pts: h.map(p => [p.t - t0, p.rx * 8]) }], t0, span);
  $('#h-cores').innerHTML = n.cores.map((c, i) => `<div class="core"><span class="k">#${i}</span>${leds(c / 100, 14, 'zones')}<span class="k right">${Math.round(c)}%</span></div>`).join('');
  $('#h-temps').innerHTML = n.temps.map(t => `<div class="well" style="padding:10px 12px"><div class="meter"><div class="mh"><span class="k">${esc(t.chip === 'nvme' && t.dev ? t.dev : t.chip)} · ${esc(t.label)}</span><span class="k">${t.high ? 'max ' + t.high : ''}</span></div>
    <div class="mrow">${leds(t.c / (t.high || 100), 20, 'zones')}<div class="rd">${seg7(String(Math.round(t.c)), 3)}</div><span class="un" style="width:24px">°C</span></div></div></div>`).join('') || '<div class="empty">No temperature sensors found.</div>';
  $('#h-net').innerHTML = `<table><tr><th>Interface</th><th>Link</th><th>Sending</th><th>Receiving</th><th>Link use</th></tr>${info.ifaces.map(i => {
    const v = n.net[i.name] || { rx: 0, tx: 0 }, use = i.speed ? Math.max(v.rx, v.tx) * 8 / (i.speed * 1e6) : null;
    return `<tr><td>${esc(i.name)} <span class="sub">${esc(i.state)}</span></td><td>${i.speed ? (i.speed >= 1000 ? i.speed / 1000 + ' Gb/s' : i.speed + ' Mb/s') : '—'}</td><td>${fmtRate(v.tx)}</td><td>${fmtRate(v.rx)}</td><td>${use == null ? '—' : Math.round(use * 100) + '%'}</td></tr>`;
  }).join('')}</table>`;
  $('#h-disks').innerHTML = `<table><tr><th>Disk</th><th class="l">Model</th><th>Size</th><th>Read</th><th>Write</th><th>Busy</th></tr>${Object.entries(n.disks).filter(([k]) => !k.startsWith('dm-') || info.disks[k]?.model).map(([k, d]) =>
    `<tr><td class="mono">${esc(k)}</td><td class="l">${esc(info.disks[k]?.model || '')}</td><td>${fmtBytes(info.disks[k]?.size)}</td><td>${fmtBytes(d.read)}/s</td><td>${fmtBytes(d.write)}/s</td><td>${Math.round(d.util)}%</td></tr>`).join('')}</table>`;
  if (!OV || !OV.docker) $('#h-ctr').innerHTML = '<div class="empty">Docker is not installed yet.</div>';
  else if (r.containers) $('#h-ctr').innerHTML = r.containers.length ? `<table><tr><th>Container</th><th>CPU</th><th>Memory</th><th>Network</th><th>Disk I/O</th></tr>${r.containers.map(c => `<tr><td>${esc(c.name)}</td><td>${esc(c.cpu)}</td><td>${esc(c.mem)}</td><td>${esc(c.net)}</td><td>${esc(c.block)}</td></tr>`).join('')}</table>` : '<div class="empty">No containers running.</div>';
}

// ================================================================ SERVICE (cache containers)
$('#r-cells').innerHTML = '<i></i>'.repeat(24);
let JOB = null;
async function loadServer(withChecks) {
  const r = await api('GET', '/api/server' + (withChecks ? '?preflight=1' : ''));
  const re = $('#reactor'), ctrs = r.containers || [];
  const up = ctrs.filter(c => c.state === 'running').length;
  const state = !r.docker.usable ? 'locked' : JOB ? 'armed' : up && up === ctrs.length ? 'ready' : up ? 'firing' : '';
  re.className = 'reactor ' + state;
  $('#r-state').textContent = !r.docker.installed ? 'Docker not installed' : !r.docker.usable ? 'Docker not usable' : JOB ? 'working…' : !ctrs.length ? 'Not started yet' : up === ctrs.length ? 'Running' : up ? 'Partly running' : 'Stopped';
  $('#r-count').innerHTML = seg7(`${up}-${ctrs.length}`, 3);
  $$('#r-cells i').forEach((e, i) => { e.className = (up && i < Math.round(24 * up / Math.max(1, ctrs.length))) ? 'on' : (JOB && i === 0 ? 'lead' : ''); });
  $('#srv-state').innerHTML = `<i class="dot ${up ? 'on' : r.docker.usable ? '' : 'err'}"></i>${r.docker.usable ? `Docker ${esc(r.docker.version)}` : 'Docker unavailable'}`;
  $$('#svc-power button').forEach(b => b.disabled = !r.docker.usable || !!JOB);
  $('#ctrs').innerHTML = !r.docker.usable ? `<div class="banner sub">${esc(r.docker.error)}</div>` : ctrs.length ? ctrs.map(c => `<div class="ctr-row"><span class="sled ${c.state === 'running' ? 'ready' : c.state === 'restarting' ? 'busy' : ''}"></span>
      <div><b>${esc(c.service)}</b> <span class="sub">${esc(c.image)}</span><div class="sub">${esc(c.status)}</div></div><span class="k">${esc(c.state)}</span></div>`).join('') : '<div class="empty">No containers yet. Press Start.</div>';
  $('#autostart').checked = r.autostart; $('#autostart').disabled = !isAdmin();
  $('#images').innerHTML = (r.images || []).length ? `<table><tr><th>Image</th><th>Created</th><th>Size</th></tr>${r.images.map(i => `<tr><td>${esc(i.repo)}:${esc(i.tag)}</td><td>${esc(i.created)}</td><td>${esc(i.size)}</td></tr>`).join('')}</table>` : '<div class="empty">Not downloaded yet. Start or Update pulls them.</div>';
  if (r.preflight) $('#checks').innerHTML = r.preflight.map(c => `<div class="check"><span class="sled ${c.ok === true ? 'ready' : c.ok === false ? 'bad' : 'busy'}"></span><div><b>${esc(c.title)}</b><div class="sub">${esc(c.detail)}</div></div></div>`).join('');
  const running = r.jobs.find(j => !j.done);
  if (running && !JOB) followJob(running.id, running.action);
}
$('#pf-run').onclick = () => loadServer(true);
$('#svc-power').addEventListener('click', e => { const b = e.target.closest('[data-act]'); if (b) serverAction(b.dataset.act); });
async function serverAction(act, force) {
  if (act === 'stop' && !await confirmBox('Stop the cache?', 'Players downloading right now will fall back to the internet (or fail if their DNS only points here).', 'Stop')) return;
  try {
    const r = await api('POST', '/api/server/action', { action: act, force: !!force });
    followJob(r.job, act);
  } catch (e) {
    if (e.status === 409 && !force && /Fix these settings/.test(e.message)) {
      if (await confirmBox('Settings have problems', esc(e.message), 'Start anyway')) serverAction(act, true);
    } else toast(e.message, 'bad');
  }
}
async function followJob(id, act, outSel = '#job-out', titleSel = '#job-title', after) {
  JOB = id;
  const out = $(outSel); out.innerHTML = ''; out.hidden = false;
  const title = $(titleSel) || { set textContent(v) {} };
  title.textContent = act + ' · running';
  let since = 0, lastNew = Date.now();
  const t0 = Date.now(), beat = document.createElement('div');
  beat.className = 'note';
  while (true) {
    let j;
    try { j = await api('GET', `/api/server/job/${id}?since=${since}`); } catch (e) { break; }
    if (j.out) { beat.remove(); out.insertAdjacentHTML('beforeend', j.out.split('\n').filter(Boolean).map(l => `<div class="${lineClass(l)}">${esc(l)}</div>`).join('')); out.scrollTop = 1e9; lastNew = Date.now(); }
    since = j.lines;
    // Some steps (formatting, removing a volume) print nothing for a while: show that it's still alive.
    if (!j.done && Date.now() - lastNew > 4000) { beat.textContent = `still working… ${Math.round((Date.now() - t0) / 1000)} s (no new output for ${Math.round((Date.now() - lastNew) / 1000)} s; some disk steps take a few minutes)`; out.appendChild(beat); out.scrollTop = 1e9; }
    if (j.done) beat.remove();
    if (j.done) { title.textContent = `${act} · ${j.code === 0 ? 'done' : 'failed (exit ' + j.code + ')'}`; toast(`${act}: ${j.code === 0 ? 'done' : 'failed'}`, j.code === 0 ? 'good' : 'bad'); break; }
    await new Promise(r => setTimeout(r, 700));
  }
  JOB = null;
  if (TAB === 'service') loadServer(true);
  if (after) after();
}
$('#autostart').onchange = async e => {
  try { const r = await api('POST', '/api/server/autostart', { on: e.target.checked }); toast(r.note); } catch (err) { toast(err.message, 'bad'); e.target.checked = !e.target.checked; }
};
$('#dns-run').onclick = async () => {
  $('#dns-out').innerHTML = '<div class="empty">Testing…</div>';
  try {
    const n = $('#dns-name').value.trim();
    const r = await api('GET', '/api/tools/dns' + (n ? '?name=' + encodeURIComponent(n) : ''));
    $('#dns-out').innerHTML = `<p class="sub">DNS server ${esc(r.server)} · cache IPs ${esc(r.cache_ips.join(', '))} · ${esc(r.method)}</p><table><tr><th class="l">Name</th><th class="l">Service</th><th class="l">Answer</th><th>Result</th></tr>${r.results.map(x =>
      `<tr class="dnsrow"><td class="l">${esc(x.name)}</td><td class="l">${esc(x.service)}</td><td class="l mono">${esc(x.error || x.ips.join(', ') || '—')}</td><td><span class="src ${x.ok ? 'cache' : 'internet'}"><i></i>${x.ok ? 'OK' : 'PROBLEM'}</span></td></tr>`).join('')}</table>`;
  } catch (e) { $('#dns-out').innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};

// ================================================================ PRELOAD
const PF = { n: 0, running: false, tick: 0, st: null };
$('#pf-cells').innerHTML = '<i></i>'.repeat(24);
$('#pf-lamps').innerHTML = [['tool', 'TOOL'], ['steam', 'STEAM'], ['cache', 'CACHE'], ['upd', 'UPD'], ['run', 'RUN']].map(l => `<div class="lamp2" id="pl-${l[0]}"><i></i><span>${l[1]}</span></div>`).join('');
const plamp = (id, st, blink) => { $('#pl-' + id).className = 'lamp2' + (st ? ' on ' + (st === true ? '' : st) : '') + (blink ? ' blink' : ''); };
async function loadPreload(first) {
  let r;
  try { r = await api('GET', '/api/prefill?since=' + (first === true ? 0 : PF.n)); } catch (e) { return; }
  PF.st = r;
  const j = r.job, running = !!(j && j.running);
  const cacheUp = OV && OV.containers.some(c => c.service === 'monolithic' && c.state === 'running');
  plamp('tool', r.installed); plamp('steam', r.logged_in); plamp('cache', cacheUp ? true : 'crit');
  plamp('upd', r.update_available.length ? 'warn' : false, r.update_available.length > 0); plamp('run', running ? 'crit' : false, running);
  const names = r.apps.map(a => (r.latest[a] && r.latest[a].name) || (a === 730 ? 'Counter-Strike 2' : 'app ' + a));
  $('#pf-go span').textContent = r.apps.length === 1 ? `Preload ${names[0] === 'Counter-Strike 2' ? 'CS2' : names[0]}` : `Preload ${r.apps.length} games`;
  $('#pf-apps').innerHTML = r.apps.map((a, i) => {
    const l = r.latest[a] || {}, have = r.builds[a], upd = r.update_available.includes(a);
    const st = !l.buildid ? 'idle' : upd ? 'internet' : 'cache', txt = !l.buildid ? (have ? 'PRELOADED' : 'UNKNOWN') : upd ? (have ? 'UPDATE OUT' : 'NOT PRELOADED') : 'UP TO DATE';
    return `<div class="pfapp"><span class="sled ${st}"></span>
      <div class="gm">${coverImg(a)}<div><b>${esc(names[i])}</b><span class="k">app ${a}</span></div></div>
      <div class="who"><b class="num">Steam build ${esc(l.buildid || '?')}</b><span class="k">${l.timeupdated ? 'released ' + fmtAgo(l.timeupdated) : l.error ? 'offline: ' + esc(l.error.slice(0, 60)) : 'not checked yet'} · cache has ${esc(have || 'nothing yet')}</span></div>
      <span class="src ${st}"><i></i>${txt}</span></div>`;
  }).join('');
  const re = $('#pf-reactor');
  re.className = 'reactor ' + (running ? '' : r.last_run && !r.last_run.ok ? 'firing' : r.update_available.length ? 'armed' : r.last_run && r.last_run.ok ? 'ready' : '');
  const tr = r.traffic || { rate: 0, hit: 0, miss: 0 };
  const flowing = running && tr.rate > 0;
  const pct = pg && pg.fraction != null ? Math.floor(pg.fraction * 100) : null;
  $('#pf-rstate').textContent = running ? (j.phone ? 'approve the sign-in on your phone' : j.prompt ? 'waiting for you: see below'
    : pct != null ? `${pct}% · ${fmtBytes(pg.done)} of ${fmtBytes(pg.total)}${flowing ? ' · ' + fmtRate(tr.rate) : ''}`
    : (pg && pg.stage && pg.stage !== 'starting' ? pg.stage + '…' : 'starting…')) : r.update_available.length ? 'new build available' : r.last_run ? (r.last_run.ok ? 'cache up to date' : 'last preload failed') : 'ready';
  const since = running ? Date.now() / 1000 - j.started : r.last_run ? Date.now() / 1000 - r.last_run.t : null;
  $('#pf-clock').innerHTML = since == null ? '' : seg7(running ? String(Math.floor(since / 60)).padStart(2, '0') + ':' + String(Math.floor(since % 60)).padStart(2, '0') : '--:--', 4);
  // LED bar: real progress of the current download (dark until SteamPrefill reports the size); full when up to date.
  const pg = j && j.progress, frac = running ? (pg && pg.fraction != null ? pg.fraction : 0) : (r.installed && r.last_run && r.last_run.ok && !r.update_available.length ? 1 : 0);
  const lit = Math.round(frac * 24);
  $$('#pf-cells i').forEach((e, i) => e.className = i < lit ? 'on' : '');
  $('#pf-go').disabled = running; $('#pf-force').disabled = running; $('#pf-stop').disabled = !running;
  $('#pf-state').innerHTML = `<i class="dot ${running ? 'live' : r.last_run && !r.last_run.ok ? 'err' : r.last_run ? 'on' : ''}"></i>${running ? 'running' : r.last_run ? 'last run ' + fmtAgo(r.last_run.t) : 'never run'}`;
  $('#pf-setup').innerHTML = [
    [r.installed ? true : null, 'SteamPrefill downloaded', r.installed ? `Version ${r.version || '?'}` : 'Not yet. Preload CS2 downloads it automatically the first time (needs internet).'],
    [r.logged_in ? true : null, 'Steam account', r.logged_in ? 'Logged in (saved by SteamPrefill)' : 'Not logged in yet. The first preload asks for it.'],
    [cacheUp ? true : false, 'Cache running', cacheUp ? 'Preloads go into the cache' : 'Start the cache on the Service tab first'],
  ].map(([ok, t, d]) => `<div class="check"><span class="sled ${ok === true ? 'ready' : ok === false ? 'bad' : 'busy'}"></span><div><b>${t}</b><div class="sub">${esc(d)}</div></div></div>`).join('');
  $('#pf-install').textContent = r.installed ? 'Update SteamPrefill' : 'Download SteamPrefill';
  if (first === true || document.activeElement?.closest('#pf-prompt, .form') == null) {
    $('#pf-auto').checked = r.auto; $('#pf-apps-in').value = r.apps.join(', ');
    $$('.pf-os').forEach(c => c.checked = r.os.split(',').includes(c.value));
  }
  ['#pf-auto', '#pf-apps-in', ...$$('.pf-os')].forEach(x => (typeof x === 'string' ? $(x) : x).disabled = !isAdmin());
  if (j) {
    const out = $('#pf-out');
    if (first === true) out.innerHTML = '';
    if (out.firstChild && out.firstChild.classList.contains('note')) out.innerHTML = '';
    if (j.lines.length) out.insertAdjacentHTML('beforeend', j.lines.map(l => `<div class="${lineClass(l)}">${esc(l)}</div>`).join(''));
    PF.n = j.n;
    let live = $('#pf-live', out);
    if (!live) { out.insertAdjacentHTML('beforeend', '<div id="pf-live" class="note"></div>'); live = $('#pf-live', out); }
    out.appendChild(live); live.textContent = running ? (j.partial || (flowing ? `Downloading through the cache at ${fmtRate(tr.rate)} (SteamPrefill shows its own progress when it finishes a step)` : '')) : '';
    out.scrollTop = 1e9;
    $('#pf-jobline').textContent = `${j.force ? 'forced · ' : ''}${j.auto ? 'automatic · ' : ''}${running ? 'running' : 'exit ' + j.code}`;
    $('#pf-phone').hidden = !(running && j.phone);
    const pr = $('#pf-prompt');
    if (j.prompt && isAdmin()) {
      if (pr.hidden || $('#pf-prompt-text').textContent !== j.prompt) {
        pr.hidden = false; $('#pf-prompt-text').textContent = j.prompt; const a = $('#pf-answer');
        a.type = j.secret ? 'password' : 'text'; a.value = ''; a.focus();
      }
    } else pr.hidden = true;
  }
  if (PF.running && !running && j) toast(`Preload ${r.last_run && r.last_run.ok ? 'finished' : 'ended with a problem; see the output'}`, r.last_run && r.last_run.ok ? 'good' : 'bad');
  PF.running = running;
}
async function pfStart(force) {
  if (force && !await confirmBox('Force a full download?', 'Downloads every file of the game again even if the cache has it. Only needed if the cache was wiped.', 'Download everything')) return;
  let login = {};
  if (!PF.st || !PF.st.logged_in) {
    // SteamPrefill only waits 30 s for each of these, so collect them first and the server answers at once.
    const f = await ask({ title: 'Log in to Steam for the preload', ok: 'Log in and preload', html: `
      <p class="sub" style="margin:0">Use an account that has CS2 in its library. This is only needed once: SteamPrefill remembers the login. The password is passed straight to SteamPrefill and never saved.</p>
      <label class="f">Steam account name<input type="text" name="u" autocomplete="username" required></label>
      <label class="f">Password<input type="password" name="p" autocomplete="current-password" required></label>
      <p class="sub" style="margin:0">Steam may then ask you to approve the sign-in in the Steam app on your phone, or for a Steam Guard code. Watch this page.</p>` });
    if (!f) return;
    login = { steam_user: f.get('u'), steam_password: f.get('p') };
  }
  if (!PF.st || !PF.st.installed) { toast('Downloading SteamPrefill first…'); $('#pf-go').disabled = true; }
  try { await api('POST', '/api/prefill/start', { force, ...login }); PF.n = 0; await loadPreload(true); }
  catch (e) {
    if (e.status === 409 && /isn't running/.test(e.message) && await confirmBox('Cache is not running', esc(e.message), 'Run anyway')) {
      try { await api('POST', '/api/prefill/start', { force, anyway: true, ...login }); await loadPreload(true); } catch (e2) { toast(e2.message, 'bad'); }
    } else toast(e.message, 'bad');
  }
}
$('#pf-go').onclick = () => pfStart(false);
$('#pf-force').onclick = () => pfStart(true);
$('#pf-stop').onclick = async () => { await api('POST', '/api/prefill/stop', {}); toast('Stopping…'); };
$('#pf-check').onclick = async () => { try { await api('POST', '/api/prefill/check', {}); loadPreload(); toast('Checked Steam for new builds'); } catch (e) { toast(e.message, 'bad'); } };
$('#pf-install').onclick = async () => {
  const b = $('#pf-install'); b.disabled = true; b.textContent = 'Downloading…';
  try { const r = await api('POST', '/api/prefill/install', {}); toast('SteamPrefill ' + r.version + ' ready', 'good'); } catch (e) { toast(e.message, 'bad'); }
  b.disabled = false; loadPreload(true);
};
$('#pf-prompt').addEventListener('submit', async e => {
  e.preventDefault();
  try { await api('POST', '/api/prefill/input', { text: $('#pf-answer').value }); $('#pf-answer').value = ''; $('#pf-prompt').hidden = true; loadPreload(); } catch (err) { toast(err.message, 'bad'); }
});
$('#pf-save').onclick = async () => {
  try {
    await api('POST', '/api/prefill/settings', { auto: $('#pf-auto').checked, apps: $('#pf-apps-in').value, os: $$('.pf-os').filter(c => c.checked).map(c => c.value).join(',') });
    toast('Saved', 'good'); loadPreload(true);
  } catch (e) { toast(e.message, 'bad'); }
};

// ================================================================ CONFIG
const CFG = { data: null, mode: 'form', edits: {}, raw: {} };
async function loadConfig() {
  const r = await api('GET', '/api/config');
  CFG.data = r; CFG.edits = {}; CFG.raw = { env: r.raw.env, compose: r.raw.compose };
  $('#env-path').textContent = r.paths.env;
  $('#host-ips').innerHTML = r.host_ips.map(a => `<button class="ipchip admin-only" data-ip="${esc(a.ip)}" title="${esc(a.iface)}${a.dynamic ? ' · from DHCP' : ' · static'}">${esc(a.ip)}/${a.prefix} <span class="sub">${esc(a.iface)}${a.dynamic ? ' · DHCP' : ''}</span></button>`).join('') +
    (isAdmin() ? '' : r.host_ips.map(a => `<code>${esc(a.ip)}</code>`).join(' '));
  $('#profiles').innerHTML = r.profiles.length ? r.profiles.map(p => `<div class="prof"><div><b>${esc(p.name)}</b><span class="k">${esc(p.summary.LANCACHE_IP || '')} · ${esc(p.summary.CACHE_ROOT || '')} · ${esc(p.summary.CACHE_DISK_SIZE || '')}</span><span class="k">saved ${fmtWhen(p.time)}</span></div>
     <div class="hrow admin-only"><button class="btn" data-prof="load" data-name="${esc(p.name)}">Load</button><button class="btn ghost" data-prof="delete" data-name="${esc(p.name)}">✕</button></div></div>`).join('') : '<div class="empty">No profiles saved yet.</div>';
  $('#backups').innerHTML = r.backups.length ? `<table>${r.backups.map(b => `<tr><td class="l mono" style="font-size:11.5px">${esc(b.name)}</td><td><button class="btn ghost" data-bk="view" data-name="${esc(b.name)}">View</button><button class="btn ghost admin-only" data-bk="restore" data-name="${esc(b.name)}">Restore</button></td></tr>`).join('')}</table>` : '<div class="empty">None yet.</div>';
  $('#set-event').value = r.settings.event_name; $('#set-window').value = r.settings.active_window;
  ['#set-event', '#set-window'].forEach(s => $(s).disabled = !isAdmin());
  $('#paths').innerHTML = `lancache: <code>${esc(r.paths.lancache)}</code> · cache data: <code>${esc(r.paths.cache_root)}</code> · GUI data: <code>${esc(r.paths.data)}</code>`;
  renderCfg();
  loadStorage();
}
function issuesFor(key, issues) { return (issues || CFG.data.issues).filter(i => i.key === key); }
function renderCfg(issues) {
  const d = CFG.data, ro = !isAdmin();
  $('#cfg-form').hidden = CFG.mode !== 'form'; $('#cfg-raw').hidden = CFG.mode === 'form';
  if (CFG.mode === 'form') {
    const q = $('#cfg-search').value.toLowerCase();
    $('#env-fields').innerHTML = d.env.filter(e => !q || (e.key + ' ' + e.help).toLowerCase().includes(q)).map(e => {
      const v = e.key in CFG.edits ? CFG.edits[e.key] : e.value;
      const ip = /_IP$/.test(e.key) && !ro ? `<button class="btn" data-useip="${e.key}" title="Use this machine's current address">This machine</button>` : '';
      return `<div class="envf"><div class="key${e.key in CFG.edits ? ' changed' : ''}">${esc(e.key)}</div>
        <div><div class="inrow"><input type="text" data-key="${esc(e.key)}" value="${esc(v)}" ${ro ? 'disabled' : ''} spellcheck="false">${ip}</div>
        ${issuesFor(e.key, issues).map(i => `<div class="iss ${i.level}">${i.level === 'error' ? '✕' : '!'} ${esc(i.msg)}</div>`).join('')}
        ${e.help ? `<div class="help">${esc(e.help)}</div>` : ''}</div></div>`;
    }).join('');
  } else {
    const t = $('#raw-text'); t.value = CFG.raw[CFG.mode]; t.disabled = ro;
  }
  cfgDirty();
}
function cfgDirty() {
  const d = CFG.data;
  const dirty = CFG.mode === 'form' ? Object.keys(CFG.edits).length > 0 : CFG.raw[CFG.mode] !== d.raw[CFG.mode];
  ['#cfg-save', '#cfg-save-restart', '#cfg-revert'].forEach(s => $(s).disabled = !dirty);
  $('#cfg-msg').textContent = dirty ? 'Unsaved changes' : '';
  return dirty;
}
let _vt;
$('#env-fields').addEventListener('input', e => {
  const k = e.target.dataset.key; if (!k) return;
  const orig = CFG.data.env.find(x => x.key === k).value;
  if (e.target.value === orig) delete CFG.edits[k]; else CFG.edits[k] = e.target.value;
  e.target.closest('.envf').querySelector('.key').classList.toggle('changed', k in CFG.edits);
  cfgDirty();
  clearTimeout(_vt);
  _vt = setTimeout(async () => {
    const r = await api('POST', '/api/config/validate', { values: CFG.edits });
    const focus = document.activeElement?.dataset?.key, pos = document.activeElement?.selectionStart;
    renderCfg(r.issues);
    if (focus) { const el = $(`[data-key="${focus}"]`); if (el) { el.focus(); el.setSelectionRange(pos, pos); } }
  }, 500);
});
$('#cfg-search').oninput = () => renderCfg();
$('#raw-text').oninput = e => { CFG.raw[CFG.mode] = e.target.value; cfgDirty(); };
$('#cfg-mode').onclick = e => {
  const b = e.target.closest('button'); if (!b) return;
  $$('#cfg-mode button').forEach(x => x.classList.toggle('act', x === b)); CFG.mode = b.dataset.m; renderCfg();
};
function setIp(ip) {
  for (const k of ['LANCACHE_IP', 'DNS_BIND_IP']) if (CFG.data.env.some(e => e.key === k)) { if (CFG.data.env.find(e => e.key === k).value === ip) delete CFG.edits[k]; else CFG.edits[k] = ip; }
  CFG.mode = 'form'; $$('#cfg-mode button').forEach(x => x.classList.toggle('act', x.dataset.m === 'form'));
  renderCfg(); $('#env-fields input')?.dispatchEvent(new Event('input', { bubbles: true }));
  toast(`LANCACHE_IP and DNS_BIND_IP set to ${ip}. Press Save.`);
}
document.addEventListener('click', e => {
  const c = e.target.closest('[data-ip]'); if (c) return setIp(c.dataset.ip);
  const u = e.target.closest('[data-useip]');
  if (u) { const ips = CFG.data.host_ips; if (ips.length) { const inp = $(`[data-key="${u.dataset.useip}"]`); inp.value = ips[0].ip; inp.dispatchEvent(new Event('input', { bubbles: true })); } }
});
async function saveCfg() {
  try {
    if (CFG.mode === 'form') {
      const r = await api('POST', '/api/config/env', { values: CFG.edits });
      toast('Saved .env (backup ' + r.backup + ')', 'good');
    } else {
      const r = await api('POST', '/api/config/raw', { file: CFG.mode, content: CFG.raw[CFG.mode] });
      toast('Saved (backup ' + r.backup + ')', 'good');
      if (r.compose_check && !r.compose_check.ok) toast('docker compose found a problem: ' + r.compose_check.out, 'bad');
    }
    const mode = CFG.mode; await loadConfig(); CFG.mode = mode; renderCfg();
    return true;
  } catch (e) { toast(e.message, 'bad'); return false; }
}
$('#cfg-save').onclick = saveCfg;
$('#cfg-save-restart').onclick = async () => { if (await saveCfg()) { showTab('service'); serverAction('restart'); } };
$('#cfg-revert').onclick = () => { CFG.edits = {}; CFG.raw = { env: CFG.data.raw.env, compose: CFG.data.raw.compose }; renderCfg(); };
$('#env-add').onclick = async () => {
  const f = await ask({ title: 'Add a setting', ok: 'Add', html: `<label class="f">Name<input type="text" name="k" pattern="[A-Za-z_][A-Za-z0-9_]*" required placeholder="e.g. CACHE_SLICE_SIZE"></label><label class="f">Value<input type="text" name="v"></label>
    <p class="sub" style="margin:0">See the lancache docs for the options the containers understand.</p>` });
  if (!f) return;
  try { await api('POST', '/api/config/env', { values: { [f.get('k')]: f.get('v') } }); loadConfig(); toast('Added', 'good'); } catch (e) { toast(e.message, 'bad'); }
};
document.addEventListener('click', async e => {
  const p = e.target.closest('[data-prof]');
  if (p) {
    const act = p.dataset.prof, name = p.dataset.name;
    if (act === 'load' && !await confirmBox(`Load "${name}"?`, 'This replaces the current .env (a backup is made first). Restart the cache afterwards.', 'Load')) return;
    if (act === 'delete' && !await confirmBox(`Delete profile "${name}"?`, 'The current .env is not changed.', 'Delete')) return;
    try { await api('POST', '/api/config/profile', { action: act, name }); toast(act === 'load' ? 'Loaded. Restart the cache to apply.' : 'Deleted', 'good'); loadConfig(); } catch (err) { toast(err.message, 'bad'); }
  }
  const b = e.target.closest('[data-bk]');
  if (b) {
    if (b.dataset.bk === 'view') {
      const r = await api('GET', '/api/config/backup?name=' + encodeURIComponent(b.dataset.name));
      ask({ title: r.name, ok: false, wide: true, html: `<textarea rows="22" readonly>${esc(r.content)}</textarea>` });
    } else if (await confirmBox('Restore this backup?', `${esc(b.dataset.name)} replaces the current file (which is backed up first).`, 'Restore')) {
      try { await api('POST', '/api/config/restore', { name: b.dataset.name }); toast('Restored', 'good'); loadConfig(); } catch (err) { toast(err.message, 'bad'); }
    }
  }
});
$('#prof-save').onclick = async () => {
  const name = $('#prof-name').value.trim(); if (!name) return toast('Give the profile a name', 'bad');
  if (cfgDirty()) toast('Note: unsaved changes are not in the profile. Save first.', 'bad');
  try { await api('POST', '/api/config/profile', { action: 'save', name }); $('#prof-name').value = ''; toast('Profile saved', 'good'); loadConfig(); } catch (e) { toast(e.message, 'bad'); }
};
$('#set-save').onclick = async () => {
  try { await api('POST', '/api/settings', { event_name: $('#set-event').value, active_window: +$('#set-window').value }); toast('Saved', 'good'); } catch (e) { toast(e.message, 'bad'); }
};
$('#cert-regen').onclick = async () => {
  if (!await confirmBox('Make a new HTTPS certificate?', 'Do this after the machine gets a new IP address. Browsers will warn once again; the GUI must be restarted to use it.', 'Make certificate')) return;
  try { const r = await api('POST', '/api/cert/regenerate', {}); toast(r.note, 'good'); } catch (e) { toast(e.message, 'bad'); }
};
$('#stats-reset').onclick = async () => {
  if (!await confirmBox('Reset all statistics?', 'Deletes all download history (clients, games, totals). The cached game data itself is not touched. Useful between events.', 'Reset')) return;
  try { await api('POST', '/api/stats/reset', {}); toast('Statistics cleared', 'good'); } catch (e) { toast(e.message, 'bad'); }
};
window.addEventListener('beforeunload', e => { if (CFG.data && TAB === 'config' && cfgDirty()) e.preventDefault(); });

// ================================================================ STORAGE
const ST = { data: null, sel: new Set(), drop: new Set() };   // sel: new disks ticked · drop: in-cache disks unticked
const STATUS_BADGE = { cache: ['cache', 'IN CACHE'], system: ['idle', 'SYSTEM'], mounted: ['mixed', 'MOUNTED'], busy: ['idle', 'IN USE'], data: ['internet', 'HAS DATA'], empty: ['blue', 'EMPTY'] };
async function loadStorage() {
  let r;
  try { r = await api('GET', '/api/storage'); } catch (e) { return; }
  ST.data = r;
  const c = r.cache, sc = r.scan;
  $('#st-where').innerHTML = `<b>Cache folder now:</b> <code>${esc(c.root)}</code> on <code>${esc(c.source || '?')}</code> (${esc(c.fstype || '?')}, mounted at <code>${esc(c.mount || '?')}</code>) ·
    ${fmtBytes(c.fs.free)} free of ${fmtBytes(c.fs.total)}${c.on_system_disk ? ' · <span style="color:#fad06c">on the system disk</span>' : ''}`;
  const hb = $('#st-helper');
  hb.hidden = r.helper.ok || !isAdmin(); hb.innerHTML = r.helper.message ? `⚠ ${esc(r.helper.message)}` : '';
  renderSysPool(sc, r.helper);
  if (sc.error) { $('#st-disks').innerHTML = `<div class="empty">${esc(sc.error)}</div>`; return; }
  ST.sel.forEach(p => { if (!sc.disks.some(d => d.path === p && d.erasable)) ST.sel.delete(p); });
  ST.drop.forEach(p => { if (!sc.disks.some(d => d.path === p && d.status === 'cache')) ST.drop.delete(p); });
  $('#st-disks').innerHTML = sc.disks.map(d => {
    const [cls, txt] = STATUS_BADGE[d.status] || ['idle', d.status.toUpperCase()];
    const sb = splitBytes(d.size);
    const parts = d.parts.filter(p => p.type === 'part' || p.type === 'lvm').map(p => `${esc(p.name)} ${fmtBytes(p.size)}${p.fstype ? ' ' + esc(p.fstype) : ''}${p.label ? ' “' + esc(p.label) + '”' : ''}${p.mountpoints.length ? ' → ' + esc(p.mountpoints.join(', ')) : ''}`);
    const adopt = isAdmin() && !sc.volume_exists ? d.adoptable.map(p => `<button class="btn ghost" data-adopt="${esc(p.path)}" title="Mount it at /srv/lancache without erasing">Use ${esc(p.name)} as-is</button>`).join('') : '';
    const folder = isAdmin() && d.status === 'mounted' ? d.mountpoints.filter(m => m.startsWith('/') && m !== '/').map(m => `<button class="btn ghost" data-stfolder="${esc(m.replace(/\/$/, '') + '/lancache')}">Use folder ${esc(m)}/lancache</button>`).join('') : '';
    const inCache = d.status === 'cache' && sc.cache_kind === 'disks', ticked = inCache ? !ST.drop.has(d.path) : ST.sel.has(d.path);
    return `<div class="disk${ST.sel.has(d.path) ? ' sel' : ''}${inCache && !ticked ? ' leaving' : ''}">
      <input type="checkbox" data-disk="${esc(d.path)}" data-incache="${inCache ? 1 : ''}" ${ticked ? 'checked' : ''} ${(d.erasable || inCache) && isAdmin() ? '' : 'disabled'} title="${inCache ? 'In the cache volume. Untick to remove the volume or move the cache.' : d.erasable ? 'Select' : esc(d.why)}">
      <div><b>${esc(d.name)}</b> <span class="sub">${esc(d.kind)}</span><span class="k">${esc(d.model || 'unknown model')}</span></div>
      <div class="size">${seg7(sb[0], 4)}<span class="k">${sb[1]}</span></div>
      <div class="what">${esc(d.why)}${parts.length ? `<span class="k">${parts.join(' · ')}</span>` : ''}${adopt || folder ? `<div class="hrow" style="margin-top:4px">${adopt}${folder}</div>` : ''}</div>
      <span class="src ${cls}"><i></i>${txt}</span></div>`;
  }).join('');
  stSel();
}
function renderSysPool(sc, helper) {
  const box = $('#st-sys'), L = sc.lvm;
  if (!L || !L.system_vg) {
    box.innerHTML = `<div class="k">System disk</div><p class="sub" style="margin:6px 0 0">${L && L.why ? esc(L.why) :
      'The free space on the system disk shows here once the disk helper is up to date: run <code>sudo ./webgui/install.sh</code>.'}</p>`;
    return;
  }
  const vg = L.system_vg, cache = L.cache_lv ? L.cache_lv.size : 0, root = L.root.size, other = Math.max(0, vg.size - vg.free - root - cache);
  const pc = v => (100 * v / vg.size).toFixed(2) + '%', usable = Math.max(0, vg.free - L.reserve);
  const onSys = sc.cache_kind === 'system', canSys = !sc.volume_exists && usable >= 10 * 2 ** 30;
  box.innerHTML = `<div class="hrow"><span class="k">System disk pool · ${esc(vg.name)}</span><span class="spacer"></span><span class="sub">${fmtBytes(vg.free)} unused of ${fmtBytes(vg.size)}</span></div>
    <div class="sysbar"><span class="root" style="width:${pc(root + other)}"></span><span class="cache" style="width:${pc(cache)}"></span><span class="rsv" style="width:${pc(Math.min(L.reserve, vg.free))}"></span></div>
    <div class="syskey"><span><i style="background:#7fb2f0"></i>System volume (/) ${fmtBytes(root)} · ${fmtBytes(L.root.fs_free)} free inside</span>
      ${cache ? `<span><i style="background:#ff2bd6"></i>Cache volume ${fmtBytes(cache)}</span>` : ''}
      <span><i style="background:#3a3a37"></i>Unused ${fmtBytes(vg.free)} (${fmtBytes(L.reserve)} always kept back)</span></div>
    <div class="neons admin-only">
      ${onSys ? `<button class="neon pink" data-sysact="grow-cache" ${vg.free >= 2 ** 30 ? '' : 'disabled'}><b>▲</b>Grow cache volume</button><button class="neon" style="--n:#ff3b3b" data-sysact="remove"><b>✕</b>Remove cache volume</button>`
              : `<button class="neon pink" data-sysact="sysvol" ${canSys ? '' : 'disabled'} title="${sc.volume_exists ? 'A cache volume already exists' : ''}"><b>▼</b>Use free space for the cache</button>`}
      <button class="neon green" data-sysact="grow-root" ${vg.free >= 2 ** 30 ? '' : 'disabled'}><b>▲</b>Grow system volume</button>
    </div>
    <p class="sub" style="margin:10px 0 0">Neither button changes partitions: they only hand out unused space in this pool. Volumes can grow later but not shrink, so take what you need.</p>`;
}
async function sysAction(kind) {
  const L = ST.data.scan.lvm, vg = L.system_vg, G = 2 ** 30;
  const isSys = kind === 'sysvol', target = kind === 'grow-root' ? 'root' : kind === 'grow-cache' ? 'cache' : null;
  const max = Math.floor((isSys ? vg.free - L.reserve : vg.free) / G);
  const def = isSys ? Math.max(10, Math.min(max, max - 100)) : kind === 'grow-root' ? Math.min(max, 150) : Math.min(max, 500);
  const color = kind === 'grow-root' ? '#39ff14' : '#ff2bd6';
  const title = isSys ? 'Make a cache volume from free space on the system disk' : kind === 'grow-root' ? 'Grow the system volume (/)' : 'Grow the cache volume';
  const action = isSys ? 'sysvol' : 'grow';
  const getPlan = (size, fs) => api('POST', '/api/storage/plan', { action, size, fs: fs || 'ext4', target });
  let p;
  try { p = await getPlan(def); } catch (e) { return toast(e.message, 'bad'); }
  const prom = ask({ title, ok: 'Apply', wide: true, html: `
    <div class="f" style="--n:${color}"><span>${isSys ? 'Size of the cache volume' : 'Add this much'} (GiB, up to ${max})</span>
      <div class="sizepick"><input type="range" id="sp-r" min="${isSys ? 10 : 1}" max="${max}" value="${def}"><input type="number" name="size" id="sp-n" min="${isSys ? 10 : 1}" max="${max}" value="${def}"></div></div>
    ${isSys ? `<label class="f">Format as<select name="fs" id="sp-fs"><option value="ext4">ext4 (recommended)</option><option value="xfs">xfs</option></select></label>` : ''}
    <div id="sp-plan" class="form">${sysPlanHtml(p, kind)}</div>
    <label class="f">To confirm, type: <b>${esc(p.confirm)}</b><input type="text" name="confirm" autocomplete="off" required></label>` });
  let t;
  const refresh = () => { clearTimeout(t); t = setTimeout(async () => {
    try { $('#sp-plan').innerHTML = sysPlanHtml(await getPlan(+$('#sp-n').value, $('#sp-fs') && $('#sp-fs').value), kind); }
    catch (e) { $('#sp-plan').innerHTML = `<div class="erase-warn">${esc(e.message)}</div>`; } }, 250); };
  $('#sp-r').oninput = () => { $('#sp-n').value = $('#sp-r').value; refresh(); };
  $('#sp-n').oninput = () => { $('#sp-r').value = $('#sp-n').value; refresh(); };
  if ($('#sp-fs')) $('#sp-fs').onchange = refresh;
  const f = await prom;
  if (!f) return;
  try {
    const r = await api('POST', '/api/storage/apply', { action, size: +f.get('size'), fs: f.get('fs') || 'ext4', target, confirm: f.get('confirm') });
    followJob(r.job, title.toLowerCase(), '#st-out', null, () => { loadStorage(); loadConfig(); });
  } catch (e) { toast(e.message, 'bad'); }
}
function sysPlanHtml(p, kind) {
  const G = 2 ** 30;
  return `<div class="layout-box">${kind === 'sysvol' ? `New cache volume   ${fmtBytes(p.size)}` : kind === 'grow-root' ? `System volume (/)  → ${fmtBytes(p.size)}` : `Cache volume       → ${fmtBytes(p.size)}`}
Unused in the pool ${fmtBytes(p.free_before)} → ${fmtBytes(p.free_after)}</div>
    ${p.layout.length ? `<div><div class="k" style="margin-bottom:6px">Folder layout</div><div class="layout-box">${p.layout.map(([a, b]) => `${esc(a.padEnd(22))}<span class="c"># ${esc(b)}</span>`).join('\n')}</div></div>` : ''}
    ${p.env_preview ? `<div><div class="k" style="margin-bottom:6px">.env will be set to</div><div class="layout-box">${Object.entries(p.env_preview).map(([k, v]) => `${esc(k)}=${esc(v)}`).join('\n')}</div></div>` : ''}
    <p class="sub" style="margin:0">${kind === 'grow-root' ? 'Happens while running; nothing restarts.' : 'Nothing existing is erased. If the cache is running it restarts afterwards to use the new space.'}</p>
    <details><summary class="sub" style="cursor:pointer">Commands that will run (${p.steps.length})</summary><ol class="plan">${p.steps.map(s => `<li><span class="d">${esc(s.desc)}</span>${s.cmd ? '<br>' + esc(s.cmd.join(' ')) : ''}</li>`).join('')}</ol></details>`;
}
document.addEventListener('click', e => { const b = e.target.closest('[data-sysact]'); if (b && !b.disabled) b.dataset.sysact === 'remove' ? stApply('remove', []) : sysAction(b.dataset.sysact); });
function stMode() {
  const sc = ST.data.scan, dropping = sc.cache_kind === 'disks' && ST.drop.size > 0;
  return dropping ? (ST.sel.size ? 'move' : 'remove') : sc.volume_exists ? 'add' : 'create';
}
function stSel() {
  const sc = ST.data.scan, n = ST.sel.size, size = sc.disks.filter(d => ST.sel.has(d.path)).reduce((a, d) => a + d.size, 0);
  const b = $('#st-apply'), mode = stMode(), names = sc.disks.filter(d => ST.sel.has(d.path)).map(d => d.name).join(' + ');
  const rm = $('#st-remove');
  rm.hidden = !(sc.volume_exists || sc.mounted) || mode === 'remove' || mode === 'move';
  rm.disabled = !ST.data.helper.ok || !!JOB;
  b.classList.toggle('danger', mode === 'remove' || mode === 'move');
  if (mode === 'move' || mode === 'remove') {
    b.disabled = !ST.data.helper.ok || !!JOB;
    b.textContent = mode === 'move' ? `Move the cache to ${names}` : 'Remove the cache volume';
    $('#st-sel').textContent = (mode === 'move' ? `The current cache volume is deleted and a new one built on ${names} (${fmtBytes(size)}, erased).` : 'Deletes the cache volume and everything cached on it; its disks become empty.') +
      (sc.disks.some(d => d.status === 'cache' && !ST.drop.has(d.path)) ? ' The whole volume goes: a joined volume can\'t drop just one disk.' : '');
    if (!ST.data.helper.ok) $('#st-sel').textContent = 'Run sudo ./webgui/install.sh to update the disk helper.';
    return;
  }
  b.disabled = !n || !ST.data.helper.ok || !!JOB;
  b.textContent = sc.cache_kind === 'system' ? 'Cache is on the system disk (grow it above)' : sc.volume_exists ? 'Add selected disks to the cache volume' : 'Use selected disks for the cache';
  if (sc.cache_kind === 'system') b.disabled = true;
  $('#st-sel').textContent = n ? `${n} disk${n > 1 ? 's' : ''} selected · ${fmtBytes(size)} · will be erased` : (ST.data.helper.ok ? 'Tick the disks to use.' : 'Run sudo ./webgui/install.sh again to enable disk setup.');
}
$('#st-disks').addEventListener('change', e => {
  const c = e.target.closest('[data-disk]'); if (!c) return;
  if (c.dataset.incache) { c.checked ? ST.drop.delete(c.dataset.disk) : ST.drop.add(c.dataset.disk); c.closest('.disk').classList.toggle('leaving', !c.checked); }
  else { c.checked ? ST.sel.add(c.dataset.disk) : ST.sel.delete(c.dataset.disk); c.closest('.disk').classList.toggle('sel', c.checked); }
  stSel();
});
$('#st-refresh').onclick = loadStorage;
function planHtml(p) {
  const rm = p.action === 'remove' ? p : p.remove;
  const rmHtml = rm ? `<div class="erase-warn"><b>The current cache volume${rm.freed && rm.freed.length ? ' on ' + esc(rm.freed.join(', ')) : ''} is deleted, with everything cached on it.</b><br>The cache is stopped first.${rm.freed && rm.freed.length ? ' Those disks become empty and can be pulled or reused.' : ''} Disks that were already pulled are fine.</div>` : '';
  return `${rmHtml}${p.erase.length ? `<div class="erase-warn"><b>Everything on ${p.erase_detail.map(d => `${esc(d.name)} (${esc(d.model)}, ${fmtBytes(d.size)})`).join(', ')} will be erased.</b><br>${p.erase_detail.map(d => esc(d.name) + ': ' + esc(d.why)).join('<br>')}</div>` : ''}
    ${p.layout.length ? `<div><div class="k" style="margin-bottom:6px">Folder layout</div><div class="layout-box">${p.layout.map(([a, b]) => `${esc(a.padEnd(22))}<span class="c"># ${esc(b)}</span>`).join('\n')}</div></div>` : ''}
    ${p.env_preview ? `<div><div class="k" style="margin-bottom:6px">.env will be set to</div><div class="layout-box">${Object.entries(p.env_preview).map(([k, v]) => `${esc(k)}=${esc(v)}`).join('\n')}</div></div>` : ''}
    <details><summary class="sub" style="cursor:pointer">Commands that will run (${p.steps.length} steps)</summary><ol class="plan">${p.steps.map(s => `<li><span class="d">${esc(s.desc)}</span>${s.cmd ? '<br>' + esc(s.cmd.join(' ')) : ''}</li>`).join('')}</ol></details>`;
}
async function stApply(action, devices) {
  let p;
  const getPlan = async fs => api('POST', '/api/storage/plan', { action, devices, fs });
  try { p = await getPlan('ext4'); } catch (e) { return toast(e.message, 'bad'); }
  const erase = p.erase.length > 0;
  const title = action === 'create' ? `Make ${p.erase.join(' + ')} the cache volume?` : action === 'add' ? `Add ${p.erase.join(' + ')} to the cache volume?`
    : action === 'move' ? `Move the cache to ${p.erase.join(' + ')}?` : action === 'remove' ? 'Remove the cache volume?' : `Use ${devices[0]} as the cache, as-is?`;
  const confirmText = p.confirm.split(',').join(' ');
  const prom = ask({ title, ok: action === 'remove' ? 'Remove' : erase ? 'Erase and apply' : 'Apply', wide: true, html: `
    ${action === 'create' || action === 'move' ? `<label class="f">Format as<select name="fs" id="st-fs"><option value="ext4">ext4 (recommended)</option><option value="xfs">xfs</option></select></label>` : action === 'add' ? '<p class="sub" style="margin:0">The new disks join the existing volume; the filesystem grows and cached games are kept.</p>' : `<p class="sub" style="margin:0">Nothing is erased: the existing ${esc(p.fs)} filesystem is mounted at ${esc(p.mount)}.</p>`}
    <div id="st-plan" class="form">${planHtml(p)}</div>
    <label class="f">To confirm, type: <b>${esc(confirmText)}</b><input type="text" name="confirm" autocomplete="off" required></label>
    <p class="sub" style="margin:0">If the cache is running it is restarted on the new storage afterwards. This can take a minute.</p>` });
  const fsSel = $('#st-fs');
  if (fsSel) fsSel.onchange = async () => { try { $('#st-plan').innerHTML = planHtml(await getPlan(fsSel.value)); } catch (e) { toast(e.message, 'bad'); } };
  const f = await prom;
  if (!f) return;
  try {
    const r = await api('POST', '/api/storage/apply', { action, devices, fs: f.get('fs') || 'ext4', confirm: f.get('confirm') });
    ST.sel.clear(); ST.drop.clear();
    $('#st-apply').disabled = true;
    followJob(r.job, 'storage', '#st-out', null, () => { loadStorage(); loadConfig(); });
  } catch (e) { toast(e.message, 'bad'); }
}
$('#st-apply').onclick = () => stApply(stMode(), [...ST.sel]);
$('#st-remove').onclick = () => stApply('remove', []);
document.addEventListener('click', async e => {
  const a = e.target.closest('[data-adopt]');
  if (a) return stApply('adopt', [a.dataset.adopt]);
  const fo = e.target.closest('[data-stfolder]');
  if (fo && await confirmBox('Use this folder for the cache?', `CACHE_ROOT becomes <code>${esc(fo.dataset.stfolder)}</code> (game data in <code>${esc(fo.dataset.stfolder)}/cache</code>). Nothing is erased. Restart the cache afterwards.`, 'Use it')) {
    try { const r = await api('POST', '/api/storage/use-folder', { path: fo.dataset.stfolder }); toast('Saved: ' + Object.entries(r.env).map(([k, v]) => k + '=' + v).join(', '), 'good'); loadConfig(); } catch (err) { toast(err.message, 'bad'); }
  }
});

// ================================================================ ARM / DISARM RESTART SWITCHES
const ARMS = [
  { id: 'cache', title: 'Restart cache', label: 'RESTART', desc: 'Recreates the cache and DNS containers so changed settings take effect. Downloads in progress pause for a few seconds.' },
  { id: 'gui', title: 'Restart web GUI', label: 'RESTART', desc: 'Restarts this control panel. The page reconnects by itself in a few seconds. The cache keeps running.' },
  { id: 'reboot', title: 'Reboot machine', label: 'REBOOT', desc: 'Restarts the whole computer. The cache is down for a minute or two; it comes back by itself if start-on-boot is on.' },
];
$('#arms').innerHTML = ARMS.map(a => `<div class="arm" data-armid="${a.id}">
  <div><div class="t"><b>${a.title}</b><div class="lamp2" title="Armed"><i></i><span>ARMED</span></div></div><div class="sub">${a.desc}</div></div>
  <div class="guard"><button class="redbtn" disabled aria-label="${a.title}">${a.label}</button><button class="cover" aria-label="Lift the cover to arm: ${a.title}"><span>LIFT TO ARM</span></button></div></div>`).join('');
const armTimers = {};
function setArmed(el, on) {
  el.classList.toggle('armed', on);
  $('.lamp2', el).className = 'lamp2' + (on ? ' on blink' : '');
  $('.redbtn', el).disabled = !on;
  clearTimeout(armTimers[el.dataset.armid]);
  if (on) armTimers[el.dataset.armid] = setTimeout(() => setArmed(el, false), 10000);   // drops the cover again
}
async function waitForGui(msg, maxSec) {
  toast(msg);
  const end = Date.now() + maxSec * 1000;
  await new Promise(r => setTimeout(r, 3000));
  while (Date.now() < end) {
    try { const r = await fetch('/api/me', { credentials: 'same-origin' }); if (r.status === 200 || r.status === 401) { location.reload(); return; } } catch (e) { /* still down */ }
    await new Promise(r => setTimeout(r, 2000));
  }
  toast('Still not back. Reload the page in a minute.', 'bad');
}
$('#arms').addEventListener('click', async e => {
  const el = e.target.closest('.arm'); if (!el) return;
  if (!isAdmin()) return toast('Viewers can look but not change anything.', 'bad');
  if (e.target.closest('.cover')) return setArmed(el, true);
  if (!e.target.closest('.redbtn') || !el.classList.contains('armed')) return;
  const id = el.dataset.armid;
  setArmed(el, false);
  try {
    if (id === 'cache') {
      const r = await api('POST', '/api/server/action', { action: 'restart' });
      toast('Restarting the cache. Output is on the Service tab.', 'good');
      followJob(r.job, 'restart');
    } else if (id === 'gui') {
      await api('POST', '/api/gui/restart', {});
      waitForGui('Restarting the web GUI…', 60);
    } else if (id === 'reboot') {
      await api('POST', '/api/system/reboot', {});
      waitForGui('Rebooting the machine. This page reconnects when it is back.', 600);
    }
  } catch (err) { toast(err.message, 'bad'); }
});

// ================================================================ USERS
async function loadUsers() {
  if (!isAdmin()) return;
  const r = await api('GET', '/api/users');
  $('#users').innerHTML = `<table><tr><th>User</th><th class="l">Role</th><th>Last login</th><th></th></tr>${r.users.map(u => `<tr><td>${esc(u.name)}${u.name === ME.user ? ' <span class="sub">(you)</span>' : ''}</td>
    <td class="l"><span class="tag-role ${u.role}">${u.role}</span></td><td>${fmtWhen(u.last_login)}</td>
    <td><button class="btn ghost" data-uedit="${esc(u.name)}" data-role="${u.role}">Edit</button>${u.name !== ME.user ? `<button class="btn ghost" data-udel="${esc(u.name)}">Delete</button>` : ''}</td></tr>`).join('')}</table>`;
  $('#audit').innerHTML = r.audit.length ? `<table><tr><th class="l">When</th><th class="l">User</th><th class="l">Action</th><th class="l">Detail</th></tr>${r.audit.map(a => `<tr><td class="l">${fmtWhen(a.t)}</td><td class="l">${esc(a.user)}</td><td class="l">${esc(a.action)}</td><td class="l" style="white-space:normal;max-width:420px">${esc(a.detail)}</td></tr>`).join('')}</table>` : '<div class="empty">Nothing yet.</div>';
}
const userForm = (name, role) => `${name ? `<input type="hidden" name="name" value="${esc(name)}">` : `<label class="f">Username<input type="text" name="name" required pattern="[A-Za-z0-9_.\\-]{2,32}"></label>`}
  <label class="f">Role<select name="role"><option value="viewer"${role === 'viewer' ? ' selected' : ''}>viewer: can look, can't change</option><option value="admin"${role === 'admin' ? ' selected' : ''}>admin: can change everything</option></select></label>
  <label class="f">${name ? 'New password (blank = keep current)' : 'Password (8+ characters)'}<input type="password" name="password" autocomplete="new-password" ${name ? '' : 'required'} minlength="8"></label>`;
$('#user-add').onclick = async () => {
  const f = await ask({ title: 'Add user', html: userForm(null, 'viewer'), ok: 'Add' });
  if (!f) return;
  try { await api('POST', '/api/users', Object.fromEntries(f)); toast('User added', 'good'); loadUsers(); } catch (e) { toast(e.message, 'bad'); }
};
document.addEventListener('click', async e => {
  const ed = e.target.closest('[data-uedit]');
  if (ed) {
    const f = await ask({ title: 'Edit ' + ed.dataset.uedit, html: userForm(ed.dataset.uedit, ed.dataset.role), ok: 'Save' });
    if (!f) return;
    try { await api('POST', '/api/users', Object.fromEntries(f)); toast('Saved', 'good'); loadUsers(); } catch (err) { toast(err.message, 'bad'); }
  }
  const del = e.target.closest('[data-udel]');
  if (del && await confirmBox(`Delete ${del.dataset.udel}?`, 'They are logged out right away.', 'Delete')) {
    try { await api('DELETE', '/api/users/' + encodeURIComponent(del.dataset.udel)); loadUsers(); } catch (err) { toast(err.message, 'bad'); }
  }
});
$('#pw-form').addEventListener('submit', async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  if (f.get('new') !== f.get('again')) return toast("The new passwords don't match", 'bad');
  try { await api('POST', '/api/me/password', { current: f.get('current'), new: f.get('new') }); toast('Password changed. Log in again.', 'good'); e.target.reset(); showLogin(); } catch (err) { toast(err.message, 'bad'); }
});

boot();
