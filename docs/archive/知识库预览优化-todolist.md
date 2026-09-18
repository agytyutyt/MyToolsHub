# TODO —— 知识库插件预览优化（2026-09-14）

> **✅ 已归档（2026-09-17）** —— 归档原因：**三项需求全部完成（`[x]`）**，
> 已随 knowledge-base **1.2.1 / 1.2.2 / 1.2.3** 三个版本交付：
> ① `.doc` 归一化警告按渲染模式分流（vendor dhr 第 5 处补丁，`PREVIEW_CACHE_VERSION` 2→3）；
> ② PDF 预览改流式连续布局（`reader.js::renderPdf` + `.pdf-scroll`）；
> ③ dock 常驻全格式内容缩放（0.5~3.0，PDF/OFD 走引擎原生缩放）。
> **现行落点**：`plugins/knowledge-base/README.md` 与
> `docs/design/知识库Office预览-设计文档.md`；设计约束看 `docs/design/知识库插件-设计文档.md`。
> 本文件**内容不再维护**。
> 文中路径引用已按 2026-09-17 的 `docs/` 分层结构更新。


> 三项需求：① 修复 .doc 预览顶部误导性提示；② PDF 预览改流式连续布局；
> ③ 底部工具栏新增全格式内容缩放（仅缩放内容，不改浏览器缩放）。

## 背景与根因

1. **.doc 提示问题**：`vendor/dhr/__init__.py::normalize_doc` 对所有 .doc 一律插入
   `"…分页位置可能不精确，建议使用流式模式"` 警告，被 renderer 渲染成预览顶部横幅。
   但插件自阶段 8 起 `office_render.render_word` **恒以 mode="flow"（流式）渲染**，
   "建议使用流式模式" 对用户是误导（已经在流式模式了）。
2. **PDF 翻页**：`reader.js::renderPdf` 同一时刻只渲染当前页，靠 ‹ › 翻页，
   需求为连续滚动（所有页面纵向排列、按需渲染）。
3. **缩放**：现缩放按钮在 `#reader-pager` 内（仅 PDF/OFD 显示），
   需求为所有预览格式都能在底部 dock 缩放内容。

## 分步实施

### 步骤 1：修复 .doc 误导提示（后端）

- [x] `backend/vendor/dhr/__init__.py`：
  `normalize_doc(data, soffice_path=None, mode=None)` 增加 mode 参数；
  flow 模式改用准确文案（"…个别复杂版式可能与原件存在细微差异"），
  非 flow 模式保留原文案；`convert()` 调用时透传 `opts.mode`。
- [x] `backend/vendor/README.md` 补丁清单登记为第 5 处补丁（上游待同步）。
- [x] `backend/routes.py`：`PREVIEW_CACHE_VERSION` 2 → 3
  （旧 .doc 预览缓存里的旧警告横幅需重新生成）。

### 步骤 2：PDF 预览改流式连续布局（前端）

- [x] `frontend/reader.js::renderPdf` 重写：
  - 拉起全部页面的元数据 viewport，先建占位框（按各页比例预留高度），
    页与页纵向连续排列，页间留间隔；
  - 按需渲染：IntersectionObserver（Chrome 72 可用）观察进入视口附近（rootMargin
    预热区）的页才入渲染队列，队列按"离视口中心最近优先"逐页渲染；
  - 页码指示随滚动更新（滚动时计算最接近视口中心的页）；
    ‹ › 按钮改为滚动到上/下一页（不再触发重渲染）；
  - 缩放仍重渲染：改 scale → 清空已渲染画布、按新 scale 重设占位尺寸、
    重新入队，保持滚动位置比例不变；
  - 「复制本页」语义保留：复制当前可见页文本；`doc.destroy()` 清理不变。
- [x] `frontend/style.css`：`.pdf-scroll` 连续容器与占位页样式。

### 步骤 3：底部工具栏全格式内容缩放（前端）

- [x] `frontend/index.html`：dock 重排——缩放组（－ / 100% / ＋）拆出
  `#reader-pager` 成为常驻 `#reader-zoom`；翻页组仅 PDF/OFD 显示；
  静态资源版本号递增（style.css v=25 / reader.js v=7 / app.js v=17）。
- [x] `frontend/reader.js`：
  - 新增通用 HTML 缩放（`htmlZoom = {el, scale}`，范围 0.5~3.0、步进 0.1），
    对渲染出的内容元素设 `style.zoom`（Chrome 全系支持、布局重排、
    **不触碰浏览器页面缩放**）；Word/Excel 引擎预览、mammoth 降级、
    SheetJS、Markdown、纯文本各渲染器注册各自的缩放目标元素；
  - `KBReader.zoomIn/zoomOut` 分派优先级：PDF/OFD 用引擎原生缩放，
    其余格式走 HTML 缩放；`onZoom` 回调同步指示器。
- [x] `frontend/app.js`：`readerCbs` 增加 `onZoom`（更新 zoom-indicator）；
  打开/关闭阅读器时重置缩放指示为 100%。
- [x] `frontend/style.css`：`.zoom` 组样式（复用 icon-btn/zoom-indicator）。

### 步骤 4：测试与文档同步

- [x] 后端：临时数据根 + venv 起服务，验证 .doc 预览 warnings 新文案、
  缓存版本 3 生效（旧缓存不命中重新渲染）。
- [x] 前端：agent-browser 走查 PDF 连续滚动 / 页码 / 缩放保位 /
  Word·Excel·MD·TXT 缩放、dock 布局。
- [x] 文档回写：`plugins/knowledge-base/README.md`、
  `docs/design/知识库插件-设计文档.md`（PDF 渲染器与缩放描述）、`HANDOFF.md` 状态头。
- [x] 当日工作日志 `.workbuddy/memory/2026-09-14.md`。

## 风险与注意

- **缓存版本必须同步递增**：不升 `PREVIEW_CACHE_VERSION`，旧警告横幅会被缓存
  继续展示，表现为"改了没生效"。
- PDF 大文件：全量占位 + 按需渲染，避免一次渲染几百页卡死；渲染队列必须可取消
  （缩放/销毁时 cancel 在途任务）。
- 缩放用 CSS `zoom`（非 transform）：随内容重排、滚动条正确；Firefox 旧版不支持，
  但目标环境为内网 Chrome（基线 ≥72），可接受。
- vendor 补丁必须登记 README，升级整目录替换后需重打。
