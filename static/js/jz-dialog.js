/* JZDialog —— 框架级自绘对话框组件（2026-10-08）
 *
 * 定位：框架 chrome（与 sidebar.js / jz-copy.js 同列，static 双端同源）。插件页面经
 * <script src="/static/js/jz-dialog.js?v=N"> 引用后调用 JZDialog.* —— **随用随调**：
 * 样式由本脚本首次调用时注入（自包含），调用方零 CSS 携带，杜绝"只搬皮不搬骨架"的样式缺失
 * （2026-10-08 商店管理页弹层落页面最下方即基样式漏带所致）。
 *
 * 用法（返回 Promise）：
 *   JZDialog.open({title, html, okText, cancelText, danger, input, placeholder, inputValue})
 *     → 确认型：true / false ；输入型：字符串（确定）/ null（取消）
 *   JZDialog.confirm(opts)  → 纯确认布尔（不含输入）
 *   JZDialog.prompt(opts)   → 输入字符串
 * 交互：ESC / 点遮罩 = 取消；Enter = 确定；自动聚焦（输入型聚焦输入框）。
 * 纪律：仅标准库 JS、幂等注入样式、无全局副作用（不改 body 类）；插件间独立铁律不涉及
 * （本文件属框架 static，非任一插件资产）。
 */
(function () {
  'use strict';

  var STYLE_ID = 'jz-dialog-style';
  var CSS = ''
    + '.jz-dlg{position:fixed;inset:0;background:rgba(32,33,36,.45);display:flex;align-items:center;'
    +   'justify-content:center;padding:24px;opacity:0;pointer-events:none;z-index:100;'
    +   'transition:opacity .2s cubic-bezier(.2,.7,.3,1)}'
    + '.jz-dlg.show{opacity:1;pointer-events:auto}'
    + '.jz-dlg-panel{width:min(400px,100%);max-height:min(76vh,640px);overflow:auto;background:#fff;'
    +   'border-radius:14px;box-shadow:0 8px 28px rgba(0,0,0,.22);padding:18px 20px;position:relative;'
    +   'transform:translateY(10px) scale(.98);transition:transform .22s cubic-bezier(.2,.7,.3,1)}'
    + '.jz-dlg.show .jz-dlg-panel{transform:none}'
    + '.jz-dlg-title{margin:0 0 10px;font-size:15px;color:#202124}'
    + '.jz-dlg-body{font-size:13px;color:#5f6368;line-height:1.75;overflow-wrap:anywhere;margin-bottom:16px}'
    + '.jz-dlg-body b{color:#202124}'
    + '.jz-dlg-input{width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid rgba(0,0,0,.15);'
    +   'border-radius:8px;font-size:13px;margin:-6px 0 14px;background:#fff}'
    + '.jz-dlg-input:focus{outline:none;border-color:#4285F4;box-shadow:0 0 0 3px rgba(66,133,244,.15)}'
    + '.jz-dlg-btns{display:flex;justify-content:flex-end;gap:8px}'
    + '.jz-dlg-btns button{padding:7px 16px;border:0;border-radius:8px;font-size:13px;cursor:pointer;'
    +   'transition:background .15s,transform .08s}'
    + '.jz-dlg-btns button:active{transform:scale(.97)}'
    + '.jz-dlg-btns button:focus-visible{outline:2px solid #4285F4;outline-offset:2px}'
    + '.jz-dlg-no{background:rgba(0,0,0,.06);color:#5f6368}'
    + '.jz-dlg-no:hover{background:rgba(0,0,0,.1)}'
    + '.jz-dlg-ok{background:#4285F4;color:#fff}'
    + '.jz-dlg-ok:hover{background:#3b78e7}'
    + '.jz-dlg-danger{background:rgba(197,34,31,.1);color:#c5221f}'
    + '.jz-dlg-danger:hover{background:rgba(197,34,31,.18)}'
    // Material Design 滚动条（统一口径，2026-10-14）：面板 max-height + overflow:auto，是纵向
    // 滚动容器。本组件承诺「自包含、调用方零 CSS 携带」，故滚动条口径也随样式一起注入；用
    // 后代选择器**限定在面板内**，不改宿主页面的全局滚动条（插件页在 iframe 里、宿主 CSS 各
    // 自一份，注入全局规则等于污染宿主）。口径同 static/css/style.css：细轨 10px、轨道透明、
    // 拇指 #dadce0 圆角 5px、悬停 #bdc1c6、不渲染箭头按钮。
    + '.jz-dlg-panel::-webkit-scrollbar{width:10px;height:10px}'
    + '.jz-dlg-panel::-webkit-scrollbar-track{background:transparent}'
    + '.jz-dlg-panel::-webkit-scrollbar-thumb{background:#dadce0;border-radius:5px}'
    + '.jz-dlg-panel::-webkit-scrollbar-thumb:hover{background:#bdc1c6}'
    + '.jz-dlg-panel::-webkit-scrollbar-corner{background:transparent}';

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var st = document.createElement('style');
    st.id = STYLE_ID;
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function open(opts) {
    opts = opts || {};
    injectStyle();
    return new Promise(function (resolve) {
      var dlg = document.createElement('div');
      dlg.className = 'jz-dlg';
      dlg.innerHTML = '<div class="jz-dlg-panel" role="alertdialog">'
        + '<h3 class="jz-dlg-title">' + esc(opts.title || '确认') + '</h3>'
        + '<div class="jz-dlg-body">' + (opts.html || '') + '</div>'
        + (opts.input ? '<input class="jz-dlg-input" type="text" value="'
            + esc(opts.inputValue || '') + '" placeholder="' + esc(opts.placeholder || '') + '">' : '')
        + '<div class="jz-dlg-btns">'
        + '<button type="button" class="jz-dlg-no" data-k="0">' + esc(opts.cancelText || '取消') + '</button>'
        + '<button type="button" class="' + (opts.danger ? 'jz-dlg-danger' : 'jz-dlg-ok')
        + '" data-k="1">' + esc(opts.okText || '确定') + '</button>'
        + '</div></div>';
      var inp = dlg.querySelector('.jz-dlg-input');
      function cancelVal() { return opts.input ? null : false; }
      function okVal() { return opts.input ? (inp ? inp.value : '') : true; }
      function done(val) {
        dlg.classList.remove('show');
        setTimeout(function () { if (dlg.parentNode) dlg.parentNode.removeChild(dlg); }, 220);
        document.removeEventListener('keydown', onKey);
        resolve(val);
      }
      function onKey(ev) {
        if (ev.key === 'Escape') done(cancelVal());
        else if (ev.key === 'Enter') done(okVal());
      }
      dlg.addEventListener('click', function (ev) {
        if (ev.target === dlg) return done(cancelVal());     // 点遮罩 = 取消
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

  window.JZDialog = {
    open: open,
    confirm: function (opts) { return open(opts && !opts.input ? opts : Object.assign({}, opts, { input: false })); },
    prompt: function (opts) { return open(Object.assign({}, opts, { input: true })); },
  };
})();
