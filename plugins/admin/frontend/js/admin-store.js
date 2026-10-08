/* 商店管理页逻辑（admin-store.html 配套；?v=1 起版，改动须递增引用处）。
 * 数据面：/plugin/admin/store/list|upload|review|redeploy|delist|meta|stats。
 * 权限：全部登录用户（需求⑦ 不分角色）；服务器侧部署走当前形态 inspect/apply。 */
(function () {
  'use strict';

  var API = '/plugin/admin/store';
  var filter = 'pending';
  var packages = [];
  var installedNames = [];   // 主包运行时自带依赖（服务端 installed-deps）
  var canReview = true;      // 审核/下架/部署权限（2026-10-08：仅管理员/超管；提交全员）
  var depProvides = {};      // 已上架依赖条目的 provides 集合（name→true）

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
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
  // 自绘对话框 = 框架级组件（static/js/jz-dialog.js；2026-10-08 解耦，样式随脚本注入）
  var adDialog = function (opts) { return window.JZDialog.open(opts); };

  function chip(text, kind) {
    return '<span class="st-chip st-chip-' + kind + '">' + esc(text) + '</span>';
  }
  var STATUS_KIND = { pending: 'warn', published: 'ok', rejected: 'bad', delisted: 'muted' };
  var RUNTIME_KIND = { local: 'info', server: 'primary', 'dep-slot': 'purple' };

  function updateCounts() {
    var rows = flatRows();
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
            (d.frontend ? '（前端包 ' + d.frontend + '）' : '');
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
          frontend_file: v.frontend_file, deps_mode: p.deps_mode,
          requires: p.requires,
        });
      });
    });
    return rows;
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

  function render() {
    var thead = document.getElementById('st-thead');
    var tbody = document.getElementById('st-rows');
    var wrap = document.getElementById('st-table-wrap');
    var statsBox = document.getElementById('st-stats');
    if (filter === 'stats') {
      wrap.hidden = true;
      statsBox.hidden = false;
      renderStats();
      return;
    }
    wrap.hidden = false;
    statsBox.hidden = true;
    thead.innerHTML = '<th>条目</th><th>版本</th><th>类别</th><th>状态</th><th>依赖</th><th>服务器侧</th><th style="text-align:right">操作</th>';
    var rows = flatRows().filter(function (r) {
      if (filter === 'pending') return r.status === 'pending';
      if (filter === 'published') return r.status === 'published';
      return r.status === 'delisted' || r.status === 'rejected';
    });
    tbody.innerHTML = '';
    if (!rows.length) {
      var EMPTY = { pending: '暂无待审核条目——上架新包后在这里审核', published: '暂无已上架条目',
                    off: '暂无已下架 / 已驳回条目' };
      tbody.innerHTML = '<tr><td colspan="7" class="admin-empty">' + (EMPTY[filter] || '暂无数据') + '</td></tr>';
      syncTableOverflow();
      return;
    }
    rows.sort(function (a, b) { return a.id < b.id ? -1 : a.id > b.id ? 1 : 0; });
    rows.forEach(function (r) {
      var tr = document.createElement('tr');
      var svr = '<span class="st-muted">—</span>';
      if (r.runtime === 'server' && r.status === 'published') {
        svr = chip('已部署 v' + esc(r.installed_version || '—'), 'ok')
          + (r.restart_pending ? ' ' + chip('待重启', 'bad') : '');
      } else if (r.runtime === 'server') {
        svr = '<span class="st-muted">发布时自动部署</span>';
      }
      // 依赖核验（✓ 满足 / ✗ 不满足）：主包自带或已上架依赖条目可满足 → ✓
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
      if (!canReview) {
        // 权限门控（2026-10-08 修订）：非管理员仅可提交上架；审核/下架/部署按钮不出现
        ops = '<span class="st-muted">待管理员审核</span>';
      } else if (r.status === 'pending') {
        ops += '<button class="admin-btn small primary" data-op="approve" data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '"'
          + (unsat.length ? ' disabled title="依赖不满足：' + esc(unsat.join('、')) + '——待对应依赖组件包上架"' : '')
          + '>通过（发布）</button> '
          + '<button class="admin-btn small" data-op="reject" data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '">驳回</button>';
      } else if (r.status === 'published') {
        ops += '<button class="admin-btn small" data-op="delist" data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '">下架</button>';
        if (r.runtime === 'server') {
          ops += ' <button class="admin-btn small" data-op="redeploy" data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '">重新部署</button>';
        }
      } else if (r.status === 'delisted') {
        ops += '<button class="admin-btn small" data-op="republish" data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '">重新上架</button>';
      }
      tr.innerHTML =
        '<td class="st-cell-stack"><b>' + esc(r.name) + '</b>'
        + '<span class="st-muted">' + esc(r.id) + (r.deps_mode === 'venv' ? ' · venv' : '') + '</span></td>'
        + '<td class="st-cell-stack st-mono"><span>v' + esc(r.version) + '</span>'
        + (r.frontend_file ? chip('前端包成对', 'muted') : '') + '</td>'
        + '<td>' + chip(runtimeLabel(r.runtime), RUNTIME_KIND[r.runtime] || 'muted') + '</td>'
        + '<td class="st-cell-stack">' + chip(statusLabel(r.status), STATUS_KIND[r.status] || 'muted')
        + (r.submit_by ? '<span class="st-muted">提交：' + esc(r.submit_by) + '</span>' : '') + '</td>'
        + '<td class="st-cell-stack">' + depsHtml + '</td>'
        + '<td class="st-cell-stack">' + svr + '</td>'
        + '<td class="st-ops">' + ops + '</td>';
      tbody.appendChild(tr);
    });
    syncTableOverflow();
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
        + list(d.by_package, '按条目') + list(d.by_version, '按版本')
        + list(d.by_day, '按日') + list(d.by_user, '按用户')
        + '</div>';
      syncTableOverflow();
    }).catch(function (e) { box.textContent = '统计加载失败：' + e; });
  }

  document.querySelectorAll('.st-filter').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.st-filter').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      filter = btn.getAttribute('data-f');
      render();
    });
  });

  document.getElementById('st-rows').addEventListener('click', async function (ev) {
    var btn = ev.target.closest ? ev.target.closest('button[data-op]') : null;
    if (!btn) return;
    var op = btn.getAttribute('data-op');
    var id = btn.getAttribute('data-id');
    var v = btn.getAttribute('data-v');
    var dlgOpts = {
      approve: { title: '发布确认', okText: '发布',
        html: '确认发布 <b>' + esc(id) + ' v' + esc(v) + '</b>？<br>server 条目将自动部署后端包；'
          + '<b>装机冒烟请确认已做</b>。' },
      reject: { title: '驳回', input: true, danger: true, okText: '驳回', placeholder: '填写驳回原因（记入台账）',
        html: '驳回 <b>' + esc(id) + ' v' + esc(v) + '</b>：' },
      delist: { title: '下架确认', danger: true, okText: '下架',
        html: '下架 <b>' + esc(id) + ' v' + esc(v) + '</b>？<br>客户端可见性回落到旧版；已装客户端不降级。' },
      republish: { title: '重新上架', okText: '重新上架',
        html: '重新上架 <b>' + esc(id) + ' v' + esc(v) + '</b>？将回到待审核状态。' },
      redeploy: { title: '重新部署', okText: '重新部署',
        html: '把 <b>' + esc(id) + ' v' + esc(v) + '</b> 重新部署到服务器？<br>自动备份，可回滚。' },
    };
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
    post(path, body).then(function (r2) {
      if (r2.data.ok) {
        toast(op === 'approve' ? '已发布' + (r2.data.status === 'published' ? '：客户端可见' : '')
          : op === 'redeploy' ? '重新部署完成' : '操作成功');
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
