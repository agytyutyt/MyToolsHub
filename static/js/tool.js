/* JZToolsHub 工具外壳页逻辑
 * 布局为「顶Bar + 插件主体全幅直出」：从 URL 解析工具 ID，
 * 请求 /api/tools/<id> 校验存在性并取入口地址，iframe 指向 /plugin/<id>/<entry>。
 * 插件页自带标题区，壳层不再重复展示元信息。
 */

// 加载并渲染当前工具外壳页
async function loadTool() {
  // 取 URL 最后一段作为工具 ID，如 /tool/base64 → base64
  const toolId = decodeURIComponent(location.pathname.split('/').filter(Boolean).pop() || '');
  const stage = document.getElementById('tool-stage');
  const frame = document.getElementById('tool-frame');

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
    showError('无效的工具 ID', '请从工具箱首页重新进入');
    return;
  }

  try {
    // 入口与校验由 /api/tools/<id> 提供；名称仅用于浏览器标签标题
    const res = await fetch('/api/tools/' + encodeURIComponent(toolId));
    if (!res.ok) throw new Error('工具不存在或未启用');
    const tool = await res.json();
    document.title = (tool.name || toolId) + ' · JZ 工具箱';
    // iframe 指向插件前端入口（插件间互不污染）
    frame.src = `/plugin/${encodeURIComponent(tool.id)}/${tool.entry || 'index.html'}`;
  } catch (err) {
    document.title = '无法加载工具 · JZ 工具箱';
    showError('无法加载工具', err.message);
  }
}

loadTool();
window.AdminCommon.renderUserMenu(document.getElementById('user-slot'));
