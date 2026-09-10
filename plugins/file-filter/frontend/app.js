/* 文件过滤器 —— 前端逻辑
 * 上传（虚线方框，点击/拖拽）→ 模式与保留字段选择 → 提交过滤（task_id 轮询）→ 结果与下载。
 * 全部动态渲染使用 textContent（F-3 XSS 防护）。
 */
(function () {
  "use strict";
  /* v2：保存提示改在配置卡片内（cfgSaveTip）；LLM 已配置判断改用后端 llm_configured 字段 */

  var API = "/api/file-filter";
  var $ = function (id) { return document.getElementById(id); };

  var state = {
    file: null,
    config: null,        // {llm, keep_columns, post_rules, can_manage}
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
      renderKeepChips();
    }).catch(function (err) {
      $("keepChips").innerHTML = "";
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = "配置加载失败：" + err.message;
      $("keepChips").appendChild(p);
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
    $("filterBtn").disabled = false;
  }

  function clearFile() {
    state.file = null;
    $("fileInfo").classList.add("hidden");
    $("filterBtn").disabled = true;
    resetResult();
  }

  function formatSize(n) {
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }

  // ===================== 保留字段勾选 =====================

  function renderKeepChips() {
    var box = $("keepChips");
    box.innerHTML = "";
    var keep = (state.config && state.config.keep_columns) || [];
    if (!keep.length) {
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = "管理员尚未配置保留字段名单，请联系管理员或进入「管理配置」设定。";
      box.appendChild(p);
      return;
    }
    keep.forEach(function (name) {
      var chip = document.createElement("span");
      chip.className = "chip";
      chip.dataset.on = "1";
      chip.textContent = name;
      chip.title = "点击切换 是否保留";
      chip.addEventListener("click", function () {
        chip.dataset.on = chip.dataset.on === "1" ? "0" : "1";
        chip.classList.toggle("off", chip.dataset.on === "0");
      });
      box.appendChild(chip);
    });
    var hint = document.createElement("p");
    hint.className = "muted";
    hint.style.marginTop = "6px";
    hint.textContent = "点击字段可临时启用/停用（不影响管理员配置）；全停用视为使用完整名单。";
    box.appendChild(hint);
  }

  function selectedKeepColumns() {
    var chips = $("keepChips").querySelectorAll(".chip");
    var on = [], anyOff = false;
    chips.forEach(function (c) {
      if (c.dataset.on === "1") on.push(c.textContent);
      else anyOff = true;
    });
    if (anyOff && on.length) return on;
    return null; // 使用完整名单
  }

  // ===================== 过滤提交与轮询 =====================

  function bindActions() {
    $("filterBtn").addEventListener("click", submitFilter);
    $("resetBtn").addEventListener("click", function () {
      clearFile();
      setStatus("");
    });
  }

  function submitFilter() {
    if (!state.file) return;
    var mode = document.querySelector('input[name="mode"]:checked').value;
    if (mode === "llm" && state.config && !state.config.llm_configured
        && !llmConfigFilled()) {
      setStatus("大模型未配置：请管理员在「管理配置」中填写 API 信息", true);
      return;
    }
    var fd = new FormData();
    fd.append("file", state.file);
    fd.append("mode", mode);
    var cols = selectedKeepColumns();
    if (cols) fd.append("columns", JSON.stringify(cols));
    $("filterBtn").disabled = true;
    setStatus("上传中…");
    resetResult();
    fetch(API + "/filter", { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
          return data;
        });
      })
      .then(function (data) {
        setStatus("过滤中，请稍候…");
        pollResult(data.task_id);
      })
      .catch(function (err) {
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
    addLine(stats, "后处理替换：", String(task.replace_count || 0) + " 处");
    if (task.llm_used) addLine(stats, "匹配方式：", "大模型语义匹配");

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
        renderKeepChips();
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
    renderKeepChips();
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
        renderKeepChips();
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
        if (!resp.ok) throw new Error((data && data.error) || ("HTTP " + resp.status));
        return data;
      });
    });
  }

  init();
})();
