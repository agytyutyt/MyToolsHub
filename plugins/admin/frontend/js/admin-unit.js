/* 单位管理：列表渲染、新建/编辑浮窗、删除确认 */
(function () {
  const { api, esc, showToast, renderUserMenu, requireModule, openModal, confirmDialog } = window.AdminCommon;

  const listEl = document.getElementById('unit-list');

  async function load() {
    try {
      const data = await api('/api/admin/units');
      renderList(data.units || []);
    } catch (err) {
      listEl.innerHTML = `<div class="loading">加载失败：${esc(err.message)}</div>`;
    }
  }

  function renderList(units) {
    if (units.length === 0) {
      listEl.innerHTML = '<div class="empty-state">暂无单位，点击右上角「新建单位」添加。</div>';
      return;
    }
    listEl.innerHTML = units.map(u => `
      <div class="data-row" data-id="${esc(u.id)}">
        <div class="row-main">
          <div class="row-title">
            <span>${esc(u.name)}</span>
            <span class="chip">${esc(u.id)}</span>
          </div>
          <div class="row-desc">${esc(u.description || '暂无描述')} · 部门 ${u.departments_count} · 人员 ${u.users_count}</div>
        </div>
        <div class="row-actions">
          <button class="admin-btn text small" type="button" data-act="edit">编辑</button>
          <button class="admin-btn text small" type="button" data-act="del">删除</button>
        </div>
      </div>`).join('');
  }

  function openForm(unit) {
    openModal(unit ? '编辑单位' : '新建单位', `
      <div class="form-grid">
        <div class="field">
          <label for="f-name">单位名称</label>
          <input class="admin-input" type="text" id="f-name" value="${esc(unit?.name || '')}" placeholder="如：某某总队">
        </div>
        <div class="field">
          <label for="f-desc">描述（可选）</label>
          <input class="admin-input" type="text" id="f-desc" value="${esc(unit?.description || '')}" placeholder="单位职责说明">
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
        if (!name) { showToast('单位名称不能为空', true); return; }
        try {
          if (unit) {
            await api('/api/admin/units/' + encodeURIComponent(unit.id), {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name, description }),
            });
          } else {
            await api('/api/admin/units', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name, description }),
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
      const name = row.querySelector('.row-title span').textContent;
      const desc = row.querySelector('.row-desc').textContent.split(' · ')[0];
      openForm({ id, name, description: desc === '暂无描述' ? '' : desc });
    } else if (btn.dataset.act === 'del') {
      confirmDialog('确定删除该单位吗？此操作不可恢复。', async () => {
        try {
          await api('/api/admin/units/' + encodeURIComponent(id), { method: 'DELETE' });
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
    const ok = await requireModule('unit');
    if (!ok) return;
    renderUserMenu(document.getElementById('user-slot'));
    load();
  })();
})();
