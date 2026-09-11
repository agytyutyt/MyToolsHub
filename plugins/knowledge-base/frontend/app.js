/* 知识库插件 —— 库视图（分类树 / 文件列表 / 上传管理）+ 视图切换
 * 约定：原生 JS（ES5 语法为主，兼容内网老 Chrome）；所有插值经 esc() 转义（F-3）。
 * 阅读视图渲染器在 reader.js。
 */
(function () {
  "use strict";

  var API = "/api/knowledge-base";

  // ==================== 小工具 ====================
  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  function fmtTime(s) { return (s || "").replace("T", " ").slice(0, 16); }

  var snackbarTimer = null;
  function toast(msg, isError, duration) {
    var el = $("snackbar");
    el.textContent = msg;
    el.className = "snackbar show" + (isError ? " error" : "");
    if (snackbarTimer) clearTimeout(snackbarTimer);
    snackbarTimer = setTimeout(function () { el.className = "snackbar"; }, duration || 2600);
  }

  // ==================== API 封装（401 跳登录） ====================
  function api(method, url, opts) {
    opts = opts || {};
    // 注意：headers/body 仅在有值时才放入 init——显式传 null 会触发
    // "Failed to read the 'headers' property" TypeError（HeadersInit 不可空）
    var init = { method: method };
    if (opts.headers) init.headers = opts.headers;
    if (opts.body) init.body = opts.body;
    return fetch(API + url, init).then(function (res) {
      if (res.status === 401) {
        location.href = "/login?next=" + encodeURIComponent("/tool/knowledge-base");
        return new Promise(function () {}); // 跳转中挂起
      }
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error(data.error || ("请求失败（" + res.status + "）"));
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  // ==================== 全局状态 ====================
  var state = {
    canManage: false,
    categories: [],        // 后端原始列表
    catCount: {},          // 分类id -> 递归文件数（渲染树时统计）
    currentCat: "all",     // all | root | 分类id
    q: "",
    sortKey: "time",       // time（上传时间）| name（名称拼音）
    sortDir: "desc",       // asc | desc（默认时间倒序）
    files: [],             // 当前列表
    folded: {},            // 分类折叠状态（持久化到 sessionStorage）
    readerOpen: false,
    currentFileId: null,   // 当前阅读的文档 id（提示条关闭状态按此记录）
    listScroll: 0
  };

  try {
    var saved = sessionStorage.getItem("kb_folded");
    if (saved) state.folded = JSON.parse(saved) || {};
  } catch (e) { /* 忽略 */ }

  function saveFolded() {
    try { sessionStorage.setItem("kb_folded", JSON.stringify(state.folded)); } catch (e) { /* 忽略 */ }
  }

  // ==================== 依赖 / 配置加载 ====================
  function loadConfig() {
    return api("GET", "/config").then(function (data) {
      state.canManage = !!data.can_manage;
      $("btn-upload").className = state.canManage ? "btn btn-primary" : "btn btn-primary hidden";
      $("btn-cat-add").className = state.canManage ? "icon-btn" : "icon-btn hidden";
      $("fab-upload").className = state.canManage ? "fab" : "fab hidden";
    });
  }

  // ==================== 分类树 ====================
  function buildCounts() {
    var counts = {};
    state.categories.forEach(function (c) {
      counts[c.id] = (counts[c.id] || 0) + 1;
    });
    return counts;
  }

  function catChildren(pid) {
    return state.categories.filter(function (c) { return (c.parent_id || null) === pid; });
  }

  function catById(id) {
    for (var i = 0; i < state.categories.length; i++) {
      if (state.categories[i].id === id) return state.categories[i];
    }
    return null;
  }

  // 递归文件数：直接数 + 子孙分类累计
  function recursiveCount(id, direct) {
    var n = direct[id] || 0;
    catChildren(id).forEach(function (ch) { n += recursiveCount(ch.id, direct); });
    return n;
  }

  function renderTree() {
    var direct = buildCounts();
    state.catCount = direct;
    var tree = $("cat-tree");
    var html = "";

    function nodeHtml(c, depth) {
      var children = catChildren(c.id);
      var isFolded = !!state.folded[c.id];
      var n = recursiveCount(c.id, direct);
      var arrowCls = "arrow" + (children.length ? (isFolded ? " folded" : "") : " leaf");
      var h = '<div class="cat-node" data-id="' + esc(c.id) + '" data-depth="' + depth + '">' +
        '<div class="cat-row' + (state.currentCat === c.id ? " active" : "") + '" data-id="' + esc(c.id) + '">' +
        '<span class="' + arrowCls + '" data-toggle="' + esc(c.id) + '">▼</span>' +
        '<span class="cat-name" title="' + esc(c.name) + '">' + esc(c.name) + "</span>" +
        '<span class="cat-count">' + n + "</span></div>";
      if (children.length && !isFolded) {
        h += '<div class="cat-children">';
        children.forEach(function (ch) { h += nodeHtml(ch, depth + 1); });
        h += "</div>";
      }
      return h + "</div>";
    }

    // 「全部」节点 + 根级分类（全部节点不展示计数，文件数由右侧列表头展示）
    html += '<div class="cat-node" data-id="all">' +
      '<div class="cat-row' + (state.currentCat === "all" ? " active" : "") + '" data-id="all">' +
      '<span class="arrow leaf">▼</span><span class="cat-name">全部文档</span></div></div>';

    var roots = catChildren(null);
    roots.forEach(function (c) { html += nodeHtml(c, 0); });

    tree.innerHTML = html;

    // 事件委托：选中分类 / 折叠展开
    tree.onclick = function (ev) {
      var t = ev.target;
      var toggleId = t.getAttribute && t.getAttribute("data-toggle");
      if (toggleId) {
        state.folded[toggleId] = !state.folded[toggleId];
        saveFolded();
        renderTree();
        ev.stopPropagation();
        return;
      }
      var row = t.closest ? t.closest(".cat-row") : null;
      if (row) {
        selectCat(row.getAttribute("data-id"));
      }
    };
  }

  function selectCat(id) {
    state.currentCat = id || "all";
    renderTree();
    renderCatActions();
    loadFiles();
  }

  function renderCatActions() {
    var isNode = state.currentCat !== "all" && catById(state.currentCat);
    $("cat-actions").className = (state.canManage && isNode) ? "cat-actions" : "cat-actions hidden";
  }

  // ==================== 文件列表 ====================
  function loadFiles() {
    var url = "/files?";
    if (state.currentCat === "root") url += "category=root&";
    else if (state.currentCat !== "all") url += "category=" + encodeURIComponent(state.currentCat) + "&";
    if (state.q) url += "q=" + encodeURIComponent(state.q) + "&";
    return api("GET", url).then(function (data) {
      state.files = sortFiles(data.items || []);
      renderFiles();
    }).catch(function (e) {
      toast(e.message, true);
    });
  }

  // 排序：key = time（上传时间）/ name（名称拼音）；dir = asc / desc（默认 time + desc）
  var SORTERS = {
    time: function (a, b) {
      return String(a.created_at || "").localeCompare(String(b.created_at || ""));
    },
    name: function (a, b) {
      return String(a.name || "").localeCompare(String(b.name || ""), "zh-Hans-CN");
    }
  };

  function sortFiles(items) {
    var fn = SORTERS[state.sortKey] || SORTERS.time;
    var sorted = items.slice().sort(fn);
    if (state.sortDir === "desc") sorted.reverse();
    return sorted;
  }

  function bindSort() {
    var keySeg = $("seg-sort-key");
    var dirSeg = $("seg-sort-dir");
    if (!keySeg || !dirSeg) return;
    function bindSeg(seg, attr) {
      seg.onclick = function (ev) {
        var btn = ev.target.closest ? ev.target.closest(".seg-btn") : null;
        if (!btn) return;
        var val = btn.getAttribute(attr);
        if (attr === "data-key") state.sortKey = val;
        else state.sortDir = val;
        // 高亮当前段内选项（分段内单选，不自动取消）
        Array.prototype.forEach.call(seg.querySelectorAll(".seg-btn"), function (b) {
          b.className = "seg-btn" + (b === btn ? " active" : "");
        });
        state.files = sortFiles(state.files);
        renderFiles();
      };
    }
    bindSeg(keySeg, "data-key");
    bindSeg(dirSeg, "data-dir");
  }

  var EXT_LABEL = { pdf: "PDF", ofd: "OFD", docx: "DOC", xlsx: "XLS", xls: "XLS", csv: "CSV", md: "MD", markdown: "MD", txt: "TXT" };

  function renderFiles() {
    var wrap = $("file-list");
    var n = state.files.length;
    $("files-count").textContent = n ? "共 " + n + " 篇" : "";
    $("empty-tip").className = n ? "empty hidden" : "empty";
    var html = "";
    state.files.forEach(function (f) {
      var label = EXT_LABEL[f.ext] || (f.ext || "?").toUpperCase();
      var summary = (f.summary || "").trim();
      // 由旧版格式（.doc/.xls）自动转换而来：卡片上打"已转换"角标，悬停显示来源格式
      var convBadge = f.converted
        ? '<span class="conv-badge" title="上传时为 .' + esc(f.original_ext || "") +
          '，已自动转换为 .' + esc(f.ext) + '">已转换</span>'
        : "";
      html += '<div class="file-card" data-id="' + esc(f.id) + '">' +
        '<div class="file-card-top"><div class="file-icon ext-' + esc(f.ext) + '">' + esc(label) + "</div>" +
        '<div class="file-main"><div class="file-name" title="' + esc(f.name) + '">' + esc(f.name) +
        convBadge + "</div>" +
        '<div class="file-meta">' + esc(fmtSize(f.size)) + " · " + esc(f.created_by_name || f.created_by || "-") +
        " · " + esc(fmtTime(f.created_at)) + "</div></div>" +
        (state.canManage
          ? '<button type="button" class="card-edit" data-edit="' + esc(f.id) + '" title="编辑">' +
            '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg></button>'
          : "") + "</div>" +
        '<div class="file-summary' + (summary ? "" : " none") + '">' +
        (summary ? esc(summary) : "（暂无简介）") + "</div>" +
        "</div>";
    });
    wrap.innerHTML = html;
    wrap.onclick = function (ev) {
      var editBtn = ev.target.closest ? ev.target.closest(".card-edit") : null;
      if (editBtn) {
        openFileEdit(editBtn.getAttribute("data-edit"));
        return;
      }
      var card = ev.target.closest ? ev.target.closest(".file-card") : null;
      if (card) openReader(card.getAttribute("data-id"));
    };
  }

  // 文档编辑对话框（仅管理员）：名称 / 简介 / 归类 / 删除
  function openFileEdit(fileId) {
    var f = null;
    state.files.forEach(function (x) { if (x.id === fileId) f = x; });
    if (!f) return;
    openModal("编辑文档",
      '<div class="form-row"><label class="form-label">文档名称</label>' +
      '<input id="m-name" class="input" maxlength="80" value="' + esc(f.name) + '"></div>' +
      '<div class="form-row"><label class="form-label">简介（展示在卡片上，≤200 字）</label>' +
      '<textarea id="m-summary" class="input textarea" rows="3" maxlength="200" placeholder="一句话介绍该文档内容…">' + esc(f.summary || "") + "</textarea></div>" +
      '<div class="form-row"><label class="form-label">所属分类</label>' +
      '<select id="m-cat" class="select">' + catOptions(f.category_id, true) + "</select></div>",
      function () {
        var name = $("m-name").value.trim();
        if (!name) throw new Error("请输入文档名称");
        return api("PUT", "/files/" + encodeURIComponent(fileId), {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: name,
            summary: $("m-summary").value.trim(),
            category_id: $("m-cat").value || null
          })
        }).then(function () {
          closeModal();
          toast("已保存");
          return loadFiles();
        });
      }, "保存");
    // 删除：与「保存」同级的胶囊描边按钮（操作区最左）
    $("modal-del").className = "btn btn-danger";
    $("modal-actions").className = "modal-actions has-del";
    $("modal-del").onclick = function () {
      api("DELETE", "/files/" + encodeURIComponent(fileId)).then(function () {
        closeModal();
        toast("文档已删除");
        return loadFiles();
      }).catch(function (e) { toast(e.message, true); });
    };
  }

  // ==================== 对话框 ====================
  var modalCtx = null; // { onOk: function(): Promise|any, okText }

  function openModal(title, bodyHtml, onOk, okText) {
    $("modal-title").textContent = title;
    $("modal-body").innerHTML = bodyHtml;
    $("modal-error").className = "modal-error hidden";
    $("modal-ok").textContent = okText || "确定";
    modalCtx = { onOk: onOk };
    // 每次打开重置可选的删除按钮（仅编辑文档对话框会显示，靠左与保存/取消同级）
    $("modal-del").className = "btn btn-danger hidden";
    $("modal-del").onclick = null;
    $("modal-actions").className = "modal-actions";
    $("modal-mask").className = "modal-mask";
  }

  function closeModal() {
    $("modal-mask").className = "modal-mask hidden";
    modalCtx = null;
  }

  function modalError(msg) {
    var el = $("modal-error");
    el.textContent = msg;
    el.className = "modal-error";
  }

  function bindModal() {
    $("modal-cancel").onclick = closeModal;
    $("modal-mask").onclick = function (ev) { if (ev.target === $("modal-mask")) closeModal(); };
    $("modal-ok").onclick = function () {
      if (!modalCtx || !modalCtx.onOk) return closeModal();
      Promise.resolve().then(modalCtx.onOk).catch(function (e) { modalError(e.message); });
    };
  }

  // 分类选择下拉（上传 / 移动用）
  function catOptions(selectedId, withRoot) {
    var opts = withRoot ? '<option value="">（未分类）</option>' : "";
    function walk(pid, prefix) {
      catChildren(pid).forEach(function (c) {
        opts += '<option value="' + esc(c.id) + '"' + (c.id === selectedId ? " selected" : "") + ">" +
          esc(prefix + c.name) + "</option>";
        walk(c.id, prefix + c.name + " / ");
      });
    }
    walk(null, "");
    return opts;
  }

  // ==================== 管理操作 ====================
  function bindManage() {
    // 新建分类
    $("btn-cat-add").onclick = function () {
      var pid = state.currentCat !== "all" ? state.currentCat : null;
      var parentName = pid && catById(pid) ? esc(catById(pid).name) : "";
      openModal("新建分类",
        '<div class="form-row"><label class="form-label">分类名称</label>' +
        '<input id="m-name" class="input" maxlength="40" placeholder="不超过 40 字"></div>' +
        '<div class="form-row"><label class="form-label">上级分类</label>' +
        '<select id="m-parent" class="select">' + catOptions(pid, true) + "</select></div>",
        function () {
          var name = $("m-name").value.trim();
          if (!name) throw new Error("请输入分类名称");
          return api("POST", "/categories", {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name, parent_id: $("m-parent").value || null })
          }).then(function () {
            closeModal();
            toast("分类已创建");
            return Promise.all([loadCats(false), loadFiles()]);
          });
        });
      if (parentName) $("m-name").focus();
    };

    // 重命名分类
    $("btn-cat-rename").onclick = function () {
      var c = catById(state.currentCat);
      if (!c) return;
      openModal("重命名分类",
        '<div class="form-row"><label class="form-label">分类名称</label>' +
        '<input id="m-name" class="input" maxlength="40" value="' + esc(c.name) + '"></div>',
        function () {
          var name = $("m-name").value.trim();
          if (!name) throw new Error("请输入分类名称");
          return api("PUT", "/categories/" + encodeURIComponent(c.id), {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name })
          }).then(function () {
            closeModal();
            toast("已重命名");
            return loadCats(false);
          });
        });
    };

    // 删除分类
    $("btn-cat-delete").onclick = function () {
      var c = catById(state.currentCat);
      if (!c) return;
      openModal("删除分类",
        "<p>确定删除分类「<b>" + esc(c.name) + "</b>」吗？</p>" +
        "<p>仅允许删除空分类（无子分类且无文件）。</p>",
        function () {
          return api("DELETE", "/categories/" + encodeURIComponent(c.id)).then(function () {
            closeModal();
            toast("分类已删除");
            state.currentCat = "all";
            return Promise.all([loadCats(false), loadFiles()]);
          });
        }, "删除");
    };

    // 上传（对话框 + FAB 共用）：虚线拖放区
    function showUpload() {
      openModal("上传文档",
        '<div class="form-row"><label class="form-label">支持 PDF / OFD / Word / Excel / Markdown / 文本，≤20MB</label></div>' +
        '<div class="form-row"><div id="m-drop" class="dropzone">' +
        '<input id="m-file" type="file" accept=".pdf,.ofd,.docx,.doc,.xlsx,.xls,.csv,.md,.markdown,.txt" hidden>' +
        '<div class="dropzone-inner"><span class="dropzone-icon">' +
        '<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>' +
        '</span><div class="dropzone-text">拖动文件到此处，或 <b>点击选择文件</b></div>' +
        '<div class="dropzone-file" id="m-file-name"></div></div></div></div>' +
        '<div class="form-row"><label class="form-label">展示名称（默认取文件名）</label>' +
        '<input id="m-name" class="input" maxlength="80"></div>' +
        '<div class="form-row"><label class="form-label">保存到分类</label>' +
        '<select id="m-cat" class="select">' + catOptions(state.currentCat === "all" ? "" : state.currentCat, true) + "</select></div>" +
        '<div class="form-hint">旧版 .doc / .xls 会自动转换为 .docx / .xlsx 后保存（仅保留文字与表格，样式/图片不迁移）。</div>',
        function () {
          var input = $("m-file");
          if (!input.files || !input.files[0]) throw new Error("请选择要上传的文件");
          var file = input.files[0];
          var fd = new FormData();
          fd.append("file", file);
          fd.append("name", $("m-name").value.trim() || file.name.replace(/\.[^.]+$/, ""));
          fd.append("category_id", $("m-cat").value || "");
          $("modal-ok").disabled = true;
          $("modal-ok").textContent = "上传中…";
          return api("POST", "/files", { body: fd }).then(function (data) {
            $("modal-ok").disabled = false;
            closeModal();
            // 服务端转换过旧版格式时给出明确提示（含原格式 → 新格式）
            if (data && data.converted_from) {
              toast("上传成功：已自动将 ." + data.converted_from + " 转换为 ." +
                    (data.item && data.item.ext ? data.item.ext : ""), false, 3600);
            } else {
              toast("上传成功");
            }
            return loadFiles();
          }).catch(function (e) {
            $("modal-ok").disabled = false;
            $("modal-ok").textContent = "确定";
            throw e;
          });
        }, "上传");
      bindDropzone($("m-drop"), $("m-file"), $("m-file-name"), $("m-name"));
    }
    $("btn-upload").onclick = showUpload;
    $("fab-upload").onclick = showUpload;
  }

  // 拖放区绑定：点击 = 打开文件选择；dragover/drop = 高亮并接收文件
  function bindDropzone(zone, input, nameEl, nameInput) {
    function setFile(file) {
      if (!file) return;
      // 用 DataTransfer 回填 input.files，提交逻辑统一走 input.files[0]
      try {
        var dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
      } catch (e) { /* 极老浏览器无 DataTransfer 构造器：展示文件名，提交前另有校验 */ }
      nameEl.textContent = file.name + "（" + fmtSize(file.size) + "）";
      zone.className = "dropzone filled";
      if (nameInput && !nameInput.value) {
        nameInput.value = file.name.replace(/\.[^.]+$/, "");
      }
    }
    zone.onclick = function () { input.click(); };
    input.onchange = function () {
      if (this.files && this.files[0]) setFile(this.files[0]);
    };
    zone.addEventListener("dragenter", function (e) {
      e.preventDefault(); e.stopPropagation();
      zone.className = "dropzone dragging";
    });
    zone.addEventListener("dragover", function (e) {
      e.preventDefault(); e.stopPropagation();
      zone.className = "dropzone dragging";
    });
    zone.addEventListener("dragleave", function (e) {
      e.preventDefault(); e.stopPropagation();
      zone.className = "dropzone" + (input.files && input.files.length ? " filled" : "");
    });
    zone.addEventListener("drop", function (e) {
      e.preventDefault(); e.stopPropagation();
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) setFile(files[0]);
    });
  }

  // ==================== 全屏 ====================
  function bindFullscreen() {
    Array.prototype.forEach.call(document.querySelectorAll(".btn-fullscreen"), function (btn) {
      btn.onclick = function () {
        var active = btn.className.indexOf("active") >= 0;
        if (active) {
          document.documentElement.classList.remove("kb-fs");
        } else {
          document.documentElement.classList.add("kb-fs");
        }
        Array.prototype.forEach.call(document.querySelectorAll(".btn-fullscreen"), function (b) {
          b.className = active ? "btn btn-fullscreen" : "btn btn-fullscreen active";
        });
        Array.prototype.forEach.call(document.querySelectorAll(".view"), function (v) {
          v.className = active ? v.className.replace(" fullscreen", "") : v.className + " fullscreen";
        });
        if (state.readerOpen && window.KBReader && KBReader.onResize) KBReader.onResize();
      };
    });
  }

  // ==================== 阅读视图切换 ====================
  // 转换提示条：展示"该文档由旧版格式自动转换而来"，用户可关闭；
  // 关闭状态存于内存（同一次会话内不再重复弹出），不落本地存储（每次打开仍是新会话观感）。
  var noticeClosed = {};

  function showReaderNotice(f) {
    var box = $("reader-notice");
    if (!f || !f.converted || noticeClosed[f.id]) {
      box.className = "reader-notice hidden";
      return;
    }
    var from = (f.original_ext || "").toUpperCase();
    var to = (f.ext || "").toUpperCase();
    $("reader-notice-text").textContent =
      "本文档由旧版 ." + (f.original_ext || "") + " 自动转换为 ." + (f.ext || "") +
      "，" + from + " → " + to + "；仅保留正文文字与表格，原样式、图片未迁移。";
    box.className = "reader-notice";
  }

  function hideReaderNotice() {
    if (state.currentFileId) noticeClosed[state.currentFileId] = true;
    $("reader-notice").className = "reader-notice hidden";
  }

  function openReader(fileId) {
    var f = null;
    state.files.forEach(function (x) { if (x.id === fileId) f = x; });
    if (!f) return;
    state.listScroll = window.pageYOffset || 0;
    state.readerOpen = true;
    state.currentFileId = fileId;
    $("view-list").className = "view hidden";
    $("view-reader").className = "view";
    // 文件信息头（徽标 + 标题）展示在底部胶囊工具条
    $("dock-ext").textContent = EXT_LABEL[f.ext] || (f.ext || "").toUpperCase();
    $("dock-ext").className = "badge ext-" + f.ext;
    $("dock-title").textContent = f.name;
    // 转换来源提示条：仅对 .doc/.xls 自动转换的文档显示（本次会话内关闭过则不再弹）
    showReaderNotice(f);
    $("btn-copy").disabled = true;
    $("reader-pager").className = "pager hidden";
    window.KBReader.render(f, {
      onReady: function (cap) {
        $("btn-copy").disabled = false;
        // 图标按钮：范围说明放 title 悬停提示，不改 textContent（会清掉图标）
        $("btn-copy").title = cap || "复制";
        if (cap === "复制本页" || cap === "复制本页文字") {
          $("reader-pager").className = "pager";
        }
      },
      onPage: function (page, total, zoom) {
        $("page-indicator").textContent = page + " / " + total;
        if (zoom != null) $("zoom-indicator").textContent = Math.round(zoom * 100) + "%";
        $("btn-prev-page").disabled = page <= 1;
        $("btn-next-page").disabled = page >= total;
      },
      onFail: function (msg) {
        $("reader-status").textContent = msg;
        $("reader-status").className = "reader-status";
      }
    });
    window.scrollTo(0, 0);
  }

  function closeReader() {
    state.readerOpen = false;
    state.currentFileId = null;
    window.KBReader.destroy();
    $("view-reader").className = "view hidden";
    $("view-list").className = "view";
    $("reader-notice").className = "reader-notice hidden";
    $("reader-status").className = "reader-status hidden";
    $("reader-container").innerHTML = "";
    $("btn-copy").title = "复制";
    $("reader-pager").className = "pager hidden";
    window.scrollTo(0, state.listScroll || 0);
  }

  // ==================== 初始化 ====================
  function loadCats(withFiles) {
    return api("GET", "/categories").then(function (data) {
      state.categories = data.items || [];
      renderTree();
      renderCatActions();
      if (withFiles !== false) return loadFiles();
    }).catch(function (e) {
      toast(e.message, true);
    });
  }

  function init() {
    bindModal();
    bindFullscreen();

    // 返回 / 复制 / 翻页 / 缩放（阅读器接口由 reader.js 挂到 window.KBReader）
    $("btn-back").onclick = closeReader;
    $("btn-notice-close").onclick = hideReaderNotice;
    $("btn-copy").onclick = function () {
      if (window.KBReader && KBReader.copy) KBReader.copy();
    };
    $("btn-prev-page").onclick = function () { if (window.KBReader) KBReader.prevPage(); };
    $("btn-next-page").onclick = function () { if (window.KBReader) KBReader.nextPage(); };
    $("btn-zoom-in").onclick = function () { if (window.KBReader) KBReader.zoomIn(); };
    $("btn-zoom-out").onclick = function () { if (window.KBReader) KBReader.zoomOut(); };

    // 搜索（输入停顿 300ms 触发）
    var searchTimer = null;
    $("search-input").oninput = function () {
      if (searchTimer) clearTimeout(searchTimer);
      var v = this.value;
      searchTimer = setTimeout(function () {
        state.q = v.trim();
        loadFiles();
      }, 300);
    };

    bindManage();
    bindSort();

    loadConfig()
      .then(function () { return loadCats(true); })
      .catch(function (e) {
        // 依赖/网络异常提示
        var d = $("deps");
        d.textContent = "加载失败：" + e.message;
        d.className = "deps";
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
