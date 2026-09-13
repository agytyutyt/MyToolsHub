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
        var tabs = document.createElement("div");
        tabs.className = "sheet-tabs";
        var wrap = document.createElement("div");
        wrap.className = "sheet-table-wrap";
        container().appendChild(tabs);
        container().appendChild(wrap);
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

  // ==================== 渲染器：PDF（PDF.js） ====================
  var pdfState = null;

  // url 为空 = 默认 /raw（渲染件）；word/excel 的 PDF 预览传 /pdf
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
        var page = 1;
        var total = doc.numPages;
        var scale = 1.25; // 默认放大展示（A4 在 scale 1.0 下仅约 595px 宽，偏小）
        var box = document.createElement("div");
        container().appendChild(box);
        var renderTask = null;

        function renderCurrent() {
          if (renderTask) { try { renderTask.cancel(); } catch (e) { /* 忽略 */ } }
          status("渲染第 " + page + " 页…");
          return doc.getPage(page).then(function (pg) {
            var dpr = window.devicePixelRatio || 1;
            var base = pg.getViewport({ scale: scale * dpr });
            var canvas = document.createElement("canvas");
            canvas.width = base.width;
            canvas.height = base.height;
            canvas.style.width = (base.width / dpr) + "px";
            canvas.style.height = (base.height / dpr) + "px";
            var ctx = canvas.getContext("2d");
            renderTask = pg.render({ canvasContext: ctx, viewport: base });
            return renderTask.promise.then(function () {
              box.innerHTML = "";
              var wrap = document.createElement("div");
              wrap.className = "pdf-page-box";
              wrap.appendChild(canvas);
              box.appendChild(wrap);
              status(null);
              if (callbacks.onReady) callbacks.onReady("复制本页");
              callbacks.onPage(page, total, scale);
            }).catch(function (e) {
              // 用户翻页/缩放导致的主动取消不算错误
              if (e && e.name === "RenderingCancelledException") return;
              throw e;
            });
          });
        }

        function safeRender() {
          renderCurrent().catch(function (e) {
            status("PDF 渲染失败：" + (e && e.message ? e.message : "未知错误"));
          });
        }

        pdfState = {
          prevPage: function () { if (page > 1) { page--; safeRender(); } },
          nextPage: function () { if (page < total) { page++; safeRender(); } },
          zoomIn: function () { if (scale < 3) { scale = Math.min(3, scale + 0.2); safeRender(); } },
          zoomOut: function () { if (scale > 0.4) { scale = Math.max(0.4, scale - 0.2); safeRender(); } },
          copy: function () {
            return doc.getPage(page).then(function (pg) {
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
          },
          onPageChange: function () { callbacks.onPage(page, total, scale); }
        };
        onCleanup(function () {
          if (renderTask) { try { renderTask.cancel(); } catch (e) { /* 忽略 */ } }
          try { doc.destroy(); } catch (e) { /* 忽略 */ }
          pdfState = null;
        });
        safeRender();
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
    window.KBReader.copy = function () { return Promise.resolve(); };
  }

  // ==================== 对外接口（app.js 调用） ====================
  window.KBReader = {
    render: render,
    destroy: destroy,
    prevPage: function () { if (pdfState) pdfState.prevPage(); else if (ofdState) ofdState.prevPage(); },
    nextPage: function () { if (pdfState) pdfState.nextPage(); else if (ofdState) ofdState.nextPage(); },
    zoomIn: function () { if (pdfState) pdfState.zoomIn(); else if (ofdState) ofdState.zoomIn(); },
    zoomOut: function () { if (pdfState) pdfState.zoomOut(); else if (ofdState) ofdState.zoomOut(); },
    copy: function () { return Promise.resolve(); },
    onResize: function () { /* EasyOFD 容器自适应，无需处理 */ }
  };
})();
