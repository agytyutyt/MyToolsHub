/* 管理后台首页：加载模块清单，以主界面方格布局渲染模块卡片 */
(function () {
  const { api, getSession, esc } = window.AdminCommon;

  const grid = document.getElementById('admin-grid');

  const MODULE_META = {
    unit: { icon: '1f3db.svg', accent: '#5E35B1' },
    department: { icon: '1f3e2.svg', accent: '#4285F4' },
    user: { icon: '1f465.svg', accent: '#34A853' },
    permission: { icon: '1f6e1.svg', accent: '#FBBC05' },
    settings: { icon: '2699.svg', accent: '#607D8B' },
    plugins: { icon: '1f9e9.svg', accent: '#009688' },
    llm: { icon: '1f4a1.svg', accent: '#8E24AA' },
    migrate: { icon: '1f4be.svg', accent: '#455A64' },   // 💾（仓库内已有的 Twemoji 图标）
  };

  // 固定文案的模块：卡片不显示"记录数"，改用一句说明
  const MODULE_DESC = {
    settings: '数据根目录与系统配置',
    plugins: '插件包升级 / 回滚 / 批量更新（仅超级管理员）',
    llm: '统一大模型：调用模式与接入配置',
    migrate: '数据导出 / 导入（按插件打包、口令加密）',
  };

  function hexToRgb(hex) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    if (!m) return '66, 133, 244';
    return `${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}`;
  }

  function moduleCard(m) {
    const meta = MODULE_META[m.id] || { icon: '1f9e9.svg', accent: '#4285F4' };
    const countLabel = m.id === 'permission' ? '角色数' : '记录数';
    const countText = MODULE_DESC[m.id] || `${countLabel} ${m.count} 条`;
    // 系统设置同样跳独立页（2026-10-12：独立页含 数据目录 + 桌面端接入密钥 两卡；
    // 原弹窗实现已删——弹窗只有数据目录，接入密钥进不去）
    const href = m.id === 'settings' ? ' href="/admin/settings"' : ' href="/admin/' + m.id + '"';
    return '<a class="tool-card"' + href
      + ' style="--tool-accent: ' + meta.accent + '; --tool-accent-rgb: ' + hexToRgb(meta.accent) + ';cursor:pointer">'
      + '<div class="admin-module-icon" style="background: rgba(' + hexToRgb(meta.accent) + ', .12);"><img class="tool-icon-img" src="/static/icons/' + encodeURIComponent(meta.icon) + '" alt=""></div>'
      + '<div class="tool-name">' + esc(m.name) + '</div>'
      + '<div class="tool-desc">' + countText + '</div>'
      + '<div class="tool-features"><span class="chip">' + (m.allowed ? '可访问' : '无权限') + '</span></div>'
      + '</a>';
  }

  async function render() {
    try {
      const user = await getSession();
      if (!user) return;

      const data = await api('/api/admin/summary');
      const modules = (data.modules || []).filter(m => m.allowed && m.id !== 'permission');
      if (modules.length === 0) {
        grid.innerHTML = '<div class="loading">当前账号无任何管理模块权限。</div>';
        return;
      }
      grid.innerHTML = modules.map(moduleCard).join('');
    } catch (err) {
      grid.innerHTML = '<div class="loading">加载失败：' + esc(err.message) + '</div>';
    }
  }

  render();
})();
