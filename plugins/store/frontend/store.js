/* 插件商店页逻辑（零构建原生 JS；?v=1 起版，改动须递增引用处）。
 *
 * 数据：GET /plugin/store/api/catalog?all=1（服务器会话鉴权）。
 * 2026-10-11 需求①②：浏览器业务面已退役（壳身份门），本页只在桌面端壳内出现——
 * 桌面端（html.jz-shell）：本地状态经 /local-api/plugins 取得，安装/生命周期操作走
 * /local-api/store/* 与 /local-api/plugins/*（同源，永不中继）；浏览器下载分支已删除。
 * 每插件一张卡：C 类的配对服务器工件条目由商店投影去重（不出卡，管理面可见）。
 */
(function () {
  'use strict';

  // 壳内自判（批次 10）：本页经工具壳 iframe 内嵌 = 桌面端 → 给 <html> 打 jz-shell，
  // 激活 .jz-only 操作列（安装/下载包）。保留自判兜底（防误部署到未启门的环境时按钮全灭）。
  try {
    if (window.self !== window.top) {
      document.documentElement.classList.add('jz-shell');
    }
  } catch (e) {
    document.documentElement.classList.add('jz-shell');   // 跨域取不到 top 视同壳内
  }

  var CATALOG_API = '/plugin/store/api/catalog?all=1';
  var IN_SHELL = document.documentElement.classList.contains('jz-shell');
  var catalog = [];              // flat 条目（含 versions 历史）
  var localState = { plugins: {}, frontends: {}, components: {}, loaded: false };
  var installs = null;           // 账号安装登记（§2.1，2026-10-11 需求①）；null=旧服务器无该 API（降级机器级行为）

  function $(s, el) { return (el || document).querySelector(s); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  var _toastTimer = null;
  function msg(t, isErr) {
    // 浮层 Toast（批次 10 优化）：卡片操作反馈不再写页签行（滚动后不可见即"无反馈"）
    var el = $('#st-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'st-toast';
      document.body.appendChild(el);
    }
    el.textContent = t || '';
    el.classList.toggle('err', !!isErr);
    el.classList.toggle('show', !!t);
    if (_toastTimer) clearTimeout(_toastTimer);
    if (t) _toastTimer = setTimeout(function () { el.classList.remove('show'); }, isErr ? 8000 : 4000);
  }
  // 自绘对话框 = 框架级组件（static/js/jz-dialog.js，样式随脚本注入，随用随调；2026-10-08 解耦）
  var stDialog = function (opts) { return window.JZDialog.open(opts); };

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
    return n + ' B';
  }
  function verNums(v) {
    return String(v || '').split('.').map(function (x) { return parseInt(x, 10) || 0; });
  }
  function verGt(a, b) {          // a > b
    var x = verNums(a), y = verNums(b);
    for (var i = 0; i < Math.max(x.length, y.length); i++) {
      var d = (x[i] || 0) - (y[i] || 0);
      if (d) return d > 0;
    }
    return false;
  }
  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      // 非 JSON 响应（如 500 HTML 错误页）也给出可读错误——2026-10-08：重启端点曾因
      // NameError 返回 500 HTML，被 r.json() 解析异常吞成「请求失败」，真因不可见。
      return r.text().then(function (t) {
        try { return JSON.parse(t); } catch (e) {
          return { ok: false, error: 'HTTP ' + r.status + '（服务端响应不可解析）' };
        }
      });
    });
  }
  function humanRuntime(e) {
    // 徽标拆分（2026-10-11 需求⑤）：server=在线；local 按 server_data 判——
    // C 类客户端工件（server_data=true）→「离线降级」，B 类纯本地 →「本地」；dep-slot →「依赖」。
    if (e.runtime === 'server') return '在线';
    if (e.runtime === 'dep-slot') return '依赖';
    return e.server_data ? '离线降级' : '本地';
  }
  function runtimeClass(rt) {
    return rt === 'server' ? 'server' : rt === 'dep-slot' ? 'dep' : 'local';
  }

  // ---------- 本地状态（仅桌面端） ----------
  function loadLocalState() {
    if (!IN_SHELL) { localState.loaded = true; return Promise.resolve(); }
    return fetch('/local-api/plugins').then(function (r) { return r.json(); }).then(function (d) {
      var plugins = {}, frontends = {}, components = {};
      (d.plugins || []).forEach(function (p) {
        plugins[p.id] = { version: p.version, enabled: p.enabled !== false, kind: p.kind,
                          server_data: !!p.server_data };
      });
      (d.server_frontends || []).forEach(function (f) {
        frontends[f.id] = { version: f.version, enabled: f.enabled !== false };
      });
      (d.components || []).forEach(function (c) {
        components[c.id] = { version: c.version, pending: !!c.pending };
      });
      localState = { plugins: plugins, frontends: frontends, components: components,
                     loaded: true };
    }).catch(function () { localState.loaded = true; });
  }

  // ---------- 账号安装登记（2026-10-11 需求①，§2.1） ----------
  function loadInstalls() {
    if (!IN_SHELL) return Promise.resolve();
    return fetch('/plugin/store/api/installs').then(function (r) { return r.json(); })
      .then(function (d) { installs = (d && d.installs) || {}; })
      .catch(function () { installs = null; });   // 旧服务器无该 API → 降级机器级行为
  }

  // ---------- 渲染 ----------
  function visibleEntries(cat) {
    var q = ($('#st-q').value || '').trim().toLowerCase();
    return catalog.filter(function (e) {
      var isDep = e.runtime === 'dep-slot';
      if (cat === 'dep' ? !isDep : isDep) return false;
      if (!q) return true;
      return (String(e.name) + ' ' + String(e.description || '') + ' ' + String(e.id))
        .toLowerCase().indexOf(q) >= 0;
    });
  }

  function requiresText(e) {
    var rows = (e.requires && e.requires.python_packages) || [];
    if (!rows.length) return '';
    return rows.map(function (d) {
      return esc(d.name) + (d.version ? ' ' + esc(d.version) : '');
    }).join('、');
  }

  function depResolutionState(e) {
    // 返回本条依赖的安装态：{installed, upgradable, entry}
    var comp = localState.components[e.id];
    var st = { installed: !!comp && !comp.pending, upgradable: false, entry: comp || null };
    if (comp && verGt(e.version, comp.version)) st.upgradable = true;
    return st;
  }

  function pluginInstallState(e) {
    if (e.runtime === 'dep-slot') return depResolutionState(e);
    var map = e.runtime === 'server' ? localState.frontends : localState.plugins;
    var inst = map[e.id];
    var st = { account: !!installs, registered: false, installed: false, upgradable: false,
               disabled: false, copy_missing: false, entry: inst || null };
    if (installs) {
      // 账号登记三态（D10）：未装→安装；已装+副本在→卸载+启停；已装+副本缺→安装到本机
      var rec = installs[e.id];
      st.registered = !!rec;
      st.installed = !!rec && !!inst && inst.enabled !== false;
      st.disabled = !!rec && rec.enabled === false;
      st.copy_missing = !!rec && !inst;
      if (rec && verGt(e.version, rec.version)) st.upgradable = true;
      return st;
    }
    // 降级（旧服务器，G2-7）：机器级行为不变
    st.installed = !!inst && inst.enabled !== false;
    st.disabled = !!inst && inst.enabled === false;
    if (inst && verGt(e.version, inst.version)) st.upgradable = true;
    return st;
  }

  function dlButtonHtml(e, label) {
    // 下载按钮 = 进度条容器：fill 随百分比填充背景，label 叠在上层
    return '<button class="st-btn-dl jz-only" data-act="download-local" data-id="' + esc(e.id)
      + '" data-ver="' + esc(e.version) + '" data-label="' + esc(label || '下载') + '">'
      + '<span class="st-dl-fill"></span>'
      + '<span class="st-dl-label">' + esc(label || '下载') + '</span></button>';
  }

  function installBtnHtml(e, label) {
    // 安装按钮 = 按钮内进度条容器（2026-10-08）：fill 随服务端安装台账 pct 填充，label 叠上层
    return '<button class="st-btn-install st-btn-prog" data-act="install" data-id="' + esc(e.id)
      + '" data-label="' + esc(label) + '">'
      + '<span class="st-dl-fill"></span><span class="st-dl-label">' + esc(label) + '</span></button>';
  }

  function buttonsHtml(e) {
    var st = pluginInstallState(e);
    var html = '';
    if (IN_SHELL && e.runtime === 'dep-slot') {
      // 依赖组件是机器能力（D6）：装/卸/升级仍按机器态，不进账号登记
      if (!st.installed) {
        html += installBtnHtml(e, '安装');
      } else if (st.upgradable) {
        html += installBtnHtml(e, '升级到 ' + e.version);
      } else {
        html += '<button class="st-btn-install" disabled title="依赖组件已在本机生效">已安装</button>';
      }
      if (st.installed || st.entry) {
        html += '<button class="st-btn-danger" data-act="uninstall-dep" data-id="' + esc(e.id) + '">卸载</button>';
      }
    }
    if (IN_SHELL && e.runtime === 'server') {
      // A 类（在线）：账号登记三态（D10）；副本缺=云卡同款「安装到本机」链
      if (!st.registered) {
        html += installBtnHtml(e, '安装');
      } else if (st.copy_missing) {
        html += installBtnHtml(e, '安装到本机');
      } else if (st.upgradable) {
        html += installBtnHtml(e, '升级到 ' + e.version);
      }
      if (st.registered && !st.copy_missing) {
        html += '<button class="st-btn-warn" data-act="toggle" data-id="' + esc(e.id)
          + '" data-en="' + (st.disabled ? '1' : '0') + '" data-account="1">'
          + (st.disabled ? '启用' : '停用') + '</button>';
        html += '<button class="st-btn-danger" data-act="uninstall" data-id="' + esc(e.id) + '">卸载</button>';
      }
    }
    if (IN_SHELL && e.runtime === 'local') {
      // B/C 类：账号登记三态（D10）
      if (!st.registered) {
        html += installBtnHtml(e, '安装');
      } else if (st.copy_missing) {
        html += installBtnHtml(e, '安装到本机');
      } else if (st.upgradable) {
        html += installBtnHtml(e, '升级到 ' + e.version);
      }
      if (st.registered && !st.copy_missing) {
        html += '<button class="st-btn-warn" data-act="toggle" data-id="' + esc(e.id)
          + '" data-en="' + (st.disabled ? '1' : '0') + '" data-account="1">'
          + (st.disabled ? '启用' : '停用') + '</button>';
        html += '<button class="st-btn-danger" data-act="uninstall" data-id="' + esc(e.id) + '">卸载</button>';
      }
    }
    // 下载（2026-10-08 需求：**全部插件**都提供「安装」「下载」两个按钮，按钮上不写说明）——
    // 桌面端「下载」= 仅下载不安装（能力层落数据目录 downloads\）；server 条目取**客户端工件
    // （前端包）**，后端包不对用户暴露。浏览器下载分支已删除（2026-10-11 需求①）。
    html += dlButtonHtml(e, '下载');
    return html;
  }

  // ---------- 详情悬浮窗（点击卡片打开；ESC/遮罩/× 关闭） ----------
  var _modal = null;
  function closeModal() {
    if (_modal) _modal.classList.remove('show');
  }
  function openDetail(e) {
    if (!_modal) {
      _modal = document.createElement('div');
      _modal.className = 'st-modal';
      _modal.innerHTML = '<div class="st-modal-panel" role="dialog" aria-modal="true">'
        + '<button class="st-modal-x" aria-label="关闭">×</button>'
        + '<div class="st-modal-body"></div></div>';
      _modal.addEventListener('click', function (ev) {
        if (ev.target === _modal || ev.target.className === 'st-modal-x') closeModal();
      });
      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape') closeModal();
      });
      document.body.appendChild(_modal);
    }
    var st = pluginInstallState(e);
    var rows = [];
    rows.push('<div class="st-md-head"><span class="st-icon st-md-icon">' + esc(e.icon || '🧩')
      + '</span><div><p class="st-name">' + esc(e.name) + '</p>'
      + '<div class="st-badges"><span class="st-badge ' + runtimeClass(e.runtime) + '">'
      + humanRuntime(e) + '</span>'
      + (e.deps_mode === 'venv' ? '<span class="st-badge venv">独立环境</span>' : '')
      + (IN_SHELL && st.installed && !st.upgradable ? '<span class="st-badge local">已安装 v'
        + esc((st.entry || {}).version || '') + '</span>' : '')
      + (IN_SHELL && st.copy_missing ? '<span class="st-badge venv">未装到本机</span>' : '')
      + (st.upgradable ? '<span class="st-badge up">可升级</span>' : '') + '</div></div></div>');
    rows.push('<dl class="st-md-list">'
      + '<dt>版本</dt><dd>v' + esc(e.version) + '（' + fmtSize(e.size) + '）</dd>'
      + '<dt>发布时间</dt><dd>' + esc(e.published_at || '—') + '</dd>'
      + '<dt>条目 ID</dt><dd>' + esc(e.id) + '</dd>');
    if (e.runtime === 'server' && e.frontend_file) {
      rows.push('<dt>前端包</dt><dd>' + esc(e.frontend_file) + '（' + fmtSize(e.frontend_size) + '）</dd>');
    }
    rows.push('<dt>依赖版本声明</dt><dd>' + (requiresText(e) || '—') + '</dd></dl>');
    if (e.changelog) rows.push('<h4>变更说明</h4><p class="st-md-text">' + esc(e.changelog) + '</p>');
    var hist = (e.versions || []).map(function (v) {
      return v.status === 'published'
        ? '<li><b>v' + esc(v.version) + '</b> <span class="st-md-muted">' + esc(v.published_at || '')
          + '</span>' + (v.changelog ? '<br><span class="st-md-text">' + esc(v.changelog) + '</span>' : '')
        : '<li><b>v' + esc(v.version) + '</b> <span class="st-md-muted">（' + esc(v.status) + '，不可见）</span>';
    });
    if (hist.length) rows.push('<h4>版本历史</h4><ul class="st-md-hist">' + hist.join('') + '</ul>');
    _modal.querySelector('.st-modal-body').innerHTML = rows.join('');
    _modal.classList.add('show');
  }

  function render() {
    var cat = $('.st-tab.active').getAttribute('data-cat');
    var grid = $('#st-grid');
    var list = visibleEntries(cat);
    grid.innerHTML = '';
    if (!list.length) {
      grid.innerHTML = '<div class="st-empty">' + (catalog.length ? '没有匹配的条目' : '商店暂无上架条目') + '</div>';
      return;
    }
    list.forEach(function (e, idx) {
      var st = pluginInstallState(e);
      var badges = '<span class="st-badge ' + runtimeClass(e.runtime) + '">' + humanRuntime(e) + '</span>'
        + (e.deps_mode === 'venv' ? '<span class="st-badge venv">独立环境</span>' : '')
        + (IN_SHELL && st.installed && !st.upgradable ? '<span class="st-badge local">已安装</span>' : '')
        + (IN_SHELL && st.copy_missing ? '<span class="st-badge venv">未装到本机</span>' : '')
        + (st.upgradable ? '<span class="st-badge up">可升级</span>' : '')
        + (st.disabled ? '<span class="st-badge venv">已停用</span>' : '')
        // V5 数据出机明示：server_data 条目（本地数据会发送到服务器）常显徽标
        + ((e.server_data || (st.entry && st.entry.server_data))
           ? '<span class="st-badge dep" title="该插件会把必要数据发送到服务器（server_data 明示）">数据出机</span>'
           : '');
      var card = document.createElement('div');
      card.className = 'st-card st-rt-' + runtimeClass(e.runtime);
      card.style.animationDelay = Math.min(idx * 35, 350) + 'ms';
      card.setAttribute('data-id', e.id);
      card.innerHTML = ''
        + '<div class="st-card-body">'
        +   '<div class="st-card-top">'
        +     '<span class="st-icon">' + esc(e.icon || '🧩') + '</span>'
        +     '<div class="st-title"><p class="st-name">' + esc(e.name) + '</p>'
        +       '<div class="st-badges">' + badges + '</div>'
        +     '</div>'
        +   '</div>'
        +   '<p class="st-desc">' + esc(e.description || '') + '</p>'
        +   '<div class="st-meta">v' + esc(e.version) + ' · '
        // server 条目用户只能装前端副本（后端包不提供下载）→ 只显示安装包体积，不露"前端/后端"分层
        +   fmtSize(e.runtime === 'server' && e.frontend_size ? e.frontend_size : e.size)
        +     (st.entry && st.entry.version && verGt(e.version, st.entry.version)
                ? ' · 本机 v' + esc(st.entry.version) : '')
        +   '<span class="st-more">详情 ›</span></div>'
        + '</div>'
        + '<div class="st-card-side">' + buttonsHtml(e) + '</div>';
      grid.appendChild(card);
    });
  }

  // ---------- 操作 ----------
  function findEntry(id) {
    return catalog.filter(function (e) { return e.id === id; })[0] || null;
  }

  async function preflightAndInstall(e, btn) {
    if (!IN_SHELL) { return; }
    var wantDeps = false;
    msg('正在预检依赖 …');
    var pre = { installable: true, deps: [] };
    try {
      pre = (await post('/local-api/store/preflight', { id: e.id, version: e.version })) || pre;
    } catch (err) { /* 网络异常：pre 保持默认，安装请求仍会给出可读错误 */ }
    // 预检不可读（本地壳自证 403 / 会话失效 401 / 条目不存在等）：**显式报错并终止**——
    // 2026-10-12 真机报告：浏览器直开本地端口时预检被 403 HTML 打中，错误被静默吞掉，
    // 界面永远停在「正在预检依赖…」。D-8 的"提示不阻断"只适用于"依赖评估"失败，
    // 不适用于"预检本身没拿到结果"。
    if (!pre || pre.error) {
      msg('预检失败：' + ((pre && pre.error) || '无法连接本机服务（请从桌面客户端内打开商店）'), true);
      return;
    }
    // 三态纪律（2026-10-08）：unknown=清单不可核验 → 不阻断（与 jz_deps 口径一致，
    // 此前把 unknown 当"缺"导致打包客户端必弹「缺少依赖」）；只有 missing/mismatch 才算缺。
    // 服务端条目的依赖属服务器侧（客户端装的是前端包），行上标 informational 不联动本地安装。
    var missing = (pre.deps || []).filter(function (d) {
      return (d.state === 'missing' || d.state === 'mismatch')
        && d.required !== false && !d.informational;
    });
    var resolvable = missing.filter(function (d) { return d.resolve === 'dep-package'; });
    var unresolvable = missing.filter(function (d) { return d.resolve !== 'dep-package'; });
    wantDeps = resolvable.length > 0;
    var ok;
    if (unresolvable.length) {
      ok = await stDialog({ title: '缺少依赖', danger: true, okText: '仍然安装',
        html: '以下依赖本机缺失且商店暂无对应依赖包：<br><b>'
          + unresolvable.map(function (d) { return esc(d.name + ' ' + (d.require || '')); }).join('<br>')
          + '</b><br><br>仍要安装吗？（插件装上后会处于降级/不可运行状态，可稍后补装依赖）' });
      if (!ok) return;
    } else if (resolvable.length) {
      ok = await stDialog({ title: '同时安装依赖', okText: '继续安装',
        html: '检测到缺失依赖，将同时安装：<br><b>'
          + resolvable.map(function (d) { return esc(d.name + ' ' + (d.require || '')); }).join('<br>')
          + '</b><br><br>继续安装「' + esc(e.name) + '」吗？' });
      if (!ok) return;
    } else if (st_upgradableConfirm(e)) {
      var stNow = pluginInstallState(e);
      var hybrid = !!(stNow.entry && stNow.entry.kind === 'hybrid');
      ok = await stDialog({ title: '升级确认', okText: '升级',
        html: '升级到 <b>v' + esc(e.version) + '</b>？'
          + (hybrid ? '<br>hybrid 插件将触发能力层重启（会话已保留，免重登）。'
                    : (e.runtime === 'server' ? '<br>将更新本机前端副本（页面本地直出）。' : '')) });
      if (!ok) return;
    } else {
      // V5：server_data 条目安装时**明示弹出**数据出机提示（红线 5）
      var sdNote = (e.server_data || (st.entry && st.entry.server_data))
        ? '<br><br><b>数据出机提示</b>：该插件的部分必要数据会发送到服务器（server_data 明示）。'
        : '';
      ok = await stDialog({ title: '安装确认', okText: '安装',
        html: '安装「<b>' + esc(e.name) + '</b>」v' + esc(e.version) + '？' + sdNote });
      if (!ok) return;
    }
    msg('正在安装 ' + e.name + ' …');
    // 按钮内进度（2026-10-08）：本页生成 token，服务端按包权重折算百分比写安装台账，400ms 轮询
    var token = 'ins-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
    var idleLabel = (btn && btn.getAttribute('data-label')) || '安装';
    function setProg(state, text, pct) {
      if (!btn) return;
      btn.classList.remove('prog', 'done');
      if (state) btn.classList.add(state);
      btn.disabled = state === 'prog';
      var fill = btn.querySelector('.st-dl-fill');
      var lab = btn.querySelector('.st-dl-label');
      if (fill) fill.style.width = (pct || 0) + '%';
      if (lab) lab.textContent = text || idleLabel;
    }
    setProg('prog', '安装中 0%', 0);
    var progTimer = setInterval(function () {
      fetch('/local-api/store/install/progress?id=' + encodeURIComponent(token))
        .then(function (r) { return r.json(); })
        .then(function (p) {
          if (p.status !== 'running') return;
          if (btn) btn.title = p.phase || '';
          setProg('prog', '安装中 ' + (p.pct || 0) + '%', p.pct || 0);
        })
        .catch(function () { /* 进度轮询失败不影响安装 */ });
    }, 400);
    post('/local-api/store/install', { id: e.id, version: e.version,
                                       include_deps: wantDeps,
                                       deps: resolvable.map(function (d) {
                                         return { dep_id: d.dep_id, dep_version: d.dep_version };
                                       }),
                                       progress_id: token })
      .then(async function (d) {
        clearInterval(progTimer);
        if (!d.ok) { setProg('', null, 0); msg('安装失败：' + (d.error || '未知错误'), true); return; }
        setProg('done', '已完成', 100);
        // 账号登记由能力层安装链先写（D18：先登记后落盘），页面只刷新状态展示
        var note = [];
        var needRestart = !!d.restart_required
          || !!(d.pending_components && d.pending_components.length);
        if (d.pending_components && d.pending_components.length) {
          note.push('依赖组件待重启生效：' + d.pending_components.join('、'));
        }
        (d.dep_results || []).forEach(function (r) {
          if (!r.ok) note.push('依赖 ' + r.id + ' 安装失败：' + (r.error || ''));
        });
        msg('安装成功：' + e.id + (note.length ? '（' + note.join('；') + '）' : ''));
        loadInstalls().then(loadLocalState).then(render);
        if (needRestart) {
          var yes = await stDialog({ title: '需要重启', okText: '立即重启',
            html: '本插件/依赖需要重启客户端才能生效（会话保留，免重登）。<br>现在自动重启？' });
          if (yes) {
            msg('正在重启客户端 …');
            post('/local-api/restart', {}).then(function (r) {
              if (!r.ok) msg('自动重启不可用：' + (r.error || '') + '——请手动重启客户端', true);
            }).catch(function () { msg('自动重启请求失败：请手动重启客户端', true); });
          } else {
            msg('已安装（重启客户端后生效，会话保留）');
          }
        }
      })
      .catch(function (err) { msg('安装异常：' + err, true); });
  }

  function st_upgradableConfirm(e) {
    var st = pluginInstallState(e);
    return st.installed;   // 已安装再点安装 = 升级
  }

  async function onAction(act, btn) {
    var id = btn.getAttribute('data-id');
    var ver = btn.getAttribute('data-ver');
    var e = findEntry(id);
    if (!e) { msg('条目不存在', true); return; }
    if (act === 'download-local') {                 // 桌面端：校验 → 进度条下载（批次 10 状态机）
      if (btn.dataset.busy) return;
      btn.dataset.busy = '1';
      var dlLabel = btn.querySelector('.st-dl-label') || btn;
      var dlFill = btn.querySelector('.st-dl-fill');
      var idleLabel = btn.getAttribute('data-label') || '下载包';
      function dlSet(state, text, pct) {
        btn.classList.remove('checking', 'progress', 'done', 'have');
        if (state) btn.classList.add(state);
        dlLabel.textContent = text || idleLabel;
        if (dlFill) dlFill.style.width = (pct || 0) + '%';
      }
      function dlRelease() { delete btn.dataset.busy; }
      dlSet('checking', '校验中');                 // ① 先校验是否已下载
      fetch('/local-api/store/downloaded?id=' + encodeURIComponent(id)
            + '&version=' + encodeURIComponent(ver))
        .then(function (r) { return r.json(); })
        .then(function (chk) {
          if (chk.downloaded) { dlSet('have', '已下载'); dlRelease(); return; }
          dlSet('progress', '下载中 0%', 0);       // ② 后台任务 + 进度填充
          return fetch('/local-api/store/download', { method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id, version: ver }) })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              if (!d.ok) { dlSet('', idleLabel, 0); dlRelease();
                msg('下载失败：' + (d.error || ''), true); return; }
              var timer = setInterval(function () {
                fetch('/local-api/store/download/progress?id=' + d.id)
                  .then(function (r2) { return r2.json(); })
                  .then(function (p) {
                    if (p.status === 'running') {
                      var pct = p.total ? Math.round(p.received / p.total * 100) : 0;
                      dlSet('progress', '下载中 ' + pct + '%', pct);
                    } else if (p.status === 'done') {
                      clearInterval(timer);
                      dlSet('done', '已完成', 100);   // ③ 完成态（进度条满）
                      dlRelease();
                      msg('已保存到本机 downloads（已打开文件夹）');
                      fetch('/local-api/store/reveal', { method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: p.path || '' }) })
                        .then(function (r3) { return r3.json(); })
                        .then(function (x) {
                          if (!x.ok) msg('已保存，但打开文件夹失败：' + (x.error || '')
                            + '——文件在下载目录 downloads 内', true);
                        })
                        .catch(function () { /* 打开失败不改变已下载事实 */ });
                    } else if (p.status === 'error') {
                      clearInterval(timer);
                      dlSet('', idleLabel, 0); dlRelease();
                      msg('下载失败：' + (p.error || ''), true);
                    }
                  });
              }, 400);
            });
        })
        .catch(function (err) { dlSet('', idleLabel, 0); dlRelease(); msg('下载校验失败：' + err, true); });
      return;
    }
    if (act === 'install') { preflightAndInstall(e, btn); return; }
    if (act === 'toggle') {
      var enable = btn.getAttribute('data-en') === '1';
      if (installs && btn.getAttribute('data-account') === '1') {
        // 账号级启停（D4）：随账号的启停写安装登记；停用=该账号不显示，其他账号不受影响
        post('/plugin/store/api/installs', { action: enable ? 'enable' : 'disable', id: id })
          .then(function (d) {
            msg(d.ok ? ('已' + (enable ? '启用' : '停用') + '（仅当前账号）')
                     : ('操作失败：' + (d.error || '')), !d.ok);
            loadInstalls().then(loadLocalState).then(render);
          });
        return;
      }
      post('/local-api/plugins/state', { id: id, enabled: enable })
        .then(function (d) {
          msg(d.ok ? '已' + (enable ? '启用' : '停用')
            + (d.restart_required ? '（需重启能力层完全生效）' : '') : ('操作失败：' + (d.error || '')), !d.ok);
          loadLocalState().then(render);
        });
      return;
    }
    if (act === 'uninstall-dep') {
      if (!await stDialog({ title: '卸载依赖组件', danger: true, okText: '卸载',
        html: '卸载依赖组件 <b>' + esc(id) + '</b>？<br>依赖组件无独立业务数据；正在被映射的原生库将在下次启动客户端后清除。' })) return;
      post('/local-api/store/uninstall-dep', { id: id }).then(function (d) {
        msg(d.ok ? ('已卸载依赖 ' + id + (d.pending_remove ? '（原生库将于下次启动清除）' : ''))
                 : ('卸载失败：' + (d.error || '')), !d.ok);
        loadLocalState().then(render);
      });
      return;
    }
    if (act === 'uninstall') {
      if (installs) {
        // 账号级卸载（2026-10-11 需求①④/§2.4/D3）：只移当前账号的登记；本机副本
        // 保留为缓存（多账号共用，换回/重装秒级可用）；数据默认保留（D9），清除时
        // 作用域=当前账号数据目录。不再出现「已从本机移除」，文案不提删除副本。
        if (!await stDialog({ title: '卸载', danger: true, okText: '卸载',
          html: '从当前账号卸载 <b>' + esc(id) + '</b>？<br>'
            + '本机副本将保留为缓存（重新安装或其他账号使用时秒级可用）；本账号的插件数据默认保留。' })) return;
        var purgeAcc = await stDialog({ title: '是否清除数据', danger: true, okText: '清除数据', cancelText: '保留数据',
          html: '清除当前账号在该插件名下的数据（配置 / 缓存 / 本地业务数据）？<br>'
            + '选择「保留数据」可稍后再装回。' });
        post('/local-api/plugins/uninstall', { id: id, purge_data: purgeAcc }).then(function (d) {
          msg(d.ok ? ('已从当前账号卸载' + (d.data_purged ? '（数据已清除）' : '（数据保留）'))
                   : ('卸载失败：' + (d.error || '')), !d.ok);
          loadInstalls().then(loadLocalState).then(render);
        });
        return;
      }
      var ent = findEntry(id);
      if (ent && ent.runtime === 'server') {
        // 降级模式（旧服务器，G2-7）：删本机前端包副本 → 回退中继直出（legacy 行为）
        if (!await stDialog({ title: '卸载', danger: true, okText: '卸载',
          html: '卸载 <b>' + esc(id) + '</b> 的本机前端包副本？<br>'
            + '页面回退中继直出；插件在服务器端照常运行（重新安装即恢复）。' })) return;
        post('/local-api/plugins/uninstall', { id: id, purge_data: false }).then(function (d) {
          msg(d.ok ? ('已卸载前端包副本：' + id + '（重新安装即恢复）')
                   : ('卸载失败：' + (d.error || '')), !d.ok);
          loadLocalState().then(render);
        });
        return;
      }
      if (!await stDialog({ title: '卸载插件', danger: true, okText: '下一步',
        html: '卸载 <b>' + esc(id) + '</b>？<br>本地包副本将被删除；下一步选择是否清除数据。' })) return;
      var purge = await stDialog({ title: '是否清除数据', danger: true, okText: '清除数据', cancelText: '保留数据',
        html: '清除该插件的数据（数据根 plugins 下的插件目录：配置 / 缓存 / 本地业务数据）？<br>'
          + '选择「保留数据」可稍后再装回。' });
      post('/local-api/plugins/uninstall', { id: id, purge_data: purge }).then(function (d) {
        msg(d.ok ? ('已卸载' + (d.data_purged ? '（数据已清除）' : '（数据保留在数据目录）'))
                 : ('卸载失败：' + (d.error || '')), !d.ok);
        loadLocalState().then(render);
      });
      return;
    }
  }

  // ---------- 启动 ----------
  document.querySelectorAll('.st-tab').forEach(function (t) {
    t.addEventListener('click', function () {
      document.querySelectorAll('.st-tab').forEach(function (x) {
        x.classList.remove('active');
        x.setAttribute('aria-selected', 'false');
      });
      t.classList.add('active');
      t.setAttribute('aria-selected', 'true');
      render();
    });
  });
  $('#st-q').addEventListener('input', render);
  $('#st-grid').addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('button[data-act]') : null;
    if (btn) { onAction(btn.getAttribute('data-act'), btn); return; }
    var card = ev.target.closest ? ev.target.closest('.st-card') : null;
    if (card && card.getAttribute('data-id')) {
      var e = findEntry(card.getAttribute('data-id'));
      if (e) openDetail(e);                 // 点击卡片 → 详情悬浮窗（操作按钮除外）
    }
  });

  function renderSkeleton() {
    var grid = $('#st-grid');
    var html = '';
    for (var i = 0; i < 6; i++) html += '<div class="st-skel"><i></i><i></i><i></i><i></i><i></i></div>';
    grid.innerHTML = html;
  }
  renderSkeleton();

  loadInstalls().then(loadLocalState).then(function () {
    return fetch(CATALOG_API).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { $('#st-grid').innerHTML = '<div class="st-empty">' + esc(d.error) + '</div>'; return; }
      catalog = d.packages || [];
      render();
    }).catch(function (err) {
      $('#st-grid').innerHTML = '<div class="st-empty">商店目录加载失败：' + esc(err) + '</div>';
    });
  });
})();
