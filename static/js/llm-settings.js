/* 统一大模型 · 用户自助设置对话框（首页右下角「⋯ → 大模型设置」）
 *
 * 需求：管理员把统一大模型设为「用户各自设置」模式时，用户可在主界面右下角的
 * 三个点按钮里自助填写 API 地址 / 格式 / Key；**管理员未开放时该选项不出现**。
 *
 * 与后端约定（框架路由，见 app.py）：
 *   GET  /api/llm/settings   读模式、格式清单、本人配置（脱敏）与当前生效来源
 *   POST /api/llm/settings   保存本人配置（仅 user 模式开放，否则 403）
 *   POST /api/llm/test       连通性测试（测表单里这套，缺省测已保存的）
 *
 * 实现要点：
 *   1. 入口是**动态注入**的：/api/llm/settings 返回 can_self_config=false 时什么都不做，
 *      因此"管理员未开启 → 看不到该选项"由数据驱动，不需要后端渲染开关；
 *   2. 对话框是常驻 DOM（hidden 切换），不是每次新建——避免重复绑定事件；
 *      结构为「遮罩 + 居中卡片（头部固定 / 主体滚动 / 底部固定）」，
 *      背景调暗 + 模糊；滚动条放在内层主体，避免压掉卡片右侧圆角（见 style.css 注释）；
 *   3. 打开时给 body 加 llm-card-open 类：锁页面滚动 + 收起悬浮球菜单（否则菜单从遮罩下透出）；
 *   4. 兼容 Chrome 72：不用可选链 / 空值合并，显式判空（与 main.js 同口径）。
 */
(function (global) {
  'use strict';

  var CARD_ID = 'llm-card';
  var loaded = false;        // 是否已成功拉到设置
  var formats = [];          // 格式清单（后端给：id/label/url_hint/key_required/desc）
  var card = null;           // 遮罩根元素（懒创建；卡片在它内部）

  function A() { return global.AdminCommon; }
  function el(id) { return document.getElementById(id); }

  function formatMeta(id) {
    for (var i = 0; i < formats.length; i++) {
      if (formats[i].id === id) return formats[i];
    }
    return formats[0] || {};
  }

  // ===================== 入口注入 =====================

  function ensureEntry() {
    var pop = el('fab-pop');
    if (!pop || el('fab-act-llm')) return;      // 无悬浮球菜单的页面（工具外壳页）不注入
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'fab-pop-item';
    btn.id = 'fab-act-llm';
    btn.setAttribute('data-action', 'llm');
    btn.innerHTML = '<span data-jz-icon="💡"></span> 大模型设置';
    pop.appendChild(btn);
    if (global.JZIcon && global.JZIcon.processDom) global.JZIcon.processDom(btn);
    btn.addEventListener('click', function () { openCard(); });
  }

  // ===================== 卡片 =====================

  function cardHtml() {
    return ''
      + '<div class="llm-card" role="dialog" aria-modal="true" aria-label="大模型设置">'
      + '  <div class="llm-card-head">'
      + '    <span class="llm-card-title">大模型设置</span>'
      + '    <button type="button" class="llm-card-close" id="llm-f-close" aria-label="关闭">✕</button>'
      + '  </div>'
      + '  <div class="llm-card-body">'
      + '    <p class="llm-card-desc">本机全部插件的大模型功能共用这里填写的接入信息，'
      + '由你自行配置，不影响其他同事。</p>'
      + '    <label class="llm-field"><span>调用格式</span>'
      + '      <select class="admin-input" id="llm-f-format"></select></label>'
      + '    <div class="llm-hint" id="llm-f-desc"></div>'
      + '    <label class="llm-field"><span>API 地址（完整接口地址）</span>'
      + '      <input class="admin-input" type="text" id="llm-f-url" spellcheck="false"></label>'
      + '    <label class="llm-field"><span>API Key <em id="llm-f-keystate"></em></span>'
      + '      <input class="admin-input" type="password" id="llm-f-key" autocomplete="new-password"></label>'
      + '    <label class="llm-field"><span>模型名称</span>'
      + '      <input class="admin-input" type="text" id="llm-f-model" spellcheck="false"'
      + '             placeholder="如 deepseek-v4-flash / gpt-4o-mini"></label>'
      + '    <div class="llm-adv" id="llm-f-adv" hidden>'
      + '      <label class="llm-field"><span>额外请求头（JSON 对象）</span>'
      + '        <input class="admin-input" type="text" id="llm-f-headers" spellcheck="false"'
      + '               placeholder=\'{"Authorization": "Bearer {{api_key}}"}\'></label>'
      + '      <label class="llm-field"><span>请求体模板（JSON，占位符写在引号内）</span>'
      + '        <textarea class="admin-input" id="llm-f-body" rows="3" spellcheck="false"'
      + '               placeholder=\'{"model": "{{model}}", "messages": {{messages_json}}}\'></textarea></label>'
      + '      <label class="llm-field"><span>正文取值路径</span>'
      + '        <input class="admin-input" type="text" id="llm-f-textpath" spellcheck="false"'
      + '               placeholder="choices.0.message.content"></label>'
      + '      <label class="llm-field"><span>错误信息取值路径（可留空）</span>'
      + '        <input class="admin-input" type="text" id="llm-f-errorpath" spellcheck="false"'
      + '               placeholder="error.message"></label>'
      + '    </div>'
      + '  </div>'
      + '  <div class="llm-card-foot">'
      + '    <div class="llm-card-status" id="llm-f-status"></div>'
      + '    <div class="llm-card-actions">'
      + '      <button type="button" class="admin-btn primary" id="llm-f-save">保存</button>'
      + '      <button type="button" class="admin-btn" id="llm-f-test">测试连通</button>'
      + '    </div>'
      + '  </div>'
      + '</div>';
  }

  function ensureCard() {
    if (card) return card;
    card = document.createElement('div');
    card.className = 'llm-overlay';
    card.id = CARD_ID;
    card.hidden = true;
    card.innerHTML = cardHtml();
    document.body.appendChild(card);

    el('llm-f-close').addEventListener('click', closeCard);
    el('llm-f-format').addEventListener('change', syncFormatUI);
    el('llm-f-save').addEventListener('click', save);
    el('llm-f-test').addEventListener('click', test);
    // 点遮罩（卡片外面）/ 按 Esc 关闭（与框架浮窗同一套交互习惯）
    document.addEventListener('click', function (e) {
      if (card.hidden) return;
      if (e.target === card) closeCard();
    });
    document.addEventListener('keydown', function (e) {
      if (!card.hidden && e.key === 'Escape') closeCard();
    });
    return card;
  }

  function syncFormatUI() {
    var meta = formatMeta(el('llm-f-format').value);
    el('llm-f-desc').textContent = meta.desc || '';
    el('llm-f-url').placeholder = meta.url_hint || '';
    el('llm-f-keystate').textContent = meta.key_required ? '（本格式必填）' : '（本格式可留空）';
    el('llm-f-adv').hidden = meta.id !== 'custom';
  }

  function renderStatus(data) {
    var mine = data.mine || {};
    var eff = data.effective || {};
    var html = '';
    if (mine.configured) {
      html += '<div class="ok">本人配置可用，保存后立即对所有插件生效</div>';
    } else if (eff.source) {
      // 没填个人配置、但系统有可用配置（回退到管理员配置）——不是错误，别用红字吓人
      html += '<div>尚未填写个人配置，当前使用「' + A().esc(eff.source_label || eff.source)
            + '」，不影响使用。</div>';
    } else {
      html += '<div class="bad">' + A().esc(mine.problem || '尚未填写完整')
            + '，填写后才能使用大模型功能</div>';
    }
    el('llm-f-status').innerHTML = html;
  }

  function fillForm(data) {
    formats = data.formats || [];
    var opts = [];
    for (var i = 0; i < formats.length; i++) {
      opts.push('<option value="' + A().esc(formats[i].id) + '">'
                + A().esc(formats[i].label) + '</option>');
    }
    el('llm-f-format').innerHTML = opts.join('');
    var mine = data.mine || {};
    el('llm-f-format').value = mine.format || 'openai';
    el('llm-f-url').value = mine.url || '';
    el('llm-f-key').value = '';
    el('llm-f-key').placeholder = mine.api_key_set
      ? '已保存（••••••••），留空表示不修改' : '请填写 API Key';
    el('llm-f-model').value = mine.model || '';
    syncFormatUI();
    renderStatus(data);
  }

  /* 收集表单 → 请求体；数值留空传 null（"用服务默认"），不要传 0 或空串 */
  function collectProvider() {
    var headersRaw = el('llm-f-headers').value.trim();
    var headers = {};
    if (headersRaw) {
      try {
        headers = JSON.parse(headersRaw);
      } catch (e) {
        throw new Error('额外请求头不是合法 JSON：' + e.message);
      }
      if (typeof headers !== 'object' || headers === null || headers.length !== undefined) {
        throw new Error('额外请求头必须是 JSON 对象');
      }
    }
    return {
      format: el('llm-f-format').value,
      url: el('llm-f-url').value.trim(),
      api_key: el('llm-f-key').value.trim(),
      model: el('llm-f-model').value.trim(),
      headers: headers,
      body: el('llm-f-body').value.trim(),
      text_path: el('llm-f-textpath').value.trim(),
      error_path: el('llm-f-errorpath').value.trim(),
    };
  }

  function reload() {
    return A().api('/api/llm/settings').then(function (data) {
      fillForm(data);
      return data;
    });
  }

  function openCard() {
    var c = ensureCard();
    c.hidden = false;
    document.body.classList.add('llm-card-open');
    reload().catch(function (err) {
      el('llm-f-status').innerHTML = '<span class="bad">读取失败：'
        + A().esc(err.message) + '</span>';
    });
  }

  function closeCard() {
    if (card) card.hidden = true;
    document.body.classList.remove('llm-card-open');
  }

  // ===================== 保存 / 测试 =====================

  function save() {
    var provider;
    try {
      provider = collectProvider();
    } catch (err) {
      A().showToast(err.message, true);
      return;
    }
    var btn = el('llm-f-save');
    btn.disabled = true;
    A().api('/api/llm/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(provider),
    }).then(function (data) {
      A().showToast(data.configured ? '已保存，大模型功能可用'
                                    : '已保存（' + (data.problem || '配置未完成') + '）',
                    !data.configured);
      return reload();
    }).catch(function (err) {
      A().showToast('保存失败：' + err.message, true);
    }).then(function () {
      btn.disabled = false;
    });
  }

  function test() {
    var provider;
    try {
      provider = collectProvider();
    } catch (err) {
      A().showToast(err.message, true);
      return;
    }
    var btn = el('llm-f-test');
    btn.disabled = true;
    btn.textContent = '测试中…';
    A().api('/api/llm/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(provider),
    }).then(function (data) {
      A().showToast(data.detail || (data.ok ? '连通正常' : '连通失败'), !data.ok);
    }).catch(function (err) {
      A().showToast('测试失败：' + err.message, true);
    }).then(function () {
      btn.disabled = false;
      btn.textContent = '测试连通';
    });
  }

  // ===================== 启动 =====================

  function init() {
    if (loaded) return;
    loaded = true;
    A().api('/api/llm/settings').then(function (data) {
      // 管理员未开启用户单独设置 → 不注入入口（需求：该选项不显示）
      if (data && data.can_self_config) ensureEntry();
    }).catch(function () { /* 未登录 / 读取失败：静默不显示入口 */ });
  }

  global.JZLLM = { init: init, open: openCard, close: closeCard };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(window);
