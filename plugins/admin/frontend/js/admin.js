/* 管理后台首页：加载模块清单，以主界面方格布局渲染模块卡片 */
(function () {
  const { api, renderUserMenu, getSession, esc } = window.AdminCommon;

  const grid = document.getElementById('admin-grid');

  const MODULE_META = {
    unit: { icon: '🏛️', accent: '#5E35B1' },
    department: { icon: '🏢', accent: '#4285F4' },
    user: { icon: '👥', accent: '#34A853' },
    permission: { icon: '🛡️', accent: '#FBBC05' },
  };

  function hexToRgb(hex) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    if (!m) return '66, 133, 244';
    return `${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}`;
  }

  function moduleCard(m) {
    const meta = MODULE_META[m.id] || { icon: '📦', accent: '#4285F4' };
    const countLabel = m.id === 'permission' ? '角色数' : '记录数';
    return `
      <a class="tool-card" href="/admin/${m.id}"
         style="--tool-accent: ${meta.accent}; --tool-accent-rgb: ${hexToRgb(meta.accent)};">
        <div class="admin-module-icon" style="background: rgba(${hexToRgb(meta.accent)}, .12);">${meta.icon}</div>
        <div class="tool-name">${esc(m.name)}</div>
        <div class="tool-desc">${countLabel} ${m.count} 条</div>
        <div class="tool-features">
          <span class="chip">${m.allowed ? '可访问' : '无权限'}</span>
        </div>
      </a>`;
  }

  async function render() {
    try {
      const user = await getSession();
      if (!user) return; // renderUserMenu 已处理跳转
      renderUserMenu(document.getElementById('user-slot'));

      const data = await api('/api/admin/summary');
      // 权限管理暂时屏蔽（已并入「人员管理」），后台首页不展示独立模块卡片
      const modules = (data.modules || []).filter(m => m.allowed && m.id !== 'permission');
      if (modules.length === 0) {
        grid.innerHTML = '<div class="loading">当前账号无任何管理模块权限。</div>';
        return;
      }
      grid.innerHTML = modules.map(moduleCard).join('');
    } catch (err) {
      grid.innerHTML = `<div class="loading">加载失败：${esc(err.message)}</div>`;
    }
  }

  render();
})();
