/* JZToolsHub 首页渲染逻辑
 * 1. 请求 /api/tools 获取站点信息、分类与工具清单；
 * 2. 按分类渲染工具卡片（Material Design 大方块风格）；
 * 3. 点击卡片跳转到 /tool/<id> 工具外壳页。
 */

// 工具卡片网格容器
const grid = document.getElementById('tool-grid');
// 分类分区块容器
const content = document.getElementById('content');

// 把十六进制颜色转成 "r, g, b" 字符串，供 CSS 变量 --tool-accent-rgb 使用（实现悬停高亮）
function hexToRgb(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex.trim());
  if (!m) return '66, 133, 244';
  return `${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}`;
}

// 生成单个工具卡片 HTML
function toolCard(tool) {
  const accents = tool.accent || '#4285F4';
  return `
    <a class="tool-card" href="/tool/${encodeURIComponent(tool.id)}"
       style="--tool-accent: ${accents}; --tool-accent-rgb: ${hexToRgb(accents)};">
      <div class="tool-icon">${tool.icon || '🧩'}</div>
      <div class="tool-name">${tool.name}</div>
      <div class="tool-desc">${tool.description || ''}</div>
      <div class="tool-features">
        ${(tool.features || []).map(f => `<span class="chip">${f}</span>`).join('')}
      </div>
    </a>`;
}

// 拉取 /api/tools 并渲染首页
async function render() {
  try {
    const res = await fetch('/api/tools');
    if (!res.ok) throw new Error('API error: ' + res.status);
    const data = await res.json();

    // 站点信息（标题 / 副标题 / 页脚）均来自 config/tools.json
    document.title = data.site?.title || document.title;
    document.getElementById('site-title').textContent = data.site?.title || '';
    document.getElementById('hero-title').textContent = data.site?.title || '工具箱';
    document.getElementById('hero-subtitle').textContent = data.site?.subtitle || '';
    document.getElementById('footer').textContent = data.site?.footer || '';

    const tools = data.tools || [];
    if (tools.length === 0) {
      grid.innerHTML = '<div class="loading">暂无可用工具，请在 config/tools.json 中注册。</div>';
      return;
    }

    // 按分类分组渲染；分类下无工具则跳过该区块
    content.innerHTML = '';
    for (const cat of data.categories || []) {
      const catTools = tools.filter(t => t.category_id === cat.id);
      if (catTools.length === 0) continue;

      const section = document.createElement('section');
      section.className = 'category';
      section.innerHTML = `
        <h2>${cat.name}</h2>
        <div class="tool-grid">${catTools.map(toolCard).join('')}</div>`;
      content.appendChild(section);
    }
  } catch (err) {
    grid.innerHTML = `<div class="loading">加载失败：${err.message}</div>`;
  }
}

render();
