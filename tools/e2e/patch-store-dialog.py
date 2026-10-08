# -*- coding: utf-8 -*-
"""商店页自绘对话框补丁（2026-10-08）：全部原生 confirm 替换为 stDialog（Promise）。

- stDialog(opts) → Promise<boolean|string|null>：opts {title, html, okText, cancelText, danger, input, placeholder}
  ESC/遮罩=取消；Enter=确定；input 模式返回字符串（空串=确定，null=取消）。
- onAction / preflightAndInstall 转 async，四处确认全部换自绘。
"""
import io

p = "plugins/store/frontend/store.js"
s = io.open(p, encoding="utf-8").read()

# ── 1) stDialog 助手（插在 msg 之后）──
anchor = "  function fmtSize(n) {"
assert anchor in s
dlg = '''  // ---------- 自绘对话框（替换原生 confirm/prompt：桌面 WebView2 原生框带来源标题、样式不可控） ----------
  function stDialog(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var dlg = document.createElement('div');
      dlg.className = 'st-modal st-dlg';
      dlg.innerHTML = '<div class="st-modal-panel" role="alertdialog">'
        + '<h3 class="st-dlg-title">' + esc(opts.title || '确认') + '</h3>'
        + '<div class="st-dlg-body">' + (opts.html || '') + '</div>'
        + (opts.input ? '<input class="st-dlg-input" type="text" value="'
            + esc(opts.inputValue || '') + '" placeholder="' + esc(opts.placeholder || '') + '">' : '')
        + '<div class="st-dlg-btns">'
        + '<button type="button" class="st-btn-ghost" data-k="0">' + esc(opts.cancelText || '取消') + '</button>'
        + '<button type="button" class="' + (opts.danger ? 'st-btn-danger' : 'st-btn-install')
        + '" data-k="1">' + esc(opts.okText || '确定') + '</button>'
        + '</div></div>';
      var inp = dlg.querySelector('.st-dlg-input');
      function done(val) {
        dlg.classList.remove('show');
        setTimeout(function () { if (dlg.parentNode) dlg.parentNode.removeChild(dlg); }, 220);
        document.removeEventListener('keydown', onKey);
        resolve(val);
      }
      function cancelVal() { return opts.input ? null : false; }
      function okVal() { return opts.input ? (inp ? inp.value : '') : true; }
      function onKey(ev) {
        if (ev.key === 'Escape') done(cancelVal());
        else if (ev.key === 'Enter') done(okVal());
      }
      dlg.addEventListener('click', function (ev) {
        if (ev.target === dlg) return done(cancelVal());   // 点遮罩 = 取消
        var b = ev.target.closest ? ev.target.closest('button[data-k]') : null;
        if (!b) return;
        done(b.getAttribute('data-k') === '0' ? cancelVal() : okVal());
      });
      document.body.appendChild(dlg);
      document.addEventListener('keydown', onKey);
      requestAnimationFrame(function () { dlg.classList.add('show'); });
      setTimeout(function () {
        var f = inp || dlg.querySelector('button[data-k="1"]');
        if (f) f.focus();
      }, 40);
    });
  }

''' + anchor
s = s.replace(anchor, dlg, 1)

# ── 2) preflightAndInstall → async + 自绘确认 ──
old = s[s.find("  function preflightAndInstall(e) {"):s.find("  function st_upgradableConfirm(e) {")]
assert old and "confirm(" in old, "preflight 段定位失败"
new = '''  async function preflightAndInstall(e) {
    if (!IN_SHELL) { return; }
    var wantDeps = false;
    msg('正在预检依赖 …');
    var pre = { installable: true, deps: [] };
    try {
      pre = (await post('/local-api/store/preflight', { id: e.id, version: e.version })) || pre;
    } catch (err) { /* 预检失败不阻断安装（D-8 口径：提示不阻断） */ }
    var missing = (pre.deps || []).filter(function (d) {
      return d.state !== 'ok' && d.required !== false && !d.informational;
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
      ok = await stDialog({ title: '升级确认', okText: '升级',
        html: '升级到 <b>v' + esc(e.version) + '</b>？<br>hybrid 插件将触发能力层重启（会话已保留，免重登）。' });
      if (!ok) return;
    } else {
      ok = await stDialog({ title: '安装确认', okText: '安装',
        html: '安装「<b>' + esc(e.name) + '</b>」v' + esc(e.version) + '？' });
      if (!ok) return;
    }
    msg('正在安装 ' + e.name + ' …');
    post('/local-api/store/install', { id: e.id, version: e.version, include_deps: wantDeps })
      .then(async function (d) {
        if (!d.ok) { msg('安装失败：' + (d.error || '未知错误'), true); return; }
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
        loadLocalState().then(render);
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

'''
s = s.replace(old, new, 1)

# ── 3) onAction → async；卸载两处换自绘 ──
old2 = "  function onAction(act, btn) {"
assert old2 in s
s = s.replace(old2, "  async function onAction(act, btn) {", 1)

old3 = """    if (act === 'uninstall-dep') {
      if (!confirm('卸载依赖组件 ' + id + '？\\n依赖组件无独立业务数据；正在被映射的原生库将在下次启动客户端后清除。')) return;"""
new3 = """    if (act === 'uninstall-dep') {
      if (!await stDialog({ title: '卸载依赖组件', danger: true, okText: '卸载',
        html: '卸载依赖组件 <b>' + esc(id) + '</b>？<br>依赖组件无独立业务数据；正在被映射的原生库将在下次启动客户端后清除。' })) return;"""
assert old3 in s, "uninstall-dep 锚点"
s = s.replace(old3, new3, 1)

old4 = """    if (act === 'uninstall') {
      if (!confirm('卸载 ' + id + '？\\n本地包副本将被删除。\\n点击「确定」进入下一步（选择是否清除数据）。')) return;
      var purge = confirm('是否同时清除该插件的数据？\\n\\n'
        + '数据 = 数据根 plugins 下的插件目录（配置 / 缓存 / 本地业务数据）。\\n'
        + '确定 = 清除数据；取消 = 保留数据（可稍后再装回）。');"""
new4 = """    if (act === 'uninstall') {
      if (!await stDialog({ title: '卸载插件', danger: true, okText: '下一步',
        html: '卸载 <b>' + esc(id) + '</b>？<br>本地包副本将被删除；下一步选择是否清除数据。' })) return;
      var purge = await stDialog({ title: '是否清除数据', danger: true, okText: '清除数据', cancelText: '保留数据',
        html: '清除该插件的数据（数据根 plugins 下的插件目录：配置 / 缓存 / 本地业务数据）？<br>'
          + '选择「保留数据」可稍后再装回。' });"""
assert old4 in s, "uninstall 锚点"
s = s.replace(old4, new4, 1)

io.open(p, "w", encoding="utf-8", newline="").write(s)
print("store.js dialog patch ok")
