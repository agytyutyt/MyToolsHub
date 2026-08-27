/* 权限管理：角色列表、新建/编辑浮窗（模块勾选）、删除确认 */
(function () {
  const { api, esc, showToast, renderUserMenu, requireModule, openModal, confirmDialog } = window.AdminCommon;

  const listEl = document.getElementById('perm-list');

  const MODULE_OPTIONS = [
    { id: 'unit', name: '单位管理' },
    { id: 'department', name: '部门管理' },
    { id: 'user', name: '人员管理' },
    { id: 'permission', name: '权限管理' },
  ];

  async function load() {
    try {
      const data = await api('/api/admin/permissions');
      renderList(data.permissions || []);
    } catch (err) {
      listEl.innerHTML = `<div class="loading">加载失败：${esc(err.message)}</div>`;
    }
  }

  function renderList(permissions) {
    if (permissions.length === 0) {
      listEl.innerHTML = '<div class="empty-state">暂无角色，点击右上角「新建角色」添加。</div>';
      return;
    }
    listEl.innerHTML = permissions.map(r => `
      <div class="data-row" data-id="${esc(r.id)}">
        <div class="row-main">
          <div class="row-title">
            <span>${esc(r.name)}</span>
            ${(r.modules || []).map(m => {
              const opt = MODULE_OPTIONS.find(o => o.id === m);
              return opt ? `<span class="chip">${opt.name}</span>` : '';
            }).join('')}
          </div>
          <div class="row-desc">${esc(r.description || '暂无描述')} · 已分配 ${r.users} 人</div>
        </div>
        <div class="row-actions">
          <button class="admin-btn text small" type="button" data-act="edit">编辑</button>
          <button class="admin-btn text small" type="button" data-act="del">删除</button>
        </div>
      </div>`).join('');
  }

  function moduleChecksHtml(selected) {
    return `
      <div class="module-check">
        ${MODULE_OPTIONS.map(o => `
          <label><input type="checkbox" value="${o.id}" ${(selected || []).includes(o.id) ? 'checked' : ''}> ${o.name}</label>
        `).join('')}
      </div>`;
  }

  function openForm(role) {
    openModal(role ? '编辑角色' : '新建角色', `
      <div class="form-grid">
        <div class="field">
          <label for="f-name">角色名称</label>
          <input class="admin-input" type="text" id="f-name" value="${esc((role && role.name) || '')}" placeholder="如：普通成员">
        </div>
        <div class="field">
          <label for="f-desc">描述（可选）</label>
          <input class="admin-input" type="text" id="f-desc" value="${esc((role && role.description) || '')}">
        </div>
        <div class="field">
          <label>可访问的管理模块</label>
          <div id="f-modules">${moduleChecksHtml(role && role.modules)}</div>
        </div>
        <div class="modal-foot">
          <button class="admin-btn" type="button" data-close>取消</button>
          <button class="admin-btn primary" type="button" id="form-save">保存</button>
        </div>
      </div>`, (wrap) => {
      wrap.querySelector('#f-name').focus();
      wrap.querySelector('#form-save').addEventListener('click', async () => {
        const name = wrap.querySelector('#f-name').value.trim();
        const description = wrap.querySelector('#f-desc').value.trim();
        const modules = [...wrap.querySelectorAll('#f-modules input:checked')].map(i => i.value);
        if (!name) { showToast('角色名称不能为空', true); return; }
        if (modules.length === 0) { showToast('请至少勾选一个可访问模块', true); return; }
        const payload = { name, description, modules };
        try {
          if (role) {
            await api('/api/admin/permissions/' + encodeURIComponent(role.id), {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            });
          } else {
            await api('/api/admin/permissions', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            });
          }
          wrap.querySelector('[data-close]').click();
          showToast('已保存');
          load();
        } catch (err) {
          showToast('保存失败：' + err.message, true);
        }
      });
    });
  }

  listEl.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const row = btn.closest('.data-row');
    const id = row.dataset.id;
    if (btn.dataset.act === 'edit') {
      const title = row.querySelector('.row-title span').textContent;
      const descEl = row.querySelector('.row-desc').textContent;
      const modules = [...row.querySelectorAll('.chip')].map(c => {
        const opt = MODULE_OPTIONS.find(o => o.name === c.textContent);
        return opt ? opt.id : null;
      }).filter(Boolean);
      openForm({ id, name: title, description: descEl.split(' · ')[0], modules });
    } else if (btn.dataset.act === 'del') {
      confirmDialog('确定删除该角色吗？此操作不可恢复。', async () => {
        try {
          await api('/api/admin/permissions/' + encodeURIComponent(id), { method: 'DELETE' });
          showToast('已删除');
          load();
        } catch (err) {
          showToast('删除失败：' + err.message, true);
        }
      });
    }
  });

  document.getElementById('btn-new').addEventListener('click', () => openForm(null));

  (async () => {
    const ok = await requireModule('permission');
    if (!ok) return;
    renderUserMenu(document.getElementById('user-slot'));
    load();
  })();
})();
