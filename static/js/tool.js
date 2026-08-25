/* JZToolsHub 工具外壳页逻辑
 * 从 URL 中解析工具 ID，请求 /api/tools/<id> 拿到元信息，
 * 然后将 iframe 指向 /plugin/<id>/<entry> 加载插件前端页面。
 */

// 把十六进制颜色转成 "r, g, b" 字符串（供 --tool-accent-rgb 使用）
function hexToRgb(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex.trim());
  if (!m) return '66, 133, 244';
  return `${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}`;
}

// 加载并渲染当前工具外壳页
async function loadTool() {
  // 取 URL 最后一段作为工具 ID，如 /tool/base64 → base64
  const toolId = decodeURIComponent(location.pathname.split('/').filter(Boolean).pop() || '');
  const icon = document.getElementById('tool-icon');
  const title = document.getElementById('tool-title');
  const desc = document.getElementById('tool-desc');
  const frame = document.getElementById('tool-frame');

  if (!toolId) {
    title.textContent = '无效的工具 ID';
    return;
  }

  try {
    // 元信息（名称 / 描述 / 图标 / 入口）由 /api/tools/<id> 提供
    const res = await fetch('/api/tools/' + encodeURIComponent(toolId));
    if (!res.ok) throw new Error('工具不存在或未启用');
    const tool = await res.json();

    document.title = tool.name + ' · JZ 工具箱';
    title.textContent = tool.name;
    desc.textContent = tool.description || '';
    icon.textContent = tool.icon || '🧩';
    icon.style.setProperty('--tool-accent-rgb', hexToRgb(tool.accent));
    // iframe 指向插件前端入口（插件间互不污染）
    frame.src = `/plugin/${encodeURIComponent(tool.id)}/${tool.entry || 'index.html'}`;
  } catch (err) {
    title.textContent = '无法加载工具';
    desc.textContent = err.message;
    frame.remove();
  }
}

loadTool();
window.AdminCommon.renderUserMenu(document.getElementById('user-slot'));
