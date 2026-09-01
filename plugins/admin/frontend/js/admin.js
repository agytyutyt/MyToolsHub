/* 管理后台首页：加载模块清单，以主界面方格布局渲染模块卡片 */
(function () {
  const { api, renderUserMenu, getSession, esc, openModal, showToast } = window.AdminCommon;

  const grid = document.getElementById('admin-grid');

  const MODULE_META = {
    unit: { icon: '1f3db.svg', accent: '#5E35B1' },
    department: { icon: '1f3e2.svg', accent: '#4285F4' },
    user: { icon: '1f465.svg', accent: '#34A853' },
    permission: { icon: '1f6e1.svg', accent: '#FBBC05' },
    settings: { icon: '2699.svg', accent: '#607D8B' },
  };

  function hexToRgb(hex) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    if (!m) return '66, 133, 244';
    return `${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}`;
  }

  function moduleCard(m) {
    const meta = MODULE_META[m.id] || { icon: '1f9e9.svg', accent: '#4285F4' };
    const countLabel = m.id === 'permission' ? '角色数'
      : m.id === 'settings' ? '数据目录' : '记录数';
    const countText = m.id === 'settings'
      ? '数据根目录与系统配置'
      : `${countLabel} ${m.count} 条`;
    const tag = m.id === 'settings' ? 'div' : 'a';
    const hrefAttr = m.id === 'settings' ? '' : ' href="/admin/' + m.id + '"';
    const clickAttr = m.id === 'settings' ? ' data-settings-card' : '';
    return '<' + tag + ' class="tool-card"' + hrefAttr + clickAttr
      + ' style="--tool-accent: ' + meta.accent + '; --tool-accent-rgb: ' + hexToRgb(meta.accent) + ';cursor:pointer">'
      + '<div class="admin-module-icon" style="background: rgba(' + hexToRgb(meta.accent) + ', .12);"><img class="tool-icon-img" src="/static/icons/' + encodeURIComponent(meta.icon) + '" alt=""></div>'
      + '<div class="tool-name">' + esc(m.name) + '</div>'
      + '<div class="tool-desc">' + countText + '</div>'
      + '<div class="tool-features"><span class="chip">' + (m.allowed ? '可访问' : '无权限') + '</span></div>'
      + '</' + tag + '>';
  }

  function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return '-';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
    return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  async function openSettingsModal() {
    var bodyHtml = '<div class="settings-card" style="max-width:none;padding:0;border:none;box-shadow:none;background:transparent">'
      + '<p class="settings-desc">所有用户数据（管理配置、文档、公告、案件、日志等）均保存在此目录。默认位于用户主目录下，更改后自动将旧目录数据迁移到新目录。</p>'
      + '<div class="settings-info" id="s-info-root">'
      + '<div class="info-row"><span class="info-label">当前目录</span><code class="info-value" id="s-cur-root">加载中…</code></div>'
      + '<div class="info-row"><span class="info-label">默认目录</span><code class="info-value" id="s-default-root">-</code></div>'
      + '<div class="info-row"><span class="info-label">占用空间</span><span class="info-value" id="s-usage-size">-</span></div>'
      + '</div>'
      + '<div class="settings-form">'
      + '<label for="s-new-root-input">更改数据目录</label>'
      + '<div class="form-row"><input type="text" id="s-new-root-input" class="admin-input" placeholder="输入新的数据目录绝对路径（如 D:\\JZData）" />'
      + '<button type="button" class="admin-btn primary" id="s-btn-change">更改并迁移</button></div>'
      + '<p class="settings-hint">新目录不存在将自动创建；已存在同名文件/目录不会覆盖。</p>'
      + '</div></div>';
    var modal = openModal('系统设置', bodyHtml);
    // 加载数据
    try {
      var data = await api('/api/admin/data-settings');
      modal.querySelector('#s-cur-root').textContent = data.data_root;
      modal.querySelector('#s-default-root').textContent = data.default_data_root;
      modal.querySelector('#s-usage-size').textContent = fmtSize(data.total_bytes);
    } catch (err) {
      modal.querySelector('#s-cur-root').textContent = '读取失败：' + err.message;
    }
    // 提交流程
    var input = modal.querySelector('#s-new-root-input');
    var btn = modal.querySelector('#s-btn-change');
    function doChange() {
      var root = input.value.trim();
      if (!root) { showToast('请先填写新的数据目录', true); input.focus(); return; }
      btn.disabled = true; btn.textContent = '迁移中…';
      api('/api/admin/data-settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data_root: root, migrate: true }),
      }).then(function (d) {
        showToast('已切换数据目录，迁移 ' + (d.migrated || 0) + ' 项数据');
        input.value = '';
        return api('/api/admin/data-settings');
      }).then(function (d) {
        modal.querySelector('#s-cur-root').textContent = d.data_root;
        modal.querySelector('#s-default-root').textContent = d.default_data_root;
        modal.querySelector('#s-usage-size').textContent = fmtSize(d.total_bytes);
      }).catch(function (err) {
        showToast(err.message, true);
      }).then(function () {
        btn.disabled = false; btn.textContent = '更改并迁移';
      });
    }
    btn.addEventListener('click', doChange);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); doChange(); } });
  }

  async function render() {
    try {
      const user = await getSession();
      if (!user) return;
      renderUserMenu(document.getElementById('user-slot'));

      const data = await api('/api/admin/summary');
      const modules = (data.modules || []).filter(m => m.allowed && m.id !== 'permission');
      if (modules.length === 0) {
        grid.innerHTML = '<div class="loading">当前账号无任何管理模块权限。</div>';
        return;
      }
      grid.innerHTML = modules.map(moduleCard).join('');
      // 绑定系统设置卡片点击事件
      var settingsCard = grid.querySelector('[data-settings-card]');
      if (settingsCard) settingsCard.addEventListener('click', openSettingsModal);
    } catch (err) {
      grid.innerHTML = '<div class="loading">加载失败：' + esc(err.message) + '</div>';
    }
  }

  render();
})();
