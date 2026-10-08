# -*- coding: utf-8 -*-
"""自绘对话框：商店管理页同款（adDialog）+ 两页 CSS + 版本戳。

- admin-store.js：adDialog 助手 + 审核点击处理器改 async 自绘（confirm/prompt 全去）
- admin-store.html：style 块补 .st-dlg 样式；admin-store.js v4→v5
- store/style.css：补 .st-dlg 样式；store/index.html 戳 style v7 / store.js v9
"""
import io

# ── A. store/style.css：追加对话框样式 ──
p = "plugins/store/frontend/style.css"
s = io.open(p, encoding="utf-8").read()
assert "st-dlg" not in s
s += """
/* ---------- 自绘对话框（替换原生 confirm/prompt） ---------- */
.st-dlg{z-index:110}
.st-dlg .st-modal-panel{width:min(400px,100%);padding:18px 20px}
.st-dlg-title{margin:0 0 10px;font-size:15px}
.st-dlg-body{font-size:13px;color:var(--ink2);line-height:1.75;overflow-wrap:anywhere;margin-bottom:16px}
.st-dlg-body b{color:var(--ink)}
.st-dlg-input{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;font-size:13px;
  margin:-6px 0 14px;background:#fff;transition:border-color .15s var(--ease),box-shadow .15s var(--ease)}
.st-dlg-input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(26,115,232,.15)}
.st-dlg-btns{display:flex;justify-content:flex-end;gap:8px}
.st-dlg-btns button{padding:7px 16px;border:0;border-radius:8px;font-size:13px;cursor:pointer;
  transition:background .15s var(--ease),transform .08s var(--ease)}
.st-dlg-btns button:active{transform:scale(.97)}
.st-dlg-btns button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
"""
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("store style ok")

# ── B. store/index.html 版本戳 ──
p = "plugins/store/frontend/index.html"
s = io.open(p, encoding="utf-8").read()
s = s.replace("style.css?v=6", "style.css?v=7", 1).replace("store.js?v=8", "store.js?v=9", 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("store stamps ok")

# ── C. admin-store.js：adDialog + 处理器 async 自绘 ──
p = "plugins/admin/frontend/js/admin-store.js"
s = io.open(p, encoding="utf-8").read()

anchor = "  function chip(text, kind) {"
assert anchor in s
dlg = '''  // 自绘对话框（替换原生 confirm/prompt；与商店页同款交互：ESC/遮罩=取消，Enter=确定）
  function adDialog(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var dlg = document.createElement('div');
      dlg.className = 'st-modal st-dlg';
      dlg.innerHTML = '<div class="st-modal-panel" role="alertdialog">'
        + '<h3 class="st-dlg-title">' + esc(opts.title || '确认') + '</h3>'
        + '<div class="st-dlg-body">' + (opts.html || '') + '</div>'
        + (opts.input ? '<input class="st-dlg-input" type="text" placeholder="'
            + esc(opts.placeholder || '') + '">' : '')
        + '<div class="st-dlg-btns">'
        + '<button type="button" class="st-dlg-no" data-k="0">' + esc(opts.cancelText || '取消') + '</button>'
        + '<button type="button" class="' + (opts.danger ? 'st-dlg-danger' : 'st-dlg-ok')
        + '" data-k="1">' + esc(opts.okText || '确定') + '</button>'
        + '</div></div>';
      var inp = dlg.querySelector('.st-dlg-input');
      function cancelVal() { return opts.input ? null : false; }
      function okVal() { return opts.input ? (inp ? inp.value : '') : true; }
      function done(val) {
        dlg.classList.remove('show');
        setTimeout(function () { if (dlg.parentNode) dlg.parentNode.removeChild(dlg); }, 220);
        document.removeEventListener('keydown', onKey);
        resolve(val);
      }
      function onKey(ev) {
        if (ev.key === 'Escape') done(cancelVal());
        else if (ev.key === 'Enter') done(okVal());
      }
      dlg.addEventListener('click', function (ev) {
        if (ev.target === dlg) return done(cancelVal());
        var b = ev.target.closest ? ev.target.closest('button[data-k]') : null;
        if (!b) return;
        done(b.getAttribute('data-k') === '0' ? cancelVal() : okVal());
      });
      document.body.appendChild(dlg);
      document.addEventListener('keydown', onKey);
      requestAnimationFrame(function () { dlg.classList.add('show'); });
      setTimeout(function () {
        var f = inp || dlg.querySelector('button[data-k="1"]');
        if (f) f.focus();
      }, 40);
    });
  }

''' + anchor
s = s.replace(anchor, dlg, 1)

old = s[s.find("  document.getElementById('st-rows').addEventListener('click', function (ev) {"):
        s.find("  function load() {")]
assert old and "prompt(" in old, "审核处理器定位失败"
new = '''  document.getElementById('st-rows').addEventListener('click', async function (ev) {
    var btn = ev.target.closest ? ev.target.closest('button[data-op]') : null;
    if (!btn) return;
    var op = btn.getAttribute('data-op');
    var id = btn.getAttribute('data-id');
    var v = btn.getAttribute('data-v');
    var dlgOpts = {
      approve: { title: '发布确认', okText: '发布',
        html: '确认发布 <b>' + esc(id) + ' v' + esc(v) + '</b>？<br>server 条目将自动部署后端包；'
          + '<b>装机冒烟请确认已做</b>。' },
      reject: { title: '驳回', input: true, danger: true, okText: '驳回', placeholder: '填写驳回原因（记入台账）',
        html: '驳回 <b>' + esc(id) + ' v' + esc(v) + '</b>：' },
      delist: { title: '下架确认', danger: true, okText: '下架',
        html: '下架 <b>' + esc(id) + ' v' + esc(v) + '</b>？<br>客户端可见性回落到旧版；已装客户端不降级。' },
      republish: { title: '重新上架', okText: '重新上架',
        html: '重新上架 <b>' + esc(id) + ' v' + esc(v) + '</b>？将回到待审核状态。' },
      redeploy: { title: '重新部署', okText: '重新部署',
        html: '把 <b>' + esc(id) + ' v' + esc(v) + '</b> 重新部署到服务器？<br>自动备份，可回滚。' },
    };
    var r = await adDialog(dlgOpts[op] || { title: '确认', html: esc(op + '？') });
    var note = '';
    if (op === 'reject') {
      if (r === null) return;
      note = r || '';
    } else if (!r) return;
    var path = op === 'redeploy' ? '/redeploy' : '/review';
    var body = op === 'redeploy' ? { id: id, version: v } : { id: id, version: v, action: op, note: note };
    if (op === 'delist') path = '/delist';
    if (op === 'delist') body = { id: id, version: v, note: note };
    post(path, body).then(function (r2) {
      if (r2.data.ok) {
        toast(op === 'approve' ? '已发布' + (r2.data.status === 'published' ? '：客户端可见' : '')
          : op === 'redeploy' ? '重新部署完成' : '操作成功');
        load();
      } else {
        toast('操作失败：' + (r2.data.error || '未知错误'), true);
      }
    }).catch(function (e) { toast('请求异常：' + e, true); });
  });

'''
s = s.replace(old, new, 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("admin-store.js ok")

# ── D. admin-store.html：样式块补对话框 CSS + 版本戳 v5 ──
p = "plugins/admin/frontend/admin-store.html"
s = io.open(p, encoding="utf-8").read()
anchor = "    /* 统计页签 */"
assert anchor in s
css = '''    /* 自绘对话框（替换原生 confirm/prompt；与商店页同款） */
    .st-dlg{z-index:110}
    .st-dlg .st-modal-panel{width:min(400px,100%);padding:18px 20px}
    .st-dlg-title{margin:0 0 10px;font-size:15px}
    .st-dlg-body{font-size:13px;color:#5f6368;line-height:1.75;overflow-wrap:anywhere;margin-bottom:16px}
    .st-dlg-body b{color:#202124}
    .st-dlg-input{width:100%;padding:8px 10px;border:1px solid rgba(0,0,0,.15);border-radius:8px;
      font-size:13px;margin:-6px 0 14px;background:#fff}
    .st-dlg-input:focus{outline:none;border-color:var(--md-primary,#4285F4);
      box-shadow:0 0 0 3px rgba(66,133,244,.15)}
    .st-dlg-btns{display:flex;justify-content:flex-end;gap:8px}
    .st-dlg-btns button{padding:7px 16px;border:0;border-radius:8px;font-size:13px;cursor:pointer}
    .st-dlg-no{background:rgba(0,0,0,.06);color:#5f6368}
    .st-dlg-no:hover{background:rgba(0,0,0,.1)}
    .st-dlg-ok{background:var(--md-primary,#4285F4);color:#fff}
    .st-dlg-ok:hover{background:#3b78e7}
    .st-dlg-danger{background:rgba(197,34,31,.1);color:#c5221f}
    .st-dlg-danger:hover{background:rgba(197,34,31,.18)}

    /* 统计页签 */'''
s = s.replace(anchor, css, 1)
s = s.replace("admin-store.js?v=4", "admin-store.js?v=5", 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("admin-store.html ok")
