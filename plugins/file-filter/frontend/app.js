/* 文件过滤器 —— 前端逻辑
 * 上传（虚线方框，点击/拖拽）→ 识别字段（胶囊：画删除线的列将被删除，点击可切换保留/删除）
 * → 后处理规则展示与开关（默认开启）→ 提交过滤（task_id 轮询）→ 结果与下载。
 * 全部动态渲染使用 textContent（F-3 XSS 防护）。
 */
(function () {
  "use strict";
  /* v4：上传后展示识别到的列名 + 硬过滤预判（胶囊 / 删除线 / 点击切换）、后处理规则展示与
   * 「启用后处理」开关（默认开启）；提交时复用 /preview 的暂存件（staged_id），
   * 用户反复调整字段不必重复上传文件。
   * v6（自学习 P1）：预览胶囊在大模型模式下按映射缓存预填判定并标注来源徽标（精确 /
   * 已确认 / 模型建议 / 确认删 / 建议删，悬停看映射目标）；结果页新增「映射复核」
   * （✓确认 / ✏改指 / ✕否决，409 冲突弹窗后覆盖）；管理配置新增「映射记忆」块
   * （过滤列表 / 改指 / 删除 / 清空 / 导出评测集，含运行统计）。 */

  var API = "/api/file-filter";
  var $ = function (id) { return document.getElementById(id); };

  var state = {
    file: null,
    config: null,        // {keep_columns, post_rules, can_manage, llm_configured, llm_reason}
    preview: null,       // /preview 响应（staged_id / columns / summary / post_rules / sanitize）
    chips: [],           // 识别到的列（可点击切换）：见 buildChips
    confirmItems: [],    // 最终确认项（P3a）：见 buildConfirmItems
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
      state.chips = buildChips(data.columns || []);   // 预填已在 buildChips 内按双重过滤口径完成
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
      var dbl = !!(state.config && state.config.llm_configured);
      return {
        index: c.index,
        name: c.name,
        key: nameKey(c.name),
        // 双重过滤预填：大模型可用时按映射缓存预填（待重映射=临时保留；负映射=删），否则仅精确同名
        keep: (dbl && c.match) ? (c.match.status === "remap" ? true : c.match.target !== "") : !!c.keep,
        hardKeep: !!c.keep,                   // 精确口径的预判（降级提示与审计用）
        match: c.match || null,               // 映射缓存预填：{target, status} | null
        touched: false,                       // 用户手动点过 → 不再被程序改预判
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
    var badge = chipBadge(c);
    if (badge) chip.appendChild(badge);
    if (c.dup && c.dupCount > 1) {
      var dup = document.createElement("span");
      dup.className = "dup";
      dup.textContent = "×" + c.dupCount;
      chip.appendChild(dup);
    }
    if (c.locked) {
      chip.title = "未命名列（空表头）无法按字段名保留，只能删除";
    } else {
      chip.title = chipTitle(c);
      chip.addEventListener("click", function () { toggleColumn(c.key); });
    }
    return chip;
  }

  // 来源徽标：大模型不可用（降级为仅精确匹配）时，记忆建议不参与判定，只显示精确徽标
  function chipBadge(c) {
    var dbl = !!(state.config && state.config.llm_configured);
    if (!dbl) {
      if (!c.matched) return null;
      var x = document.createElement("span");
      x.className = "src exact";
      x.textContent = "精确";
      x.title = "与保留名单字段同名";
      return x;
    }
    if (c.match) {
      var confirmed = c.match.status === "confirmed";
      if (c.match.status === "remap") {
        var rm = document.createElement("span");
        rm.className = "src remap";
        rm.textContent = "待重映射";
        rm.title = "用户判定保留但目标未定：本次临时保留，管理员重映射后定目标";
        return rm;
      }
      if (c.match.target) {
        var b = document.createElement("span");
        b.className = "src " + (confirmed ? "confirmed" : "suggested");
        b.textContent = confirmed ? "已确认" : "模型建议";
        b.title = (confirmed ? "已确认映射「" : "模型建议「") + c.name + "」→「" + c.match.target
          + "」（下次同样字段直接复用，可在结果页复核）";
        return b;
      }
      var d = document.createElement("span");
      d.className = "src drop";
      d.textContent = confirmed ? "确认删" : "建议删";
      d.title = confirmed
        ? "已确认与保留名单无关联：下次同样字段直接删除"
        : "模型建议与保留名单无关联（悬停后可在结果页确认 / 改指）";
      return d;
    }
    if (c.matched) {
      var e = document.createElement("span");
      e.className = "src exact";
      e.textContent = "精确";
      e.title = "与保留名单字段同名";
      return e;
    }
    return null;
  }

  function chipTitle(c) {
    if (!c.keep) return "删除 — 点击改为保留";
    if (c.match && c.match.status === "remap") {
      return "待重映射（临时保留，管理员重映射中）— 点击改为删除";
    }
    if (c.match && c.match.target) {
      return (c.match.status === "confirmed" ? "已确认映射「" : "模型建议「") + c.name
        + "」→「" + c.match.target + "」— 点击改为删除";
    }
    return (c.matched && c.matched !== c.name
      ? "保留（匹配保留字段「" + c.matched + "」）— 点击改为删除"
      : "保留 — 点击改为删除");
  }

  function previewHint() {
    var parts = [];
    if (state.config && state.config.llm_configured) {
      parts.push("双重过滤：点击字段可切换保留 / 删除。带「已确认 / 模型建议 / 确认删 / 建议删」"
        + "徽标的字段按学习记忆预填了判定（悬停可看映射目标）；画删除线的字段若与保留字段"
        + "语义相关仍会被保留，你手动点开的字段一定保留。");
    } else {
      parts.push("大模型未配置：本次仅按保留字段名单精确匹配（画删除线的字段将被删除），"
        + "点击字段可切换。");
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
    state.chips.forEach(function (c) {
      if (c.key === key) { c.keep = !anyOn; c.touched = true; }
    });
    renderPreview();
  }

  function selectedColumns() {
    // on = 保留预判的列；off = 只有用户**手动点掉**的列才进 exclude（最终否决）。
    // 未动过的删除预判不进 exclude——否则大模型模式永远学不到新映射：
    // 画了删除线的列全部被否决，LLM 根本没机会判定（这也是 v1.3 提示文案
    // 「画删除线的字段若语义相关仍会被保留」一直未真正成立的根因）。
    var on = [], off = [];
    state.chips.forEach(function (c) {
      if (c.locked) return;                    // 空表头列无论如何都删
      if (c.keep) { on.push(c.name); return; }
      if (c.touched) off.push(c.name);
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
    $("confirmBtn").addEventListener("click", confirmAndDownload);
    $("resetBtn").addEventListener("click", function () {
      clearFile();
      setStatus("");
    });
  }

  function submitFilter(opts) {
    if (!state.file) return;
    var picked = selectedColumns();
    if (!picked.on.length) {
      setStatus("请至少保留一个字段：点击字段胶囊可切换保留 / 删除", true);
      return;
    }
    // 名单内字段被手动点掉（决策 ⑦ 提示）：本次按用户决定删除，但名单与记忆不受影响
    var keepFold = {};
    ((state.config && state.config.keep_columns) || []).forEach(function (n) {
      keepFold[nameKey(n)] = 1;
    });
    var inListOff = picked.off.filter(function (n) { return keepFold[nameKey(n)]; });
    if (inListOff.length && !(opts && opts.skipListWarning) && !window.confirm("字段 "
        + inListOff.map(function (n) { return "「" + n + "」"; }).join("、")
        + " 在保留字段名单中，本次将按您的选择删除（仅对本次文件生效，名单与学习记忆不受影响）。继续吗？")) {
      return;
    }
    // 待重映射列被手动点掉（P4 收尾）：计一张 drop 票——连续 5 票集体定论为删除
    // （按用户去重；当前文件仍按用户决定删除，remap 保留态由票数决定是否推翻）
    var remapDrops = [];
    state.chips.forEach(function (c) {
      if (c.touched && !c.keep && c.match && c.match.status === "remap") {
        remapDrops.push({ key: c.key, dir: "drop" });
      }
    });
    if (remapDrops.length) {
      fetchJSON(API + "/mappings/vote", {
        method: "POST", body: JSON.stringify({ items: remapDrops }),
      }).catch(function () { /* 计票失败不影响本次过滤 */ });
    }
    var fd = new FormData();
    if (state.preview && state.preview.staged_id) {
      fd.append("staged_id", state.preview.staged_id);   // 复用已上传的暂存件
    } else {
      fd.append("file", state.file);
    }
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

  // ===================== 结果展示 =====================

  function resetResult() {
    if (state.taskTimer) clearTimeout(state.taskTimer);
    state.confirmItems = [];
    $("resultCard").classList.add("hidden");
    $("resultEmpty").classList.remove("hidden");
    $("downloadBtn").classList.add("hidden");
  }

  // ===================== 最终确认（P3a：只拦建议级判定） =====================

  var VERIFIED_SOURCES = { exact: 1, confirmed: 1, confirmed_drop: 1, remap_keep: 1,
                           excluded: 1, empty: 1, hard_unmatched: 1 };

  // 把任务结果折算成逐列确认项：verified（精确/已确认/用户点掉等）直通，
  // llm/llm_drop/suggested/suggested_drop 为 proposal，需用户把关。
  function buildConfirmItems(task) {
    var used = {};
    (task.mappings_used || []).forEach(function (m) {
      used[m.key] = m;
      if (m.sample) used[m.sample] = m;
    });
    function lookup(column) {
      return used[column] || used[nameKey(column)] || null;
    }
    var items = [];
    (task.kept || []).forEach(function (k) {
      var m = lookup(k.column);
      items.push({
        column: k.column,
        key: m ? m.key : nameKey(k.column),
        sample: m ? (m.sample || k.column) : k.column,
        target: (k.source === "llm" || k.source === "suggested") ? (k.matched || "") : "",
        source: k.source,
        original: "keep", decision: "keep", newTarget: "",
        verified: !!VERIFIED_SOURCES[k.source],
      });
    });
    (task.removed_detail || []).forEach(function (d) {
      var m = lookup(d.column);
      items.push({
        column: d.column,
        key: m ? m.key : nameKey(d.column),
        sample: m ? (m.sample || d.column) : d.column,
        target: "",                            // 被删除的列无需目标；改留时由用户选 newTarget
        source: d.source,
        original: "drop", decision: "drop", newTarget: "",
        verified: !!VERIFIED_SOURCES[d.source],
      });
    });
    return items;
  }

  function renderConfirmBox() {
    var box = $("confirmBox");
    var items = state.confirmItems || [];
    var need = items.some(function (i) { return !i.verified; });
    if (!need) {
      box.classList.add("hidden");
      return false;
    }
    box.classList.remove("hidden");
    renderConfirmChips($("confirmKeepChips"), items.filter(function (i) {
      return i.decision === "keep";
    }), "点击改为删除");
    renderConfirmChips($("confirmDropChips"), items.filter(function (i) {
      return i.decision === "drop";
    }), "点击改为保留");
    $("confirmBtn").disabled = false;
    $("confirmTip").textContent = "";
    return true;
  }

  function renderConfirmChips(box, items, flipHint) {
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
      chip.className = "chip" + (it.decision === "keep" ? "" : " off");
      chip.appendChild(document.createTextNode(it.column));
      if (it.source === "llm" || it.source === "llm_drop") {
        var b = document.createElement("span");
        b.className = "src suggested";
        b.textContent = it.decision === "keep" ? "模型建议" : "建议删";
        chip.appendChild(b);
      } else if (it.source === "suggested" || it.source === "suggested_drop") {
        var s = document.createElement("span");
        s.className = "src " + (it.decision === "keep" ? "suggested" : "drop");
        s.textContent = it.decision === "keep" ? "模型建议" : "建议删";
        chip.appendChild(s);
      }
      if (it.verified) {
        chip.title = "已直接生效（" + sourceLabel(it.source) + "）";
      } else {
        chip.classList.add("toggle");
        chip.title = flipHint;
        chip.addEventListener("click", function () { flipConfirmItem(it); });
      }
      box.appendChild(chip);
    });
  }

  function sourceLabel(s) {
    return { exact: "名单内同名", confirmed: "已确认映射", confirmed_drop: "已确认删除",
             remap_keep: "待重映射（管理员重映射中）",
             excluded: "您手动点掉", empty: "未命名列", hard_unmatched: "未匹配名单" }[s] || s;
  }

  function flipConfirmItem(item) {
    // 同名重复列同判：一起翻转
    (state.confirmItems || []).forEach(function (i) {
      if (i.key === item.key) i.decision = (i.decision === "keep") ? "drop" : "keep";
    });
    renderConfirmBox();
  }

  function confirmAndDownload() {
    var items = state.confirmItems || [];
    var proposals = items.filter(function (i) { return !i.verified; });
    // ① 名单内删除提示（决策 ⑦：确认删除压过名单同名字段，需让操作员知道后果）
    var keepFold = {};
    ((state.config && state.config.keep_columns) || []).forEach(function (n) {
      keepFold[nameKey(n)] = 1;
    });
    var inList = items.filter(function (i) {
      return i.decision === "drop" && keepFold[nameKey(i.column)];
    });
    if (inList.length && !window.confirm("字段 "
        + inList.map(function (i) { return "「" + i.column + "」"; }).join("、")
        + " 在保留字段名单中。确认删除将写入学习记忆，后续文件持续生效并优先于名单"
        + "（管理员可在映射记忆中恢复）。继续吗？")) {
      return;
    }
    // ② 决策落库（P4 两种粒度）：
    //    未翻转项 = 被动采纳 → 计票（同向 ≥5 票且零翻转自动转正）；
    //    翻转项 = 显式动作 → 立即生效（改删=否决+负映射；改留=转待重映射，目标由管理员重映射确定）。
    var votes = [], writes = [];
    proposals.forEach(function (it) {
      if (it.original === "keep" && it.decision === "keep") {
        votes.push({ key: it.key, dir: "keep", sample: it.sample });
      } else if (it.original === "drop" && it.decision === "drop") {
        votes.push({ key: it.key, dir: "drop", sample: it.sample });
      } else if (it.original === "keep" && it.decision === "drop") {
        if (it.target) {
          writes.push(fetchJSON(API + "/mappings/reject", {
            method: "POST",
            body: JSON.stringify({ key: it.key, target: it.target }),
          }));
        }
        writes.push(confirmMappings([{ key: it.key, target: "", sample: it.sample }], false));
      } else {  // 删除→保留翻转：转待重映射（不再强制用户选目标）
        writes.push(fetchJSON(API + "/mappings/remap", {
          method: "POST",
          body: JSON.stringify({ key: it.key, sample: it.sample }),
        }));
      }
    });
    var adjusted = proposals.some(function (it) { return it.decision !== it.original; });
    $("confirmBtn").disabled = true;
    $("confirmTip").textContent = (writes.length + votes.length) ? "落库中…" : "";
    Promise.all(writes).catch(function () { return null; })
      .then(function () {
        if (!votes.length) return { promoted: [] };
        return fetchJSON(API + "/mappings/vote", {
          method: "POST", body: JSON.stringify({ items: votes }),
        }).catch(function () { return { promoted: [] }; });
      })
      .then(function (res) {
        var promoted = ((res && res.promoted) || []).length;
        var oc = (res && res.outcomes) || {};
        if (!adjusted) {
          $("confirmBox").classList.add("hidden");
          $("downloadBtn").classList.remove("hidden");
          if (promoted) {
            setStatus("已确认：" + writes.length + " 条显式落库、" + votes.length
              + " 条计票，其中 " + (oc.confirmed || 0) + " 条定论为已确认"
              + ((oc.remap || 0) ? "、" + oc.remap + " 条定论转待重映射（管理员定目标）" : "") + "。");
          } else {
            setStatus("已确认：" + writes.length + " 条显式落库、" + votes.length
              + " 条计票（累计同向 5 票后自动定论）。");
          }
          return;
        }
        // ③ 调整折算进预览胶囊（touched 语义），重跑——已落库，重跑命中缓存
        // （名单内提示已在 ① 用"持续生效"措辞确认过，重跑不再重复弹）
        items.forEach(function (it) {
          if (it.verified || it.decision === it.original) return;
          state.chips.forEach(function (c) {
            if (c.locked || c.key !== nameKey(it.column)) return;
            c.keep = (it.decision === "keep");
            c.touched = true;
          });
        });
        submitFilter({ skipListWarning: true });
      });
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
    if (task.degraded) {
      addLine(stats, "匹配方式：", "⚠️ 大模型未配置，已降级为仅精确匹配"
        + (task.degraded_reason ? "（" + task.degraded_reason + "）" : ""));
    } else if (task.llm_used) {
      addLine(stats, "匹配方式：", "双重过滤（精确 + 大模型语义匹配）");
    } else {
      addLine(stats, "匹配方式：", "精确匹配 + 学习记忆（未调大模型）");
    }
    if (task.sanitize && task.sanitize.note) {
      addLine(stats, "文档预处理：", task.sanitize.note);
    }

    renderChipList($("keptChips"), task.kept || [], true);
    renderChipList($("removedChips"), (task.removed || []).map(function (c) {
      return { column: c };
    }), false);
    renderReview(task);
    state.confirmItems = buildConfirmItems(task);
    var needConfirm = renderConfirmBox();

    var dl = $("downloadBtn");
    dl.href = task.download || "#";
    dl.setAttribute("download", (task.filename || "filtered") + "_已过滤." + (task.output_ext || "xlsx"));
    // 有待把关的 proposal 判定时，先经「确认并下载」，防 LLM 误判静默落盘
    if (task.download && !needConfirm) dl.classList.remove("hidden");

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

  // ===================== 映射复核（自学习把关，P1） =====================

  // 把本次实际采用的映射（task.mappings_used）列出来供把关：✓ 确认 / ✏ 改指 / ✕ 否决。
  // 确认与否决对所有登录用户开放（操作员最懂自己的表）；删除/清空等维护在配置面板归管理员。
  function renderReview(task) {
    var box = $("reviewBox");
    var list = $("reviewList");
    list.innerHTML = "";
    var used = task.mappings_used || [];
    if (!used.length) {
      box.classList.add("hidden");
      return;
    }
    box.classList.remove("hidden");
    used.forEach(function (m) { list.appendChild(buildReviewRow(m)); });
  }

  function buildReviewRow(m) {
    var row = document.createElement("div");
    row.className = "review-row";
    var isAdmin = !!(state.config && state.config.can_manage);

    var label = document.createElement("span");
    label.className = "name";
    label.textContent = reviewLabel(m);
    row.appendChild(label);

    var badge = document.createElement("span");
    badge.className = "badge " + (m.status === "confirmed" ? "ok" : "warn");
    badge.textContent = m.status === "confirmed" ? "已确认" : "模型建议";
    row.appendChild(badge);

    var actions = document.createElement("span");
    actions.className = "review-actions";

    if (m.status !== "confirmed") {
      actions.appendChild(mkAction("✓ 确认", function () {
        confirmMappings([{ key: m.key, target: m.target, sample: m.sample }], false)
          .then(function () { markReviewRow(row, m); })
          .catch(function (err) { setStatus("确认失败：" + err.message, true); });
      }));
    }

    // 改指：换成名单里另一个字段（「建议删/确认删」的行也可借此改为保留）。
    // 对已确认映射的改指：管理员立即覆盖；非管理员 = 异议重议（计票，决策 11）。
    actions.appendChild(buildTargetSelect());
    actions.appendChild(mkAction("应用", function () {
      var sel = actions.querySelector("select");
      var t = sel ? sel.value : "";
      if (!t) return;
      if (m.status === "confirmed" && !isAdmin) {
        dissentVote(m, "keep", row);
        return;
      }
      applyReassign(row, m, t);
    }));

    if (m.target) {
      // 否决映射对（对「删除」判定否决没有意义——想保留请用改指）。
      // 已确认映射的否决：管理员立即生效；非管理员 = 异议重议。
      actions.appendChild(mkAction("✕ 否决", function () {
        if (m.status === "confirmed" && !isAdmin) {
          dissentVote(m, "drop", row);
          return;
        }
        fetchJSON(API + "/mappings/reject", {
          method: "POST",
          body: JSON.stringify({ key: m.key, target: m.target }),
        }).then(function () {
          row.classList.add("rejected");
          row.querySelectorAll("button, select").forEach(function (el) { el.disabled = true; });
          setStatus("已否决「" + (m.sample || m.key) + " ↔ " + m.target + "」，该判定不再复用。");
        }).catch(function (err) { setStatus("否决失败：" + err.message, true); });
      }));
    }

    row.appendChild(actions);
    return row;
  }

  // 非管理员对已确认映射的异议（P4 决策 11）：降级重议 + 计一票，不立即覆盖。
  function dissentVote(m, direction, row) {
    fetchJSON(API + "/mappings/vote", {
      method: "POST",
      body: JSON.stringify({ items: [{ key: m.key, dir: direction, dissent: true }] }),
    }).then(function (res) {
      if ((res.dissented || 0) < 1) {
        setStatus("该映射已进入重议流程，无需重复提交。");
        return;
      }
      m.status = "suggested";
      if (row) {
        var badge = row.querySelector(".badge");
        badge.textContent = "模型建议";
        badge.className = "badge warn";
        badge.title = "重议中：有用户对已确认映射提出异议；连续同向 5 票后自动定论";
        var actions = row.querySelector(".review-actions");
        if (actions && !actions.querySelector(".reconfirm")) {
          // 异议后补 ✓ 入口：提交人马上改主意时可直接重新确认
          var re = mkAction("✓ 确认", function () {
            confirmMappings([{ key: m.key, target: m.target, sample: m.sample }], false)
              .then(function () { markReviewRow(row, m); })
              .catch(function (err) { setStatus("确认失败：" + err.message, true); });
          });
          re.classList.add("reconfirm");
          actions.insertBefore(re, actions.firstChild);
        }
      }
      setStatus("已记录你的判断：「" + (m.sample || m.key) + "」进入重议"
        + "（建议级）；后续使用与确认将决定最终走向，管理员可提前定论。");
    }).catch(function (err) { setStatus("提交失败：" + err.message, true); });
  }

  function reviewLabel(m) {
    return (m.sample || m.key) + " ↔ " + (m.target || "（删除）");
  }

  function buildTargetSelect() {
    var sel = document.createElement("select");
    sel.className = "review-select";
    var opt0 = document.createElement("option");
    opt0.value = "";
    opt0.textContent = "改指为…";
    sel.appendChild(opt0);
    ((state.config && state.config.keep_columns) || []).forEach(function (name) {
      var opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    });
    return sel;
  }

  function applyReassign(row, m, target) {
    confirmMappings([{ key: m.key, target: target, sample: m.sample }], false)
      .then(function () {
        m.target = target;
        markReviewRow(row, m);
      })
      .catch(function (err) {
        // 409：同字段已有不同的已确认映射 → 弹窗确认后覆盖（force）
        if (hasConflicts(err) && window.confirm(conflictText(err, target))) {
          confirmMappings([{ key: m.key, target: target, sample: m.sample }], true)
            .then(function () { m.target = target; markReviewRow(row, m); })
            .catch(function (e2) { setStatus("改指失败：" + e2.message, true); });
        } else if (!hasConflicts(err)) {
          setStatus("改指失败：" + err.message, true);
        }
      });
  }

  function markReviewRow(row, m) {
    m.status = "confirmed";
    var badge = row.querySelector(".badge");
    badge.textContent = "已确认";
    badge.className = "badge ok";
    row.querySelector(".name").textContent = reviewLabel(m);
    row.querySelectorAll("button").forEach(function (b) {
      if (b.textContent.indexOf("确认") >= 0) b.disabled = true;
    });
    setStatus("已确认「" + (m.sample || m.key) + "」的映射，下次同样字段直接按此过滤。");
  }

  function mkAction(text, onClick) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn secondary mini";
    b.textContent = text;
    b.addEventListener("click", onClick);
    return b;
  }

  function hasConflicts(err) {
    return !!(err && err.data && err.data.conflicts && err.data.conflicts.length);
  }

  function conflictText(err, target) {
    return (err.data.conflicts || []).map(function (c) {
      return "字段「" + c.key + "」已确认映射为「" + c.existing_target + "」";
    }).join("；") + "。要覆盖为「" + target + "」吗？";
  }

  // 确认映射（登录用户）；force=true 覆盖已有确认（409 冲突弹窗后）
  function confirmMappings(items, force) {
    return fetchJSON(API + "/mappings/confirm", {
      method: "POST",
      body: JSON.stringify({ items: items, force: !!force }),
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
    renderCfgKeepChips();
    renderCfgRules(cfg.post_rules || []);

    $("cfgKeepAdd").addEventListener("click", addCfgKeep);
    $("cfgKeepInput").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); addCfgKeep(); }
    });
    $("cfgKeepFilter").addEventListener("input", renderCfgKeepChips);
    $("cfgKeepImport").addEventListener("click", function () { $("cfgKeepImportFile").click(); });
    $("cfgKeepImportFile").addEventListener("change", importKeepColumns);
    $("cfgRuleAdd").addEventListener("click", function () {
      var tbody = $("cfgRulesBody");
      tbody.appendChild(buildRuleRow({ pattern: "", replacement: "", is_regex: false, enabled: true }));
    });
    $("cfgSaveBtn").addEventListener("click", saveConfig);
    $("cfgTestBtn").addEventListener("click", testConfig);
    bindMappingsPanel();
    refreshMappings();
  }

  function renderCfgKeepChips() {
    var box = $("cfgKeepChips");
    box.innerHTML = "";
    var all = (state.config && state.config.keep_columns) || [];
    var filterEl = $("cfgKeepFilter");
    var q = (filterEl ? filterEl.value : "").trim().toLowerCase();
    var shown = 0;
    all.forEach(function (name, idx) {
      if (q && name.toLowerCase().indexOf(q) < 0) return;   // 实时筛选（idx 仍是原数组下标）
      shown += 1;
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
    $("cfgKeepCount").textContent = "共 " + all.length + " 个字段"
      + (q ? "，匹配 " + shown + " 个" : "");
    if (!all.length) {
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = "暂无保留字段，请在下方添加或从表格导入。";
      box.appendChild(p);
    } else if (!shown) {
      var p2 = document.createElement("p");
      p2.className = "muted";
      p2.textContent = "没有匹配「" + q + "」的字段。";
      box.appendChild(p2);
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

  // ===================== 名单批量导入（管理员） =====================

  function importKeepColumns() {
    var input = $("cfgKeepImportFile");
    var f = input.files && input.files[0];
    input.value = "";                          // 允许重复选同一文件
    if (!f) return;
    var ext = (f.name.split(".").pop() || "").toLowerCase();
    if (["xlsx", "xls", "csv"].indexOf(ext) < 0) {
      setImportTip("仅支持 xlsx / xls / csv 文件", true);
      return;
    }
    if (f.size > 20 * 1024 * 1024) {
      setImportTip("文件超过 20MB 上限", true);
      return;
    }
    setImportTip("解析中…");
    var fd = new FormData();
    fd.append("file", f);
    fetchJSON(API + "/config/import-columns", { method: "POST", body: fd })
      .then(function (data) {
        var added = mergeKeepColumns(data.columns || []);
        renderCfgKeepChips();
        setImportTip("✓ 解析到 " + (data.count || 0) + " 个字段：新增 " + added
          + " 个、跳过表头/重复 " + (data.skipped || 0) + " 个。核对后点「保存配置」生效。");
      })
      .catch(function (err) {
        setImportTip("导入失败：" + err.message, true);
      });
  }

  // 大小写不敏感去重合并进名单芯片（只改前端 state，保存配置才落盘）
  function mergeKeepColumns(names) {
    state.config.keep_columns = state.config.keep_columns || [];
    var seen = {};
    state.config.keep_columns.forEach(function (n) { seen[nameKey(n)] = 1; });
    var added = 0;
    (names || []).forEach(function (n) {
      var k = nameKey(String(n == null ? "" : n).trim());
      if (!k || seen[k]) return;
      seen[k] = 1;
      state.config.keep_columns.push(String(n).trim());
      added += 1;
    });
    return added;
  }

  function setImportTip(msg, isError) {
    var el = $("cfgKeepImportTip");
    el.textContent = msg || "";
    el.className = "tip" + (isError ? " err" : "");
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
      keep_columns: state.config.keep_columns || [],
      post_rules: collectRules(),
    };
    fetchJSON(API + "/config", { method: "POST", body: JSON.stringify(body) })
      .then(function () {
        return fetchJSON(API + "/config", { method: "GET" });
      })
      .then(function (cfg) {
        state.config = cfg;
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
    fetchJSON(API + "/config/test", { method: "POST", body: JSON.stringify({}) })
      .then(function (data) {
      tip.textContent = data.detail || (data.ok ? "连通正常" : "连接失败");
      tip.classList.add(data.ok ? "ok" : "err");
    }).catch(function (err) {
      tip.textContent = err.message;
      tip.classList.add("err");
    });
  }

  // ===================== 映射记忆管理（管理员，P1/P3b/P3c） =====================

  var mapOpenNodes = new Set();                   // 保持展开状态（键 = kind:name）
  var recheckDiffs = [];                          // 最近一次匹配修正的 diff

  function bindMappingsPanel() {
    $("mapRefresh").addEventListener("click", refreshMappings);
    $("mapClear").addEventListener("click", clearMappings);
    $("mapLimitSave").addEventListener("click", saveMappingLimit);
    $("mapRecheck").addEventListener("click", startRecheck);
    $("recheckAcceptAll").addEventListener("click", acceptAllRecheck);
    $("recheckDismiss").addEventListener("click", function () {
      $("recheckBox").classList.add("hidden");
    });
    $("mapQ").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); renderMapAccord(); }
    });
    // 懒渲染：details 的 toggle 事件不冒泡，用捕获 phase 在容器上代理
    $("mapAccord").addEventListener("toggle", function (e) {
      var details = e.target;
      if (details.tagName !== "DETAILS") return;
      var key = details.dataset.kind + ":" + details.dataset.name;
      if (details.open) {
        mapOpenNodes.add(key);
        if (!details.dataset.built) {
          var node = findAccordNode(details.dataset.kind, details.dataset.name);
          var q = $("mapQ").value.trim();
          var visible = node ? nodeVisibleEntries(node, q) : [];
          details.querySelector(".node-body").appendChild(buildNodeTable(node, visible));
          details.dataset.built = "1";
        }
      } else {
        mapOpenNodes.delete(key);
      }
    }, true);
    // 展开面板时重拉：页面加载时的快照不含本轮运行新产生的映射
    $("cfgCard").addEventListener("toggle", function () {
      if ($("cfgCard").open) refreshMappings();
    });
  }

  function saveMappingLimit() {
    var v = parseInt($("mapLimit").value, 10);
    if (isNaN(v) || v < 100 || v > 100000) {
      setStatus("自动学习上限需为 100~100000 的整数", true);
      return;
    }
    fetchJSON(API + "/config", { method: "POST", body: JSON.stringify({ mapping_limit: v }) })
      .then(function () {
        setStatus("✓ 自动学习上限已保存（" + v + " 条）");
        refreshMappings();
      })
      .catch(function (err) { setStatus("保存失败：" + err.message, true); });
  }

  function refreshMappings() {
    fetchJSON(API + "/mappings")
      .then(function (data) {
        state.mapEntries = data.entries || [];
        state.mapKeep = data.keep_columns || [];
        var st = data.stats || {};
        var gov = data.governance || {};
        $("mapStats").textContent = "大模型已调用 " + (st.llm_calls || 0)
          + " 次，映射缓存替它省下 " + (st.llm_saved || 0) + " 次调用；已用 "
          + (data.count != null ? data.count : state.mapEntries.length) + " / 上限 "
          + (data.limit != null ? data.limit : "—") + " 条；治理：加权转正 "
          + (gov.auto_promoted || 0) + " · 重议中 " + (gov.revising || 0)
          + " · 有翻转 " + (gov.flipped || 0) + " 条。";
        $("mapLimit").value = data.limit != null ? data.limit : "";
        renderMapAccord();
      })
      .catch(function (err) {
        $("mapStats").textContent = "映射记忆加载失败：" + err.message;
      });
  }

  // ---------- 手风琴（P3c 修订）：名单字段为可折叠节点 + 虚拟分组节点 ----------
  // 数据形态：名单可达数千条、单字段映射通常仅数十条——主从分栏改为单列折叠；
  // 初始渲染只建 summary 行，展开才建子表（懒渲染），过滤框作用于字段名与条目两层。

  function mapAccordNodes() {
    var entries = state.mapEntries || [];
    var keep = state.mapKeep || [];
    var byTarget = {};
    var dropEntries = [], orphanEntries = [];
    entries.forEach(function (e) {
      if (!e.target) {
        if (e.status !== "remap") dropEntries.push(e);   // 待重映射有专属分组，不混入确认删除
        return;
      }
      var k = nameKey(e.target);
      (byTarget[k] = byTarget[k] || []).push(e);
      if (e.orphan) orphanEntries.push(e);
    });
    var nodes = keep.map(function (n) {
      return { kind: "field", name: n, entries: byTarget[nameKey(n)] || [] };
    });
    nodes.push({ kind: "drop", name: "确认删除", entries: dropEntries });
    var remapEntries = entries.filter(function (e) { return e.status === "remap"; });
    nodes.push({ kind: "remap", name: "待重映射", entries: remapEntries });
    nodes.push({ kind: "orphan", name: "失配", entries: orphanEntries });
    // 未学习 = 名单字段既无正向映射（作为 target）、也无自身记录（作为 key，
    // 例如确认删除）——有过任何学习记录就不算覆盖缺口
    var entryKeys = new Set(entries.map(function (e) { return e.key; }));
    nodes.push({
      kind: "unlearned", name: "未学习", entries: [],
      missed: keep.filter(function (n) {
        return !(byTarget[nameKey(n)] || []).length && !entryKeys.has(nameKey(n));
      }),
    });
    nodes.push({ kind: "all", name: "全部条目", entries: entries });
    return nodes;
  }

  function nodeVisibleEntries(node, q) {
    if (!q) return node.entries;
    if (node.name.indexOf(q) >= 0) return node.entries;   // 名称命中 → 显示全部条目
    return node.entries.filter(function (e) {
      return (e.sample || "").indexOf(q) >= 0 || e.key.indexOf(q) >= 0
        || (e.target || "").indexOf(q) >= 0;
    });
  }

  function findAccordNode(kind, name) {
    return mapAccordNodes().filter(function (n) {
      return n.kind === kind && n.name === name;
    })[0] || null;
  }

  function renderMapAccord() {
    var box = $("mapAccord");
    var q = $("mapQ").value.trim();
    box.innerHTML = "";
    var shown = 0;
    mapAccordNodes().forEach(function (node) {
      var nameHit = !q || node.name.indexOf(q) >= 0;
      var visible = nodeVisibleEntries(node, q);
      if (node.kind === "unlearned" && q) {
        node.missed = (node.missed || []).filter(function (n) { return n.indexOf(q) >= 0; });
      }
      var entryHit = visible.length > 0;
      if (q && !nameHit && !entryHit
          && !(node.kind === "unlearned" && (node.missed || []).length)) {
        return;  // 过滤掉无关节点
      }
      // 条目命中（字段名未命中）→ 自动展开并只显示命中的条目
      var forceOpen = !!q && !nameHit && entryHit;
      box.appendChild(buildMapNode(node, visible, forceOpen));
      shown += 1;
    });
    var emptyEl = $("mapEmpty");
    if (!shown) {
      emptyEl.textContent = q
        ? "没有匹配的名单字段或映射条目。"
        : "暂无映射记录：操作员在过滤结果里「确认 / 改指 / 否决」后，判定会存入这里，"
          + "下次同样字段直接复用、不再询问大模型。";
      emptyEl.classList.remove("hidden");
    } else {
      emptyEl.classList.add("hidden");
    }
  }

  function nodeKey(node) {
    return node.kind + ":" + node.name;
  }

  function buildMapNode(node, visibleEntries, forceOpen) {
    var key = nodeKey(node);
    var isOpen = forceOpen || mapOpenNodes.has(key);
    var details = document.createElement("details");
    details.className = "map-node";
    details.dataset.kind = node.kind;
    details.dataset.name = node.name;
    if (isOpen) details.open = true;
    var summary = document.createElement("summary");
    var label = document.createElement("span");
    label.textContent = node.name;
    var cnt = document.createElement("span");
    var n = node.kind === "unlearned" ? (node.missed || []).length : visibleEntries.length;
    cnt.className = "cnt" + (n ? "" : " zero");
    cnt.textContent = String(n);
    summary.appendChild(label);
    summary.appendChild(cnt);
    details.appendChild(summary);
    var body = document.createElement("div");
    body.className = "node-body";
    if (node.kind === "unlearned") {
      var hint = document.createElement("p");
      hint.className = "node-hint";
      var missed = node.missed || [];
      // 名单上千时全量罗列会成为巨幅文本：只展示前 50 个
      var shownNames = missed.slice(0, 50);
      hint.textContent = "以下保留字段尚无任何映射记录（覆盖缺口，共 " + missed.length + " 个）："
        + (shownNames.join("、") || "无") + (missed.length > shownNames.length ? " …等" : "") + "。";
      body.appendChild(hint);
      details.dataset.built = "1";
      details.appendChild(body);
      return details;
    }
    if (isOpen) {
      body.appendChild(buildNodeTable(node, visibleEntries));
      details.dataset.built = "1";
    }
    details.appendChild(body);  // 懒渲染：首次展开时由 toggle 代理补建子表
    return details;
  }

  function buildNodeTable(node, entries) {
    var table = document.createElement("table");
    table.className = "rules-table map-table";
    var thead = document.createElement("thead");
    var trh = document.createElement("tr");
    ["表头", "映射目标", "状态", "票数", "命中", "确认人", ""].forEach(function (t) {
      var th = document.createElement("th");
      th.textContent = t;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    if (!entries.length) {
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 7;
      td.className = "muted";
      td.textContent = node.kind === "field" ? "该字段尚无映射记录。" : "无条目。";
      tr.appendChild(td);
      tbody.appendChild(tr);
    } else {
      entries.forEach(function (m) { tbody.appendChild(buildMapRow(m)); });
    }
    table.appendChild(tbody);
    return table;
  }

  function buildMapRow(m) {
    var tr = document.createElement("tr");

    var tdH = document.createElement("td");
    tdH.textContent = m.sample || m.key;
    if (m.sample && m.sample !== m.key) tdH.title = "归一化键：" + m.key;
    tr.appendChild(tdH);

    var tdT = document.createElement("td");
    tdT.textContent = m.target || "（删除）";
    tr.appendChild(tdT);

    var tdS = document.createElement("td");
    var badge = document.createElement("span");
    if (m.orphan) {
      badge.className = "badge off";
      badge.textContent = "失配";
      badge.title = "映射目标不在当前保留名单里（名单变更过），不参与过滤；请改指或删除";
    } else if (m.status === "remap") {
      badge.className = "badge warn";
      badge.textContent = "待重映射";
      badge.title = "用户判定保留但目标未定：匹配修正重匹配后由管理员确定；重映射前该表头按保留处理";
    } else if (m.status === "confirmed") {
      badge.className = "badge ok";
      badge.textContent = "已确认";
    } else {
      badge.className = "badge warn";
      badge.textContent = "模型建议";
    }
    tdS.appendChild(badge);
    if (m.confirmed_by === "加权转正" && m.status === "confirmed") {
      var auto = document.createElement("span");
      auto.className = "badge";
      auto.textContent = "加权转正";
      auto.title = "由被动采纳计票自动转正（连续同向 ≥5 票）；删除或改指即推翻";
      tdS.appendChild(auto);
    }
    if (m.dissent_by && m.status === "suggested") {
      var ds = document.createElement("span");
      ds.className = "badge drop";
      ds.textContent = "重议中";
      ds.title = "有用户（" + m.dissent_by + "）对已确认映射提出异议，已降级重议；"
        + "连续同向 5 票后自动定论，或直接改指/删除";
      tdS.appendChild(ds);
    }
    if (!m.target && m.in_keep_list) {
      var ik = document.createElement("span");
      ik.className = "badge drop";
      ik.textContent = "名单内";
      ik.title = "该确认删除正压着保留名单里的同名字段（优先于名单生效）；删除本条即恢复名单效力";
      tdS.appendChild(ik);
    }
    tr.appendChild(tdS);

    var tdV = document.createElement("td");
    tdV.textContent = (m.keep_votes || 0) + "/" + (m.drop_votes || 0);
    tdV.title = "保留票/删除票；同向 ≥5 票且零翻转自动转正";
    tr.appendChild(tdV);

    var tdN = document.createElement("td");
    tdN.textContent = String(m.hits || 0);
    tr.appendChild(tdN);

    var tdU = document.createElement("td");
    tdU.textContent = m.confirmed_by || "—";
    if (m.confirmed_at) tdU.title = "确认于 " + m.confirmed_at;
    tr.appendChild(tdU);

    var tdA = document.createElement("td");
    tdA.appendChild(buildTargetSelect());
    tdA.appendChild(mkAction("应用", function () {
      var sel = tdA.querySelector("select");
      var t = sel ? sel.value : "";
      if (!t) return;
      confirmMappings([{ key: m.key, target: t, sample: m.sample }], false)
        .then(refreshMappings)
        .catch(function (err) {
          if (hasConflicts(err) && window.confirm(conflictText(err, t))) {
            confirmMappings([{ key: m.key, target: t, sample: m.sample }], true)
              .then(refreshMappings)
              .catch(function (e2) { setStatus("改指失败：" + e2.message, true); });
          } else if (!hasConflicts(err)) {
            setStatus("改指失败：" + err.message, true);
          }
        });
    }));
    tdA.appendChild(mkAction("删除", function () {
      fetchJSON(API + "/mappings/delete", {
        method: "POST",
        body: JSON.stringify({ key: m.key }),
      }).then(refreshMappings)
        .catch(function (err) { setStatus("删除失败：" + err.message, true); });
    }));
    tr.appendChild(tdA);
    return tr;
  }

  // ---------- 匹配修正（P3b）：AI 复核建议级条目，diff 供采纳 ----------

  function startRecheck() {
    if (!window.confirm("匹配修正将把学习记忆中的【建议级】条目（含失配）分批交由大模型重新判断；"
      + "已确认条目不在范围。复核结果写回建议级，采纳后才升级为已确认。继续吗？")) {
      return;
    }
    $("mapRecheck").disabled = true;
    $("recheckTip").className = "tip";
    $("recheckTip").textContent = "启动中…";
    fetchJSON(API + "/mappings/recheck", { method: "POST", body: JSON.stringify({}) })
      .then(function (data) { pollRecheck(data.task_id); })
      .catch(function (err) {
        $("mapRecheck").disabled = false;
        $("recheckTip").className = "tip err";
        $("recheckTip").textContent = err.message;
      });
  }

  function pollRecheck(taskId) {
    fetchJSON(API + "/mappings/recheck/" + taskId)
      .then(function (t) {
        if (t.status === "done") {
          $("mapRecheck").disabled = false;
          var oc = t.outcomes || {};
          $("recheckTip").textContent = "复核完成：共 " + (t.total || 0) + " 条，更新 "
            + (t.diffs || []).length + " 条、维持原判 " + (oc.unchanged || 0)
            + " 条、跳过 " + (oc.skipped || 0) + " 条（名单外/已确认等）。";
          renderRecheckDiffs(t.diffs || []);
          refreshMappings();
          return;
        }
        if (t.status === "error") {
          $("mapRecheck").disabled = false;
          $("recheckTip").className = "tip err";
          $("recheckTip").textContent = t.detail || "匹配修正失败";
          return;
        }
        $("recheckTip").textContent = "复核中… " + (t.processed || 0) + " / "
          + (t.total || 0) + " 条";
        setTimeout(function () { pollRecheck(taskId); }, 1200);
      })
      .catch(function (err) {
        $("mapRecheck").disabled = false;
        $("recheckTip").className = "tip err";
        $("recheckTip").textContent = err.message;
      });
  }

  function renderRecheckDiffs(diffs) {
    recheckDiffs = diffs || [];
    var box = $("recheckBox");
    var tbody = $("recheckBody");
    tbody.innerHTML = "";
    if (!recheckDiffs.length) {
      box.classList.add("hidden");
      return;
    }
    box.classList.remove("hidden");
    recheckDiffs.forEach(function (d) {
      var tr = document.createElement("tr");
      var tdH = document.createElement("td");
      tdH.textContent = d.sample || d.key;
      tr.appendChild(tdH);
      var tdO = document.createElement("td");
      tdO.textContent = d.old_target || "（删除）";
      tr.appendChild(tdO);
      var tdN = document.createElement("td");
      tdN.textContent = d.new_target || "（删除）";
      tr.appendChild(tdN);
      var tdA = document.createElement("td");
      tdA.appendChild(mkAction("✓ 采纳", function () {
        adoptRecheck([d], tdA);
      }));
      tr.appendChild(tdA);
      tbody.appendChild(tr);
    });
  }

  function adoptRecheck(items, td) {
    if (td) td.textContent = "采纳中…";
    var done = 0, failed = [];
    var next = function (idx) {
      if (idx >= items.length) {
        if (td) td.textContent = "已采纳 " + done + " 条" + (failed.length ? "，失败 " + failed.length + " 条" : "");
        refreshMappings();
        if (!failed.length) {
          recheckDiffs = recheckDiffs.filter(function (d) {
            return !items.some(function (x) { return x.key === d.key; });
          });
          if (!recheckDiffs.length) $("recheckBox").classList.add("hidden");
          else renderRecheckDiffs(recheckDiffs);
        }
        return;
      }
      var d = items[idx];
      confirmMappings([{ key: d.key, target: d.new_target, sample: d.sample }], false)
        .then(function () { done += 1; next(idx + 1); })
        .catch(function (err) {
          if (hasConflicts(err) && window.confirm(conflictText(err, d.new_target))) {
            confirmMappings([{ key: d.key, target: d.new_target, sample: d.sample }], true)
              .then(function () { done += 1; next(idx + 1); })
              .catch(function (e2) { failed.push(d.key); next(idx + 1); });
          } else {
            failed.push(d.key);
            next(idx + 1);
          }
        });
    };
    next(0);
  }

  function acceptAllRecheck() {
    if (!recheckDiffs.length) return;
    if (!window.confirm("确定全部采纳 " + recheckDiffs.length
        + " 条修正建议吗？采纳后升级为「已确认」。")) {
      return;
    }
    adoptRecheck(recheckDiffs.slice(), $("recheckAcceptAll"));
  }

  function clearMappings() {
    if (!window.confirm("确定清空全部映射记忆吗？清空后大模型过滤将重新逐字段判定（运行统计保留）。")) {
      return;
    }
    fetchJSON(API + "/mappings/clear", { method: "POST", body: JSON.stringify({}) })
      .then(function () {
        setStatus("映射记忆已清空。");
        refreshMappings();
      })
      .catch(function (err) { setStatus("清空失败：" + err.message, true); });
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
          err.data = data;                     // 结构化载荷（409 conflicts 等）
          throw err;
        }
        return data;
      });
    });
  }

  init();
})();
