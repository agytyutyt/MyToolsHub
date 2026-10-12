/* 商店管理页逻辑（admin-store.html 配套；?v=1 起版，改动须递增引用处）。
 * 数据面：/plugin/admin/store/list|upload|review|redeploy|delist|meta|stats。
 * 权限：全部登录用户（需求⑦ 不分角色）；服务器侧部署走当前形态 inspect/apply。 */
(function () {
  'use strict';

  var API = '/plugin/admin/store';
  var filter = 'pending';
  var scope = 'plugin';   // 二级页签：插件 / 依赖（2026-10-12 优化——范围筛选替代上下分段）
  var packages = [];
  var installedNames = [];   // 主包运行时自带依赖（服务端 installed-deps）
  var canReview = true;      // 审核/下架/部署权限（2026-10-08：仅管理员/超管；提交全员）
  var canSrv = false;        // 服务器端启停/撤包权限（2026-10-08：仅超级管理员）
  var depProvides = {};      // 已上架依赖条目的 provides 集合（name→true）

  function esc(s) {
    // 与 admin-common.js 的 esc 同字符集（含单引号）：值也会进单引号属性/模板语境，
    // 少替一个 ' 就有逃逸出属性的面（L2 分诊登记的低优先加固项，裁定 7 ③ 现补）。
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function toast(t, isErr) {
    var el = document.getElementById('toast');
    el.textContent = t || '';
    el.classList.toggle('error', !!isErr);
    el.classList.add('show');
    setTimeout(function () { el.classList.remove('show'); }, 5000);
  }
  function fmtSize(n) {
    n = Number(n) || 0;
    if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
    return n + ' B';
  }
  function post(path, body) {
    return fetch(API + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); });
  }
  function runtimeLabel(rt) {
    return rt === 'server' ? '服务器' : rt === 'dep-slot' ? '依赖' : '本地';
  }
  function statusLabel(s) {
    return { pending: '待审核', published: '已上架', rejected: '已驳回', delisted: '已下架' }[s] || s;
  }

  // 操作区=图标按钮（2026-10-12 需求①）：一行两个、鼠标悬停显示中文标签（title）。
  // data-* 与旧按钮完全一致——点击处理无需改动。
  var OP_ICONS = {
    approve: '✅', reject: '⛔', delist: '⬇️', republish: '⬆️',
    redeploy: '🚀', srv_enable: '▶️', srv_disable: '⏸️', withdraw: '🗑️',
    gdelist: '⬇️', gsrv_enable: '▶️', gsrv_disable: '⏸️', gwithdraw: '🗑️',
  };
  function opBtn(op, attrs, label, extraTitle) {
    var icon = OP_ICONS[op] || '•';
    var title = label + (extraTitle ? '——' + extraTitle : '');
    return '<button type="button" class="st-op-btn" data-op="' + esc(op) + '" ' + (attrs || '')
      + ' title="' + esc(title) + '" aria-label="' + esc(label) + '">' + icon + '</button>';
  }
  // 自绘对话框 = 框架级组件（static/js/jz-dialog.js；2026-10-08 解耦，样式随脚本注入）
  var adDialog = function (opts) { return window.JZDialog.open(opts); };

  function chip(text, kind) {
    return '<span class="st-chip st-chip-' + kind + '">' + esc(text) + '</span>';
  }
  var STATUS_KIND = { pending: 'warn', published: 'ok', rejected: 'bad', delisted: 'muted' };
  var RUNTIME_KIND = { local: 'info', server: 'primary', 'dep-slot': 'purple' };

  function isDepRow(r) { return r.runtime === 'dep-slot'; }
  function inScope(r) { return (scope === 'dep') === isDepRow(r); }
  function updateCounts() {
    var rows = flatRows().filter(inScope);
    function n(f) { return rows.filter(f).length; }
    document.getElementById('cnt-pending').textContent = n(function (r) { return r.status === 'pending'; });
    document.getElementById('cnt-published').textContent = n(function (r) { return r.status === 'published'; });
    document.getElementById('cnt-off').textContent =
      n(function (r) { return r.status === 'delisted' || r.status === 'rejected'; });
  }

  // ---------------- 上架表单 ----------------
  var runtimeSel = document.getElementById('up-runtime');
  function syncFormByRuntime() {
    var rt = runtimeSel.value;
    document.getElementById('row-frontend').hidden = rt !== 'server';
    document.getElementById('row-client').hidden = rt !== 'server';
    document.getElementById('row-deps-mode').hidden = rt === 'dep-slot';
  }
  runtimeSel.addEventListener('change', syncFormByRuntime);
  syncFormByRuntime();

  // ---------------- 主包拖拽上传区（包主导，批次 10） ----------------
  var upFile = document.getElementById('up-file');
  var drop = document.getElementById('st-drop');
  var dropHint = document.getElementById('st-drop-hint');

  function inferRuntime(filename) {
    if (filename.indexOf('JZClient-本地插件-') === 0) return 'local';
    if (filename.indexOf('JZToolsHub-插件-') === 0) return 'server';
    if (filename.indexOf('JZToolsHub-依赖-') === 0) return 'dep-slot';
    return '';
  }

  function assignPackage(f) {
    if (!f) return;
    var dt = new DataTransfer();
    dt.items.add(f);
    upFile.files = dt.files;
    var rt = inferRuntime(f.name);
    if (rt && rt !== runtimeSel.value) {
      runtimeSel.value = rt;
      syncFormByRuntime();
    }
    var note = rt ? ('已识别类别：' + runtimeLabel(rt)
      + (rt === 'server' ? '——请在下方选择成对前端包' : '（可手动更改）'))
      : '未识别命名规范——请手动选择类别';
    dropHint.innerHTML = '<b>' + esc(f.name) + '</b> <span class="st-muted">' + fmtSize(f.size)
      + ' · ' + note + '</span>';
  }

  ['dragenter', 'dragover'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('dragover'); });
  });
  drop.addEventListener('dragleave', function (e) {
    e.preventDefault(); drop.classList.remove('dragover');
  });
  drop.addEventListener('drop', function (e) {
    e.preventDefault(); drop.classList.remove('dragover');
    if (e.dataTransfer && e.dataTransfer.files.length) assignPackage(e.dataTransfer.files[0]);
  });
  drop.addEventListener('click', function () { upFile.click(); });
  upFile.addEventListener('change', function () { assignPackage(upFile.files[0]); });


  document.getElementById('btn-upload').addEventListener('click', function () {
    var file = document.getElementById('up-file').files[0];
    if (!file) { toast('请选择包文件（zip）', true); return; }
    var fd = new FormData();
    fd.append('runtime', runtimeSel.value);
    fd.append('file', file);
    fd.append('name', document.getElementById('up-name').value);
    fd.append('description', document.getElementById('up-desc').value);
    fd.append('icon', document.getElementById('up-icon').value);
    fd.append('changelog', document.getElementById('up-changelog').value);
    fd.append('deps_mode', document.getElementById('up-deps-mode').value);
    var fe = document.getElementById('up-frontend').files[0];
    if (fe) fd.append('frontend_file', fe);
    var cli = document.getElementById('up-client').files[0];
    if (cli) fd.append('client_artifact', cli);   // C 类配对客户端工件（可选，2026-10-11）
    var btn = this;
    btn.disabled = true;
    document.getElementById('upload-result').textContent = '上传并校验中…';
    fetch(API + '/upload', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        if (!d.ok) {
          document.getElementById('upload-result').textContent = '';
          toast('上架失败：' + (d.errors || [d.error || '未知错误']).join('；'), true);
        } else {
          document.getElementById('upload-result').textContent =
            '已提交待审核：' + d.id + ' v' + d.version +
            (d.frontend ? '（前端包 ' + d.frontend + '）' : '') +
            (d.client ? '（客户端工件 ' + d.client.id + ' v' + d.client.version + '，配对发布）' : '');
          toast('上架成功，等待审核');
          load();
        }
      })
      .catch(function (e) { btn.disabled = false; toast('上传异常：' + e, true); });
  });

  // ---------------- 目录管理 ----------------
  function versionsOf(entry) {
    return entry.versions || [];
  }
  // C 类配对（2026-10-11 需求② G3-11）：id → {partner, hidden}
  // hidden=true = 服务器工件（不出商店卡，商店只出客户端工件卡）；false = 客户端工件（商店卡）
  var pairInfo = {};
  function computePairs() {
    pairInfo = {};
    var auto = {};                       // 服务器工件 id → 客户端工件 id（local-<id>+server_data 自动识别）
    packages.forEach(function (p) {
      if (String(p.id).indexOf('local-') === 0 && p.server_data) auto[p.id.slice(6)] = p.id;
    });
    packages.forEach(function (p) {
      if (p.runtime !== 'server') return;
      var ce = '';
      (p.versions || []).forEach(function (v) { if (v.client_entry) ce = v.client_entry; });
      var partner = ce || auto[p.id] || '';
      if (!partner) return;
      pairInfo[p.id] = { partner: partner, hidden: true };
      pairInfo[partner] = { partner: p.id, hidden: false };
    });
  }
  function flatRows() {
    // 目录 → 行（每版本一行，按状态过滤）
    var rows = [];
    packages.forEach(function (p) {
      versionsOf(p).forEach(function (v) {
        rows.push({
          id: p.id, name: p.name || p.id, runtime: p.runtime,
          version: v.version, status: v.status, file: v.file, size: v.size,
          changelog: v.changelog, published_at: v.published_at,
          submit_by: (v.submit || {}).by, review: v.review || {},
          installed_version: p.installed_version, restart_pending: p.restart_pending,
          srv_enabled: p.srv_enabled, backups: p.backups,
          srv_component_version: p.srv_component_version,
          frontend_file: v.frontend_file, deps_mode: p.deps_mode,
          requires: p.requires, server_data: !!p.server_data,
          // 多版本独立配置（2026-10-12）：该行是否即服务器上正在运行的版本
          deployed: !!p.installed_version && String(v.version) === String(p.installed_version),
        });
      });
      // 目录外版本（真机 2026-10-12）：服务器部署的版本不在目录版本列表里（历史部署/
      // 目录被清理），补一条合成行承载"停用/卸载"等插件级操作，否则它们无处落脚。
      if (p.runtime === 'server' && p.installed_version
          && !versionsOf(p).some(function (v) {
               return String(v.version) === String(p.installed_version); })) {
        rows.push({
          id: p.id, name: p.name || p.id, runtime: 'server',
          version: String(p.installed_version), status: 'published', synthetic: true,
          deployed: true, installed_version: p.installed_version,
          restart_pending: p.restart_pending, srv_enabled: p.srv_enabled, backups: p.backups,
          deps_mode: p.deps_mode, requires: p.requires,
        });
      }
    });
    return rows;
  }

  // ---------- 展开板分组（2026-10-12 需求①） ----------
  // 同一插件的多个版本收进一个可折叠分组；C 类（配对）在组内用「后端包/前端包」切换卡。
  var groupState = {};   // key -> {expanded: bool, subtab: 'backend'|'frontend'}
  function gs(key) {
    // 默认收缩（2026-10-12 需求①）：首次访问某组时处于折叠态，点击 ▸ 展开
    if (!groupState[key]) groupState[key] = { expanded: false, subtab: 'backend' };
    return groupState[key];
  }
  function buildGroups() {
    // 分组口径：server 条目 + 其配对客户端工件（显式 client_entry 或 local-<id>+server_data）
    // 合成一个 pair 组；未配对的条目各自 single 组。
    var byId = {};
    packages.forEach(function (p) { byId[p.id] = p; });
    var consumed = {};
    var groups = [];
    packages.forEach(function (p) {
      if (consumed[p.id]) return;
      if (p.runtime === 'server') {
        var ce = '';
        (p.versions || []).forEach(function (v) { if (v.client_entry) ce = v.client_entry; });
        var cli = ce ? byId[ce] : null;
        if (!cli) {
          var sib = byId['local-' + p.id];
          if (sib && sib.server_data) cli = sib;
        }
        if (cli) consumed[cli.id] = 1;
        consumed[p.id] = 1;
        groups.push({ key: p.id, kind: cli ? 'pair' : 'server', server: p, client: cli || null });
        return;
      }
      if (p.runtime === 'local' && p.server_data) {
        var srv = byId[p.id.replace(/^local-/, '')];
        if (srv) consumed[srv.id] = 1;   // 理论上 srv 先被扫描消费；防御式
        consumed[p.id] = 1;
        groups.push({ key: p.id, kind: srv ? 'pair' : 'client', server: srv || null, client: p });
        return;
      }
      consumed[p.id] = 1;
      groups.push({ key: p.id, kind: 'single', server: null, client: null, single: p });
    });
    // 需求③（2026-10-12）：默认按**名称**排序（组间）；组内版本按**上架时间**倒序
    // （未上架/无时间的排后，并列回落版本号倒序）
    groups.sort(function (a, b) {
      var ta = groupTitleOf(a), tb = groupTitleOf(b);
      return String(ta.name).localeCompare(String(tb.name), 'zh-Hans-CN');
    });
    return groups;
  }

  function groupTitleOf(g) {
    var first = g.server || g.client || g.single;
    return { name: first.name || first.id, id: first.id };
  }

  function groupRowsOf(g) {
    // 组内应参与渲染的条目（pair 组按当前 subtab 选边）
    if (g.kind === 'pair') {
      var tab = gs(g.key).subtab;
      return tab === 'frontend' ? [g.client] : [g.server];
    }
    return [g.server || g.client || g.single].filter(Boolean);
  }

  // 横向溢出侦测（2026-10-08）：本页不引 admin-plugins.js，需自带；驱动钉列渐变带
  // （.is-overflowing）与左缘渐隐（.is-scrolled），语义与插件列表页 syncTableOverflow 一致。
  function syncTableOverflow() {
    document.querySelectorAll('.data-table-wrap').forEach(function (w) {
      var overflowing = w.scrollWidth > w.clientWidth + 1;
      var atStart = w.scrollLeft <= 1;
      var atEnd = w.scrollLeft + w.clientWidth >= w.scrollWidth - 1;
      // 右缘渐隐/钉列渐变带 = 有溢出且没到尽头；左缘渐隐 = 有溢出且已向左滚动
      w.classList.toggle('is-overflowing', overflowing && !atEnd);
      w.classList.toggle('is-scrolled', overflowing && !atStart);
      if (!w.dataset.scrollBound) {
        w.dataset.scrollBound = '1';
        w.addEventListener('scroll', syncTableOverflow, { passive: true });
      }
    });
  }

  // 单版本行（组内子行）——沿用既有列语义；条目列改为版本号 + 侧别/合成标记
  function versionRowHtml(r, g) {
    var svr = '<span class="st-muted">—</span>';
    if (r.runtime === 'server' && r.status === 'published') {
      if (r.deployed) {
        svr = chip('部署中 v' + esc(r.version), 'ok')
          + (r.srv_enabled === false ? ' ' + chip('已停用', 'warn') : '')
          + (r.restart_pending ? ' ' + chip('待重启', 'bad') : '')
          + (r.backups ? ' ' + chip('有备份 ' + r.backups, 'info') : '');
      } else if (r.installed_version) {
        svr = chip('未部署（服务器当前 v' + esc(r.installed_version) + '）', 'muted');
      } else {
        svr = chip('未部署（后端包已撤下）', 'muted')
          + (r.backups ? ' ' + chip('有备份 ' + r.backups, 'info') : '');
      }
    } else if (r.runtime === 'server') {
      svr = '<span class="st-muted">发布时自动部署</span>';
    } else if (r.runtime === 'dep-slot' && r.status === 'published') {
      svr = r.srv_component_version
        ? chip('已部署 v' + esc(r.srv_component_version), 'ok')
        : chip('未部署（服务器端）', 'muted');
    }
    var depsHtml = '<span class="st-muted">—</span>';
    var unsat = [];
    var reqs = (r.requires || {}).python_packages || [];
    if (reqs.length) {
      depsHtml = '';
      reqs.forEach(function (x) {
        var name = String(x.name || '');
        var ok = depProvides[name.toLowerCase()] || installedNames.indexOf(name.toLowerCase()) >= 0;
        if (!ok) unsat.push(name);
        depsHtml += chip((ok ? '✓ ' : '✗ ') + name, ok ? 'ok' : 'bad')
          + '<span class="st-muted"> </span>';
      });
      if (unsat.length && r.status === 'pending') {
        depsHtml += '<span class="st-muted">待依赖组件包上架（任意用户可传）</span>';
      }
    }
    var ops = '';
    var IDP = 'data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '"';
    if (!canReview) {
      ops = '<span class="st-muted">待审</span>';
    } else if (r.status === 'pending') {
      ops += opBtn('approve', IDP, '通过（发布）',
                   unsat.length ? '依赖不满足：' + unsat.join('、') + '，待对应依赖组件包上架' : '');
      ops += opBtn('reject', IDP, '驳回');
    } else if (r.status === 'published' && r.synthetic) {
      if (canSrv) {
        ops += r.srv_enabled === false
          ? opBtn('srv_enable', IDP, '启用')
          : opBtn('srv_disable', IDP, '停用');
        ops += opBtn('withdraw', IDP, '卸载（撤下后端包）');
      }
    } else if (r.status === 'published') {
      ops += opBtn('delist', IDP, '下架（该版本）');
      if (r.runtime === 'dep-slot') {
        ops += opBtn('redeploy', IDP, '部署到服务器');
      }
      if (r.runtime === 'server') {
        ops += r.deployed
          ? opBtn('redeploy', IDP, '重新部署（当前版本）')
          : opBtn('redeploy', IDP, '部署到该版本', '切换/回退服务器运行版本');
        if (r.deployed && canSrv) {
          ops += r.srv_enabled === false
            ? opBtn('srv_enable', IDP, '启用')
            : opBtn('srv_disable', IDP, '停用');
          ops += opBtn('withdraw', IDP, '卸载（撤下后端包）');
        }
      }
    } else if (r.status === 'delisted') {
      ops += opBtn('republish', IDP, '重新上架');
    }
    return '<td class="st-cell-stack"><span class="st-mono">v' + esc(r.version) + '</span>'
      + (r.synthetic ? chip('目录外版本（历史部署）', 'info') : '')
      + (r.side_label ? '<span class="st-muted">' + esc(r.side_label) + '</span>' : '')
      + '</td>'
      + '<td class="st-cell-stack st-mono">'
      + (r.frontend_file ? chip('前端包成对', 'muted') : '') + '</td>'
      + '<td>' + chip(runtimeLabel(r.runtime), RUNTIME_KIND[r.runtime] || 'muted') + '</td>'
      + '<td class="st-cell-stack">' + chip(statusLabel(r.status), STATUS_KIND[r.status] || 'muted')
      + (r.submit_by ? '<span class="st-muted">提交：' + esc(r.submit_by) + '</span>' : '') + '</td>'
      + '<td class="st-mono"><span class="st-muted">'
      + esc(String(r.published_at || '').replace('T', ' ').slice(0, 16) || '—') + '</span></td>'
      + '<td class="st-cell-stack">' + depsHtml + '</td>'
      + '<td class="st-cell-stack">' + svr + '</td>'
      + '<td class="st-ops"><div class="st-ops-grid">' + ops + '</div></td>';
  }

  function rowsOfGroup(g) {
    var ids = [];
    if (g.server) ids.push(g.server.id);
    if (g.client) ids.push(g.client.id);
    if (g.single) ids.push(g.single.id);
    return ids;
  }

  function latestPublished(rows) {
    // 组内最新「已上架」时间（截断到分钟；无已上架版本返回空串）
    var best = '';
    rows.forEach(function (r) {
      if (r.status !== 'published' || !r.published_at) return;
      var t = String(r.published_at);
      if (t > best) best = t;
    });
    return best ? best.replace('T', ' ').slice(0, 16) : '';
  }

  function groupHeaderHtml(g, allRows) {
    var t = groupTitleOf(g);
    var ids = rowsOfGroup(g);
    var rows = allRows.filter(function (r) { return ids.indexOf(r.id) >= 0; });
    var cnt = { pending: 0, published: 0, off: 0 };
    rows.forEach(function (r) {
      if (r.status === 'pending') cnt.pending++;
      else if (r.status === 'published') cnt.published++;
      else cnt.off++;
    });
    var st = '';
    if (cnt.pending) st += chip('待审核 ' + cnt.pending, 'warn') + ' ';
    if (cnt.published) st += chip('已上架 ' + cnt.published, 'ok') + ' ';
    if (cnt.off) st += chip('已下架/驳回 ' + cnt.off, 'muted');
    var svr = '';
    if (g.server && g.server.installed_version) {
      svr = chip('部署中 v' + esc(g.server.installed_version), 'ok')
        + (g.server.srv_enabled === false ? ' ' + chip('已停用', 'warn') : '')
        + (g.server.restart_pending ? ' ' + chip('待重启', 'bad') : '');
    } else if (g.server) {
      svr = '<span class="st-muted">未部署</span>';
    } else {
      svr = '<span class="st-muted">—（本地/依赖）</span>';
    }
    var kindChips = '';
    if (g.kind === 'pair') kindChips = chip('C 类配对 · 前端包 + 后端包', 'info');
    else if (g.kind === 'client') kindChips = chip('客户端工件 · 服务器工件未上架', 'warn');
    var expanded = gs(g.key).expanded;
    var base = (g.server || g.client || g.single);
    return '<td class="st-cell-stack">'
      + '<button type="button" class="st-group-toggle" data-group-toggle="' + esc(g.key) + '" title="展开/折叠该插件的版本列表">'
      + (expanded ? '▾' : '▸') + '</button>'
      + '<span class="st-grp-title"><b>' + esc(t.name) + '</b>'
      + '<span class="st-muted">' + esc(t.id) + '</span></span>'
      + (kindChips ? ' ' + kindChips : '')
      + '</td>'
      + '<td class="st-cell-stack st-mono"><span class="st-muted">—</span></td>'
      + '<td>' + chip(runtimeLabel(base.runtime), RUNTIME_KIND[base.runtime] || 'muted') + '</td>'
      + '<td class="st-cell-stack">' + (st || '<span class="st-muted">—</span>') + '</td>'
      + '<td class="st-mono"><span class="st-muted">'
      + esc(latestPublished(rows) || '—') + '</span></td>'
      + '<td><span class="st-muted">—（见各版本行）</span></td>'
      + '<td class="st-cell-stack">' + svr + '</td>'
      + '<td class="st-ops">' + groupOpsHtml(g, rows) + '</td>';
  }

  function groupOpsHtml(g, rows) {
    // 组级操作（2026-10-12）：点击后对**该插件全部版本**生效；图标按钮 + 悬停中文标签。
    if (!canReview) return '<span class="st-muted">待审</span>';
    var html = '';
    var GID = 'data-group="' + esc(g.key) + '"';
    var hasPub = rows.some(function (r) { return r.status === 'published'; });
    if (hasPub) {
      html += opBtn('gdelist', GID, '下架（全部版本）');
    }
    if (g.server && g.server.installed_version && canSrv) {
      html += g.server.srv_enabled === false
        ? opBtn('gsrv_enable', GID + ' data-en="1"', '启用（服务器端）')
        : opBtn('gsrv_disable', GID + ' data-en="0"', '停用（服务器端）');
      html += opBtn('gwithdraw', GID, '卸载（撤下后端包）');
    }
    return '<div class="st-ops-grid">' + (html || '<span class="st-muted">—</span>') + '</div>';
  }

  function subtabHtml(g) {
    var st = gs(g.key);
    function sideBtn(tab, label, entry) {
      var act = st.subtab === tab;
      var extra = '';
      if (entry) {
        if (tab === 'backend' && entry.installed_version) extra = '部署中 v' + esc(entry.installed_version);
        else if (tab === 'backend') extra = '未部署';
        else extra = '可安装';
      }
      return '<button type="button" class="st-subtab-btn' + (act ? ' active' : '') + '"'
        + ' data-subtab="' + tab + '" data-group="' + esc(g.key) + '">'
        + esc(label) + (extra ? ' <span class="st-muted">· ' + extra + '</span>' : '') + '</button>';
    }
    return '<td colspan="8" class="st-subtab-cell">'
      + '<span class="st-muted">工件切换：</span>'
      + sideBtn('backend', '后端包（服务器工件）', g.server)
      + sideBtn('frontend', '前端包（客户端工件）', g.client)
      + '</td>';
  }

  function render() {
    var thead = document.getElementById('st-thead');
    var tbody = document.getElementById('st-rows');
    var wrap = document.getElementById('st-table-wrap');
    var statsBox = document.getElementById('st-stats');
    var scopeBox = document.getElementById('st-scope');
    if (filter === 'stats') {
      wrap.hidden = true;
      statsBox.hidden = false;
      if (scopeBox) scopeBox.style.display = 'none';
      renderStats();
      return;
    }
    wrap.hidden = false;
    statsBox.hidden = true;
    if (scopeBox) scopeBox.style.display = '';
    thead.innerHTML = '<th>插件（组内为各版本）</th><th>版本</th><th>类别</th><th>状态</th><th>上架时间</th><th>依赖</th><th>服务器侧</th><th style="text-align:right">操作</th>';
    var allRows = flatRows();
    function passFilter(r) {
      if (filter === 'pending') return r.status === 'pending';
      if (filter === 'published') return r.status === 'published';
      return r.status === 'delisted' || r.status === 'rejected';
    }
    var groups = buildGroups();
    tbody.innerHTML = '';
    var any = false;
    // 2026-10-12 优化：插件/依赖 由上下分段改为**二级页签范围筛选**（依赖不再需要下拉翻找）
    function isDepGroup(g) { return (g.server || g.client || g.single).runtime === 'dep-slot'; }
    function renderGroups(list) {
      list.forEach(function (g) {
      var ids = rowsOfGroup(g);
      var tab = gs(g.key).subtab;
      var mine = allRows.filter(function (r) {
        if (ids.indexOf(r.id) < 0 || !passFilter(r)) return false;
        // C 类配对：组内版本行按当前工件页签选边（前端包=客户端工件 / 后端包=服务器工件）
        if (g.kind === 'pair') {
          return tab === 'frontend' ? r.id === g.client.id : r.id === g.server.id;
        }
        return true;
      });
      if (!mine.length) return;   // 当前筛选（+页签）下该组无行 → 整组隐藏
      any = true;
      mine.sort(function (a, b) {
        var pa = String(a.published_at || ''), pb = String(b.published_at || '');
        if (pa !== pb) return pa < pb ? 1 : -1;        // 上架时间倒序
        return a.version < b.version ? 1 : a.version > b.version ? -1 : 0;  // 并列：版本倒序
      });
      var trH = document.createElement('tr');
      trH.className = 'st-group-row';
      trH.innerHTML = groupHeaderHtml(g, allRows);
      tbody.appendChild(trH);
      if (!gs(g.key).expanded) return;
      if (g.kind === 'pair') {
        var trS = document.createElement('tr');
        trS.className = 'st-subtab-row';
        trS.innerHTML = subtabHtml(g);
        tbody.appendChild(trS);
      }
      mine.forEach(function (r) {
        if (g.kind === 'pair') {
          r.side_label = (g.kind === 'pair' && r.id === g.client.id)
            ? '（前端包 · 客户端工件）' : '（后端包 · 服务器工件）';
        }
        var tr = document.createElement('tr');
        tr.className = 'st-version-row';
        tr.innerHTML = versionRowHtml(r, g);
        tbody.appendChild(tr);
      });
      });
    }
    renderGroups(groups.filter(function (g) { return (scope === 'dep') === isDepGroup(g); }));
    if (!any) {
      var EMPTY = { pending: '暂无待审核条目——上架新包后在这里审核', published: '暂无已上架条目',
                    off: '暂无已下架 / 已驳回条目' };
      var scoped = (scope === 'dep' ? '依赖' : '插件');
      tbody.innerHTML = '<tr><td colspan="8" class="admin-empty">'
        + (EMPTY[filter] || '暂无数据') + '（当前范围：' + scoped + '）</td></tr>';
    }
    syncTableOverflow();
  }

  function byKindOf(obj, kind) {
    // 统计拆表（需求④）：按 runtime 归类条目（dep-slot→依赖，其余→插件）
    var rt = {};
    packages.forEach(function (p) { rt[p.id] = p.runtime; });
    var out = {};
    Object.keys(obj).forEach(function (k) {
      var isDep = rt[k] === 'dep-slot';
      if ((kind === 'dep') === isDep) out[k] = obj[k];
    });
    return out;
  }

  function renderStats() {
    var box = document.getElementById('st-stats');
    box.textContent = '统计加载中…';
    fetch(API + '/stats').then(function (r) { return r.json(); }).then(function (d) {
      function list(obj, label) {
        var keys = Object.keys(obj || {});
        if (!keys.length) return '<div><h4>' + label + '</h4><p class="st-muted">暂无数据</p></div>';
        var rows = keys.slice(0, 12).map(function (k) {
          return '<tr><td>' + esc(k) + '</td><td>' + obj[k] + '</td></tr>';
        }).join('');
        return '<div><h4>' + label + (keys.length > 12 ? ' <span class="st-muted">（前 12 项）</span>' : '') + '</h4>'
          + '<div class="data-table-wrap"><table class="data-table"><thead><tr><th>项</th><th style="text-align:right">下载次数</th></tr></thead><tbody>' + rows + '</tbody></table></div></div>';
      }
      var via = d.by_via || {};
      box.innerHTML = '<p class="settings-desc">口径：仅统计<b>下载事件</b>（下载 ≠ 安装）；按用户列为内网审计口径。</p>'
        + '<div class="st-stat-cards">'
        +   '<div class="st-stat-card"><div class="st-stat-num">' + (d.total || 0) + '</div><div class="st-stat-label">总下载次数</div></div>'
        +   '<div class="st-stat-card"><div class="st-stat-num">' + (via.client || 0) + '</div><div class="st-stat-label">桌面端</div></div>'
        +   '<div class="st-stat-card"><div class="st-stat-num">' + (via.browser || 0) + '</div><div class="st-stat-label">浏览器</div></div>'
        + '</div>'
        + '<div class="st-stats-grid">'
        + list(byKindOf(d.by_package || {}, 'plugin'), '按条目 · 插件')
        + list(byKindOf(d.by_package || {}, 'dep'), '按条目 · 依赖')
        + list(d.by_version, '按版本')
        + list(d.by_day, '按日') + list(d.by_user, '按用户')
        + '</div>';
      syncTableOverflow();
    }).catch(function (e) { box.textContent = '统计加载失败：' + e; });
  }

  (function bindExpandAll() {
    var bar = document.querySelector('.st-tabs');
    if (!bar) return;
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'st-filter';
    b.id = 'st-expand-all';
    b.style.marginLeft = '10px';   // 二级页签（st-scope）占用了 auto 位置（2026-10-12）
    b.textContent = '全部展开';
    b.addEventListener('click', function () {
      // 任一已展开 → 全部折叠；否则全部展开（默认收缩，首次点击即"展开全部"）
      var anyExpanded = Object.keys(groupState).some(function (k) { return groupState[k].expanded; });
      buildGroups().forEach(function (g) {
        var st = gs(g.key);
        st.expanded = !anyExpanded;
      });
      b.textContent = anyExpanded ? '全部展开' : '全部折叠';
      render();
    });
    bar.appendChild(b);
  })();

  document.querySelectorAll('.st-filter').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.st-filter').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      filter = btn.getAttribute('data-f');
      render();
    });
  });

  // 二级页签：插件 / 依赖（2026-10-12 优化）——切换即重算状态计数与列表
  document.querySelectorAll('.st-scope-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.st-scope-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      scope = btn.getAttribute('data-scope') === 'dep' ? 'dep' : 'plugin';
      updateCounts();
      render();
    });
  });

  document.getElementById('st-rows').addEventListener('click', async function (ev) {
    // 展开板交互（2026-10-12 需求①）：分组折叠 + C 类工件页签切换——纯视图态，直接重渲染
    var tg = ev.target.closest ? ev.target.closest('[data-group-toggle]') : null;
    if (tg) {
      var k = tg.getAttribute('data-group-toggle');
      gs(k).expanded = !gs(k).expanded;
      render();
      return;
    }
    var sb = ev.target.closest ? ev.target.closest('[data-subtab]') : null;
    if (sb) {
      var g2 = sb.getAttribute('data-group');
      gs(g2).subtab = sb.getAttribute('data-subtab') === 'frontend' ? 'frontend' : 'backend';
      render();
      return;
    }
    // 组级操作（需求②）：点击后对全部版本生效——下架全体已上架版本 / 插件级停用·卸载
    var gb = ev.target.closest ? ev.target.closest('button[data-op^="g"][data-group]') : null;
    if (gb) {
      var gk = gb.getAttribute('data-group');
      var g = buildGroups().filter(function (x) { return x.key === gk; })[0];
      if (!g) return;
      var gop = gb.getAttribute('data-op');
      if (gop === 'gdelist') {
        var okd = await adDialog({ title: '下架（全部版本）', danger: true, okText: '全部下架',
          html: '把 <b>' + esc(gk) + '</b> 的**全部已上架版本**下架？（客户端可见性整体隐藏；'
            + '已装客户端不受影响）' });
        if (!okd) return;
        var ids = rowsOfGroup(g);
        for (var i = 0; i < ids.length; i++) {
          var rr = await post('/delist', { id: ids[i], all: true });
          if (!rr.data.ok) { toast('下架失败（' + ids[i] + '）：' + (rr.data.error || ''), true); load(); return; }
        }
        toast('已下架全部版本：' + gk);
        load();
        return;
      }
      if (gop === 'gsrv' || gop === 'gsrv_enable' || gop === 'gsrv_disable' || gop === 'gwithdraw') {
        var srv = g.server;
        if (!srv || !srv.installed_version) { toast('该插件未部署到服务器', true); return; }
        var isSrv = (gop === 'gsrv' || gop === 'gsrv_enable' || gop === 'gsrv_disable');
        var en = gb.getAttribute('data-en') === '1';
        var verb = isSrv ? (en ? '启用' : '停用') : '卸载（撤下后端包）';
        var ok2 = await adDialog({ title: '插件级操作', danger: !isSrv || !en, okText: verb,
          html: (isSrv
            ? verb + ' <b>' + esc(srv.id) + '</b> 的服务器端运行？<br>作用于当前部署版本 v' + esc(srv.installed_version) + '（插件级）。'
            : '把 <b>' + esc(srv.id) + '</b> v' + esc(srv.installed_version) + ' 的后端包从服务器撤下？<br>自动备份，可用「重新部署」恢复。') });
        if (!ok2) return;
        var path2 = isSrv ? '/srv-state' : '/withdraw';
        var body2 = isSrv
          ? { id: srv.id, version: srv.installed_version, enabled: en }
          : { id: srv.id, version: srv.installed_version, note: '组级卸载' };
        var r2 = await post(path2, body2);
        if (r2.data.ok) { toast(verb + '完成'); load(); }
        else { toast(verb + '失败：' + (r2.data.error || ''), true); }
        return;
      }
    }
    var btn = ev.target.closest ? ev.target.closest('button[data-op]') : null;
    if (!btn) return;
    var op = btn.getAttribute('data-op');
    var id = btn.getAttribute('data-id');
    var v = btn.getAttribute('data-v');
    var dlgOpts = {
      approve: { title: '发布确认', okText: '发布',
        html: '确认发布 <b>' + esc(id) + ' v' + esc(v) + '</b>？<br>server 条目将自动部署后端包；'
          + (pairInfo[id] ? '<b>C 类配对</b>：将同时发布 ' + esc(pairInfo[id].partner)
            + '（商店只出客户端工件卡）。<br>' : '')
          + '<b>装机冒烟请确认已做</b>。' },
      reject: { title: '驳回', input: true, danger: true, okText: '驳回', placeholder: '填写驳回原因（记入台账）',
        html: '驳回 <b>' + esc(id) + ' v' + esc(v) + '</b>：' },
      delist: { title: '下架确认', danger: true, okText: '下架',
        html: '下架 <b>' + esc(id) + ' v' + esc(v) + '</b>？<br>客户端可见性回落到旧版；已装客户端不降级。'
          + (pairInfo[id] ? '<br>配对条目 <b>' + esc(pairInfo[id].partner) + '</b> 将一并下架。' : '') },
      republish: { title: '重新上架', okText: '重新上架',
        html: '重新上架 <b>' + esc(id) + ' v' + esc(v) + '</b>？将回到待审核状态。' },
      redeploy: { title: '重新部署', okText: '重新部署',
        html: '把 <b>' + esc(id) + ' v' + esc(v) + '</b> 重新部署到服务器？<br>自动备份，可回滚。' },
      srv_disable: { title: '停用插件（服务器端）', danger: true, okText: '停用',
        html: '停用 <b>' + esc(id) + '</b> 的服务器端运行？<br>清单/主页即时隐藏；'
          + '路由随下次服务重启卸载；后端包保留，可随时启用。' },
      srv_enable: { title: '启用插件（服务器端）', okText: '启用',
        html: '启用 <b>' + esc(id) + '</b>？<br>清单/主页即时恢复；路由随下次服务重启加载。' },
      withdraw: { title: '卸载（撤下后端包）', danger: true, okText: '撤下',
        html: '把 <b>' + esc(id) + ' v' + esc(v) + '</b> 的后端包从服务器撤下？<br>'
          + '自动备份，可用「重新部署」原样恢复；条目与客户端前端副本不受影响。' },
    };
    if (op === 'redeploy') {                       // 依赖条目 → 部署到服务器（2026-10-08）
      var prow = packages.filter(function (p) { return p.id === id; })[0] || {};
      if (prow.runtime === 'dep-slot') {
        dlgOpts.redeploy = { title: '部署到服务器', okText: '部署',
          html: '把依赖组件 <b>' + esc(id) + ' v' + esc(v) + '</b> 部署到服务器？<br>'
            + '装入服务器 runtime/pylibs（客户端安装只落桌面端）；无需重启即生效。' };
      } else if (prow.runtime === 'server' && prow.installed_version
                 && String(prow.installed_version) !== String(v)) {
        // 多版本独立配置（2026-10-12）：部署非当前版本 = 版本切换（含回退）
        dlgOpts.redeploy = { title: '部署到该版本', okText: '部署',
          html: '把 <b>' + esc(id) + '</b> 从当前部署的 v' + esc(prow.installed_version)
            + ' 切换为 <b>v' + esc(v) + '</b>？<br>自动备份，可用「部署到该版本」切回。' };
      }
    }
    var r = await adDialog(dlgOpts[op] || { title: '确认', html: esc(op + '？') });
    var note = '';
    if (op === 'reject') {
      if (r === null) return;
      note = r || '';
    } else if (!r) return;
    var path = op === 'redeploy' ? '/redeploy' : '/review';
    var body = op === 'redeploy' ? { id: id, version: v } : { id: id, version: v, action: op, note: note };
    if (op === 'delist') path = '/delist';
    if (op === 'delist') body = { id: id, version: v, note: note };
    if (op === 'srv_disable' || op === 'srv_enable') {
      path = '/srv-state';
      body = { id: id, version: v, enabled: op === 'srv_enable' };
    }
    if (op === 'withdraw') {
      path = '/withdraw';
      body = { id: id, version: v, note: note };
    }
    post(path, body).then(function (r2) {
      if (r2.data.ok) {
        toast(op === 'approve' ? '已发布' + (r2.data.status === 'published' ? '：客户端可见' : '')
          : op === 'redeploy' ? '重新部署完成'
          : op === 'withdraw' ? '已撤下后端包' + (r2.data.restart_hint ? '（' + r2.data.restart_hint + '）' : '')
          : op === 'srv_disable' ? '已停用（' + (r2.data.restart_hint || '') + '）'
          : op === 'srv_enable' ? '已启用' : '操作成功');
        load();
      } else {
        toast('操作失败：' + (r2.data.error || '未知错误'), true);
      }
    }).catch(function (e) { toast('请求异常：' + e, true); });
  });

  function load() {
    fetch(API + '/list').then(function (r) { return r.json(); }).then(function (d) {
      packages = d.packages || [];
      installedNames = d.installed_names || [];
      canReview = d.can_review !== false;
      canSrv = d.super_admin === true;
      computePairs();
      depProvides = {};
      packages.forEach(function (p) {
        if (p.runtime !== 'dep-slot') return;
        (p.versions || []).forEach(function (v) {
          if (v.status === 'published') (p.provides || []).forEach(function (x) {
            depProvides[String(x).toLowerCase()] = true;
          });
        });
      });
      updateCounts();
      render();
    }).catch(function (e) { toast('目录加载失败：' + e, true); });
  }
  window.addEventListener('resize', syncTableOverflow);
  load();
})();
