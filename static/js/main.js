/* JZToolsHub 首页渲染逻辑
 * 1. 请求 /api/tools 获取站点信息、分类与工具清单；
 * 2. 按分类渲染工具卡片（Material Design 大方块风格，每行最多 4 个）；
 * 3. 点击卡片跳转到 /tool/<id> 工具外壳页；
 * 4. 右下角悬浮球进入「编辑位置」模式：
 *    - 插件卡片：Android 桌面式拖拽——按住图标拖起（图标跟随光标放大悬停），
 *      拖动过程中目标槽位实时生成图标「虚影」，沿途卡片顺序让位，松手落位并自动保存；
 *    - 分类区块：拖动分类标题调整分类顺序。
 */

// 工具卡片网格容器
const grid = document.getElementById('tool-grid');
// 分类分区块容器
const content = document.getElementById('content');
// 右侧分类快速定位标签容器
const catNav = document.getElementById('cat-nav');
// 右下角「更多」悬浮球 / 展开菜单 / 编辑提示 / 轻提示
const fabPop = document.getElementById('fab-pop');
const fabEditItem = document.getElementById('fab-act-edit');
const hint = document.getElementById('edit-hint');
const toast = document.getElementById('toast');
// 浮窗：隐藏工具
const modalHide = document.getElementById('modal-hide');
const hideCats = document.getElementById('hide-cats');
const hideTools = document.getElementById('hide-tools');

// HTML 转义（浮窗里展示分类/插件名用）
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// 编辑位置模式状态
let editing = false;
// HTML5 分类拖拽：{ type:'cat', id, element }
let dragSource = null;
// 指针拖拽（插件卡片）：{ card, ghost, grid, originIndex, startX, startY, offsetX, offsetY, lifted }
let pointerDrag = null;

// 拖拽期间需要清理/恢复的卡片内联样式（注意不能清掉卡片自带的 --tool-accent 等变量）
const DRAG_STYLES = ['position', 'left', 'top', 'width', 'height', 'margin',
  'zIndex', 'pointerEvents', 'transition', 'transform'];

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
    <a class="tool-card" data-tool-id="${tool.id}" href="/tool/${encodeURIComponent(tool.id)}"
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
      section.id = 'cat-' + cat.id;
      section.dataset.category = cat.id;
      section.innerHTML = `
        <h2>${cat.name}</h2>
        <div class="tool-grid">${catTools.map(toolCard).join('')}</div>`;
      content.appendChild(section);
    }

    // 渲染右侧快速定位标签，并启用滚动高亮
    buildCatNav();
    window.addEventListener('scroll', spyActiveCategory, { passive: true });
    spyActiveCategory();
    // 若正处编辑模式，重新铺好分类标题的可拖拽属性
    applyEditAttrs();
  } catch (err) {
    grid.innerHTML = `<div class="loading">加载失败：${err.message}</div>`;
  }
}

// 生成右侧快速定位标签：每个分类一个标点，单击平滑滚动到对应分类区块
function buildCatNav() {
  catNav.innerHTML = '';
  content.querySelectorAll('.category').forEach(section => {
    const dot = document.createElement('button');
    dot.type = 'button';
    dot.className = 'cat-nav-dot';
    dot.dataset.target = 'cat-' + section.dataset.category;
    dot.dataset.name = section.querySelector('h2').textContent;
    dot.setAttribute('aria-label', '定位到 ' + dot.dataset.name);
    dot.addEventListener('click', () => {
      document.getElementById(dot.dataset.target)
        .scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    catNav.appendChild(dot);
  });
}

// 滚动监听：高亮当前浏览区域对应的分类标点
function spyActiveCategory() {
  const dots = catNav.querySelectorAll('.cat-nav-dot');
  const sections = content.querySelectorAll('.category');
  let current = null;
  // 视窗处于最下方时：最后分类默认高亮（此时页面无法再滚动，末分类无法越过阈值线）
  if (sections.length &&
      window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) {
    current = sections[sections.length - 1];
  } else {
    for (const section of sections) {
      const r = section.getBoundingClientRect();
      if (r.top <= 120 && r.bottom > 120) {
        current = section;
        break;
      }
    }
  }
  for (const dot of dots) {
    dot.classList.toggle('active', dot.dataset.target === (current && current.id));
  }
}

// ===================== 编辑位置模式 =====================

function applyEditAttrs() {
  // 分类标题可用 HTML5 拖拽调整顺序；插件卡片走指针拖拽（见下方）
  content.querySelectorAll('.category > h2').forEach(h => { h.draggable = editing; });
}

function updateEditItem() {
  if (editing) {
    fabEditItem.textContent = '✅ 完成编辑';
    fabEditItem.classList.add('active');
  } else {
    fabEditItem.textContent = '✏️ 编辑位置';
    fabEditItem.classList.remove('active');
  }
}

function enterEditMode() {
  editing = true;
  document.body.classList.add('editing');
  hint.hidden = false;
  applyEditAttrs();
  updateEditItem();
}

function exitEditMode() {
  editing = false;
  document.body.classList.remove('editing');
  hint.hidden = true;
  applyEditAttrs();
  clearDropMarks();
  dragSource = null;
  pointerDrag = null;
  updateEditItem();
}

// 悬浮球菜单：编辑位置 / 隐藏工具
fabPop.addEventListener('click', (e) => {
  const item = e.target.closest('.fab-pop-item');
  if (!item) return;
  const action = item.dataset.action;
  if (action === 'edit' || action === 'hide') {
    // 布局修改属管理操作，未登录先跳登录页
    window.AdminCommon.getSession().then(user => {
      if (!user) {
        showToast('请先登录后操作');
        window.location.href = '/login?next=/';
        return;
      }
      if (action === 'edit') {
        editing ? exitEditMode() : enterEditMode();
      } else if (action === 'hide') {
        openHideModal();
      }
    });
  }
});

// 顶部用户菜单：未登录显示「登录」，已登录显示用户名 + 管理后台入口
window.AdminCommon.renderUserMenu(document.getElementById('user-slot'));

// ===================== 浮窗：隐藏工具 =====================

async function openHideModal() {
  try {
    const res = await fetch('/api/tools/visibility');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    hideCats.innerHTML = (data.categories || []).map(c => hideRowHtml('category', c)).join('');
    hideTools.innerHTML = (data.tools || []).map(t => hideRowHtml('tool', t)).join('');
    modalHide.hidden = false;
    document.body.classList.add('modal-open');
  } catch (err) {
    showToast('加载失败：' + err.message, true);
  }
}

function hideRowHtml(kind, item) {
  return `
    <label class="hide-row">
      <span class="hide-name">${esc(item.name)}</span>
      <span class="switch">
        <input type="checkbox" data-kind="${kind}" data-id="${esc(item.id)}" ${item.enabled ? 'checked' : ''}>
        <i></i>
      </span>
    </label>`;
}

[hideCats, hideTools].forEach(list => {
  list.addEventListener('change', (e) => setVisibility(e.target));
});

async function setVisibility(checkbox) {
  const kind = checkbox.dataset.kind;
  const id = checkbox.dataset.id;
  const enabled = checkbox.checked;
  try {
    const res = await fetch('/api/tools/visibility', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: kind, id, enabled }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    showToast((enabled ? '已显示' : '已隐藏') + (kind === 'category' ? '分类' : '插件'));
    render();
  } catch (err) {
    checkbox.checked = !enabled;
    showToast('操作失败：' + err.message, true);
  }
}

// 浮窗通用关闭：点遮罩 / ✕ / 取消
document.querySelectorAll('.modal').forEach(m => {
  m.addEventListener('click', (e) => {
    if (e.target === m || e.target.closest('[data-close]')) {
      m.hidden = true;
      document.body.classList.remove('modal-open');
    }
  });
});

function clearDropMarks() {
  content.querySelectorAll('.dragging, .drag-target, .drop-here, .drop-end').forEach(el => {
    el.classList.remove('dragging', 'drag-target', 'drop-here', 'drop-end');
  });
}

// 计算在网格中应插入到哪个卡片之前（null 表示追加到末尾）。
// 用「行容差」判定光标所在行：同一行内按水平位置比较，
// 光标明显在某行上方则该行卡片视为「之后」，明显在下方则视为「之前」，
// 从而保证同一行内的中间槽位（如 3 个卡片的位置 2）也能精确命中。
function getInsertTarget(gridEl, x, y) {
  const cards = [...gridEl.querySelectorAll('.tool-card:not(.dragging)')];
  for (let i = 0; i < cards.length; i++) {
    const box = cards[i].getBoundingClientRect();
    const cy = box.top + box.height / 2;
    const cx = box.left + box.width / 2;
    const tol = box.height * 0.35;
    let before;
    if (y > cy + tol) before = true;      // 光标在该行下方：该卡片视为已掠过
    else if (y < cy - tol) before = false; // 光标在该行上方：该卡片视为未到达
    else before = cx < x;                 // 同一行：按水平位置
    if (!before) return cards[i];
  }
  return null;
}

// 拖走插件后，若某个分类已无插件则移除该空分类区块
function pruneEmptySections() {
  content.querySelectorAll('.category').forEach(section => {
    if (!section.querySelector('.tool-card')) section.remove();
  });
  buildCatNav();
}

// 把当前 DOM 布局写回 config/tools.json
async function saveLayout() {
  const sections = [...content.querySelectorAll('.category')];
  const categories = sections.map(s => s.dataset.category);
  const tools = {};
  sections.forEach(s => {
    tools[s.dataset.category] = [...s.querySelectorAll('.tool-card')].map(c => c.dataset.toolId);
  });
  try {
    const res = await fetch('/api/tools/reorder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ categories, tools }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    showToast('顺序已保存');
  } catch (err) {
    showToast('保存失败：' + err.message, true);
  }
}

// 轻提示
let toastTimer = null;
function showToast(msg, isError) {
  toast.textContent = msg;
  toast.classList.toggle('error', !!isError);
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2000);
}

// 编辑模式下点击卡片不跳转
content.addEventListener('click', (e) => {
  if (editing && e.target.closest('.tool-card')) e.preventDefault();
});

// ===================== 插件卡片：Android 桌面式指针拖拽 =====================

// 生成目标槽位的「虚影」：复制卡片内容，半透明 + 虚线框
function makeGhost(card) {
  const ghost = document.createElement('div');
  ghost.className = 'drop-ghost';
  ghost.innerHTML = card.innerHTML;
  ghost.setAttribute('aria-hidden', 'true');
  return ghost;
}

// 拖起卡片：固定定位跟随光标 + 放大悬停，并在原位生成虚影占位
function liftCard(d, e) {
  const card = d.card;
  const rect = card.getBoundingClientRect();
  card.classList.add('dragging');
  card.style.position = 'fixed';
  card.style.left = '0px';
  card.style.top = '0px';
  card.style.width = rect.width + 'px';
  card.style.height = rect.height + 'px';
  card.style.margin = '0px';
  card.style.zIndex = '1000';
  card.style.pointerEvents = 'none';
  card.style.transition = 'none';
  card.style.transform =
    `translate(${e.clientX - d.offsetX}px, ${e.clientY - d.offsetY}px) scale(1.08)`;
  const ghost = makeGhost(card);
  d.ghost = ghost;
  d.grid.insertBefore(ghost, card);
  d.lifted = true;
}

// 把虚影挪到光标对应的目标槽位：沿途卡片由网格自动顺序让位
function reorderGhost(d, clientX, clientY) {
  const gridEl = d.grid;
  const cards = [...gridEl.children].filter(el =>
    el.classList.contains('tool-card') && el !== d.card);
  const insertBefore = getInsertTarget(gridEl, clientX, clientY);
  let insertIndex = cards.length;
  if (insertBefore) {
    const idx = cards.indexOf(insertBefore);
    if (idx !== -1) insertIndex = idx;
  }
  const cur = [...gridEl.children].indexOf(d.ghost);
  if (insertIndex !== cur) {
    gridEl.removeChild(d.ghost);
    gridEl.insertBefore(d.ghost, cards[insertIndex] || null);
  }
}

// 松手落位：用卡片替换虚影，恢复网格布局并保存
function commitDrop(d) {
  d.grid.replaceChild(d.card, d.ghost);
  finishDrag(d.card);
  pruneEmptySections();
  saveLayout();
  pointerDrag = null;
}

// 取消拖拽：虚影移除，卡片放回原网格原位
function cancelDrag(d) {
  if (d.ghost && d.ghost.parentNode) d.ghost.parentNode.removeChild(d.ghost);
  const others = [...d.originGrid.children].filter(el => el !== d.card);
  d.originGrid.insertBefore(d.card, others[d.originIndex] || null);
  finishDrag(d.card);
  pointerDrag = null;
}

// 清理卡片拖拽时的内联样式与 class（保留卡片自带的 accent 变量）
function finishDrag(card) {
  card.classList.remove('dragging');
  for (const p of DRAG_STYLES) card.style[p] = '';
}

content.addEventListener('pointerdown', (e) => {
  if (!editing || e.button !== 0) return;
  const card = e.target.closest('.tool-card');
  if (!card) return;
  e.preventDefault();
  const rect = card.getBoundingClientRect();
  let originIndex = 0;
  for (const el of card.parentElement.children) {
    if (el === card) break;
    originIndex++;
  }
  pointerDrag = {
    card,
    grid: card.closest('.tool-grid'),
    originGrid: card.closest('.tool-grid'),
    originIndex,
    startX: e.clientX,
    startY: e.clientY,
    offsetX: e.clientX - rect.left,
    offsetY: e.clientY - rect.top,
    lifted: false,
  };
  // 捕获指针：即使拖出窗口/悬浮球上方也能持续收到 move/up
  content.setPointerCapture(e.pointerId);
});

content.addEventListener('pointermove', (e) => {
  if (!pointerDrag) return;
  const d = pointerDrag;
  if (!d.lifted) {
    // 移动超过阈值才真正拖起，避免误触
    if ((e.clientX - d.startX) ** 2 + (e.clientY - d.startY) ** 2 < 25) return;
    liftCard(d, e);
    return;
  }
  d.card.style.transform =
    `translate(${e.clientX - d.offsetX}px, ${e.clientY - d.offsetY}px) scale(1.08)`;
  // 虚影跟随：命中哪个网格就在哪个网格让位
  const under = document.elementFromPoint(e.clientX, e.clientY);
  const section = under && under.closest('.category');
  const targetGrid = (under && under.closest('.tool-grid')) || (section && section.querySelector('.tool-grid'));
  if (!targetGrid) return;
  if (targetGrid !== d.grid) {
    d.grid.removeChild(d.ghost);
    d.grid = targetGrid;
    targetGrid.appendChild(d.ghost);
  }
  reorderGhost(d, e.clientX, e.clientY);
});

content.addEventListener('pointerup', () => {
  if (!pointerDrag) return;
  if (pointerDrag.lifted) commitDrop(pointerDrag);
  else pointerDrag = null;
});

content.addEventListener('pointercancel', () => {
  if (!pointerDrag) return;
  if (pointerDrag.lifted) cancelDrag(pointerDrag);
  else pointerDrag = null;
});

// Esc：取消拖拽 / 关闭浮窗
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (pointerDrag && pointerDrag.lifted) {
    cancelDrag(pointerDrag);
    showToast('已取消移动');
  }
  const open = document.querySelector('.modal:not([hidden])');
  if (open) {
    open.hidden = true;
    document.body.classList.remove('modal-open');
  }
});

// ===================== 分类区块：HTML5 拖拽调整顺序 =====================

content.addEventListener('dragstart', (e) => {
  if (!editing) return;
  const header = e.target.closest('.category > h2');
  if (!header) return;
  const section = header.closest('.category');
  dragSource = { type: 'cat', id: section.dataset.category, element: section };
  header.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', 'cat:' + dragSource.id);
});

content.addEventListener('dragover', (e) => {
  if (!editing || !dragSource) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  const section = e.target.closest('.category');
  if (!section || section === dragSource.element) return;
  content.querySelectorAll('.drag-target, .drop-here, .drop-end').forEach(el =>
    el.classList.remove('drag-target', 'drop-here', 'drop-end'));
  section.classList.add('drop-here');
});

content.addEventListener('drop', (e) => {
  if (!editing || !dragSource) return;
  e.preventDefault();
  const src = dragSource;
  clearDropMarks();
  dragSource = null;
  const section = e.target.closest('.category');
  if (!section || section === src.element) return;
  const box = section.getBoundingClientRect();
  const ref = e.clientY < box.top + box.height / 2 ? section : section.nextElementSibling;
  let moved = false;
  if (ref === src.element) {
    moved = false;
  } else if (ref === null) {
    if (content.lastElementChild !== src.element) {
      content.appendChild(src.element);
      moved = true;
    }
  } else if (ref.previousElementSibling !== src.element) {
    content.insertBefore(src.element, ref);
    moved = true;
  }
  if (moved) saveLayout();
});

content.addEventListener('dragend', () => {
  clearDropMarks();
  dragSource = null;
});

render();
