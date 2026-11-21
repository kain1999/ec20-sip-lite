/* EC20 SIP 网关管理面板 —— 原生 JS，无依赖，无构建 */
(function () {
  'use strict';

  var S = {
    status: null, view: 'dashboard', ext: [], sms: [], smsTotal: 0, rules: [], settings: null,
    logs: [], revealed: {}, expanded: {}, dial: '', filter: { q: '', unread: false },
    pollTimer: null, logsTimer: null
  };

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function fmtMb(kb) {
    kb = Number(kb) || 0;
    return kb >= 1024 ? (kb / 1024).toFixed(0) + ' MB' : kb + ' KB';
  }
  function fmtDur(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    if (sec < 60) return sec + 's';
    if (sec < 3600) return Math.floor(sec / 60) + 'm' + String(sec % 60).padStart(2, '0') + 's';
    return Math.floor(sec / 3600) + 'h' + String(Math.floor(sec % 3600 / 60)).padStart(2, '0') + 'm';
  }
  function fmtUptime(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    if (d) return d + '天' + h + '小时';
    if (h) return h + '小时' + m + '分';
    return m + '分';
  }

  function toast(message, kind) {
    var box = $('#toasts');
    if (!box) return;
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.textContent = message;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 4200);
  }

  function api(path, options) {
    var opt = Object.assign({ method: 'GET', headers: {} }, options || {});
    if (opt.body !== undefined && typeof opt.body !== 'string') {
      opt.body = JSON.stringify(opt.body);
      opt.headers['Content-Type'] = 'application/json';
    }
    opt.credentials = 'same-origin';
    return fetch(path, opt).then(function (res) {
      if (res.status === 401) { showLogin(); throw new Error('请先登录'); }
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
        return data;
      });
    });
  }

  /* ------------------------------------------------------------------ 登录 */
  function showLogin() {
    var box = $('#login');
    if (box) box.classList.remove('hidden');
    var pass = $('#login-pass');
    if (pass) setTimeout(function () { pass.focus(); }, 30);
  }
  function hideLogin() {
    var box = $('#login');
    if (box) box.classList.add('hidden');
    var err = $('#login-err');
    if (err) err.textContent = '';
  }

  function bindLogin() {
    var form = $('#loginform');
    if (!form) return;
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var username = $('#login-user').value.trim();
      var password = $('#login-pass').value;
      api('/api/login', { method: 'POST', body: { username: username, password: password } })
        .then(function () {
          hideLogin();
          $('#login-pass').value = '';
          return boot();
        })
        .catch(function (err) {
          var box = $('#login-err');
          if (box) box.textContent = err.message || '登录失败';
        });
    });
  }

  /* ------------------------------------------------------------------ 路由 */
  var VIEWS = ['dashboard', 'extensions', 'sms', 'rules', 'settings', 'logs'];

  function route() {
    var hash = (location.hash || '#/dashboard').replace(/^#\/?/, '');
    S.view = VIEWS.indexOf(hash) >= 0 ? hash : 'dashboard';
    $$('.sidebar nav a').forEach(function (a) {
      a.classList.toggle('active', a.dataset.view === S.view);
    });
    render();
  }

  function render() {
    if (S.logsTimer) { clearInterval(S.logsTimer); S.logsTimer = null; }
    var box = $('#view');
    if (!box) return;
    if (S.view === 'dashboard') { box.innerHTML = dashboardHtml(); paintDashboard(); }
    else if (S.view === 'extensions') { box.innerHTML = extensionsHtml(); loadExtensions().then(renderExtensions); }
    else if (S.view === 'sms') { box.innerHTML = smsHtml(); loadSms(); }
    else if (S.view === 'rules') { box.innerHTML = rulesHtml(); loadRules().then(renderRules); }
    else if (S.view === 'settings') { box.innerHTML = settingsHtml(); loadSettings().then(fillSettings); }
    else if (S.view === 'logs') { box.innerHTML = logsHtml(); loadLogs(); }
  }

  /* ---------------------------------------------------------------- 顶栏 */
  function paintTopbar() {
    var st = S.status;
    if (!st) return;
    var modem = st.modem || {};
    var pill = $('#pill');
    if (pill) {
      var online = !!modem.present;
      pill.className = 'pill ' + (online ? 'online' : 'offline');
      pill.textContent = online ? '模块在线' : '模块离线';
    }
    var bars = Number(modem.signal_bars) || 0;
    $$('.signal i').forEach(function (el, idx) { el.classList.toggle('on', idx < bars); });
    var calls = $('#callcount');
    if (calls) calls.textContent = '通话中 ' + ((st.sip && st.sip.active_calls) || 0);
    var mem = $('#meminfo');
    if (mem) mem.textContent = '可用内存 ' + fmtMb((st.host && st.host.mem_avail_kb) || 0);
    var ver = $('#ver');
    if (ver) ver.textContent = 'v' + (st.version || '1.0.0');
  }

  /* ---------------------------------------------------------------- 概览 */
  function dashboardHtml() {
    return [
      '<div class="grid c4">',
      '<div class="card"><h3>可用内存</h3><div class="kpi" id="kpi-mem">-</div></div>',
      '<div class="card"><h3>系统负载</h3><div class="kpi" id="kpi-load">-</div></div>',
      '<div class="card"><h3>温度</h3><div class="kpi" id="kpi-temp">-</div></div>',
      '<div class="card"><h3>磁盘剩余</h3><div class="kpi" id="kpi-disk">-</div></div>',
      '</div>',
      '<div class="grid c2">',
      '<div class="card"><h3>EC20 模块</h3><div id="modem-body" class="muted">读取中…</div></div>',
      '<div class="card"><h3>拨号</h3>',
      '<div class="row"><input id="dial-input" placeholder="号码，例如 13800138000">',
      '<button class="btn primary" data-action="dial">呼叫</button></div>',
      '<div class="dialpad" style="margin-top:10px">',
      ['1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '0', '#'].map(function (d) {
        return '<button class="btn" data-action="pad" data-digit="' + d + '">' + d + '</button>';
      }).join(''),
      '</div>',
      '<p class="muted" style="margin:10px 0 0">SIP 分机拨外线：语音前缀（默认 9）+ 号码。</p>',
      '</div>',
      '</div>',
      '<div class="card"><h3>活动通话</h3><div id="calls-body" class="muted">无</div></div>',
      '<div class="card"><h3>SIP 概况</h3><div id="sip-body" class="muted">-</div></div>'
    ].join('');
  }

  function paintDashboard() {
    var st = S.status;
    if (!st) return;
    var host = st.host || {};
    if ($('#kpi-mem')) $('#kpi-mem').innerHTML = fmtMb(host.mem_avail_kb) + ' <small>/ ' + fmtMb(host.mem_total_kb) + '</small>';
    if ($('#kpi-load')) $('#kpi-load').textContent = host.load || '-';
    if ($('#kpi-temp')) $('#kpi-temp').textContent = (host.temp_c == null ? '-' : host.temp_c + ' ℃');
    if ($('#kpi-disk')) $('#kpi-disk').textContent = (host.disk_free_mb || 0) + ' MB';

    var m = st.modem || {};
    var body = $('#modem-body');
    if (body) {
      body.classList.remove('muted');
      var rows = [
        ['状态', m.present ? '<span class="tag ok">在线</span>' : '<span class="tag err">离线</span>'],
        ['型号', esc(m.model || '-')],
        ['固件', esc(m.firmware || '-')],
        ['SIM', esc(m.sim_state || '-')],
        ['运营商', esc(m.operator || '-')],
        ['信号', esc((m.signal_dbm || '-') + ' dBm')],
        ['注册', esc(m.reg_status || '-')],
        ['IMS', m.ims ? '<span class="tag ok">已注册</span>' : '<span class="tag warn">未注册</span>'],
        ['IMEI', esc(m.imei || '-')],
        ['ICCID', esc(m.iccid || '-')]
      ];
      body.innerHTML = rows.map(function (r) {
        return '<div class="row between" style="padding:2px 0"><span class="muted">' + r[0] +
          '</span><span>' + r[1] + '</span></div>';
      }).join('');
    }

    var calls = (m.calls || []).concat(st.calls || []);
    var callsBody = $('#calls-body');
    if (callsBody) {
      if (!calls.length) callsBody.textContent = '无活动通话';
      else {
        callsBody.classList.remove('muted');
        callsBody.innerHTML = '<table><thead><tr><th>方向</th><th>号码</th><th>状态</th><th>时长</th><th>操作</th></tr></thead><tbody>' +
          calls.map(function (c) {
            var number = c.number || c.to || '-';
            return '<tr><td>' + (c.dir === 'in' ? '来电' : '呼出') + '</td><td class="mono">' + esc(number) +
              '</td><td>' + esc(c.state) + '</td><td>' + fmtDur(c.duration) + '</td><td>' +
              '<button class="btn sm" data-action="answer" data-id="' + esc(c.id) + '">接听</button> ' +
              '<button class="btn sm danger" data-action="hangup" data-id="' + esc(c.id) + '">挂断</button></td></tr>';
          }).join('') + '</tbody></table>';
      }
    }

    var sip = st.sip || {};
    var sipBody = $('#sip-body');
    if (sipBody) {
      sipBody.classList.remove('muted');
      sipBody.innerHTML = [
        ['监听地址', esc(sip.listening || '-')],
        ['已注册分机', (sip.registrations || 0) + ' / ' + (sip.extensions || 0)],
        ['未读短信', (st.sms && st.sms.unread) || 0],
        ['运行时长', fmtUptime(st.uptime)]
      ].map(function (r) {
        return '<div class="row between"><span class="muted">' + r[0] + '</span><span>' + r[1] + '</span></div>';
      }).join('');
    }
  }

  /* ---------------------------------------------------------------- 分机 */
  function extensionsHtml() {
    return [
      '<div class="card"><h3>新增分机</h3><div class="row" style="align-items:flex-end">',
      '<label style="width:110px">分机号<input id="ext-number" placeholder="1001"></label>',
      '<label style="flex:1;min-width:120px">名称<input id="ext-name" placeholder="前台"></label>',
      '<label style="width:170px">密码（留空自动生成）<input id="ext-pass" placeholder="自动生成 8 位"></label>',
      '<label style="width:220px">振铃组（可为空）<input id="ext-ring" placeholder="1001&1002&1003"></label>',
      '<button class="btn primary" data-action="ext-add">添加</button></div>',
      '<p class="muted" style="margin:8px 0 0">振铃组：把几个分机用 <b>&amp;</b> 串起来，任何一个被叫（分机或 EC20 外线）时组成员一起振铃；' +
      '谁先接听谁通话，其余自动挂断/取消，不影响其他分机。</p></div>',
      '<div class="card"><h3>分机列表</h3><div id="ext-table" class="muted">加载中…</div></div>'
    ].join('');
  }

  function renderExtensions() {
    var box = $('#ext-table');
    if (!box) return;
    if (!S.ext.length) { box.textContent = '还没有分机，先添加一个。'; return; }
    box.classList.remove('muted');
    box.innerHTML = '<table><thead><tr><th>分机号</th><th>名称</th><th>密码</th><th>状态</th><th>振铃组</th><th>来源</th><th>操作</th></tr></thead><tbody>' +
      S.ext.map(function (e) {
        var shown = S.revealed[e.id] ? e.password : '••••••';
        var state = !e.enabled ? '<span class="tag err">停用</span>'
          : (e.registered ? '<span class="tag ok">在线</span>' : '<span class="tag">未注册</span>');
        var members = e.ring_members || [e.number];
        var ring = members.length > 1
          ? '<span class="tag ok">' + esc(members.join(' & ')) + '</span>'
          : '<span class="muted">-</span>';
        return '<tr><td class="mono">' + esc(e.number) + '</td><td>' + esc(e.display_name) + '</td>' +
          '<td class="mono">' + esc(shown) +
          ' <button class="btn sm ghost" data-action="ext-reveal" data-id="' + e.id + '">' + (S.revealed[e.id] ? '隐藏' : '显示') + '</button>' +
          ' <button class="btn sm ghost" data-action="ext-copy" data-id="' + e.id + '">复制</button></td>' +
          '<td>' + state + '</td><td>' + ring + '</td><td class="mono muted">' + esc(e.contact || '-') + '</td>' +
          '<td><button class="btn sm" data-action="ext-kick" data-id="' + e.id + '">踢下线</button> ' +
          '<button class="btn sm" data-action="ext-edit" data-id="' + e.id + '">编辑</button> ' +
          '<button class="btn sm danger" data-action="ext-del" data-id="' + e.id + '">删除</button></td></tr>';
      }).join('') + '</tbody></table>';
  }

  function openModal(html) {
    var body = $('#modalbody');
    var box = $('#modal');
    if (!body || !box) return;
    body.innerHTML = html;
    box.classList.remove('hidden');
  }
  function closeModal() {
    var box = $('#modal');
    if (box) box.classList.add('hidden');
  }

  function editExtension(id) {
    var e = S.ext.filter(function (x) { return String(x.id) === String(id); })[0];
    if (!e) return;
    openModal([
      '<h2>编辑分机 ' + esc(e.number) + '</h2>',
      '<div class="form-grid">',
      '<label>名称<input id="m-ext-name" value="' + esc(e.display_name) + '"></label>',
      '<label>新密码（留空不修改）<input id="m-ext-pass" placeholder="不修改"></label>',
      '<label>状态<select id="m-ext-enabled"><option value="1"' + (e.enabled ? ' selected' : '') + '>启用</option>' +
      '<option value="0"' + (e.enabled ? '' : ' selected') + '>停用</option></select></label>',
      '<label>呼叫转移至<input id="m-ext-forward" value="' + esc(e.forward_to || '') + '" placeholder="留空不转移"></label>',
      '<label style="grid-column:span 2">振铃组（用 &amp; 连接分机号，例如 1001&amp;1002&amp;1003）' +
      '<input id="m-ext-ring" value="' + esc(e.ring_group || '') + '" placeholder="留空表示只响本机"></label>',
      '</div>',
      '<div class="row end"><button class="btn ghost" data-action="modal-close">取消</button>',
      '<button class="btn primary" data-action="ext-save" data-id="' + e.id + '">保存</button></div>'
    ].join(''));
  }

  /* ---------------------------------------------------------------- 短信 */
  function smsHtml() {
    return [
      '<div class="grid c2">',
      '<div class="card"><h3>发送短信</h3>',
      '<div class="row"><input id="sms-to" placeholder="手机号" style="flex:1">',
      '<button class="btn primary" data-action="sms-send">发送</button></div>',
      '<textarea id="sms-text" rows="3" placeholder="短信内容" style="margin-top:10px"></textarea>',
      '<p class="muted" id="sms-count" style="margin:6px 0 0">0 字</p></div>',
      '<div class="card"><h3>筛选</h3>',
      '<div class="row"><input id="sms-q" placeholder="搜索号码或内容" style="flex:1">',
      '<label class="row" style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" id="sms-unread" style="width:auto"> 只看未读</label></div>',
      '<div class="row" style="margin-top:10px"><button class="btn" data-action="sms-search">搜索</button>',
      '<button class="btn" data-action="sms-readall">全部已读</button>',
      '<button class="btn ghost" data-action="sms-reload">刷新</button></div></div>',
      '</div>',
      '<div class="card"><h3>短信记录 <span class="muted" id="sms-total"></span></h3><div id="sms-list" class="muted">加载中…</div></div>'
    ].join('');
  }

  function renderSms() {
    var box = $('#sms-list');
    if (!box) return;
    var total = $('#sms-total');
    if (total) total.textContent = S.smsTotal ? '共 ' + S.smsTotal + ' 条' : '';
    if (!S.sms.length) { box.textContent = '没有短信'; return; }
    box.classList.remove('muted');
    box.innerHTML = S.sms.map(function (m) {
      var expanded = !!S.expanded[m.id];
      return '<div class="sms-item' + (m.read ? '' : ' unread') + (expanded ? '' : ' collapsed') +
        '" data-action="sms-toggle" data-id="' + m.id + '" style="margin-bottom:8px">' +
        '<div class="sms-head"><span class="mono">' + (m.direction === 'in' ? '← ' : '→ ') + esc(m.peer) + '</span>' +
        '<span>' + esc(m.ts) + (m.forwarded ? ' · <span class="tag ok">已转发</span>' : '') +
        (m.parts > 1 ? ' · ' + m.parts + '段' : '') + '</span></div>' +
        '<div class="sms-text">' + esc(m.text) + '</div>' +
        '<div class="row end" style="margin-top:8px">' +
        '<button class="btn sm" data-action="sms-read" data-id="' + m.id + '">标记已读</button>' +
        '<button class="btn sm danger" data-action="sms-del" data-id="' + m.id + '">删除</button></div></div>';
    }).join('');
  }

  function loadSms() {
    var params = new URLSearchParams();
    params.set('limit', '80');
    params.set('unread', S.filter.unread ? '1' : '0');
    if (S.filter.q) params.set('q', S.filter.q);
    return api('/api/sms?' + params.toString()).then(function (data) {
      S.sms = data.items || [];
      S.smsTotal = data.total || 0;
      renderSms();
    }).catch(function (err) { toast(err.message, 'err'); });
  }

  /* ---------------------------------------------------------------- 规则 */
  function rulesHtml() {
    return [
      '<div class="card"><h3 style="display:flex;justify-content:space-between;align-items:center">短信转发规则' +
      '<button class="btn sm" data-action="rule-new">新增规则</button></h3>',
      '<div id="rules-list" class="muted">加载中…</div></div>',
      '<div class="card"><h3>说明</h3><p class="muted">规则按顺序匹配，命中后把短信推送到所选渠道。' +
      '渠道需要先在「设置」里启用并测试通过。模板可用 {peer} {text} {time}。</p></div>'
    ].join('');
  }

  function renderRules() {
    var box = $('#rules-list');
    if (!box) return;
    if (!S.rules.length) { box.textContent = '还没有规则'; return; }
    box.classList.remove('muted');
    box.innerHTML = S.rules.map(function (r) {
      var channels = String(r.channels || '').split(',').filter(Boolean);
      var match = r.match_type === 'all' ? '全部短信'
        : (r.match_type === 'sender' ? '号码包含 ' + (r.pattern || '') : '关键词包含 ' + (r.pattern || ''));
      return '<div class="card" style="margin-bottom:10px;background:var(--bg3)">' +
        '<div class="row between"><strong>' + esc(r.name) + '</strong><div class="row">' +
        '<span class="tag ' + (r.enabled ? 'ok' : '') + '">' + (r.enabled ? '启用' : '停用') + '</span>' +
        channels.map(function (c) { return '<span class="tag">' + esc(c) + '</span>'; }).join('') +
        '</div></div><div class="muted" style="margin-top:6px">匹配：' + esc(match) + '</div>' +
        '<div class="row end" style="margin-top:8px">' +
        '<button class="btn sm" data-action="rule-test" data-id="' + r.id + '">测试</button> ' +
        '<button class="btn sm" data-action="rule-edit" data-id="' + r.id + '">编辑</button> ' +
        '<button class="btn sm danger" data-action="rule-del" data-id="' + r.id + '">删除</button></div></div>';
    }).join('');
  }

  function editRule(id) {
    var r = id ? S.rules.filter(function (x) { return String(x.id) === String(id); })[0]
      : { name: '', enabled: 1, match_type: 'all', pattern: '', channels: 'email', template: '' };
    if (!r) return;
    var channels = String(r.channels || '').split(',');
    openModal([
      '<h2>' + (id ? '编辑规则' : '新增规则') + '</h2>',
      '<div class="form-grid">',
      '<label>名称<input id="m-rule-name" value="' + esc(r.name) + '"></label>',
      '<label>匹配方式<select id="m-rule-match">' +
      '<option value="all"' + (r.match_type === 'all' ? ' selected' : '') + '>全部短信</option>' +
      '<option value="sender"' + (r.match_type === 'sender' ? ' selected' : '') + '>号码包含</option>' +
      '<option value="keyword"' + (r.match_type === 'keyword' ? ' selected' : '') + '>关键词包含</option></select></label>',
      '<label>号码/关键词<input id="m-rule-pattern" value="' + esc(r.pattern) + '" placeholder="多个用逗号分隔"></label>',
      '<label>状态<select id="m-rule-enabled"><option value="1"' + (r.enabled ? ' selected' : '') + '>启用</option>' +
      '<option value="0"' + (r.enabled ? '' : ' selected') + '>停用</option></select></label>',
      '</div>',
      '<div class="row" style="margin-top:10px"><span class="muted">渠道：</span>' +
      ['email', 'dingtalk', 'webhook'].map(function (c) {
        return '<label class="row" style="flex-direction:row;align-items:center;gap:6px">' +
          '<input type="checkbox" id="m-rule-c-' + c + '" style="width:auto"' +
          (channels.indexOf(c) >= 0 ? ' checked' : '') + '> ' + c + '</label>';
      }).join('') + '</div>',
      '<label style="margin-top:10px;display:flex">模板<textarea id="m-rule-template" rows="2">' + esc(r.template) + '</textarea></label>',
      '<div class="row end"><button class="btn ghost" data-action="modal-close">取消</button>',
      '<button class="btn primary" data-action="rule-save" data-id="' + (r.id || '') + '">保存</button></div>'
    ].join(''));
  }

  /* ---------------------------------------------------------------- 设置 */
  function settingsHtml() {
    return [
      '<div class="card"><h3>邮件通知（SMTP）</h3><div class="form-grid">',
      '<label>启用<select id="s-smtp-enabled"><option value="0">关闭</option><option value="1">开启</option></select></label>',
      '<label>SMTP 服务器<input id="s-smtp-host" placeholder="smtp.qq.com"></label>',
      '<label>端口<input id="s-smtp-port" placeholder="465"></label>',
      '<label>加密<select id="s-smtp-tls"><option value="ssl">SSL</option><option value="starttls">STARTTLS</option><option value="none">不加密</option></select></label>',
      '<label>账号<input id="s-smtp-user" placeholder="user@qq.com"></label>',
      '<label>密码<input id="s-smtp-pass" type="password" placeholder="留空不修改"></label>',
      '<label>发件人<input id="s-smtp-from" placeholder="user@qq.com"></label>',
      '</div><div class="row end" style="margin-top:10px"><button class="btn" data-action="test-email">发送测试</button>',
      '<button class="btn primary" data-action="save-smtp">保存</button></div></div>',

      '<div class="card"><h3>钉钉机器人</h3><div class="form-grid">',
      '<label>启用<select id="s-ding-enabled"><option value="0">关闭</option><option value="1">开启</option></select></label>',
      '<label>加签密钥<input id="s-ding-secret" type="password" placeholder="留空不修改"></label>',
      '<label style="grid-column:span 2">Webhook<input id="s-ding-webhook" placeholder="https://oapi.dingtalk.com/robot/send?access_token=..."></label>',
      '<label>@手机号<input id="s-ding-at" placeholder="13800138000"></label>',
      '</div><div class="row end" style="margin-top:10px"><button class="btn" data-action="test-dingtalk">发送测试</button>',
      '<button class="btn primary" data-action="save-dingtalk">保存</button></div></div>',

      '<div class="card"><h3>通用 Webhook</h3><div class="form-grid">',
      '<label>启用<select id="s-hook-enabled"><option value="0">关闭</option><option value="1">开启</option></select></label>',
      '<label style="grid-column:span 2">地址<input id="s-hook-url" placeholder="http://192.168.1.10:9000/sms"></label>',
      '</div><div class="row end" style="margin-top:10px"><button class="btn" data-action="test-webhook">发送测试</button>',
      '<button class="btn primary" data-action="save-webhook">保存</button></div></div>',

      '<div class="card"><h3>拨号与 SIP</h3><div class="form-grid">',
      '<label>语音前缀<input id="s-dial-prefix" placeholder="9"></label>',
      '<label>振铃时长（秒）<input id="s-dial-ring" placeholder="30"></label>',
      '<label>来电自动接听<select id="s-dial-auto"><option value="0">关闭</option><option value="1">开启</option></select></label>',
      '<label>来电统一转移至<input id="s-dial-forward" placeholder="留空则群振所有在线分机"></label>',
      '<label>SIP 域<input id="s-sip-realm" placeholder="ec20sip.local"></label>',
      '<label>对外 IP<input id="s-sip-ip" placeholder="192.168.1.188"></label>',
      '<label>SIP 端口<input id="s-sip-port" placeholder="5060"></label>',
      '</div><div class="row end" style="margin-top:10px"><button class="btn primary" data-action="save-dial">保存</button></div></div>',

      '<div class="card"><h3>管理账号</h3>',
      '<div class="row" style="margin-bottom:10px;padding:8px 10px;background:var(--bg3);border-radius:8px">' +
      '<label style="flex-direction:row;align-items:center;gap:8px;flex:1">' +
      '<input type="checkbox" id="s-auth-enabled" style="width:auto"> 面板需要登录' +
      '</label><span class="muted">取消勾选 = 局域网免密直接进面板</span></div>',
      '<div class="form-grid">',
      '<label>用户名<input id="s-auth-user" placeholder="admin"></label>',
      '<label>新密码<input id="s-auth-pass" type="password" placeholder="留空不修改"></label>',
      '</div><div class="row end" style="margin-top:10px"><button class="btn primary" data-action="save-auth">保存</button></div></div>'
    ].join('');
  }

  function fillSettings() {
    var c = S.settings;
    if (!c) return;
    function set(id, value) {
      var el = $('#' + id);
      if (el && value !== undefined && value !== null) el.value = value;
    }
    var smtp = c.smtp || {};
    set('s-smtp-enabled', smtp.enabled ? '1' : '0');
    set('s-smtp-host', smtp.host || '');
    set('s-smtp-port', smtp.port || '');
    set('s-smtp-tls', smtp.tls || 'ssl');
    set('s-smtp-user', smtp.user || '');
    set('s-smtp-from', smtp.from || '');
    var pass = $('#s-smtp-pass');
    if (pass) pass.placeholder = smtp.pass_set ? '已设置（留空不修改）' : '未设置';

    var ding = c.dingtalk || {};
    set('s-ding-enabled', ding.enabled ? '1' : '0');
    set('s-ding-webhook', ding.webhook || '');
    set('s-ding-at', ding.at_mobiles || '');
    var secret = $('#s-ding-secret');
    if (secret) secret.placeholder = ding.secret_set ? '已设置（留空不修改）' : '未设置';

    var hook = c.webhook || {};
    set('s-hook-enabled', hook.enabled ? '1' : '0');
    set('s-hook-url', hook.url || '');

    var dial = c.dial || {};
    set('s-dial-prefix', dial.voice_prefix || '9');
    set('s-dial-ring', dial.ring_seconds || 30);
    set('s-dial-auto', dial.auto_answer ? '1' : '0');
    set('s-dial-forward', dial.forward_all_to || '');

    var sip = c.sip || {};
    set('s-sip-realm', sip.realm || '');
    set('s-sip-ip', sip.external_ip || '');
    set('s-sip-port', sip.port || 5060);

    set('s-auth-user', (c.auth && c.auth.username) || 'admin');
    var enabledBox = $('#s-auth-enabled');
    if (enabledBox) enabledBox.checked = !!(c.auth && Number(c.auth.enabled));
  }

  function val(id) { var el = $('#' + id); return el ? el.value.trim() : ''; }
  function rawVal(id) { var el = $('#' + id); return el ? el.value : ''; }

  /* ---------------------------------------------------------------- 日志 */
  function logsHtml() {
    return [
      '<div class="card"><h3>运行日志</h3><div class="row">',
      '<label class="row" style="flex-direction:row;align-items:center;gap:6px">' +
      '<input type="checkbox" id="log-auto" checked style="width:auto"> 自动刷新</label>',
      '<button class="btn" data-action="logs-refresh">刷新</button></div>',
      '<div id="logbox" class="logbox" style="margin-top:10px"></div></div>'
    ].join('');
  }

  function loadLogs() {
    return api('/api/logs?tail=300').then(function (data) {
      S.logs = data.lines || [];
      renderLogs();
    }).catch(function (err) { toast(err.message, 'err'); });
  }

  function renderLogs() {
    var box = $('#logbox');
    if (!box) return;
    box.innerHTML = S.logs.map(function (line) {
      var level = / ERROR /.test(line) ? 'ERROR' : (/ WARNING /.test(line) ? 'WARNING'
        : (/ DEBUG /.test(line) ? 'DEBUG' : 'INFO'));
      return '<div class="lv-' + level + '">' + esc(line) + '</div>';
    }).join('');
    box.scrollTop = box.scrollHeight;
    var auto = $('#log-auto');
    if (auto && auto.checked && !S.logsTimer) S.logsTimer = setInterval(loadLogs, 5000);
    if (auto && !auto.checked && S.logsTimer) { clearInterval(S.logsTimer); S.logsTimer = null; }
  }

  /* ------------------------------------------------------------ 数据加载 */
  function loadExtensions() { return api('/api/extensions').then(function (d) { S.ext = d.items || []; }); }
  function loadRules() { return api('/api/rules').then(function (d) { S.rules = d.items || []; }); }
  function loadSettings() { return api('/api/settings').then(function (d) { S.settings = d; }); }

  function pollStatus(verbose) {
    return api('/api/status').then(function (data) {
      S.status = data;
      paintTopbar();
      if (S.view === 'dashboard') paintDashboard();
      if (verbose) toast('状态已更新', 'ok');
    }).catch(function (err) {
      if (verbose) toast(err.message, 'err');
    });
  }

  /* ------------------------------------------------------------ 事件处理 */
  document.addEventListener('click', function (ev) {
    var el = ev.target.closest('[data-action]');
    if (!el) return;
    var action = el.dataset.action;
    var id = el.dataset.id;

    function fail(err) { toast(err.message || '操作失败', 'err'); }

    switch (action) {
      case 'refresh': pollStatus(true); break;
      case 'logout':
        api('/api/logout', { method: 'POST' }).catch(function () {}).then(function () { showLogin(); });
        break;
      case 'modal-close': closeModal(); break;

      case 'pad':
        S.dial = (S.dial || '') + el.dataset.digit;
        if ($('#dial-input')) $('#dial-input').value = S.dial;
        break;

      case 'dial':
        var number = val('dial-input');
        if (!number) { toast('请输入号码', 'err'); break; }
        api('/api/dial', { method: 'POST', body: { number: number } })
          .then(function () { toast('正在拨号 ' + number, 'ok'); return pollStatus(); })
          .catch(fail);
        break;

      case 'hangup': api('/api/calls/' + id + '/hangup', { method: 'POST' }).then(pollStatus).catch(fail); break;
      case 'answer': api('/api/calls/' + id + '/answer', { method: 'POST' }).then(pollStatus).catch(fail); break;

      case 'ext-add':
        var num = val('ext-number');
        if (!/^\d{2,8}$/.test(num)) { toast('分机号需 2-8 位数字', 'err'); break; }
        api('/api/extensions', { method: 'POST', body: {
          number: num, display_name: val('ext-name'), password: rawVal('ext-pass'),
          ring_group: val('ext-ring'), enabled: 1 } })
          .then(function (res) {
            toast('已创建分机 ' + num + '，密码 ' + (res.password || ''), 'ok');
            return loadExtensions();
          })
          .then(function () { render(); })
          .catch(fail);
        break;

      case 'ext-reveal': S.revealed[id] = !S.revealed[id]; renderExtensions(); break;
      case 'ext-copy':
        var item = S.ext.filter(function (x) { return String(x.id) === String(id); })[0];
        if (item && navigator.clipboard) navigator.clipboard.writeText(item.password).then(function () { toast('密码已复制', 'ok'); });
        break;
      case 'ext-edit': editExtension(id); break;
      case 'ext-save':
        var body = {
          display_name: val('m-ext-name'),
          enabled: Number(val('m-ext-enabled')),
          forward_to: val('m-ext-forward'),
          ring_group: val('m-ext-ring')
        };
        var pass2 = rawVal('m-ext-pass');
        if (pass2) body.password = pass2;
        api('/api/extensions/' + id, { method: 'PUT', body: body })
          .then(function () { closeModal(); toast('已保存', 'ok'); return loadExtensions(); })
          .then(function () { render(); })
          .catch(fail);
        break;
      case 'ext-kick':
        api('/api/extensions/' + id + '/kick', { method: 'POST' })
          .then(function () { toast('已踢下线', 'ok'); return loadExtensions(); })
          .then(function () { render(); })
          .catch(fail);
        break;
      case 'ext-del':
        if (!confirm('确认删除该分机？')) break;
        api('/api/extensions/' + id, { method: 'DELETE' })
          .then(function () { toast('已删除', 'ok'); return loadExtensions(); })
          .then(function () { render(); })
          .catch(fail);
        break;

      case 'sms-send':
        var to = val('sms-to'), text = rawVal('sms-text');
        if (!to || !text) { toast('号码和内容都要填', 'err'); break; }
        api('/api/sms/send', { method: 'POST', body: { to: to, text: text } })
          .then(function () {
            toast('短信已提交', 'ok');
            if ($('#sms-text')) $('#sms-text').value = '';
            return loadSms();
          })
          .catch(fail);
        break;
      case 'sms-search':
        S.filter.q = val('sms-q');
        S.filter.unread = !!($('#sms-unread') && $('#sms-unread').checked);
        loadSms();
        break;
      case 'sms-reload': loadSms(); break;
      case 'sms-readall': api('/api/sms/read_all', { method: 'POST' }).then(function () { loadSms(); pollStatus(); }).catch(fail); break;
      case 'sms-read': api('/api/sms/' + id + '/read', { method: 'POST' }).then(loadSms).catch(fail); break;
      case 'sms-del': api('/api/sms/' + id, { method: 'DELETE' }).then(loadSms).catch(fail); break;
      case 'sms-toggle': S.expanded[id] = !S.expanded[id]; renderSms(); break;

      case 'rule-new': editRule(null); break;
      case 'rule-edit': editRule(id); break;
      case 'rule-save':
        var channels = ['email', 'dingtalk', 'webhook'].filter(function (c) {
          var box = $('#m-rule-c-' + c);
          return box && box.checked;
        });
        var ruleBody = {
          name: val('m-rule-name') || '规则',
          match_type: val('m-rule-match'),
          pattern: val('m-rule-pattern'),
          channels: channels.join(','),
          template: rawVal('m-rule-template'),
          enabled: Number(val('m-rule-enabled'))
        };
        var req = id ? api('/api/rules/' + id, { method: 'PUT', body: ruleBody })
          : api('/api/rules', { method: 'POST', body: ruleBody });
        req.then(function () { closeModal(); toast('规则已保存', 'ok'); return loadRules(); })
          .then(function () { render(); })
          .catch(fail);
        break;
      case 'rule-del':
        if (!confirm('删除该规则？')) break;
        api('/api/rules/' + id, { method: 'DELETE' })
          .then(function () { toast('已删除', 'ok'); return loadRules(); })
          .then(function () { render(); })
          .catch(fail);
        break;
      case 'rule-test':
        api('/api/rules/' + id + '/test', { method: 'POST' })
          .then(function (res) { toast('测试结果：' + JSON.stringify(res.results || []), 'ok'); })
          .catch(fail);
        break;

      case 'save-smtp':
        api('/api/settings', { method: 'PUT', body: { smtp: {
          enabled: Number(val('s-smtp-enabled')), host: val('s-smtp-host'), port: val('s-smtp-port'),
          tls: val('s-smtp-tls'), user: val('s-smtp-user'), password: rawVal('s-smtp-pass'),
          from: val('s-smtp-from') } } })
          .then(function () { toast('SMTP 已保存', 'ok'); return loadSettings(); })
          .then(fillSettings).catch(fail);
        break;
      case 'save-dingtalk':
        api('/api/settings', { method: 'PUT', body: { dingtalk: {
          enabled: Number(val('s-ding-enabled')), webhook: val('s-ding-webhook'),
          secret: rawVal('s-ding-secret'), at_mobiles: val('s-ding-at') } } })
          .then(function () { toast('钉钉已保存', 'ok'); return loadSettings(); })
          .then(fillSettings).catch(fail);
        break;
      case 'save-webhook':
        api('/api/settings', { method: 'PUT', body: { webhook: {
          enabled: Number(val('s-hook-enabled')), url: val('s-hook-url') } } })
          .then(function () { toast('Webhook 已保存', 'ok'); return loadSettings(); })
          .then(fillSettings).catch(fail);
        break;
      case 'save-dial':
        api('/api/settings', { method: 'PUT', body: {
          dial: { voice_prefix: val('s-dial-prefix'), ring_seconds: val('s-dial-ring'),
                  auto_answer: Number(val('s-dial-auto')), forward_all_to: val('s-dial-forward') },
          sip: { realm: val('s-sip-realm'), external_ip: val('s-sip-ip'), port: val('s-sip-port') } } })
          .then(function () { toast('拨号 / SIP 已保存', 'ok'); return loadSettings(); })
          .then(fillSettings).catch(fail);
        break;
      case 'save-auth':
        var authBody = { auth: {
          username: val('s-auth-user'),
          enabled: ($('#s-auth-enabled') && $('#s-auth-enabled').checked) ? 1 : 0
        } };
        var newPass = rawVal('s-auth-pass');
        if (newPass) authBody.auth.password = newPass;
        api('/api/settings', { method: 'PUT', body: authBody })
          .then(function () {
            toast(authBody.auth.enabled ? '账号已保存，下次打开需要登录' : '已关闭登录，面板免密访问', 'ok');
            if (!authBody.auth.enabled) setTimeout(function () { location.reload(); }, 800);
            return loadSettings();
          })
          .then(fillSettings)
          .catch(fail);
        break;

      case 'test-email':
        api('/api/settings/test_email', { method: 'POST', body: {} })
          .then(function (r) { toast('邮件：' + (r.detail || (r.ok ? '成功' : '失败')), r.ok ? 'ok' : 'err'); })
          .catch(fail);
        break;
      case 'test-dingtalk':
        api('/api/settings/test_dingtalk', { method: 'POST' })
          .then(function (r) { toast('钉钉：' + (r.detail || (r.ok ? '成功' : '失败')), r.ok ? 'ok' : 'err'); })
          .catch(fail);
        break;
      case 'test-webhook':
        api('/api/settings/test_webhook', { method: 'POST' })
          .then(function (r) { toast('Webhook：' + (r.detail || (r.ok ? '成功' : '失败')), r.ok ? 'ok' : 'err'); })
          .catch(fail);
        break;

      case 'logs-refresh': loadLogs(); break;
      default: break;
    }
  });

  document.addEventListener('input', function (ev) {
    if (ev.target.id === 'sms-text') {
      var len = ev.target.value.length;
      var parts = len > 70 ? Math.ceil(len / 67) : 1;
      var counter = $('#sms-count');
      if (counter) counter.textContent = len + ' 字 · ' + parts + ' 条';
    }
    if (ev.target.id === 'dial-input') S.dial = ev.target.value;
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') closeModal();
    if (ev.key === 'Enter' && ev.target.id === 'dial-input') {
      ev.preventDefault();
      var btn = document.querySelector('[data-action="dial"]');
      if (btn) btn.click();
    }
  });

  document.addEventListener('change', function (ev) {
    if (ev.target.id === 'log-auto') {
      if (ev.target.checked) { if (!S.logsTimer) S.logsTimer = setInterval(loadLogs, 5000); }
      else if (S.logsTimer) { clearInterval(S.logsTimer); S.logsTimer = null; }
    }
  });

  document.addEventListener('visibilitychange', function () { if (!document.hidden) pollStatus(); });

  /* ---------------------------------------------------------------- 启动 */
  function startPolling() {
    if (S.pollTimer) clearInterval(S.pollTimer);
    S.pollTimer = setInterval(function () { if (!document.hidden) pollStatus(); }, 5000);
  }

  function boot() {
    return pollStatus().then(function () {
      return Promise.all([loadExtensions(), loadRules(), loadSettings()]);
    }).then(function () {
      route();
    });
  }

  window.addEventListener('hashchange', route);
  bindLogin();

  // layout diagnostics: open the panel with ?debug=1 to see the widths in the title
  if (location.search.indexOf('debug=1') >= 0) {
    setTimeout(function () {
      var doc = document.documentElement;
      var wide = [];
      $$('#view *').forEach(function (el) {
        var r = el.getBoundingClientRect();
        if (r.right > window.innerWidth + 1) {
          wide.push((el.id || el.className || el.tagName) + ':' + Math.round(r.right));
        }
      });
      document.title = 'vw=' + window.innerWidth + ' sw=' + doc.scrollWidth + ' overflow=' + (wide.slice(0, 4).join(',') || 'none');
    }, 2500);
  }

  api('/api/status').then(function (data) {
    S.status = data;
    hideLogin();
    return boot();
  }).then(startPolling).catch(function () { showLogin(); });
})();