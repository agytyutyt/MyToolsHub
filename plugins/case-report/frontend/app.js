/* 战果录入 —— 前端逻辑
 *
 * 流程：输入收网情况报告 → 大模型解析五要素 → 可编辑键值对 → 保存到本地 JSON 台账
 * - 解析走后台任务（task_id 轮询），仅支持大模型解析，未配置/失败时提示用户；
 * - 抽取结果以键值对表单呈现，可修改后再保存；
 * - 台账列表支持 复制JSON / 导出下载 / 删除。
 */
(function () {
  "use strict";

  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

  var FIELD_KEYS = ["案件名", "时间", "主办大队", "主办人", "抓获人数", "涉案价值", "缴获物品"];
  var POLL_MS = 1200;
  var POLL_TIMEOUT = 90000;

  var state = {
    taskTimer: null,
    sourceText: "",
    currentFields: {},
    records: [],
    caseNorm: "",   // 当前筛选案件（规整名），空串=全部案件
    caseName: "",   // 当前筛选案件的展示名
    scope: "mine",  // 查看范围：mine=我的战果 / dept=本部门 / all=全部(仅超管)
    from: "",       // 自定义开始日期 YYYY-MM-DD
    to: "",         // 自定义结束日期 YYYY-MM-DD
    dept: "",       // 部门筛选（部门ID）
    person: "",     // 用户筛选（用户名/姓名）
    user: null,     // 当前登录用户（AdminCommon.getSession() 取）
    orgUnits: [],   // 主办大队可选值（插件目录 org_units.json 固化）
  };

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

  /* ==================== Toast ==================== */
  var toastTimer = null;
  function toast(text, kind) {
    var el = $("#toast");
    el.textContent = text;
    el.className = "toast" + (kind ? " " + kind : "");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.add("hidden"); }, 2600);
  }

  /* ==================== 通用对话框 ==================== */
  function openDialog(title, bodyHtml, actions) {
    $("#dialogTitle").textContent = title;
    $("#dialogBody").innerHTML = bodyHtml;
    var box = $("#dialogActions");
    box.innerHTML = "";
    (actions || []).forEach(function (a) {
      box.appendChild(btn(a.label, a.handler, a.primary ? "btn btn-primary" : "btn"));
    });
    $("#dialog").classList.remove("hidden");
    var first = box.querySelector("button");
    if (first) first.focus();
  }

  function closeDialog() {
    $("#dialog").classList.add("hidden");
    $("#dialogBody").innerHTML = "";
    var dlg = $("#dialog .dialog");
    if (dlg) dlg.classList.remove("wide");
  }

  /* ==================== 大模型配置 ==================== */
  function loadConfig() {
    api("/api/case-report/config").then(function (data) {
      $("#baseUrl").value = data.base_url || "";
      // 安全：Key 不回传明文，仅展示掩码；输入框留空 = 保存时沿用原值
      $("#apiKey").value = "";
      $("#apiKey").placeholder = data.api_key_set
        ? ("已设置（" + (data.api_key_masked || "****") + "），留空则不修改")
        : "sk-...";
      $("#model").value = data.model || "";
      if (data.llm_configured) {
        $("#configTip").textContent = "✓ 已配置大模型，解析将走大模型";
      } else {
        $("#configTip").textContent = "⚠ 未配置完整大模型，无法解析报告";
      }
    }).catch(function () {});
  }

  function loadOrgUnits() {
    api("/api/case-report/org-units").then(function (data) {
      state.orgUnits = data.units || [];
    }).catch(function () {});
  }

  function saveConfig() {
    var btn = $("#saveConfig");
    btn.disabled = true;
    postJSON("/api/case-report/config", {
      base_url: $("#baseUrl").value,
      api_key: $("#apiKey").value,
      model: $("#model").value,
    }).then(function (data) {
      toast(data.llm_configured ? "配置已保存，解析将走大模型" : "配置已保存，但尚未配置完整，无法解析报告", "ok");
      $("#configTip").textContent = data.llm_configured ? "✓ 已配置大模型" : "⚠ 未配置完整大模型，无法解析报告";
      if (data.llm_configured) {
        $("#apiKey").value = "";
        $("#apiKey").placeholder = "已设置，留空则不修改";
      }
    }).catch(function (e) {
      toast("保存配置失败：" + e.message, "err");
    }).then(function () {
      btn.disabled = false;
    });
  }

  function testConfig() {
    var btn = $("#testConfig");
    var tip = $("#configTestTip");
    btn.disabled = true;
    tip.textContent = "测试中…";
    tip.className = "tip";
    postJSON("/api/case-report/config/test", {
      base_url: $("#baseUrl").value,
      api_key: $("#apiKey").value,
      model: $("#model").value,
    }).then(function (data) {
      if (data.ok) {
        tip.textContent = "✓ " + (data.detail || "连通正常");
        tip.className = "tip";
      } else {
        tip.textContent = "✗ " + (data.detail || "连通失败");
        tip.className = "tip err";
      }
    }).catch(function (e) {
      tip.textContent = "✗ " + e.message;
      tip.className = "tip err";
    }).then(function () {
      btn.disabled = false;
    });
  }

  /* ==================== 解析 ==================== */
  function setStatus(text, kind) {
    var el = $("#parseStatus");
    el.textContent = text || "";
    el.className = "toolbar-status" + (kind ? " " + kind : "");
  }

  function startParse() {
    var text = $("#reportText").value.trim();
    if (!text) { toast("请先输入收网情况报告", "err"); return; }
    state.sourceText = text;
    setStatus("正在解析…");
    $("#parseBtn").disabled = true;
    $("#clearBtn").disabled = true;
    postJSON("/api/case-report/parse", { text: text })
      .then(function (data) { pollResult(data.task_id); })
      .catch(function (e) {
        setStatus("", "");
        toast("提交解析失败：" + e.message, "err");
        $("#parseBtn").disabled = false;
        $("#clearBtn").disabled = false;
      });
  }

  function pollResult(taskId) {
    var start = Date.now();
    if (state.taskTimer) clearInterval(state.taskTimer);
    state.taskTimer = setInterval(function () {
      if (Date.now() - start > POLL_TIMEOUT) {
        clearInterval(state.taskTimer);
        state.taskTimer = null;
        setStatus("解析超时，请重试", "err");
        toast("解析超时，请重试", "err");
        $("#parseBtn").disabled = false;
        $("#clearBtn").disabled = false;
        return;
      }
      api("/api/case-report/result/" + taskId).then(function (data) {
        if (data.status === "done") {
          clearInterval(state.taskTimer);
          state.taskTimer = null;
          onParsed(data);
        } else if (data.status === "error") {
          clearInterval(state.taskTimer);
          state.taskTimer = null;
          setStatus("", "");
          toast(data.detail || "解析失败（请检查大模型配置）", "err");
          $("#parseBtn").disabled = false;
          $("#clearBtn").disabled = false;
        }
      }).catch(function () { /* 忽略瞬时错误，下轮重试 */ });
    }, POLL_MS);
  }

  function onParsed(data) {
    state.currentFields = data.fields || {};
    renderFields(state.currentFields);
    $("#resultCard").classList.remove("hidden");
    var badge = $("#methodBadge");
    badge.classList.remove("hidden");
    badge.textContent = "大模型解析";
    badge.title = "llm";
    setStatus("解析完成 ✓", "ok");
    $("#parseBtn").disabled = false;
    $("#clearBtn").disabled = false;
    renderItemsEditor(data.items || []);
    $("#resultCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /* ==================== 抽取结果表单 ==================== */
  function orgUnitOptions(sel) {
    var opts = "";
    opts += "<option value=''>（未指定）</option>";
    (state.orgUnits || []).forEach(function (u) {
      opts += "<option value='" + esc(u) + "'" + (sel === u ? " selected" : "") + ">" + esc(u) + "</option>";
    });
    return opts;
  }

  function renderFields(fields) {
    var grid = $("#fieldGrid");
    grid.innerHTML = "";
    FIELD_KEYS.forEach(function (k) {
      var row = document.createElement("div");
      row.className = "field-row";
      if (k === "缴获物品") row.classList.add("hidden");  // 明细在缴获物品明细编辑器里编辑
      var key = document.createElement("div");
      key.className = "key";
      key.textContent = k;
      var val = fields[k] == null ? "" : fields[k];
      // 主办人：解析/兜底未给时默认填当前登录用户姓名
      if (!val && k === "主办人" && state.user && state.user.name) {
        val = state.user.name;
      }
      if (k === "主办大队") {
        var sel = document.createElement("select");
        sel.dataset.field = k;
        sel.innerHTML = orgUnitOptions(val);
        row.appendChild(key);
        row.appendChild(sel);
        grid.appendChild(row);
        return;
      }
      var input = document.createElement("input");
      input.dataset.field = k;
      input.value = val;
      input.placeholder = k === "抓获人数" ? "如：5" : (k === "涉案价值" ? "如：80万元" : "");
      row.appendChild(key);
      row.appendChild(input);
      grid.appendChild(row);
    });
  }

  function collectFields() {
    var fields = {};
    $("#fieldGrid").querySelectorAll("[data-field]").forEach(function (el) {
      fields[el.dataset.field] = el.value.trim();
    });
    return fields;
  }

  /* ==================== 保存入库（含同案合并检测） ==================== */
  function saveEntry() {
    var fields = collectFields();
    var has = FIELD_KEYS.some(function (k) { return fields[k]; });
    if (!has) { toast("没有任何要素，请先解析或补充字段", "err"); return; }
    doSave("auto");
  }

  function doSave(mode, matched) {
    var btn = $("#saveEntryBtn");
    btn.disabled = true;
    var body = {
      fields: collectFields(),
      source_text: state.sourceText,
      items: collectItems(),
    };
    if (mode === "merge") {
      body.merge_mode = "merge";
      body.merge_case = matched.name;
    } else if (mode === "new") {
      body.merge_mode = "new";
    }
    postJSON("/api/case-report/records", body).then(function (data) {
      if (data.duplicate && data.matches && data.matches.length) {
        btn.disabled = false;
        showMergeDialog(data.matches);
        return;
      }
      if (mode === "merge") {
        toast("战果已并入案件「" + (data.record.fields["案件名"] || "") + "」", "ok");
      } else {
        toast("已入库：" + (data.record.fields["案件名"] || "战果记录"), "ok");
      }
      syncCaseView();
      loadCategories();
      $("#resultCard").classList.add("hidden");  // 入库后不再展示抽取结果卡片
    }).catch(function (e) {
      toast("保存失败：" + e.message, "err");
    }).then(function () {
      btn.disabled = false;
    });
  }

  /* 同案合并确认：后端检测到规整后案件名已存在时弹出 */
  function showMergeDialog(matches) {
    var m = matches[0] || {};
    var name = m.name || "";
    var body = "<p class='merge-tip'>检测到台账中已存在相同案件「<b>" + esc(name) + "</b>」（" +
      matches.length + " 条已入库记录，最近一条：" + esc(m.created_at || "") + "）。" +
      "</p><p class='merge-tip'>选择<b>并入</b>后，本次战果归入该案件，战果汇总「涉及 N 起」按案件去重计算；" +
      "选择<b>作为新案件</b>则保留为一条独立台账记录。</p>";
    openDialog("检测到相同案件", body, [
      { label: "并入该案件", primary: true, handler: function () { closeDialog(); doSave("merge", m); } },
      { label: "作为新案件保存", handler: function () { closeDialog(); doSave("new"); } },
      { label: "取消", handler: closeDialog },
    ]);
  }

  /* ==================== 数值/战果显示 ==================== */
  function fmtNum(n) {
    if (n == null) return "";
    var v = Math.abs(n);
    var s = (v % 1 === 0) ? String(v) : String(+v.toFixed(2));
    var parts = s.split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (n < 0 ? "-" : "") + parts.join(".");
  }

  function itemQtyHtml(it) {
    if (it.quantity == null) {
      return "<span class='v'>" + esc(it.name || "") + "（" + esc(it.unit || "若干") + "）</span>";
    }
    return "<span class='v'>" + esc(it.name || "") + " × " + fmtNum(it.quantity) +
      (it.unit ? " " + esc(it.unit) : "") + "</span>";
  }

  /* ==================== 缴获物品明细编辑器（可改类别，改动即持久化） ==================== */
  function renderItemsEditor(items) {
    var wrap = $("#itemsEditorWrap");
    var box = $("#itemsEditor");
    box.innerHTML = "";
    wrap.classList.remove("hidden");  // 缴获物品明细默认展开显示
    (items || []).forEach(function (it) { box.appendChild(buildItemRow(it)); });
  }

  function buildItemRow(it) {
    it = it || {};
    var row = document.createElement("div");
    row.className = "item-row";

    var cat = document.createElement("input");
    cat.className = "ie-cat";
    cat.setAttribute("list", "catOptions");
    cat.placeholder = "类别";
    cat.value = it.category || "";
    cat.title = "修改并失焦后自动记住该类别";

    var name = document.createElement("input");
    name.className = "ie-name";
    name.placeholder = "物品名称";
    name.value = it.name || "";

    var qty = document.createElement("input");
    qty.className = "ie-qty";
    qty.type = "number";
    qty.min = "0";
    qty.step = "any";
    qty.placeholder = "数量";
    if (it.quantity != null) qty.value = it.quantity;

    var unit = document.createElement("input");
    unit.className = "ie-unit";
    unit.placeholder = "单位";
    unit.value = it.unit || "";
    unit.title = "如：台 / 部 / 克 / 万元；留空表示若干";

    var del = document.createElement("button");
    del.type = "button";
    del.className = "ie-del";
    del.textContent = "✕";
    del.title = "删除该条目";
    del.addEventListener("click", function () { row.remove(); });

    cat.addEventListener("change", function () {
      learnCategory(name.value.trim(), cat.value.trim());
    });
    name.addEventListener("change", function () {
      learnCategory(name.value.trim(), cat.value.trim());
    });

    row.appendChild(cat);
    row.appendChild(name);
    row.appendChild(qty);
    row.appendChild(unit);
    row.appendChild(del);
    return row;
  }

  function collectItemsFrom(sel) {
    var out = [];
    document.querySelectorAll(sel).forEach(function (row) {
      var name = row.querySelector(".ie-name").value.trim();
      if (!name) return;
      var qtyEl = row.querySelector(".ie-qty");
      var qty = null;
      if (qtyEl.value !== "") qty = parseFloat(qtyEl.value);
      out.push({
        category: row.querySelector(".ie-cat").value.trim() || "其他",
        name: name,
        quantity: (isFinite(qty) ? qty : null),
        unit: row.querySelector(".ie-unit").value.trim(),
      });
    });
    return out;
  }

  function collectItems() {
    return collectItemsFrom("#itemsEditor .item-row");
  }

  var learnTimer = null;
  function learnCategory(name, category) {
    if (!name || !category) return;
    if (learnTimer) clearTimeout(learnTimer);
    learnTimer = setTimeout(function () {
      postJSON("/api/case-report/categories", { name: name, category: category })
        .then(function (data) {
          refreshCatOptions(data.learned || {});
          toast("已记住「" + name + " → " + category + "」，后续解析将优先采用", "ok");
        })
        .catch(function () {});
    }, 300);
  }

  function loadCategories() {
    api("/api/case-report/categories").then(function (data) {
      refreshCatOptions(data.learned || {});
      renderCatKnown(data.known || []);
      state.catKnown = data.known || [];
    }).catch(function () {});
  }

  function refreshCatOptions(learned) {
    var used = (state.catKnown || []).slice();
    Object.keys(learned || {}).forEach(function (k) { used.push(learned[k]); });
    used = used.filter(function (c, i) { return c && used.indexOf(c) === i; });
    renderCatKnown(used.sort(function (a, b) { return a === "其他" ? 1 : b === "其他" ? -1 : a < b ? -1 : 1; }));
  }

  function renderCatKnown(cats) {
    var dl = $("#catOptions");
    dl.innerHTML = "";
    cats.forEach(function (c) {
      var opt = document.createElement("option");
      opt.value = c;
      dl.appendChild(opt);
    });
  }

  /* ==================== 战果汇总 ==================== */
  function refreshSummary() {
    api("/api/case-report/aggregate" + caseQuery()).then(function (data) {
      var rows = data.categories || [];
      var list = $("#summaryList");
      list.innerHTML = "";
      var scope = state.caseName
        ? "案件「" + state.caseName + "」 · "
        : "全部案件 · ";
      $("#summaryCount").textContent = rows.length
        ? scope + "共 " + rows.length + " 类"
        : scope + "暂无数据";
      $("#summaryEmpty").classList.toggle("hidden", rows.length > 0);
      rows.forEach(function (r) {
        var el = document.createElement("div");
        el.className = "summary-item";
        var qtyHtml;
        if (r.quantity == null) {
          qtyHtml = "<span class='total'>若干</span>";
        } else {
          var q = fmtNum(r.quantity);
          var unit = esc(r.unit || "");
          if (r.unit === "元" && r.quantity >= 10000) {
            q = fmtNum(Math.round(r.quantity / 10000 * 100) / 100);
            unit = "万元";
          }
          qtyHtml = "<span class='total'>" + q + "</span><span class='meta'>" + unit + "</span>";
        }
        el.innerHTML = "<span class='cat'>" + esc(r.category) + "</span>" + qtyHtml +
          "<span class='meta'>涉及 " + (r.records || 0) + " 起</span>";
        list.appendChild(el);
      });
    }).catch(function () {});
  }

  /* ==================== 台账列表（可按案件筛选） ==================== */
  function caseQuery() {
    var parts = [];
    if (state.caseNorm) parts.push("case=" + encodeURIComponent(state.caseNorm));
    if (state.scope && state.scope !== "mine") parts.push("scope=" + state.scope);
    if (state.from) parts.push("from=" + encodeURIComponent(state.from));
    if (state.to) parts.push("to=" + encodeURIComponent(state.to));
    if (state.dept) parts.push("dept=" + encodeURIComponent(state.dept));
    if (state.person) parts.push("user=" + encodeURIComponent(state.person));
    return parts.length ? "?" + parts.join("&") : "";
  }

  /* 载入部门/用户筛选项（来自管理后台组织树） */
  function loadOrgOptions() {
    var deptSel = $("#deptFilter");
    var userSel = $("#userFilter");
    if (!deptSel || !userSel) return Promise.resolve();
    // 部门筛选：以「主办大队」为检索字段（一大队/二大队/三大队，来自 org_units.json）
    return api("/api/case-report/org-units").then(function (unitData) {
      var units = unitData.units || [];
      deptSel.innerHTML = '<option value="">全部部门</option>' +
        units.map(function (u) { return "<option value='" + esc(u) + "'>" + esc(u) + "</option>"; }).join("");
      if (state.dept) deptSel.value = state.dept;
    }).catch(function () {}).then(function () {
      // 主办人筛选：以「主办人」为检索字段（姓名，来自组织树用户）
      return api("/api/admin/org-tree").then(function (data) {
        var users = [];
        (data.tree || []).forEach(function (unit) {
          (unit.children || []).forEach(function (d) {
            (d.children || []).forEach(function (u) {
              users.push({ name: u.name, id: u.id });
            });
          });
        });
        var userMap = {};
        users.forEach(function (u) { if (u.name && !userMap[u.name]) userMap[u.name] = u; });
        users = Object.keys(userMap).map(function (k) { return userMap[k]; });
        userSel.innerHTML = '<option value="">全部人员</option>' +
          users.map(function (u) { return "<option value='" + esc(u.name) + "'>" + esc(u.name) + "</option>"; }).join("");
        if (state.person) userSel.value = state.person;
      }).catch(function () {});
    });
  }

  /* 应用时间筛选后统一刷新（案件侧边栏、台账、汇总） */
  function applyPeriod() {
    var from = $("#fromDate").value;
    var to = $("#toDate").value;
    if (from && to && from > to) { toast("开始日期不能晚于结束日期", "err"); return; }
    state.from = from;
    state.to = to;
    state.caseNorm = "";
    state.caseName = "";
    refreshCases().then(function () {
      refreshRecords();
      refreshSummary();
    });
  }

  function clearPeriod() {
    state.from = "";
    state.to = "";
    $("#fromDate").value = "";
    $("#toDate").value = "";
    applyPeriod();
  }

  /* 重建左侧案件侧边栏（后端已按拼音排序），并保持当前选中高亮 */
  function refreshCases() {
    var q = [];
    if (state.scope && state.scope !== "mine") q.push("scope=" + state.scope);
    if (state.from) q.push("from=" + encodeURIComponent(state.from));
    if (state.to) q.push("to=" + encodeURIComponent(state.to));
    return api("/api/case-report/cases" + (q.length ? "?" + q.join("&") : "")).then(function (data) {
      var box = $("#caseList");
      box.innerHTML = "";
      box.appendChild(buildCaseItem("全部案件", "", 0));
      (data.cases || []).forEach(function (c) {
        box.appendChild(buildCaseItem(c.name, c.normalized || c.name, c.records));
      });
      // 若选中案件已不存在（记录被删/改名），回落「全部案件」
      if (state.caseNorm) {
        var still = Array.prototype.some.call(box.children, function (ch) {
          return ch.dataset.norm === state.caseNorm;
        });
        if (!still) {
          state.caseNorm = "";
          state.caseName = "";
          var all = box.querySelector(".case-item");
          Array.prototype.forEach.call(box.children, function (ch) {
            ch.classList.toggle("active", ch === all);
          });
        }
      }
    });
  }

  function buildCaseItem(label, norm, records) {
    var el = document.createElement("button");
    el.type = "button";
    el.className = "case-item";
    el.dataset.norm = norm || "";
    var name = document.createElement("span");
    name.className = "ci-name";
    name.textContent = label;
    var cnt = document.createElement("span");
    cnt.className = "ci-count";
    cnt.textContent = records ? records + " 条" : "";
    el.appendChild(name);
    el.appendChild(cnt);
    var isActive = function () { return state.caseNorm === (norm || ""); };
    el.classList.toggle("active", isActive());
    el.addEventListener("click", function () {
      state.caseNorm = norm || "";
      state.caseName = label === "全部案件" ? "" : label;
      Array.prototype.forEach.call(el.parentNode.children, function (ch) {
        ch.classList.toggle("active", ch === el);
      });
      refreshRecords();
      refreshSummary();
    });
    return el;
  }

  /* 重建侧边栏后再刷新台账与汇总（保存/删除/改名后用） */
  function syncCaseView() {
    return refreshCases().then(function () {
      refreshRecords();
      refreshSummary();
    });
  }

  function refreshRecords() {
    api("/api/case-report/records" + caseQuery()).then(function (data) {
      state.records = data.records || [];
      renderRecords();
    }).catch(function () {});
  }

  function renderRecords() {
    var box = $("#recordList");
    box.innerHTML = "";
    var count = $("#recordCount");
    count.textContent = state.records.length
      ? (state.caseName ? "案件「" + state.caseName + "」共 " : "共 ") + state.records.length + " 条"
      : "";
    $("#emptyTip").classList.toggle("hidden", state.records.length > 0);

    state.records.forEach(function (rec) {
      var f = rec.fields || {};
      var card = document.createElement("div");
      card.className = "record-card";

      var title = document.createElement("div");
      title.className = "record-title";
      var caseName = document.createElement("span");
      caseName.className = "case";
      caseName.textContent = f["案件名"] || ("记录 " + rec.id);
      var time = document.createElement("span");
      time.className = "time";
      time.textContent = (f["时间"] || "") + " · 入库 " + (rec.created_at || "");
      title.appendChild(caseName);
      title.appendChild(time);
      card.appendChild(title);

      var chips = document.createElement("div");
      chips.className = "kv-chips";
      // 主办大队、主办人常驻显示（字段空缺则显示为空）；抓获人数有值才显示
      [["主办大队", "主办大队"], ["主办人", "主办人"]].forEach(function (pair) {
        var chip = document.createElement("span");
        chip.className = "kv-chip";
        chip.innerHTML = "<b>" + esc(pair[1]) + "：</b><span class='v'>" + esc(f[pair[0]] || "") + "</span>";
        chips.appendChild(chip);
      });
      if (f["抓获人数"] != null && f["抓获人数"] !== "") {
        var chip = document.createElement("span");
        chip.className = "kv-chip";
        chip.innerHTML = "<b>抓获人数：</b><span class='v'>" + esc(f["抓获人数"]) + "</span>";
        chips.appendChild(chip);
      }
      // 涉案价值：未解析出内容则为空，台账中为空时不显示
      if (f["涉案价值"] != null && f["涉案价值"] !== "") {
        var chip = document.createElement("span");
        chip.className = "kv-chip";
        chip.innerHTML = "<b>涉案价值：</b><span class='v'>" + esc(f["涉案价值"]) + "</span>";
        chips.appendChild(chip);
      }
      // 缴获物品逐项单列（类似物品归类）
      var items = rec.items || [];
      if (items.length) {
        items.forEach(function (it) {
          var chip = document.createElement("span");
          chip.className = "kv-chip item-chip";
          chip.innerHTML = "<b>" + esc(it.category || "其他") + "：</b>" + itemQtyHtml(it);
          chips.appendChild(chip);
        });
      } else if (f["缴获物品"]) {
        var chip = document.createElement("span");
        chip.className = "kv-chip";
        chip.innerHTML = "<b>缴获物品：</b><span class='v'>" + esc(f["缴获物品"]) + "</span>";
        chips.appendChild(chip);
      }
      card.appendChild(chips);

      var src = document.createElement("div");
      src.className = "source-collapse";
      var toggleBtn = document.createElement("button");
      toggleBtn.className = "source-toggle";
      toggleBtn.textContent = "查看原始报告";
      var body = document.createElement("div");
      body.className = "source-body hidden";
      body.textContent = rec.source_text || "（无原始报告）";
      toggleBtn.addEventListener("click", function () {
        var hidden = body.classList.toggle("hidden");
        toggleBtn.textContent = hidden ? "查看原始报告" : "收起原始报告";
      });
      src.appendChild(toggleBtn);
      src.appendChild(body);
      card.appendChild(src);

      var actions = document.createElement("div");
      actions.className = "record-actions";
      actions.appendChild(btn("复制 JSON", function () { copyJSON(rec); }));
      actions.appendChild(btn("导出 JSON", function () { exportJSON(rec.id); }));
      // 「删除」「改案件名」仅录入人本人或超管可用（前端显隐只是体验，后端有 403 兜底）
      var canManage = !!(state.user && (state.user.super_admin || rec.created_by === state.user.username));
      if (canManage) {
        actions.appendChild(btn("编辑", function () { openEditRecord(rec); }));
        actions.appendChild(btn("修改案件名", function () { editCaseName(rec); }));
        actions.appendChild(btn("删除", function () { deleteRecord(rec); }, "btn btn-danger"));
      }
      card.appendChild(actions);

      box.appendChild(card);
    });
  }

  function btn(text, handler, cls) {
    var b = document.createElement("button");
    b.className = cls || "btn";
    b.textContent = text;
    b.addEventListener("click", handler);
    return b;
  }

  function copyJSON(rec) {
    var json = JSON.stringify(rec, null, 2);
    function done() { toast("JSON 已复制到剪贴板", "ok"); }
    function fail() {
      var ta = document.createElement("textarea");
      ta.value = json;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { toast("复制失败，请手动选择复制", "err"); }
      ta.remove();
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(json).then(done).catch(fail);
    } else {
      fail();
    }
  }

  function exportJSON(id) {
    var a = document.createElement("a");
    a.href = "/api/case-report/records/" + id + "/download";
    a.download = "case-" + id + ".json";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  /* ==================== 台账改案件名（新建案件 / 并入既有案件） ==================== */
  function editCaseName(rec) {
    api("/api/case-report/cases" + caseQuery()).then(function (data) {
      var cases = data.cases || [];
      var opts = cases.map(function (c) {
        return "<option value='" + esc(c.name) + "'>" + esc(c.name) + "（" + c.records + " 条）</option>";
      }).join("");
      var cur = rec.fields["案件名"] || "";
      var body = "<p class='merge-tip' style='margin-top:0'>从既有案件中选择并入，或输入新案件名新建案件。" +
        "改后「战果汇总」的 涉及 N 起 会按案件自动重算。" +
        "</p>" +
        "<div class='dl-body'>" +
        "<div><div class='dl-label'>既有案件（选择后自动填入）</div>" +
        "<select id='dlCasePick'><option value=''>— 输入新案件名 —</option>" + opts + "</select></div>" +
        "<div><div class='dl-label'>案件名</div>" +
        "<input id='dlCaseInput' value='" + esc(cur) + "' placeholder='如：2.11开设赌场案' /></div>" +
        "</div>";
      openDialog("修改案件名", body, [
        { label: "保存", primary: true, handler: function () { doSaveCaseName(rec.id); } },
        { label: "取消", handler: closeDialog },
      ]);
      var pick = $("#dlCasePick");
      var inp = $("#dlCaseInput");
      pick.addEventListener("change", function () {
        if (pick.value) inp.value = pick.value;
      });
      inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); doSaveCaseName(rec.id); }
      });
      inp.focus();
    }).catch(function () {});
  }

  function doSaveCaseName(rid) {
    var name = $("#dlCaseInput").value.trim();
    if (!name) { toast("请输入案件名或选择既有案件", "err"); return; }
    api("/api/case-report/records/" + rid + "/case", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case_name: name }),
    }).then(function (data) {
      toast("已修改案件名：「" + (data.record.fields["案件名"] || "") + "」", "ok");
      closeDialog();
      syncCaseView();
    }).catch(function (e) {
      toast("修改失败：" + e.message, "err");
    });
  }

  /* ==================== 台账编辑（完整记录） ==================== */
  function openEditRecord(rec) {
    var f = rec.fields || {};
    var fieldHtml = FIELD_KEYS.map(function (k) {
      if (k === "缴获物品") return "";
      var v = f[k] == null ? "" : esc(f[k]);
      if (k === "主办大队") {
        return "<div class='field-row'><div class='key'>" + esc(k) + "</div>" +
               "<select data-field='" + esc(k) + "'>" + orgUnitOptions(v) + "</select></div>";
      }
      return "<div class='field-row'><div class='key'>" + esc(k) + "</div>" +
             "<input data-field='" + esc(k) + "' value='" + v + "' /></div>";
    }).join("");
    var srcHtml = "<textarea class='edit-source' id='editSource'>" + esc(rec.source_text || "") + "</textarea>";
    var itemsHtml = "<div id='editItemRows' class='items-editor-list'></div>" +
      "<div class='row-actions' style='margin-top:8px'><button id='editAddItem' class='btn'>＋ 添加条目</button></div>" +
      "<div class='dl-label' style='margin-top:12px'>原始报告</div>" + srcHtml;
    var body = "<div class='edit-field-grid'>" + fieldHtml + "</div>" +
      "<div class='dl-label' style='margin-top:12px'>缴获物品明细（可修改类别/名称/数量/单位）</div>" + itemsHtml;
    openDialog("编辑战果记录", body, [
      { label: "保存", primary: true, handler: function () { doSaveEdit(rec.id); } },
      { label: "取消", handler: closeDialog },
    ]);
    var rowsBox = $("#editItemRows");
    (rec.items || []).forEach(function (it) { rowsBox.appendChild(buildItemRow(it)); });
    $("#editAddItem").addEventListener("click", function () {
      rowsBox.appendChild(buildItemRow({}));
    });
    var dlg = $("#dialog .dialog");
    if (dlg) dlg.classList.add("wide");
  }

  function doSaveEdit(rid) {
    var fields = {};
    $$("#dialog [data-field]").forEach(function (el) {
      fields[el.dataset.field] = el.value.trim();
    });
    var has = FIELD_KEYS.some(function (k) { return fields[k]; });
    if (!has) { toast("没有任何要素可保存", "err"); return; }
    var body = { fields: fields, items: collectItemsFrom("#editItemRows .item-row") };
    var src = $("#editSource");
    if (src) body.source_text = src.value;
    api("/api/case-report/records/" + rid, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (data) {
      toast("已保存修改", "ok");
      closeDialog();
      loadCategories();
      syncCaseView();
    }).catch(function (e) {
      toast("保存失败：" + e.message, "err");
    });
  }

  function deleteRecord(rec) {
    var name = (rec.fields && rec.fields["案件名"]) || rec.id;
    if (!confirm("确定删除战果记录「" + name + "」？该操作不可恢复。")) return;
    api("/api/case-report/records/" + rec.id, { method: "DELETE" })
      .then(function () {
        toast("已删除", "ok");
        syncCaseView();
      })
      .catch(function (e) { toast("删除失败：" + e.message, "err"); });
  }

  /* ==================== 事件绑定 ==================== */
  function bindEvents() {
    $("#parseBtn").addEventListener("click", startParse);
    $("#reparseBtn").addEventListener("click", startParse);
    $("#clearBtn").addEventListener("click", function () {
      $("#reportText").value = "";
      setStatus("", "");
      $("#resultCard").classList.add("hidden");
    });
    $("#saveEntryBtn").addEventListener("click", saveEntry);
    $("#saveConfig").addEventListener("click", saveConfig);
    $("#testConfig").addEventListener("click", testConfig);
    $("#refreshRecords").addEventListener("click", function () {
      refreshRecords();
      refreshSummary();
    });
    $("#addItemBtn").addEventListener("click", function () {
      $("#itemsEditorWrap").classList.remove("hidden");
      $("#itemsEditor").appendChild(buildItemRow({}));
    });

    $("#reportText").addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        startParse();
      }
    });

    // 缴获物品字段改动时实时预览拆分归类
    // 作用域切换：我的战果 / 本部门 / 全部（仅超管显示「全部」）
    $("#scope-toggle").addEventListener("click", function (e) {
      var btn = e.target.closest(".scope-btn");
      if (!btn) return;
      state.scope = btn.dataset.scope;
      $$(".scope-btn").forEach(function (b) {
        b.classList.toggle("active", b === btn);
      });
      state.caseNorm = "";
      state.caseName = "";
      // 重建案件侧边栏与台账/汇总
      refreshCases().then(function () {
        refreshRecords();
        refreshSummary();
      });
    });

    // 时间筛选：自定义起止日期
    $("#applyPeriod").addEventListener("click", applyPeriod);
    $("#clearPeriod").addEventListener("click", clearPeriod);

    // 部门/人员筛选
    $("#deptFilter").addEventListener("change", function () {
      state.dept = this.value || "";
      state.caseNorm = "";
      state.caseName = "";
      refreshCases().then(function () { refreshRecords(); refreshSummary(); });
    });
    $("#userFilter").addEventListener("change", function () {
      state.person = this.value || "";
      state.caseNorm = "";
      state.caseName = "";
      refreshCases().then(function () { refreshRecords(); refreshSummary(); });
    });

    // 导出 Excel
    $("#exportExcel").addEventListener("click", function () {
      var url = "/api/case-report/export" + caseQuery();
      var a = document.createElement("a");
      a.href = url;
      a.download = "case-report.xlsx";
      document.body.appendChild(a);
      a.click();
      a.remove();
      toast("正在导出…", "ok");
    });

    // 对话框：点遮罩或按 Esc 关闭
    var dialog = $("#dialog");
    dialog.addEventListener("mousedown", function (e) {
      if (e.target === dialog) closeDialog();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !dialog.classList.contains("hidden")) closeDialog();
    });
  }

  /* ==================== 初始化 ==================== */
  document.addEventListener("DOMContentLoaded", function () {
    bindEvents();
    loadConfig();
    loadOrgUnits();
    loadCategories();
    loadOrgOptions();
    // 当前登录用户（服务端权威）：决定「全部」范围可见性与台账操作按钮显隐
    if (window.AdminCommon && window.AdminCommon.getSession) {
      window.AdminCommon.getSession().then(function (u) {
        state.user = u;
        var allBtn = $("#scope-all");
        if (u && u.super_admin) {
          allBtn.classList.remove("hidden");
        } else {
          allBtn.classList.add("hidden");
        }
        refreshRecords();
      }).catch(function () {});
    }
    syncCaseView();
  });
})();