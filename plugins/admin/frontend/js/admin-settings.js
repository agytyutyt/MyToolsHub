/* 系统设置页：展示/修改数据保存目录，迁移后刷新显示 */
(function () {
  const { api, showToast, getSession, renderUserMenu } = window.AdminCommon;

  function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return '-';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
    return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  async function load() {
    try {
      const data = await api('/api/admin/data-settings');
      document.getElementById('cur-root').textContent = data.data_root;
      document.getElementById('default-root').textContent = data.default_data_root;
      document.getElementById('usage-size').textContent = fmtSize(data.total_bytes);
    } catch (err) {
      document.getElementById('cur-root').textContent = '读取失败：' + err.message;
    }
  }

  async function changeRoot() {
    const input = document.getElementById('new-root-input');
    const root = input.value.trim();
    if (!root) { showToast('请先填写新的数据目录', true); input.focus(); return; }
    const btn = document.getElementById('btn-change-root');
    btn.disabled = true;
    btn.textContent = '迁移中…';
    try {
      const data = await api('/api/admin/data-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data_root: root, migrate: true }),
      });
      showToast(`已切换数据目录，迁移 ${data.migrated || 0} 项数据`);
      input.value = '';
      await load();
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '更改并迁移';
    }
  }

  function init() {
    renderUserMenu(document.getElementById('user-slot'));
    document.getElementById('btn-change-root').addEventListener('click', changeRoot);
    document.getElementById('new-root-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); changeRoot(); }
    });
    load();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
