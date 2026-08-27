/* 共享文档 —— 前端逻辑
 *
 * 协作模型（轮询 + 乐观锁）：
 * - 编辑内容后防抖自动保存，携带 base_version；
 * - 若服务端版本已前进（他人已保存）则返回 409，前端弹冲突条，
 *   由用户选择「丢弃本地修改」或「覆盖保存」；
 * - 前台每 3s 轮询一次文档详情（含在线用户），版本变化时自动拉取最新内容。
 */
(function () {
  "use strict";

  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

  var POLL_MS = 3000;        // 轮询文档版本 / 在线用户
  var PRESENCE_MS = 10000;   // 在线心跳
  var SAVE_DEBOUNCE = 1200;  // 停止输入多久后自动保存

  var state = {
    docs: [],
    current: null,          // 当前打开的文档 {id,name,type,version,content,updated_at,updated_by}
    baseVersion: 0,         // 本地上次已知的服务端版本
    dirty: false,
    saving: false,
    conflict: false,
    serverDoc: null,        // 冲突时服务端最新文档
    clientId: localStorage.getItem("sd_client_id") || genId(),
    user: null,             // 当前登录用户（AdminCommon.getSession() 取，服务端权威）
    activeLevel: "unit",    // 列表当前 Tab：unit / department / private
    matrix: [],             // Excel 编辑中的二维数组
    saveTimer: null,
    pollTimer: null,
    presenceTimer: null,
  };
  localStorage.setItem("sd_client_id", state.clientId);

  function genId() {
    return "c" + Math.random().toString(36).slice(2, 10);
  }

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* ==================== API ==================== */
  function api(url, options) {
    options = options || {};
    options.headers = options.headers || {};
    return fetch(url, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.status === 401) {
          // iframe 场景要跳顶层窗口，否则登录页会嵌在小框框里
          window.top.location.href = '/login?next=' + encodeURIComponent(window.top.location.pathname);
          var err = new Error(data.detail || '未登录或登录已过期');
          err.status = 401;
          throw err;
        }
        if (!res.ok) {
          var err = new Error(data.detail || ("请求失败 " + res.status));
          err.status = res.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function postJSON(url, body) {
    return api(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  /* ==================== 保存状态提示 ==================== */
  function setSaveState(kind, text) {
    var el = $("#save-state");
    if (!el) return;
    var map = {
      idle: ["", "未修改"],
      dirty: ["", "有未保存修改"],
      saving: ["", "保存中…"],
      saved: ["ok", "已保存"],
      synced: ["ok", "已同步最新"],
      conflict: ["err", "版本冲突"],
      error: ["err", "保存失败"],
    };
    var m = map[kind] || ["", text || ""];
    el.className = "save-state " + m[0];
    el.textContent = text || m[1];
  }

  /* ==================== 视图切换 ==================== */
  function showList() {
    stopCollab();
    state.current = null;
    $("#view-editor").classList.add("hidden");
    $("#view-list").classList.remove("hidden");
    refreshList();
  }

  function showEditor() {
    $("#view-list").classList.add("hidden");
    $("#view-editor").classList.remove("hidden");
    $("#doc-title").textContent = state.current.name;
    var badge = $("#doc-type");
    badge.textContent = state.current.type === "word" ? "Word 文档" : "Excel 表格";
    renderUsers([]);
    setSaveState("idle");
  }

  /* ==================== 文档列表 ==================== */
  function refreshList() {
    api("/api/shared-docs/documents").then(function (data) {
      state.docs = data.documents || [];
      renderTabs();
      renderList();
    }).catch(function (e) {
      alert("加载文档列表失败：" + e.message);
    });
  }

  function typeIcon(t) { return t === "word" ? "📄" : "📊"; }

  function scopeLabel(level) {
    if (level === "unit") return "单位";
    if (level === "department") return "部门";
    return "私人";
  }

  /* 三个 Tab 的数量徽标：按文档 scope.level 分组 */
  function renderTabs() {
    var unit = 0, dept = 0, mine = 0;
    state.docs.forEach(function (d) {
      var lv = (d.scope && d.scope.level) || "private";
      if (lv === "unit") unit++;
      else if (lv === "department") dept++;
      else mine++;
    });
    $("#count-unit").textContent = unit;
    $("#count-dept").textContent = dept;
    $("#count-mine").textContent = mine;
  }

  function renderList() {
    var level = state.activeLevel;
    var docs = state.docs.filter(function (d) {
      return ((d.scope && d.scope.level) || "private") === level;
    });
    var box = $("#doc-list");
    box.innerHTML = "";
    $("#empty-tip").classList.toggle("hidden", docs.length > 0);
    docs.forEach(function (d) {
      var card = document.createElement("div");
      card.className = "doc-card";
      var meta = "更新于 " + (d.updated_at || "-") + " · 版本 " + (d.version || 0);
      if (d.updated_by) meta += " · " + esc(d.updated_by);
      var lv = (d.scope && d.scope.level) || "private";
      var canManage = !!(state.user && (state.user.super_admin || d.created_by === state.user.username));
      card.innerHTML =
        '<div class="doc-card-head">' +
          '<span class="doc-icon">' + typeIcon(d.type) + "</span>" +
          '<h3 title="' + esc(d.name) + '">' + esc(d.name) + "</h3>" +
        "</div>" +
        '<div class="doc-meta">' +
          '<span class="tag">' + (d.type === "word" ? "Word" : "Excel") + "</span>" +
          '<span class="tag scope-tag scope-' + esc(lv) + '">' + esc(scopeLabel(lv)) + "</span>" +
          (d.created_by_name ? '<span class="owner">' + esc(d.created_by_name) + "</span>" : "") +
          "<br>" + meta +
        "</div>" +
        '<div class="doc-actions">' +
          '<button class="btn btn-primary" data-act="open">打开</button>' +
          '<button class="btn" data-act="export">导出</button>' +
          (canManage ? '<button class="btn" data-act="rename">重命名</button>' : "") +
          (canManage ? '<button class="btn" data-act="scope">调整挂靠</button>' : "") +
          (canManage ? '<button class="btn danger" data-act="del">删除</button>' : "") +
        "</div>";
      card.querySelector('[data-act="open"]').addEventListener("click", function () { openDoc(d.id); });
      card.querySelector('[data-act="export"]').addEventListener("click", function () { exportDoc(d.id, d.name, d.type); });
      if (canManage) {
        card.querySelector('[data-act="rename"]').addEventListener("click", function () { renameDoc(d.id); });
        card.querySelector('[data-act="scope"]').addEventListener("click", function () { openScopeModal(d.id); });
        card.querySelector('[data-act="del"]').addEventListener("click", function () { deleteDoc(d.id); });
      }
      box.appendChild(card);
    });
  }

  function openCreateModal() {
    $("#create-modal").classList.remove("hidden");
    var hint = $("#create-level-hint");
    hint.classList.toggle("hidden", !(state.user && state.user.super_admin));
    if (state.user && state.user.super_admin) {
      hint.textContent = "管理员创建的单位/部门文档，挂靠在您所属的单位/部门下";
    }
    var nameInput = $("#create-name");
    nameInput.value = "";
    nameInput.focus();
  }

  function closeCreateModal() {
    $("#create-modal").classList.add("hidden");
  }

  function submitCreate() {
    var name = $("#create-name").value.trim();
    if (!name) { alert("请输入文档名称"); return; }
    var type = $("#create-type").value;
    var level = $("#create-level").value;
    postJSON("/api/shared-docs/documents", { name: name, type: type, level: level })
      .then(function (data) {
        closeCreateModal();
        refreshList();
        openDoc(data.document.id);
      })
      .catch(function (e) { alert("创建失败：" + e.message); });
  }

  function renameDoc(id) {
    var d = state.docs.filter(function (x) { return x.id === id; })[0];
    var name = prompt("输入新名称：", d && d.name || "");
    if (!name) return;
    name = name.trim();
    if (!name) return;
    postJSON("/api/shared-docs/documents/" + id + "/rename", { name: name })
      .then(function () { refreshList(); })
      .catch(function (e) { alert("重命名失败：" + e.message); });
  }

  function deleteDoc(id) {
    var d = state.docs.filter(function (x) { return x.id === id; })[0];
    if (!confirm("确定删除文档「" + (d && d.name || id) + "」？该操作不可恢复。")) return;
    api("/api/shared-docs/documents/" + id, { method: "DELETE" })
      .then(function () { refreshList(); })
      .catch(function (e) { alert("删除失败：" + e.message); });
  }

  var scopeDocId = null;

  function openScopeModal(id) {
    scopeDocId = id;
    var d = state.docs.filter(function (x) { return x.id === id; })[0];
    var cur = (d.scope && d.scope.level) || "private";
    $("#scope-level").value = cur;
    $("#scope-modal").classList.remove("hidden");
  }

  function submitScope() {
    var id = scopeDocId;
    var level = $("#scope-level").value;
    if (!id) return;
    postJSON("/api/shared-docs/documents/" + id + "/scope", { level: level })
      .then(function () {
        $("#scope-modal").classList.add("hidden");
        scopeDocId = null;
        refreshList();
      })
      .catch(function (e) { alert("调整挂靠失败：" + e.message); });
  }

  function exportDoc(id, name, type) {
    var ext = type === "word" ? "docx" : "xlsx";
    var a = document.createElement("a");
    a.href = "/api/shared-docs/documents/" + id + "/export";
    a.download = (name || "document") + "." + ext;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  /* ==================== 打开文档 ==================== */
  function openDoc(id) {
    api("/api/shared-docs/documents/" + id).then(function (data) {
      var doc = data.document;
      state.current = {
        id: doc.id, name: doc.name, type: doc.type, version: doc.version,
        content: doc.content, updated_at: doc.updated_at, updated_by: doc.updated_by,
      };
      state.baseVersion = doc.version;
      state.dirty = false;
      state.conflict = false;
      state.serverDoc = null;
      $("#conflict").classList.add("hidden");
      showEditor();
      if (doc.type === "word") {
        $("#word-editor").classList.remove("hidden");
        $("#excel-editor").classList.add("hidden");
        renderWord();
      } else {
        $("#excel-editor").classList.remove("hidden");
        $("#word-editor").classList.add("hidden");
        loadMatrix();
        renderGrid();
      }
      renderUsers(doc.users || []);
      startCollab();
    }).catch(function (e) {
      alert("打开文档失败：" + e.message);
    });
  }

  /* ==================== Word 内容渲染 / 序列化 ==================== */
  function renderWord() {
    var area = $("#word-area");
    area.innerHTML = (state.current.content.blocks || []).map(blockHTML).join("");
  }

  function runHTML(run) {
    var t = esc(run.t == null ? "" : run.t);
    if (run.b) t = "<b>" + t + "</b>";
    if (run.i) t = "<i>" + t + "</i>";
    if (run.u) t = "<u>" + t + "</u>";
    return t;
  }

  function blockHTML(b) {
    var inner, tag;
    if (b.type === "ul" || b.type === "ol") {
      inner = (b.items || []).map(function (it) {
        return "<li>" + (it.runs || []).map(runHTML).join("") + "</li>";
      }).join("");
      return "<" + b.type + ">" + inner + "</" + b.type + ">";
    }
    tag = /^h[1-6]$/.test(b.type) ? b.type : "div";
    inner = (b.runs || []).map(runHTML).join("");
    return "<" + tag + ">" + inner + "</" + tag + ">";
  }

  /* 从 contenteditable DOM 提取 runs（带继承的加粗/斜体/下划线） */
  function runsFromEl(el) {
    var runs = [];
    el.childNodes.forEach(function (node) {
      if (node.nodeType === Node.TEXT_NODE) {
        if (node.textContent) runs.push({ t: node.textContent });
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      var tag = node.tagName.toLowerCase();
      if (tag === "br") return;
      runsFromEl(node).forEach(function (inner) {
        runs.push({
          t: inner.t,
          b: inner.b || tag === "b" || tag === "strong",
          i: inner.i || tag === "i" || tag === "em",
          u: inner.u || tag === "u" || tag === "ins",
        });
      });
    });
    return runs;
  }

  function wordToBlocks() {
    var blocks = [];
    $("#word-area").childNodes.forEach(function (node) {
      if (node.nodeType === Node.TEXT_NODE) {
        if (node.textContent) blocks.push({ type: "p", runs: [{ t: node.textContent }] });
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      var tag = node.tagName.toLowerCase();
      if (tag === "br") return;
      if (tag === "ul" || tag === "ol") {
        var items = [];
        node.querySelectorAll("li").forEach(function (li) { items.push({ runs: runsFromEl(li) }); });
        if (items.length) blocks.push({ type: tag, items: items });
        return;
      }
      if (/^h[1-6]$/.test(tag)) {
        blocks.push({ type: tag, runs: runsFromEl(node) });
        return;
      }
      // div / p / 其余块级元素都作为段落
      var runs = runsFromEl(node);
      if (runs.length) blocks.push({ type: "p", runs: runs });
    });
    return { blocks: blocks };
  }

  /* ==================== Excel 内容渲染 / 序列化 ==================== */
  function colName(n) {
    var s = "";
    n++;
    while (n > 0) {
      var m = (n - 1) % 26;
      s = String.fromCharCode(65 + m) + s;
      n = Math.floor((n - 1) / 26);
    }
    return s;
  }

  function loadMatrix() {
    var content = state.current.content || {};
    var rows = (content.rows && content.rows.length) ? content.rows : [[""]];
    state.matrix = rows.map(function (r) { return r.slice(); });
    state.colWidths = (content.colWidths || []).slice();
    if (!state.matrix.length) state.matrix = [[""]];
  }

  function renderGrid() {
    var rows = state.matrix;
    var cols = 0;
    rows.forEach(function (r) { cols = Math.max(cols, r.length); });
    var html = '<tr><th class="rowhead"></th>';
    for (var c = 0; c < cols; c++) {
      html += '<th class="colhead" data-c="' + c + '">' + colName(c) + '<span class="col-resize" data-c="' + c + '"></span></th>';
    }
    html += "</tr>";
    rows.forEach(function (row, r) {
      html += '<tr><th class="rowhead">' + (r + 1) + "</th>";
      for (var c = 0; c < cols; c++) {
        var v = row[c] == null ? "" : row[c];
        html += '<td contenteditable="true" data-r="' + r + '" data-c="' + c + '">' + esc(v) + "</td>";
      }
      html += "</tr>";
    });
    $("#excel-grid").innerHTML = html;
    syncColWidths();
    updateExcelSize();
  }

  /* 通过动态 <style> 规则直接给「该列的 th + 所有 td」设宽度，
   * 在 table-layout:auto 下确定生效（<col> 宽度只能作为最小参考，不可靠）。 */
  function syncColWidths() {
    var style = document.getElementById("excel-col-rules");
    if (!style) {
      style = document.createElement("style");
      style.id = "excel-col-rules";
      document.head.appendChild(style);
    }
    var rules = "";
    (state.colWidths || []).forEach(function (w, c) {
      if (w) {
        rules += '#excel-grid th.colhead[data-c="' + c + '"],' +
                 '#excel-grid td[data-c="' + c + '"]{width:' + w + "px;min-width:" + w + "px}";
      }
    });
    style.textContent = rules;
  }

  function gridToMatrix() {
    $$("#excel-grid td[data-c]").forEach(function (td) {
      var r = parseInt(td.getAttribute("data-r"), 10);
      var c = parseInt(td.getAttribute("data-c"), 10);
      if (state.matrix[r] == null) state.matrix[r] = [];
      state.matrix[r][c] = td.textContent;
    });
  }

  function updateExcelSize() {
    var rows = state.matrix.length;
    var cols = state.matrix.reduce(function (m, r) { return Math.max(m, r.length); }, 0);
    $("#excel-size").textContent = rows + " 行 × " + cols + " 列";
  }

  function addRow() {
    var cols = state.matrix.reduce(function (m, r) { return Math.max(m, r.length); }, 0);
    state.matrix.push(new Array(cols).fill(""));
    renderGrid();
    markDirty();
  }

  function delRow() {
    if (state.matrix.length <= 1) { alert("至少保留一行"); return; }
    state.matrix.pop();
    renderGrid();
    markDirty();
  }

  function addCol() {
    state.matrix.forEach(function (r) { r.push(""); });
    state.colWidths.push(0);
    renderGrid();
    markDirty();
  }

  function delCol() {
    var cols = state.matrix.reduce(function (m, r) { return Math.max(m, r.length); }, 0);
    if (cols <= 1) { alert("至少保留一列"); return; }
    state.matrix.forEach(function (r) { r.pop(); });
    state.colWidths.pop();
    renderGrid();
    markDirty();
  }

  /* ==================== 列宽拖拽调整 ==================== */
  var MIN_COL_W = 30, MAX_COL_W = 600, DEFAULT_COL_W = 80;

  function startColResize(colIndex, startX) {
    // 以该列当前实际渲染宽度为起点：未调整过的列 colWidths 为 0，
    // 若直接用 DEFAULT_COL_W 作基线，首次拖动会瞬间从自然宽度跳到 80px 附近（不跟手）
    var td = document.querySelector('#excel-grid td[data-c="' + colIndex + '"]');
    var startWidth = td ? td.offsetWidth : ((state.colWidths && state.colWidths[colIndex]) || DEFAULT_COL_W);
    document.body.classList.add("col-resizing");

    function onMove(ev) {
      var w = Math.round(Math.max(MIN_COL_W, Math.min(MAX_COL_W, startWidth + (ev.clientX - startX))));
      if (state.colWidths) state.colWidths[colIndex] = w;
      syncColWidths();
    }

    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.classList.remove("col-resizing");
      markDirty();
    }

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  /* ==================== 编辑与自动保存 ==================== */
  function markDirty() {
    state.dirty = true;
    setSaveState("dirty");
    scheduleSave();
  }

  function scheduleSave() {
    if (state.saveTimer) clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(saveNow, SAVE_DEBOUNCE);
  }

  function buildContent() {
    if (state.current.type === "word") return wordToBlocks();
    return {
      rows: state.matrix.map(function (r) { return r.slice(); }),
      colWidths: state.colWidths.slice(),
    };
  }

  function saveNow() {
    if (!state.current || state.conflict) return;
    if (state.saving) { scheduleSave(); return; }
    if (!state.dirty) return;
    state.saving = true;
    setSaveState("saving");
    postJSON("/api/shared-docs/documents/" + state.current.id + "/content", {
      base_version: state.baseVersion,
      content: buildContent(),
    }).then(function (data) {
      state.baseVersion = data.document.version;
      state.current.updated_at = data.document.updated_at;
      state.current.updated_by = data.document.updated_by;
      state.dirty = false;
      setSaveState("saved");
    }).catch(function (e) {
      if (e.status === 409 && e.data.document) {
        state.conflict = true;
        state.serverDoc = e.data.document;
        $("#conflict").classList.remove("hidden");
        setSaveState("conflict");
      } else {
        setSaveState("error");
      }
    }).then(function () {
      state.saving = false;
    });
  }

  /* ==================== 轮询与在线用户 ==================== */
  function renderUsers(users) {
    var box = $("#users");
    box.innerHTML = "";
    (users || []).forEach(function (u) {
      var chip = document.createElement("span");
      chip.className = "user-chip";
      chip.textContent = u;
      chip.title = "在线编辑中：" + u;
      box.appendChild(chip);
    });
  }

  function pollDoc() {
    if (!state.current) return;
    api("/api/shared-docs/documents/" + state.current.id).then(function (data) {
      var doc = data.document;
      renderUsers(doc.users || []);
      if (doc.version > state.baseVersion) {
        if (state.dirty) {
          state.conflict = true;
          state.serverDoc = doc;
          $("#conflict").classList.remove("hidden");
          setSaveState("conflict");
        } else {
          state.current.content = doc.content;
          state.baseVersion = doc.version;
          state.current.updated_at = doc.updated_at;
          state.current.updated_by = doc.updated_by;
          renderContent();
          setSaveState("synced");
        }
      }
    }).catch(function () { /* 忽略瞬时网络错误，下轮重试 */ });
  }

  function renderContent() {
    if (state.current.type === "word") renderWord();
    else { loadMatrix(); renderGrid(); }
  }

  function heartbeat() {
    if (!state.current) return;
    postJSON("/api/shared-docs/documents/" + state.current.id + "/presence", {
      client_id: state.clientId,
    }).then(function (data) {
      renderUsers(data.users || []);
    }).catch(function () {});
  }

  function startCollab() {
    stopCollab();
    heartbeat();
    state.pollTimer = setInterval(pollDoc, POLL_MS);
    state.presenceTimer = setInterval(heartbeat, PRESENCE_MS);
  }

  function stopCollab() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    if (state.presenceTimer) { clearInterval(state.presenceTimer); state.presenceTimer = null; }
    if (state.saveTimer) { clearTimeout(state.saveTimer); state.saveTimer = null; }
  }

  /* ==================== 冲突处理 ==================== */
  function discardLocal() {
    if (!state.serverDoc) return;
    state.current.content = state.serverDoc.content;
    state.baseVersion = state.serverDoc.version;
    state.current.updated_at = state.serverDoc.updated_at;
    state.current.updated_by = state.serverDoc.updated_by;
    state.conflict = false;
    state.dirty = false;
    state.serverDoc = null;
    $("#conflict").classList.add("hidden");
    renderContent();
    setSaveState("synced");
  }

  function overwriteLocal() {
    if (!state.serverDoc) return;
    state.baseVersion = state.serverDoc.version;
    state.conflict = false;
    state.serverDoc = null;
    $("#conflict").classList.add("hidden");
    if (state.dirty) {
      state.saving = false;
      saveNow();
    } else {
      setSaveState("idle");
    }
  }

  /* ==================== 导入 / 导出 ==================== */
  function exportCurrent() {
    if (!state.current) return;
    if (state.dirty) {
      alert("存在未保存的修改，请等待保存完成后再导出。");
      return;
    }
    exportDoc(state.current.id, state.current.name, state.current.type);
  }

  function doImport() {
    var input = $("#file-input");
    var f = input.files && input.files[0];
    if (!f || !state.current) return;
    var fd = new FormData();
    fd.append("file", f);
    setSaveState("saving", "导入中…");
    api("/api/shared-docs/documents/" + state.current.id + "/import", { method: "POST", body: fd })
      .then(function (data) {
        state.current.content = data.document.content;
        state.baseVersion = data.document.version;
        state.current.updated_at = data.document.updated_at;
        state.current.updated_by = data.document.updated_by;
        state.dirty = false;
        state.conflict = false;
        state.serverDoc = null;
        $("#conflict").classList.add("hidden");
        renderContent();
        setSaveState("saved");
      })
      .catch(function (e) { alert("导入失败：" + e.message); setSaveState("idle"); })
      .then(function () { input.value = ""; });
  }

  /* ==================== 全屏切换 ==================== */
  function toggleFullscreen() {
    var isFs = false;
    $$(".view").forEach(function (v) {
      v.classList.toggle("fullscreen");
      if (v.classList.contains("fullscreen")) isFs = true;
    });
    $$(".btn-fullscreen").forEach(function (b) {
      b.classList.toggle("active", isFs);
      b.title = isFs ? "退出全屏" : "全屏显示";
      if (b.textContent.indexOf("全屏") > -1) b.textContent = isFs ? "⛶ 退出全屏" : "⛶ 全屏";
    });
  }

  /* ==================== 事件绑定 ==================== */
  function bindEvents() {
    $("#btn-create").addEventListener("click", openCreateModal);
    $("#create-cancel").addEventListener("click", closeCreateModal);
    $("#create-ok").addEventListener("click", submitCreate);
    $("#create-name").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); submitCreate(); }
    });
    $("#scope-cancel").addEventListener("click", function () {
      $("#scope-modal").classList.add("hidden");
      scopeDocId = null;
    });
    $("#scope-ok").addEventListener("click", submitScope);

    // 三个 Tab：本单位 / 本部门 / 我的（服务端已过滤，此处只是 UI 分组）
    $("#doc-tabs").addEventListener("click", function (e) {
      var tab = e.target.closest(".doc-tab");
      if (!tab) return;
      state.activeLevel = tab.dataset.level;
      $$("#doc-tabs .doc-tab").forEach(function (t) {
        t.classList.toggle("active", t === tab);
      });
      renderList();
    });

    // 弹窗：点遮罩或 Esc 关闭
    ["#create-modal", "#scope-modal"].forEach(function (sel) {
      var m = $(sel);
      m.addEventListener("mousedown", function (e) {
        if (e.target === m) m.classList.add("hidden");
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      $("#create-modal").classList.add("hidden");
      $("#scope-modal").classList.add("hidden");
    });

    $("#btn-back").addEventListener("click", showList);
    $("#btn-export").addEventListener("click", exportCurrent);
    $("#btn-import").addEventListener("click", function () { $("#file-input").click(); });
    $("#file-input").addEventListener("change", doImport);
    $$(".btn-fullscreen").forEach(function (b) {
      b.addEventListener("click", toggleFullscreen);
    });

    $("#btn-discard").addEventListener("click", discardLocal);
    $("#btn-overwrite").addEventListener("click", overwriteLocal);

    // Word 工具栏
    $("#word-toolbar").addEventListener("click", function (e) {
      var btn = e.target.closest(".tool-btn");
      if (!btn) return;
      $("#word-area").focus();
      var cmd = btn.getAttribute("data-cmd");
      var block = btn.getAttribute("data-block");
      if (cmd) {
        document.execCommand(cmd, false, null);
      } else if (block) {
        document.execCommand("formatBlock", false, "<" + block.toUpperCase() + ">");
      }
      markDirty();
      e.preventDefault();
    });

    // Word 输入
    $("#word-area").addEventListener("input", markDirty);

    // Excel 结构操作
    $("#excel-add-row").addEventListener("click", addRow);
    $("#excel-del-row").addEventListener("click", delRow);
    $("#excel-add-col").addEventListener("click", addCol);
    $("#excel-del-col").addEventListener("click", delCol);

    // Excel 单元格输入（事件委托）
    $("#excel-grid").addEventListener("input", function (e) {
      if (e.target.getAttribute("contenteditable") === "true") {
        gridToMatrix();
        markDirty();
      }
    });

    // Excel 列宽拖拽调整（事件委托）
    $("#excel-grid").addEventListener("mousedown", function (e) {
      var handle = e.target.closest(".col-resize");
      if (!handle) return;
      e.preventDefault();
      startColResize(parseInt(handle.getAttribute("data-c"), 10), e.clientX);
    });

    // 离开页面前提醒未保存修改
    window.addEventListener("beforeunload", function (e) {
      if (state.dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    });
  }

  function checkDeps() {
    api("/api/shared-docs/status").then(function (data) {
      var missing = [];
      if (!data.python_docx) missing.push("python-docx（Word 导入/导出）");
      if (!data.openpyxl) missing.push("openpyxl（Excel 导入/导出）");
      if (!data.xlrd) missing.push("xlrd（.xls 导入）");
      if (missing.length) {
        var el = $("#deps");
        el.classList.remove("hidden");
        el.textContent = "提示：未安装 " + missing.join("、") + "，在线编辑不受影响，但导入 / 导出 Office 文件将不可用。请执行：pip install " + missing.join(" ").replace(/（.*?）/g, "");
      }
    }).catch(function () {});
  }

  /* ==================== 初始化 ==================== */
  document.addEventListener("DOMContentLoaded", function () {
    bindEvents();
    checkDeps();
    // 当前登录用户（服务端权威）；用于卡片菜单权限显隐与创建弹窗提示
    if (window.AdminCommon && window.AdminCommon.getSession) {
      window.AdminCommon.getSession().then(function (u) {
        state.user = u;
        renderTabs();
        renderList();
      }).catch(function () {});
    }
    refreshList();
  });
})();
