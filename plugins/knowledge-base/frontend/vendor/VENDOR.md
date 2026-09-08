# Vendor 第三方库清单（知识库插件）

> 本目录库文件随插件源码分发（离线内网可用，禁止运行时 CDN 依赖，规范 F-7）。
> 更新版本时必须同步更新本清单。

| 目录 | 文件 | 库 / 版本 | 来源 | 许可证 | 用途 |
| --- | --- | --- | --- | --- | --- |
| pdf/ | pdf.min.js · pdf.worker.min.js | pdfjs-dist 3.11.174（legacy 构建） | https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/legacy/build/ | Apache-2.0 | PDF 逐页 Canvas 渲染 + 文本提取（复制本页） |
| ofd/ | easyofd.js | EasyOFD 1.2.1（easyofd npm 包） | https://registry.npmjs.org/easyofd/-/easyofd-1.2.1.tgz | Apache-2.0 © ZhangXinPan | OFD 渲染：Canvas 绘制 + 内置文本选择层（复制）+ 页码/缩放 API |
| ofd/ | x2js.js | x2js 3.4.0（Axinom） | easyofd 包内 lib/ | Apache-2.0 | EasyOFD 依赖：OFD 内 XML → JS 对象 |
| ofd/ | jszip.min.js | JSZip 3.10.1 | https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js | MIT/MIT-Style | EasyOFD 依赖：OFD 包（zip）解压 |
| ofd/ | opentype.min.js | opentype.js | easyofd 包内 lib/ | MIT | EasyOFD 依赖：路径图元字体支持 |
| ofd/ | eaysjbig2.js | easyjbig2（ZhangXinPan） | easyofd 包内 lib/ | Apache-2.0 | EasyOFD 依赖：JB2 图片解码（window.JB2） |
| mammoth/ | mammoth.browser.min.js | mammoth 1.6.0 | https://cdn.jsdelivr.net/npm/mammoth@1.6.0/mammoth.browser.min.js | BSD-2-Clause | .docx → HTML（输出必须过 DOMPurify） |
| xlsx/ | xlsx.full.min.js | SheetJS CE 0.18.5 | https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js | Apache-2.0 | .xlsx/.xls/.csv → HTML 表格 |
| marked/ | marked.min.js | marked 4.3.0（UMD） | https://cdn.jsdelivr.net/npm/marked@4.3.0/marked.min.js | MIT | Markdown → HTML（输出必须过 DOMPurify） |
| purify/ | purify.min.js | DOMPurify 3.0.6 | https://cdn.jsdelivr.net/npm/dompurify@3.0.6/dist/purify.min.js | Apache-2.0/MPL-2.0/GPL | HTML 消毒（mammoth/marked 输出） |

## 加载顺序约定（reader.js 惰性注入时必须遵守）

- OFD（EasyOFD UMD 读取全局依赖，顺序必须为）：`x2js` → `jszip` → `opentype` → `eaysjbig2` → `easyofd`
  （easyofd 头部：`e((t=globalThis).EasyOFD={}, t.JSZip, t.X2JS, t.opentype, t.JB2)`）
- PDF：仅 `pdf.min.js`（worker 由其内部按同目录路径加载 `pdf.worker.min.js`，二者必须同目录）
- 其余各自单文件，无顺序依赖

## EasyOFD 集成要点（reader.js 实现时遵守）

- 构造：`new EasyOFD(id, containerDiv)` —— 会注入自带工具栏/水印/选择文件按钮，
  与本插件 Material UI 冲突：容器用独立包裹层，`.ofd-toolbar`、`.ofd-watermark`、
  `.ofd-select-file` 等注入元素以 CSS 隐藏（见 style.css），导航/缩放/复制由本插件顶栏调用其 API 实现：
  - 页码：`FirstPage/PrePage/NextPage/LastPage`、`view.pageNow`、`view.AllPageNo`
  - 缩放：`ZoomIn/ZoomOut/FitWidth/FitPage`（zoomSize 0.25~3）
  - 文本：`GetPageText(pageIndex0)`（复制本页）、`GetAllText()`（复制全文）
  - 渲染：`loadFromBlob(blob)` 载入，`RenderAllPages()` 重绘
- `t._injectStyles()` 会向 `<head>` 注入其样式表（id=easyofd-styles），含 `.ofd-toolbar` 等选择器，
  隐藏规则须在注入后仍生效（style.css 中用同等或更高优先级）。
