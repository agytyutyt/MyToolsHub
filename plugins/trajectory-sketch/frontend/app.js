/* 轨迹速写插件前端
 * - 图标：启动时 fetch 自带 SVG（frontend/icons/*.svg）并注入内联 SVG，可用 currentColor 跟随主题色，
 *         不依赖系统 emoji 字体（Win7 兼容）。
 * - 安全：所有动态内容一律用 textContent / createElement 渲染，不使用 innerHTML 拼接用户数据（F-3）。
 * - 接口：/api/trajectory-sketch/{status,config,upload,analyze,result,download}
 */
(function () {
  'use strict';

  var API = '/api/trajectory-sketch';
  var ICON_NAMES = ['upload', 'filter', 'route', 'clock', 'warning', 'gap', 'report',
                    'summary', 'quality', 'download', 'copy', 'settings', 'check', 'reset', 'user'];
  var ICONS = {};

  // ---------------------------------------------------------------- 工具

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (txt !== undefined && txt !== null) { n.textContent = String(txt); }
    return n;
  }

  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  /** 图标元素：优先用已注入的内联 SVG（可着色），降级为 <img>。 */
  function ic(name, cls) {
    var span = el('span', 'ic-slot' + (cls ? ' ' + cls : ''));
    if (ICONS[name]) {
      span.innerHTML = ICONS[name];
    } else {
      var img = el('img', 'ic-img');
      img.src = './icons/' + name + '.svg';
      img.alt = '';
      span.appendChild(img);
    }
    return span;
  }

  function fillIconSlot(id, name) {
    var node = $(id);
    if (!node) { return; }
    clear(node);
    node.appendChild(ic(name));
  }

  function loadIcons() {
    var list = ICON_NAMES.map(function (n) {
      return fetch('./icons/' + n + '.svg')
        .then(function (r) { return r.ok ? r.text() : ''; })
        .then(function (t) { ICONS[n] = t; })
        .catch(function () { ICONS[n] = ''; });
    });
    return Promise.all(list);
  }

  /** 布尔安全文本（防止 undefined 被渲染成 "undefined"） */
  function s(v) { return v === undefined || v === null ? '' : String(v); }

  function num(v, d) {
    var n = Number(v);
    return isFinite(n) ? n : (d === undefined ? 0 : d);
  }

  function api(path, opt) {
    return fetch(API + path, opt || {}).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) {
          var e = new Error(j && j.error ? j.error : ('请求失败（HTTP ' + r.status + '）'));
          e.status = r.status;
          throw e;
        }
        return j;
      });
    });
  }

  function setTip(node, text, bad) {
    node.textContent = text;
    node.className = 'tip' + (bad ? ' bad' : '');
    if (text) {
      setTimeout(function () {
        if (node.textContent === text) { node.textContent = ''; }
      }, 4000);
    }
  }

  // ---------------------------------------------------------------- 状态

  var state = {
    status: null,
    config: null,
    staged: null,      // 上传自检结果
    mode: 'hard',
    polling: false,
    result: null
  };

  // ---------------------------------------------------------------- 表格渲染

  /** 渲染表头 + 行；单元格可为原始值，或 {text, cls} 以便定制样式。 */
  function renderTable(table, headers, rows, wrapCols) {
    clear(table);
    var thead = el('thead');
    var tr = el('tr');
    headers.forEach(function (h) { tr.appendChild(el('th', null, h)); });
    thead.appendChild(tr);
    table.appendChild(thead);

    var tbody = el('tbody');
    if (!rows.length) {
      var r0 = el('tr');
      var td0 = el('td', 'muted', '（无）');
      td0.colSpan = headers.length;
      r0.appendChild(td0);
      tbody.appendChild(r0);
    } else {
      rows.forEach(function (row) {
        var r = el('tr');
        row.forEach(function (cell, i) {
          var isObj = cell && typeof cell === 'object' && 'text' in cell;
          var td = el('td', null, isObj ? cell.text : cell);
          var cls = [];
          if (isObj && cell.cls) { cls.push(cell.cls); }
          if (wrapCols && wrapCols.indexOf(i) >= 0) { cls.push('wrap'); }
          if (cls.length) { td.className = cls.join(' '); }
          r.appendChild(td);
        });
        tbody.appendChild(r);
      });
    }
    table.appendChild(tbody);
  }

  // ---------------------------------------------------------------- 环境提示

  function renderBanner() {
    var banner = $('envBanner');
    var st = state.status || {};
    var deps = st.dependencies || {};
    var missing = Object.keys(deps).filter(function (k) { return !deps[k]; });
    var flt = st.filter_plugin || {};

    if (missing.length) {
      banner.hidden = false;
      banner.className = 'banner err';
      banner.textContent = '后端缺少依赖：' + missing.join(' / ') +
        '。请在服务器执行 pip install -r plugins/trajectory-sketch/backend/requirements.txt 后重启服务。';
      return;
    }
    if (!flt.available) {
      banner.hidden = false;
      banner.className = 'banner err';
      banner.textContent = '「过滤器」插件不可用（' + s(flt.reason) +
        '）。本插件的字段过滤依赖该插件，请联系管理员在首页启用「过滤器」后重启服务。';
      return;
    }
    banner.hidden = false;
    banner.className = 'banner ok';
    banner.textContent = '依赖齐备；已连接「过滤器」插件（字段过滤由其执行）；当前算法版本 ' +
      s(st.algo) + '。';
    if (st.config_warnings && st.config_warnings.length) {
      banner.className = 'banner';
      banner.textContent += ' 配置提醒：' + st.config_warnings.join('；');
    }
  }

  // ---------------------------------------------------------------- 配置面板

  var NUM_FIELDS = [
    { sect: 'clean', key: 'min_longitude', label: '经度下限', hint: '低于该值的行视为无效（默认 1）' },
    { sect: 'clean', key: 'geo_precision', label: '位置指纹精度(位)', hint: '去重用的经纬度小数位（默认 3）' },
    { sect: 'clean', key: 'grid_seconds', label: '时间网格(秒)', hint: '同网格同位置点只留最早一条（默认 300）' },
    { sect: 'clean', key: 'max_gap_seconds', label: '断档阈值(秒)', hint: '超过则标注「无定位上报」（默认 6000）' },
    { sect: 'cluster', key: 'co_site_meters', label: '共址合并距离(米)', hint: '同站不同小区必合并（默认 50）' },
    { sect: 'cluster', key: 'handover_min_count', label: '邻区切换次数下限', hint: '双向切换达此值且距离在下方范围内即合并' },
    { sect: 'cluster', key: 'handover_max_meters', label: '邻区合并距离上限(米)', hint: '默认 2000' },
    { sect: 'staypoint', key: 'fixed_radius_meters', label: '停留半径(米)', hint: '作用于地点簇质心（默认 800）' },
    { sect: 'staypoint', key: 'min_stay_minutes', label: '最短停留(分钟)', hint: '低于此时长不算停留点（默认 15）' },
    { sect: 'staypoint', key: 'radius_factor', label: '噪声放大系数', hint: '未开簇时 D_thr = 噪声 × 该系数' },
    { sect: 'staypoint', key: 'noise_percentile', label: '噪声分位数', hint: '低速位移的分位数（默认 95）' },
    { sect: 'staypoint', key: 'slow_speed_kmh', label: '低速样本上限(km/h)', hint: '用于估计定位噪声（默认 5）' },
    { sect: 'trip', key: 'high_conf_factor', label: '有效出行倍数', hint: '净位移 ≥ 该倍数 × 噪声即判有效出行（默认 3）' },
    { sect: 'trip', key: 'mid_conf_factor', label: '位移判定倍数', hint: '低于该倍数直接合并停留点（默认 1.5）' },
    { sect: 'trip', key: 'straightness_min', label: '直线度下限', hint: '净位移/累计位移，低于则判为切换（默认 0.5）' },
    { sect: 'trip', key: 'walk_speed_kmh', label: '步行速度(km/h)', hint: '仅用于文案提示（默认 4）' },
    { sect: 'trip', key: 'min_trip_minutes', label: '最短出行(分钟)', hint: '首末时段低于该值不出条（默认 5）' },
    { sect: 'trip', key: 'revisit_gap_minutes', label: '回访时间窗(分钟)', hint: '离开后又回到同一地点算乒乓（默认 60）' }
  ];

  function renderConfigPanel() {
    var box = $('cfgCard');
    var cfg = state.config;
    if (!cfg || !cfg.can_manage) {
      box.hidden = !cfg || !cfg.can_manage;
      return;
    }
    box.hidden = false;
    var c = cfg.config || {};

    // 保留字段 chips
    var chips = $('cfgKeepChips');
    clear(chips);
    (c.filter && c.filter.keep_columns || []).forEach(function (k) {
      var chip = el('span', 'chip', k);
      var del = el('span', 'chip-del', '✕');
      del.title = '删除';
      del.onclick = function () {
        chip.parentNode.removeChild(chip);
        syncKeepExpand();
      };
      chip.appendChild(del);
      chips.appendChild(chip);
    });
    syncKeepExpand();

    // 列映射
    var lines = [];
    var cmap = (c.schema && c.schema.column_map) || {};
    Object.keys(cmap).forEach(function (canon) {
      lines.push(canon + ' = ' + (cmap[canon] || []).join(', '));
    });
    $('cfgColumnMap').value = lines.join('\n');
    $('cfgRequiredHint').textContent = ((c.schema && c.schema.required) || []).join('、') || '（空）';
    $('cfgOptionalHint').textContent = ((c.schema && c.schema.optional) || []).join('、') || '（空）';

    // 数值参数
    var grid = $('cfgParams');
    clear(grid);
    var algoWrap = el('label', 'cfg-num');
    algoWrap.appendChild(el('span', null, '算法版本'));
    var sel = el('select');
    (cfg.algorithms || []).forEach(function (a) {
      var o = el('option', null, a);
      o.value = a;
      if (c.analysis && c.analysis.algo === a) { o.selected = true; }
      sel.appendChild(o);
    });
    algoWrap.appendChild(sel);
    algoWrap.appendChild(el('small', null, '算法实现位于引擎包，可插拔替换'));
    grid.appendChild(algoWrap);
    sel.id = 'cfgAlgo';

    NUM_FIELDS.forEach(function (f) {
      var wrap = el('label', 'cfg-num');
      wrap.appendChild(el('span', null, f.label));
      var inp = el('input');
      inp.type = 'number';
      inp.step = 'any';
      inp.value = num((c.analysis && c.analysis[f.sect] || {})[f.key]);
      inp.dataset.sect = f.sect;
      inp.dataset.key = f.key;
      wrap.appendChild(inp);
      wrap.appendChild(el('small', null, f.hint));
      grid.appendChild(wrap);
    });

    // 报告文案
    $('cfgHeader').value = (c.report && c.report.header) || '';
    $('cfgFooter').value = (c.report && c.report.footer) || '';
    $('cfgSummary').checked = !(c.report && c.report.append_summary === false);
    $('cfgHoles').checked = !(c.report && c.report.fill_holes === false);

    $('cfgKeepExpand').textContent = cfg.keep_expand && cfg.keep_expand.notes
      ? '展开规则：' + cfg.keep_expand.notes.join('；') : '';
  }

  function syncKeepExpand() {
    var cols = [];
    var chips = $('cfgKeepChips').childNodes;
    for (var i = 0; i < chips.length; i++) {
      var t = chips[i].firstChild ? chips[i].firstChild.nodeValue : '';
      if (t) { cols.push(t); }
    }
    var note = '将提交给「过滤器」的字段条目：' + (cols.join('、') || '（空）') +
      '（保存后自动展开为别名全集）';
    $('cfgKeepExpand').textContent = note;
  }

  function addKeepChip(value) {
    var v = (value || '').trim();
    if (!v) { return; }
    var chips = $('cfgKeepChips').childNodes;
    for (var i = 0; i < chips.length; i++) {
      if (chips[i].firstChild && chips[i].firstChild.nodeValue === v) { return; }
    }
    var chip = el('span', 'chip', v);
    var del = el('span', 'chip-del', '✕');
    del.onclick = function () { chip.parentNode.removeChild(chip); syncKeepExpand(); };
    chip.appendChild(del);
    $('cfgKeepChips').appendChild(chip);
    syncKeepExpand();
  }

  function collectKeep() {
    var out = [];
    var chips = $('cfgKeepChips').childNodes;
    for (var i = 0; i < chips.length; i++) {
      var t = chips[i].firstChild ? chips[i].firstChild.nodeValue : '';
      if (t) { out.push(t); }
    }
    return out;
  }

  function parseColumnMap(text) {
    var map = {};
    (text || '').split('\n').forEach(function (line) {
      var p = line.split('=');
      if (p.length < 2) { return; }
      var canon = p[0].trim();
      if (!canon) { return; }
      var aliases = p.slice(1).join('=').split(',').map(function (x) { return x.trim(); })
        .filter(function (x) { return x; });
      if (aliases.length) { map[canon] = aliases; }
    });
    return map;
  }

  function saveConfig() {
    var analysis = { algo: $('cfgAlgo') ? $('cfgAlgo').value : 'v2' };
    var inputs = $('cfgParams').querySelectorAll('input[data-key]');
    for (var i = 0; i < inputs.length; i++) {
      var inp = inputs[i];
      var sect = inp.dataset.sect;
      if (!analysis[sect]) { analysis[sect] = {}; }
      var v = parseFloat(inp.value);
      analysis[sect][inp.dataset.key] = isFinite(v) ? v : 0;
    }
    var payload = {
      filter: { keep_columns: collectKeep() },
      schema: { column_map: parseColumnMap($('cfgColumnMap').value) },
      analysis: analysis,
      report: {
        header: $('cfgHeader').value,
        footer: $('cfgFooter').value,
        append_summary: $('cfgSummary').checked,
        fill_holes: $('cfgHoles').checked
      }
    };
    api('/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function () {
      setTip($('cfgSaveTip'), '✓ 配置已保存');
      return loadConfig();
    }).catch(function (e) {
      setTip($('cfgSaveTip'), e.message, true);
    });
  }

  function loadConfig() {
    return api('/config').then(function (j) {
      state.config = j;
      renderConfigPanel();
      return j;
    });
  }

  // ---------------------------------------------------------------- 上传与自检

  function renderFileInfo(up) {
    var box = $('fileInfo');
    clear(box);
    box.hidden = false;
    var left = el('span');
    left.appendChild(el('b', null, up.filename));
    left.appendChild(el('span', 'muted',
      '　' + (up.size / 1024).toFixed(1) + ' KB　原始数据行 ' + up.row_count +
      '　过滤后 ' + (up.filter ? up.filter.rows : '-') + ' 行'));
    box.appendChild(left);
    box.appendChild(el('span', 'muted', '上传暂存 30 分钟内有效'));
  }

  function renderCheck(up) {
    var card = $('checkCard');
    card.hidden = false;

    var sum = $('checkSummary');
    clear(sum);
    var pills = [
      ['算法 ' + s(up.algorithm), ''],
      ['过滤后数据行 ' + s(up.filter.rows), ''],
      [up.schema.can_analyze ? '必需字段齐备' : '缺少必需字段', up.schema.can_analyze ? 'ok' : 'bad']
    ];
    pills.forEach(function (p) {
      sum.appendChild(el('span', 'pill' + (p[1] ? ' ' + p[1] : ''), p[0]));
    });

    var rows = (up.schema.fields || []).map(function (f) {
      return [
        { text: f.required ? '必需' : '可选', cls: f.required ? 'wrap' : 'wrap' },
        f.label + '（' + f.canonical + '）',
        f.matched
          ? { text: f.matched, cls: 'ok-mark' }
          : { text: f.required ? '✗ 未命中（无法分析）' : '— 未提供（将降级处理）',
              cls: f.required ? 'miss-mark' : 'muted' }
      ];
    });
    renderTable($('checkTable'), ['类别', '字段', '命中的表头'], rows);

    var kept = $('keptChips');
    clear(kept);
    (up.filter.kept || []).forEach(function (k) {
      var label = k.column + (k.matched && k.matched !== k.column ? '（匹配 ' + k.matched + '）' : '');
      kept.appendChild(el('span', 'chip', label));
    });
    if (!(up.filter.kept || []).length) { kept.appendChild(el('span', 'chip miss', '无')); }

    var removed = $('removedChips');
    clear(removed);
    (up.filter.removed || []).forEach(function (k) { removed.appendChild(el('span', 'chip', k)); });
    if (!(up.filter.removed || []).length) { removed.appendChild(el('span', 'chip', '无')); }

    var prev = up.preview || [];
    renderTable($('previewTable'), prev.length ? prev[0].map(function (h) { return s(h); }) : [],
      prev.slice(1));

    // 模式与提示
    state.mode = up.mode_default === 'llm' ? 'llm' : 'hard';
    var radios = document.getElementsByName('mode');
    for (var i = 0; i < radios.length; i++) {
      radios[i].checked = radios[i].value === state.mode;
      radios[i].disabled = radios[i].value === 'llm' && !up.llm_configured;
    }
    if (!up.llm_configured && state.mode === 'llm') {
      state.mode = 'hard';
      for (var j = 0; j < radios.length; j++) { radios[j].checked = radios[j].value === 'hard'; }
    }
    $('modeHint').textContent = up.llm_configured
      ? '大模型辅助可用于表头语义关联（如「开始时间」↔「时间」）；开关切换只影响最终分析时执行的过滤，上方自检为硬过滤预演。'
      : '「过滤器」插件尚未配置大模型，大模型辅助不可用；请在过滤器插件页面完成 API 地址 / Key / 模型配置。';
    $('llmRow').className = 'mode-row' + (up.llm_configured ? '' : ' disabled');

    $('analyzeBtn').disabled = !up.schema.can_analyze;
    $('runStatus').className = 'status' + (up.schema.can_analyze ? ' ok' : ' err');
    $('runStatus').textContent = up.schema.can_analyze ? '自检通过，可生成报告' : up.schema.hint;
  }

  function doUpload(file) {
    if (!file) { return; }
    var fd = new FormData();
    fd.append('file', file);
    $('runStatus').className = 'status';
    $('runStatus').textContent = '正在上传并自检…';
    $('analyzeBtn').disabled = true;

    api('/upload', { method: 'POST', body: fd }).then(function (up) {
      state.staged = up;
      state.result = null;
      $('resultBox').hidden = true;
      $('resultEmpty').hidden = false;
      renderFileInfo(up);
      renderCheck(up);
    }).catch(function (e) {
      $('runStatus').className = 'status err';
      $('runStatus').textContent = e.message;
      $('checkCard').hidden = true;
      $('fileInfo').hidden = true;
    });
  }

  // ---------------------------------------------------------------- 分析

  function startAnalyze() {
    if (!state.staged || state.polling) { return; }
    var radios = document.getElementsByName('mode');
    for (var i = 0; i < radios.length; i++) { if (radios[i].checked) { state.mode = radios[i].value; } }

    $('analyzeBtn').disabled = true;
    $('progressBar').hidden = false;
    $('runStatus').className = 'status';
    $('runStatus').textContent = '提交任务…';

    api('/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ staged_id: state.staged.staged_id, mode: state.mode })
    }).then(function (j) {
      poll(j.task_id, 0);
    }).catch(function (e) {
      finishRun();
      $('runStatus').className = 'status err';
      $('runStatus').textContent = e.message;
    });
  }

  function poll(taskId, tick) {
    state.polling = true;
    if (tick > 300) {
      finishRun();
      $('runStatus').className = 'status err';
      $('runStatus').textContent = '分析超时（超过 6 分钟），请检查数据量或大模型配置后重试。';
      return;
    }
    api('/result/' + taskId).then(function (j) {
      if (j.status === 'done') {
        finishRun();
        state.result = j;
        renderResult(j);
        $('runStatus').className = 'status ok';
        $('runStatus').textContent = '分析完成';
        return;
      }
      if (j.status === 'error') {
        finishRun();
        $('runStatus').className = 'status err';
        $('runStatus').textContent = j.detail || '分析失败';
        return;
      }
      $('runStatus').textContent = (j.step ? j.step + '…' : '处理中…') +
        '（' + (state.mode === 'llm' ? '大模型辅助模式' : '硬过滤模式') + '）';
      setTimeout(function () { poll(taskId, tick + 1); }, 1200);
    }).catch(function (e) {
      finishRun();
      $('runStatus').className = 'status err';
      $('runStatus').textContent = e.message;
    });
  }

  function finishRun() {
    state.polling = false;
    $('progressBar').hidden = true;
    $('analyzeBtn').disabled = !(state.staged && state.staged.schema && state.staged.schema.can_analyze);
  }

  // ---------------------------------------------------------------- 结果渲染

  function badge(kind) {
    var cls = kind === '有效出行' ? 'trip' : (kind === '位置变动' ? 'shift'
      : (kind === '停留' ? 'stay' : 'gap'));
    return { text: s(kind), cls: 'badge ' + cls };
  }

  function renderResult(j) {
    var q = j.quality || {};
    var rep = j.report || {};
    var summary = rep.summary || {};

    $('resultEmpty').hidden = true;
    $('resultBox').hidden = false;

    // 摘要数字
    var stats = [
      ['号码数', summary['号码数']],
      ['停留点数', summary['停留点数']],
      ['有效出行', summary['有效出行次数']],
      ['位置变动', summary['位置变动次数']],
      ['最远位移', fmtDistance(summary['最远位移_米'])],
      ['最长停留', fmtDuration(summary['最长停留_分钟'])],
      ['时间跨度', summary['时间跨度']],
      ['报告条目', summary['报告条目数']]
    ];
    var grid = $('statGrid');
    clear(grid);
    stats.forEach(function (p) {
      var box = el('div', 'stat');
      box.appendChild(el('div', 'k', p[0]));
      box.appendChild(el('div', 'v', p[1] === undefined || p[1] === null ? '-' : p[1]));
      grid.appendChild(box);
    });

    // 报告正文
    var text = Object.keys(rep.text_by_user || {}).map(function (k) {
      return rep.text_by_user[k];
    }).join('\n\n');
    $('reportText').textContent = text || '（无）';

    // 下载 / 复制
    var dl = $('downloadBtn');
    dl.href = j.download || '#';
    dl.setAttribute('download', j.filename || '轨迹速写报告.xlsx');
    $('outNote').textContent = '算法 ' + s(j.algo) +
      '；过滤模式 ' + (j.filter && j.filter.mode === 'llm' ? '大模型辅助' : '硬过滤') +
      '；后处理替换 ' + num(j.filter && j.filter.replace_count) + ' 处' +
      '；去重后轨迹点 ' + num(q['去重后轨迹点数']) + ' 个。';

    // 停留点
    var stays = j.stays || [];
    $('stayCount').textContent = '共 ' + stays.length + ' 个';
    renderTable($('stayTable'),
      ['号码', '进入时间', '离开时间', '停留时长', '代表地址', '坐标点数', '抖动半径'],
      stays.map(function (x) {
        return [s(x['USERNUM']), s(x['进入时间']), s(x['离开时间']), s(x['停留时长']),
                s(x['代表地址']), s(x['涉及坐标点数']), s(x['抖动半径_米']) + ' 米'];
      }), [4]);

    // 出行段
    var trips = j.trips || [];
    $('tripCount').textContent = '共 ' + trips.length + ' 段';
    renderTable($('tripTable'),
      ['判定', '开始时间', '结束时间', '时长', '净位移', '均速(km/h)', '直线度', '回访', '起点 → 终点'],
      trips.map(function (x) {
        return [badge(x['判定']), s(x['开始时间']), s(x['结束时间']), s(x['时长']),
                s(x['距离']), s(x['均速_公里每小时']), s(x['直线度']),
                s(x['回访次数']), s(x['起点']) + ' → ' + s(x['终点'])];
      }), [8]);

    // 数据质量
    var qrows = [];
    Object.keys(q).forEach(function (k) {
      var v = q[k];
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        Object.keys(v).forEach(function (k2) { qrows.push([k + ' · ' + k2, s(v[k2])]); });
      } else if (Array.isArray(v)) {
        qrows.push([k, v.join(' ~ ')]);
      } else {
        qrows.push([k, s(v)]);
      }
    });
    if (j.schema && j.schema.matched) {
      Object.keys(j.schema.matched).forEach(function (k) {
        qrows.push(['列映射 · ' + k, s(j.schema.matched[k])]);
      });
    }
    qrows.push(['实际保留字段', (j.filter && (j.filter.kept || []).map(function (x) {
      return x.column + (x.matched && x.matched !== x.column ? '（匹配 ' + x.matched + '）' : '');
    }).join('、')) || '（无）']);
    qrows.push(['实际删除字段', ((j.filter && j.filter.removed) || []).join('、') || '（无）']);
    renderTable($('qualityTable'), ['指标', '数值'], qrows);

    var warnBox = $('warnBox');
    var warns = (j.warnings || []).concat((q['质量提示'] || []));
    if (warns.length) {
      warnBox.hidden = false;
      warnBox.textContent = '提示：\n· ' + warns.join('\n· ');
    } else {
      warnBox.hidden = true;
    }

    // 地点簇
    var clusters = j.clusters || [];
    renderTable($('clusterTable'), ['地点簇', '代表地址', '小区数', '上报次数', '抖动半径'],
      clusters.map(function (x) {
        return [s(x.clu), s(x.address), s(x.n_cell), s(x.n), s(x.jitter_m) + ' 米'];
      }), [1]);

    $('footAlgo').textContent = '轨迹速写 · 分析引擎 ' + s(j.algo) + '（可插拔算法包）';
  }

  function fmtDuration(minutes) {
    var m = Number(minutes);
    if (!isFinite(m) || m <= 0) { return '-'; }
    if (m < 60) { return Math.round(m) + '分钟'; }
    return (m / 60).toFixed(1).replace(/\.0$/, '') + '小时';
  }

  function fmtDistance(meters) {
    var m = Number(meters);
    if (!isFinite(m) || m <= 0) { return '-'; }
    if (m < 1000) { return Math.round(m) + '米'; }
    if (m < 10000) { return (m / 1000).toFixed(1).replace(/\.0$/, '') + '公里'; }
    return Math.round(m / 1000) + '公里';
  }

  function copyReport() {
    var text = $('reportText').textContent || '';
    var done = function () { setTip($('runStatus'), '✓ 报告全文已复制'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
    } else {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { /* 忽略 */ }
    document.body.removeChild(ta);
  }

  // ---------------------------------------------------------------- 重置

  function resetAll() {
    state.staged = null;
    state.result = null;
    state.polling = false;
    $('fileInput').value = '';
    $('fileInfo').hidden = true;
    $('fileInfo').textContent = '';
    $('checkCard').hidden = true;
    $('resultBox').hidden = true;
    $('resultEmpty').hidden = false;
    $('progressBar').hidden = true;
    $('analyzeBtn').disabled = true;
    $('runStatus').className = 'status';
    $('runStatus').textContent = '';
    var radios = document.getElementsByName('mode');
    for (var i = 0; i < radios.length; i++) { radios[i].checked = radios[i].value === 'hard'; }
    state.mode = 'hard';
  }

  // ---------------------------------------------------------------- 启动

  function boot() {
    loadIcons().then(function () {
      fillIconSlot('headIcon', 'report');
      fillIconSlot('dzIcon', 'upload');
      fillIconSlot('checkIcon', 'filter');
      fillIconSlot('summaryIcon', 'summary');
      fillIconSlot('reportIcon', 'report');
      fillIconSlot('stayIcon', 'clock');
      fillIconSlot('tripIcon', 'route');
      fillIconSlot('qualityIcon', 'quality');
      fillIconSlot('clusterIcon', 'user');
      fillIconSlot('dlIcon', 'download');
      fillIconSlot('copyIcon', 'copy');
      fillIconSlot('cfgIcon', 'settings');
      $('footAlgo').textContent = '轨迹速写 · 分析引擎（可插拔算法包）';

      var dz = $('dropzone');
      dz.onclick = function () { $('fileInput').click(); };
      ['dragenter', 'dragover'].forEach(function (ev) {
        dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('drag'); });
      });
      ['dragleave', 'drop'].forEach(function (ev) {
        dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('drag'); });
      });
      dz.addEventListener('drop', function (e) {
        var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        doUpload(f);
      });
      $('fileInput').onchange = function () { doUpload(this.files && this.files[0]); };

      $('analyzeBtn').onclick = startAnalyze;
      $('resetBtn').onclick = resetAll;
      $('copyBtn').onclick = copyReport;
      $('cfgSaveBtn').onclick = saveConfig;
      $('cfgKeepAdd').onclick = function () {
        addKeepChip($('cfgKeepInput').value);
        $('cfgKeepInput').value = '';
      };
      $('cfgKeepInput').onkeydown = function (e) {
        if (e.key === 'Enter') { e.preventDefault(); $('cfgKeepAdd').click(); }
      };
      $('cfgKeepReset').onclick = function () {
        clear($('cfgKeepChips'));
        ['BEGINTIME', 'USERNUM', 'LAI', 'CI', 'ADDRESS', 'LONGITUDE', 'LATITUDE'].forEach(addKeepChip);
      };

      api('/status').then(function (j) {
        state.status = j;
        renderBanner();
        return loadConfig();
      }).catch(function (e) {
        var banner = $('envBanner');
        banner.hidden = false;
        banner.className = 'banner err';
        banner.textContent = '初始化失败：' + e.message;
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
