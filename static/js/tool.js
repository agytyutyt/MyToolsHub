function hexToRgb(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex.trim());
  if (!m) return '66, 133, 244';
  return `${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}`;
}

async function loadTool() {
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
    const res = await fetch('/api/tools/' + encodeURIComponent(toolId));
    if (!res.ok) throw new Error('工具不存在或未启用');
    const tool = await res.json();

    document.title = tool.name + ' · JZ 工具箱';
    title.textContent = tool.name;
    desc.textContent = tool.description || '';
    icon.textContent = tool.icon || '🧩';
    icon.style.setProperty('--tool-accent-rgb', hexToRgb(tool.accent));
    frame.src = `/plugin/${encodeURIComponent(tool.id)}/${tool.entry || 'index.html'}`;
  } catch (err) {
    title.textContent = '无法加载工具';
    desc.textContent = err.message;
    frame.remove();
  }
}

loadTool();