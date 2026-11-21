'use strict';
/* ==========================================================================
   ec20-sip-lite 管理面板 —— 原生 ES2017，无框架 / 无构建 / 无外部资源
   章节：1 工具 2 API 3 登录 4 Toast+对话框 5 顶栏轮询 6 路由
        7 概览 8 分机 9 短信 10 规则 11 设置 12 日志 13 事件 14 启动
   ========================================================================== */

/* ------------------------------ 1. 工具 ------------------------------ */
const $ = (sel, root) => (root || document).querySelector(sel);
const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const int = (v, d) => { const n = parseInt(v, 10); return Number.isNaN(n) ? (d || 0) : n; };
const isOn = (v) => v === 1 || v === true || v === '1';
const kb2mb = (kb) => { const n = int(kb); return n >= 1024 ? (n / 1024).toFixed(1) + ' MB' : n + ' KB'; };
const fmtDur = (s) => { s = Math.max(0, int(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const rndPass = () => {
  const cs = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
  const buf = new Uint8Array(8);
  (window.crypto || {}).getRandomValues ? window.crypto.getRandomValues(buf) : buf.forEach((_, i) => { buf[i] = Math.random() * 255; });
  return Array.from(buf).map((b) => cs[b % cs.length]).join('');
};

const S = {
  status: null, route: 'dashboard', callId: '', ext: [], rules: [], settings: null,
  revealed: {}, sms: { q: '', unread: false, open: {} }, logsAuto: true, smsAuto: 0, poll: null,
};

/* ------------------------------ 2. API ------------------------------ */
class ApiError extends Error { constructor(m, s) { super(m); this.status = s; } }
async function api(path, opt) {
  const o = opt || {};
  const init = { method: o.method || 'GET', credentials: 'same-origin', headers: {} };
  if (o.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(o.body); }
  let res;
  try { res = await fetch('/api' + path, init); } catch (e) { throw new ApiError('网络不可达', 0); }
  if (res.status === 401) { showLogin(); throw new ApiError('未登录', 401); }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) throw new ApiError((data && (data.error || data.detail)) || ('HTTP ' + res.status), res.status);
  return data || {};
}
const post = (p, body) => api(p, { method: 'POST', body });
const put = (p, body) => api(p, { method: 'PUT', body });
const del = (p) => api(p, { method: 'DELETE' });

/* ------------------------------ 3. 登录 ------------------------------ */
function showLogin() { $('#login').classList.remove('hidden'); setTimeout(() => $('#lgPass').focus(), 30); }
function hideLogin() { $('#login').classList.add('hidden'); $('#lgErr').textContent = ''; }

$('#loginForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = ev.target.querySelector('button[type=submit]');
  const body = { username: $('#lgUser').value.trim(), password: $('#lgPass').value };
  btn.disabled = true;
  try {
    const res = await fetch('/api/login', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error('登录失败（HTTP ' + res.status + '）');
    hideLogin(); $('#lgPass').value = '';
    toast('登录成功');
    route(); await loadStatus();
  } catch (e) {
    $('#lgErr').textContent = e.message || '登录失败';
  } finally { btn.disabled = false; }
});

/* --------------------------- 4. Toast / 对话框 --------------------------- */
function toast(msg, kind) {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (kind === 'err' ? 'err' : 'ok');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, 3200);
}

function openDialog(title, bodyHtml, onOk, okText) {
  const host = $('#dlgHost'), box = $('#dlgBox');
  box.innerHTML = '<h3>' + esc(title) + '</h3><div class="dlg-body">' + bodyHtml + '</div>' +
    '<div class="row end mt"><button class="btn ghost" data-dlg="cancel">取消</button>' +
    '<button class="btn primary" data-dlg="ok">' + esc(okText || '保存') + '</button></div>';
  host.classList.remove('hidden');
  const close = () => { host.classList.add('hidden'); box.innerHTML = ''; };
  box.querySelector('[data-dlg=cancel]').addEventListener('click', close);
  box.querySelector('[data-dlg=ok]').addEventListener('click', async () => {
    try { if ((await onOk(box)) !== false) close(); }
    catch (e) { if (e.status !== 401) toast(e.message || '操作失败', 'err'); }
  });
  const first = box.querySelector('input,select,textarea'); if (first) first.focus();
  return { close };
}

/* --------------------------- 5. 顶栏与轮询 --------------------------- */
function barHtml(n) {
  n = Math.max(0, Math.min(5, int(n)));
  let out = '';
  for (let i = 0; i < 5; i++) out += '<i class="' + (i < n ? 'on' : '') + '"></i>';
  return '<span class="bars">' + out + '</span>';
}

function paintTopbar(s) {
  const m = (s && s.modem) || {}, sip = (s && s.sip) || {};
  const online = !!(s && m.present && sip.listening);
  const pill = $('#pill');
  pill.textContent = online ? '在线' : '离线';
  pill.className = 'pill ' + (online ? 'on' : 'off');
  $('#bars').innerHTML = barHtml(m.signal_bars);
  $('#bars').title = '信号 ' + (m.signal_dbm == null ? '未知' : m.signal_dbm + ' dBm');
  const ac = int(sip.active_calls);
  $('#calls').textContent = '通话 ' + ac;
  $('#calls').classList.toggle('hot', ac > 0);
  $('#ver').textContent = 'v' + ((s && s.version) || '1.0.0');
}

async function loadStatus() {
  try {
    S.status = await api('/status');
    paintTopbar(S.status);
    fillDash(S.status);
  } catch (e) {
    if (e.status !== 401) { paintTopbar(null); toast(e.message || '状态获取失败', 'err'); }
  }
}

function startPoll() {
  if (S.poll) clearInterval(S.poll);
  S.poll = setInterval(() => {
    if (document.hidden) return;
    loadStatus();
    if (S.route === 'logs' && S.logsAuto) loadLogs();
    if (S.route === 'dashboard' && !document.querySelector('#dialNum:focus')) loadDashCdr();
  }, 5000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadStatus(); });

/* ------------------------------ 6. 路由 ------------------------------ */
const ROUTES = {
  dashboard: () => viewDashboard(), extensions: () => viewExtensions(), sms: () => viewSms(),
  rules: () => viewRules(), settings: () => viewSettings(), logs: () => viewLogs(),
};
function route() {
  const p = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
  S.route = ROUTES[p] ? p : 'dashboard';
  Array.from(document.querySelectorAll('#nav a')).forEach((a) => a.classList.toggle('active', a.dataset.route === S.route));
  document.body.classList.remove('nav-open');
  Promise.resolve(ROUTES[S.route]()).catch((e) => { if (e.status !== 401) toast(e.message || '加载失败', 'err'); });
}
window.addEventListener('hashchange', route);

/* ------------------------------ 7. 概览 ------------------------------ */
function statCard(title, value, sub, pct, warn) {
  return '<div class="card stat' + (warn ? ' warn' : '') + '">' +
    '<div class="stat-t">' + esc(title) + '</div><div class="stat-v">' + value + '</div>' +
    (pct == null ? '' : '<div class="bar"><i style="width:' + Math.max(0, Math.min(100, pct)) + '%"></i></div>') +
    '<div class="stat-s">' + sub + '</div></div>';
}
const kvRow = (k, v) => '<div><span>' + esc(k) + '</span><b>' + v + '</b></div>';

function viewDashboard() {
  $('#view').innerHTML = [
    '<section class="page">',
    '<div class="page-head"><h1>概览</h1><span class="muted" id="upLine">—</span></div>',
    '<div class="grid four" id="dashCards"></div>',
    '<div class="grid two">',
    '<div class="card"><h3>4G 模块</h3><div id="dashModem" class="kv"></div>',
    '<div class="row gap mt"><button class="btn ghost" data-act="modem-reset">重置模块</button>',
    '<button class="btn ghost" data-act="modem-init">重新初始化</button></div></div>',
    '<div class="card"><h3>拨号盘</h3>',
    '<div class="row gap"><input id="dialNum" class="grow" inputmode="tel" placeholder="号码，例如 13800138000">',
    '<button class="btn primary" data-act="dial">呼叫</button>',
    '<button class="btn danger" data-act="dial-hangup">挂断</button></div>',
    '<p class="muted mt" id="dialState">SIP 分机拨打外线需加语音前缀（默认 9）。</p>',
    '<div class="pad">', ['1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '0', '#']
      .map((d) => '<button class="btn" data-act="pad" data-digit="' + d + '">' + d + '</button>').join(''), '</div>',
    '</div>',
    '</div>',
    '<div class="card"><h3>活动通话</h3><div id="dashCalls" class="muted">—</div></div>',
    '<div class="grid two">',
    '<div class="card"><h3>SIP 概况</h3><div id="dashSip" class="kv"></div></div>',
    '<div class="card"><h3>最近短信</h3><div id="dashSms" class="muted">加载中…</div></div>',
    '</div>',
    '<div class="card"><h3>最近通话记录</h3><div id="dashCdr" class="muted">加载中…</div></div>',
    '</section>',
  ].join('');
  fillDash(S.status);
  loadDashSms();
  loadDashCdr();
}

function fillDash(s) {
  if (!s || !$('#dashCards')) return;
  const h = s.host || {};
  const total = int(h.mem_total_kb), avail = int(h.mem_avail_kb);
  const memPct = total ? Math.round((total - avail) / total * 100) : 0;
  const temp = h.temp_c == null ? null : Number(h.temp_c);
  $('#dashCards').innerHTML =
    statCard('可用内存', kb2mb(avail), '共 ' + kb2mb(total) + ' · 已用 ' + memPct + '%', memPct, memPct > 85) +
    statCard('系统负载', esc(h.load || '-'), '1 分钟平均', Math.min(100, (Number(h.load) || 0) * 20), (Number(h.load) || 0) > 2) +
    statCard('温度', (temp == null ? '-' : esc(temp)) + ' ℃', temp == null ? '无传感器' : (temp > 70 ? '偏高' : '正常'), temp == null ? null : Math.min(100, temp), temp != null && temp > 70) +
    statCard('磁盘可用', esc(int(h.disk_free_mb)) + ' MB', int(h.disk_free_mb) < 200 ? '空间不足' : '数据分区', null, int(h.disk_free_mb) < 200);
  $('#upLine').textContent = '运行 ' + esc(h.uptime || '—') + ' · 版本 ' + esc(s.version || '-');

  const m = s.modem || {};
  $('#dashModem').innerHTML = [
    kvRow('型号', esc(m.model || '-') + (m.present ? '' : ' <span class="tag warn">未检测</span>')),
    kvRow('固件', esc(m.firmware || '-')), kvRow('IMEI', esc(m.imei || '-')), kvRow('ICCID', esc(m.iccid || '-')),
    kvRow('SIM', '<span class="tag ' + (m.sim_state === 'READY' ? 'ok' : 'warn') + '">' + esc(m.sim_state || '未知') + '</span>'),
    kvRow('信号', barHtml(m.signal_bars) + ' <span class="muted">' + esc(m.signal_dbm == null ? '-' : m.signal_dbm + ' dBm') + '</span>'),
    kvRow('注册', esc(m.reg_status || '-')), kvRow('运营商', esc(m.operator || '-')),
    kvRow('IMS', isOn(m.ims) ? '<span class="tag ok">已启用</span>' : '<span class="tag">未启用</span>'),
    kvRow('语音就绪', isOn(m.voice_ready) ? '<span class="tag ok">是</span>' : '<span class="tag warn">否</span>'),
  ].join('');

  const calls = m.calls || [];
  const cbox = $('#dashCalls');
  if (!calls.length) { cbox.className = 'muted'; cbox.textContent = '当前无活动通话'; }
  else {
    cbox.className = '';
    cbox.innerHTML = '<table><thead><tr><th>方向</th><th>号码</th><th>状态</th><th>时长</th><th>操作</th></tr></thead><tbody>' +
      calls.map((c) => '<tr><td>' + (c.dir === 'in' ? '来电' : '呼出') + '</td>' +
        '<td class="mono">' + esc(c.number) + '</td><td>' + esc(c.state) + '</td>' +
        '<td class="mono">' + fmtDur(c.duration) + '</td><td class="row">' +
        (c.dir === 'in' ? '<button class="btn sm" data-act="call-answer" data-id="' + esc(c.id) + '">接听</button>' : '') +
        '<button class="btn sm danger" data-act="call-hangup" data-id="' + esc(c.id) + '">挂断</button>' +
        '</td></tr>').join('') + '</tbody></table>';
  }

  const sip = s.sip || {}, sms = s.sms || {}, fw = s.forward || {};
  $('#dashSip').innerHTML = [
    kvRow('监听地址', '<span class="mono">' + esc(sip.listening || '-') + '</span>'),
    kvRow('在线分机', esc(int(sip.registrations)) + ' / ' + esc(int(sip.extensions))),
    kvRow('活动通话', esc(int(sip.active_calls))),
    kvRow('短信', '共 ' + esc(int(sms.total)) + ' · 未读 ' + esc(int(sms.unread)) + ' · 今日 ' + esc(int(sms.today))),
    kvRow('转发', isOn(fw.enabled) ? '<span class="tag ok">已启用</span> <span class="muted">' + esc((fw.channels || []).join(', ')) + '</span>' : '<span class="tag">未启用</span>'),
  ].join('');
}

async function loadDashSms() {
  try {
    const d = await api('/sms?limit=5&offset=0');
    const box = $('#dashSms'); if (!box) return;
    const items = d.items || [];
    if (!items.length) { box.className = 'muted'; box.textContent = '暂无短信'; return; }
    box.className = '';
    box.innerHTML = items.map((m) => '<div class="mini-sms' + (isOn(m.read) ? '' : ' unread') + '">' +
      '<div class="mini-h"><span class="mono">' + (m.direction === 'in' ? '← ' : '→ ') + esc(m.peer) + '</span>' +
      '<span class="muted">' + esc(m.ts) + '</span></div>' +
      '<div class="mini-t">' + esc(m.text) + '</div></div>').join('');
  } catch (e) { if (e.status !== 401 && $('#dashSms')) $('#dashSms').textContent = '加载失败'; }
}

async function loadDashCdr() {
  try {
    const d = await api('/cdr?limit=8');
    const box = $('#dashCdr'); if (!box) return;
    const items = d.items || [];
    if (!items.length) { box.className = 'muted'; box.textContent = '暂无通话记录'; return; }
    box.className = '';
    box.innerHTML = '<table><thead><tr><th>开始时间</th><th>主叫</th><th>被叫</th><th>方向</th><th>状态</th><th>时长</th></tr></thead><tbody>' +
      items.map((c) => '<tr><td class="mono">' + esc(c.started) + '</td><td class="mono">' + esc(c.from) + '</td>' +
        '<td class="mono">' + esc(c.to) + '</td><td>' + (c.dir === 'out' ? '呼出' : '呼入') + '</td>' +
        '<td>' + esc(c.state) + '</td><td class="mono">' + fmtDur(c.duration) + '</td></tr>').join('') + '</tbody></table>';
  } catch (e) { if (e.status !== 401 && $('#dashCdr')) $('#dashCdr').textContent = '加载失败'; }
}

/* ------------------------------ 8. 分机 ------------------------------ */
async function viewExtensions() {
  $('#view').innerHTML = [
    '<section class="page">',
    '<div class="page-head"><h1>分机</h1></div>',
    '<div class="card"><h3>新增分机</h3><div class="row gap end">',
    '<label class="field"><span>分机号（3-6 位数字）</span><input id="newNum" inputmode="numeric" placeholder="1001"></label>',
    '<label class="field grow"><span>显示名称</span><input id="newName" placeholder="前台"></label>',
    '<label class="field"><span>密码（留空自动生成）</span><input id="newPass" placeholder="8 位随机"></label>',
    '<button class="btn primary" data-act="ext-add">添加</button></div></div>',
    '<div class="card"><h3>分机列表</h3><div id="extTable" class="muted">加载中…</div></div>',
    '</section>',
  ].join('');
  const d = await api('/extensions');
  S.ext = d.items || [];
  renderExtTable();
}

function renderExtTable() {
  const box = $('#extTable'); if (!box) return;
  if (!S.ext.length) { box.className = 'muted'; box.textContent = '还没有分机'; return; }
  box.className = '';
  box.innerHTML = '<div class="tbl-wrap"><table><thead><tr><th>分机号</th><th>名称</th><th>密码</th><th>状态</th>' +
    '<th>联系地址</th><th>最近注册</th><th>操作</th></tr></thead><tbody>' +
    S.ext.map((e) => {
      const pw = S.revealed[e.id] ? esc(e.password) : '••••••••';
      return '<tr><td class="mono">' + esc(e.number) + '</td><td>' + esc(e.display_name) + '</td>' +
        '<td class="mono">' + pw + ' <button class="btn sm ghost" data-act="ext-reveal" data-id="' + esc(e.id) + '">' + (S.revealed[e.id] ? '隐藏' : '显示') + '</button>' +
        '<button class="btn sm ghost" data-act="ext-copy" data-id="' + esc(e.id) + '">复制</button></td>' +
        '<td>' + (isOn(e.enabled) ? (e.registered ? '<span class="tag ok">在线</span>' : '<span class="tag">未注册</span>') : '<span class="tag warn">已停用</span>') + '</td>' +
        '<td class="mono muted">' + esc(e.contact || '-') + '</td><td class="mono muted">' + esc(e.last_seen || '-') + '</td>' +
        '<td class="row"><button class="btn sm" data-act="ext-edit" data-id="' + esc(e.id) + '">编辑</button>' +
        '<button class="btn sm ghost" data-act="ext-kick" data-id="' + esc(e.id) + '">踢下线</button>' +
        '<button class="btn sm danger" data-act="ext-del" data-id="' + esc(e.id) + '">删除</button></td></tr>';
    }).join('') + '</tbody></table></div>';
}

async function addExtension() {
  const number = $('#newNum').value.trim(), name = $('#newName').value.trim();
  if (!/^\d{3,6}$/.test(number)) return toast('分机号必须是 3-6 位数字', 'err');
  const typed = $('#newPass').value.trim();
  const password = typed || rndPass();
  await post('/extensions', { number, display_name: name, password, enabled: 1 });
  toast('已添加分机 ' + number + '，密码 ' + password);
  $('#newNum').value = ''; $('#newName').value = ''; $('#newPass').value = '';
  S.ext = (await api('/extensions')).items || [];
  renderExtTable();
}

function editExtension(id) {
  const e = S.ext.find((x) => String(x.id) === String(id));
  if (!e) return;
  openDialog('编辑分机 ' + e.number, [
    '<label class="field"><span>显示名称</span><input id="edName" value="' + esc(e.display_name) + '"></label>',
    '<label class="field"><span>新密码（留空不修改）</span><input id="edPass" placeholder="不修改"></label>',
    '<label class="field"><span>状态</span><select id="edEnabled">',
    '<option value="1"' + (isOn(e.enabled) ? ' selected' : '') + '>启用</option>',
    '<option value="0"' + (isOn(e.enabled) ? '' : ' selected') + '>停用</option></select></label>',
  ].join(''), async (box) => {
    const body = { display_name: box.querySelector('#edName').value.trim(), enabled: int(box.querySelector('#edEnabled').value) };
    const pw = box.querySelector('#edPass').value.trim();
    if (pw) body.password = pw;
    await put('/extensions/' + encodeURIComponent(id), body);
    toast('分机已更新');
    S.ext = (await api('/extensions')).items || [];
    renderExtTable();
  });
}

/* ------------------------------ 9. 短信 ------------------------------ */
async function viewSms() {
  $('#view').innerHTML = [
    '<section class="page">',
    '<div class="page-head"><h1>短信</h1><div class="row gap">',
    '<input id="smsQ" class="grow" placeholder="搜索号码或内容">',
    '<label class="chk"><input type="checkbox" id="smsUnread"> 只看未读</label>',
    '<button class="btn ghost" data-act="sms-search">搜索</button>',
    '<button class="btn ghost" data-act="sms-readall">全部已读</button>',
    '</div></div>',
    '<div class="grid two">',
    '<div class="card"><h3>发送新短信</h3>',
    '<label class="field"><span>收件号码</span><input id="smsTo" inputmode="tel" placeholder="13800138000"></label>',
    '<label class="field"><span>内容</span><textarea id="smsText" rows="3" placeholder="短信内容，超出 70 字自动分条"></textarea></label>',
    '<div class="row between"><span class="muted" id="smsLen">0 字</span><button class="btn primary" data-act="sms-send">发送</button></div></div>',
    '<div class="card"><h3>说明</h3><p class="muted">短信通过 EC20 模块收发，每 5 秒自动读状态。命中转发规则的短信会自动推送到已配置渠道。</p></div>',
    '</div>',
    '<div class="card"><h3>短信列表 <span class="muted" id="smsTotal"></span></h3><div id="smsList" class="muted">加载中…</div></div>',
    '</section>',
  ].join('');
  $('#smsQ').value = S.sms.q;
  $('#smsUnread').checked = S.sms.unread;
  await loadSms();
}

async function loadSms() {
  const qs = '?limit=50&offset=0&q=' + encodeURIComponent(S.sms.q) + '&unread=' + (S.sms.unread ? 1 : 0);
  const d = await api('/sms' + qs);
  const box = $('#smsList'); if (!box) return;
  $('#smsTotal').textContent = d.total ? '共 ' + d.total + ' 条' : '';
  const items = d.items || [];
  if (!items.length) { box.className = 'muted'; box.textContent = '没有符合条件的短信'; return; }
  box.className = '';
  box.innerHTML = items.map((m) => {
    const open = !!S.sms.open[m.id];
    return '<article class="sms' + (isOn(m.read) ? '' : ' unread') + (open ? '' : ' fold') + '" data-act="sms-toggle" data-id="' + esc(m.id) + '">' +
      '<div class="sms-h"><span class="mono">' + (m.direction === 'in' ? '← ' : '→ ') + esc(m.peer) + '</span>' +
      '<span class="row">' + (isOn(m.read) ? '' : '<span class="tag warn">未读</span>') +
      (isOn(m.forwarded) ? '<span class="tag ok">已转发</span>' : '') +
      (int(m.parts) > 1 ? '<span class="tag">' + int(m.parts) + ' 段</span>' : '') +
      '<span class="muted">' + esc(m.ts) + '</span></span></div>' +
      '<div class="sms-b">' + esc(m.text) + '</div>' +
      '<div class="row end sms-a"><button class="btn sm ghost" data-act="sms-read" data-id="' + esc(m.id) + '">标记已读</button>' +
      '<button class="btn sm danger" data-act="sms-del" data-id="' + esc(m.id) + '">删除</button></div></article>';
  }).join('');
}

async function sendSms() {
  const to = $('#smsTo').value.trim(), text = $('#smsText').value.trim();
  if (!to) return toast('请填写收件号码', 'err');
  if (!text) return toast('请填写短信内容', 'err');
  await post('/sms/send', { to, text });
  toast('短信已提交');
  $('#smsText').value = ''; $('#smsLen').textContent = '0 字';
  await loadSms();
}

/* ------------------------------ 10. 规则 ------------------------------ */
async function viewRules() {
  $('#view').innerHTML = [
    '<section class="page">',
    '<div class="page-head"><h1>转发规则</h1><button class="btn primary" data-act="rule-new">新增规则</button></div>',
    '<div id="ruleList" class="grid two muted">加载中…</div>',
    '</section>',
  ].join('');
  await loadRules();
}

const MATCH_TEXT = { all: '全部短信', sender: '号码包含', keyword: '关键词包含' };

async function loadRules() {
  const d = await api('/rules');
  S.rules = d.items || [];
  renderRules();
}

function renderRules() {
  const box = $('#ruleList'); if (!box) return;
  if (!S.rules.length) { box.className = 'muted'; box.textContent = '还没有规则，点右上角新增'; return; }
  box.className = 'grid two';
  box.innerHTML = S.rules.map((r) => {
    const chs = String(r.channels || '').split(',').map((x) => x.trim()).filter(Boolean);
    return '<div class="card rule' + (isOn(r.enabled) ? '' : ' off') + '">' +
      '<div class="row between"><strong>' + esc(r.name) + '</strong>' +
      '<label class="chk"><input type="checkbox" data-act="rule-toggle" data-id="' + esc(r.id) + '"' + (isOn(r.enabled) ? ' checked' : '') + '> 启用</label></div>' +
      '<div class="muted mt">匹配：' + esc(MATCH_TEXT[r.match_type] || r.match_type) + (r.match_type === 'all' ? '' : '「' + esc(r.pattern) + '」') + '</div>' +
      '<div class="muted">模板：' + (r.template ? esc(r.template) : '默认（{peer} / {text} / {time}）') + '</div>' +
      '<div class="row gap mt">' + ['email', 'dingtalk', 'webhook'].map((c) => '<label class="chk"><input type="checkbox" data-act="rule-chan" data-id="' + esc(r.id) + '" data-ch="' + c + '"' + (chs.indexOf(c) >= 0 ? ' checked' : '') + '> ' + c + '</label>').join('') + '</div>' +
      '<div class="row end mt"><button class="btn sm" data-act="rule-test" data-id="' + esc(r.id) + '">测试</button>' +
      '<button class="btn sm ghost" data-act="rule-edit" data-id="' + esc(r.id) + '">编辑</button>' +
      '<button class="btn sm danger" data-act="rule-del" data-id="' + esc(r.id) + '">删除</button></div></div>';
  }).join('');
}

function editRule(id) {
  const r = id ? S.rules.find((x) => String(x.id) === String(id)) : { name: '', enabled: 1, match_type: 'keyword', pattern: '', channels: 'email', template: '' };
  if (!r) return;
  const chs = String(r.channels || '').split(',');
  openDialog(id ? '编辑规则' : '新增规则', [
    '<label class="field"><span>规则名称</span><input id="ruName" value="' + esc(r.name) + '"></label>',
    '<label class="field"><span>匹配方式</span><select id="ruMatch">' +
    ['all', 'sender', 'keyword'].map((k) => '<option value="' + k + '"' + (r.match_type === k ? ' selected' : '') + '>' + MATCH_TEXT[k] + '</option>').join('') + '</select></label>',
    '<label class="field"><span>号码 / 关键词</span><input id="ruPat" value="' + esc(r.pattern) + '" placeholder="多个用逗号分隔"></label>',
    '<div class="row gap"><span class="muted">渠道</span>' +
    ['email', 'dingtalk', 'webhook'].map((c) => '<label class="chk"><input type="checkbox" id="ruCh-' + c + '"' + (chs.indexOf(c) >= 0 ? ' checked' : '') + '> ' + c + '</label>').join('') + '</div>',
    '<label class="field"><span>模板（可用 {peer} {text} {time}）</span><textarea id="ruTpl" rows="2">' + esc(r.template) + '</textarea></label>',
    '<label class="chk"><input type="checkbox" id="ruEnabled"' + (isOn(r.enabled) ? ' checked' : '') + '> 启用此规则</label>',
  ].join(''), async (box) => {
    const body = {
      name: box.querySelector('#ruName').value.trim() || '未命名规则',
      enabled: box.querySelector('#ruEnabled').checked ? 1 : 0,
      match_type: box.querySelector('#ruMatch').value,
      pattern: box.querySelector('#ruPat').value.trim(),
      channels: ['email', 'dingtalk', 'webhook'].filter((c) => box.querySelector('#ruCh-' + c).checked).join(','),
      template: box.querySelector('#ruTpl').value,
    };
    if (id) await put('/rules/' + encodeURIComponent(id), body); else await post('/rules', body);
    toast('规则已保存');
    await loadRules();
  });
}

/* ------------------------------ 11. 设置 ------------------------------ */
const fval = (id) => { const el = $('#' + id); return el ? el.value.trim() : ''; };
const fchk = (id) => { const el = $('#' + id); return el && el.checked ? 1 : 0; };
const field = (label, inner, cls) => '<label class="field ' + (cls || '') + '"><span>' + esc(label) + '</span>' + inner + '</label>';
const input = (id, ph, type) => '<input id="' + id + '"' + (type ? ' type="' + type + '"' : '') + ' placeholder="' + esc(ph || '') + '">';
const switchLine = (id, label) => '<label class="chk wide"><input type="checkbox" id="' + id + '"> ' + esc(label) + '</label>';
const box_ = (title, inner, sec, testKind) => '<div class="card"><h3>' + esc(title) + '</h3><div class="form grid two">' + inner + '</div>' +
  '<div class="row end mt">' + (testKind ? '<button class="btn ghost" data-act="set-test" data-kind="' + testKind + '">测试</button>' : '') +
  '<button class="btn primary" data-act="set-save" data-sec="' + sec + '">保存</button></div></div>';

async function viewSettings() {
  $('#view').innerHTML = [
    '<section class="page"><div class="page-head"><h1>设置</h1></div>',
    box_('邮件通知（SMTP）',
      switchLine('stSmtpEnabled', '开启 SMTP 转发') +
      field('服务器', input('stSmtpHost', 'smtp.qq.com')) + field('端口', input('stSmtpPort', '465')) +
      field('加密', '<select id="stSmtpTls"><option value="ssl">SSL</option><option value="starttls">STARTTLS</option><option value="none">不加密</option></select>') +
      field('账号', input('stSmtpUser', 'user@qq.com')) + field('密码', input('stSmtpPass', '留空不修改', 'password')) +
      field('发件人', input('stSmtpFrom', 'user@qq.com')), 'smtp', 'email'),
    box_('钉钉机器人',
      switchLine('stDingEnabled', '开启钉钉转发') + field('加签密钥', input('stDingSecret', '留空不修改', 'password')) +
      field('Webhook', input('stDingWebhook', 'https://oapi.dingtalk.com/robot/send?access_token=...'), 'wide') +
      field('@手机号', input('stDingAt', '多个用逗号分隔')), 'dingtalk', 'dingtalk'),
    box_('通用 Webhook',
      switchLine('stHookEnabled', '开启 Webhook 转发') + field('地址', input('stHookUrl', 'http://192.168.1.10:9000/sms'), 'wide'), 'webhook', 'webhook'),
    box_('拨号参数',
      field('语音前缀', input('stDialPrefix', '9')) + field('振铃时长（秒）', input('stDialRing', '30')) +
      field('来电自动接听', '<select id="stDialAuto"><option value="0">关闭</option><option value="1">开启</option></select>') +
      field('来电转移至', input('stDialForward', '留空则群振在线分机'), 'wide'), 'dial', ''),
    box_('SIP 参数',
      field('域（realm）', input('stSipRealm', 'ec20sip.local')) + field('对外 IP', input('stSipIp', '192.168.1.188')) +
      field('端口', input('stSipPort', '5060')), 'sip', ''),
    box_('管理账号',
      field('用户名', input('stAuthUser', 'admin')) + field('新密码', input('stAuthPass', '留空不修改', 'password')), 'auth', ''),
    '<div class="card"><h3>系统信息</h3><div id="sysInfo" class="kv muted">加载中…</div></div>',
    '</section>',
  ].join('');
  await loadSettings();
  loadSysInfo();
}

async function loadSettings() {
  S.settings = await api('/settings');
  if (!S.route || S.route !== 'settings') return;
  const c = S.settings;
  const set = (id, v) => { const el = $('#' + id); if (el && v != null) el.value = v; };
  const smtp = c.smtp || {};
  $('#stSmtpEnabled').checked = isOn(smtp.enabled) || false;
  set('stSmtpHost', smtp.host || ''); set('stSmtpPort', smtp.port || 465); set('stSmtpTls', smtp.tls || 'ssl');
  set('stSmtpUser', smtp.user || ''); set('stSmtpFrom', smtp.from || '');
  $('#stSmtpPass').placeholder = smtp.pass_set ? '已设置，留空不修改' : '未设置';
  const ding = c.dingtalk || {};
  $('#stDingEnabled').checked = !!isOn(ding.enabled);
  set('stDingWebhook', ding.webhook || ''); set('stDingAt', ding.at_mobiles || '');
  $('#stDingSecret').placeholder = ding.secret_set ? '已设置，留空不修改' : '未设置';
  const hook = c.webhook || {};
  $('#stHookEnabled').checked = !!isOn(hook.enabled);
  set('stHookUrl', hook.url || '');
  const dial = c.dial || {};
  set('stDialPrefix', dial.voice_prefix || ''); set('stDialRing', dial.ring_seconds || 30);
  set('stDialAuto', isOn(dial.auto_answer) ? '1' : '0'); set('stDialForward', dial.forward_all_to || '');
  const sip = c.sip || {};
  set('stSipRealm', sip.realm || ''); set('stSipIp', sip.external_ip || ''); set('stSipPort', sip.port || 5060);
  set('stAuthUser', (c.auth && c.auth.username) || 'admin');
}

async function saveSection(sec) {
  const body = {};
  if (sec === 'smtp') {
    body.smtp = { enabled: fchk('stSmtpEnabled'), host: fval('stSmtpHost'), port: int(fval('stSmtpPort'), 465), user: fval('stSmtpUser'), from: fval('stSmtpFrom'), tls: fval('stSmtpTls') };
    const p = $('#stSmtpPass').value; if (p) body.smtp.pass = p;
  } else if (sec === 'dingtalk') {
    body.dingtalk = { enabled: fchk('stDingEnabled'), webhook: fval('stDingWebhook'), at_mobiles: fval('stDingAt') };
    const s = $('#stDingSecret').value; if (s) body.dingtalk.secret = s;
  } else if (sec === 'webhook') {
    body.webhook = { enabled: fchk('stHookEnabled'), url: fval('stHookUrl') };
  } else if (sec === 'dial') {
    body.dial = { voice_prefix: fval('stDialPrefix'), ring_seconds: int(fval('stDialRing'), 30), auto_answer: int(fval('stDialAuto')), forward_all_to: fval('stDialForward') };
  } else if (sec === 'sip') {
    body.sip = { realm: fval('stSipRealm'), external_ip: fval('stSipIp'), port: int(fval('stSipPort'), 5060) };
  } else if (sec === 'auth') {
    body.auth = { username: fval('stAuthUser') || 'admin' };
    const p = $('#stAuthPass').value; if (p) body.auth.password = p;
  }
  await put('/settings', body);
  toast('已保存');
  $('#stSmtpPass').value = ''; $('#stDingSecret').value = ''; $('#stAuthPass').value = '';
  await loadSettings();
}

async function testChannel(kind) {
  if (kind === 'email') {
    const to = fval('stSmtpFrom') || fval('stSmtpUser');
    if (!to) return toast('请先填写发件人地址', 'err');
    const r = await post('/settings/test_email', { to });
    return toast('邮件测试：' + (r.detail || (r.ok ? '成功' : '失败')), r.ok ? 'ok' : 'err');
  }
  const path = kind === 'dingtalk' ? '/settings/test_dingtalk' : '/settings/test_webhook';
  const r = await post(path);
  toast((kind === 'dingtalk' ? '钉钉' : 'Webhook') + '测试：' + (r.detail || (r.ok ? '成功' : '失败')), r.ok ? 'ok' : 'err');
}

async function loadSysInfo() {
  const box = $('#sysInfo'); if (!box) return;
  try {
    const d = await api('/system/info');
    box.className = 'kv';
    box.innerHTML = [
      kvRow('Python', esc(d.python || '-')), kvRow('内核', esc(d.kernel || '-')),
      kvRow('硬件', esc(d.model || '-')), kvRow('架构', esc(d.arch || '-')),
      kvRow('内存占用', esc(kb2mb(d.rss_kb))), kvRow('安装目录', '<span class="mono">' + esc(d.install_dir || '-') + '</span>'),
      kvRow('数据目录', '<span class="mono">' + esc(d.data_dir || '-') + '</span>'),
    ].join('');
  } catch (e) { if (e.status !== 401) box.textContent = '获取失败'; }
}

/* ------------------------------ 12. 日志 ------------------------------ */
function viewLogs() {
  $('#view').innerHTML = [
    '<section class="page">',
    '<div class="page-head"><h1>日志</h1><div class="row gap">',
    '<label class="chk"><input type="checkbox" id="logAuto"' + (S.logsAuto ? ' checked' : '') + '> 自动刷新</label>',
    '<button class="btn ghost" data-act="logs-refresh">刷新</button></div></div>',
    '<pre id="logPre" class="pre">加载中…</pre>',
    '</section>',
  ].join('');
  $('#logAuto').addEventListener('change', (e) => { S.logsAuto = e.target.checked; });
  loadLogs();
}

async function loadLogs() {
  try {
    const d = await api('/logs?tail=200');
    const pre = $('#logPre'); if (!pre) return;
    pre.textContent = (d.lines || []).join('\n') || '（暂无日志）';
    pre.scrollTop = pre.scrollHeight;
  } catch (e) { if (e.status !== 401) { const pre = $('#logPre'); if (pre) pre.textContent = '读取失败：' + e.message; } }
}

/* --------------------------- 13. 事件委托 --------------------------- */
document.addEventListener('click', async (ev) => {
  const el = ev.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act, id = el.dataset.id;
  try {
    switch (act) {
      case 'pad': {
        const inp = $('#dialNum'); if (inp) { inp.value += el.dataset.digit; S.dialNum = inp.value; }
        break;
      }
      case 'dial': {
        const number = fval('dialNum');
        if (!number) { toast('请输入号码', 'err'); break; }
        const r = await post('/dial', { number });
        S.callId = r.call_id || '';
        if ($('#dialState')) $('#dialState').textContent = '正在呼叫 ' + number + '（call_id: ' + (S.callId || '-') + '）';
        toast('正在呼叫 ' + number);
        break;
      }
      case 'dial-hangup':
        if (!S.callId) { toast('没有进行中的呼叫', 'err'); break; }
        await post('/calls/' + encodeURIComponent(S.callId) + '/hangup');
        S.callId = ''; toast('已挂断'); await loadStatus();
        if ($('#dialState')) $('#dialState').textContent = '通话已结束。';
        break;
      case 'call-answer': await post('/calls/' + encodeURIComponent(id) + '/answer'); toast('已接听'); await loadStatus(); break;
      case 'call-hangup': await post('/calls/' + encodeURIComponent(id) + '/hangup'); toast('已挂断'); await loadStatus(); break;
      case 'modem-reset': await post('/modem/reset'); toast('已下发模块重置'); await loadStatus(); break;
      case 'modem-init': await post('/modem/init'); toast('已下发模块初始化'); await loadStatus(); break;

      case 'ext-add': await addExtension(); break;
      case 'ext-reveal': S.revealed[id] = !S.revealed[id]; renderExtTable(); break;
      case 'ext-copy': {
        const e = S.ext.find((x) => String(x.id) === String(id));
        if (!e) break;
        if (navigator.clipboard && navigator.clipboard.writeText) { await navigator.clipboard.writeText(e.password); toast('密码已复制'); }
        else { S.revealed[id] = true; renderExtTable(); toast('已显示密码，请手动复制', 'err'); }
        break;
      }
      case 'ext-edit': editExtension(id); break;
      case 'ext-kick': await post('/extensions/' + encodeURIComponent(id) + '/kick'); toast('已踢下线'); S.ext = (await api('/extensions')).items || []; renderExtTable(); break;
      case 'ext-del':
        if (!confirm('确认删除该分机？')) break;
        await del('/extensions/' + encodeURIComponent(id)); toast('已删除');
        S.ext = (await api('/extensions')).items || []; renderExtTable();
        break;

      case 'sms-search': S.sms.q = fval('smsQ'); S.sms.unread = $('#smsUnread').checked; await loadSms(); break;
      case 'sms-readall': await post('/sms/read_all'); toast('已全部标为已读'); await loadSms(); await loadStatus(); break;
      case 'sms-send': await sendSms(); break;
      case 'sms-toggle': S.sms.open[id] = !S.sms.open[id]; await loadSms(); break;
      case 'sms-read': await post('/sms/' + encodeURIComponent(id) + '/read'); await loadSms(); await loadStatus(); break;
      case 'sms-del': await del('/sms/' + encodeURIComponent(id)); toast('已删除'); await loadSms(); break;

      case 'rule-new': editRule(null); break;
      case 'rule-edit': editRule(id); break;
      case 'rule-test': {
        const r = await post('/rules/' + encodeURIComponent(id) + '/test');
        const res = r.result || {};
        const txt = Object.keys(res).map((k) => k + '=' + res[k]).join('，') || '无结果';
        toast('测试结果：' + txt, Object.keys(res).some((k) => res[k] === 'ok' || res[k] === 'sent') ? 'ok' : 'err');
        break;
      }
      case 'rule-del':
        if (!confirm('确认删除该规则？')) break;
        await del('/rules/' + encodeURIComponent(id)); toast('已删除'); await loadRules();
        break;

      case 'set-save': await saveSection(el.dataset.sec); break;
      case 'set-test': await testChannel(el.dataset.kind); break;
      case 'logs-refresh': await loadLogs(); break;
      default: break;
    }
  } catch (e) {
    if (e.status !== 401) toast(e.message || '操作失败', 'err');
  }
});

document.addEventListener('change', async (ev) => {
  const el = ev.target.closest('[data-act]');
  if (!el) return;
  const id = el.dataset.id;
  try {
    if (el.dataset.act === 'rule-toggle') {
      const r = S.rules.find((x) => String(x.id) === String(id));
      if (!r) return;
      await put('/rules/' + encodeURIComponent(id), { name: r.name, enabled: el.checked ? 1 : 0, match_type: r.match_type, pattern: r.pattern, channels: r.channels, template: r.template });
      toast(el.checked ? '规则已启用' : '规则已停用', 'ok');
      await loadRules();
    } else if (el.dataset.act === 'rule-chan') {
      const r = S.rules.find((x) => String(x.id) === String(id));
      if (!r) return;
      const set = new Set(String(r.channels || '').split(',').map((x) => x.trim()).filter(Boolean));
      if (el.checked) set.add(el.dataset.ch); else set.delete(el.dataset.ch);
      await put('/rules/' + encodeURIComponent(id), { name: r.name, enabled: isOn(r.enabled) ? 1 : 0, match_type: r.match_type, pattern: r.pattern, channels: Array.from(set).join(','), template: r.template });
      await loadRules();
    }
  } catch (e) { if (e.status !== 401) toast(e.message || '操作失败', 'err'); }
});

document.addEventListener('input', (ev) => {
  if (ev.target.id === 'smsText') $('#smsLen').textContent = ev.target.value.length + ' 字';
  if (ev.target.id === 'dialNum') S.dialNum = ev.target.value;
});
document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Enter') return;
  if (ev.target.id === 'dialNum') { ev.preventDefault(); const b = $('[data-act=dial]'); if (b) b.click(); }
  if (ev.target.id === 'smsQ') { ev.preventDefault(); const b = $('[data-act="sms-search"]'); if (b) b.click(); }
  if (ev.key === 'Escape') { const h = $('#dlgHost'); if (h && !h.classList.contains('hidden')) h.classList.add('hidden'); }
});

$('#refreshBtn').addEventListener('click', async () => { await loadStatus(); toast('已刷新'); });
$('#menuBtn').addEventListener('click', () => document.body.classList.toggle('nav-open'));

/* ------------------------------ 14. 启动 ------------------------------ */
(async function boot() {
  try {
    await loadStatus();
    hideLogin();
  } catch (e) {
    if (e.status === 401) showLogin();
  }
  route();
  startPoll();
})();