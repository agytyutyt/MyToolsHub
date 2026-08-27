/* 部门管理：列表渲染（含所属单位）、新建/编辑浮窗（单位下拉）、删除确认 */
(function () {
  const { api, esc, showToast, renderUserMenu, requireModule, openModal, confirmDialog } = window.AdminCommon;

  const listEl = document.getElementById('dept-list');
  let units = [];

  async function load() {
    try {
      const data = await api('/api/admin/departments');
      units = data.units || [];
      renderList(data.departments || []);
    } catch (err) {
      listEl.innerHTML = `<div class="loading">加载失败：${esc(err.message)}</div>`;
    }
  }

  function renderList(departments) {
    if (departments.length === 0) {
      listEl.innerHTML = '<div class="empty-state">暂无部门，点击右上角「新建部门」添加。</div>';
      return;
    }
    listEl.innerHTML = departments.map(d => `
      <div class="data-row" data-id="${esc(d.id)}">
        <div class="row-main">
          <div class="row-title">
            <span>${esc(d.name)}</span>
            <span class="chip">${esc(d.unit_name)}</span>
            <span class="chip">${esc(d.id)}</span>
          </div>
          <div class="row-desc">${esc(d.description || '暂无描述')} · 人员 ${d.users_count}</div>
        </div>
        <div class="row-actions">
          <button class="admin-btn text small" type="button" data-act="edit">编辑</button>
          <button class="admin-btn text small" type="button" data-act="del">删除</button>
        </div>
      </div>`).join('');
  }

  function unitOptionsHtml(selected) {
    return '<option value="">请选择单位</option>' + units.map(u =>
      `<option value="${esc(u.id)}" ${u.id === selected ? 'selected' : ''}>${esc(u.name)}</option>`
    ).join('');
  }

  function openForm(dept) {
    openModal(dept ? '编辑部门' : '新建部门', `
      <div class="form-grid">
        <div class="field">
          <label for="f-unit">所属单位</label>
          <select class="admin-input" id="f-unit">${unitOptionsHtml((dept && dept.unit_id) || '')}</select>
        </div>
        <div class="field">
          <label for="f-name">部门名称</label>
          <input class="admin-input" type="text" id="f-name" value="${esc((dept && dept.name) || '')}" placeholder="如：技术部">
        </div>
        <div class="field">
          <label for="f-desc">描述（可选）</label>
          <input class="admin-input" type="text" id="f-desc" value="${esc((dept && dept.description) || '')}" placeholder="部门职责说明">
        </div>
        <div class="modal-foot">
          <button class="admin-btn" type="button" data-close>取消</button>
          <button class="admin-btn primary" type="button" id="form-save">保存</button>
        </div>
      </div>`, (wrap) => {
      wrap.querySelector('#f-unit').focus();
      wrap.querySelector('#form-save').addEventListener('click', async () => {
        const unit_id = wrap.querySelector('#f-unit').value;
        const name = wrap.querySelector('#f-name').value.trim();
        const description = wrap.querySelector('#f-desc').value.trim();
        if (!unit_id) { showToast('请选择所属单位', true); return; }
        if (!name) { showToast('部门名称不能为空', true); return; }
        const payload = { unit_id, name, description };
        try {
          if (dept) {
            await api('/api/admin/departments/' + encodeURIComponent(dept.id), {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            });
          } else {
            await api('/api/admin/departments', {
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
      const titleEls = row.querySelectorAll('.row-title span');
      const name = titleEls[0].textContent;
      const unitName = titleEls[1].textContent;
      const desc = row.querySelector('.row-desc').textContent.split(' · ')[0];
      const unit = units.find(u => u.name === unitName);
      openForm({ id, name, description: desc === '暂无描述' ? '' : desc, unit_id: unit ? unit.id : '' });
    } else if (btn.dataset.act === 'del') {
      confirmDialog('确定删除该部门吗？此操作不可恢复。', async () => {
        try {
          await api('/api/admin/departments/' + encodeURIComponent(id), { method: 'DELETE' });
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
    const ok = await requireModule('department');
    if (!ok) return;
    renderUserMenu(document.getElementById('user-slot'));
    load();
  })();
})();
