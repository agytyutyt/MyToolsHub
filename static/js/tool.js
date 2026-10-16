/* JZToolsHub 工具外壳页逻辑
 * 布局为「左侧导航栏 + 插件主体全幅直出」（侧栏由 static/js/sidebar.js 构建）：
 * 从 URL 解析工具 ID，请求 /api/tools/<id> 校验存在性并取入口地址，
 * iframe 指向 /plugin/<id>/<entry>。插件页自带标题区，壳层不再重复展示元信息。
 */

// 加载并渲染当前工具外壳页
async function loadTool() {
  // 取 URL 最后一段作为工具 ID，如 /tool/base64 → base64
  const toolId = decodeURIComponent(location.pathname.split('/').filter(Boolean).pop() || '');
  const stage = document.getElementById('tool-stage');
  const frame = document.getElementById('tool-frame');

  // 站点名称来自轻量端点 /api/site 的 site.title（配置化；不为一行标题拉全量工具清单）。
  // 旧主包与桌面客户端离线态没有该端点（客户端本地直出的是 /api/tools）→ 回退取一次；
  // 两条路都失败时用默认值。
  let siteName = 'JZ 工具箱';
  try {
    const siteRes = await fetch('/api/site');
    if (siteRes.ok) {
      const siteData = await siteRes.json();
      siteName = (siteData.site && siteData.site.title) || siteName;
    } else {
      const res = await fetch('/api/tools');
      if (res.ok) {
        const d = await res.json();
        siteName = (d.site && d.site.title) || siteName;
      }
    }
  } catch (e) { /* 忽略，使用默认值 */ }
  const titleEl = document.getElementById('site-title');
  if (titleEl) titleEl.textContent = siteName;

  function showError(title, detail) {
    if (frame) frame.remove();
    const box = document.createElement('div');
    box.className = 'tool-error';
    const h = document.createElement('div');
    h.className = 'tool-error-title';
    h.textContent = title;
    const p = document.createElement('p');
    p.textContent = detail || '';
    box.appendChild(h);
    box.appendChild(p);
    stage.appendChild(box);
  }

  if (!toolId) {
    document.title = '无效的工具 ID · ' + siteName;
    showError('无效的工具 ID', '请从工具箱首页重新进入');
    return;
  }

  try {
    // 入口与校验由 /api/tools/<id> 提供；名称仅用于浏览器标签标题
    const res = await fetch('/api/tools/' + encodeURIComponent(toolId));
    if (!res.ok) throw new Error('工具不存在或未启用');
    const tool = await res.json();
    document.title = (tool.name || toolId) + ' · ' + siteName;
    // iframe 指向插件前端入口（插件间互不污染）
    // 透传外层查询串（2026-10-17，P2-3②）：插件页此前拿不到导航时的 URL 参数，
    // 使「用 URL 参数驱动插件视图」类能力（如知识库 Word 渲染模式 flow/paged）无法触达。
    // 已核实：现有插件均不读自身 location.search，故透传不会改变既有行为。
    frame.src = `/plugin/${encodeURIComponent(tool.id)}/${tool.entry || 'index.html'}`
                + (location.search || '');
  } catch (err) {
    document.title = '无法加载工具 · ' + siteName;
    showError('无法加载工具', err.message);
  }
}

loadTool();
// 侧栏底部用户区由 sidebar.js 调 AdminCommon.renderSidebarUser 渲染，这里不再重复
