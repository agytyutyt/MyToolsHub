/* 插件管理页：插件盘点 / 插件包上传应用 / 共享盘批量更新 / 待重启提示（阶段二·三）
 *
 * 与后端约定（plugins/admin/backend/plugin_admin.py）：
 *   GET  /api/admin/plugins              盘点
 *   POST /api/admin/plugins/upload       上传+只读校验 → 计划
 *   POST /api/admin/plugins/apply        应用（服务端会重新校验；成功且含后端改动则自动停服重启）
 *   POST /api/admin/plugins/rollback     回滚（成功后同样自动重启）
 *   POST /api/admin/plugins/enable       启用/停用
 *   GET  /api/admin/plugins/index        读共享盘索引并比对版本
 *   POST /api/admin/plugins/batch-apply  批量应用（全部成功后统一重启一次）
 *   POST /api/admin/plugins/restart      重启服务（仅打包运行；自动重启关闭/失败时的手工兜底）
 *
 * 升级主流程（管理员只需两步：选包 → 确认）：
 *   上传 .zip → 后端只读校验出计划 → 确认应用 → 备份 → 替换 → 自动停服重启 → 本页自动刷新。
 *   响应里的 restarting=true 表示服务端即将退出并自拉起，前端轮询等待后刷新。
 */
(function () {
  const { api, showToast, getSession, renderUserMenu, esc, confirmDialog } = window.AdminCommon;
  let lastUpload = '';      // 最近一次上传成功的包文件名（应用时回传给服务端）
  let lastUploadId = '';    // 最近一次上传成功的插件 id（提示语里显示中文名用）
  let indexRows = [];       // 最近一次"检查更新"的结果
  let nameMap = {};         // 插件 id → 中文展示名（后端按 tools.json 权威来源给出）
  const nm = (id, fallback) => nameMap[id] || fallback || id;

  function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return '-';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
    return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  function statusChips(p) {
    const out = [];
    if (!p.registered) out.push('<span class="plugin-chip warn">未登记</span>');
    else out.push(p.enabled ? '<span class="plugin-chip ok">已启用</span>'
                            : '<span class="plugin-chip">已停用</span>');
    if (p.hidden) out.push('<span class="plugin-chip">隐藏</span>');
    if (p.restart_pending) out.push('<span class="plugin-chip warn">待重启</span>');
    if (p.code_version && p.state_version && p.code_version !== p.state_version) {
      out.push('<span class="plugin-chip warn">登记不一致</span>');
    }
    return out.join(' ');
  }

  function renderRows(plugins) {
    const tbody = document.getElementById('plugin-rows');
    if (!plugins.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="admin-empty">没有发现插件</td></tr>';
      return;
    }
    tbody.innerHTML = plugins.map(p => `
      <tr data-id="${esc(p.id)}">
        <td>
          <div class="plugin-name"><span class="plugin-icon">${esc(p.icon || '🧩')}</span>
            <span>${esc(p.name)}<span class="plugin-id">${esc(p.id)}</span></span></div>
        </td>
        <td>${esc(p.code_version || '-')}</td>
        <td>${esc(p.state_version || '-')}</td>
        <td>${statusChips(p)}</td>
        <td>${p.backups ? p.backups + ' 份' : '-'}</td>
        <td>${fmtSize(p.data_bytes)}</td>
        <td style="text-align:right;white-space:nowrap">
          <button type="button" class="admin-btn small" data-act="rollback"
                  ${p.backups ? '' : 'disabled'}>回滚</button>
          <button type="button" class="admin-btn small ${p.enabled ? 'danger' : 'primary'}"
                  data-act="toggle">${p.enabled ? '停用' : '启用'}</button>
        </td>
      </tr>`).join('');
  }

  function renderBanner(pending, frozen) {
    const banner = document.getElementById('restart-banner');
    if (!pending || !pending.length) { banner.hidden = true; return; }
    banner.hidden = false;
    const names = pending.map(id => nm(id)).join('、');
    document.getElementById('restart-banner-text').textContent =
      `${names} 的代码已更新，需重启服务后生效。`;
    document.getElementById('btn-restart').hidden = false;
    document.getElementById('btn-restart').title = frozen ? '' : '源码开发模式不自动重启';
  }

  async function load() {
    try {
      const data = await api('/api/admin/plugins');
      nameMap = {};
      (data.plugins || []).forEach(p => { nameMap[p.id] = p.name; });
      renderRows(data.plugins || []);
      renderBanner(data.restart_pending, data.frozen);
      document.getElementById('app-version-note').textContent =
        data.app_version ? ` 主程序版本 ${data.app_version}。` : '';
      const idx = document.getElementById('index-path');
      if (!idx.value && data.index_path) idx.value = data.index_path;
    } catch (err) {
      document.getElementById('plugin-rows').innerHTML =
        `<tr><td colspan="7" class="admin-empty">加载失败：${esc(err.message)}</td></tr>`;
    }
  }

  // ---------------- 上传 / 应用 ----------------
  function planHtml(res) {
    const p = res.plan || {};
    const list = (title, arr, cls) => (arr && arr.length)
      ? `<div class="plugin-plan-line ${cls}">${title}：${arr.length} 个
           <div class="plugin-plan-files">${arr.map(esc).join('<br>')}</div></div>` : '';
    return `
      <div class="plugin-plan">
        <div class="plugin-plan-head">
          <b>${esc(nm(res.id, res.name))}</b><span class="plugin-id">${esc(res.id)}</span>
          ${esc(res.from_version || '（未安装）')} → <b>${esc(res.version)}</b>
          ${res.requires_restart ? '<span class="plugin-chip warn">需重启</span>'
                                 : '<span class="plugin-chip ok">前端更新，无需重启</span>'}
        </div>
        <div class="plugin-plan-line">新增 ${p.new || 0} · 修改 ${p.changed || 0} ·
          未变 ${p.unchanged || 0} · 删除 ${p.deleted || 0} · 保留未知 ${p.unknown || 0}</div>
        <div class="plugin-plan-line plugin-muted">基线：${esc(p.base_source || '')}</div>
        ${list('将删除', p.deleted_files, 'warn')}
        ${list('将保留（新旧清单都没有）', p.unknown_files, '')}
        ${(res.warnings && res.warnings.length)
          ? `<div class="plugin-plan-line warn">提示：${res.warnings.map(esc).join('；')}</div>` : ''}
        <div class="plugin-plan-actions">
          <button type="button" class="admin-btn primary" id="btn-apply">
            确认应用到 ${esc(nm(res.id, res.name))}</button>
          <span class="plugin-muted">应用前会自动备份旧版到数据根 backups 目录</span>
        </div>
      </div>`;
  }

  // 校验失败：把后端给的逐条原因列出来（不再只显示"HTTP 400"），并给出可操作的下一步
  function errListHtml(err) {
    const items = (err && Array.isArray(err.errors) && err.errors.length)
      ? err.errors : [err && err.message ? err.message : '未知原因'];
    return items.map(e => `<div class="plugin-plan-files">• ${esc(e)}</div>`).join('');
  }

  function rejectHint(err) {
    const all = ((err && err.errors) || []).join('；') + '；' + ((err && err.message) || '');
    if (/同版本重装|拒绝降级|不允许跳版升级/.test(all)) {
      return '<div class="plugin-plan-line">如确需重装 / 降级，勾选上方「强制」后重新上传。</div>';
    }
    if (/主程序版本过低|请先升级主程序/.test(all)) {
      return '<div class="plugin-plan-line">请先用整包升级主程序，再来上传本插件包。</div>';
    }
    if (/缺 version\.json|无法读取主程序版本/.test(all)) {
      return '<div class="plugin-plan-line">程序目录缺少 version.json：确认部署目录由 build-deploy.ps1 完整产出；'
        + '确属特殊情况可勾选「强制」跳过该项校验。</div>';
    }
    if (/哈希校验未通过|解压失败|包内缺少/.test(all)) {
      return '<div class="plugin-plan-line">包文件在传递中损坏或被人改过：请重新拷贝原始 zip（用旁挂的 .sha256 核对），不要解压后重压。</div>';
    }
    return '';
  }

  async function inspect() {
    const fileInput = document.getElementById('pkg-file');
    const f = fileInput.files && fileInput.files[0];
    if (!f) { showToast('请先选择插件包（.zip）', true); return; }
    const box = document.getElementById('inspect-result');
    box.innerHTML = '<div class="plugin-plan-line">校验中…</div>';
    const fd = new FormData();
    fd.append('file', f);
    if (document.getElementById('opt-force').checked) fd.append('force', '1');
    try {
      const res = await api('/api/admin/plugins/upload', { method: 'POST', body: fd });
      lastUpload = res.file;
      lastUploadId = res.id;
      box.innerHTML = planHtml(res);
      const btn = document.getElementById('btn-apply');
      if (btn) btn.addEventListener('click', applyPackage);
      showToast('校验通过，请确认计划后应用');
    } catch (err) {
      lastUpload = '';
      box.innerHTML = `<div class="plugin-plan-line warn">校验未通过：</div>${errListHtml(err)}${rejectHint(err)}`;
      showToast('校验未通过', true);
    }
  }

  // 服务自重启期间轮询，等它重新可用后自动刷新
  // （比固定 8 秒延时稳：慢机器也不会刷到"服务未启动"；最多等 90 秒）
  function waitAndReload(maxMs = 90000) {
    const started = Date.now();
    const tick = async () => {
      if (Date.now() - started > maxMs) { location.reload(); return; }
      try {
        const r = await fetch('/api/admin/plugins', { credentials: 'same-origin', cache: 'no-store' });
        if (r.status < 500) { location.reload(); return; }   // 401 也算"起来了"，刷新后跳登录
      } catch (err) { /* 重启中，继续等 */ }
      setTimeout(tick, 1500);
    };
    setTimeout(tick, 4000);   // 先给旧进程退出留出时间，避免打到还没停的实例
  }

  function applyPackage() {
    if (!lastUpload) { showToast('请先上传并校验插件包', true); return; }
    const targetName = nm(lastUploadId);
    const autoRestart = document.getElementById('opt-auto-restart').checked;
    const body = {
      file: lastUpload,
      force: document.getElementById('opt-force').checked,
      purge_unknown: document.getElementById('opt-purge').checked,
      update_entry: document.getElementById('opt-update-entry').checked,
      auto_restart: autoRestart,
    };
    confirmDialog(`确认将该插件包应用到「${targetName}」？应用会先备份旧版，再替换插件代码（用户数据不受影响）。`
      + (autoRestart
        ? '\n若包含后端改动，应用后会自动重启服务（约 5~10 秒不可用），本页随后自动刷新。'
        : '\n已关闭自动重启：应用后需点上方「立即重启服务」。'), async () => {
      const box = document.getElementById('inspect-result');
      box.innerHTML = '<div class="plugin-plan-line">应用中…</div>';
      try {
        const res = await api('/api/admin/plugins/apply', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        lastUpload = '';
        lastUploadId = '';
        document.getElementById('pkg-file').value = '';
        const head = `<div class="plugin-plan-head">已应用：<b>${esc(nm(res.id, res.name))}</b>
            <span class="plugin-id">${esc(res.id)}</span>
            ${esc(res.from_version || '（未安装）')} → <b>${esc(res.to_version)}</b></div>
          <div class="plugin-plan-line">写入 ${res.written} 个文件，删除 ${res.deleted} 个，
            保留未知 ${res.kept_unknown} 个</div>
          <div class="plugin-plan-line plugin-muted">备份：${esc(res.backup || '（全新安装，无旧版备份）')}</div>`;
        if (res.restarting) {
          box.innerHTML = `<div class="plugin-plan">${head}
            <div class="plugin-plan-line warn">${esc(res.restart_message || '服务正在重启…')}
              <div class="plugin-plan-files">请勿关闭本页，恢复后会自动刷新。</div></div>
          </div>`;
          showToast('已应用，服务正在重启…');
          waitAndReload();
          return;                       // 服务马上要停，别再发盘点请求
        }
        box.innerHTML = `<div class="plugin-plan">${head}
          ${res.restart_needed
            ? `<div class="plugin-plan-line warn">${esc(res.restart_message || '后端有改动：请点上方「立即重启服务」使其生效')}</div>`
            : '<div class="plugin-plan-line">前端已生效，页面按 Ctrl+F5 强刷一次即可</div>'}
        </div>`;
        showToast('已应用');
        await load();
      } catch (err) {
        // 极端时序：响应还没拿到进程就已退出（fetch 直接失败）→ 视作正在重启
        if (/fetch|network|failed|abort/i.test(err.message || '')) {
          box.innerHTML = '<div class="plugin-plan-line warn">服务正在重启，本页恢复后会自动刷新…</div>';
          waitAndReload();
          return;
        }
        box.innerHTML = `<div class="plugin-plan-line warn">应用失败：${esc(err.message)}</div>`;
        showToast('应用失败', true);
      }
    });
  }

  // ---------------- 盘点表操作 ----------------
  function bindTable() {
    document.getElementById('plugin-rows').addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button[data-act]');
      if (!btn) return;
      const tr = btn.closest('tr');
      const id = tr.getAttribute('data-id');
      if (btn.dataset.act === 'rollback') {
        const data = await api('/api/admin/plugins/backups?id=' + encodeURIComponent(id));
        const items = data.backups || [];
        if (!items.length) { showToast('该插件没有备份', true); return; }
        const opts = items.map((b, i) =>
          `<option value="${esc(b.name)}">${esc(b.name)}（${fmtSize(b.size)}）${i === 0 ? ' ← 最近' : ''}</option>`).join('');
        const wrap = window.AdminCommon.openModal('回滚插件：' + nm(id), `
          <div class="settings-form">
            <label for="rb-select">选择要恢复的备份</label>
            <select id="rb-select" class="admin-input">${opts}</select>
            <p class="settings-hint">回滚是"整目录替换"：当前目录里不在该备份中的文件会被丢弃（会先列出）。
              回滚前会自动把当前版本也备份一份。</p>
            <div class="modal-foot">
              <button type="button" class="admin-btn" data-close>取消</button>
              <button type="button" class="admin-btn danger" id="rb-ok">确认回滚</button>
            </div>
          </div>`);
        wrap.querySelector('#rb-ok').addEventListener('click', async () => {
          const name = wrap.querySelector('#rb-select').value;
          wrap.querySelector('[data-close]').click();
          try {
            const res = await api('/api/admin/plugins/rollback', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ id, backup: name }),
            });
            showToast(`已回滚 ${nm(id, res.name)}：${res.from_version} → ${res.to_version}`);
            if (res.dropped && res.dropped.length) {
              showToast(`注意：有 ${res.dropped.length} 个文件未包含在备份中，已丢弃`, true);
            }
            if (res.restarting) {
              showToast(res.restart_message || '服务正在重启…');
              waitAndReload();
              return;
            }
            if (res.restart_needed) {
              showToast(res.restart_message || '需重启后生效，可点上方「立即重启服务」', true);
            }
            await load();
          } catch (err) { showToast(err.message, true); }
        });
        return;
      }
      if (btn.dataset.act === 'toggle') {
        const enable = btn.textContent.trim() === '启用';
        try {
          await api('/api/admin/plugins/enable', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, enabled: enable }),
          });
          showToast(enable ? '已启用（刷新页面后卡片可见）' : '已停用（卡片隐藏、后端不再注册）');
          await load();
        } catch (err) { showToast(err.message, true); }
      }
    });
  }

  // ---------------- 共享盘批量更新 ----------------
  function indexHtml(res) {
    const rows = res.rows || [];
    if (!rows.length) return '<div class="plugin-plan-line">索引里没有插件包记录。</div>';
    indexRows = rows;
    const body = rows.map(r => `
      <tr>
        <td><input type="checkbox" data-id="${esc(r.id)}"
             ${r.upgrade_available ? 'checked' : 'disabled'}></td>
        <td>${esc(nm(r.id, r.name))}<span class="plugin-id">${esc(r.id)}</span>
          ${r.requires_restart ? '<span class="plugin-chip warn">需重启</span>' : ''}</td>
        <td>${esc(r.current || '（未安装）')} → <b>${esc(r.available)}</b></td>
        <td>${fmtSize(r.size)}</td>
        <td>${r.upgrade_available ? '<span class="plugin-chip ok">可升级</span>'
                                  : '<span class="plugin-chip">' + esc(r.blocked_reason || '跳过') + '</span>'}</td>
        <td class="plugin-muted">${esc(r.changelog || '')}</td>
      </tr>`).join('');
    const upgradable = rows.filter(r => r.upgrade_available).length;
    return `
      <div class="plugin-plan">
        <div class="plugin-plan-line">索引目录：<code>${esc(res.index_dir || '')}</code>
          · 共 ${rows.length} 项，可升级 ${upgradable} 项</div>
        <div class="data-table-wrap">
          <table class="data-table">
            <thead><tr><th></th><th>插件</th><th>版本</th><th>体积</th><th>状态</th><th>说明</th></tr></thead>
            <tbody>${body}</tbody>
          </table>
        </div>
        <div class="plugin-plan-actions">
          <button type="button" class="admin-btn primary" id="btn-batch"
                  ${upgradable ? '' : 'disabled'}>批量升级所选</button>
          <span class="plugin-muted">顺序应用；后端有改动时最后统一重启一次</span>
        </div>
      </div>`;
  }

  async function checkUpdates() {
    const path = document.getElementById('index-path').value.trim();
    if (!path) { showToast('请填写索引文件路径', true); return; }
    const box = document.getElementById('index-result');
    box.innerHTML = '<div class="plugin-plan-line">读取索引中…</div>';
    try {
      const res = await api('/api/admin/plugins/index?path=' + encodeURIComponent(path));
      box.innerHTML = indexHtml(res);
      const btn = document.getElementById('btn-batch');
      if (btn) btn.addEventListener('click', batchApply);
      showToast('已读取索引');
    } catch (err) {
      box.innerHTML = `<div class="plugin-plan-line warn">读取失败：${esc(err.message)}</div>`;
      showToast('读取失败', true);
    }
  }

  function batchApply() {
    const ids = Array.from(document.querySelectorAll('#index-result input[type=checkbox]:checked'))
      .map(c => c.getAttribute('data-id'));
    if (!ids.length) { showToast('请勾选要升级的插件', true); return; }
    const needRestart = indexRows.filter(r => ids.includes(r.id) && r.requires_restart).map(r => nm(r.id, r.name));
    confirmDialog(`将升级 ${ids.length} 个插件：${ids.map(id => nm(id)).join('、')}。`
      + (needRestart.length ? `其中 ${needRestart.join('、')} 含后端改动，完成后需重启服务。` : ''),
      async () => {
        const box = document.getElementById('index-result');
        box.innerHTML = '<div class="plugin-plan-line">批量升级中…</div>';
        try {
          const res = await api('/api/admin/plugins/batch-apply', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids }),
          });
          const lines = (res.results || []).map(r => r.ok
            ? `<div class="plugin-plan-line">✅ ${esc(nm(r.id, r.name))}（${esc(r.id)}）：${esc(r.from_version || '（未安装）')} → ${esc(r.to_version)}</div>`
            : `<div class="plugin-plan-line warn">❌ ${esc(nm(r.id, r.name))}（${esc(r.id)}）：${esc(r.error)}</div>`).join('');
          if (res.restarting) {
            box.innerHTML = `<div class="plugin-plan">${lines}
              <div class="plugin-plan-line warn">${esc(res.restart_message || '服务正在重启…')}
                <div class="plugin-plan-files">请勿关闭本页，恢复后会自动刷新。</div></div></div>`;
            showToast(`批量升级完成：成功 ${res.applied}，失败 ${res.failed}；服务正在重启…`);
            waitAndReload();
            return;
          }
          box.innerHTML = `<div class="plugin-plan">${lines}
            ${res.restart_needed
              ? `<div class="plugin-plan-line warn">${esc(res.restart_message || '有后端改动：请点上方「立即重启服务」')}</div>`
              : '<div class="plugin-plan-line">前端已生效（Ctrl+F5 强刷）</div>'}</div>`;
          showToast(`批量升级完成：成功 ${res.applied}，失败 ${res.failed}`);
          await load();
        } catch (err) {
          box.innerHTML = `<div class="plugin-plan-line warn">批量升级失败：${esc(err.message)}</div>`;
          showToast('批量升级失败', true);
        }
      });
  }

  // ---------------- 重启 ----------------
  function bindRestart() {
    document.getElementById('btn-restart').addEventListener('click', () => {
      confirmDialog('确认重启服务？期间约 5~10 秒无法访问，页面会自动重新加载。', async () => {
        try {
          const res = await api('/api/admin/plugins/restart', { method: 'POST' });
          if (res.ok) {
            showToast(res.message || '服务正在重启…');
            waitAndReload();
          } else {
            showToast(res.message || res.error || '未能自动重启', true);
          }
        } catch (err) {
          // 服务重启瞬间请求会失败，属预期
          showToast('服务正在重启，稍后刷新页面…');
          waitAndReload();
        }
      });
    });
  }

  async function init() {
    const user = await getSession();
    if (!user) return;
    if (!user.super_admin) {
      showToast('仅超级管理员可管理插件', true);
      setTimeout(() => { window.location.href = '/admin'; }, 800);
      return;
    }
    renderUserMenu(document.getElementById('user-slot'));
    document.getElementById('btn-inspect').addEventListener('click', inspect);
    document.getElementById('btn-check').addEventListener('click', checkUpdates);
    document.getElementById('index-path').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); checkUpdates(); }
    });
    bindTable();
    bindRestart();
    load();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
