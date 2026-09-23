/* 统一大模型设置页（管理员 / 超级管理员）
 *
 * 与后端约定（plugins/admin/backend/routes.py）：
 *   GET  /api/admin/llm-settings   读全局设置（API Key 只回掩码）
 *   POST /api/admin/llm-settings   存全局设置（**按字段局部更新**：只改传进来的字段）
 *   POST /api/admin/llm-test       连通性测试（测表单里这套，缺省测已保存的）
 *
 * 两张卡各存各的（后端局部更新保证互不覆盖）：
 *   「调用模式」卡 —— 改模式 / 改回退开关即**自动保存**（saveMode），页面无保存按钮；
 *   「全局接入配置」卡 —— 点「保存」只提交 provider，**不影响调用模式**。
 *
 * 配置真源是框架模块 jz_llm 的 <数据根>/config/llm.json —— 本页只是它的界面，
 * 因此这里不缓存任何配置，保存后重新拉取以服务端规整结果为准。
 */
(function () {
  const { api, showToast, getSession, renderUserMenu, esc } = window.AdminCommon;

  const MODE_LABEL = {
    admin: '管理员统一配置',
    user: '用户各自设置',
  };

  const MODE_TIP = {
    admin: '所有插件的大模型调用都使用下面这套全局配置，用户端不显示自助设置入口。'
         + '适合内网统一网关 / 统一计费的场景。',
    user: '每位用户可在首页右下角「⋯ → 大模型设置」中填写自己的地址、格式与 Key，'
        + '互不影响。适合各人自备 Key 的场景。',
  };

  let formats = [];   // 后端给的格式清单（id / label / url_hint / key_required / desc）

  function el(id) { return document.getElementById(id); }

  function currentFormat() {
    return formats.find(f => f.id === el('llm-format').value) || formats[0] || {};
  }

  function syncFormatUI() {
    const f = currentFormat();
    el('format-desc').textContent = f.desc || '';
    el('llm-url').placeholder = f.url_hint || '';
    el('key-state').textContent = f.key_required ? '（本格式必填）' : '（本格式可留空）';
    el('llm-adv').hidden = f.id !== 'custom';
  }

  function renderStatus(data) {
    const p = data.provider || {};
    const keyText = p.api_key ? '已配置' : '未配置';
    const okClass = p.configured ? 'ok' : 'bad';
    const modeName = data.mode === 'user' ? '用户各自设置' : '管理员统一配置';
    const rows = [
      `<div>当前模式：<b>${esc(modeName)}</b></div>`,
      `<div>全局配置：<span class="${okClass}"><b>${p.configured ? '可用' : '未完成'}</b></span>`
        + `${p.configured ? '' : '　' + esc(p.problem || '请填写 API 地址' + (p.api_key ? '' : ' 与 API Key'))}</div>`,
      `<div>API 地址：<code class="llm-mono">${esc(p.url || '（未填写）')}</code></div>`,
      `<div>调用格式：<b>${esc((formats.find(f => f.id === p.format) || {}).label || p.format || '-')}</b>`
        + `　模型：<b>${esc(p.model || '（未填写）')}</b>　API Key：<b>${keyText}</b></div>`,
    ];
    if (!data.requests_available) {
      rows.push('<div class="bad">缺少 requests 依赖：大模型调用不可用，请安装依赖组件包 '
        + 'JZToolsHub-依赖-requests-v*.zip 后刷新页面。</div>');
    }
    if (data.mode === 'user') {
      rows.push(`<div>已有账号数：<b>${data.user_count || 0}</b>（用户也可在首页「⋯」中自行填写）</div>`);
    }
    el('llm-status').innerHTML = rows.join('');
  }

  function fillForm(data) {
    formats = data.formats || [];
    el('llm-format').innerHTML = formats
      .map(f => `<option value="${esc(f.id)}">${esc(f.label)}</option>`).join('');
    const p = data.provider || {};
    el('llm-format').value = p.format || 'openai';
    el('llm-url').value = p.url || '';
    el('llm-key').value = '';
    el('llm-key').placeholder = p.api_key
      ? '已保存（••••••••），留空表示不修改' : '请填写 API Key';
    el('llm-model').value = p.model || '';
    el('llm-timeout').value = p.timeout || '';
    el('llm-temp').value = (p.temperature === null || p.temperature === undefined) ? '' : p.temperature;
    el('llm-maxtok').value = p.max_tokens || '';
    el('llm-headers').value = (p.headers && Object.keys(p.headers).length)
      ? JSON.stringify(p.headers) : '';
    el('llm-body').value = p.body || '';
    el('llm-text-path').value = p.text_path || '';
    el('llm-error-path').value = p.error_path || '';
    el('llm-mode').value = data.mode || 'admin';
    el('llm-fallback').checked = data.fallback !== false;
    el('mode-tip').textContent = MODE_TIP[data.mode] || MODE_TIP.admin;
    syncFormatUI();
    renderStatus(data);
  }

  /* 收集「全局接入配置」表单 → 请求体。数值留空传 null（表示"用服务默认"），不要传 0 或空串。
     注意：**不含** mode / fallback —— 那两项由「调用模式」卡自动保存（见 saveMode），
     这样点本卡的保存按钮不会顺带改动模式。 */
  function collectProvider() {
    const headersRaw = el('llm-headers').value.trim();
    let headers = {};
    if (headersRaw) {
      try {
        headers = JSON.parse(headersRaw);
      } catch (e) {
        throw new Error('额外请求头不是合法 JSON：' + e.message);
      }
      if (typeof headers !== 'object' || headers === null || Array.isArray(headers)) {
        throw new Error('额外请求头必须是 JSON 对象，如 {"Authorization": "Bearer {{api_key}}"}');
      }
    }
    const num = (id) => {
      const v = el(id).value.trim();
      return v === '' ? null : Number(v);
    };
    return {
      format: el('llm-format').value,
      url: el('llm-url').value.trim(),
      api_key: el('llm-key').value.trim(),
      model: el('llm-model').value.trim(),
      timeout: num('llm-timeout'),
      temperature: num('llm-temp'),
      max_tokens: num('llm-maxtok'),
      headers: headers,
      body: el('llm-body').value.trim(),
      text_path: el('llm-text-path').value.trim(),
      error_path: el('llm-error-path').value.trim(),
    };
  }

  async function reload() {
    try {
      const data = await api('/api/admin/llm-settings');
      fillForm(data);
    } catch (err) {
      el('llm-status').innerHTML = '<span class="bad">读取失败：' + esc(err.message) + '</span>';
    }
  }

  /* 「全局接入配置」卡的保存按钮：**只提交 provider**，不动调用模式（后端按字段局部更新） */
  async function save() {
    let provider;
    try {
      provider = collectProvider();
    } catch (err) {
      showToast(err.message, true);
      return;
    }
    const btn = el('llm-save');
    btn.disabled = true;
    try {
      const data = await api('/api/admin/llm-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: provider }),
      });
      showToast(data.configured ? '接入配置已保存（全局配置可用）'
                                : '接入配置已保存（' + (data.problem || '配置未完成') + '）');
      await reload();
    } catch (err) {
      showToast('保存失败：' + err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  /* 「调用模式」卡：改模式 / 改回退开关即**自动保存**，不需要点任何按钮。
     只提交 mode + fallback（后端按字段局部更新），不会覆盖正在编辑的接入配置。 */
  let modeSaving = false;
  async function saveMode(what) {
    if (modeSaving) return;                 // 连点两次不并发提交
    modeSaving = true;
    el('llm-mode').disabled = true;
    el('llm-fallback').disabled = true;
    const mode = el('llm-mode').value;
    try {
      await api('/api/admin/llm-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: mode, fallback: el('llm-fallback').checked }),
      });
      showToast('已自动保存：' + (what || MODE_LABEL[mode] || mode));
      await reload();                       // 状态摘要 / 模式提示随之刷新
    } catch (err) {
      showToast('自动保存失败：' + err.message, true);
      await reload();                       // 回显服务端真值，避免界面与配置不一致
    } finally {
      modeSaving = false;
      el('llm-mode').disabled = false;
      el('llm-fallback').disabled = false;
    }
  }

  async function test() {
    let payload;
    try {
      payload = collect().provider;
    } catch (err) {
      showToast(err.message, true);
      return;
    }
    const btn = el('llm-test');
    btn.disabled = true;
    btn.textContent = '测试中…';
    try {
      const data = await api('/api/admin/llm-test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      showToast(data.detail || (data.ok ? '连通正常' : '连通失败'), !data.ok);
    } catch (err) {
      showToast('测试失败：' + err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '测试连通';
    }
  }

  function init() {
    renderUserMenu(el('user-slot'));
    el('llm-format').addEventListener('change', syncFormatUI);
    // 「调用模式」卡：改完即自动保存（无需按钮）；回退开关同卡，同样自动保存。
    // 提示语按"实际改了什么"给，避免只改了回退开关却说"已保存模式"。
    el('llm-mode').addEventListener('change', () => {
      const mode = el('llm-mode').value;
      el('mode-tip').textContent = MODE_TIP[mode] || MODE_TIP.admin;
      saveMode('模式 = ' + (MODE_LABEL[mode] || mode));
    });
    el('llm-fallback').addEventListener('change', () => {
      saveMode('回退策略 = ' + (el('llm-fallback').checked ? '开启' : '关闭'));
    });
    el('llm-save').addEventListener('click', save);
    el('llm-test').addEventListener('click', test);
    getSession().then(user => { if (!user) location.href = '/login?next=/admin/llm'; });
    reload();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
