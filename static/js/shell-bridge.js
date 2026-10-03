/* 壳↔Web 桥适配器（批次 2 T12；方案 §7.3）。
 * 页面侧唯一感知壳的文件：浏览器路径下 window.jzShell 不存在，调用点判空即与现状一致。
 * 壳内：探测 pywebview.api → 暴露 window.jzShell（version/engine/openExternal 至多三项）
 * + 注入 html.jz-shell 结构类 + 站外链接/新窗转交壳（导航锁定的页面侧配合）。
 * 本文件随主界面 static 双端同源（主包与客户端包各带一份），改动随主界面发版节奏。 */
(function () {
  'use strict';
  if (window.jzShell !== undefined) return;

  var ORIGIN = location.origin;

  function wireExternalLinks(shell) {
    // 站外 <a> 一律转交壳打开；站内不动。壳侧另有 loaded 守卫兜底（JS 跳转场景）。
    document.addEventListener('click', function (e) {
      var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
      if (!a) return;
      var href = a.getAttribute('href') || '';
      if (!href || href.charAt(0) === '#' || href.indexOf('javascript:') === 0) return;
      var url;
      try { url = new URL(href, location.href); } catch (_) { return; }
      if (url.origin === ORIGIN) return;
      if (shell.openExternal) { e.preventDefault(); shell.openExternal(url.href); }
    }, true);
    var _open = window.open;
    window.open = function (u) {
      try {
        var url = new URL(u, location.href);
        if (url.origin !== ORIGIN && shell.openExternal) { shell.openExternal(url.href); return null; }
      } catch (_) { /* 非法 URL 交回原逻辑 */ }
      return _open.apply(window, arguments);
    };
  }

  function ready(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  ready(function () {
    var tries = 0;
    (function poll() {
      var api = window.pywebview && window.pywebview.api;
      if (!api) { if (++tries < 100) setTimeout(poll, 100); return; }
      Promise.all([api.version(), api.engine()]).then(function (r) {
        window.jzShell = {
          version: r[0] || '',
          engine: r[1] || navigator.userAgent,
          openExternal: function (url) { return api.openExternal(url); }
        };
        document.documentElement.classList.add('jz-shell');  // 结构差异唯一开关（Tier S）
        wireExternalLinks(window.jzShell);
      }).catch(function () { /* 桥失败=浏览器等同行为 */ });
    })();
  });
})();
