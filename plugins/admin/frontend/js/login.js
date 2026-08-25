/* 登录页逻辑：提交 /api/login，成功后跳转到 next 或 /admin */
(function () {
  const form = document.getElementById('login-form');
  const errEl = document.getElementById('login-error');
  const btn = document.getElementById('login-btn');

  const next = new URLSearchParams(location.search).get('next') || '/admin';

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errEl.textContent = '';
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    if (!username || !password) {
      errEl.textContent = '请输入用户名和密码';
      return;
    }
    btn.disabled = true;
    btn.textContent = '登 录中…';
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      // 仅允许跳转到站内路径，避免开放重定向
      window.location.href = (next.startsWith('/') && !next.startsWith('//')) ? next : '/admin';
    } catch (err) {
      errEl.textContent = err.message;
      btn.disabled = false;
      btn.textContent = '登 录';
    }
  });
})();
