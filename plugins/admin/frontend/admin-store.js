/* 商店管理页逻辑（admin-store.html 配套；?v=1 起版，改动须递增引用处）。
 * 数据面：/plugin/admin/store/list|upload|review|redeploy|delist|meta|stats。
 * 权限：全部登录用户（需求⑦ 不分角色）；服务器侧部署走当前形态 inspect/apply。 */
(function () {
  'use strict';

  var API = '/plugin/admin/store';
  var filter = 'pending';
  var packages = [];

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

  // ---------------- 上架表单 ----------------
  var runtimeSel = document.getElementById('up-runtime');
  function syncFormByRuntime() {
    var rt = runtimeSel.value;
    document.getElementById('row-frontend').hidden = rt !== 'server';
    document.getElementById('row-provides').hidden = rt !== 'dep-slot';
    document.getElementById('row-deps-mode').hidden = rt === 'dep-slot';
    document.getElementById('up-file').previousElementSibling && (document
      .querySelector('label[for="up-file"]').textContent = rt === 'server' ? '后端包' : '包文件');
  }
  runtimeSel.addEventListener('change', syncFormByRuntime);
  syncFormByRuntime();

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
    fd.append('provides', document.getElementById('up-provides').value);
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
        });
      });
    });
    return rows;
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
    thead.innerHTML = '<th>条目</th><th>版本</th><th>类别</th><th>状态</th><th>服务器侧</th><th style="text-align:right">操作</th>';
    var rows = flatRows().filter(function (r) {
      if (filter === 'pending') return r.status === 'pending';
      if (filter === 'published') return r.status === 'published';
      return r.status === 'delisted' || r.status === 'rejected';
    });
    tbody.innerHTML = '';
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="admin-empty">（空）</td></tr>';
      return;
    }
    rows.sort(function (a, b) { return a.id < b.id ? -1 : a.id > b.id ? 1 : 0; });
    rows.forEach(function (r) {
      var tr = document.createElement('tr');
      var svr = '';
      if (r.runtime === 'server' && r.status === 'published') {
        svr = '已部署 v' + esc(r.installed_version || '—')
          + (r.restart_pending ? ' <span class="dep-badge bad">待重启</span>' : '');
      } else if (r.runtime === 'server') {
        svr = '待发布自动部署';
      }
      var ops = '';
      if (r.status === 'pending') {
        ops += '<button class="admin-btn small primary" data-op="approve" data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '">通过（发布）</button> '
          + '<button class="admin-btn small" data-op="reject" data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '">驳回</button>';
      } else if (r.status === 'published') {
        ops += '<button class="admin-btn small" data-op="delist" data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '">下架</button>';
        if (r.runtime === 'server') {
          ops += ' <button class="admin-btn small" data-op="redeploy" data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '">重新部署</button>';
        }
      } else if (r.status === 'delisted') {
        ops += '<button class="admin-btn small" data-op="republish" data-id="' + esc(r.id) + '" data-v="' + esc(r.version) + '">重新上架</button>';
      }
      tr.innerHTML = '<td><b>' + esc(r.name) + '</b><br><span style="color:#5f6368;font-size:12px">' + esc(r.id)
        + (r.deps_mode === 'venv' ? ' · venv' : '') + '</span></td>'
        + '<td>v' + esc(r.version) + (r.frontend_file ? '<br><span style="font-size:12px">前端包成对</span>' : '') + '</td>'
        + '<td>' + runtimeLabel(r.runtime) + '</td>'
        + '<td>' + statusLabel(r.status) + '<br><span style="color:#5f6368;font-size:12px">'
        + esc(r.submit_by || '') + '</span></td>'
        + '<td>' + svr + '</td>'
        + '<td style="text-align:right">' + ops + '</td>';
      tbody.appendChild(tr);
    });
  }

  function renderStats() {
    var box = document.getElementById('st-stats');
    box.textContent = '统计加载中…';
    fetch(API + '/stats').then(function (r) { return r.json(); }).then(function (d) {
      function list(obj, label) {
        var keys = Object.keys(obj || {});
        if (!keys.length) return '<p class="settings-desc">' + label + '：暂无数据</p>';
        var rows = keys.slice(0, 12).map(function (k) {
          return '<tr><td>' + esc(k) + '</td><td>' + obj[k] + '</td></tr>';
        }).join('');
        return '<p class="settings-desc">' + label + '</p><div class="data-table-wrap"><table class="data-table"><thead><tr><th>项</th><th>下载次数</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
      }
      box.innerHTML = '<p class="settings-desc">口径：仅统计<b>下载事件</b>（下载 ≠ 安装）；按用户列为内网审计口径。'
        + '总计 <b>' + (d.total || 0) + '</b> 次。'
        + (d.by_via && d.by_via.client ? '桌面端 ' + d.by_via.client + ' 次 / 浏览器 ' + (d.by_via.browser || 0) + ' 次。' : '')
        + '</p>'
        + list(d.by_package, '按条目') + list(d.by_version, '按版本') + list(d.by_day, '按日') + list(d.by_user, '按用户');
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

  document.getElementById('st-rows').addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('button[data-op]') : null;
    if (!btn) return;
    var op = btn.getAttribute('data-op');
    var id = btn.getAttribute('data-id');
    var v = btn.getAttribute('data-v');
    var notes = {
      approve: '确认发布 ' + id + ' v' + v + '？（server 条目将自动部署后端包；装机冒烟请确认已做）',
      reject: '驳回原因（记入台账）：',
      delist: '下架 ' + id + ' v' + v + '？（客户端可见性回落到旧版；已装客户端不降级）',
      republish: '重新上架 ' + id + ' v' + v + '？将回到待审核状态。',
      redeploy: '重新部署 ' + id + ' v' + v + ' 到服务器？（自动备份，可回滚）',
    };
    var note = '';
    if (op === 'reject') {
      note = prompt(notes[op]) || '';
      if (note === null) return;
    } else {
      if (!confirm(notes[op] || '确认？')) return;
    }
    var path = op === 'redeploy' ? '/redeploy' : '/review';
    var body = op === 'redeploy' ? { id: id, version: v } : { id: id, version: v, action: op, note: note };
    if (op === 'delist') path = '/delist';
    if (op === 'delist') body = { id: id, version: v, note: note };
    post(path, body).then(function (r) {
      if (r.data.ok) {
        toast(op === 'approve' ? '已发布' + (r.data.status === 'published' ? '：客户端可见' : '')
          : op === 'redeploy' ? '重新部署完成' : '操作成功');
        load();
      } else {
        toast('操作失败：' + (r.data.error || '未知错误'), true);
      }
    }).catch(function (e) { toast('请求异常：' + e, true); });
  });

  function load() {
    fetch(API + '/list').then(function (r) { return r.json(); }).then(function (d) {
      packages = d.packages || [];
      render();
    }).catch(function (e) { toast('目录加载失败：' + e, true); });
  }
  load();
})();
