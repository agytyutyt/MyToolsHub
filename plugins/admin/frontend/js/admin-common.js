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

  window.AdminCommon = {
    esc,
    api,
    showToast,
    getSession,
    renderUserMenu,
    requireModule,
    openModal,
    confirmDialog,
  };
})();
