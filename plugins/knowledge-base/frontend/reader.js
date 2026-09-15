/* 知识库插件 —— 阅读视图渲染器
 * 分派（阶段 8）：office(docx/doc → dhr 引擎、xlsx/xls → xhr 引擎：服务端按需渲染的
 * HTML 片段，样式还原、连续单页不分页、有磁盘缓存) / pdf(PDF.js，原生 PDF 文件) /
 * ofd(EasyOFD) / docx(mammoth 降级) / xlsx·xls·csv(SheetJS 降级) / md(marked) / txt(原生)。
 * Office 预览由 GET /preview 返回 {kind, html, warnings}：Word 注入流式文档，
 * Excel 注入自带页签的表格（前端只做页签显隐接线，与引擎 runtime 行为一致）；
 * 拉取/渲染失败自动回退 mammoth / SheetJS 简化渲染（不阻断阅读）。
 * 所有第三方库自 frontend/vendor/ 惰性按需注入（离线内网，禁止运行时 CDN）；
 * mammoth / marked / 引擎 HTML 一律经 DOMPurify 消毒后再 innerHTML（防 XSS）。
 * 只读保证：渲染容器不可编辑，仅提供选择复制。
 */
(function () {
  "use strict";

  var V = "./vendor/";
  var VIEWER_ID = "kb-ofd-viewer";

  var callbacks = null;   // { onReady, onPage, onFail }
  var current = null;     // 当前文件元数据
  var cleanupFns = [];    // 视图销毁时的清理函数

  // ==================== 基础工具 ====================
  function $(id) { return document.getElementById(id); }

  function container() { return $("reader-container"); }

  function status(msg) {
    var el = $("reader-status");
    if (msg == null) {
      el.className = "reader-status hidden";
    } else {
      el.textContent = msg;
      el.className = "reader-status";
    }
  }

  function onCleanup(fn) { cleanupFns.push(fn); }

  function runCleanups() {
    cleanupFns.forEach(function (fn) {
      try { fn(); } catch (e) { /* 忽略 */ }
    });
    cleanupFns = [];
  }

  function copyText(text) {
    if (!text) return Promise.resolve(false);
    // 优先 Clipboard API，失败回退 execCommand（内网老 Chrome / 非 https 场景）
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; })
        .catch(function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }

  function legacyCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;left:-9999px;top:0";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (e) {
      return false;
    }
  }

  // ==================== 惰性脚本注入 ====================
  var loadedScripts = {};

  function loadScript(url) {
    if (loadedScripts[url]) return loadedScripts[url];
    loadedScripts[url] = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = url;
      s.onload = resolve;
      s.onerror = function () {
        delete loadedScripts[url];
        reject(new Error("渲染库加载失败：" + url));
      };
      document.head.appendChild(s);
    });
    return loadedScripts[url];
  }

  // 阅读视图每次打开都重新拉取字节流（Range 缓存由框架处理）
  // url 为空 = 默认 /raw（渲染件）；word/excel 的 PDF 预览传 /pdf
  function fetchUrl(file, url) {
    if (!url) url = "/api/knowledge-base/files/" + encodeURIComponent(file.id) + "/raw";
    return fetch(url)
      .then(function (res) {
        if (res.status === 401) {
          location.href = "/login?next=" + encodeURIComponent("/tool/knowledge-base");
          return new Promise(function () {});
        }
        if (!res.ok) throw new Error("文件读取失败（" + res.status + "）");
        return res.arrayBuffer();
      });
  }

  function fetchRaw(file) { return fetchUrl(file, null); }

  function officePreviewUrl(file) {
    return "/api/knowledge-base/files/" + encodeURIComponent(file.id) + "/preview";
  }

  // ==================== 通用 HTML 内容缩放（Word/Excel/MD/文本等） ====================
  // 仅缩放预览内容（CSS zoom：随内容重排、滚动条正确），不改浏览器页面缩放。
  // 缩放作用目标是**背景卡片 .reader-frame** 而不是内容元素：卡片承载白底/圆角/
  // 阴影/内边距，只缩内容会让表格放大后溢出卡片（xls 等宽内容尤其明显）——
  // 连卡片一起缩放，观感与 PDF（画布带动卡片变宽）一致。PDF/OFD 走各自引擎的
  // 原生缩放，不经此路径。
  var htmlZoom = { el: null, scale: 1 };
  var HTML_ZOOM_MIN = 0.5, HTML_ZOOM_MAX = 3.0, HTML_ZOOM_STEP = 0.1;

  function zoomFrame() { return document.querySelector(".reader-frame"); }

  function htmlZoomApply() {
    if (htmlZoom.el) {
      // 居中交给 CSS 的 margin:0 auto（卡片比视口窄→居中；比视口宽→auto 边距
      // 自动归 0 + #view-reader 横向滚动，内容全部可达），JS 不干预对齐。
      htmlZoom.el.style.zoom = String(htmlZoom.scale);
    }
    if (callbacks && callbacks.onZoom) callbacks.onZoom(htmlZoom.scale);
  }

  function clampHtmlZoom(s) {
    return Math.min(HTML_ZOOM_MAX, Math.max(HTML_ZOOM_MIN, Math.round(s * 100) / 100));
  }

  function htmlZoomIn() {
    htmlZoom.scale = clampHtmlZoom(htmlZoom.scale + HTML_ZOOM_STEP);
    htmlZoomApply();
  }

  function htmlZoomOut() {
    htmlZoom.scale = clampHtmlZoom(htmlZoom.scale - HTML_ZOOM_STEP);
    htmlZoomApply();
  }

  function htmlZoomSet(scale) {
    var s = Number(scale);
    if (!isFinite(s)) return;
    htmlZoom.scale = clampHtmlZoom(s);
    htmlZoomApply();
  }

  // 渲染器注册缩放目标（渲染完内容后调用）；scale 每次渲染重置为 1。
  // el 参数是内容元素，仅在拿不到背景卡片时兜底（结构变化时缩放不失效）。
  function setZoomTarget(el) {
    htmlZoom.el = zoomFrame() || el;
    htmlZoom.scale = 1;
  }

  // 渲染前/销毁时清掉卡片上的缩放残留（卡片是静态 DOM，不随渲染重建）
  function resetFrameZoom() {
    var frame = zoomFrame();
    if (frame) frame.style.zoom = "";
  }

  // ==================== 渲染器：Office 预览（服务端 xhr/dhr 双引擎） ====================
  // 后端按需渲染并缓存：Word = dhr 流式文档片段（<style>+<div class="kbdoc">）；
  // Excel = xhr 表格片段（<div class="kbsheet">，含 .kbsheet-tabs 页签与多个
  // .kbsheet-sheet[hidden]）。前端只负责注入与页签显隐接线（行为对齐引擎 runtime：
  // 点 .kbsheet-tab → 同步 aria-selected + 兄弟 sheet 的 hidden）。404 = 引擎缺失/
  // 渲染失败 → 调用方回退降级渲染器。引擎输出全部文本已转义且 URL/字体白名单，
  // 这里仍过 DOMPurify（纵深防御）。
  function fetchPreview(file) {
    return fetch(officePreviewUrl(file)).then(function (res) {
      if (res.status === 401) {
        location.href = "/login?next=" + encodeURIComponent("/tool/knowledge-base");
        return new Promise(function () {});
      }
      if (!res.ok) throw new Error("预览读取失败（" + res.status + "）");
      return res.json();
    });
  }

  function renderOffice(file) {
    return Promise.all([fetchPreview(file), loadScript(V + "purify/purify.min.js?v=1")])
      .then(function (results) {
        var data = results[0];
        if (!data || !data.ok || !data.html) throw new Error("预览数据为空");
        var isSheet = data.kind === "sheet";
        // DOMPurify 会整块丢弃 <style> 元素（引擎的 class 型 CSS 全在里面），
        // 因此先把 <style> 摘出来，只对正文消毒，CSS 原样挂回——CSS 为引擎生成
        // 的静态内容（字体名白名单、无 URL 注入面），不经过消毒是安全的。
        var cssParts = [];
        var body = data.html.replace(/<style[^>]*>([\s\S]*?)<\/style>/gi,
          function (m0, css) { cssParts.push(css); return ""; });
        var box = document.createElement("div");
        box.className = "kb-office" + (isSheet ? " kb-office-sheet" : " kb-office-word");
        box.innerHTML = window.DOMPurify.sanitize(body, { USE_PROFILES: { html: true } });
        if (cssParts.length) {
          var st = document.createElement("style");
          st.textContent = cssParts.join("\n");
          box.insertBefore(st, box.firstChild);
        }
        container().appendChild(box);
        setZoomTarget(box);   // Word/Excel 引擎预览支持内容缩放
        // Excel 页签接线（与 xhr 引擎 runtime 同款逻辑：hidden 属性切显隐）
        var activeSheet = null;
        if (isSheet) {
          box.addEventListener("click", function (ev) {
            var t = ev.target;
            if (!t || !t.className || String(t.className).indexOf("kbsheet-tab") < 0) return;
            var idx = t.getAttribute("data-target");
            var tabs = box.querySelectorAll(".kbsheet-tab");
            var sheets = box.querySelectorAll(".kbsheet-sheet");
            for (var i = 0; i < tabs.length; i++) {
              tabs[i].setAttribute("aria-selected", String(i === Number(idx)));
            }
            for (var j = 0; j < sheets.length; j++) {
              sheets[j].hidden = String(j) !== idx;
            }
          });
          var sheets = box.querySelectorAll(".kbsheet-sheet");
          for (var k = 0; k < sheets.length; k++) {
            if (!sheets[k].hidden) { activeSheet = sheets[k]; break; }
          }
        }
        status(null);
        callbacks.onReady(isSheet ? (box.querySelectorAll(".kbsheet-tab").length > 1 ? "复制本表" : "复制全文")
                                  : "复制全文");
        return {
          copy: function () {
            if (!isSheet) return box.innerText || "";
            // 当前展示 sheet 的表格提取纯文本（保留行列制表分隔）
            var host = activeSheet || box;
            var rows = host.querySelectorAll("tr");
            var lines = [];
            for (var r = 0; r < rows.length; r++) {
              var cells = rows[r].querySelectorAll("td,th");
              var parts = [];
              for (var c = 0; c < cells.length; c++) parts.push(cells[c].textContent);
              lines.push(parts.join("\t"));
            }
            return lines.join("\n");
          }
        };
      });
  }

  // ==================== 渲染器：纯文本 ====================
  function renderText(file) {
    return fetchRaw(file).then(function (buf) {
      var text = new TextDecoder("utf-8").decode(buf);
      var div = document.createElement("div");
      div.className = "paper";
      var pre = document.createElement("div");
      pre.className = "plain-text";
      pre.textContent = text;
      div.appendChild(pre);
      container().appendChild(div);
      status(null);
      callbacks.onReady("复制全文");
      var full = text;
      return {
        copy: function () { return full; }
      };
    });
  }

  // ==================== 渲染器：Markdown ====================
  function renderMarkdown(file) {
    return Promise.all([fetchRaw(file), loadScript(V + "marked/marked.min.js?v=1"), loadScript(V + "purify/purify.min.js?v=1")])
      .then(function (results) {
        var buf = results[0];
        var text = new TextDecoder("utf-8").decode(buf);
        var html = window.marked.parse(text);
        var clean = window.DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
        var div = document.createElement("div");
        div.className = "paper";
        div.innerHTML = clean;
        container().appendChild(div);
        setZoomTarget(div);
        status(null);
        callbacks.onReady("复制原文");
        return {
          copy: function () { return text; } // 复制 Markdown 原文
        };
      });
  }

  // ==================== 渲染器：Word（.docx） ====================
  function renderDocx(file) {
    return Promise.all([fetchRaw(file), loadScript(V + "mammoth/mammoth.browser.min.js?v=1"), loadScript(V + "purify/purify.min.js?v=1")])
      .then(function (results) {
        var buf = results[0];
        return window.mammoth.convertToHtml({ arrayBuffer: buf }).then(function (result) {
          var clean = window.DOMPurify.sanitize(result.value || "", { USE_PROFILES: { html: true } });
          var div = document.createElement("div");
          div.className = "paper";
          div.innerHTML = clean || "<p>（空文档）</p>";
          container().appendChild(div);
          status(null);
          callbacks.onReady("复制全文");
          return {
            copy: function () { return div.innerText || div.textContent || ""; }
          };
        });
      });
  }

  // ==================== 渲染器：Excel（.xlsx/.xls/.csv） ====================
  function renderSheet(file) {
    return Promise.all([fetchRaw(file), loadScript(V + "xlsx/xlsx.full.min.js?v=1")])
      .then(function (results) {
        var buf = results[0];
        var wb = window.XLSX.read(buf, { type: "array" });
        // 外层缩放容器：SheetJS 降级渲染产两个兄弟节点（页签 + 表格区），
        // 统一包一层供通用内容缩放（style.zoom）作用
        var zoomBox = document.createElement("div");
        zoomBox.className = "kb-sheet-fallback";
        container().appendChild(zoomBox);
        var tabs = document.createElement("div");
        tabs.className = "sheet-tabs";
        var wrap = document.createElement("div");
        wrap.className = "sheet-table-wrap";
        zoomBox.appendChild(tabs);
        zoomBox.appendChild(wrap);
        setZoomTarget(zoomBox);
        var names = wb.SheetNames || [];
        var tableHtmls = {};
        names.forEach(function (name) {
          var ws = wb.Sheets[name];
          tableHtmls[name] = window.XLSX.utils.sheet_to_html(ws, { header: "", footer: "" })
            .replace(/<table/, '<table class="sheet-table"');
        });
        function show(name) {
          wrap.innerHTML = tableHtmls[name] || "";
          Array.prototype.forEach.call(tabs.children, function (b) {
            b.className = b.textContent === name ? "sheet-tab active" : "sheet-tab";
          });
        }
        names.forEach(function (name) {
          var b = document.createElement("button");
          b.type = "button";
          b.className = "sheet-tab";
          b.textContent = name;
          b.onclick = function () { show(name); };
          tabs.appendChild(b);
        });
        if (names.length) show(names[0]);
        else wrap.innerHTML = '<div class="empty">（空工作簿）</div>';
        status(null);
        callbacks.onReady(names.length > 1 ? "复制本表" : "复制全文");
        var activeName = names[0];
        tabs.onclick = function (ev) {
          var t = ev.target;
          if (t.className.indexOf("sheet-tab") >= 0) activeName = t.textContent;
        };
        return {
          copy: function () {
            // 从当前展示的 HTML 表格提取纯文本（保留行列制表分隔）
            var html = tableHtmls[activeName] || "";
            var tmp = document.createElement("div");
            tmp.innerHTML = html;
            var rows = tmp.querySelectorAll("tr");
            var lines = [];
            for (var i = 0; i < rows.length; i++) {
              var cells = rows[i].querySelectorAll("td,th");
              var parts = [];
              for (var j = 0; j < cells.length; j++) parts.push(cells[j].textContent);
              lines.push(parts.join("\t"));
            }
            return lines.join("\n");
          }
        };
      });
  }

  // ==================== 渲染器：PDF（PDF.js，流式连续布局） ====================
  var pdfState = null;

  // url 为空 = 默认 /raw（渲染件）；word/excel 的 PDF 预览传 /pdf
  // 流式连续布局：全部页面纵向排列（页间留间隔），占位框按各页比例预留尺寸，
  // 进入视口附近才渲染（IntersectionObserver，Chrome 72 可用）——
  // 大文档不卡首屏；页码指示随滚动更新，‹ › 改为滚动到上/下一页。
  function renderPdf(file, opts) {
    var src = (opts && opts.url) ? fetchUrl(file, opts.url) : fetchRaw(file);
    return Promise.all([src, loadScript(V + "pdf/pdf.min.js?v=1")])
      .then(function (results) {
        var buf = results[0];
        var pdfjs = window.pdfjsLib;
        pdfjs.GlobalWorkerOptions.workerSrc = V + "pdf/pdf.worker.min.js?v=1";
        return pdfjs.getDocument({ data: new Uint8Array(buf) }).promise;
      })
      .then(function (doc) {
        var total = doc.numPages;
        var scale = 1.25;   // 默认放大展示（A4 在 scale 1.0 下仅约 595px 宽，偏小）
        var dpr = window.devicePixelRatio || 1;
        var gen = 0;        // 渲染代次：缩放重排后旧任务完成也作废（防旧 scale 画布误入）
        var box = document.createElement("div");
        box.className = "pdf-scroll";
        container().appendChild(box);

        var wraps = [null];     // 1-based：每页占位容器
        var rendered = [false]; // 1-based：画布是否已渲染
        var tasks = {};         // page -> renderTask（在途任务，缩放/销毁时取消）
        var queue = [];         // 待渲染页号
        var pumping = false;
        var current = 1;
        var firstDone = false;
        var io = null;
        var scrollBound = false;

        // 设备像素对齐（清晰度关键）：画布必须落在整数设备像素上，合成器才会 1:1 贴图。
        // 页面居中（margin:0 auto）与 A4 自动高度（841.89pt × 1.25 = 1052.36px）都会算出
        // 小数位置，乘 dpr 后相位落回小数，合成器随即对整张画布做双线性重采样——实测
        // 边缘对比度只剩 64%（整数定位 100%），观感就是"分辨率低、发糊"。
        // 做法：先清零量出自然相位，再一次性补偿到网格上（位移 <1 个设备像素，肉眼不可见，
        // 且只向左/上偏移，不会向右下溢出容器）。刻意不做迭代累积——布局会对小数边距取整，
        // 累积会让画布整体漂移出几像素的居中偏差。
        function snapCanvasToDevicePixel(canvas) {
          if (!(dpr > 0)) return;
          canvas.style.marginLeft = "";
          canvas.style.marginTop = "";
          var r = canvas.getBoundingClientRect();
          if (!r.width) return;                         // 未布局/不可见，跳过
          var fx = (r.left * dpr) % 1;
          var fy = (r.top * dpr) % 1;
          if (fx >= 0.002 && fx <= 0.998) canvas.style.marginLeft = (-fx / dpr) + "px";
          if (fy >= 0.002 && fy <= 0.998) canvas.style.marginTop = (-fy / dpr) + "px";
        }

        function snapAll() {
          for (var i = 1; i <= total; i++) {
            if (!rendered[i]) continue;
            var cv = wraps[i].querySelector("canvas");
            if (cv) snapCanvasToDevicePixel(cv);
          }
        }

        // 渲染后布局仍可能异步变化（滚动条出现/消失、状态条收起都会改变居中位置），
        // 因此除了渲染当次校正，再在布局稳定后补校一次；resize 同理。
        var snapTimer = null;
        function scheduleSnap(delay) {
          if (snapTimer) clearTimeout(snapTimer);
          snapTimer = setTimeout(snapAll, delay || 150);
        }

        var resizeBound = false;
        function onResizeSnap() { scheduleSnap(120); }

        function bindResize() {
          if (resizeBound) return;
          resizeBound = true;
          window.addEventListener("resize", onResizeSnap, false);
        }

        // 先取全部页面的元数据 viewport（不渲染，开销小），据此建占位框
        var metas = [];
        for (var mi = 1; mi <= total; mi++) metas.push(doc.getPage(mi));
        return Promise.all(metas).then(function (pages) {
          var meta = [];
          pages.forEach(function (pg) { meta[pg.pageNumber] = pg.getViewport({ scale: 1 }); });

          for (var i = 1; i <= total; i++) {
            var w = document.createElement("div");
            w.className = "pdf-page-box loading";
            w.setAttribute("data-page", i);
            w.style.width = (meta[i].width * scale) + "px";
            w.style.height = (meta[i].height * scale) + "px";
            box.appendChild(w);
            wraps[i] = w;
          }

          function enqueue(p) {
            if (!rendered[p] && queue.indexOf(p) < 0) { queue.push(p); pump(); }
          }

          function renderPage(p) {
            if (rendered[p]) return Promise.resolve();
            var g = gen;
            return doc.getPage(p).then(function (pg) {
              if (g !== gen || rendered[p]) return null;
              var base = pg.getViewport({ scale: scale * dpr });
              // 位图尺寸取整后再反推显示尺寸：位图与显示尺寸严格 1 设备像素对 1 位图像素。
              // （直接给 canvas 赋小数会被截断，1000px 宽上会累积出 0.25px 级缩放，
              //   合成器随即对整页做双线性重采样。）
              var bw = Math.max(1, Math.round(base.width));
              var bh = Math.max(1, Math.round(base.height));
              var canvas = document.createElement("canvas");
              canvas.width = bw;
              canvas.height = bh;
              canvas.style.width = (bw / dpr) + "px";
              canvas.style.height = (bh / dpr) + "px";
              var ctx = canvas.getContext("2d");
              var task = pg.render({ canvasContext: ctx, viewport: base });
              tasks[p] = task;
              return task.promise.then(function () {
                delete tasks[p];
                if (g !== gen || rendered[p]) return null; // 缩放竞态：旧代次产物丢弃
                var w = wraps[p];
                w.innerHTML = "";
                w.className = "pdf-page-box";
                w.appendChild(canvas);
                snapCanvasToDevicePixel(canvas);
                scheduleSnap(160);
                rendered[p] = true;
                if (!firstDone) {
                  firstDone = true;
                  status(null);
                  if (callbacks.onReady) callbacks.onReady("复制本页");
                }
                if (p === current && callbacks.onPage) callbacks.onPage(current, total, scale);
                return null;
              }).catch(function (e) {
                delete tasks[p];
                // 缩放/销毁导致的主动取消不算错误
                if (e && e.name === "RenderingCancelledException") return null;
                throw e;
              });
            });
          }

          function pump() {
            if (pumping) return;
            pumping = true;
            function step() {
              if (!queue.length) { pumping = false; return; }
              // 离视口中心最近的页优先渲染
              var mid = window.pageYOffset + window.innerHeight / 2;
              var best = -1, bestDist = Infinity, bestIdx = -1;
              for (var i = 0; i < queue.length; i++) {
                var w = wraps[queue[i]];
                var top = w.getBoundingClientRect().top + window.pageYOffset;
                var d = Math.abs(top + w.offsetHeight / 2 - mid);
                if (d < bestDist) { bestDist = d; best = queue[i]; bestIdx = i; }
              }
              queue.splice(bestIdx, 1);
              return renderPage(best).then(step, step);
            }
            Promise.resolve().then(step);
          }

          function updateCurrent() {
            var mid = window.pageYOffset + window.innerHeight / 2;
            var best = 1, bestDist = Infinity;
            for (var i = 1; i <= total; i++) {
              var w = wraps[i];
              var top = w.getBoundingClientRect().top + window.pageYOffset;
              var d = Math.abs(top + w.offsetHeight / 2 - mid);
              if (d < bestDist) { bestDist = d; best = i; }
            }
            if (best !== current) {
              current = best;
              if (callbacks.onPage) callbacks.onPage(current, total, scale);
            }
          }

          var scrollTick = false;
          function onScroll() {
            if (scrollTick) return;
            scrollTick = true;
            setTimeout(function () { scrollTick = false; updateCurrent(); }, 120);
          }

          function bindScroll() {
            if (scrollBound) return;
            scrollBound = true;
            window.addEventListener("scroll", onScroll, false);
          }

          // 缩放：取消在途任务 → 清已渲染画布 → 重设占位尺寸 → 视口附近重新入队；
          // 保持滚动位置比例（内容总高变了，按比例还原阅读位置）
          function applyScale(ns) {
            scale = ns;
            gen++;
            var max = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
            var ratio = (max > window.innerHeight)
              ? window.pageYOffset / (max - window.innerHeight) : 0;
            for (var tp in tasks) { try { tasks[tp].cancel(); } catch (e) { /* 忽略 */ } }
            queue.length = 0;
            for (var i = 1; i <= total; i++) {
              var w = wraps[i];
              if (rendered[i]) {
                w.innerHTML = "";
                w.className = "pdf-page-box loading";
                rendered[i] = false;
              }
              w.style.width = (meta[i].width * scale) + "px";
              w.style.height = (meta[i].height * scale) + "px";
            }
            // IO 对"已相交未变化"的元素不会重复回调，手动把视口附近的页入队
            var vh = window.innerHeight;
            for (var j = 1; j <= total; j++) {
              var r = wraps[j].getBoundingClientRect();
              if (r.bottom > -1200 && r.top < vh + 1200) enqueue(j);
            }
            if (callbacks.onPage) callbacks.onPage(current, total, scale);
            setTimeout(function () {
              var max2 = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
              window.scrollTo(0, ratio * Math.max(0, max2 - window.innerHeight));
              updateCurrent();
            }, 80);
          }

          function scrollToPage(p) {
            if (p < 1 || p > total) return;
            current = p;
            var top = wraps[p].getBoundingClientRect().top + window.pageYOffset - 70;
            window.scrollTo(0, top);
            scheduleSnap(150);   // 跳页后滚动位置变化，重校一次设备像素相位
            if (callbacks.onPage) callbacks.onPage(current, total, scale);
          }

          function pageText(p) {
            return doc.getPage(p).then(function (pg) {
              return pg.getTextContent().then(function (tc) {
                var lines = [];
                var last = null;
                var line = "";
                (tc.items || []).forEach(function (it) {
                  if (last !== null && it.transform[5] !== last) { lines.push(line); line = ""; }
                  line += it.str;
                  last = it.transform[5];
                });
                if (line) lines.push(line);
                return lines.join("\n");
              });
            });
          }

          if (window.IntersectionObserver) {
            io = new IntersectionObserver(function (entries) {
              entries.forEach(function (en) {
                if (!en.isIntersecting) return;
                var p = Number(en.target.getAttribute("data-page"));
                enqueue(p);
              });
            }, { rootMargin: "1200px 0px" });
            for (var oi = 1; oi <= total; oi++) io.observe(wraps[oi]);
          } else {
            // 极老内核无 IO：退化为顺序渲染全部页
            for (var ai = 1; ai <= total; ai++) queue.push(ai);
          }
          bindScroll();
          bindResize();

          pdfState = {
            prevPage: function () { scrollToPage(current - 1); },
            nextPage: function () { scrollToPage(current + 1); },
            zoomIn: function () { if (scale < 3) applyScale(Math.min(3, scale + 0.2)); },
            zoomOut: function () { if (scale > 0.4) applyScale(Math.max(0.4, scale - 0.2)); },
            // 绝对缩放（百分比输入框用）：与 ± 按钮同一套 clamp 与重渲染管线
            setZoom: function (s) {
              s = Number(s);
              if (!isFinite(s)) return;
              s = Math.min(3, Math.max(0.4, s));
              if (Math.abs(s - scale) > 0.001) applyScale(s);
            },
            getZoom: function () { return scale; },
            copy: function () { return pageText(current); },
            onPageChange: function () { if (callbacks.onPage) callbacks.onPage(current, total, scale); }
          };
          onCleanup(function () {
            gen++;
            if (io) { try { io.disconnect(); } catch (e) { /* 忽略 */ } }
            if (scrollBound) window.removeEventListener("scroll", onScroll, false);
            if (resizeBound) window.removeEventListener("resize", onResizeSnap, false);
            if (snapTimer) clearTimeout(snapTimer);
            for (var tp in tasks) { try { tasks[tp].cancel(); } catch (e) { /* 忽略 */ } }
            try { doc.destroy(); } catch (e) { /* 忽略 */ }
            pdfState = null;
          });
          pump();
        });
      });
  }

  // ==================== 渲染器：OFD（EasyOFD） ====================
  var ofdState = null;

  function renderOfd(file) {
    // 依赖顺序必须为 x2js → jszip → opentype → eaysjbig2 → easyofd（见 VENDOR.md）
    return Promise.all([
      fetchRaw(file),
      loadScript(V + "ofd/x2js.js?v=1"),
      loadScript(V + "ofd/jszip.min.js?v=1"),
      loadScript(V + "ofd/opentype.min.js?v=1"),
      loadScript(V + "ofd/eaysjbig2.js?v=1"),
      loadScript(V + "ofd/easyofd.js?v=1")
    ]).then(function (results) {
      var buf = results[0];
      if (!window.EasyOFD) throw new Error("EasyOFD 渲染库未能加载");
      // 容器：EasyOFD 会在此元素内创建自己的 DOM（id=VIEWER_ID）
      var host = document.createElement("div");
      host.className = "easyofd-host";
      container().appendChild(host);

      var blob = new Blob([buf]);
      var inst = new window.EasyOFD(VIEWER_ID, host);
      // 调整其内置画布容器样式（去除灰底/限高，融入本插件纸张风格）
      if (inst.canvasdiv) {
        inst.canvasdiv.style.backgroundColor = "transparent";
        inst.canvasdiv.style.maxHeight = "none";
        inst.canvasdiv.style.maxWidth = "100%";
        inst.canvasdiv.style.padding = "6px 0";
      }
      var cancelTimer = null;
      var settled = false;
      function waitReady() {
        var t0 = Date.now();
        cancelTimer = setInterval(function () {
          if (settled) return;
          if (inst.view && inst.view.AllPageNo > 0) {
            settled = true;
            clearInterval(cancelTimer);
            status(null);
            callbacks.onReady("复制本页");
            callbacks.onPage(inst.view.pageNow, inst.view.AllPageNo, inst.zoomSize);
          } else if (Date.now() - t0 > 30000) {
            settled = true;
            clearInterval(cancelTimer);
            throw new Error("OFD 文档解析超时");
          }
        }, 120);
      }
      onCleanup(function () {
        if (cancelTimer) clearInterval(cancelTimer);
        ofdState = null;
        // EasyOFD 未提供 destroy：随容器 innerHTML 清空一起回收
      });
      waitReady();
      inst.loadFromBlob(blob);

      ofdState = {
        prevPage: function () { inst.PrePage(); afterNav(); },
        nextPage: function () { inst.NextPage(); afterNav(); },
        zoomIn: function () { inst.ZoomIn(); afterNav(); },
        zoomOut: function () { inst.ZoomOut(); afterNav(); },
        // EasyOFD 只有步进缩放（ZoomIn/ZoomOut），没有绝对设置接口：
        // 用步进逼近目标比例（有上限防死循环；到边界或步长不再变化即停）
        setZoom: function (target) {
          target = Number(target);
          if (!isFinite(target)) return;
          target = Math.min(3, Math.max(0.4, target));
          var cur = Number(inst.zoomSize) || 1;
          var tries = 0;
          while (Math.abs(cur - target) > 0.02 && tries < 24) {
            if (cur < target) inst.ZoomIn(); else inst.ZoomOut();
            var now = Number(inst.zoomSize);
            if (!isFinite(now) || now === cur) break;   // 已到引擎边界
            cur = now;
            tries++;
          }
          afterNav();
        },
        getZoom: function () { return Number(inst.zoomSize) || 1; },
        copy: function () {
          try {
            return String(inst.GetPageText(inst.view.pageNow - 1) || "");
          } catch (e) {
            return "";
          }
        },
        onPageChange: function () { afterNav(); }
      };
      function afterNav() {
        if (inst.view && inst.view.AllPageNo > 0) {
          callbacks.onPage(inst.view.pageNow, inst.view.AllPageNo, inst.zoomSize);
        }
      }
    });
  }

  // ==================== 分派与生命周期 ====================
  var RENDERERS = {
    pdf: renderPdf,
    ofd: renderOfd,
    docx: renderDocx,
    xlsx: renderSheet,
    xls: renderSheet,
    csv: renderSheet,
    md: renderMarkdown,
    markdown: renderMarkdown,
    txt: renderText
  };

  // 需要页码导航的格式（按**实际使用的渲染器**判定，不按扩展名：
  // word/excel 转出 PDF 后同样需要翻页/缩放）
  var NAV_FORMATS = { pdf: 1, ofd: 1 };

  function render(file, cbs) {
    destroy();
    callbacks = cbs || {};
    current = file;
    htmlZoom.el = null;
    htmlZoom.scale = 1;
    resetFrameZoom();   // 上一份文档的卡片缩放不带到新文档
    // ★预览分派（阶段 8）：Word/Excel → 服务端 xhr/dhr 引擎按需渲染的 HTML
    //   预览（样式还原、连续单页），失败自动回退降级渲染器（mammoth / SheetJS）；
    //   其余格式按扩展名分派（原生 PDF 仍走 PDF.js）。
    var fn;
    if (file.ext === "docx" || file.ext === "doc" ||
        file.ext === "xlsx" || file.ext === "xls") {
      var officeFallback = RENDERERS[file.ext];
      fn = function () {
        return renderOffice(file).catch(function () {
          // 预览拉取/渲染失败 → 自动回退简化渲染（不阻断阅读）
          if (!officeFallback) throw new Error("预览不可用");
          status("样式预览不可用，已回退简化渲染");
          return officeFallback(file);
        });
      };
    } else {
      fn = RENDERERS[file.ext];
    }
    if (!fn) {
      status("暂不支持在线阅读 ." + file.ext + " 格式，可下载原件查看");
      return;
    }
    status("加载中…");
    var copyHandler = null;
    window.KBReader.copy = function () {
      if (!copyHandler) return Promise.resolve();
      return Promise.resolve().then(copyHandler).then(function (text) {
        return copyText(text).then(function (ok) {
          if (ok) toast("已复制到剪贴板");
          else toast("复制失败，请手动选择文本复制", true);
        });
      }).catch(function (e) {
        toast("复制失败：" + (e && e.message ? e.message : "未知错误"), true);
      });
    };
    fn(file).then(function (h) {
      if (h) copyHandler = h.copy;
    }).catch(function (e) {
      status(e && e.message ? e.message : "文档加载失败");
      if (callbacks.onFail) callbacks.onFail(e && e.message ? e.message : "文档加载失败");
    });
  }

  function destroy() {
    runCleanups();
    callbacks = null;
    current = null;
    pdfState = null;
    ofdState = null;
    resetFrameZoom();
    window.KBReader.copy = function () { return Promise.resolve(); };
  }

  // ==================== 对外接口（app.js 调用） ====================
  // 缩放分派：PDF/OFD 用引擎原生缩放（重渲染/自带 API），
  // 其余格式走通用内容缩放（CSS zoom 作用在背景卡片上，不改浏览器页面缩放）。
  window.KBReader = {
    render: render,
    destroy: destroy,
    prevPage: function () { if (pdfState) pdfState.prevPage(); else if (ofdState) ofdState.prevPage(); },
    nextPage: function () { if (pdfState) pdfState.nextPage(); else if (ofdState) ofdState.nextPage(); },
    zoomIn: function () {
      if (pdfState) return pdfState.zoomIn();
      if (ofdState) return ofdState.zoomIn();
      htmlZoomIn();
    },
    zoomOut: function () {
      if (pdfState) return pdfState.zoomOut();
      if (ofdState) return ofdState.zoomOut();
      htmlZoomOut();
    },
    // 绝对缩放（0.4~3.0；HTML 内容为 0.5~3.0，各自 clamp）——百分比输入框用
    setZoom: function (scale) {
      if (pdfState) return pdfState.setZoom(scale);
      if (ofdState) return ofdState.setZoom(scale);
      htmlZoomSet(scale);
    },
    getZoom: function () {
      if (pdfState) return pdfState.getZoom();
      if (ofdState) return ofdState.getZoom();
      return htmlZoom.scale;
    },
    copy: function () { return Promise.resolve(); },
    onResize: function () { /* EasyOFD 容器自适应，无需处理 */ }
  };
})();
