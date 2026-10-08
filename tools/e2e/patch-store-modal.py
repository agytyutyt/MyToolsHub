# -*- coding: utf-8 -*-
"""商店页三项优化补丁（2026-10-08）：详情悬浮窗 / 右侧竖排按钮 / reveal 带路径。"""
import io

p = "plugins/store/frontend/store.js"
s = io.open(p, encoding="utf-8").read()

# ── 1) 详情：内联展开 → 悬浮窗 ──
old = '''  function detailHtml(e) {
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
  }'''
new = '''  // ---------- 详情悬浮窗（点击卡片打开；ESC/遮罩/× 关闭） ----------
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
    rows.push('<div class="st-md-head"><span class="st-icon st-md-icon">' + esc(e.icon || '\\u{1F9E9}'.replace('\\\\u{1F9E9}', '\\uD83E\\uDDE9'))
      + '</span><div><p class="st-name">' + esc(e.name) + '</p>'
      + '<div class="st-badges"><span class="st-badge ' + runtimeClass(e.runtime) + '">'
      + humanRuntime(e.runtime) + '</span>'
      + (e.deps_mode === 'venv' ? '<span class="st-badge venv">独立环境</span>' : '')
      + (IN_SHELL && st.installed ? '<span class="st-badge local">已安装 v'
        + esc((st.entry || {}).version || '') + '</span>' : '')
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
  }'''
assert old in s, "detailHtml 锚点"
s = s.replace(old, new, 1)

# ── 2) 卡片：右竖排按钮 + 点击开详情 ──
old2 = '''      var card = document.createElement('div');
      card.className = 'st-card st-rt-' + runtimeClass(e.runtime);
      card.style.animationDelay = Math.min(idx * 35, 350) + 'ms';
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
      grid.appendChild(card);'''
new2 = '''      var card = document.createElement('div');
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
        +   '<div class="st-meta">v' + esc(e.version) + ' · ' + fmtSize(e.size)
        +     (e.runtime === 'server' && e.frontend_size ? ' · 前端 ' + fmtSize(e.frontend_size) : '')
        +     (st.entry && st.entry.version && verGt(e.version, st.entry.version)
                ? ' · 本机 v' + esc(st.entry.version) : '')
        +   '<span class="st-more">详情 ›</span></div>'
        + '</div>'
        + '<div class="st-card-side">' + buttonsHtml(e) + '</div>';
      grid.appendChild(card);'''
assert old2 in s, "卡片锚点"
s = s.replace(old2, new2, 1)

# ── 3) 网格点击：按钮优先；否则卡片 → 详情悬浮窗 ──
old3 = '''  $('#st-grid').addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('button[data-act]') : null;
    if (btn) onAction(btn.getAttribute('data-act'), btn);
  });'''
new3 = '''  $('#st-grid').addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('button[data-act]') : null;
    if (btn) { onAction(btn.getAttribute('data-act'), btn); return; }
    var card = ev.target.closest ? ev.target.closest('.st-card') : null;
    if (card && card.getAttribute('data-id')) {
      var e = findEntry(card.getAttribute('data-id'));
      if (e) openDetail(e);                 // 点击卡片 → 详情悬浮窗（操作按钮除外）
    }
  });'''
assert old3 in s, "网格点击锚点"
s = s.replace(old3, new3, 1)

# ── 4) 移除 detail act 分支 ──
old4 = '''    if (act === 'detail') {
      btn.parentNode.classList.toggle('open');
      btn.textContent = btn.parentNode.classList.contains('open') ? '详情 ▴' : '详情 ▾';
      return;
    }
'''
assert old4 in s, "detail act 锚点"
s = s.replace(old4, "", 1)

# ── 5) 下载完成：reveal 带路径 + 失败浮出 ──
old5 = '''                } else if (p.status === 'done') {
                  clearInterval(timer); restore();
                  msg('已保存到本机 downloads（已打开文件夹）');
                  fetch('/local-api/store/reveal', { method: 'POST' });
                } else if (p.status === 'error') {'''
new5 = '''                } else if (p.status === 'done') {
                  clearInterval(timer); restore();
                  msg('已保存到本机 downloads（已打开文件夹）');
                  fetch('/local-api/store/reveal', { method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: p.path || '' }) })
                    .then(function (r) { return r.json(); })
                    .then(function (x) {
                      if (!x.ok) msg('已保存，但打开文件夹失败：' + (x.error || '')
                        + '——文件在下载目录 downloads 内', true);
                    })
                    .catch(function () { /* 打开动作失败不改变已下载事实 */ });
                } else if (p.status === 'error') {'''
assert old5 in s, "下载完成锚点"
s = s.replace(old5, new5, 1)

io.open(p, "w", encoding="utf-8", newline="").write(s)
print("store.js patched")
