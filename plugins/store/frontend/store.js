/* 插件商店页逻辑（零构建原生 JS；?v=1 起版，改动须递增引用处）。
 *
 * 数据：GET /plugin/store/api/catalog?all=1（服务器会话鉴权，浏览器与桌面端同页）。
 * 桌面端（html.jz-shell）：本地状态经 /local-api/plugins 取得，安装/生命周期操作走
 * /local-api/store/* 与 /local-api/plugins/*（同源，永不中继）；浏览器路径只有浏览与
 * 下载（安装类按钮由 CSS 门控隐藏，红线 2）。
 */
(function () {
  'use strict';

  // 壳内自判（批次 10）：本页经工具壳 iframe 内嵌 = 桌面端 → 给 <html> 打 jz-shell，
  // 激活 .jz-only 操作列（安装/下载包）。中继透传不注入该类（A 类页面框架 chrome 由壳页承载），
  // 不自判则桌面端永远只有浏览器路径按钮——而 iframe 内的下载会静默失败（2026-10-07 真机报告）。
  // 浏览器路径下本页是顶层窗口 → 不打类 → 只显示浏览器下载按钮（红线 2 不变）。
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

  function $(s, el) { return (el || document).querySelector(s); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function msg(t, isErr) {
    var el = $('#st-msg');
    el.textContent = t || '';
    el.classList.toggle('err', !!isErr);
    if (t) setTimeout(function () { if (el.textContent === t) el.textContent = ''; }, 6000);
  }
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
    }).then(function (r) { return r.json(); });
  }
  function humanRuntime(rt) {
    return rt === 'server' ? '服务器' : rt === 'dep-slot' ? '依赖' : '本地';
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
        plugins[p.id] = { version: p.version, enabled: p.enabled !== false, kind: p.kind };
      });
      (d.server_frontends || []).forEach(function (f) {
        frontends[f.id] = { version: f.version, enabled: f.enabled !== false };
      });
      (d.components || []).forEach(function (c) {
        components[c.id] = { version: c.version, pending: !!c.pending };
      });
      localState = { plugins: plugins, frontends: frontends, components: components, loaded: true };
    }).catch(function () { localState.loaded = true; });
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
    var st = { installed: !!inst && inst.enabled !== false, upgradable: false,
               disabled: !!inst && inst.enabled === false, entry: inst || null };
    if (inst && verGt(e.version, inst.version)) st.upgradable = true;
    return st;
  }

  function buttonsHtml(e) {
    var st = pluginInstallState(e);
    var html = '';
    if (IN_SHELL && e.runtime !== 'server') {
      if (!st.installed) {
        html += '<button class="st-btn-install" data-act="install" data-id="' + esc(e.id) + '">安装</button>';
      } else if (st.upgradable) {
        html += '<button class="st-btn-install" data-act="install" data-id="' + esc(e.id) + '">升级到 ' + esc(e.version) + '</button>';
      }
    }
    if (IN_SHELL && e.runtime === 'server') {
      if (!st.installed && !st.disabled) {
        html += '<button class="st-btn-install" data-act="install" data-id="' + esc(e.id) + '">安装前端</button>';
      } else if (st.upgradable) {
        html += '<button class="st-btn-install" data-act="install" data-id="' + esc(e.id) + '">升级前端到 ' + esc(e.version) + '</button>';
      }
      if (st.installed) {
        html += '<button class="st-btn-warn" data-act="toggle" data-id="' + esc(e.id) + '" data-en="0">停用（回退中继）</button>';
      } else if (st.disabled) {
        html += '<button class="st-btn-warn" data-act="toggle" data-id="' + esc(e.id) + '" data-en="1">启用本地直出</button>';
      }
      if (st.installed || st.disabled) {
        html += '<button class="st-btn-danger" data-act="uninstall" data-id="' + esc(e.id) + '">卸载本地副本</button>';
      }
    }
    if (IN_SHELL && e.runtime === 'local' && st.entry) {
      if (st.disabled) {
        html += '<button class="st-btn-warn" data-act="toggle" data-id="' + esc(e.id) + '" data-en="1">启用</button>';
      } else {
        html += '<button class="st-btn-warn" data-act="toggle" data-id="' + esc(e.id) + '" data-en="0">停用</button>';
      }
      html += '<button class="st-btn-danger" data-act="uninstall" data-id="' + esc(e.id) + '">卸载</button>';
    }
    // 下载：两端可见。桌面端经能力层落数据目录 downloads\；浏览器直接下载文件。
    if (e.runtime === 'server') {
      html += '<button class="st-btn-ghost jz-only" data-act="download-local" data-id="' + esc(e.id) + '" data-ver="' + esc(e.version) + '">下载后端包</button>';
      html += '<button class="st-btn-ghost st-browser-dl" data-act="download" data-id="' + esc(e.id) + '" data-ver="' + esc(e.version) + '">下载后端包</button>';
    } else {
      html += '<button class="st-btn-ghost jz-only" data-act="download-local" data-id="' + esc(e.id) + '" data-ver="' + esc(e.version) + '">下载包</button>';
      html += '<button class="st-btn-ghost st-browser-dl" data-act="download" data-id="' + esc(e.id) + '" data-ver="' + esc(e.version) + '">下载包</button>';
    }
    return html;
  }

  function detailHtml(e) {
    var rows = [];
    var req = requiresText(e);
    if (req) rows.push('<h4>依赖版本声明</h4>' + req);
    if (e.runtime === 'server' && e.frontend_file) {
      rows.push('<h4>前端包</h4>' + esc(e.frontend_file) + '（' + fmtSize(e.frontend_size) + '）');
    }
    rows.push('<h4>发布时间</h4>' + esc(e.published_at || '—'));
    var hist = (e.versions || []).map(function (v) {
      return v.status === 'published'
        ? 'v' + esc(v.version) + '（' + esc(v.published_at || '') + '）' + (v.changelog ? '：' + esc(v.changelog) : '')
        : 'v' + esc(v.version) + '（' + esc(v.status) + '，不可见）';
    });
    if (hist.length) rows.push('<h4>版本历史</h4>' + hist.join('<br>'));
    return rows.join('');
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
    list.forEach(function (e) {
      var st = pluginInstallState(e);
      var badges = '<span class="st-badge ' + runtimeClass(e.runtime) + '">' + humanRuntime(e.runtime) + '</span>'
        + (e.deps_mode === 'venv' ? '<span class="st-badge venv">独立环境</span>' : '')
        + (IN_SHELL && st.installed && !st.upgradable ? '<span class="st-badge local">已安装</span>' : '')
        + (st.upgradable ? '<span class="st-badge up">可升级</span>' : '')
        + (st.disabled ? '<span class="st-badge venv">已停用</span>' : '');
      var card = document.createElement('div');
      card.className = 'st-card';
      card.innerHTML = ''
        + '<div class="st-card-top">'
        +   '<span class="st-icon">' + esc(e.icon || '🧩') + '</span>'
        +   '<div class="st-title"><p class="st-name">' + esc(e.name) + '</p>'
        +     '<div class="st-badges">' + badges + '</div>'
        +   '</div>'
        + '</div>'
        + '<p class="st-desc">' + esc(e.description || '') + '</p>'
        + '<div class="st-meta">v' + esc(e.version) + ' · ' + fmtSize(e.size)
        +   (e.runtime === 'server' && e.frontend_size ? ' · 前端 ' + fmtSize(e.frontend_size) : '')
        +   (st.entry && st.entry.version && verGt(e.version, st.entry.version)
              ? ' · 本机 v' + esc(st.entry.version) : '')
        + '</div>'
        + '<div class="st-actions">' + buttonsHtml(e) + '</div>'
        + '<button class="st-detail-toggle" data-act="detail">详情 ▾</button>'
        + '<div class="st-detail">' + detailHtml(e) + '</div>';
      grid.appendChild(card);
    });
  }

  // ---------- 操作 ----------
  function findEntry(id) {
    return catalog.filter(function (e) { return e.id === id; })[0] || null;
  }

  function preflightAndInstall(e) {
    var wantDeps = false;
    var pre = { installable: true, deps: [] };
    var proceed = function () {
      msg('正在安装 ' + e.name + ' …');
      post('/local-api/store/install', { id: e.id, version: e.version, include_deps: wantDeps })
        .then(function (d) {
          if (!d.ok) { msg('安装失败：' + (d.error || '未知错误'), true); return; }
          var note = [];
          if (d.restart_required) note.push('需重启能力层生效（会话已保留）');
          if (d.pending_components && d.pending_components.length) {
            note.push('依赖组件待下次启动生效：' + d.pending_components.join('、'));
          }
          (d.dep_results || []).forEach(function (r) {
            if (!r.ok) note.push('依赖 ' + r.id + ' 安装失败：' + (r.error || ''));
          });
          msg('安装成功：' + e.id + (note.length ? '（' + note.join('；') + '）' : ''));
          loadLocalState().then(render);
        })
        .catch(function (err) { msg('安装异常：' + err, true); });
    };
    if (!IN_SHELL) { return; }
    msg('正在预检依赖 …');
    post('/local-api/store/preflight', { id: e.id, version: e.version })
      .then(function (d) { pre = d; })
      .catch(function () { /* 预检失败不阻断安装（D-8 口径：提示不阻断） */ })
      .then(function () {
        var missing = (pre.deps || []).filter(function (d) {
          return d.state !== 'ok' && d.required !== false && !d.informational;
        });
        var resolvable = missing.filter(function (d) { return d.resolve === 'dep-package'; });
        var unresolvable = missing.filter(function (d) { return d.resolve !== 'dep-package'; });
        wantDeps = resolvable.length > 0;
        if (unresolvable.length) {
          if (!confirm('以下依赖本机缺失且商店暂无对应依赖包：\n  ' + unresolvable.map(function (d) { return d.name + ' ' + (d.require || ''); }).join('\n  ')
            + '\n\n仍要安装吗？（插件装上后会处于降级/不可运行状态，可稍后补装依赖）')) return;
        } else if (resolvable.length) {
          if (!confirm('检测到缺失依赖，将同时安装：\n  ' + resolvable.map(function (d) { return d.name + ' ' + (d.require || ''); }).join('\n  ')
            + '\n\n继续安装「' + e.name + '」吗？')) return;
        } else if (st_upgradableConfirm(e)) {
          if (!confirm('升级到 v' + e.version + '？hybrid 插件将触发能力层秒级重启（会话已保留，免重登）。')) return;
        } else if (!confirm('安装「' + e.name + '」v' + e.version + '？')) {
          return;                                    // 全新安装也确认（P2：与升级/带依赖路径口径一致）
        }
        proceed();
      });
  }
  function st_upgradableConfirm(e) {
    var st = pluginInstallState(e);
    return st.installed;   // 已安装再点安装 = 升级
  }

  function onAction(act, btn) {
    var id = btn.getAttribute('data-id');
    var ver = btn.getAttribute('data-ver');
    var e = findEntry(id);
    if (act === 'detail') {
      btn.parentNode.classList.toggle('open');
      btn.textContent = btn.parentNode.classList.contains('open') ? '详情 ▴' : '详情 ▾';
      return;
    }
    if (!e) { msg('条目不存在', true); return; }
    if (act === 'download') {                       // 浏览器路径：普通文件下载
      window.location = '/plugin/store/api/download/' + encodeURIComponent(id)
        + '/' + encodeURIComponent(ver) + '?via=browser';
      return;
    }
        if (act === 'download-local') {                 // 桌面端：启动后台任务，轮询进度（批次 10）
      if (btn.dataset.busy) return;
      btn.dataset.busy = '1';
      var restore = function () { btn.textContent = btn.getAttribute('data-label') || '下载包';
        delete btn.dataset.busy; };
      msg('准备下载…');
      fetch('/local-api/store/download', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id, version: ver }) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) { restore(); msg('下载失败：' + (d.error || ''), true); return; }
          btn.setAttribute('data-label', btn.textContent);
          var timer = setInterval(function () {
            fetch('/local-api/store/download/progress?id=' + d.id)
              .then(function (r2) { return r2.json(); })
              .then(function (p) {
                if (p.status === 'running') {
                  var pct = p.total ? Math.round(p.received / p.total * 100) + '%'
                                    : fmtSize(p.received);
                  btn.textContent = '下载中 ' + pct;
                  msg('下载中 ' + pct + (p.total ? ('（' + fmtSize(p.received) + ' / '
                    + fmtSize(p.total) + '）') : ''));
                } else if (p.status === 'done') {
                  clearInterval(timer); restore();
                  msg('已保存到本机 downloads（已打开文件夹）');
                  fetch('/local-api/store/reveal', { method: 'POST' });
                } else if (p.status === 'error') {
                  clearInterval(timer); restore();
                  msg('下载失败：' + (p.error || ''), true);
                }
              });
          }, 400);
        })
        .catch(function (e) { restore(); msg('下载请求异常：' + e, true); });
      return;
    }
    if (act === 'install') { preflightAndInstall(e); return; }
    if (act === 'toggle') {
      post('/local-api/plugins/state', { id: id, enabled: btn.getAttribute('data-en') === '1' })
        .then(function (d) {
          msg(d.ok ? '已' + (btn.getAttribute('data-en') === '1' ? '启用' : '停用')
            + (d.restart_required ? '（需重启能力层完全生效）' : '') : ('操作失败：' + (d.error || '')), !d.ok);
          loadLocalState().then(render);
        });
      return;
    }
    if (act === 'uninstall') {
      if (!confirm('卸载 ' + id + '？\n本地包副本将被删除；数据默认保留。')) return;
      post('/local-api/plugins/uninstall', { id: id }).then(function (d) {
        msg(d.ok ? '已卸载（数据保留在数据目录）' : ('卸载失败：' + (d.error || '')), !d.ok);
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
    if (btn) onAction(btn.getAttribute('data-act'), btn);
  });

  loadLocalState().then(function () {
    return fetch(CATALOG_API).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { $('#st-grid').innerHTML = '<div class="st-empty">' + esc(d.error) + '</div>'; return; }
      catalog = d.packages || [];
      render();
    }).catch(function (err) {
      $('#st-grid').innerHTML = '<div class="st-empty">商店目录加载失败：' + esc(err) + '</div>';
    });
  });
})();
