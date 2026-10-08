# -*- coding: utf-8 -*-
"""商店下载状态机补丁（2026-10-08）：校验端点 + 按钮进度条填充 + 依赖安装/已安装态。

- capability_app：GET /local-api/store/downloaded?id&version（catalog 文件名在 downloads 下
  存在且大小一致 → 已下载）
- store.js：下载按钮状态机 校验中(转圈) → 下载中 NN%（填充） → 已完成 → 已下载；
  依赖条目安装按钮补「已安装」禁用态
- style.css：.st-btn-dl 进度填充/spinner/已完成/已下载 态
"""
import io
import py_compile

# ── 1) capability_app：校验端点（插在 reveal 块之前）──
p = "client/capability_app.py"
s = io.open(p, encoding="utf-8").read()
anchor = '    @app.post("/local-api/store/reveal")'
assert anchor in s, "reveal 锚点"
block = '''    @app.get("/local-api/store/downloaded")
    def store_downloaded():
        """校验下载状态（点击下载前先查）：catalog 文件名在 downloads 下存在且大小一致 → 已下载。"""
        pid = request.args.get("id") or ""
        version = request.args.get("version") or ""
        src = _store_source()
        catalog, err = src.get_catalog()
        if err:
            return jsonify({"ok": False, "error": err}), 200
        ver, err = _find_catalog_entry(catalog, pid, version)
        if err:
            return jsonify({"ok": False, "error": err}), 200
        filename = os.path.basename(str(ver.get("file") or ("%s-%s.zip" % (pid, version))))
        path = os.path.join(_data_dir, "downloads", filename)
        exists = os.path.isfile(path)
        try:
            size_ok = exists and (not ver.get("size")
                                  or os.path.getsize(path) == int(ver.get("size")))
        except (OSError, TypeError, ValueError):
            size_ok = exists
        return jsonify({"ok": True, "downloaded": bool(size_ok), "filename": filename})

'''
s = s.replace(anchor, block + anchor, 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
py_compile.compile(p, doraise=True)
print("capability downloaded 端点 ok")

# ── 2) store.js：下载状态机 + 依赖安装态 ──
p2 = "plugins/store/frontend/store.js"
s2 = io.open(p2, encoding="utf-8").read()

# 2a) 下载按钮渲染：进度条结构（fill + label）
old = '''    } else {
      html += '<button class="st-btn-ghost jz-only" data-act="download-local" data-id="' + esc(e.id) + '" data-ver="' + esc(e.version) + '">下载包</button>';
      html += '<button class="st-btn-ghost st-browser-dl" data-act="download" data-id="' + esc(e.id) + '" data-ver="' + esc(e.version) + '">下载包</button>';
    }
    return html;'''
new = '''    } else {
      html += dlButtonHtml(e);
      html += '<button class="st-btn-ghost st-browser-dl" data-act="download" data-id="' + esc(e.id) + '" data-ver="' + esc(e.version) + '">下载包</button>';
    }
    return html;'''
assert old in s2, "本地下载按钮锚点"
s2 = s2.replace(old, new, 1)
old = '''    if (e.runtime === 'server') {
      html += '<button class="st-btn-ghost jz-only" data-act="download-local" data-id="' + esc(e.id) + '" data-ver="' + esc(e.version) + '">下载后端包</button>';
      html += '<button class="st-btn-ghost st-browser-dl" data-act="download" data-id="' + esc(e.id) + '" data-ver="' + esc(e.version) + '">下载后端包</button>';'''
new = '''    if (e.runtime === 'server') {
      html += dlButtonHtml(e, '下载后端包');
      html += '<button class="st-btn-ghost st-browser-dl" data-act="download" data-id="' + esc(e.id) + '" data-ver="' + esc(e.version) + '">下载后端包</button>';'''
assert old in s2, "服务器下载按钮锚点"
s2 = s2.replace(old, new, 1)

# 2b) 依赖安装按钮：已安装态（禁用标签）
old = '''    if (IN_SHELL && e.runtime !== 'server') {
      if (!st.installed) {
        html += '<button class="st-btn-install" data-act="install" data-id="' + esc(e.id) + '">安装</button>';
      } else if (st.upgradable) {
        html += '<button class="st-btn-install" data-act="install" data-id="' + esc(e.id) + '">升级到 ' + esc(e.version) + '</button>';
      }
    }'''
new = '''    if (IN_SHELL && e.runtime !== 'server') {
      if (!st.installed) {
        html += '<button class="st-btn-install" data-act="install" data-id="' + esc(e.id) + '">安装</button>';
      } else if (st.upgradable) {
        html += '<button class="st-btn-install" data-act="install" data-id="' + esc(e.id) + '">升级到 ' + esc(e.version) + '</button>';
      } else if (e.runtime === 'dep-slot') {
        html += '<button class="st-btn-install" disabled title="依赖组件已在本机生效">已安装</button>';
      }
    }'''
assert old in s2, "依赖安装按钮锚点"
s2 = s2.replace(old, new, 1)

# 2c) dlButtonHtml 工厂（进度条结构）
old = '''  function buttonsHtml(e) {'''
new = '''  function dlButtonHtml(e, label) {
    // 下载按钮 = 进度条容器：fill 随百分比填充背景，label 叠在上层
    return '<button class="st-btn-dl jz-only" data-act="download-local" data-id="' + esc(e.id)
      + '" data-ver="' + esc(e.version) + '" data-label="' + esc(label || '下载包') + '">'
      + '<span class="st-dl-fill"></span>'
      + '<span class="st-dl-label">' + esc(label || '下载包') + '</span></button>';
  }

  function buttonsHtml(e) {'''
assert old in s2, "buttonsHtml 锚点"
s2 = s2.replace(old, new, 1)

# 2d) 下载动作整段重写：校验中 → 进度 → 已完成 / 已下载
start = s2.find("    if (act === 'download-local') {")
assert start >= 0
end = s2.find("    if (act === 'install') {", start)
assert end > start
new_dl = '''    if (act === 'download-local') {                 // 桌面端：校验 → 进度条下载（批次 10 状态机）
      if (btn.dataset.busy) return;
      btn.dataset.busy = '1';
      var dlLabel = btn.querySelector('.st-dl-label') || btn;
      var dlFill = btn.querySelector('.st-dl-fill');
      var idleLabel = btn.getAttribute('data-label') || '下载包';
      function dlSet(state, text, pct) {
        btn.classList.remove('checking', 'progress', 'done', 'have');
        if (state) btn.classList.add(state);
        dlLabel.textContent = text || idleLabel;
        if (dlFill) dlFill.style.width = (pct || 0) + '%';
      }
      function dlRelease() { delete btn.dataset.busy; }
      dlSet('checking', '校验中');                 // ① 先校验是否已下载
      fetch('/local-api/store/downloaded?id=' + encodeURIComponent(id)
            + '&version=' + encodeURIComponent(ver))
        .then(function (r) { return r.json(); })
        .then(function (chk) {
          if (chk.downloaded) { dlSet('have', '已下载'); dlRelease(); return; }
          dlSet('progress', '下载中 0%', 0);       // ② 后台任务 + 进度填充
          return fetch('/local-api/store/download', { method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id, version: ver }) })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              if (!d.ok) { dlSet('', idleLabel, 0); dlRelease();
                msg('下载失败：' + (d.error || ''), true); return; }
              var timer = setInterval(function () {
                fetch('/local-api/store/download/progress?id=' + d.id)
                  .then(function (r2) { return r2.json(); })
                  .then(function (p) {
                    if (p.status === 'running') {
                      var pct = p.total ? Math.round(p.received / p.total * 100) : 0;
                      dlSet('progress', '下载中 ' + pct + '%', pct);
                    } else if (p.status === 'done') {
                      clearInterval(timer);
                      dlSet('done', '已完成', 100);   // ③ 完成态（进度条满）
                      dlRelease();
                      msg('已保存到本机 downloads（已打开文件夹）');
                      fetch('/local-api/store/reveal', { method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: p.path || '' }) })
                        .then(function (r3) { return r3.json(); })
                        .then(function (x) {
                          if (!x.ok) msg('已保存，但打开文件夹失败：' + (x.error || '')
                            + '——文件在下载目录 downloads 内', true);
                        })
                        .catch(function () { /* 打开失败不改变已下载事实 */ });
                    } else if (p.status === 'error') {
                      clearInterval(timer);
                      dlSet('', idleLabel, 0); dlRelease();
                      msg('下载失败：' + (p.error || ''), true);
                    }
                  });
              }, 400);
            });
        })
        .catch(function (err) { dlSet('', idleLabel, 0); dlRelease(); msg('下载校验失败：' + err, true); });
      return;
    }
'''
s2 = s2[:start] + new_dl + s2[end:]
io.open(p2, "w", encoding="utf-8", newline="").write(s2)
print("store.js ok")

# ── 3) style.css：下载按钮状态样式 ──
p3 = "plugins/store/frontend/style.css"
s3 = io.open(p3, encoding="utf-8").read()
old3 = """.st-btn-ghost{background:var(--gray-bg);color:var(--ink2)}
.st-btn-ghost:hover{background:#e6e9ec;color:var(--ink)}"""
new3 = """.st-btn-ghost{background:var(--gray-bg);color:var(--ink2)}
.st-btn-ghost:hover{background:#e6e9ec;color:var(--ink)}

/* 下载按钮状态机：校验中(转圈) → 下载中(进度填充) → 已完成 / 已下载 */
.st-btn-dl{position:relative;overflow:hidden;background:var(--gray-bg);color:var(--ink2)}
.st-btn-dl:hover{background:#e6e9ec}
.st-dl-fill{position:absolute;left:0;top:0;bottom:0;width:0%;background:rgba(26,115,232,.28);
  transition:width .3s var(--ease)}
.st-dl-label{position:relative;z-index:1}
.st-btn-dl.checking::before{content:'';display:inline-block;width:11px;height:11px;margin-right:6px;
  vertical-align:-1px;border:2px solid rgba(0,0,0,.18);border-top-color:var(--blue);
  border-radius:50%;animation:stSpin .7s linear infinite;position:relative;z-index:1}
@keyframes stSpin{to{transform:rotate(360deg)}}
.st-btn-dl.progress .st-dl-fill{background:rgba(26,115,232,.32)}
.st-btn-dl.done{color:var(--green)}
.st-btn-dl.done .st-dl-fill{background:rgba(19,115,51,.28)}
.st-btn-dl.have{background:var(--green-bg);color:var(--green)}"""
assert old3 in s3, "ghost 按钮锚点"
s3 = s3.replace(old3, new3, 1)
io.open(p3, "w", encoding="utf-8", newline="").write(s3)
print("style.css ok")
