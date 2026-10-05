/* 框架左侧导航栏（主界面 / 插件外壳页 / 管理后台页共用）
 *
 * 顶栏改侧栏批次：原顶栏（站点名 / 用户菜单）与右下角悬浮球
 * （位置编辑 / 隐藏插件 / 大模型设置）的能力全部收进左侧栏，另加插件商店占位项。
 * 两档显示：展开（图标 + 文字）/ 精简（仅图标），由 body.side-collapsed 切换；
 * 用户偏好写 localStorage（键 jz_sidebar_collapsed），窄视口自动精简（不写偏好）。
 * 收展开关：侧栏头部的三横按钮，点站点图标 / 名称同样生效（logo 不再是首页链接，
 * 回首页走导航首项）。
 *
 * 页面侧接入（脚本顺序）：jz-icon.js(可选) → admin-common.js → 本文件 → 页面逻辑
 * （main.js / tool.js / admin-*.js）→ llm-settings.js。本文件按 body[data-page] 构建侧栏：
 *   home  → 主界面 / 插件商店 / 大模型设置 / 位置编辑 / 隐藏插件
 *   tool  → 返回   / 插件商店 / 大模型设置
 *   admin → 主界面 / 插件商店 / 大模型设置（logo 标题按 body[data-side-suffix] 带「· xx」后缀）
 * 底部（登录后）：管理后台（按权限，admin 页内高亮）/ 修改密码 /
 * 用户信息（左头像+名称，右退出登录），由 admin-common.js 的 renderSidebarUser 渲染。
 * 本文件随主界面 static 双端同源（主包与客户端包各带一份），改动随主界面发版节奏。
 */
(function () {
  'use strict';

  var PAGE = document.body.getAttribute('data-page') || 'home';
  var SUFFIX = document.body.getAttribute('data-side-suffix') || '';
  var STORE_KEY = 'jz_sidebar_collapsed';
  var AUTO_COLLAPSE_WIDTH = 900;   // 窄于此宽度自动精简（客户端最小窗 480，展开档放不下）

  // Material 风格单色图标（24 viewBox，currentColor 着色）——侧栏是框架 chrome，
  // 不走 JZIcon emoji 兼容层，避免缺彩色 emoji 字体的环境出现 tofu。
  var ICONS = {
    home: 'M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z',
    back: 'M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z',
    menu: 'M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z',
    store: 'M20 4H4v2h16V4zm1 10v-2l-1-5H4l-1 5v2h1v6h10v-6h4v6h2v-6h1zm-9 4H6v-4h6v4z',
    bulb: 'M9 21c0 .55.45 1 1 1h4c.55 0 1-.45 1-1v-1H9v1zm3-19C8.14 2 5 5.14 5 9c0 2.38 1.19 4.47 3 5.74V17c0 .55.45 1 1 1h6c.55 0 1-.45 1-1v-2.26c1.81-1.27 3-3.36 3-5.74 0-3.86-3.14-7-7-7z',
    edit: 'M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z',
    check: 'M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z',
    hide: 'M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.43-4.75-1.73-4.39-6-7.5-11-7.5-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78l3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z',
    settings: 'M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z',
    lock: 'M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z',
    logout: 'M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z',
    login: 'M11 7L9.6 8.4l2.6 2.6H2v2h10.2l-2.6 2.6L11 17l5-5-5-5zm9 12h-8v2h8c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2h-8v2h8v14z',
  };

  function svg(name) {
    return '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="' + (ICONS[name] || '') + '"/></svg>';
  }

  // 供外部复用（admin-common 的侧栏底部用户区、main.js 的编辑项换图标）
  function icon(name) {
    return '<span class="side-ico">' + svg(name) + '</span>';
  }

  // 通用条目：icon 图标名 / label 文字 / href 有值渲染 <a>，否则渲染 <button>
  function itemHtml(opts) {
    var tag = opts.href ? 'a' : 'button';
    var attrs = ' class="side-item' + (opts.className ? ' ' + opts.className : '') + '"'
      + ' id="' + opts.id + '" data-tip="' + opts.label + '"'
      + (opts.href ? ' href="' + opts.href + '"' : ' type="button"')
      + (opts.extra || '');
    return '<' + tag + attrs + '>' + icon(opts.icon)
      + '<span class="side-label">' + opts.label + '</span></' + tag + '>';
  }

  function build() {
    var aside = document.createElement('aside');
    aside.className = 'side-nav';
    aside.id = 'side-nav';

    // 首项：主页/管理后台显示「主界面」，插件外壳页显示「返回」（都离开当前页，
    // 主页与管理后台回首页、插件页回工具箱首页）
    var homeLabel = PAGE === 'tool' ? '返回' : '主界面';
    var middle = itemHtml({
      id: 'side-home', icon: PAGE === 'tool' ? 'back' : 'home', label: homeLabel, href: '/',
      className: PAGE === 'home' ? 'active' : '',
      extra: PAGE === 'home' ? ' aria-current="page"' : '',
    })
      // 插件商店：设计稿已入库、功能未落地，先占位（点击轻提示，见下方绑定）
      + itemHtml({ id: 'side-store', icon: 'store', label: '插件商店', className: 'side-store', extra: ' aria-disabled="true"' })
      // 大模型设置：默认隐藏，llm-settings.js 按后端「用户各自设置」模式动态放开
      + itemHtml({ id: 'side-act-llm', icon: 'bulb', label: '大模型设置', extra: ' hidden' })
      + (PAGE === 'home'
        ? itemHtml({ id: 'side-act-edit', icon: 'edit', label: '位置编辑' })
          + itemHtml({ id: 'side-act-hide', icon: 'hide', label: '隐藏插件' })
        : '');

    // 站点名可点击收展（不再链到首页，回首页走导航首项）；
    // 管理后台页按 body[data-side-suffix] 在标题带「· xx」后缀（loadSiteTitle 会保后缀替换站点名）
    aside.innerHTML = ''
      + '<div class="side-head">'
      +   '<button type="button" class="side-logo" id="side-logo" aria-label="收起或展开侧栏">'
      +     '<span class="logo-mark"></span>'
      +     '<span class="logo-title" id="site-title"' + (SUFFIX ? ' data-suffix="' + SUFFIX + '"' : '') + '>JZ 工具箱' + SUFFIX + '</span>'
      +   '</button>'
      +   '<button type="button" class="side-toggle" id="side-toggle" aria-label="收起侧栏" aria-expanded="true">' + svg('menu') + '</button>'
      + '</div>'
      + '<nav class="side-list" aria-label="主导航">' + middle + '</nav>'
      + '<div class="side-foot" id="side-user-slot"></div>';

    document.body.insertBefore(aside, document.body.firstChild);
    document.body.classList.add('has-side-nav');
    return aside;
  }

  var aside = build();
  var toggle = aside.querySelector('#side-toggle');

  // title 提示：精简档把条目文字挂到 title（悬停可见），展开档文字已可见则清掉
  function syncTitles() {
    var collapsed = document.body.classList.contains('side-collapsed');
    var els = aside.querySelectorAll('[data-tip]');
    for (var i = 0; i < els.length; i++) {
      els[i].title = collapsed ? (els[i].getAttribute('data-tip') || '') : '';
    }
    toggle.title = collapsed ? '展开侧栏' : '收起侧栏';
  }

  function readStoredCollapsed() {
    try { return localStorage.getItem(STORE_KEY) === '1'; } catch (e) { return false; }
  }

  function applyCollapsed(collapsed, persist) {
    document.body.classList.toggle('side-collapsed', collapsed);
    toggle.setAttribute('aria-label', collapsed ? '展开侧栏' : '收起侧栏');
    toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    if (persist) {
      try { localStorage.setItem(STORE_KEY, collapsed ? '1' : '0'); } catch (e) { /* 隐私模式忽略 */ }
    }
    syncTitles();
  }

  function toggleCollapsed() {
    applyCollapsed(!document.body.classList.contains('side-collapsed'), true);
  }

  // 三横开关与站点图标 / 名称点击同效
  toggle.addEventListener('click', toggleCollapsed);
  aside.querySelector('#side-logo').addEventListener('click', toggleCollapsed);

  // 窄视口自动精简（不写偏好），恢复宽视口时回到用户偏好
  function syncAuto() {
    applyCollapsed(window.innerWidth < AUTO_COLLAPSE_WIDTH ? true : readStoredCollapsed(), false);
  }
  window.addEventListener('resize', syncAuto);
  applyCollapsed(window.innerWidth < AUTO_COLLAPSE_WIDTH ? true : readStoredCollapsed(), false);

  // 插件商店占位：功能未落地，点击给一句可预期的反馈
  aside.querySelector('#side-store').addEventListener('click', function () {
    if (window.AdminCommon) window.AdminCommon.showToast('插件商店尚未开放，敬请期待');
  });

  // admin-common 的 loadSiteTitle 在本文件之前自动跑过（当时侧栏还不存在），
  // 这里补一次把配置的站点名写进侧栏标题（管理后台页连后缀一起）
  if (window.AdminCommon && window.AdminCommon.loadSiteTitle) {
    window.AdminCommon.loadSiteTitle();
  }

  // 底部用户区：管理后台（按权限）/ 修改密码 / 用户信息（头像+名称 | 退出登录），
  // 会话获取、权限过滤与退出逻辑都在 admin-common（与原顶栏用户菜单同一套）
  if (window.AdminCommon && window.AdminCommon.renderSidebarUser) {
    window.AdminCommon.renderSidebarUser(document.getElementById('side-user-slot'));
  }

  // 供 main.js（位置编辑项换图标/文字）与 admin-common（底部条目图标）使用
  window.JZSidebar = { icon: icon, syncTitles: syncTitles };
})();
