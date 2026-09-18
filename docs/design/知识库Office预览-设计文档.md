# 插件库优化方案 —— 知识库插件 Word/Excel 转 PDF 预览与文档下载

| 文档属性 | 内容 |
| --- | --- |
| 文档版本 | v1.2（2026-09-11） |
| --- | --- |
| 适用插件 | `knowledge-base`（知识库） |
| 文档状态 | **全部阶段（0~5）已完成**：含真实 LibreOffice（25.8.7）环境下的端到端验证 |
| 实施记录 | §13（本轮实施落地与踩坑） |
| 关联文档 | 《docs/知识库插件-设计文档.md》（本方案的基线，下称「原设计」）、《插件设计规范.md》（B-1~B-8、SEC-1~6、F-1~F-7、8.5 异步任务、9.2 归属铁律）、《HANDOFF.md》§7 踩坑 / §7.5 速查表 |

---

## 0. 一页速览（接手者先读这里）

**要做什么**：知识库上传 Word（doc/docx）与 Excel（xls/xlsx）后，服务端把文档**转成 PDF 用于在线阅读**（保留原文档样式）；**原件与 PDF 双份保存**；**全部类型文件提供下载**，其中 Word/Excel 的下载给的是**原始文档**（不是 PDF）。

**核心方案**：

1. 存储从「单文件」变「多件套」：原件（`<id>.<原始扩展名>`）＋ 降级渲染件（doc/xls 转的 `<id>.docx/.xlsx`，现状逻辑保留）＋ 展示 PDF（`<id>.pdf`，新增）。
2. PDF 转换引擎主选 **LibreOffice headless**（`soffice --headless --convert-to pdf`，保真度最高、免费、离线可用）；探测不到引擎时**优雅降级**为现状前端 mammoth/SheetJS 渲染，功能不受损。
3. 转换走**后台线程池异步**（规范 8.5）：上传立即返回（`pdf_status=pending`），转换完成后回写 `pdf_status=ok`；阅读端按 `pdf_ready` 自动在「PDF 渲染 / 降级渲染」间分派。
4. 阅读视图**零新渲染器**：Word/Excel 的 PDF 复用现有 pdf.js 渲染器（翻页/缩放/复制本页全部现成），只是数据源换成 `/files/<id>/pdf` 端点。
5. 新增 `GET /files/<id>/download`（attachment 下载原件，中文文件名由 Flask `download_name` 自动处理）。

**断点续作**：每个 TODO 项都写明「文件 / 函数 / 关键步骤 / 验证方式」，完成一项把 `[ ]` 改 `[x]` 并附日期。

---

## 1. 需求与边界

### 1.1 需求

1. **Word（.doc/.docx）**：用户上传后服务端转换为 PDF，在线阅读展示 PDF（保留原文档样式，解决 mammoth 渲染丢样式的问题）。
2. **Excel（.xls/.xlsx）**：对**有数据的部分**转换为 PDF（展示原文档样式，解决 SheetJS 渲染无列宽/边框/合并样式的问题）。
3. **双份保存**：上传后同时保存**原始文档**与 **PDF 版本**。
4. **下载功能**：各类型文档均提供下载；其中 **Word/Excel 下载链接给原始文档**（非 PDF 版）；PDF/OFD/MD/TXT/CSV 下载给文件本身。

### 1.2 明确的边界（不做）

| 不做 | 理由 |
| --- | --- |
| OFD 转 PDF | OFD 已有 easyofd 前端渲染，且 OFD→PDF 工具链不成熟；OFD 阅读与下载均走原件 |
| PDF 版 Word/Excel 提供下载 | 需求明确：Word/Excel 下载 = 原始文档；PDF 仅在线展示（`/pdf` 端点 inline、不设 attachment） |
| 在线编辑 / 批注 | 知识库只读定位不变 |
| 全文检索 | 与原设计一致，本期不做 |
| WPS / MS Office COM 自动化转换 | 沿用原设计 §10 的拒绝理由：服务账户/无桌面会话不可靠、有弹窗与授权风险、部署机未必安装 |
| 转换失败阻断上传 | 优雅降级（B-4）：PDF 生成失败不阻断上传与降级阅读，只影响「样式保真预览」这一目标 |

---

## 2. 现状与差距分析

### 2.1 现状（原设计已实现）

- 存储：`data/files/<id>.<ext>` 单文件；`.doc/.xls` 上传时经 `doc_convert.py` 转成 docx/xlsx 落盘，**原件不保留**。
- 阅读：前端按 ext 分派渲染器（pdf.js / easyofd / mammoth / SheetJS / marked），mammoth 只还原文字与粗结构，**样式（字体/字号/颜色/页眉页脚/分栏）基本丢失**；SheetJS 只还原文本表格，**列宽/边框/填充色/合并单元格丢失**。
- 下载：**刻意不提供**（原设计 §1.2「仅阅读 + 复制」）。
- 元数据：`files.json` 有 `ext`（归一化主格式）与 `original_ext`（仅旧格式转换记录有值）。

### 2.2 差距

| 需求 | 现状差距 |
| --- | --- |
| Word/Excel 按原样式展示 | mammoth/SheetJS 保真度不足 → 需服务端转 PDF + pdf.js 展示 |
| 保存原始文档 | doc/xls 原件被转换件覆盖，未保存 → 存储模型需改「原件 + 转换件 + PDF」三件套 |
| 各类型可下载 | 无下载端点、无前端入口 → 新增 download 端点与按钮 |
| Word/Excel 下载给原件 | 原件当前不存在（旧数据）/ 未保留（新逻辑）→ 上传流程需先落盘原件 |

---

## 3. PDF 转换引擎选型

### 3.1 候选对比

| 方案 | 保真度 | 部署依赖 | 服务可靠性 | 打包影响 | 离线内网 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| **LibreOffice headless**（`soffice --headless --convert-to pdf`） | ★★★★★ 接近原生打印 | 目标机需安装 LibreOffice（一次性，约 300MB，内网可分发离线安装包） | 高（官方支持无头转换，无桌面会话可用） | 零（不进 PyInstaller 包，外部进程调用） | ✅ | **主选** |
| 纯 Python 自绘（python-docx/openpyxl 读 + reportlab 排版） | ★★☆ 中等（段落/表格/字体能做，分栏/文本框/复杂页眉页脚难） | reportlab 需打进 exe（+约 5MB） | 高 | spec 增 reportlab | ✅ | 二期可选降级引擎（§7.5），本期不做 |
| docx2pdf / WPS·MS Office COM | ★★★★★ | 目标机装 Office/WPS | **低**（服务账户 COM 不可靠、弹窗、授权）——原设计已否决 | 零 | ✅ | 否决 |
| mammoth/SheetJS → 前端打印为 PDF | ★★ | 无 | — | — | ✅ | 不解决保真问题，否决 |
| 云转换 API | ★★★★ | 外网 | — | — | ❌ 内网不可用 | 否决 |

> 开发机实测环境（2026-09-11）：本机装有 WPS Office 11.8（`winword`/`wps` App Paths 均指向 WPS），**无** MS Office、**无** LibreOffice。WPS 仅用于人工对照验证转换效果，不作为运行时依赖。

### 3.2 引擎探测与降级链

```
上传 Word/Excel
   │
   ▼
检测 PDF 引擎（模块级缓存，/status 可刷新）
   │ soffice 可用？
   ├─ 是 → 转 PDF → pdf_status=ok → 阅读 = pdf.js（原样式）
   └─ 否 → pdf_status=failed（原因：无引擎）
           → 阅读 = 降级渲染件（mammoth/SheetJS，现状体验）+ 可下载原件
```

探测顺序（`pdf_convert.detect_soffice()`）：

1. 插件配置 `backend/config.json` 的 `pdf.soffice_path`（管理员可显式指定，B-5）；
2. 注册表 `HKLM\SOFTWARE\LibreOffice\UNO\InstallPath`（读 `Path` 值，winreg 标准库）；
3. 常见安装路径：`C:\Program Files\LibreOffice\program\soffice.exe`、`C:\Program Files (x86)\LibreOffice\program\soffice.exe`；
4. `shutil.which("soffice")`（PATH）。

探测结果模块级缓存（dict，含 `path/version/available/error`）；`GET /status` 返回该信息并支持 `?refresh=1` 重探（管理员排障用）。

---

## 4. 总体架构

### 4.1 存储布局（数据根目录 `plugins/knowledge-base/data/`）

```
data/
├── categories.json
├── files.json                      # 元数据（新增字段见 §5）
└── files/
    ├── <id>.<original_ext>         # ★原件：所有类型都保存；下载端点专用（权威原件）
    ├── <id>.docx / <id>.xlsx       # 降级渲染件：仅旧版 doc/xls 由 doc_convert 生成
    │                               #   （docx/xlsx 原生上传时，原件本身就是渲染件，不重复存）
    └── <id>.pdf                    # ★展示用 PDF：仅 word/excel 类生成；可能不存在
```

- 路径安全不变：`<id>` 白名单正则 `^[A-Za-z0-9_-]+$`，扩展名白名单二次确认（SEC-1/SEC-3）。
- 磁盘占用：word/excel 类约为原来的 2~3 倍（原件 + 降级件 + PDF）。20MB 上限不变，PDF 产物一般远小于原件；超 20MB 的 PDF 产物仍落盘（不重复卡上限，仅记录大小）。

### 4.2 阅读渲染链路（优先级链）

```
reader.js render(file)
   │
   ├─ file.pdf_ready（= pdf_status=="ok"）
   │     └─ GET /files/<id>/pdf → renderPdf（pdf.js，翻页/缩放/复制本页全复用）
   │
   ├─ 否则 ext ∈ {docx} → GET /files/<id>/raw → mammoth（降级，现状）
   ├─ 否则 ext ∈ {xlsx,xls,csv} → GET /files/<id>/raw → SheetJS（降级，现状）
   └─ 其余（pdf/ofd/md/txt）→ 现状不变
```

要点：**阅读视图不新增渲染器**，只给 `renderPdf` 增加「URL 覆盖」参数（默认 `/raw`，PDF 预览时 `/pdf`）。

### 4.3 转换管线（异步，规范 8.5）

```
POST /files（管理员上传）
  1. 校验（白名单 / 20MB / 空文件）                    —— 现状逻辑不变
  2. 原件落盘 <id>.<original_ext>                     —— ★新增（先于一切转换）
  3. doc/xls → doc_convert 生成降级渲染件 <id>.docx/.xlsx  —— 现状逻辑保留（失败处理见 §7.3）
  4. 写 files.json：pdf_status="pending"，立即返回响应 —— 不等转换（响应含 pdf_status）
  5. 转换任务入队（ThreadPoolExecutor max_workers=1，串行）
        ├─ 成功：<id>.pdf 原子落盘，pdf_status="ok" + pdf_size
        └─ 失败：pdf_status="failed" + pdf_error（用户可读文案）
  6. 前端轮询列表（3s），角标从「PDF 生成中」变正常
```

**断点恢复**：`register()` 启动时扫描 files.json，凡 word/excel 类且 `pdf_status` 字段**缺失或为 pending** 的记录自动入队补转（服务重启/升级后存量自动补齐）；`failed` 不自动重试（避免死循环），管理员可经 retry 端点手动重试。

### 4.4 下载链路

```
卡片下载按钮 / 阅读页 dock 下载按钮（全员可见）
   → GET /api/knowledge-base/files/<id>/download
   → send_file(<id>.<original_ext>, as_attachment=True,
               download_name=original_name)   # Flask 自动 RFC 5987 中文文件名编码
```

- Word/Excel：`<original_ext>` = 用户上传时的原始格式（doc/docx/xls/xlsx）——满足「下载给原文档」。
- PDF/OFD/MD/TXT/CSV：原件即唯一文件，同一下载端点天然适用。
- 权限：全体登录用户（与阅读一致）；ID 正则 + 元数据校验，不存在一律 404（SEC-1/SEC-5）。

---

## 5. 数据模型变更（files.json）

```jsonc
{
  "id": "f-xxxxxxxx",
  "name": "某某培训课件",
  "original_name": "培训课件.doc",     // 现状字段：原始文件名（下载时作 download_name）
  "ext": "docx",                       // 现状字段：归一化主格式（doc→docx、xls→xlsx），渲染分派依据
  "original_ext": "doc",               // ★语义升级：所有记录都有值 = 上传时的扩展名（旧数据迁移回填 = ext）
  "size": 1234567,                     // 现状字段：主落盘件（渲染件）字节数
  "original_size": 2345678,            // ★新增：原件字节数（卡片/详情显示「原件大小」用）
  "pdf_status": "ok",                  // ★新增：none | pending | ok | failed（word/excel 类以外恒 none）
  "pdf_size": 345678,                  // ★新增：PDF 字节数（ok 时有值）
  "pdf_error": "",                     // ★新增：failed 时的用户可读原因
  "category_id": "cat-xxxxxxxx",
  "created_by": "...", "created_by_name": "...", "unit_id": "...", "department_id": "...",
  "created_at": "...", "updated_at": null
}
```

**兼容与迁移**（`register()` 启动时幂等执行，`_migrate_files()`）：

1. 旧记录无 `original_ext` → 回填 `original_ext = ext`（这些记录的原件 = 现有落盘件，下载可用）。
2. 旧记录（含 doc/xls 转来的）无 `pdf_status` → word/excel 类置 `pending` 入队补转，其余类型置 `none`。
3. 迁移只补字段、不动文件、不改语义；重复启动无副作用。

`_file_brief()` 输出新增：`pdf_ready`（bool，= pdf_status=="ok"）、`pdf_status`、`original_ext`（恒有值）、`original_size`。

---

## 6. API 设计（前缀 `/api/knowledge-base`，B-2 合规）

| 方法 | 路径 | 权限 | 变更 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/status` | 登录 | 变更 | 增加 `pdf_engine: {available, path, version, error}`；支持 `?refresh=1` 重新探测 |
| GET | `/files` | 登录 | 变更 | 列表项新增 `pdf_ready / pdf_status / original_ext / original_size` |
| POST | `/files` | 管理员 | 变更 | 流程见 §4.3：先落盘原件 → 降级件照旧 → `pdf_status=pending` 立即返回 → 异步入队；响应新增 `pdf_status` |
| GET | `/files/<id>/raw` | 登录 | 不变 | 仍返回「渲染件」（doc/xls 的 docx/xlsx 转换件，其余=原件），降级渲染用 |
| GET | `/files/<id>/pdf` | 登录 | **新增** | inline 返回展示 PDF（`send_file(conditional=True)` 支持 Range；无 attachment 头）；`pdf_status!=ok` 或文件缺失一律 404 |
| GET | `/files/<id>/download` | 登录 | **新增** | attachment 下载**原件**（`download_name=original_name`）；原件缺失 404 |
| POST | `/files/<id>/pdf-retry` | 管理员 | **新增** | `pdf_status=failed` 的记录重置为 `pending` 并重新入队；非 failed 409 |
| DELETE | `/files/<id>` | 管理员 | 变更 | 删除时同步清理三件套：原件 + 降级渲染件 + PDF（逐个 `os.remove`，缺失跳过） |

错误风格不变：`{"ok": false, "error": "..."}` + 4xx；越权 403、不存在一律 404（SEC-5）。

---

## 7. 后端实现细化

### 7.1 新模块 `backend/pdf_convert.py`

职责：引擎探测 + 单文件转换（纯函数式，不碰 Flask 上下文，便于测试）。

```python
"""知识库 —— Word/Excel → PDF 转换（LibreOffice headless）。"""

class PdfConvertError(Exception):
    """转换失败，携带面向用户的提示文案。"""

def detect_soffice(refresh=False):
    """探测 LibreOffice。返回 {"available": bool, "path": str|None,
    "version": str|None, "error": str|None}。模块级缓存，refresh=True 重探。
    探测顺序：config.json pdf.soffice_path → 注册表 → 常见路径 → PATH。"""

def engine_status():
    """detect_soffice 的对外封装（供 /status）。"""

def convert_to_pdf(src_path, dst_pdf, timeout=180):
    """把 src_path（doc/docx/xls/xlsx）转换为 dst_pdf。
    成功返回 None；失败抛 PdfConvertError。
    实现要点见下。"""
```

`convert_to_pdf` 关键步骤（照抄级细化）：

1. 取 `detect_soffice()`，不可用 → `PdfConvertError("服务器未检测到 LibreOffice，无法生成 PDF 预览")`。
2. 建临时目录 `work = tempfile.mkdtemp(prefix="kbpdf_")`；其中建 `profile/` 子目录作独立用户配置（**避免 soffice 全局单实例冲突**）：
   `-env:UserInstallation=file:///<profile 绝对路径，反斜杠换正斜杠>`
3. 组装命令（**参数列表，禁止 shell 拼接**，SEC-10/B-18）：
   ```python
   cmd = [soffice, "--headless", "--norestore", "--nolockcheck", "--nodefault",
          "-env:UserInstallation=file:///" + profile_uri,
          "--convert-to", "pdf", "--outdir", work, src_path]
   ```
   （`pdf` 不写过滤器名，soffice 按源格式自动选 writer/calc_pdf_Export。）
4. `subprocess.run(cmd, timeout=timeout, capture_output=True)`；
   `TimeoutExpired` → `PdfConvertError("PDF 生成超时（超过 3 分钟），请稍后重试")`。
5. 产物 = `work/<src 基名>.pdf`；不存在或返回码非 0 → `PdfConvertError("PDF 生成失败，请检查文档是否损坏或另存后重试")`。
6. `os.replace(产物, dst_pdf)`（同分区原子）；`finally` 里 `shutil.rmtree(work, ignore_errors=True)`。

**串行化**：模块级 `_CONVERT_LOCK = threading.Lock()`，`convert_to_pdf` 全程持锁（独立 profile 理论上可并发，但 soffice 进程 CPU 开销大，串行最稳，与线程池 max_workers=1 双重保险）。

### 7.2 转换队列（写在 `routes.py` 内，约 60 行，不另起模块）

```python
from concurrent.futures import ThreadPoolExecutor
_PDF_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kb-pdf")

def _enqueue_pdf(fid):
    """入队一个转换任务；任务体内按 fid 读元数据 → 调 pdf_convert → 回写状态。"""

def _pdf_task(fid):
    # 1. _LOCK 内读 files.json 取记录（ext/original_ext/pdf_status），确认仍是 pending
    # 2. 组路径：src = <id>.<original_ext>（word/excel 的原件）；dst = <id>.pdf
    #    —— ★用原件而非降级件做转换源：soffice 原生支持 doc/xls，保真最高
    # 3. try: pdf_convert.convert_to_pdf(src, dst) → ok：回写 pdf_status/pdf_size/pdf_error=""
    #    except PdfConvertError as e → failed + pdf_error=str(e)
    #    except Exception → failed + pdf_error="PDF 生成失败（内部错误）"（SEC-5 不泄堆栈）
    # 4. 回写在 _LOCK 内（先重新 _load_store，防并发覆盖；记录可能已被删除 → 静默放弃并清理 dst）
```

**启动补偿**：`register()` 末尾 `_migrate_files()`（§5）之后，把 word/excel 类且 `pdf_status=="pending"` 的记录逐个 `_enqueue_pdf(fid)`。

### 7.3 `routes.py` 上传流程改动点（`kb_file_upload`）

按现状代码（v1.6）逐步改：

1. **落盘原件**（在现有「旧版格式转换」之前）：
   - 现状：读 `blob` 后 doc/xls 直接转换、`blob/ext` 被替换为转换产物。
   - 改法：转换前先用 `original_ext = ext` 与 `original_blob = blob` 暂存；分配 `fid` 后：
     - doc/xls：原件写 `<fid>.<original_ext>`；再走现状转换逻辑得 docx/xlsx 写 `<fid>.<ext>`。
     - 其他类型：原件 = 主落盘件，只写一次（`<fid>.<ext>`，`original_ext = ext`）。
2. **doc_convert 失败处理放宽**：现状 doc→docx 失败 422 阻断。新逻辑：
   - PDF 引擎可用（`detect_soffice()["available"]`）→ 不再 422：降级件缺失仅意味着降级阅读不可用，记 `convert_warn` 进响应，继续走 PDF 管线；
   - 引擎不可用 → 保持 422（此时降级件是唯一的阅读途径，失败必须告诉用户）。
3. **元数据**：写 `original_ext`（恒有值）、`original_size`、`pdf_status="pending"`（word/excel 类）或 `"none"`（其他）、`pdf_size=None`、`pdf_error=""`。
4. **响应**：`resp["pdf_status"] = "pending"|"none"`；word/excel 类随后 `_enqueue_pdf(fid)`（**在返回响应之前入队、在锁外执行转换**——入队即返回，转换在线程里）。

### 7.4 Excel「有数据的部分」处理策略

- 默认：直接整本喂给 soffice（`calc_pdf_Export` 按各 sheet 的**使用区域**分页导出，空白区域天然不产出页面）——满足「对有数据的部分转换」。
- 已知边缘：单元格带残留格式（无边框但有样式）会扩大使用区域产生空白页。列为**遗留增强**（§12 阶段 6）：转换前用 openpyxl 预扫描生成「trimmed 临时副本」（删除完全空白的 sheet、裁剪有效行列之外的区域）再喂 soffice；注意 openpyxl 重写会丢图表/条件格式，故默认不启用。

### 7.5（二期可选）纯 Python reportlab 降级引擎

仅在「目标机无法安装 LibreOffice」成为现实约束时启动，单独立项：
`backend/pdf_render/`（docx 引擎：python-docx 段落/表格 → reportlab platypus；xlsx 引擎：openpyxl → reportlab Table；CJK 用 `UnicodeCIDFont('STSong-Light')` 或探测系统 simsun/msyh TTF 嵌入；spec PACKAGES + reportlab）。本期不实施，TODO 列在阶段 6。

---

## 8. 前端实现细化

> 版本纪律（F-2）：本轮 `index.html` 引用改为 `style.css?v=20`、`reader.js?v=4`、`app.js?v=14`（当前 v19 / v3 / v13）。
> 图标纪律：新增按钮用**内联单色 SVG**（currentColor，stroke 2，16px），**不用 emoji**（HANDOFF §4 emoji 字体坑 + 工作记忆约定）。

### 8.1 `reader.js`（约改 20 行）

1. `renderPdf(file, opts)`：增加 `opts.url` 覆盖 fetch 地址（默认 `/raw`）：
   - 现状 `renderPdf` 内部用 `fetchRaw(file)`；改为 `fetchUrl(file, opts && opts.url)`，把 `fetchRaw` 抽出自 `fetchRaw(file)` 与 `fetchPdf(file)` 两个薄封装。
2. `render(file, cbs)` 分派前加短路：
   ```js
   if (file.pdf_ready) { /* 走 renderPdf(file, {url:"/pdf"})，NAV_FORMATS 按 pdf 处理 */ }
   ```
   - 复制/翻页/缩放/销毁逻辑零改动（pdf.js 渲染器原样复用）。
   - dock 类型徽标仍显示原格式（DOC/XLS），由 app.js 控制，reader 不管。
3. RENDERERS 表不动；`NAV_FORMATS` 判断改为「实际渲染器是 pdf/ofd」而非按 ext。

### 8.2 `app.js`（约改 80 行）

1. **下载工具函数** `downloadFile(f)`（仿 admin-common.js 的 `download()`）：
   `fetch(API+"/files/"+id+"/download")` → 401 跳登录 → `res.blob()` → 从 `Content-Disposition` 解析 `filename*=`（RFC 5987 解码，回退 `filename=`）→ `URL.createObjectURL` + 临时 `<a download>` 点击 → 回收。
2. **卡片下载按钮**：`renderFiles()` 卡片操作区加「下载」图标按钮（全员可见，不属管理操作）；点击 `event.stopPropagation()` 防触发 openReader。
3. **阅读页 dock 下载按钮**：`openReader()` 里给 `#btn-download` 绑定当前文件；`closeReader()` 解绑。
4. **PDF 状态角标**：`renderFiles()` 在现有「已转换」角标逻辑旁加：
   - `pdf_status=="pending"` → 角标「PDF 生成中」（靛蓝色，转圈动画可选，纯 CSS）；
   - `pdf_status=="failed"` → 角标「预览降级」（琥珀色，悬停 title=`pdf_error`）。
5. **轮询**：上传成功响应 `pdf_status=="pending"` 时启动 `state.pdfPoll`（`setInterval` 3s 调 `loadFiles(true)` 静默刷新，列表中无 pending 项即停，最多 60s 兜底 `clearInterval`）；`openReader` 打开 pending 文件时同款轮询，`pdf_ready` 变 true 后自动重调 `KBReader.render(f)` 切换 PDF 渲染。
6. **说明条**：复用现有 `showReaderNotice`（sessionStorage 记忆关闭，HANDOFF §7-18 教训）扩展两类文案：
   - pending：「PDF 预览生成中，当前为简化渲染；稍候自动切换，或下载原件查看完整样式」；
   - failed：「PDF 预览生成失败（原因见管理端），已回退为简化渲染；可下载原件查看完整样式」。

### 8.3 `index.html`（约改 15 行）

1. dock 内 `#btn-copy` 后新增：
   ```html
   <button id="btn-download" class="btn btn-icon" title="下载原始文档">
     <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
       <path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/>
     </svg><span>下载</span>
   </button>
   ```
2. 版本号：`style.css?v=20`、`reader.js?v=4`、`app.js?v=14`。

### 8.4 `style.css`

新增：`.badge-pending` / `.badge-degraded`（角标配色 + 可选 `@keyframes` 旋转）、卡片下载按钮 `.card-dl`（与现有 icon-btn 对齐）、窄屏下 dock「下载」文字隐藏只留图标（现有响应式惯例）。

---

## 9. 安全清单对照

| 规范 | 落实 |
| --- | --- |
| SEC-1 目录穿越 | `<id>` 白名单正则不变；`/pdf`、`/download` 与 `/raw` 同款校验；三件套路径仅由 `id + 白名单扩展名` 拼出 |
| SEC-3 上传 | 白名单/20MB/服务端 ID 重命名不变；原件落盘同样走 `_file_path` 二次校验 |
| SEC-4 匿名 | 框架全局拦截；`/pdf`、`/download` 第一步 `get_session_user()`，401 |
| SEC-5 泄漏 | 转换异常统一转 `pdf_error` 用户可读文案，不回堆栈；`/pdf` 未就绪与不存在同为 404 |
| SEC-10 子进程 | soffice 调用参数列表化，禁止 `shell=True`；`src_path` 由服务端 ID 生成，不含用户输入 |
| B-4 依赖自检 | `/status` 报告 `pdf_engine`；无引擎优雅降级（上传不阻断、阅读走降级链） |
| B-8 日志 | 不自落日志；写操作 `set_operation("上传知识库文件"/"下载知识库文件"/"重试PDF转换")` |
| 9.2 铁律 | 归属字段 session 取值不变；下载/阅读为全站公共资源（原设计 §3.3 既定） |
| XSS | `original_name` 插值一律 `textContent`/转义；下载文件名由 Flask `download_name` 处理，不经手拼 header |

> 权限决策记录：下载端点放开给全体登录用户——这是对原设计「不提供下载」边界的**需求级变更**（用户明确提出），在此留痕。

---

## 10. 性能策略

1. **异步转换**：上传请求零转换耗时；转换串行（线程池 1 + 模块锁），服务器 CPU 峰值可控。
2. **转换缓存即落盘**：PDF 一次生成永久复用，`/pdf` 端点 `send_file(conditional=True)` 自带 ETag/Range/304。
3. **前端零新增库**：阅读复用 pdf.js；列表轮询 3s × 最多 60s，无 pending 即停。
4. **降级链免费**：无引擎时与现状体验完全一致，无额外开销。
5. 磁盘：word/excel 类约 2~3 倍占用；`MAX_FILES=2000` × 20MB 上限下可控（运维层面如有压力，遗留项提供「清理降级件」开关）。

---

## 11. 风险与预案

| 风险 | 预案 |
| --- | --- |
| 目标机未装 LibreOffice | 不阻断任何功能：上传/阅读/下载均可用（阅读走降级链）；`/status` 与上传响应明确告知；README 提供内网离线安装指引 |
| soffice 转换挂死 | 180s 超时 + 临时 profile 隔离 + 串行锁；超时记 failed 可重试 |
| soffice 并发冲突 | 线程池 1 + 模块级转换锁双重串行 |
| 大文件转换慢（10~20MB Word 可能 30s+） | 异步管线 + 前端「生成中」角标；用户可先以降级渲染阅读 |
| 旧数据无原件（doc/xls 历史记录原件已被转换件覆盖） | 迁移回填 `original_ext=ext`，下载给现有落盘件（docx/xlsx）——接受此为已知折衷，文档中注明 |
| Excel 空白页（残留格式撑大使用区域） | 遗留增强：openpyxl 预修剪副本（§7.4） |
| PyInstaller 打包 | 不新增 Python 依赖（subprocess/winreg/tempfile 均标准库），spec 零改动 |
| 内网老 Chrome（浏览器基线 Chrome ≥72） | 前端新增代码保持 ES5 风格（var/无 ?.），图标内联 SVG 不用 emoji |

---

## 12. TODO（断点续开发清单 · 按序执行）

> 约定：完成一项把 `[ ]` 改 `[x]` 并附日期；发现新事项追加到对应阶段末尾，不打乱顺序。
> 阶段 1→2→3 串行；阶段 4 穿插在各阶段内进行；低性能模型接手时从第一个 `[ ]` 继续即可。
> 测试统一套路（务必遵守，保护真实数据 `~/.jztoolshub`）：
> 先 `import jztools_data; jztools_data.get_data_root = lambda: <临时目录>`，再 `import app`，
> 复制 `config/tools.json` 进临时目录，`register_plugin_backends(app)`，用 `app.test_client()` 打接口；
> **不要调用 `init_data_root()`**。运行用隔离 venv
> `C:\Users\yfjz\.workbuddy\binaries\python\envs\default\Scripts\python.exe`。

### 阶段 0：设计 ✅（本文档）

- [x] 需求确认与边界划定（§1）
- [x] 转换引擎选型定稿（§3：LibreOffice headless 主选，探测+降级链）
- [x] 存储/数据模型/API/前后端改动面设计（§4~§8）
- [x] 本机环境探测：WPS 有、MS Office 无、LibreOffice 无（§3.1 脚注）

### 阶段 1：后端存储与下载 ✅ 2026-09-11（不依赖 LibreOffice，已独立交付）

- [x] **1.1 `routes.py` 元数据层**：`_file_brief()` 增加输出 `pdf_ready`（`rec.get("pdf_status")=="ok"`）、`pdf_status`、`original_ext`（回落 `rec.get("ext")`）、`original_size`（回落 `size`）。
  `converted` 语义同步修正为 `original_ext != ext`（原为「original_ext 有值」——原件字段普及后会误判所有记录都"已转换"）。
  验证：py_compile 通过。
- [x] **1.2 `routes.py` 迁移函数**：新增 `_migrate_files()`（§5 三条规则：回填 original_ext / pdf_status / original_size / pdf_size / pdf_error），在 `register()` 开头调用（try/except 兜底，失败不影响插件主体）；幂等。
  **实测坑**：判定"是否需要迁移"必须用**键是否存在**（`"pdf_size" not in rec`），
  不能判取值（`rec.get("pdf_size") is None`）——未生成 PDF 时 pdf_size 恒为 None，
  会导致每次启动都误判需迁移并重写 files.json。
- [x] **1.3 `routes.py` 上传改双份落盘**：按 §7.3 改 `kb_file_upload`——`upload_ext`（原件扩展名，恒记录）、
  `converted_from`（旧版转换来源，仅 doc/xls 有值，前端"已转换"角标仍用 `converted_from`）；
  doc/xls 先写原件 `<fid>.<original_ext>` 再写转换件；其余类型 `original_ext=ext` 单次落盘
  （按绝对路径去重，避免重复写）；元数据补 `original_ext/original_size/pdf_status=none` 等。
- [x] **1.4 `routes.py` 新增下载端点**：`GET /files/<id>/download`（视图名 `kb_file_download`）。
  验证：docx/xls/pdf/csv 下载字节与上传源一致；中文文件名响应头含 `filename*=UTF-8''...`。
- [x] **1.5 `routes.py` 删除改三件套**：按 `original_ext / ext / pdf` 三个扩展名生成路径并按
  绝对路径去重后逐个删除（缺失跳过）。验证：xls 记录删除后 `.xls` 与 `.xlsx` 均无残留。
- [x] **1.6 隔离测试**：54 项全过（`%TEMP%\kb_phase1_smoke.py`，临时数据根目录 + test_client）：
  双份落盘 / 下载字节一致 / 中文文件名 / 删除清理 / 迁移幂等 / 401 / 403 / 404 / 穿越防护 /
  坏文件 422 / 白名单外 415 / /raw 回归。
  **补充实施决策**：`_pdf_engine_available()` 已在阶段 1 预置（pdf_convert 模块缺失时恒 False），
  doc/xls 转换失败在**引擎可用时不阻断**、不可用时维持 422——阶段 2 接入引擎后自动生效。

### 阶段 2：PDF 转换引擎与异步管线 ✅ 2026-09-11

- [x] **2.1 新增 `backend/pdf_convert.py`**：按 §7.1 实现 `PdfConvertError / detect_soffice(refresh) / engine_status(refresh) / convert_to_pdf`。
  验证：py_compile 通过；本机（无 LibreOffice）`detect_soffice()` 返回 `available=False` 且不抛异常，
  `convert_to_pdf` 抛 `PdfConvertError("服务器未检测到 LibreOffice…")`。
  **实施补充**：探测候选路径加了 `LibreOffice 7` 两个目录与 `WOW6432Node` 注册表键；
  Windows 下用 `STARTUPINFO` 隐藏子进程黑框；配置文件同时支持
  `<数据根>/plugins/knowledge-base/config.json`（运维可改、不随升级被覆盖）与模块目录同级。
- [x] **2.2 `routes.py` 接线 `/status`**：返回增加 `pdf_engine` + `pdf_preview`；`?refresh=1` 调 `detect_soffice(refresh=True)`。
  **实施补充**：refresh 会派生子进程读版本号，因此**仅管理员生效**，普通用户拿到缓存结果（防探测风暴）。
  实测：`/status` 受全局鉴权，未登录 401（与 `/config` 不同，写用例时别按匿名接口断言）。
- [x] **2.3 `routes.py` 转换队列**：`_PDF_POOL`（max_workers=1）/ `_enqueue_pdf` / `_pdf_task` / `_pdf_finish`；
  上传把 word/excel 类的 `pdf_status` 置 `"pending"` 并入队，响应返回 `pdf_status`；
  `pdf_error` 截断 200 字符、不含堆栈与路径（SEC-5）。
  验证：无引擎环境上传 docx → `pending` → `failed`，`pdf_error` 含「未检测到 LibreOffice」。
- [x] **2.4 `routes.py` 新增 `/files/<id>/pdf`**（`kb_file_pdf`）：仅 `pdf_status=="ok"` 且文件存在才返回，
  `application/pdf` + `Content-Disposition: inline` + `conditional=True`；否则 404。
- [x] **2.5 `routes.py` 新增 `/files/<id>/pdf-retry`**（`kb_file_pdf_retry`）：仅 `failed` → `pending` 入队；
  非 failed 409、不存在 404、非管理员 403；`set_operation("重试知识库PDF转换")`。
- [x] **2.6 启动补偿**：新增 `_recover_pending_pdfs()`，`register()` 在迁移后调用；
  命中「word/excel 类 + `pdf_status ∈ {none, pending}` + PDF 文件不存在」，置 pending 入队。
  验证：手工把记录改 `none` 并删 PDF → 调 `_recover_pending_pdfs()` → 命中并转 ok。
- [x] **2.7 fake-soffice 测试夹具**：`soffice.bat` 转调 `fake_soffice.py`（解析 `--outdir` 与源文件，
  输出**最小但结构合法**的 PDF，pdf.js 可直接打开）；测试里打桩 `pdf_convert.detect_soffice` 指向 bat。
  验证：`%TEMP%\kb_phase2_smoke.py` **53/53 通过**——pending→ok、`/pdf` 200 + `%PDF-`、
  失败 failed 与重试（409→200→ok）、启动补偿、无引擎优雅降级、下载/raw 回归、权限 401/403/404。
- [x] **2.8 `backend/requirements.txt`**：加注释说明「PDF 预览依赖目标机安装 LibreOffice
  （可选外部程序，非 pip 包）」+ `pdf.soffice_path` 配置示例。
  **补充**：`_file_brief` 增加 `pdf_error` 输出（卡片"预览降级"角标悬停展示原因，服务端生成的用户可读文案）。

### 阶段 3：前端 ✅ 2026-09-11

- [x] **3.1 `reader.js`**：`fetchUrl(file, url)` 抽出（默认 `/raw`），`renderPdf(file, opts)` 支持 URL 覆盖，
  `render()` 里 `file.pdf_ready` 短路到 `renderPdf(file, {url: pdfUrl(file)})`；
  `NAV_FORMATS` 注释改为「按实际渲染器判定」（该常量当前无引用，仅作说明）。
  验证：`node --check reader.js` 通过（受管 node 22.22.2）。
- [x] **3.2 `app.js`**：`downloadFile(f)`（fetch + blob + `<a download>`，文件名按 RFC 5987 解析
  `filename*=` 回退 `filename=`）+ `DL_SVG` 内联图标 + `fileById()` +
  卡片下载按钮（全员可见，不影响管理按钮显隐）+ dock 下载按钮（按 `state.currentFileId` 取当前文件）+
  "PDF 生成中"/"预览降级"角标 + `startPdfPoll/stopPdfPoll`（3s × 最多 20 次，就绪自动重渲染切 PDF 视图）+
  提示条三类文案（PDF 生成中→info 蓝 / 生成失败→warn 琥珀 / 旧版格式转换→原样式）。
  验证：`node --check app.js` 通过。
  **实施补充**：`loadFiles(silent)` 加静默参数（轮询不弹错）；`readerCbs()` 抽出供打开与自动重渲染共用。
- [x] **3.3 `index.html`**：dock 下载按钮（内联 SVG，无 emoji）+ 版本号 `style.css?v=20 / reader.js?v=4 / app.js?v=14`。
- [x] **3.4 `style.css`**：`.card-dl`（卡片下载按钮）、`.pdf-badge.pending/.degraded`、
  `@keyframes pdf-pulse`、`.reader-notice.info`（提示条信息态）。
- [x] **3.5 无引擎环境走查**：见阶段 4.3（已用预置数据验证 pending/failed/ok 三种角标与提示条）。

### 阶段 4：真机验证（含 LibreOffice 环境）

- [x] **4.1 开发机安装 LibreOffice** ✅ 2026-09-11：采用 **非管理员「管理安装」** 解包到 `D:\LibreOffice\`
  （`msiexec /a <msi> /qn TARGETDIR=D:\LibreOffice`，避免 UAC），版本 25.8.7（366MB MSI / 解包 1.5GB），
  探测顺序：先尝试注册表（未命中）→ 走 **`<数据根>/plugins/knowledge-base/config.json` 的 `pdf.soffice_path`**。
- [x] **4.2 真实样例转换** ✅ 2026-09-11：用 openpyxl/python-docx 生成含中文/表格/图片/页眉页脚的
  docx（2 页 / 105KB PDF）与多 sheet xlsx（3 sheet → 3 页 / 230KB PDF）。PDF 由 LibreOffice headless
  生成，`%PDF-` 头校验通过；xlsx `SUM(E3:E7)` 公式被 LibreOffice 求值（120200）。
- [x] **4.4 重启补偿验证** ✅ 2026-09-11：上传后转换 ok → 把 `pdf_status` 重置回 pending 并删掉 PDF →
  重新加载 routes 模块（与「服务重启 register()」走同一段 `_recover_pending_pdfs` 代码）→
  自动入队并转回 ok（约 18s）。
- [x] **4.3 浏览器走查**（agent-browser，先 `set viewport 1440 1000`，`no_proxy=127.0.0.1,localhost`）✅ 2026-09-11：
  **用「临时数据根副本 + 预置记录」的方式验证**（复制 `~/.jztoolshub` 到临时目录并注入两条样例，
  真实库零改动、零上传，测完即删）。结果：
  - `style.css?v=20 / reader.js?v=4 / app.js?v=14` 均被加载（版本号生效）；
  - `f-pdftest1`（docx + pdf_status=ok）→ 卡片无任何异常角标 → 点击后
    `GET /files/f-pdftest1/pdf 200` + pdf.js/pdf.worker 加载 + `#reader-container canvas` = 1、
    页码 `1 / 1`、提示条 hidden → **PDF 预览链路成立**；
  - `f-pdftest2`（xls→xlsx + pdf_status=failed）→ 卡片显示「已转换 + 预览降级」，
    打开后提示条「PDF 预览生成失败，已回退为简化渲染；可下载原件查看完整样式。」+
    SheetJS 渲染出表格 → **降级链路成立**；
  - 8 张卡片每张都有「下载原始文档」按钮；点击 `f-pdftest2` 的下载按钮 →
    `GET /files/f-pdftest2/download 200`（headless 环境报 "Download was canceled" 属自动化环境限制，
    字节与响应头已在阶段 1/2 的 test_client 用例中断言）。
  **踩坑**：插件前端跑在 `/tool/<id>` 的 iframe 里，`click`/`eval` 默认作用于父文档 ——
  登录后直接开 `/plugin/knowledge-base/index.html` 才行（见 HANDOFF §7-21）。
  待补：未登录访问被拦、无权限账号只见下载不见管理按钮（本次未造普通账号，代码层已按 `can_manage` 控制）。
- [x] **4.4 重启补偿验证**：隔离用例覆盖（把记录改 `none` + 删 PDF → `_recover_pending_pdfs()` → 转 ok）。

### 阶段 5：文档同步与收尾（改功能后必做，顺序执行）

- [x] **5.1 `plugins/knowledge-base/README.md`**：新增「PDF 预览（Word / Excel 原样式）」章节
  （异步管线 / 降级行为 / LibreOffice 安装与 `pdf.soffice_path` 配置）、三件套存储表、
  新增端点列举、已知限制补充（Excel 空白页、历史 doc/xls 原件不可恢复）。
- [x] **5.2 根 `README.md`**：内置插件一览（knowledge-base 行）、目录树注释、
  HTTP API 表新增 `/pdf`、`/download`、`/pdf-retry`，`/status` 补 `pdf_engine`，
  上传行补双份保存与 `pdf_status=pending`，脚注改「无编辑接口，阅读与下载原件对全员开放」。
- [x] **5.3 `docs/知识库插件-设计文档.md`**：增补「阶段 7：PDF 预览 + 原件下载」记录本轮改动
  （事实源仍为本文件，此处只记落地结果）。
- [x] **5.4 `HANDOFF.md`**：状态头「最后更新」+ §2 git 状态 + §3 插件一览 + §7 踩坑
  （新增 20~23：.bat 转调 Python 做转换夹具 / 插件前端在 iframe 内 / agent-browser 会话与 batch 语法 /
  `download_name` 与前端文件名解析）+ §7.5 速查表（knowledge-base 行长任务列改为线程池 1）。
- [x] **5.5 当日工作日志** `.workbuddy/memory/2026-09-11.md`。
- [x] **5.6 规范自检**（插件设计规范 §14 前四组逐项）：见 §14 自检记录。
  - [ ] git 提交（**等用户确认**，与历史条目一致：本轮改动全部未提交）。

### 阶段 6：遗留 / 增强（本期不做，可后补）

- [ ] reportlab 纯 Python 降级引擎（§7.5，目标机无法装 LibreOffice 时立项；spec PACKAGES + reportlab）
- [ ] Excel 预修剪（§7.4：openpyxl trimmed 副本，去空白 sheet/空白页）
- [ ] 存量 doc/xls 历史记录原件不可恢复的补偿策略评估（§11 已知折衷）
- [ ] 管理端「批量重转全部 PDF」按钮（retry 的批量版）
- [ ] 磁盘压力时的「降级渲染件清理」开关（docx/xlsx 转换件删除，仅留原件+PDF）

### 阶段 7：Excel 表格 HTML 预览（替代 Excel 的 PDF 预览）✅ 2026-09-13

> 背景与需求变更：xlsx 走 LibreOffice→PDF 预览时**按打印分页**，宽表/长表被切成多页
> 不利阅读（阶段 4 真机实测：3 sheet xlsx → 3 页 PDF 即分页证据）。本轮把 Excel 类预览
> 从「转 PDF」改为「**Python 手绘 HTML 表格**」（参考共享文档插件的表格呈现思路 +
> GitHub Apkawa/xlsx2html 的 openpyxl→HTML 样式映射，按本项目约束重写，**零新增 pip 依赖**）；
> Word 类 PDF 预览不受影响。需求来源：用户反馈 2026-09-13。

- [x] **7.1 新模块 `backend/xlsx_render.py`**：openpyxl 读簿（`data_only=True` 取公式缓存值）
  → 逐 sheet 手绘 `<table>`（`table-layout:fixed` + `<colgroup>` 锁列宽）→ JSON
  （`{sheets:[{name,rows_total,cols_total,truncated,html}]}`）原子落盘。
  还原：合并单元格（colspan/rowspan + 外边框按边缘单元格取值——即「openpyxl 丢合并边框」的正确读法）、
  列宽（`px≈width*7+5`，修正 xlsx2html 的 /10*96 偏窄）/行高（pt→px）/隐藏行列、
  13 种边框样式、填充色（solid 取 fgColor，图案取底色近似；**theme+tint 主题色**解析
  `wb.loaded_theme`——xlsx2html 直接丢弃 theme 色）、字体（名称/字号 pt→px——修正其
  11pt 当 11px/加粗/斜体/下划线/删除线/颜色）、对齐（水平/垂直默认 bottom/换行/缩进）、
  数字与日期显示格式（千分位/百分比/货币/[Red] 负数/中文日期/时分秒消歧/AM-PM/[h] 经时/
  科学计数，ROUND_HALF_UP 对齐 Excel；不引入 babel）。
  不还原：批注/浮动图片/图表/条件格式/冻结窗格/富文本局部样式。
- [x] **7.2 上限熔断与降级链**：单 sheet 3000 行 / 120 列 / 12 万格 / 4MB 输出封顶，
  超限截断并在表尾提示「下载原件查看全部」；源文件 >15MB 或解析失败 → `html_status=failed`
  → 前端回退 SheetJS（上传/阅读/下载不受影响，B-4）。
- [x] **7.3 `routes.py` 管线**（与 PDF 管线同构）：`PDF_SOURCE_EXTS` 收窄为 `{doc,docx}`，
  新增 `XLSX_SOURCE_EXTS={xls,xlsx}`；`html_status/html_size/html_error` 三字段；
  `_HTML_POOL`（独立 1 线程池，不与 LibreOffice 长任务互相阻塞）、`_html_task/_html_finish/
  _enqueue_html/_recover_pending_htmls`（重启补渲，`failed` 不自动重试）；
  新端点 `GET /files/<id>/preview`（JSON，未就绪/失败/不存在一律 404，SEC-5）、
  `POST /files/<id>/preview-retry`（管理员）；上传元数据与响应补 `html_status`；
  删除清理扩为四件套（+`<id>.json`）；`/status` 报 `xlsx_render/xlsx_preview`。
- [x] **7.4 数据迁移**：`_migrate_files` 幂等补齐 html 三字段；历史 Excel 记录的
  `pdf_status∈{pending,failed}` 复位 `none`（PDF 预览管线对 Excel 退役；已生成的 PDF 文件
  留存不删，仅不再被阅读端消费）。
- [x] **7.5 前端**：`reader.js` 新增 `renderSheetHtml`（/preview → DOMPurify → 页签 +
  注入 + TSV 复制，拉取失败自动回退 SheetJS）；分派改为「Excel×html_ready → 表格预览、
  Word×pdf_ready → PDF.js、其余按扩展名降级」；`app.js` 状态辅助函数
  （`isSheetFile/previewStatus/previewPending/previewReady`）统一轮询/角标/提示条/
  上传 toast；`style.css` 增 `.kb-xlsx-table`（默认网格线 + 兜底字体，样式主体由
  单元格 inline style 承载）；版本号 `style.css?v=21 / reader.js?v=5 / app.js?v=15`（F-2）。
- [x] **7.6 验证**：格式化矩阵 40+ 用例（数字/日期/经时/货币/[Red]/科学计数/舍入口径）全过；
  Flask test_client 集成（假 admin 会话）：上传→异步渲染 ok→/preview 200 四 sheet→
  404 语义→删除四件套无残留；浏览器目检（IAB 1280×900）：合并标题蓝底白字、表头
  双线底边、红字负数、¥ 千分位、中文日期、百分比、隐藏行/列、theme+tint 填充、
  `9:05 AM`/`[h]:mm`、关网格线 sheet、wrap_text 折行、超宽表截断提示条——全部符合预期。
  自测脚本留存：`backend/test_xlsx_render.py`、`backend/test_routes_preview.py`（产物
  `backend/out/` 已入 .gitignore）。
- [x] **7.7 文档同步**：本文件（阶段 7 + §13 决策）、`docs/知识库插件-设计文档.md`（阶段 8）、
  插件 README、根 README（插件一览/API 表/四件套注）、requirements.txt 注释、HANDOFF、当日日志。
- [x] git 提交 ✅ 2026-09-13（`77e0f22`，阶段 1~7 一次补交）。

### 阶段 8：Office 预览引擎替换为 xhr/dhr 双引擎（阶段 7 的手绘方案与阶段 2 的 PDF 方案一并退役）✅ 2026-09-13

> 需求：用户指定采用 `D:\TestWorkSpace\xlsx-html-preview` 项目的转换引擎
> （xhr = XLSX/XLS→HTML 重绘引擎、dhr = DOC/DOCX→HTML 渲染引擎，纯 Python 标准库、
> 自带解析器与 IR 层、369 个测试、错误码/安全白名单完备），Word 与 Excel 两类
> 预览**舍弃原方案**（Word→LibreOffice 转 PDF + pdf.js 分页渲染；Excel→xlsx_render
> openpyxl 手绘 HTML）。仅知识库插件预览链路变更，下载/分类/权限不动。

- [x] **8.1 引擎 vendor**：`backend/vendor/{xhr,dhr}` 原样拷贝（53 个 .py 约 581KB，
  零改动；升级整目录替换），`vendor/README.md` 记录来源/版本/许可证/接入方式。
  dhr 顶层 `from xhr.core import ...` → 两包必须同为顶层可导入，适配层用
  `sys.path` 注入（上游 demo 同款做法）。
- [x] **8.2 适配层 `backend/office_render.py`**：引擎导入与 `XHR_SOFFICE` 环境注入
  （配置 `office.soffice_path` 新键、兼容旧 `pdf.soffice_path`）；模块级渲染锁；
  `render_word`（dhr：flow 流式 + fragment + 图片 base64）/ `render_sheet`
  （xhr：fragment + 页签 + max_cells=12 万截断）；`XhrError` →
  `OfficeRenderError`（用户可读文案）；`availability()/soffice_available()`。
- [x] **8.3 routes.py 重写**：删除 `pdf_convert.py`、`xlsx_render.py` 与
  pdf_status/html_status 全套状态机（线程池/启动补偿/轮询字段/`/pdf`、`/pdf-retry`、
  `/preview-retry` 端点、`_file_brief` 状态字段、上传 pending 入队）；
  `GET /files/<id>/preview` 改为**按需同步渲染 + 原子缓存** `<id>.preview.json`
  （文件按 id 不可变无失效问题；失败/引擎缺失/非 Office 类一律 404+用户可读
  error，前端回退）；删除清理改为「原件+渲染件+预览缓存+历史 PDF 残留」；
  上传旧格式转换失败的放行口径改为 `_office_fallback_possible`（.xls 恒放行——
  xhr xlrd 兜底；.doc 看 soffice——dhr 归一化）；`/status` 报
  `office_render/office_preview`；`_migrate_files` 精简回 original_ext/original_size。
- [x] **8.4 前端**：reader.js 新增 `renderOffice`（/preview → 摘取引擎 `<style>` →
  正文过 DOMPurify → CSS 原样挂回——**实测 DOMPurify 会整块丢弃 `<style>` 元素**，
  导致 xhr class 型 CSS 全灭；CSS 为引擎生成的静态内容无注入面，摘取挂回安全）、
  Excel 页签接线（`.kbsheet-tab` → aria-selected + `.kbsheet-sheet[hidden]`，与
  引擎 runtime 行为一致）、TSV 复制；分派改为 docx/doc/xlsx/xls → renderOffice，
  失败自动回退 mammoth/SheetJS；原生 PDF 仍走 pdf.js。app.js 删除轮询/角标/
  提示条 pending 分支；style.css 删手绘表格样式、`.kb-office` 容器约束；
  版本号 v22/v6/v16（F-2）。
- [x] **8.5 验证**：模块级四格式渲染（docx/xlsx 直读 + soffice 转 .xls/.doc 走
  归一化/兜底通道）通过；Flask test_client 全链路（上传→首渲染 19~70ms→缓存命中
  cached 标记→非 Office 404→删除无残留）通过；浏览器目检（IAB，含插件真实
  DOMPurify 与 style.css）：Word 标题/加粗/表格边框、Excel 蓝色主题/红字负数/
  货币/日期格式/页签切换全部正常（修复 DOMPurify 丢 style 后复验）。
- [x] **8.6 文档同步**：本节、知识库设计文档（阶段 9）、插件 README、根 README、
  requirements.txt 注释、HANDOFF、当日日志。
- [x] **8.7 阅读卡片贴合内容宽度（2026-09-14 追加，纯前端 CSS）**：`.reader-frame`
  由「通栏白卡片」改为 `width: fit-content` 收缩居中——卡片宽度由预览件实际宽度
  决定（Word dhr 版心 `.kbdoc-body` 的 max-width、xhr 表格显式 px 宽、PDF 画布、
  `.paper` 860px 版心），上限仍为视口宽；不支持 fit-content 的老内核回退
  `width:auto`（通栏，同旧行为）。配套：≥600px 视口给 520px 最小宽兜底
  （窄表/加载态不出细条卡片）；`.reader-notice` 限宽 640px，长文案换行不参与
  撑宽卡片。style.css v24。浏览器（IAB）实测八场景全过：Word 正常 698px /
  宽表格卡内滚动、Excel 宽表撑满+viewport 内滚 / 窄表 520px / SheetJS 降级、
  PDF 776px 贴合画布、Markdown 随内容伸缩、加载态 520px、转换提示条不撑宽。
- [x] **8.7.1 修「会话开发文档」横向滚动条（2026-09-14，用户实测反馈）**：该
  docx 的参数说明表「说明」列含 `chat.completion/chat.completion.chunk` 类
  **无空格长 token**，把 auto 表格列 min-content 撑到 345px，整表 601px 超版心
  （618px body 的内容盒 554px）47px → 触发 8.7 的 `.kb-office-word` 横向滚动。
  根修：`.kb-office-word` 加 `overflow-wrap:anywhere`（断词且**参与 min-content
  计算**，`break-word` 不参与）→ 表格列收缩回版心（实测两表均 554px、全层级
  scrollWidth==clientWidth、零元素溢出、卡片仍 650px 贴合）；正文 NBSP 缩进
  JSON 长行同获正常折行。同类隐患 `.paper` 一并加 `anywhere`。
  `.kb-office-word{overflow-x:auto}` 保留为真正超宽表格（引擎 width_px 显式
  宽度，如 1200px）的兜底滚动——回归验证 guard 仍生效、页面级永不横滚。
  style.css v24。预览缓存为渲染产物不受 CSS 影响，无需失效。
- [x] **8.8 dhr 引擎缩进/编号修复（2026-09-14，用户实测反馈）**：两个问题——
  ① 部分文档预览顶部提示「编号 numId=0.0 未在 numbering.xml 中定义，该列表降级为
  普通段落」：`<w:numId w:val="0"/>` 是 OOXML 的**取消编号**合法值（Word/WPS 用它
  清除段落从样式继承的编号），引擎将其误判为「未定义编号」而告警污染提示条；
  ② 部分段落开头缩进不显示：`w:ind` 的 `leftChars`/`rightChars`（中文文档「缩进
  N 字符」写法）解析后**无消费方**（margin 丢失）；同一缺陷族还包括 `w:start`/
  `w:end`（Strict 别名）被忽略、首行/悬挂缩进的 px/em 两形态在「样式 ← 段落直接
  格式」合并时各自独立继承（样式的字符缩进压过段落直接写的 twips 缩进，实测
  「样式 leftChars=200 + 段落 left=420」错误输出 2em 而非 28px）。
  补丁（vendor/dhr 三文件，清单见 `vendor/README.md` 第 3/4 条）：numId=0 静默按
  普通段落（真未定义的 numId 仍告警且消息整数化）；`para_props_to_css` 消费
  `_left_chars_em`/`_right_chars_em` 输出 `margin:{n}em`；`parse_ppr` 记录形态标记
  （`_fl_form`/`_hg_form`/私有 em 键，`merge_para` 无条件透传）实现「最近来源整组
  覆盖」；`left/right` 缺席时兜底 `start/end`、`startChars/endChars`。
  适配层：`office_render._meta_warnings` 修正 `meta` 为 dict 时 `getattr` 取不到
  warnings 的缺陷（此前 `/preview` 的 warnings 字段恒为空）；`routes.py` 新增
  `PREVIEW_CACHE_VERSION`（本次递增为 2）——引擎行为变化时旧缓存自动重渲染，
  避免升级后「修复不生效」。
  验证：模块级探针（numId=0/未定义、四种字符缩进、样式覆盖矩阵 5 例）全过；
  **已入库 4 份 docx 修复前后 HTML 字节一致**（补丁零副作用）；浏览器实测计算样式
  （2em=32px 缩进可见、numId=0 无标记无警告、提示条仅剩真实警告）；
  新增自测 `backend/test_dhr_indent_numid.py`（14 项断言）；`test_routes_preview.py`
  全链路回归通过。
- [ ] git 提交（**等用户确认**）。

### 阶段 8 遗留 / 增强（本期不做，可后补）

- [ ] `.doc` LibreOffice 归一化对微型表格的退化（本会话实测：合成 1 行表格经
  doc→docx 回环退化为制表符段落，内容保留）——真实链路 .doc 走 doc_convert
  转换件不经过此通道，仅历史失败记录会踩到；如需彻底解决需上游排查。
- [ ] Word 渲染模式（flow/paged）、修订显示模式暴露为 URL 参数（引擎已支持）。
- [ ] 引擎上游升级通道（vendor 目录整替换 + 上游 pytest 回归）。

---

## 13. 关键决策速查（为什么这么做）

- **选 LibreOffice 而非 WPS/COM**：服务可靠性（原设计已否决 COM 路线），且 doc/docx/xls/xlsx 四格式一个引擎全覆盖，保真度接近原生打印。
- **原件必须落盘**：需求「下载给原文档」的硬前提；顺带让 doc_convert 的 doc→docx 转换从「唯一保存件」降级为「降级渲染件」，失败不再致命。
- **PDF 用原件转换而非降级件**：soffice 原生支持 doc/xls，从原件转保真最高；降级件只服务 mammoth/SheetJS 兜底。
- **异步而非同步转换**：规范 8.5 长耗时必须异步；上传响应即时，体验与现状一致。
- **阅读零新渲染器**：复用 pdf.js 全链路（翻页/缩放/复制/Range），前端改动面最小，低性能模型可安全实施。
- **无通用任务端点**：复用 `pdf_status` 字段 + 现有列表接口轮询，少一套任务表/归属校验概念（与规范 8.5 不冲突——状态即任务状态，轮询即状态轮询）。
- **failed 不自动重试**：防止坏文件每次启动触发转换风暴；人工 retry 端点兜底。
- **Excel 预览弃 PDF 改手绘 HTML（阶段 7）**：PDF 按打印分页，宽表/长表被切碎不利阅读
  （用户核心痛点）；xlsx2html 思路证明 openpyxl 可以低依赖还原样式，自实现而非引包
  （内网离线装不了新 pip 依赖 + 要修它的主题色/列宽/字号失真）。连续单页 + 页签 =
  阅读动线与 SheetJS 降级一致，前端零新渲染库。
- **Excel 渲染与 Word 转换分池**：openpyxl 渲染秒级、LibreOffice 转换可达分钟级，
  独立线程池避免 Excel 预览被 Word 队列阻塞（反之亦然）。
- **预览引擎统一换 xhr/dhr（阶段 8）**：用户指定。相对自研手绘（Excel）与
  LibreOffice-PDF（Word）：测试覆盖厚（369 用例）、格式坑沉淀全（主题色索引交换/
  dxf 缺省 solid 等）、IR 层可缓存、错误码/安全白名单完备；且**亚秒级渲染使
  整个异步状态机（pending/轮询/重试端点）失去存在必要**，方案显著简化。
- **CSS 摘取挂回而非整段消毒（阶段 8）**：实测 DOMPurify 整块丢弃 `<style>` 元素，
  xhr 的 class 型 CSS 全在里面；CSS 为引擎生成的静态内容（字体白名单/无 URL），
  摘出后原样挂回，仅正文过 DOMPurify——安全面不扩大。

---

## 14. 规范自检记录（2026-09-11，对照《插件设计规范.md》§14 前四组）

| 规范 | 结论 | 依据 |
| --- | --- | --- |
| B-1 单一职责 | ✅ | 只动 knowledge-base 插件；`pdf_convert.py` 只管转换、不碰 Flask 上下文 |
| B-2 接口前缀 | ✅ | 全部 `/api/knowledge-base`；新端点视图名带插件前缀 `kb_file_*` |
| B-3 不反向依赖 | ✅ | `pdf_convert.py` 只依赖标准库 + `jztools_data`（读数据根），不 import 主应用 |
| B-4 依赖自检与降级 | ✅ | `/status` 报 `pdf_engine` + `pdf_preview`；无引擎时上传/阅读/下载全可用 |
| B-5 配置外置 | ✅ | `pdf.soffice_path` 支持数据根 `config.json`（运维可改） |
| B-8 日志 | ✅ | 写操作 `set_operation("下载知识库文件"/"重试知识库PDF转换")`，不自落日志文件 |
| SEC-1 目录穿越 | ✅ | `<id>` 白名单正则（三端点一致）+ 扩展名白名单二次校验；用例覆盖 `..%2f` 型 |
| SEC-3 上传 | ✅ | 白名单/20MB/服务端 ID 重命名不变；原件走同一 `_file_path` 校验 |
| SEC-4 匿名 | ✅ | 三端点首行 `get_session_user()`，未登录 401（用例覆盖） |
| SEC-5 信息泄漏 | ✅ | `pdf_error` 只存用户可读文案（截断 200 字符、无堆栈无路径）；`/pdf` 未就绪与不存在同为 404 |
| SEC-10 子进程 | ✅ | `subprocess.run(参数列表)`，无 `shell=True`；源路径由服务端 ID 生成 |
| F-2 资源版本 | ✅ | `style.css?v=20 / reader.js?v=4 / app.js?v=14` |
| F-3 XSS | ✅ | 新插值一律 `esc()`；图标内联 SVG 不用 emoji（老 Chrome 字体坑） |
| 8.5 异步任务 | ✅ | 线程池 + 状态字段轮询；启动补偿；不新增任务端点概念 |
| 9.2 归属铁律 | ✅ | 归属字段仍只从会话取，新端点不接收任何归属入参 |

---

## 15. 实施落地与实测踩坑（2026-09-11 真机验证）

### 15.1 真机验证结果（含 LibreOffice 25.8.7）

| 维度 | 实测结果 |
| --- | --- |
| 引擎安装 | 非管理员 `msiexec /a <msi> /qn TARGETDIR=D://LibreOffice` 解包 25.8.7（1.5GB），注册表无 LibreOffice；通过 `<数据根>/plugins/knowledge-base/config.json` 的 `pdf.soffice_path` 显式指定 |
| docx（2 页 / 中文/表格/图片/页眉页脚） | pending → ok 用时 45.1s（首次建 profile）/ 18s（profile 复用）；PDF 105KB，2 页 |
| xlsx（3 sheet / 表头底色/数字格式/求和公式） | pending → ok 用时 18.1s；PDF 230KB，**3 页（按 sheet 分页）**，`SUM(E3:E7)=120200` 被 LibreOffice 求值 |
| 浏览器目视（agent-browser，1440×1000） | PDF 在 pdf.js 中渲染，**中文正常无方块**、红字/表格边框/列表/图片/页眉保留；xlsx 表头底色、合计行加粗、`15,800.00` 千分位格式全保留 |
| 重启补偿 | 转换完成后把 `pdf_status` 重置 pending + 删 PDF → 重新加载 routes → `_recover_pending_pdfs()` 自动入队转 ok |
| 全链路测试（test_client，隔离数据根） | 16/16 通过：登录、/status 引擎、上传/转换/下载字节一致、retry 409、未登录 401 |

### 15.2 本轮相对原方案的关键变更（建议同步到原设计）

1. **profile 从「每次新建临时」改为「固定复用」**——实测 37~40s → 15~18s，profile 损坏时自动重置重试一次；profile 位置 `<数据根>/.lo-profile`（运维可见、可清理）。
2. **`soffice --version` 短超时**——解包版 LibreOffice（管理安装）的 `--version` 在 URE 未注册场景会启动后不退出（实测 60s+ 挂起）；版本探测超时压到 10s、失败返 `None`（不影响可用性，仅展示丢失）。
3. **前端轮询上限 60s → 180s**——LibreOffice 冷启动实测 40~90s，原 20×3s=60s 兜底会过早停止轮询，让角标卡在「生成中」；与后端 180s 超时对齐。
4. **修复 routes.py import 兜底缺失**——`pdf_convert` 原本只有 `try: from . import pdf_convert except Exception: _pdf_convert=None`，**无裸 `import pdf_convert` fallback**，导致脚本方式加载（importlib/补偿测试）的实例永远把引擎当作「不存在」置 failed；与 `doc_convert` 一致补上两步式兜底。

### 15.3 已知坑（已留痕在 HANDOFF §7）

- 解包 LibreOffice 启动会打 stderr `Could not find platform independent libraries <prefix>`，可忽略；设置 `URE_BOOTSTRAP`/`SAL_*` 环境变量**不改善**耗时（实测 17.5s vs 17.8s 基线）。
- 隔离测试必须复制引擎 config 到临时数据根；`/files?category=root` 只返回已归类文件，未分类文件用 `category=all`。
- 浏览器自动化：`agent-browser screenshot` 路径用正斜杠；批量点击用 `find text` 绕开 `eval` 的引号歧义；登录 + 全流程要在**同一次 `batch`** 内。
