/* 战果录入 —— 前端逻辑
 *
 * 流程：输入收网情况报告 → 后端抽取五要素 → 可编辑键值对 → 保存到本地 JSON 台账
 * - 解析走后台任务（task_id 轮询），大模型未配置时后端自动回落本地规则解析；
 * - 抽取结果以键值对表单呈现，可修改后再保存；
 * - 台账列表支持 复制JSON / 导出下载 / 删除。
 */
(function () {
  "use strict";

  var $ = function (s) { return document.querySelector(s); };

  var FIELD_KEYS = ["案件名", "时间", "主办大队", "抓获人数", "缴获物品"];
  var POLL_MS = 1200;
  var POLL_TIMEOUT = 90000;

  var state = {
    taskTimer: null,
    sourceText: "",
    currentFields: {},
    records: [],
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

  /* ==================== 大模型配置 ==================== */
  function loadConfig() {
    api("/api/case-report/config").then(function (data) {
      $("#baseUrl").value = data.base_url || "";
      $("#apiKey").value = data.api_key || "";
      $("#model").value = data.model || "";
      if (data.llm_configured) {
        $("#configTip").textContent = "✓ 已配置大模型，解析将优先走大模型";
      } else {
        $("#configTip").textContent = "使用本地规则解析";
      }
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
      toast(data.llm_configured ? "配置已保存，将优先使用大模型解析" : "配置已保存，当前使用本地规则解析", "ok");
      $("#configTip").textContent = data.llm_configured ? "✓ 已配置大模型" : "使用本地规则解析";
    }).catch(function (e) {
      toast("保存配置失败：" + e.message, "err");
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
          toast((data.detail || "解析失败") + "，已改用本地规则解析", "err");
          fallbackParse();
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
    var method = data.method || "rules";
    var badge = $("#methodBadge");
    badge.classList.remove("hidden");
    var map = {
      "llm": "大模型解析",
      "llm+rules": "大模型解析（本地规则补全）",
      "rules": "本地规则解析（未配置大模型）",
    };
    badge.textContent = map[method] || method;
    badge.title = method;
    if (data.llm_error) {
      var line = $(".method-note");
      if (!line) {
        line = document.createElement("p");
        line.className = "muted method-note";
        $("#resultCard .result-head").after(line);
      }
      line.textContent = "提示：大模型解析未成功（" + data.llm_error + "），已改用本地规则补全。";
    }
    setStatus("解析完成 ✓", "ok");
    $("#parseBtn").disabled = false;
    $("#clearBtn").disabled = false;
    renderItemsEditor(data.items || []);
    $("#resultCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function fallbackParse() {
    /* 后端口径：error 分支不会返回字段，这里用本地规则兜底解析 */
    try {
      var fields = localRulesParse(state.sourceText || $("#reportText").value);
      renderFields(fields);
      $("#resultCard").classList.remove("hidden");
      $("#methodBadge").textContent = "本地规则解析（兜底）";
      $("#methodBadge").classList.remove("hidden");
      setStatus("部分字段未能抽取，请手动补充", "err");
    } catch (e) {
      setStatus("", "");
    }
  }

  /* 本地规则兜底（仅前端辅助，与后端 parser.py 逻辑一致的精简版） */
  function localRulesParse(text) {
    var fields = { "案件名": "", "时间": "", "主办大队": "", "抓获人数": "", "缴获物品": "" };
    if (!text) return fields;
    var m;
    m = text.match(/(?:主办|承办|牵头)(?:单位)?[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9]{1,10}(?:支队|大队|中队|专班|专案组|工作组))/);
    if (m) fields["主办大队"] = m[1];
    m = text.match(/(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?/) || text.match(/\d{1,2}\s*月\s*\d{1,2}\s*日/);
    if (m) fields["时间"] = m[0];
    m = text.match(/[“"]([^”"]{1,40})[”"]\s*(?:专案|系列案|案)?/) ||
        text.match(/(?:案件名|案名|案件名称)[:：]+\s*([^\s，。,;；]{1,40})/);
    if (m) fields["案件名"] = m[1] || m[0];
    m = text.match(/(?:抓获|抓捕|到案|落网|刑事拘留|刑拘|带回|审查)(?:犯罪嫌疑人|嫌疑人|涉案人员|人员)?(?:共)?([0-9一二三四五六七八九十百两]+)名?/);
    if (m) fields["抓获人数"] = cn2num(m[1]);
    m = text.match(/缴获([^。；;\n]{1,120})/);
    if (m) fields["缴获物品"] = m[1].replace(/等[、，。\s]*$/, "");
    return fields;
  }

  function cn2num(s) {
    if (/^\d+$/.test(s)) return String(parseInt(s, 10));
    var digits = { "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9 };
    var total = 0, num = 0;
    for (var i = 0; i < s.length; i++) {
      var ch = s[i];
      if (ch in digits) { num = digits[ch]; }
      else if (ch === "十") { total += (num || 1) * 10; num = 0; }
      else if (ch === "百") { total += (num || 1) * 100; num = 0; }
      else return s;
    }
    total += num;
    return total ? String(total) : s;
  }

  /* ==================== 抽取结果表单 ==================== */
  function renderFields(fields) {
    var grid = $("#fieldGrid");
    grid.innerHTML = "";
    FIELD_KEYS.forEach(function (k) {
      var row = document.createElement("div");
      row.className = "field-row";
      var key = document.createElement("div");
      key.className = "key";
      key.textContent = k;
      var input = document.createElement("input");
      input.dataset.field = k;
      input.value = fields[k] == null ? "" : fields[k];
      input.placeholder = k === "抓获人数" ? "如：5" : "";
      row.appendChild(key);
      row.appendChild(input);
      grid.appendChild(row);
    });
  }

  function collectFields() {
    var fields = {};
    $("#fieldGrid").querySelectorAll("input[data-field]").forEach(function (input) {
      fields[input.dataset.field] = input.value.trim();
    });
    return fields;
  }

  /* ==================== 保存入库 ==================== */
  function saveEntry() {
    var fields = collectFields();
    var has = FIELD_KEYS.some(function (k) { return fields[k]; });
    if (!has) { toast("没有任何要素，请先解析或补充字段", "err"); return; }
    var btn = $("#saveEntryBtn");
    btn.disabled = true;
    postJSON("/api/case-report/records", {
      fields: fields,
      source_text: state.sourceText,
      items: collectItems(),
    }).then(function (data) {
      toast("已入库：" + (data.record.fields["案件名"] || "战果记录"), "ok");
      refreshRecords();
      refreshSummary();
      loadCategories();
    }).catch(function (e) {
      toast("保存失败：" + e.message, "err");
    }).then(function () {
      btn.disabled = false;
    });
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
  var itemsTimer = null;
  function scheduleItemsPreview() {
    if (itemsTimer) clearTimeout(itemsTimer);
    itemsTimer = setTimeout(function () {
      var input = document.querySelector('#fieldGrid input[data-field="缴获物品"]');
      if (!input) return;
      var text = input.value.trim();
      if (!text) {
        $("#itemsEditorWrap").classList.add("hidden");
        $("#itemsEditor").innerHTML = "";
        return;
      }
      postJSON("/api/case-report/items", { text: text })
        .then(function (data) { renderItemsEditor(data.items || []); })
        .catch(function () {});
    }, 400);
  }

  function renderItemsEditor(items) {
    var wrap = $("#itemsEditorWrap");
    var box = $("#itemsEditor");
    box.innerHTML = "";
    if (!items.length) { wrap.classList.add("hidden"); return; }
    wrap.classList.remove("hidden");
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

  function collectItems() {
    var out = [];
    document.querySelectorAll("#itemsEditor .item-row").forEach(function (row) {
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
    api("/api/case-report/aggregate").then(function (data) {
      var rows = data.categories || [];
      var list = $("#summaryList");
      list.innerHTML = "";
      $("#summaryCount").textContent = rows.length ? "共 " + rows.length + " 类" : "";
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

  /* ==================== 台账列表 ==================== */
  function refreshRecords() {
    api("/api/case-report/records").then(function (data) {
      state.records = data.records || [];
      renderRecords();
    }).catch(function () {});
  }

  function renderRecords() {
    var box = $("#recordList");
    box.innerHTML = "";
    var count = $("#recordCount");
    count.textContent = state.records.length ? "共 " + state.records.length + " 条" : "";
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
      [["主办大队", "主办大队"], ["抓获人数", "抓获人数"]].forEach(function (pair) {
        var v = f[pair[0]];
        if (v === "" || v == null) return;
        var chip = document.createElement("span");
        chip.className = "kv-chip";
        chip.innerHTML = "<b>" + esc(pair[1]) + "：</b><span class='v'>" + esc(v) + "</span>";
        chips.appendChild(chip);
      });
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
      actions.appendChild(btn("删除", function () { deleteRecord(rec); }, "btn btn-danger"));
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

  function deleteRecord(rec) {
    var name = (rec.fields && rec.fields["案件名"]) || rec.id;
    if (!confirm("确定删除战果记录「" + name + "」？该操作不可恢复。")) return;
    api("/api/case-report/records/" + rec.id, { method: "DELETE" })
      .then(function () {
        toast("已删除", "ok");
        refreshRecords();
        refreshSummary();
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
    $("#fieldGrid").addEventListener("input", function (e) {
      var input = e.target.closest('input[data-field]');
      if (!input) return;
      if (input.dataset.field === "缴获物品") scheduleItemsPreview();
    });
  }

  /* ==================== 初始化 ==================== */
  document.addEventListener("DOMContentLoaded", function () {
    bindEvents();
    loadConfig();
    loadCategories();
    refreshRecords();
    refreshSummary();
  });
})();