/* 系统设置页：展示/修改数据保存目录，迁移后刷新显示 */
(function () {
  const { api, showToast, getSession } = window.AdminCommon;

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

  /* ==================== 桌面端接入密钥（壳身份门） ==================== */

  let shellToken = '';
  let shellTokenShown = false;

  function maskToken(t) {
    return (t && t.length > 12) ? t.slice(0, 6) + '…' + t.slice(-4) : t || '';
  }

  function renderShellToken() {
    const input = document.getElementById('shell-token-value');
    input.value = shellTokenShown ? shellToken : maskToken(shellToken);
    document.getElementById('btn-toggle-token').textContent = shellTokenShown ? '隐藏' : '显示';
  }

  async function loadShellToken() {
    try {
      const data = await api('/api/admin/shell-token');
      shellToken = data.token || '';
      renderShellToken();
      const notes = [];
      if (data.env_override === false) notes.push('环境变量已强制关闭门控（JZTOOLS_SHELL_ENFORCE=0）');
      else if (data.env_override === true) notes.push('环境变量已强制开启门控');
      else if (data.enforce === false) notes.push('门控当前关闭（shell-token.json enforce=false）');
      if (Array.isArray(data.allowed_ips) && data.allowed_ips.length) {
        notes.push('IP 白名单 ' + data.allowed_ips.length + ' 条');
      }
      notes.push('生成于 ' + (data.created_at || '-'));
      document.getElementById('shell-token-meta').textContent = notes.join('；');
    } catch (err) {
      shellToken = '';
      renderShellToken();
      document.getElementById('shell-token-meta').textContent = '读取失败：' + err.message;
    }
  }

  async function copyShellToken() {
    if (!shellToken) { showToast('密钥尚未加载', true); return; }
    try {
      await navigator.clipboard.writeText(shellToken);
      showToast('已复制，请粘贴到桌面端「连接设置」的接入密钥中');
    } catch (e) {
      // 剪贴板 API 不可用（非安全上下文等）→ 退回选中文本让管理员手工复制
      const input = document.getElementById('shell-token-value');
      shellTokenShown = true;
      renderShellToken();
      input.focus();
      input.select();
      showToast('请按 Ctrl+C 复制选中的密钥');
    }
  }

  function resetShellToken() {
    confirmDialog('重置桌面端接入密钥？所有已分发旧密钥将立即失效，需逐台更新客户端配置。', async () => {
      try {
        const data = await api('/api/admin/shell-token', { method: 'POST' });
        shellToken = data.token || '';
        shellTokenShown = true;
        renderShellToken();
        showToast('已重置，请把新密钥分发到各桌面端');
        await loadShellToken();   // 刷新 meta（created_at 等）；显示态沿用 shellTokenShown
      } catch (err) {
        showToast(err.message, true);
      }
    });
  }

  function init() {
    document.getElementById('btn-change-root').addEventListener('click', changeRoot);
    document.getElementById('new-root-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); changeRoot(); }
    });
    document.getElementById('btn-toggle-token').addEventListener('click', () => {
      shellTokenShown = !shellTokenShown;
      renderShellToken();
    });
    document.getElementById('btn-copy-token').addEventListener('click', copyShellToken);
    document.getElementById('btn-reset-token').addEventListener('click', resetShellToken);
    load();
    loadShellToken();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
