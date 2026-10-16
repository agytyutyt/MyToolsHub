/* 数据迁移页（仅超级管理员）：导出 / 导入
 *
 * 与后端约定（plugins/admin/backend/routes.py + data_migrate.py）：
 *   GET  /api/admin/migrate/plan     可导出分段盘点（框架三段 + 各插件数据段 + 体积）
 *   POST /api/admin/migrate/export   导出（{passphrase, sections[]} → 直接下载 .jzdata）
 *   POST /api/admin/migrate/inspect  上传包 + 口令 → 预览（不写数据），返回 token
 *   POST /api/admin/migrate/import   {token, sections[]} → 写入数据根（覆盖前备份）
 *
 * 两步导入（解析预览 → 确认导入）之间不重复要口令：口令派生的信封密钥在服务端内存里
 * 活 30 分钟（见 routes.py 的 _MIGRATE_STAGE）；服务重启则该 token 失效，重新解析即可。
 *
 * 桌面端（壳内）适配：壳内浏览器式下载会被静默取消（客户端 ALLOW_DOWNLOADS=False），
 * 导出改为 fetch 到 blob 后交本地能力层 /local-api/fs/* 落盘（同商店页下载的口径）。
 * 壳内与否用"探测即检测"：save-dir 端点 200=壳内；403/404/断网=浏览器（本地端口
 * 永不中继，直连服务器访问时该路径不存在），页面维持纯浏览器行为不变。
 */
(function () {
  const { api, showToast, getSession, esc } = window.AdminCommon;

  let token = '';        // 最近一次解析成功的迁移包 token
  let sections = [];     // 最近一次盘点/预览的分段
  let inShell = false;   // 桌面客户端壳内（探测 /local-api/fs/save-dir 而定）
  let savedPath = '';    // 壳内最近一次落盘的绝对路径（"打开所在文件夹"用）

  function el(id) { return document.getElementById(id); }

  function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return '-';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
    return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  function sectionHtml(sec, checked) {
    const tags = [];
    if (sec.kind === 'config') tags.push('框架配置');
    if (sec.version) tags.push('v' + sec.version);
    if (sec.installed === false) tags.push('插件未安装');
    if (sec.existing) tags.push('将覆盖 ' + sec.existing + ' 个文件');
    if (sec.secrets) tags.push('含 ' + sec.secrets + ' 处密钥');
    return '<label class="mg-sec">'
      + '<input type="checkbox" value="' + esc(sec.id) + '"' + (checked ? ' checked' : '') + '>'
      + '<span><span class="mg-sec-name">' + esc(sec.label || sec.id) + '</span>'
      + tags.map(t => '<span class="mg-sec-tag">' + esc(t) + '</span>').join('')
      + '<div class="mg-sec-meta">' + sec.files + ' 个文件 · ' + fmtSize(sec.bytes)
      + (sec.plugin_id ? ' · <code>plugins/' + esc(sec.plugin_id) + '/</code>' : '') + '</div>'
      + '</span></label>';
  }

  function picked(listId) {
    return [...el(listId).querySelectorAll('input[type=checkbox]')]
      .filter(c => c.checked).map(c => c.value);
  }

  function report(boxId, html) {
    const box = el(boxId);
    box.hidden = false;
    box.innerHTML = html;
  }

  // ===================== 桌面端（壳内）适配 =====================

  async function probeShell() {
    try {
      const res = await fetch('/local-api/fs/save-dir', { credentials: 'same-origin' });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;   // 浏览器直开：无本地端口或无壳 Cookie
    }
  }

  function renderSaveDir(info) {
    el('mg-savedir').hidden = false;
    el('mg-savedir-path').textContent = info.effective_dir || '';
    el('mg-savedir-note').textContent = info.dir
      ? '已由你手动设置，导出的数据包会直接保存到这里。'
      : '默认为客户端下载目录，可点「修改…」换成其他位置。';
  }

  async function setSaveDir(dir) {
    const res = await fetch('/local-api/fs/save-dir', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir: dir }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    renderSaveDir(data);
    showToast(dir ? '已更新保存目录' : '已恢复默认保存目录');
  }

  async function pickSaveDir() {
    try {
      const res = await fetch('/local-api/fs/pick-dir', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seed: el('mg-savedir-path').textContent }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      if (!data.dir) return;   // 用户取消
      try { await setSaveDir(data.dir); }
      catch (err) { showToast(err.message, true); }
    } catch (err) {
      showToast('无法打开目录选择框：' + err.message, true);
    }
  }

  async function saveLocalFile(blob, fname) {
    const fd = new FormData();
    fd.append('filename', fname);
    fd.append('file', blob, fname);
    const res = await fetch('/local-api/fs/save-file', {
      method: 'POST', credentials: 'same-origin', body: fd,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 404) throw new Error('客户端能力不可用（版本过旧），请升级桌面客户端');
      throw new Error(data.error || ('HTTP ' + res.status));
    }
    return data;
  }

  function exportBtnLabel() { return inShell ? '导出并保存' : '导出并下载'; }

  // ===================== 导出 =====================

  async function loadPlan() {
    try {
      const data = await api('/api/admin/migrate/plan');
      sections = data.sections || [];
      el('mg-export-list').innerHTML = sections.length
        ? sections.map(s => sectionHtml(s, true)).join('')
        : '<div class="loading">没有可导出的数据（数据根为空？）</div>';
      el('mg-export-hint').innerHTML = '共 <b>' + (data.totals.files || 0) + '</b> 个文件 · '
        + '<b>' + fmtSize(data.totals.bytes) + '</b>　当前数据目录：<code>' + esc(data.data_root) + '</code>'
        + (data.app_version ? '　程序版本：' + esc(data.app_version) : '');
    } catch (err) {
      el('mg-export-list').innerHTML = '<div class="loading">加载失败：' + esc(err.message) + '</div>';
    }
  }

  async function doExport() {
    const pass = el('mg-pass').value;
    if (pass.length < 8) { showToast('导出口令至少 8 位', true); el('mg-pass').focus(); return; }
    if (pass !== el('mg-pass2').value) { showToast('两次输入的口令不一致', true); el('mg-pass2').focus(); return; }
    const pickedSections = picked('mg-export-list');
    if (!pickedSections.length) { showToast('请至少勾选一个数据段', true); return; }

    const btn = el('mg-export');
    btn.disabled = true;
    btn.textContent = '打包中…';
    try {
      // 导出是"文件下载"，不能走 AdminCommon.api（它按 JSON 解析）；这里直接 fetch 拿 blob
      const res = await fetch('/api/admin/migrate/export', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ passphrase: pass, sections: pickedSections }),
      });
      if (!res.ok) {
        let msg = 'HTTP ' + res.status;
        try { msg = (await res.json()).error || msg; } catch (e) { /* 非 JSON */ }
        throw new Error(msg);
      }
      const blob = await res.blob();
      // 文件名优先取 RFC 5987 的 filename*=UTF-8''…（中文名在这里），退回 ASCII 的 filename=
      const disp = res.headers.get('Content-Disposition') || '';
      const utf8 = disp.match(/filename\*=UTF-8''([^;]+)/i);
      const plain = disp.match(/filename="?([^";]+)"?/i);
      let fname = 'JZToolsHub-数据迁移.jzdata';
      if (utf8) {
        try { fname = decodeURIComponent(utf8[1]); } catch (e) { /* 保持默认名 */ }
      } else if (plain) {
        fname = plain[1];
      }
      const passNote = '<br><b>请牢记导出口令</b>：包内数据（含账号与大模型 Key）只认这一个口令，遗失后无法恢复。';
      if (inShell) {
        const saved = await saveLocalFile(blob, fname);
        savedPath = saved.path;
        report('mg-export-report', '<span class="ok">✓ 已保存</span>：' + esc(saved.path)
          + '　体积 ' + fmtSize(saved.size || blob.size) + '（' + pickedSections.length + ' 个数据段）'
          + '<br><button type="button" class="admin-btn" id="mg-open-dir">打开所在文件夹</button>'
          + passNote);
        showToast('数据包已保存：' + (saved.filename || fname));
      } else {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = fname;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(a.href), 60000);
        report('mg-export-report', '<span class="ok">✓ 已导出</span>：' + esc(fname)
          + '　体积 ' + fmtSize(blob.size) + '（' + pickedSections.length + ' 个数据段）'
          + passNote);
        showToast('已导出数据包');
      }
      el('mg-pass').value = '';
      el('mg-pass2').value = '';
    } catch (err) {
      showToast('导出失败：' + err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = exportBtnLabel();
    }
  }

  // ===================== 导入 =====================

  async function doInspect() {
    const file = el('mg-file').files[0];
    if (!file) { showToast('请先选择 .jzdata 数据包', true); return; }
    const pass = el('mg-import-pass').value;
    if (!pass) { showToast('请输入导出时设置的口令', true); el('mg-import-pass').focus(); return; }

    const btn = el('mg-inspect');
    btn.disabled = true;
    btn.textContent = '解析中…';
    el('mg-import-report').hidden = true;
    try {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('passphrase', pass);
      const res = await fetch('/api/admin/migrate/inspect', {
        method: 'POST', credentials: 'same-origin', body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));

      token = data.token;
      const list = data.sections || [];
      el('mg-preview-meta').innerHTML = '来源：<b>' + esc(data.source || '未知') + '</b>　导出时间：<b>'
        + esc(data.created_at || '未知') + '</b>　程序版本：' + esc(data.app_version || '-')
        + '　共 <b>' + (data.totals.files || 0) + '</b> 个文件 · <b>' + fmtSize(data.totals.bytes) + '</b>';
      el('mg-import-list').innerHTML = list.map(s => sectionHtml(s, true)).join('');
      el('mg-import-preview').hidden = false;
      el('mg-import-hint').innerHTML = '✓ 口令正确，包完整。<b>此时尚未写入任何数据</b>，'
        + '请确认下面要导入的数据段后再点「确认导入」。';
      showToast('解析成功，请确认要导入的数据段');
    } catch (err) {
      token = '';
      el('mg-import-preview').hidden = true;
      el('mg-import-hint').innerHTML = '<span class="bad">✗ ' + esc(err.message) + '</span>';
      showToast('解析失败：' + err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '解析预览';
    }
  }

  async function doImport() {
    if (!token) { showToast('请先解析数据包', true); return; }
    const pickedSections = picked('mg-import-list');
    if (!pickedSections.length) { showToast('请至少勾选一个数据段', true); return; }
    if (!window.confirm('确认导入这 ' + pickedSections.length + ' 个数据段？\n'
        + '被覆盖的文件会先备份到数据根 backups/migrate/ 下。')) return;

    const btn = el('mg-import');
    btn.disabled = true;
    btn.textContent = '导入中…';
    try {
      const data = await api('/api/admin/migrate/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token, sections: pickedSections }),
      });
      token = '';
      const lines = [
        '<span class="ok">✓ 导入完成</span>（' + esc(data.at || '') + '）',
        '写入 <b>' + data.written + '</b> 个文件 · ' + fmtSize(data.bytes)
          + '　跳过 <b>' + data.skipped + '</b> 个（内容一致或未勾选）',
      ];
      if (data.backed_up) {
        lines.push('覆盖前已备份 <b>' + data.backed_up + '</b> 个文件 → <code>' + esc(data.backup_dir) + '</code>');
      }
      if (data.plugins && data.plugins.length) {
        lines.push('涉及插件：<b>' + data.plugins.map(esc).join('、') + '</b>');
      }
      if (data.accounts_written) {
        lines.push('<span class="bad">账号与权限数据已替换</span>：若当前登录账号不在包内，'
          + '后续操作需要重新登录；权限变化也可能影响可见工具。');
      }
      if (pickedSections.indexOf('tools') >= 0) {
        lines.push('提示：工具启停 / 排序的变更需<b>重启服务</b>后完全生效。');
      }
      report('mg-import-report', lines.join('<br>'));
      el('mg-import-preview').hidden = true;
      el('mg-file').value = '';
      el('mg-import-pass').value = '';
      showToast('导入完成：写入 ' + data.written + ' 个文件');
    } catch (err) {
      showToast('导入失败：' + err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '确认导入';
    }
  }

  function init() {
    el('mg-export').addEventListener('click', doExport);
    el('mg-reload').addEventListener('click', loadPlan);
    el('mg-inspect').addEventListener('click', doInspect);
    el('mg-import').addEventListener('click', doImport);
    el('mg-savedir-pick').addEventListener('click', pickSaveDir);
    el('mg-savedir-reset').addEventListener('click', () => {
      setSaveDir(null).catch(err => showToast(err.message, true));
    });
    // 报告区是 innerHTML 动态生成的，「打开所在文件夹」用事件委托接住
    el('mg-export-report').addEventListener('click', async (ev) => {
      if (!ev.target || ev.target.id !== 'mg-open-dir' || !savedPath) return;
      try {
        const res = await fetch('/local-api/fs/reveal', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: savedPath }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
      } catch (err) {
        showToast('打开文件夹失败：' + err.message, true);
      }
    });
    getSession().then(user => { if (!user) location.href = '/login?next=/admin/migrate'; });
    loadPlan();
    probeShell().then(info => {
      if (!info) return;
      inShell = true;
      renderSaveDir(info);
      el('mg-export').textContent = exportBtnLabel();
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
