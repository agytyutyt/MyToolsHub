/* 人员管理：列表表格、新建/编辑浮窗（单位→部门级联、身份证、大模型配置）、
 * 按人分配工具权限（权限弹窗）、角色管理（原权限管理模块并入本页）、删除确认 */
(function () {
  const { api, esc, showToast, renderUserMenu, requireModule, openModal, confirmDialog } = window.AdminCommon;

  const wrapEl = document.getElementById('user-wrap');
  const roleListEl = document.getElementById('role-list');
  let units = [];
  let depts = [];
  let roles = [];
  let allUsers = [];
  let userMap = {}; // username -> 完整用户数据（供编辑浮窗回填）

  const MODULE_OPTIONS = [
    { id: 'unit', name: '单位管理' },
    { id: 'department', name: '部门管理' },
    { id: 'user', name: '人员管理' },
    { id: 'permission', name: '权限管理' },
  ];

  async function load() {
    try {
      const data = await api('/api/admin/users');
      units = data.units || [];
      depts = data.departments || [];
      roles = data.permissions || [];
      allUsers = data.users || [];
      userMap = {};
      allUsers.forEach(u => { userMap[u.username] = u; });
      renderList(allUsers);
      renderRoles();
    } catch (err) {
      wrapEl.innerHTML = `<div class="loading">加载失败：${esc(err.message)}</div>`;
    }
  }

  function maskIdcard(idcard) {
    const s = String(idcard || '');
    if (!s) return '—';
    if (s.length <= 8) return s.replace(/^(.).+(.)$/, '$1***$2');
    return s.slice(0, 4) + '**********' + s.slice(-4);
  }

  function maskKey(key) {
    const s = String(key || '');
    if (!s) return '<span style="color:var(--md-on-surface-variant);">未配置</span>';
    if (s.length <= 8) return '••••••••';
    return esc(s.slice(0, 4)) + '…' + esc(s.slice(-4));
  }

  function llmStatus(u) {
    const llm = u.llm || {};
    if (!llm.api_key) return '<span style="color:var(--md-on-surface-variant);">未配置</span>';
    return maskKey(llm.api_key);
  }

  function renderList(users) {
    if (users.length === 0) {
      wrapEl.innerHTML = '<div class="empty-state">暂无人员，点击右上角「新建人员」添加。</div>';
      return;
    }
    wrapEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>登录名</th>
            <th>姓名</th>
            <th>所属单位</th>
            <th>所属部门</th>
            <th>角色</th>
            <th>身份证号码</th>
            <th>大模型 API</th>
            <th style="text-align:right;">操作</th>
          </tr>
        </thead>
        <tbody>
          ${users.map(u => `
            <tr data-username="${esc(u.username)}">
              <td><strong>${esc(u.username)}</strong></td>
              <td>${esc(u.name)}</td>
              <td>${esc(u.unit_name || '—')}</td>
              <td>${esc(u.department_name || '—')}</td>
              <td>${esc(u.role_name || '未分配')}</td>
              <td>${esc(maskIdcard(u.idcard))}</td>
              <td>${llmStatus(u)}</td>
              <td style="text-align:right;white-space:nowrap;">
                <button class="admin-btn text small" type="button" data-act="perm">权限</button>
                <button class="admin-btn text small" type="button" data-act="edit">编辑</button>
                <button class="admin-btn text small" type="button" data-act="del">删除</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  }

  function optionsHtml(list, key, label, selected) {
    return '<option value="">未分配</option>' + list.map(item =>
      `<option value="${esc(item[key])}" ${item[key] === selected ? 'selected' : ''}>${esc(item[label])}</option>`
    ).join('');
  }

  function unitOptionsHtml(selected) {
    return '<option value="">请选择单位</option>' + units.map(u =>
      `<option value="${esc(u.id)}" ${u.id === selected ? 'selected' : ''}>${esc(u.name)}</option>`
    ).join('');
  }

  // 根据选中的单位渲染部门下拉
  function deptOptionsHtml(unitId, selectedDeptId) {
    const opts = depts.filter(d => d.unit_id === unitId);
    if (opts.length === 0) return '<option value="">该单位暂无部门</option>';
    return '<option value="">请选择部门</option>' + opts.map(d =>
      `<option value="${esc(d.id)}" ${d.id === selectedDeptId ? 'selected' : ''}>${esc(d.name)}</option>`
    ).join('');
  }

  function openForm(user) {
    const llm = (user && user.llm) || {};
    openModal(user ? '编辑人员' : '新建人员', `
      <div class="form-grid">
        <div class="field">
          <label for="f-username">登录名</label>
          <input class="admin-input" type="text" id="f-username" value="${esc((user && user.username) || '')}"
                 ${user ? 'disabled' : ''} placeholder="字母/数字/_ . -">
          ${user ? '<span class="field-hint">登录名创建后不可修改</span>' : ''}
        </div>
        <div class="field">
          <label for="f-name">姓名</label>
          <input class="admin-input" type="text" id="f-name" value="${esc((user && user.name) || '')}" placeholder="真实姓名">
        </div>
        <div class="field">
          <label for="f-password">密码 ${user ? '（留空保持不变）' : ''}</label>
          <input class="admin-input" type="password" id="f-password" autocomplete="new-password" placeholder="至少 6 位">
        </div>
        <div class="field">
          <label for="f-unit">所属单位</label>
          <select class="admin-input" id="f-unit">${unitOptionsHtml((user && user.unit_id) || '')}</select>
        </div>
        <div class="field">
          <label for="f-dept">所属部门</label>
          <select class="admin-input" id="f-dept">${deptOptionsHtml((user && user.unit_id) || '', (user && user.department_id) || '')}</select>
        </div>
        <div class="field">
          <label for="f-role">角色</label>
          <select class="admin-input" id="f-role">${optionsHtml(roles, 'id', 'name', (user && user.role) || '')}</select>
        </div>
        <div class="field">
          <label for="f-idcard">身份证号码</label>
          <input class="admin-input" type="text" id="f-idcard" value="${esc((user && user.idcard) || '')}"
                 placeholder="18 位居民身份证号" autocomplete="off">
        </div>
        <div class="field" style="margin-top:4px;padding-top:14px;border-top:1px solid var(--md-outline-variant);">
          <label for="f-llm-url">大模型 Base URL</label>
          <input class="admin-input" type="text" id="f-llm-url" value="${esc(llm.base_url || '')}"
                 placeholder="如：https://api.deepseek.com/v1">
        </div>
        <div class="field">
          <label for="f-llm-key">大模型 API Key ${user && llm.api_key ? '（已配置，留空保持不变）' : ''}</label>
          <input class="admin-input" type="password" id="f-llm-key" value="${esc(llm.api_key || '')}"
                 autocomplete="off" placeholder="留空则保持不变">
        </div>
        <div class="field">
          <label for="f-llm-model">模型名称</label>
          <input class="admin-input" type="text" id="f-llm-model" value="${esc(llm.model || '')}"
                 placeholder="如：deepseek-chat">
        </div>
        <div class="modal-foot">
          <button class="admin-btn" type="button" data-close>取消</button>
          <button class="admin-btn primary" type="button" id="form-save">保存</button>
        </div>
      </div>`, (wrap) => {
      const unitSel = wrap.querySelector('#f-unit');
      const deptSel = wrap.querySelector('#f-dept');
      // 单位切换后刷新部门下拉
      unitSel.addEventListener('change', () => {
        deptSel.innerHTML = deptOptionsHtml(unitSel.value, '');
      });
      wrap.querySelector('#f-name').focus();
      wrap.querySelector('#form-save').addEventListener('click', async () => {
        const password = wrap.querySelector('#f-password').value;
        const payload = {
          name: wrap.querySelector('#f-name').value.trim(),
          unit_id: unitSel.value,
          department_id: deptSel.value,
          role: wrap.querySelector('#f-role').value,
          idcard: wrap.querySelector('#f-idcard').value.trim(),
          llm: {
            base_url: wrap.querySelector('#f-llm-url').value.trim(),
            api_key: wrap.querySelector('#f-llm-key').value.trim(),
            model: wrap.querySelector('#f-llm-model').value.trim(),
          },
        };
        if (password) payload.password = password;
        if (!payload.name) { showToast('姓名不能为空', true); return; }
        if (!payload.unit_id || !payload.department_id) { showToast('请选择所属单位与部门', true); return; }
        try {
          if (user) {
            await api('/api/admin/users/' + encodeURIComponent(user.username), {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            });
          } else {
            const username = wrap.querySelector('#f-username').value.trim();
            if (!username) { showToast('登录名不能为空', true); return; }
            if (!password) { showToast('请设置初始密码', true); return; }
            await api('/api/admin/users', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ username, ...payload }),
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

  wrapEl.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const tr = btn.closest('tr');
    const username = tr.dataset.username;
    if (btn.dataset.act === 'perm') {
      openPermModal(userMap[username] || { username });
    } else if (btn.dataset.act === 'edit') {
      openForm(userMap[username] || { username });
    } else if (btn.dataset.act === 'del') {
      confirmDialog(`确定删除人员「${username}」吗？其登录账号将立即失效。`, async () => {
        try {
          await api('/api/admin/users/' + encodeURIComponent(username), { method: 'DELETE' });
          showToast('已删除');
          load();
        } catch (err) {
          showToast('删除失败：' + err.message, true);
        }
      });
    }
  });

  document.getElementById('btn-new').addEventListener('click', () => openForm(null));

  /* ==================== 权限设置（按人勾选可访问的功能模块） ==================== */
  async function openPermModal(user) {
    let tools;
    try {
      const data = await api('/api/tools/visibility');
      tools = (data.tools || []).slice();
    } catch (e) {
      showToast('加载功能模块失败：' + e.message, true);
      return;
    }
    const superAdmin = !!user.super_admin;
    // 管理员展示全部模块（含管理后台）；办案员等其他角色不展示「管理后台」
    const list = superAdmin ? tools : tools.filter(function (t) { return t.id !== 'admin'; });
    const checkedSet = new Set(user.permissions || []);
    const rows = list.map(function (t) {
      var checked = superAdmin ? 'checked disabled' : (checkedSet.has(t.id) ? 'checked' : '');
      return '<label style="display:flex;align-items:center;gap:8px;font-size:14px;cursor:pointer;padding:4px 0;">' +
        '<input type="checkbox" value="' + esc(t.id) + '" ' + checked + '>' +
        esc(t.name || t.id) +
        (t.enabled === false ? ' <span style="color:var(--md-on-surface-variant);font-size:12px;">（已停用）</span>' : '') +
        '</label>';
    }).join('');

    openModal('权限设置 — ' + user.name, '' +
      '<div class="form-grid">' +
      (superAdmin
        ? '<div style="font-size:13px;color:var(--md-on-surface-variant);">该人员为管理员，拥有全部功能模块权限，无需调整。</div>'
        : '<div style="font-size:13px;color:var(--md-on-surface-variant);">勾选该人员可访问的功能模块（管理后台不开放）。</div>') +
      '<div class="module-check">' + rows + '</div>' +
      (superAdmin ? '' : '<div class="modal-foot"><button class="admin-btn" type="button" data-close>取消</button>' +
        '<button class="admin-btn primary" type="button" id="perm-save">保存</button></div>') +
      '</div>', function (wrap) {
      var saveBtn = wrap.querySelector('#perm-save');
      if (!saveBtn) return;
      saveBtn.addEventListener('click', async function () {
        var selected = [...wrap.querySelectorAll('.module-check input:checked')].map(function (i) { return i.value; });
        saveBtn.disabled = true;
        try {
          await api('/api/admin/users/' + encodeURIComponent(user.username), {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: user.name || '', permissions: selected }),
          });
          wrap.querySelector('[data-close]').click();
          showToast('权限已保存');
          load();
        } catch (err) {
          saveBtn.disabled = false;
          showToast('保存失败：' + err.message, true);
        }
      });
    });
  }

  /* ==================== 角色管理（权限管理已并入人员管理） ==================== */
  function renderRoles() {
    if (!roleListEl) return;
    if (roles.length === 0) {
      roleListEl.innerHTML = '<div class="empty-state">暂无角色，点击右上角「新建角色」添加。</div>';
      return;
    }
    var usage = {};
    allUsers.forEach(function (u) { usage[u.role] = (usage[u.role] || 0) + 1; });
    roleListEl.innerHTML = roles.map(function (r) {
      var chips = (r.modules || []).map(function (m) {
        var opt = MODULE_OPTIONS.find(function (o) { return o.id === m; });
        return opt ? '<span class="chip">' + esc(opt.name) + '</span>' : '';
      }).join('');
      return '<div class="data-row" data-id="' + esc(r.id) + '">' +
        '<div class="row-main">' +
        '<div class="row-title"><span>' + esc(r.name) + '</span>' + chips + '</div>' +
        '<div class="row-desc">' + esc(r.description || '暂无描述') + ' · 已分配 ' + (usage[r.id] || 0) + ' 人</div>' +
        '</div>' +
        '<div class="row-actions">' +
        '<button class="admin-btn text small" type="button" data-act="edit">编辑</button>' +
        '<button class="admin-btn text small" type="button" data-act="del">删除</button>' +
        '</div></div>';
    }).join('');
  }

  function moduleChecksHtml(selected) {
    return MODULE_OPTIONS.map(function (o) {
      return '<label><input type="checkbox" value="' + o.id + '" ' +
        ((selected || []).includes(o.id) ? 'checked' : '') + '> ' + o.name + '</label>';
    }).join('');
  }

  function openRoleForm(role) {
    openModal(role ? '编辑角色' : '新建角色', '' +
      '<div class="form-grid">' +
      '<div class="field"><label for="rf-name">角色名称</label>' +
      '<input class="admin-input" type="text" id="rf-name" value="' + esc((role && role.name) || '') + '" placeholder="如：办案员"></div>' +
      '<div class="field"><label for="rf-desc">描述（可选）</label>' +
      '<input class="admin-input" type="text" id="rf-desc" value="' + esc((role && role.description) || '') + '"></div>' +
      '<div class="field"><label>可访问的管理模块（不勾选则为纯工具使用者，如办案员）</label>' +
      '<div class="module-check" id="rf-modules">' + moduleChecksHtml(role && role.modules) + '</div></div>' +
      '<div class="modal-foot"><button class="admin-btn" type="button" data-close>取消</button>' +
      '<button class="admin-btn primary" type="button" id="role-save">保存</button></div>' +
      '</div>', function (wrap) {
      wrap.querySelector('#rf-name').focus();
      wrap.querySelector('#role-save').addEventListener('click', async function () {
        var name = wrap.querySelector('#rf-name').value.trim();
        var description = wrap.querySelector('#rf-desc').value.trim();
        var modules = [...wrap.querySelectorAll('#rf-modules input:checked')].map(function (i) { return i.value; });
        if (!name) { showToast('角色名称不能为空', true); return; }
        var payload = { name: name, description: description, modules: modules };
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

  // 角色列表的操作按钮（编辑/删除）
  if (roleListEl) {
    roleListEl.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-act]');
      if (!btn) return;
      var row = btn.closest('.data-row');
      var id = row.dataset.id;
      if (btn.dataset.act === 'edit') {
        openRoleForm(roles.find(function (r) { return r.id === id; }) || { id: id });
      } else if (btn.dataset.act === 'del') {
        confirmDialog('确定删除该角色吗？此操作不可恢复。', async function () {
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
  }

  var btnNewRole = document.getElementById('btn-new-role');
  if (btnNewRole) btnNewRole.addEventListener('click', function () { openRoleForm(null); });

  (async () => {
    const ok = await requireModule('user');
    if (!ok) return;
    renderUserMenu(document.getElementById('user-slot'));
    load();
  })();
})();
