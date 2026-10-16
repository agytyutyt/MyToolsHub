# 知识库插件 Office 预览与文档下载 · 设计文档

> **原名 `插件库优化方案-设计文档.md`（2026-09-17 改名，原名与内容不符）。**
> ⚠️ **引擎路线已变更**：本文 §3 选型的「LibreOffice → PDF 预览」已于 **2026-09-13 被弃用**，
> 现行为服务端 **vendor 双引擎（`dhr`=Word / `xhr`=Excel）按需同步渲染为 HTML**（亚秒级、无后台状态机、
> 磁盘缓存 `<id>.preview.json`）；上传时不再生成 PDF（历史 `.pdf` 产物已废弃、阅读端不消费）。
> **仍现行的部分**：原件下载路径与权限决策，以及 §9/§10 中的 **SEC-10 子进程、B-8 日志、9.2 铁律、XSS** 等条目。
> **§9/§10 中凡引用 `/pdf`、`/pdf-retry`、`pdf_error` / `pdf_engine` 与「列表轮询」的表述均已废止**
> （`backend/routes.py:20-21`：旧版 `pdf_status` / `html_status` 异步状态机与 `/pdf`、`/pdf-retry`、`/preview-retry` 端点全部移除，
> 引擎亚秒级、无需轮询；现行为 `GET /files/<id>/preview` 按需同步渲染，见《知识库插件-设计文档》§6 与插件 README）。
> **现行权威落点**：`plugins/knowledge-base/README.md`（接口与行为）、`docs/design/知识库插件-设计文档.md`（设计契约）。

| 文档属性 | 内容 |
| --- | --- |
| 文档版本 | v1.2（2026-09-11）+ 2026-09-17 状态标注 + 2026-09-18 阶段 9 / 阶段 10 / 阶段 11 |
| 适用插件 | `knowledge-base`（知识库） |
| 文档状态 | **全部阶段（0~11）已完成**：含真实 LibreOffice（25.8.7）环境下的端到端验证（阶段清单见 §12 TODO 与 §16~§18） |
| 实施记录 | §13（本轮实施落地与踩坑） |
| 关联文档 | 《docs/design/知识库插件-设计文档.md》（本方案的基线，下称「原设计」）、《插件设计规范.md》（B-1~B-8、SEC-1~6、F-1~F-7、8.5 异步任务、9.2 归属铁律）、《HANDOFF.md》§7 踩坑 / §7.5 速查表 |

| 四段覆盖 | 落点 |
| --- | --- |
| 接口契约 | `plugins/knowledge-base/README.md`（**现行接口唯一真源**）；§5 数据模型变更（其中 PDF 相关字段已作废）、§6 API 设计（其中的 `/pdf`、`/pdf-retry` **已删除**） |
| 实现路径 | §7 后端实现细化、§8 前端实现细化；适配层 `backend/office_render.py` + `backend/doc_convert.py` |
| 当前实现状态 | §13 关键决策速查、§15 实施落地与实测踩坑；**引擎已于 2026-09-13 换成 xhr/dhr 双引擎**（见文首横幅） |
| 后续优化方向 | §11 风险与预案；预览体验相关后续方向见 `HANDOFF.md` §4 |

> **覆盖范围**：知识库「预览与文档下载」的设计演进：数据模型、API、前端、安全、性能，以及引擎替换的决策记录
> ｜**不覆盖**：**该插件的接口与已知限制 → `plugins/knowledge-base/README.md`**；插件整体设计 → `docs/design/知识库插件-设计文档.md`
> ｜**随包**：是 ｜**最后核对**：2026-09-25

---

## 0. 一页速览（接手者先读这里）

**要做什么**：知识库上传 Word（doc/docx）与 Excel（xls/xlsx）后，服务端把文档**转成 PDF 用于在线阅读**（保留原文档样式）；**原件与 PDF 双份保存**；**全部类型文件提供下载**，其中 Word/Excel 的下载给的是**原始文档**（不是 PDF）。

> ⚠️ 上述「转成 PDF 用于在线阅读 / 原件与 PDF 双份保存」为**已弃用原方案**描述；现行实现为服务端 xhr/dhr 双引擎**按需渲染 HTML**（Word/Excel 走 `GET /preview` 返回 HTML），不再生成 PDF、不再双份保存，详见 `plugins/knowledge-base/README.md` 与 `docs/design/知识库插件-设计文档.md`。

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

> ⚠️ 此条为**已弃用原方案**（Word→PDF 预览）描述；现行 Word 预览改由 dhr 引擎**按需渲染 HTML**（`GET /preview`），不再生成 PDF，Excel 同理改由 xhr 引擎（详见 `plugins/knowledge-base/README.md`「Office 预览」）。
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

> ⚠️ 本节描述的 PDF 管线已于 2026-09-13 被 xhr/dhr HTML 渲染取代，**已删除** `/pdf` 与 `/pdf-retry` 端点与状态机（见 `plugins/knowledge-base/README.md`）。

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

> ⚠️ 本节描述的 PDF 管线已于 2026-09-13 被 xhr/dhr HTML 渲染取代，**已删除** `/pdf` 与 `/pdf-retry` 端点与状态机（见 `plugins/knowledge-base/README.md`）。

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
- 已知边缘：单元格带残留格式（无边框但有样式）会扩大使用区域产生空白页。曾拟用 openpyxl 预扫描生成「trimmed 临时副本」缓解（本期不做，随 PDF 管线退役）；注意 openpyxl 重写会丢图表/条件格式，故默认不启用。

### 7.5（二期可选）纯 Python reportlab 降级引擎

仅在「目标机无法安装 LibreOffice」成为现实约束时启动，单独立项：
`backend/pdf_render/`（docx 引擎：python-docx 段落/表格 → reportlab platypus；xlsx 引擎：openpyxl → reportlab Table；CJK 用 `UnicodeCIDFont('STSong-Light')` 或探测系统 simsun/msyh TTF 嵌入；spec PACKAGES + reportlab）。本期不实施（随 PDF 管线退役）。

---

## 8. 前端实现细化

> ⚠️ 本节描述的 PDF 管线已于 2026-09-13 被 xhr/dhr HTML 渲染取代，**已删除** `/pdf` 与 `/pdf-retry` 端点与状态机（见 `plugins/knowledge-base/README.md`）。

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
| SEC-1 目录穿越 | `<id>` 白名单正则不变；`/download` 与 `/raw` 同款校验；落盘路径仅由 `id + 白名单扩展名` 拼出（原文并列的 `/pdf` **已废止**，随端点删除） |
| SEC-3 上传 | 白名单/20MB/服务端 ID 重命名不变；原件落盘同样走 `_file_path` 二次校验 |
| SEC-4 匿名 | 框架全局拦截；`/download` 第一步 `get_session_user()`，401（`/pdf` **已废止**） |
| SEC-5 泄漏 | 转换/渲染异常统一转用户可读文案，不回堆栈；`/preview` 渲染失败与不存在同为 404（原 `pdf_error` 字段与 `/pdf` 未就绪判定**已废止**） |
| SEC-10 子进程 | soffice 调用参数列表化，禁止 `shell=True`；`src_path` 由服务端 ID 生成，不含用户输入 |
| B-4 依赖自检 | `/status` 报告依赖与 vendor 引擎可用性（原 `pdf_engine` 字段**已废止**）；无引擎优雅降级（上传不阻断、阅读走降级链） |
| B-8 日志 | 不自落日志；写操作 `set_operation("上传知识库文件"/"下载知识库文件"/"删除知识库文件")`（原「重试PDF转换」随 `/pdf-retry` **已废止**） |
| 9.2 铁律 | 归属字段 session 取值不变；下载/阅读为全站公共资源（原设计 §3.3 既定） |
| XSS | `original_name` 插值一律 `textContent`/转义；下载文件名由 Flask `download_name` 处理，不经手拼 header |

> 权限决策记录：下载端点放开给全体登录用户——这是对原设计「不提供下载」边界的**需求级变更**（用户明确提出），在此留痕。

---

## 10. 性能策略

1. **异步转换**：上传请求零转换耗时；转换串行（线程池 1 + 模块锁），服务器 CPU 峰值可控。
2. **预览缓存**：现行预览为 `/preview` **按需同步渲染** + 磁盘缓存 `<id>.preview.json`（首次请求渲染、后续命中缓存）；原 `/pdf` 的 ETag/Range 复用方案随端点移除。
3. **（已废止）** 原「列表轮询 3s × 最多 60s」异步状态机已随 `/pdf` 移除（引擎亚秒级、无需轮询）；原生 PDF 阅读仍走 pdf.js。
4. **降级链免费**：无引擎时与现状体验完全一致，无额外开销。
5. 磁盘：word/excel 类约 2~3 倍占用；`MAX_FILES=2000` × 20MB 上限下可控。

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
| ~~内网老 Chrome~~（浏览器基线 2026-10-16 取消） | 内联 SVG 不用 emoji **仍保留**（触发=缺彩色 emoji 字体，与浏览器无关）；ES5 风格要求已随基线取消解除 |

---

## 12. 交付摘要与遗留

> 各阶段（0~11）已全部交付；逐项断点勾选清单不再保留。实施记录与踩坑见 §15–§18，接口与行为真源见 `plugins/knowledge-base/README.md`。
> 测试套路（保护真实数据 `~/.jztoolshub`）：先 `import jztools_data; jztools_data.get_data_root = lambda: <临时目录>`，再 `import app`，
> 复制 `config/tools.json` 进临时目录，`register_plugin_backends(app)`，用 `app.test_client()` 打接口；**不要调用 `init_data_root()`**；运行用隔离 venv
> （`C:/Users/yfjz/.workbuddy/binaries/python/envs/default/Scripts/python.exe`）。

| 阶段 | 主题 | 关键落点 |
| --- | --- | --- |
| 0 设计 | 需求边界 / 引擎选型 / 存储·模型·API·前后端改动面 | 本文 §1–§8 |
| 1 后端存储与下载 | `_file_brief` / 幂等迁移 `_migrate_files` / 原件落盘 / `kb_file_download` / 删除清理 | `backend/routes.py` |
| 2 PDF 引擎与异步管线（**已退役**） | `pdf_convert.py`、`_PDF_POOL`、`/pdf`、`/pdf-retry`、启动补偿 | 被阶段 8 替换；留痕 §15 |
| 3 前端 | `reader.js` `renderPdf` URL 覆盖、`app.js` 下载/角标/轮询、`index.html`/`style.css` | `frontend/{reader,app}.js` |
| 4 真机验证 | LibreOffice 25.8.7 端到端、重启补偿、浏览器走查 | §15 |
| 5 文档同步 | 插件/根 README、知识库设计文档、HANDOFF | — |
| 7 Excel HTML 预览（**已退役**） | `xlsx_render.py` openpyxl 手绘 HTML、`/preview` | 被阶段 8 替换 |
| 8 xhr/dhr 双引擎 | `office_render.py`、`vendor/{xhr,dhr}`、`/preview` 按需同步渲染 + `<id>.preview.json` 缓存 | §13 决策末两条 |
| 9 Word 产物后处理补丁 | `_polish_word_html` / `_patch_font_fallback` / `_patch_table_overflow` / `_patch_css` | §16 |
| 10 异步下载源 | `_read_upload`、`<id>.dl.<ext>`、`kb_file_download` 下载源优先 | §17 |
| 11 开关点击修复 | `.switch-row` 整行包 `<label>`（原生 label↔input 关联） | §18 |

### 阶段 6：遗留 / 增强（本期不做，可后补）

- [ ] 存量 doc/xls 历史记录原件不可恢复的补偿策略评估（§11 已知折衷）

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

## 15. 实施落地与实测踩坑（2026-09-11 真机验证，**PDF 管线，已退役**）

- 引擎：非管理员 `msiexec /a` 解包 LibreOffice 25.8.7，经数据根 `config.json` 的 `pdf.soffice_path` 指定（探测链 `backend/pdf_convert.py`）；全链路 test_client 16/16、重启补偿 `_recover_pending_pdfs` 通过。
- 实测口径：docx 2 页 / 105KB、xlsx 3 sheet→3 页 / 230KB（`SUM(E3:E7)=120200` 被求值）；PDF 在 pdf.js 中中文无方块、样式保留。
- 踩坑（已留痕 HANDOFF §7）：profile 固定复用使 37~40s→15~18s；解包版 `soffice --version` 会挂起，须压 10s 超时；前端轮询上限需 180s（与后端转换超时对齐）；`import` 兜底须两步式（与 `doc_convert` 对齐）。
- 上述随 §7/§8 的 PDF 管线一并退役，留作史实与踩坑索引。

---

## 16. 阶段 9：Word 产物后处理补丁（2026-09-18）

- 根因（报告 `docs/archive/知识库Word预览三问题排查报告.md`）：① 客户端缺 仿宋_GB2312，引擎 fallback 直落微软雅黑（实测墨量差 2.56×）；② `<tr>` 的 `overflow:hidden` 对 table-row 无效 + base CSS 未重置 `<p>` margin 撑高单元格（27px→49/67px）。
- 决策：不改 vendor（升级整目录替换、易漏），在 `office_render.render_word()` 出口做 HTML 后处理 `_polish_word_html`（`_patch_font_fallback` / `_patch_table_overflow` / `_patch_css`）。
- 踩坑：裸 `"仿宋"` 既是查找键又是插入值，须单独分支（否则自我匹配、重复注入、幂等破坏）；CSS 补丁须有存在性判据（防重复膨胀）；`<tr>` 清理须用全标签正则（防破坏标签闭合）；`font-family:` 断言取值须到 `;` / `}`（声明串本身含双引号）。
- 验证：回归 `backend/test_routes_preview.py` 第 0b 节 5 组 + 边界用例全过；字体→仿宋、行高 49/67→33/51px。

---

## 17. 阶段 10：异步下载源（2026-09-18）

- 需求：上传时可选另附「异步下载源」（文案固定「为预览文档提供异步下载源。」）；预览 `Demo.pdf` 时下载给 `Demo.doc`——预览保版式、下载给可编辑件，两个诉求两个文件。
- 决策：与预览件格式解耦；落盘 `<id>.dl.<download_ext>`（区别于原件与预览缓存）；下载源校验失败**不阻断主上传**（B-4，响应带 `download_error`）；盘上缺失**静默回落原件**；记录字段 `download_ext` / `download_name` / `download_size`（未配置不写键）；不递增 `PREVIEW_CACHE_VERSION`。
- 实现（`backend/routes.py`）：`_read_upload` 统一校验口径、`kb_file_upload` 取 `download_file`、`_download_path`、`kb_file_download` 下载源优先、`_file_brief` 加 `has_download_source` 等、`kb_file_delete` 清理下载源；前端 `frontend/{app.js,index.html,style.css}` 加上传开关与 `.dl-badge`。
- 踩坑：落盘名必须唯一（下载源与原件同扩展名会互相覆盖）；`_read_upload` 只取 basename（SEC-1）；开关关闭须清空 `input.files`；探针页 `file://` 相对路径写错会静默 404（表现为 CSS 全失效）。
- 验证：`backend/test_routes_preview.py` 第 4b 节 5 组 + 浏览器实测（FormData 含 `file` 与 `download_file`）。

---

## 18. 阶段 11：修复「异步下载源」开关点不动（2026-09-18）

- 根因：`.slider` 是 `position:absolute` 覆盖层，盖住 `<input>` 且自身非 `<label>`、`<span class="switch">` 不转发点击，仅左侧 `<label for>` 能激活——用户点滑块无效（标记结构选错，非 CSS）。
- 修法：`app.js` `showUpload()` 把整行 `.switch-row` 改为 `<label>` 直接包住 `<input type="checkbox">` 与 `.slider`；`style.css` `.switch-row{cursor:pointer}`；资源戳（F-2）`style.css?v=29→30`、`app.js?v=19→20`（现值以 `frontend/index.html` 为准）。
- 验证 / 回归锁：真实指针点击四态通过；`test_routes_preview.py` 第 6 节断言开关行必须是 `<label class="form-row switch-row">` 且禁旧写法 `for="m-dl-switch"`。
- 踩坑：浏览器验证不能只用程序化 `.click()`（修复前也「看似」走通），须真实指针点击；agent-browser 会话不跨命令（链式 `open→click→eval→screenshot`）；隔离 E2E 须伪造 `get_session_user` 为管理员，否则一律 401 跳登录页。

---

## 19. 阶段 12：LibreOffice 持久 profile 池（P2-21，2026-10-10）

- 背景：`.xls`/`.doc` 归一化走 LibreOffice，**每次转换新建 UserInstallation profile**（冷启动，本机实测每次 39~77s）；方案《20261009 决策落地方案》§B-21 裁定「vendor 加默认保持现行为的最小接缝 + 上层持久 profile 池」（历史目标机实测冷 37~40s → 热 15~18s，2.2× 收益）。
- 落点（vendor **两份同改**，默认行为不变；登记 `vendor/README.md` 清单第 6、7 条）：
  - `xhr/normalize/libreoffice.py`：模块级 `set_profile_provider(provider)` 钩子 + `_acquire_profile()`；`convert_to_xlsx` 优先租槽，取不到回退「`mkdtemp` 新建即删」；
  - `dhr/normalize.py`：同构接缝（`normalize_doc_to_docx`）；
  - 不注册时与上游逐字节等价（回归第 1~4 组钉死）；provider 抛异常/返回畸形 → 回退临时路径（由接缝兜住、不外泄）；三条铁律（唯一 profile / 输出存在性校验 / 杀进程树）全部保留。
- 池（`office_render._LoProfilePool`）：两池（`xhr-slot`/`dhr-slot`）各 2 槽 = 引擎并发闸门数（两处 `_SEMA=2`）；**LIFO（后进先出）**租出——串行场景第 2 次转换起即复用热槽位（这是 LIFO 而非轮转的目的：串行不为第二槽白付一次冷启动），并发场景两租约自然分属不同槽；位置 `<数据根>/plugins/knowledge-base/data/lo_profiles/`；固定槽位名 + TTL 30 天清扫 → 数量与体积有界；转换失败 → 释放时擦除该槽（失败清理铁律不回退）；取不到/池异常 → `None` → 引擎降级临时 profile（渲染不失败，B-4）。
- 实测（`backend/bench_office_profile_pool.py`，本机 D:\LibreOffice，N=3，秒）：

  | 轮次 | 改动前（每次新建，全冷） | 改动后（持久池，LIFO） |
  | --- | --- | --- |
  | .xls run1/2/3 | 39.3 / 48.6 / 46.3 | 46.1 / **16.2 / 18.0** |
  | .doc run1/2/3 | 45.6 / 46.4 / 49.9 | 44.0 / **14.5 / 16.9** |

  - 槽位体积：`xhr-slot-1` 0.9MB、`dhr-slot-1` 1.0MB（串行只占用一槽，另一槽未建——固定槽位数使上限 ≈4MB）；热态 14.5~18.0s 与历史目标机 15~18s 同量级。
  - 并发 2 路（验收②）：预热轮含一次性「第二槽冷启动」41.3s；两槽皆热后串行基线 16.6/15.8s、并发 16.0/16.0s（墙钟 16.0 ≈ max，真并行；并发/串行 = 0.97x/1.01x ≤ 1.5x 判定阈值）；产物 HTML 逐字一致（不串扰）。
  - 判定口径：不退化按「病态退化」定义阈值（两路撞同一 profile 的锁等待/共享锁序列化会到 ≥2×），正常 CPU 竞争波动（本机复测 ±1~3s）不判退化；另加「墙钟 ≈ max(A,B) 而非 A+B」的真并行检查。
- 已知边界（留痕，未改口径）：
  - 生产默认转换超时 60s（xhr `soffice_timeout_ms`）：本机全冷首跑 39.3~76.8s（跨多次运行波动），慢机冷路径可能 TIMEOUT → 前端自动回退降级渲染（B-4），profile 预热后正常。**【2026-10-16 拍板：上调默认超时】** 候选值 120s（或「首次宽限 + 后续收紧」二段式）——超时是硬失败、用户无从补救，放宽几乎无副作用；具体值随实施定，落点 `plugins/knowledge-base/backend/vendor/xhr/normalize/__init__.py` 的 `soffice_timeout_ms`。
  - 池状态为进程内单例（与「单 Flask 进程」部署形态一致）；同机多进程共指同一数据根时，最坏为槽位复用冲突 → 输出存在性校验兜底 → B-4 降级，不静默。
- 回归锁：`backend/test_office_profile_pool.py`（9 组 51 断言：接缝默认/租用/降级、dhr 接缝、池 LIFO/清扫/失败降级/并发/接线；不依赖真实 LibreOffice——假 soffice + 注入 runner）；`backend/bench_office_profile_pool.py`（本表数据来源：改动前/后两臂 + 并发对照 + 槽位体积）。两脚本按 payload 规则（`test_*.py`/`bench_*.py`）剔除，不入插件包。
