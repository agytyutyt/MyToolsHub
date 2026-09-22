/* 文件过滤器 —— 前端逻辑
 * 上传（虚线方框，点击/拖拽）→ 识别字段（胶囊：画删除线的列将被删除，点击可切换保留/删除）
 * → 后处理规则展示与开关（默认开启）→ 提交过滤（task_id 轮询）→ 结果与下载。
 * 全部动态渲染使用 textContent（F-3 XSS 防护）。
 */
(function () {
  "use strict";
  /* v4：上传后展示识别到的列名 + 硬过滤预判（胶囊 / 删除线 / 点击切换）、后处理规则展示与
   * 「启用后处理」开关（默认开启）；提交时复用 /preview 的暂存件（staged_id），
   * 用户反复调整字段不必重复上传文件。 */

  var API = "/api/file-filter";
  var $ = function (id) { return document.getElementById(id); };

  var state = {
    file: null,
    config: null,        // {llm, keep_columns, post_rules, can_manage, llm_configured}
    preview: null,       // /preview 响应（staged_id / columns / summary / post_rules / sanitize）
    chips: [],           // 识别到的列（可点击切换）：见 buildChips
    taskTimer: null,
  };

  // ===================== 初始化 =====================

  function init() {
    bindUpload();
    bindActions();
    loadConfig();
  }

  function loadConfig() {
    fetchJSON(API + "/config", { method: "GET" }).then(function (cfg) {
      state.config = cfg;
      if (cfg.can_manage) {
        $("cfgCard").hidden = false;
        renderAdminPanel();
      }
      renderPostRules();
    }).catch(function (err) {
      var list = $("postRulesList");
      list.innerHTML = "";
      var li = document.createElement("li");
      li.className = "muted";
      li.textContent = "配置加载失败：" + err.message;
      list.appendChild(li);
    });
  }

  // ===================== 上传 =====================

  function bindUpload() {
    var dz = $("dropzone");
    var input = $("fileInput");
    dz.addEventListener("click", function () { input.click(); });
    input.addEventListener("change", function () {
      if (input.files && input.files[0]) pickFile(input.files[0]);
      input.value = "";
    });
    dz.addEventListener("dragover", function (e) { e.preventDefault(); dz.classList.add("drag"); });
    dz.addEventListener("dragleave", function () { dz.classList.remove("drag"); });
    dz.addEventListener("drop", function (e) {
      e.preventDefault();
      dz.classList.remove("drag");
      if (e.dataTransfer.files && e.dataTransfer.files[0]) pickFile(e.dataTransfer.files[0]);
    });
  }

  function pickFile(f) {
    var ext = (f.name.split(".").pop() || "").toLowerCase();
    if (["xlsx", "xls", "csv"].indexOf(ext) < 0) {
      setStatus("仅支持 xlsx / xls / csv 文件", true);
      return;
    }
    if (f.size > 20 * 1024 * 1024) {
      setStatus("文件超过 20MB 上限", true);
      return;
    }
    state.file = f;
    setStatus("");
    var info = $("fileInfo");
    info.classList.remove("hidden");
    info.innerHTML = "";
    var name = document.createElement("span");
    name.className = "name";
    name.textContent = f.name + "（" + formatSize(f.size) + "）";
    var rm = document.createElement("span");
    rm.className = "rm";
    rm.textContent = "✕ 移除";
    rm.addEventListener("click", clearFile);
    info.appendChild(name);
    info.appendChild(rm);
    resetResult();
    runPreview();
  }

  function clearFile() {
    state.file = null;
    state.preview = null;
    state.chips = [];
    $("fileInfo").classList.add("hidden");
    $("filterBtn").disabled = true;
    renderPreview();
    resetResult();
  }

  function formatSize(n) {
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }

  // ===================== 识别（上传预览） =====================

  function runPreview() {
    if (!state.file) return;
    state.preview = null;
    state.chips = [];
    renderPreview();
    $("filterBtn").disabled = true;
    setStatus("识别中…");
    var fd = new FormData();
    fd.append("file", state.file);
    fetchJSON(API + "/preview", { method: "POST", body: fd }).then(function (data) {
      state.preview = data;
      state.chips = buildChips(data.columns || []);
      renderPreview();
      renderPostRules(data.post_rules);
      $("filterBtn").disabled = false;
      setStatus("");
    }).catch(function (err) {
      setStatus("无法识别该文件：" + err.message, true);
      renderPreview();
    });
  }

  function buildChips(columns) {
    var counts = {};
    columns.forEach(function (c) {
      var k = nameKey(c.name);
      counts[k] = (counts[k] || 0) + 1;
    });
    return columns.map(function (c) {
      return {
        index: c.index,
        name: c.name,
        key: nameKey(c.name),
        keep: !!c.keep,                       // 硬过滤预判：名单里有同名 → 保留
        matched: c.matched || "",
        postName: c.post_name || c.name,      // 后处理规则预演后的表头
        replaceCount: c.replace_count || 0,   // 该列预计替换处数（仅保留的列计入合计）
        locked: !!c.locked,                   // 空表头列：无法按字段名保留
        dup: !!c.dup,
        dupCount: counts[nameKey(c.name)],
      };
    });
  }

  function nameKey(n) {
    return String(n == null ? "" : n).toLowerCase();   // 与后端"忽略大小写"的同名口径一致
  }

  function renderPreview() {
    var box = $("previewChips");
    box.innerHTML = "";
    if (!state.chips.length) {
      $("previewBody").classList.add("hidden");
      $("previewEmpty").classList.remove("hidden");
      updatePostSummary();
      return;
    }
    $("previewEmpty").classList.add("hidden");
    $("previewBody").classList.remove("hidden");

    var nKeep = 0;
    state.chips.forEach(function (c) {
      if (c.keep) nKeep += 1;
      box.appendChild(buildChip(c));
    });

    var s = state.preview.summary || {};
    $("previewSummary").textContent =
      "识别到 " + state.chips.length + " 个字段（" + (state.preview.rows || 0) + " 行数据）："
      + "预计保留 " + nKeep + " 个、删除 " + (state.chips.length - nKeep) + " 个";
    $("previewHint").textContent = previewHint();
    updatePostSummary();
  }

  function buildChip(c) {
    var chip = document.createElement("span");
    chip.className = "chip" + (c.keep ? "" : " off") + (c.locked ? " locked" : "");
    chip.appendChild(document.createTextNode(c.name || "（第 " + (c.index + 1) + " 列·未命名）"));
    if (!c.locked && c.postName && c.postName !== c.name) {
      var alias = document.createElement("span");
      alias.className = "alias";
      alias.textContent = "→ " + c.postName;
      alias.title = "后处理规则会把这一列的表头改成「" + c.postName + "」";
      chip.appendChild(alias);
    }
    if (c.dup && c.dupCount > 1) {
      var dup = document.createElement("span");
      dup.className = "dup";
      dup.textContent = "×" + c.dupCount;
      chip.appendChild(dup);
    }
    if (c.locked) {
      chip.title = "未命名列（空表头）无法按字段名保留，只能删除";
    } else {
      chip.title = c.keep
        ? (c.matched && c.matched !== c.name ? "保留（匹配保留字段「" + c.matched + "」）— 点击改为删除"
                                            : "保留 — 点击改为删除")
        : "删除 — 点击改为保留";
      chip.addEventListener("click", function () { toggleColumn(c.key); });
    }
    return chip;
  }

  function previewHint() {
    var mode = currentMode();
    var parts = [];
    if (mode === "llm") {
      parts.push("点击字段可切换保留 / 删除。大模型过滤还会做语义匹配（如「时间」↔「开始时间」）："
        + "画删除线的字段若与保留字段语义相关仍会被保留，你手动点开的字段一定保留。");
    } else {
      parts.push("点击字段可切换保留 / 删除：画删除线的字段会被删除，其余保留。");
    }
    if (state.chips.some(function (c) { return c.dup && c.dupCount > 1; })) {
      parts.push("同名重复列（×N）会一起保留 / 删除。");
    }
    if (state.chips.some(function (c) { return c.locked; })) {
      parts.push("未命名列（空表头）无法按字段名保留，只能删除。");
    }
    return parts.join("");
  }

  function toggleColumn(key) {
    var anyOn = state.chips.some(function (c) { return c.key === key && c.keep; });
    state.chips.forEach(function (c) { if (c.key === key) c.keep = !anyOn; });
    renderPreview();
  }

  function currentMode() {
    var el = document.querySelector('input[name="mode"]:checked');
    return el ? el.value : "hard";
  }

  function selectedColumns() {
    var on = [], off = [];
    state.chips.forEach(function (c) {
      if (c.locked) return;                    // 空表头列无论如何都删
      (c.keep ? on : off).push(c.name);
    });
    return { on: uniq(on), off: uniq(off) };
  }

  function uniq(arr) {
    var seen = {}, out = [];
    arr.forEach(function (v) { if (!seen[v]) { seen[v] = 1; out.push(v); } });
    return out;
  }

  // ===================== 后处理规则展示与开关 =====================

  function bindPostToggle() {
    $("postToggle").addEventListener("change", function () {
      updatePostSummary();
      if ($("postToggle").checked) setStatus("");
    });
  }

  function renderPostRules(rules) {
    if (!rules) {
      rules = (state.preview && state.preview.post_rules)
        || (state.config && state.config.post_rules) || [];
    }
    var list = $("postRulesList");
    list.innerHTML = "";
    if (!rules.length) {
      var li = document.createElement("li");
      li.className = "muted";
      li.textContent = "管理员尚未配置后处理规则（本次不会做文本替换）。";
      list.appendChild(li);
      updatePostSummary();
      return;
    }
    rules.forEach(function (r) {
      var li = document.createElement("li");
      var p = document.createElement("code");
      p.textContent = r.pattern;
      li.appendChild(p);
      li.appendChild(document.createTextNode(" → "));
      var v = document.createElement("code");
      v.textContent = (r.replacement === "" || r.replacement == null) ? "（删除）" : r.replacement;
      li.appendChild(v);
      var badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = r.is_regex ? "正则" : "文本";
      li.appendChild(badge);
      if (r.enabled === false) {
        var off = document.createElement("span");
        off.className = "badge off";
        off.textContent = "已停用";
        li.appendChild(off);
      }
      list.appendChild(li);
    });
    updatePostSummary();
  }

  function updatePostSummary() {
    var on = $("postToggle").checked;
    $("postRulesBox").classList.toggle("off", !on);
    var box = $("postSummary");
    if (!on) {
      box.textContent = "已关闭：本次不改写列名与单元格文本（过滤规则照常生效）。";
      return;
    }
    if (!state.chips.length) {
      box.textContent = "上传文件后显示预计替换处数。";
      return;
    }
    var n = 0;
    state.chips.forEach(function (c) { if (c.keep) n += c.replaceCount; });
    box.textContent = "按当前保留字段预计替换 " + n + " 处（表头 + 单元格，实际以过滤结果为准）。";
  }

  // ===================== 过滤提交与轮询 =====================

  function bindActions() {
    bindPostToggle();
    $("filterBtn").addEventListener("click", submitFilter);
    $("resetBtn").addEventListener("click", function () {
      clearFile();
      setStatus("");
    });
    document.querySelectorAll('input[name="mode"]').forEach(function (el) {
      el.addEventListener("change", function () {
        renderPreview();                       // 预判口径随模式变：重写提示文案
        if (el.value === "llm" && state.config && !state.config.llm_configured
            && !llmConfigFilled()) {
          setStatus("提示：大模型未配置，请管理员在「管理配置」中填写 API 信息", true);
        } else {
          setStatus("");
        }
      });
    });
  }

  function submitFilter() {
    if (!state.file) return;
    var mode = currentMode();
    if (mode === "llm" && state.config && !state.config.llm_configured
        && !llmConfigFilled()) {
      setStatus("大模型未配置：请管理员在「管理配置」中填写 API 信息", true);
      return;
    }
    var picked = selectedColumns();
    if (!picked.on.length) {
      setStatus("请至少保留一个字段：点击字段胶囊可切换保留 / 删除", true);
      return;
    }
    var fd = new FormData();
    if (state.preview && state.preview.staged_id) {
      fd.append("staged_id", state.preview.staged_id);   // 复用已上传的暂存件
    } else {
      fd.append("file", state.file);
    }
    fd.append("mode", mode);
    fd.append("columns", JSON.stringify(picked.on));
    if (picked.off.length) fd.append("exclude", JSON.stringify(picked.off));
    fd.append("post_process", $("postToggle").checked ? "1" : "0");
    postFilter(fd, false);
  }

  function postFilter(fd, retried) {
    $("filterBtn").disabled = true;
    setStatus("提交中…");
    resetResult();
    fetch(API + "/filter", { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) {
            var err = new Error(data.error || ("HTTP " + resp.status));
            err.code = data.code;
            throw err;
          }
          return data;
        });
      })
      .then(function (data) {
        setStatus("过滤中，请稍候…");
        pollResult(data.task_id);
      })
      .catch(function (err) {
        // 暂存过期（超过 30 分钟）：自动改传文件重试一次，用户不必重新选文件
        if (!retried && err.code === "staged_expired" && state.file) {
          var fd2 = new FormData();
          fd2.append("file", state.file);
          fd.forEach(function (v, k) { if (k !== "staged_id") fd2.append(k, v); });
          postFilter(fd2, true);
          return;
        }
        $("filterBtn").disabled = false;
        setStatus(err.message, true);
      });
  }

  function pollResult(taskId) {
    if (state.taskTimer) clearTimeout(state.taskTimer);
    state.taskTimer = setTimeout(function () {
      fetchJSON(API + "/result/" + taskId, { method: "GET" })
        .then(function (task) {
          if (task.status === "done") {
            showResult(task);
            return;
          }
          if (task.status === "error") {
            $("filterBtn").disabled = false;
            setStatus(task.detail || "过滤失败", true);
            return;
          }
          pollResult(taskId);
        })
        .catch(function (err) {
          $("filterBtn").disabled = false;
          setStatus(err.message, true);
        });
    }, 1000);
  }

  function llmConfigFilled() {
    var b = $("cfgBaseUrl").value.trim(), k = $("cfgApiKey").value.trim();
    return !!(b && k);
  }

  // ===================== 结果展示 =====================

  function resetResult() {
    if (state.taskTimer) clearTimeout(state.taskTimer);
    $("resultCard").classList.add("hidden");
    $("resultEmpty").classList.remove("hidden");
    $("downloadBtn").classList.add("hidden");
  }

  function showResult(task) {
    $("filterBtn").disabled = false;
    setStatus("");
    $("resultEmpty").classList.add("hidden");
    $("resultCard").classList.remove("hidden");

    var stats = $("resultStats");
    stats.innerHTML = "";
    addLine(stats, "数据行数：", String(task.rows || 0));
    addLine(stats, "保留字段：", String((task.kept || []).length) + " 个");
    addLine(stats, "删除字段：", String((task.removed || []).length) + " 个");
    addLine(stats, "后处理替换：", task.post_enabled === false
      ? "已关闭（本次不改写文本）" : String(task.replace_count || 0) + " 处");
    if (task.llm_used) addLine(stats, "匹配方式：", "大模型语义匹配");
    if (task.sanitize && task.sanitize.note) {
      addLine(stats, "文档预处理：", task.sanitize.note);
    }

    renderChipList($("keptChips"), task.kept || [], true);
    renderChipList($("removedChips"), (task.removed || []).map(function (c) {
      return { column: c };
    }), false);

    var dl = $("downloadBtn");
    dl.href = task.download || "#";
    dl.setAttribute("download", (task.filename || "filtered") + "_已过滤." + (task.output_ext || "xlsx"));
    if (task.download) dl.classList.remove("hidden");

    var note = $("outNote");
    note.textContent = "";
    if (task.output_ext === "xlsx" && /\.xls$/i.test(task.filename || "")) {
      note.textContent = "提示：.xls 为只读格式，结果已转为 .xlsx 输出。";
    }
    if (!(task.kept || []).length) {
      note.textContent = "警告：没有匹配到任何保留字段，输出文件仅含表头。";
    }
  }

  function addLine(box, label, value) {
    var div = document.createElement("div");
    var b = document.createElement("b");
    b.textContent = label;
    div.appendChild(b);
    div.appendChild(document.createTextNode(value));
    box.appendChild(div);
  }

  function renderChipList(box, items, withMatch) {
    box.innerHTML = "";
    if (!items.length) {
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = "无";
      box.appendChild(p);
      return;
    }
    items.forEach(function (it) {
      var chip = document.createElement("span");
      chip.className = "chip" + (withMatch ? "" : " off");
      chip.textContent = it.column + (withMatch && it.matched && it.matched !== it.column
        ? " ↔ " + it.matched : "");
      box.appendChild(chip);
    });
  }

  function setStatus(msg, isError) {
    var el = $("filterStatus");
    el.textContent = msg || "";
    el.className = "status" + (isError ? " error" : "");
  }

  // ===================== 管理配置面板 =====================

  function renderAdminPanel() {
    var cfg = state.config;
    $("cfgBaseUrl").value = (cfg.llm && cfg.llm.base_url) || "";
    $("cfgModel").value = (cfg.llm && cfg.llm.model) || "";
    $("cfgApiKey").value = ""; // api_key 掩码存储，留空表示不修改
    renderCfgKeepChips();
    renderCfgRules(cfg.post_rules || []);

    $("cfgKeepAdd").addEventListener("click", addCfgKeep);
    $("cfgKeepInput").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); addCfgKeep(); }
    });
    $("cfgRuleAdd").addEventListener("click", function () {
      var tbody = $("cfgRulesBody");
      tbody.appendChild(buildRuleRow({ pattern: "", replacement: "", is_regex: false, enabled: true }));
    });
    $("cfgSaveBtn").addEventListener("click", saveConfig);
    $("cfgTestBtn").addEventListener("click", testConfig);
  }

  function renderCfgKeepChips() {
    var box = $("cfgKeepChips");
    box.innerHTML = "";
    ((state.config && state.config.keep_columns) || []).forEach(function (name, idx) {
      var chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = name;
      var x = document.createElement("span");
      x.className = "x";
      x.textContent = "✕";
      x.title = "删除该字段";
      x.addEventListener("click", function () {
        state.config.keep_columns.splice(idx, 1);
        renderCfgKeepChips();
      });
      chip.appendChild(x);
      box.appendChild(chip);
    });
    if (!(state.config.keep_columns || []).length) {
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = "暂无保留字段，请在下方添加。";
      box.appendChild(p);
    }
  }

  function addCfgKeep() {
    var input = $("cfgKeepInput");
    var name = input.value.trim();
    if (!name) return;
    state.config.keep_columns = state.config.keep_columns || [];
    if (state.config.keep_columns.indexOf(name) < 0) state.config.keep_columns.push(name);
    input.value = "";
    renderCfgKeepChips();
  }

  function buildRuleRow(rule) {
    var tr = document.createElement("tr");

    var tdP = document.createElement("td");
    var p = document.createElement("input");
    p.type = "text";
    p.value = rule.pattern || "";
    p.placeholder = "查找内容";
    tdP.appendChild(p);

    var tdR = document.createElement("td");
    var r = document.createElement("input");
    r.type = "text";
    r.value = rule.replacement || "";
    r.placeholder = "替换为（留空=删除）";
    tdR.appendChild(r);

    var tdX = document.createElement("td");
    var x = document.createElement("input");
    x.type = "checkbox";
    x.checked = !!rule.is_regex;
    x.title = "按正则表达式解析";
    tdX.appendChild(x);

    var tdE = document.createElement("td");
    var e = document.createElement("input");
    e.type = "checkbox";
    e.checked = rule.enabled !== false;
    tdE.appendChild(e);

    var tdD = document.createElement("td");
    var d = document.createElement("span");
    d.className = "del";
    d.textContent = "删除";
    d.addEventListener("click", function () { tr.remove(); });
    tdD.appendChild(d);

    tr.appendChild(tdP);
    tr.appendChild(tdR);
    tr.appendChild(tdX);
    tr.appendChild(tdE);
    tr.appendChild(tdD);
    return tr;
  }

  function renderCfgRules(rules) {
    var tbody = $("cfgRulesBody");
    tbody.innerHTML = "";
    rules.forEach(function (rule) { tbody.appendChild(buildRuleRow(rule)); });
  }

  function collectRules() {
    var rules = [];
    $("cfgRulesBody").querySelectorAll("tr").forEach(function (tr) {
      var inputs = tr.querySelectorAll("input[type=text]");
      rules.push({
        pattern: inputs[0].value.trim(),
        replacement: inputs[1].value,
        is_regex: tr.querySelectorAll("input[type=checkbox]")[0].checked,
        enabled: tr.querySelectorAll("input[type=checkbox]")[1].checked,
      });
    });
    return rules;
  }

  function saveConfig() {
    var tip = $("cfgSaveTip");
    tip.classList.remove("hidden", "ok", "err");
    tip.textContent = "保存中…";
    var body = {
      llm: {
        base_url: $("cfgBaseUrl").value.trim(),
        api_key: $("cfgApiKey").value.trim(),   // 留空=不修改
        model: $("cfgModel").value.trim(),
      },
      keep_columns: state.config.keep_columns || [],
      post_rules: collectRules(),
    };
    fetchJSON(API + "/config", { method: "POST", body: JSON.stringify(body) })
      .then(function () {
        return fetchJSON(API + "/config", { method: "GET" });
      })
      .then(function (cfg) {
        state.config = cfg;
        $("cfgApiKey").value = "";
        renderCfgKeepChips();
        renderPostRules();
        tip.textContent = "✓ 配置已保存";
        tip.classList.add("ok");
        setTimeout(function () { tip.classList.add("hidden"); }, 3000);
      })
      .catch(function (err) {
        tip.textContent = "保存失败：" + err.message;
        tip.classList.add("err");
      });
  }

  function testConfig() {
    var tip = $("cfgTestTip");
    tip.classList.remove("hidden", "ok", "err");
    tip.textContent = "测试中…";
    fetchJSON(API + "/config/test", {
      method: "POST",
      body: JSON.stringify({
        base_url: $("cfgBaseUrl").value.trim(),
        api_key: $("cfgApiKey").value.trim(),
        model: $("cfgModel").value.trim(),
      }),
    }).then(function (data) {
      tip.textContent = data.detail || (data.ok ? "连通正常" : "连接失败");
      tip.classList.add(data.ok ? "ok" : "err");
    }).catch(function (err) {
      tip.textContent = err.message;
      tip.classList.add("err");
    });
  }

  // ===================== 工具 =====================

  function fetchJSON(url, opts) {
    opts = opts || {};
    opts.credentials = "same-origin";
    if (opts.body && typeof opts.body === "string") {
      opts.headers = { "Content-Type": "application/json" };
    }
    return fetch(url, opts).then(function (resp) {
      return resp.json().then(function (data) {
        if (!resp.ok) {
          var err = new Error((data && data.error) || ("HTTP " + resp.status));
          err.code = data && data.code;
          throw err;
        }
        return data;
      });
    });
  }

  init();
})();
