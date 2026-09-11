/* 管理后台公共逻辑：会话校验、用户菜单、通用 API 封装、浮窗/提示 */
(function () {
  const SESSION = Symbol('session');

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // 统一的 JSON 请求封装：失败抛错，401/403 自动跳登录
  async function api(url, options) {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      window.location.href = '/login?next=' + encodeURIComponent(location.pathname);
      throw new Error(data.error || '未登录');
    }
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }

  // 轻提示（复用主界面 toast 样式，admin 页自行内联）
  let toastEl = null;
  let toastTimer = null;
  function ensureToast() {
    if (toastEl) return toastEl;
    toastEl = document.createElement('div');
    toastEl.id = 'toast';
    toastEl.className = 'toast';
    document.body.appendChild(toastEl);
    return toastEl;
  }

  function showToast(msg, isError) {
    const t = ensureToast();
    t.textContent = msg;
    t.classList.toggle('error', !!isError);
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 2000);
  }

  // 获取当前登录状态（带缓存，供页面多处使用）
  async function getSession(force) {
    if (window[SESSION] && !force) return window[SESSION];
    const data = await api('/api/session');
    window[SESSION] = data.user || null;
    return window[SESSION];
  }

  // 加载站点名称到 logo 标题（.logo-title，来自 /api/tools 的 site.title）
  async function loadSiteTitle() {
    try {
      const res = await fetch('/api/tools');
      const data = await res.json().catch(() => ({}));
      const siteName = (data.site && data.site.title) || '';
      if (!siteName) return;
      const els = document.querySelectorAll('.logo-title');
      els.forEach(el => {
        // 保留原有「 · 管理后台」等后缀，仅替换站点名前缀
        const suffix = el.dataset.suffix || '';
        el.textContent = siteName + suffix;
      });
    } catch (e) { /* 忽略 */ }
  }

  // 渲染顶部用户菜单（由各 admin 页面调用，注入到 #user-slot）
  function renderUserMenu(container) {
    getSession().then(user => {
      if (!user) {
        container.innerHTML = '<a class="admin-btn primary" href="/login?next=' +
          encodeURIComponent(location.pathname) + '">登录</a>';
        return;
      }
      container.innerHTML = `
        <div class="user-chip"><span class="avatar">${esc(user.name.slice(0, 1))}</span>${esc(user.name)}</div>
        ${(user.modules && user.modules.length) ? '<a class="admin-btn small" href="/admin">管理后台</a>' : ''}
        <button type="button" class="admin-btn small" id="btn-chg-pwd">修改密码</button>
        <button type="button" class="admin-btn small" id="btn-logout">退出登录</button>`;
      const btn = container.querySelector('#btn-logout');
      btn.addEventListener('click', async () => {
        try {
          await api('/api/logout', { method: 'POST' });
          window[SESSION] = null;
        } catch (e) { /* 已跳转登录 */ }
        window.location.href = '/';
      });
      container.querySelector('#btn-chg-pwd').addEventListener('click', openChangePasswordModal);
    });

    // 会话心跳：每 60 秒确认一次登录状态，过期则整页跳登录（R1 前端侧）
    if (!window.__sessionPoller) {
      window.__sessionPoller = setInterval(async () => {
        try {
          const res = await fetch('/api/session', { credentials: 'same-origin' });
          const data = await res.json().catch(() => ({}));
          if (!data.user) {
            clearInterval(window.__sessionPoller);
            alert('登录已过期，请重新登录');
            window.location.href = '/login?next=' + encodeURIComponent(location.pathname);
          }
        } catch (e) { /* 网络抖动忽略，等下一轮 */ }
      }, 60000);
    }
  }

  // 修改密码：旧密码 + 新密码（≥6 位，不得与旧密码相同），成功后保持当前会话
  function openChangePasswordModal() {
    const wrap = openModal('修改密码', `
      <div class="form-grid">
        <div class="field">
          <label for="pwd-old">原密码</label>
          <input class="admin-input" type="password" id="pwd-old" autocomplete="current-password">
        </div>
        <div class="field">
          <label for="pwd-new">新密码（至少 6 位）</label>
          <input class="admin-input" type="password" id="pwd-new" minlength="6" autocomplete="new-password">
        </div>
        <div class="field">
          <label for="pwd-confirm">确认新密码</label>
          <input class="admin-input" type="password" id="pwd-confirm" minlength="6" autocomplete="new-password">
        </div>
        <div class="field" id="pwd-error" style="font-size:13px;color:var(--md-error);min-height:18px;"></div>
        <div class="modal-foot">
          <button class="admin-btn" type="button" data-close>取消</button>
          <button class="admin-btn primary" type="button" id="pwd-save">保存</button>
        </div>
      </div>`, (w) => {
      const errEl = w.querySelector('#pwd-error');
      const oldEl = w.querySelector('#pwd-old');
      const newEl = w.querySelector('#pwd-new');
      const confirmEl = w.querySelector('#pwd-confirm');
      const saveBtn = w.querySelector('#pwd-save');
      const submit = async () => {
        errEl.textContent = '';
        const oldPwd = oldEl.value;
        const newPwd = newEl.value;
        if (newPwd !== confirmEl.value) { errEl.textContent = '两次输入的新密码不一致'; return; }
        saveBtn.disabled = true;
        try {
          await api('/api/account/password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
          });
          w.querySelector('[data-close]').click();
          showToast('密码已修改');
        } catch (err) {
          errEl.textContent = err.message;
          saveBtn.disabled = false;
        }
      };
      saveBtn.addEventListener('click', submit);
      newEl.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
      oldEl.focus();
    });
    return wrap;
  }

  // 管理页鉴权：未登录跳登录，无权限跳 /admin
  function requireModule(module) {
    return getSession().then(user => {
      if (!user) {
        window.location.href = '/login?next=' + encodeURIComponent(location.pathname);
        return false;
      }
      if (module && !(user.modules || []).includes(module)) {
        showToast('无此模块的操作权限', true);
        setTimeout(() => { window.location.href = '/admin'; }, 800);
        return false;
      }
      return true;
    });
  }

  // 通用浮窗（打开 / 关闭），返回 panel 容器
  function openModal(title, bodyHtml, onOpen) {
    const wrap = document.createElement('div');
    wrap.className = 'modal';
    wrap.innerHTML = `
      <div class="modal-panel">
        <div class="modal-head">
          <span class="modal-title"></span>
          <button type="button" class="modal-close" data-close aria-label="关闭">✕</button>
        </div>
        <div class="modal-body"></div>
      </div>`;
    wrap.querySelector('.modal-title').textContent = title;
    wrap.querySelector('.modal-body').innerHTML = bodyHtml;
    document.body.appendChild(wrap);
    document.body.classList.add('modal-open');
    const close = () => {
      wrap.remove();
      document.body.classList.remove('modal-open');
    };
    wrap.addEventListener('click', (e) => {
      if (e.target === wrap || e.target.closest('[data-close]')) close();
    });
    document.addEventListener('keydown', function onKey(e) {
      if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); }
    });
    if (typeof onOpen === 'function') onOpen(wrap);
    return wrap;
  }

  // 确认对话框
  function confirmDialog(message, onConfirm) {
    const wrap = openModal('确认操作', `
      <div style="font-size:14px;line-height:1.6;">${esc(message)}</div>
      <div class="modal-foot">
        <button type="button" class="admin-btn" data-close>取消</button>
        <button type="button" class="admin-btn danger" id="confirm-ok">确定</button>
      </div>`);
    wrap.querySelector('#confirm-ok').addEventListener('click', () => {
      wrap.querySelector('[data-close]').click();
      onConfirm();
    });
  }

  /* ==================== 批量导入（单位 / 部门 / 人员通用） ====================
   * 两步式：选择文件（模板下载 / 导出入口）→ 预览校验（逐行结果着色）→ 确认导入。
   * opts: { module: 'unit'|'department'|'user', label, allowAutoParent, onDone }
   */
  const BATCH_ACTIONS = {
    create: ['ok', '新增'],
    update: ['warn', '更新'],
    skip: ['mute', '跳过'],
    error: ['bad', '错误'],
  };

  function batchImport(opts) {
    const base = '/api/admin/batch/' + encodeURIComponent(opts.module);
    const label = opts.label || '数据';
    const state = { file: null, preview: null, result: null };

    const pickerHtml = () => `
      <div class="batch-tip">
        支持 <b>.xlsx</b> / <b>.csv</b>，单次最多 3000 行。首次使用请先下载模板，按模板表头填写
        （表头行请勿改动，示例行可删除）。
      </div>
      <div class="batch-links">
        <a class="admin-btn small" href="${base}/template?format=xlsx">下载 xlsx 模板</a>
        <a class="admin-btn small" href="${base}/template?format=csv">下载 csv 模板</a>
        <a class="admin-btn small" href="${base}/export?format=xlsx">导出当前${esc(label)}</a>
      </div>
      <div class="batch-drop" id="bi-drop">
        <input type="file" id="bi-file" accept=".xlsx,.xlsm,.csv" hidden>
        <div class="batch-drop-title">点击选择文件，或将文件拖拽到此处</div>
        <div class="batch-file" id="bi-filename">尚未选择文件</div>
      </div>
      <div class="form-grid">
        <div class="field">
          <label for="bi-mode">导入方式</label>
          <select class="admin-input" id="bi-mode">
            <option value="upsert">存在则更新，不存在则新增（推荐）</option>
            <option value="insert">仅新增（已存在则跳过）</option>
            <option value="update">仅更新（不存在则跳过）</option>
          </select>
        </div>
        ${opts.allowAutoParent ? `<div class="field">
          <label class="batch-check"><input type="checkbox" id="bi-parent"> 上级单位／部门不存在时自动创建</label>
        </div>` : ''}
        <div class="field">
          <label>遇到错误行时</label>
          <div class="module-check" id="bi-onerror">
            <label><input type="radio" name="bi-onerror" value="abort" checked> 全部回滚（存在错误行则不导入任何数据）</label>
            <label><input type="radio" name="bi-onerror" value="skip"> 跳过错误行，其余照常导入</label>
          </div>
        </div>
        <div class="field"><div class="batch-error" id="bi-error"></div></div>
      </div>
      <div class="modal-foot">
        <button class="admin-btn" type="button" data-close>取消</button>
        <button class="admin-btn primary" type="button" id="bi-preview">预览校验</button>
      </div>`;

    const summaryHtml = (s) => `
      <div class="batch-summary">
        <span class="batch-chip mute">共 ${s.total} 行</span>
        <span class="batch-chip ok">新增 ${s.create}</span>
        <span class="batch-chip warn">更新 ${s.update}</span>
        <span class="batch-chip mute">跳过 ${s.skip}</span>
        <span class="batch-chip bad">错误 ${s.error}</span>
      </div>`;

    const previewHtml = (p) => {
      const rows = p.rows.map(r => {
        const [cls, txt] = BATCH_ACTIONS[r.action] || ['mute', r.action];
        return `<tr>
          <td class="batch-row-no">${r.row}</td>
          <td><span class="batch-chip ${cls}">${txt}</span></td>
          <td>${esc(r.label)}</td>
          <td class="batch-msg">${esc(r.message || '')}</td>
        </tr>`;
      }).join('');
      const ignored = (p.ignored_columns && p.ignored_columns.length)
        ? `<div class="batch-tip">已忽略未识别的列：${esc(p.ignored_columns.join('、'))}</div>` : '';
      // 解析来源与格式层提示（工作表 / 表头行 / 合并单元格 / 公式 / 精度等）
      const where = (p.sheet ? `工作表 <b>${esc(p.sheet)}</b>` : '')
        + (p.header_row > 1 ? `　表头在第 <b>${p.header_row}</b> 行` : '');
      const notes = (p.notes && p.notes.length)
        ? `<div class="batch-tip">${p.notes.map(esc).join('；')}</div>` : '';
      // blocked：后端判定「存在错误行 + 当前为整批回滚」——此时不允许确认导入
      const blocked = !!p.blocked;
      const noChange = (p.summary.create + p.summary.update) === 0;
      const warn = blocked
        ? `<div class="batch-tip batch-warn">按当前设置「遇到错误行：全部回滚」，存在错误行时<b>不会写入任何数据</b>。
             请修正文件后重新导入，或点「返回修改」改选「跳过错误行」。</div>`
        : (noChange && !p.summary.error
          ? '<div class="batch-tip">没有需要新增或更新的数据（内容与现状一致）。</div>' : '');
      return `
        <div class="batch-tip">导入方式：<b>${esc(p.mode_name || '')}</b>　文件：<b>${esc(state.file ? state.file.name : '')}</b>${where ? '　' + where : ''}</div>
        ${summaryHtml(p.summary)}
        ${ignored}
        ${notes}
        ${warn}
        <div class="batch-table-wrap">
          <table class="data-table batch-table">
            <thead><tr><th>行号</th><th>结果</th><th>对象</th><th>说明</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <div class="modal-foot">
          <button class="admin-btn" type="button" id="bi-back">返回修改</button>
          <button class="admin-btn primary" type="button" id="bi-commit"
                  ${(blocked || noChange) ? 'disabled' : ''}>
            确认导入（新增 ${p.summary.create} · 更新 ${p.summary.update}）</button>
        </div>`;
    };

    const doneHtml = (p) => `
      <div class="batch-done">
        <div class="batch-done-title">导入完成</div>
        ${summaryHtml(p.summary)}
        <div class="batch-tip">数据已写入，列表将在关闭后自动刷新。</div>
      </div>
      <div class="modal-foot">
        <button class="admin-btn primary" type="button" id="bi-finish">完成</button>
      </div>`;

    function submit(payload) {
      const fd = new FormData();
      fd.append('file', state.file);
      Object.keys(payload).forEach(k => fd.append(k, String(payload[k])));
      return api(base + '/import', { method: 'POST', body: fd });
    }

    return openModal(opts.title || ('批量导入' + label), '<div id="bi-root"></div>', (wrap) => {
      const root = wrap.querySelector('#bi-root');

      function stepPicker() {
        root.innerHTML = pickerHtml();
        const drop = root.querySelector('#bi-drop');
        const input = root.querySelector('#bi-file');
        const nameEl = root.querySelector('#bi-filename');
        const errEl = root.querySelector('#bi-error');
        const showFile = () => {
          nameEl.textContent = state.file
            ? state.file.name + '（' + Math.max(1, Math.round(state.file.size / 1024)) + ' KB）'
            : '尚未选择文件';
          nameEl.classList.toggle('picked', !!state.file);
          errEl.textContent = '';
        };
        const pick = (f) => {
          if (!f) return;
          if (!/\.(xlsx|xlsm|csv)$/i.test(f.name)) {
            errEl.textContent = '仅支持 .xlsx / .csv 文件（旧版 .xls 请先另存为 .xlsx）';
            return;
          }
          state.file = f;
          showFile();
        };
        // 注意：input 在 drop 内部，input.click() 触发的 click 会冒泡回 drop，
        // 必须判断事件源，否则会无限递归。
        drop.addEventListener('click', (e) => {
          if (e.target === input) return;
          input.click();
        });
        input.addEventListener('change', () => pick(input.files[0]));
        ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, (e) => {
          e.preventDefault(); drop.classList.add('over');
        }));
        ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, (e) => {
          e.preventDefault(); drop.classList.remove('over');
        }));
        drop.addEventListener('drop', (e) => pick(e.dataTransfer.files[0]));
        showFile();

        root.querySelector('#bi-preview').addEventListener('click', async (e) => {
          if (!state.file) { errEl.textContent = '请先选择要导入的文件'; return; }
          const mode = root.querySelector('#bi-mode').value;
          const autoParent = root.querySelector('#bi-parent');
          const onError = root.querySelector('#bi-onerror input:checked').value;
          const btn = e.currentTarget;
          btn.disabled = true;
          btn.textContent = '校验中…';
          // 记录本次选项，确认导入时原样复用，保证「预览所见 = 执行所得」
          state.opts = {
            mode,
            on_error: onError,
            auto_create_parent: (autoParent && autoParent.checked) ? 1 : 0,
          };
          try {
            const data = await submit(Object.assign({ dry_run: 1 }, state.opts));
            state.preview = data;
            stepPreview();
          } catch (err) {
            errEl.textContent = err.message;
            btn.disabled = false;
            btn.textContent = '预览校验';
          }
        });
      }

      function stepPreview() {
        const p = state.preview;
        root.innerHTML = previewHtml(p);
        root.querySelector('#bi-back').addEventListener('click', stepPicker);
        const commit = root.querySelector('#bi-commit');
        if (!commit || commit.disabled) return;
        commit.addEventListener('click', async () => {
          commit.disabled = true;
          commit.textContent = '导入中…';
          try {
            const data = await submit(Object.assign({ dry_run: 0 }, state.opts || {}));
            state.result = data;
            stepDone();
          } catch (err) {
            showToast('导入失败：' + err.message, true);
            commit.disabled = false;
            commit.textContent = '确认导入';
          }
        });
      }

      function stepDone() {
        const p = state.result || state.preview;
        root.innerHTML = doneHtml(p);
        root.querySelector('#bi-finish').addEventListener('click', () => {
          wrap.querySelector('[data-close]').click();
          showToast(`导入完成：新增 ${p.summary.create} · 更新 ${p.summary.update}`
            + (p.summary.skip ? ` · 跳过 ${p.summary.skip}` : '')
            + (p.summary.error ? ` · 错误 ${p.summary.error}` : ''));
          if (typeof opts.onDone === 'function') opts.onDone();
        });
      }

      stepPicker();
    });
  }

  /* ==================== 文件下载（模板 / 导出） ====================
   * 用 fetch + Blob 下载，可正确处理 401（跳登录）与后端 JSON 错误提示，
   * 并从 Content-Disposition 解析中文文件名。
   */
  async function download(url) {
    const res = await fetch(url, { credentials: 'same-origin' });
    if (res.status === 401) {
      window.location.href = '/login?next=' + encodeURIComponent(location.pathname);
      return;
    }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + res.status));
    }
    const blob = await res.blob();
    const disp = res.headers.get('Content-Disposition') || '';
    let name = '';
    const star = /filename\*=UTF-8''([^;]+)/i.exec(disp);
    const plain = /filename="?([^";]+)"?/i.exec(disp);
    if (star) { try { name = decodeURIComponent(star[1]); } catch (e) { name = star[1]; } }
    else if (plain) name = plain[1];
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name || 'download';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }

  window.AdminCommon = {
    esc,
    api,
    download,
    showToast,
    getSession,
    renderUserMenu,
    loadSiteTitle,
    requireModule,
    openModal,
    confirmDialog,
    batchImport,
  };

  // 自动把 logo 标题替换为 config/tools.json 配置的站点名（保留「 · 管理后台」等后缀）
  loadSiteTitle();
})();
