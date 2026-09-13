"""知识库 —— JZToolsHub 后端插件路由。

功能：
- 管理员角色（role-admin）/超级管理员可上传 PDF / OFD / Word / Excel / Markdown /
  纯文本等文档，落盘到数据根目录 plugins/knowledge-base/data/files/
  （服务端生成 ID 重命名，SEC-3），并支持多级分类树管理（新建 / 改名 / 移动 / 删除）；
- **旧版格式自动转换**：上传 `.doc` / `.xls` 时先由服务端转换为 `.docx` / `.xlsx`
  再落盘（见 doc_convert.py），原格式记于 `original_ext` 字段，
  前端据此在页面提示"已自动转换"。转换依赖缺失或文件损坏时返回 4xx 明确提示，不 500；
- **双份保存（《插件库优化方案》阶段 1）**：上传时先保存**原件** `<id>.<original_ext>`
  （下载端点专用，权威原件），再保存**渲染件** `<id>.docx/.xlsx`（旧版格式转换产物，
  供前端降级渲染）；Word 类另生成展示用 PDF `<id>.pdf`（阶段 2）；
- **下载**：`GET /files/<id>/download` 以 attachment 附件形式返回**原件**
  （Word/Excel 下载给原始文档，不提供 PDF 版下载）；全体登录用户可用；
- **PDF 预览（《插件库优化方案》阶段 2，Word 类专用）**：doc/docx 上传后由后台线程池
  调 LibreOffice headless 生成 `<id>.pdf`（`pdf_status`: pending→ok/failed），
  `GET /files/<id>/pdf` 内联返回供前端 pdf.js 按原样式渲染；生成失败不阻断任何功能，
  阅读端回退降级渲染；`POST /files/<id>/pdf-retry` 供管理员手动重转；
- **Excel 表格预览（阶段 3，替代 Excel 的 PDF 预览）**：xlsx/xls 上传后由后台线程池
  调 xlsx_render（openpyxl 手绘 HTML 表格）生成 `<id>.json`（`html_status`:
  pending→ok/failed，见 xlsx_render.py 模块头），`GET /files/<id>/preview` 返回
  各 sheet 的 HTML 片段——**连续单页无分页**，还原合并单元格/列宽/边框/填充/
  字体/数字与日期格式；生成失败不阻断任何功能，阅读端回退 SheetJS 简化渲染；
  `POST /files/<id>/preview-retry` 供管理员手动重转；
- 全体登录用户可浏览分类树与文件列表，并经 /files/<id>/raw 内联读取文件内容
  供前端纯 JS 渲染（pdf.js / ofd.js / mammoth / SheetJS / marked，无浏览器控件）。

数据持久化（规范 9.1/9.2）：
- data/categories.json  分类树（单库文件）
- data/files.json       文件元数据索引（单库文件）
- data/files/<id>.<original_ext>  上传的**原件**（下载用；<id> 匹配 ^[A-Za-z0-9_-]+$，SEC-1）
- data/files/<id>.<ext>           **渲染件**（doc/xls 的转换产物 docx/xlsx；
                                  原生 docx/xlsx/pdf/… 与原件同名，不重复落盘）
- data/files/<id>.pdf             展示用 PDF（阶段 2 起，仅 Word 类）
- data/files/<id>.json            Excel 表格预览（阶段 3 起，仅 xlsx/xls 类）
- 分类与文件记录均含 created_by / created_by_name / unit_id / department_id
  四个归属字段，一律取自服务端会话、禁止从请求体接收（铁律一）；
  阅读为全站公共资源，列表不做单位/部门过滤（《知识库插件-设计文档》§3.3）。

接口前缀：/api/knowledge-base
"""

import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import jsonify, request, send_file

try:
    from jztools_admin.routes import get_session_user as _get_session_user
except Exception:  # admin 插件缺失时兜底（理论上不会发生）
    _get_session_user = None

try:
    from jztools_admin.routes import set_operation as _set_operation
except Exception:  # 主应用未提供日志辅助时兜底（理论上不会发生）
    def _set_operation(op):
        pass

import jztools_data

try:
    from . import doc_convert as _doc_convert
except ImportError:  # 插件以脚本方式加载时的兜底（无包上下文）
    import doc_convert as _doc_convert

# PDF 转换引擎（阶段 2 引入 pdf_convert.py；模块缺失 = 引擎不可用，走降级链）
# 注意两段式兜底：包上下文（主应用加载）→ 裸 import（脚本方式加载，如测试/补偿场景）。
# 实测坑：except Exception 一把梭 + 不做裸 import 兜底时，脚本方式加载的实例
# 会把「引擎不存在」误判为 True（_pdf_convert=None），补偿扫描直接置 failed。
try:
    from . import pdf_convert as _pdf_convert
except ImportError:
    try:
        import pdf_convert as _pdf_convert
    except ImportError:
        _pdf_convert = None

# Excel 表格预览渲染引擎（阶段 3 引入 xlsx_render.py；模块缺失 = 引擎不可用，走降级链。
# 两段式兜底口径同 pdf_convert）
try:
    from . import xlsx_render as _xlsx_render
except ImportError:
    try:
        import xlsx_render as _xlsx_render
    except ImportError:
        _xlsx_render = None

DATA_DIR = jztools_data.get_data_root_dir("plugins", "knowledge-base", "data")
FILES_DIR = jztools_data.get_data_root_dir("plugins", "knowledge-base", "data", "files")
API_PREFIX = "/api/knowledge-base"

# 可管理（上传/分类维护）角色：超级管理员 + 管理员角色（与公告板口径一致）
MANAGE_ROLE_IDS = {"role-admin"}
MANAGE_ROLE_NAMES = {"管理员"}

ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_NAME_LEN = 80                      # 展示名/分类名长度上限
MAX_SUMMARY_LEN = 200                  # 文件简介长度上限
MAX_UPLOAD_BYTES = 20 * 1024 * 1024    # 20MB（SEC-3 大小上限）
MAX_CATEGORIES = 200                   # 分类总量上限（防滥用）
MAX_FILES = 2000                       # 文件总量上限（防滥用）

# 上传扩展名白名单（SEC-3）
# 其中 doc / xls 为旧版二进制格式，服务端会先转为 docx / xlsx 再落盘（见 doc_convert.py），
# 库内只保留现代格式，前端渲染链路无需为老格式引入额外渲染库。
ALLOWED_EXTS = {"pdf", "ofd", "docx", "xlsx", "xls", "doc", "md", "markdown", "txt", "csv"}

# 需要服务端转换的旧版格式 → 目标格式
LEGACY_CONVERT = {"doc": "docx", "xls": "xlsx"}

# Word 类：需要生成展示用 PDF（阶段 3 起 Excel 不再走 PDF——其 PDF 按打印分页，
# 宽表/长表被切碎不利阅读，Excel 预览改由 xlsx_render 手绘 HTML 连续表格）
PDF_SOURCE_EXTS = {"doc", "docx"}

# Excel 类：需要生成表格 HTML 预览（阶段 3）
XLSX_SOURCE_EXTS = {"xls", "xlsx"}

# raw 端点 mimetype（前端一律 fetch 字节流交渲染器，mimetype 仅供调试）
EXT_MIME = {
    "pdf": "application/pdf",
    "ofd": "application/ofd",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "doc": "application/msword",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
}

# 全局文件写锁（categories.json / files.json 读写共用）
_LOCK = threading.RLock()


# ===================== 会话与权限 =====================

def _viewer():
    """当前登录用户上下文；未登录返回 None。"""
    if _get_session_user is None:
        return None
    try:
        return _get_session_user()
    except Exception:
        return None


def _can_manage(user):
    """管理权限：超级管理员或管理员角色。"""
    if not user:
        return False
    if user.get("super_admin"):
        return True
    return (user.get("role_id") in MANAGE_ROLE_IDS
            or user.get("role") in MANAGE_ROLE_NAMES)


# ===================== 存储（单库 JSON + 原子写） =====================

def _categories_file():
    return os.path.join(DATA_DIR, "categories.json")


def _files_file():
    return os.path.join(DATA_DIR, "files.json")


def _load_store(path):
    """读取单库 JSON；文件缺失/损坏时返回空库（幂等重建）。"""
    if not os.path.isfile(path):
        return {"version": 1, "categories": []} if path == _categories_file() \
            else {"version": 1, "files": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        return {"version": 1, "categories": []} if path == _categories_file() \
            else {"version": 1, "files": []}
    if not isinstance(rec, dict):
        rec = {}
    if not isinstance(rec.get("categories"), list) and "categories" in path:
        rec["categories"] = []
    if not isinstance(rec.get("files"), list) and "files" in path:
        rec["files"] = []
    rec.setdefault("version", 1)
    return rec


def _save_store(path, rec):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _gen_id(prefix, existing_ids):
    """服务端生成 ID（8 位 hex），带查重（SEC-3：禁止沿用用户输入作为主键）。"""
    while True:
        rid = f"{prefix}{uuid.uuid4().hex[:8]}"
        if rid not in existing_ids:
            return rid


def _file_path(fid, ext):
    """落盘路径：id 必须匹配白名单正则（SEC-1 目录穿越防线）。"""
    if not ID_RE.match(fid or ""):
        return None
    if ext not in ALLOWED_EXTS:
        return None
    return os.path.join(FILES_DIR, f"{fid}.{ext}")


def _pdf_engine_available():
    """PDF 转换引擎是否可用（阶段 2 前恒 False，调用处按降级链处理）。"""
    if _pdf_convert is None:
        return False
    try:
        return bool(_pdf_convert.detect_soffice().get("available"))
    except Exception:
        return False


def _migrate_files():
    """files.json 字段补齐（幂等，启动时执行一次）。

    历史记录的原件已被转换件覆盖（"替换存储"时代的行为），无法回溯，
    因此按「现有落盘件即原件」回填，下载端点据此仍可提供文件。
    """
    with _LOCK:
        store = _load_store(_files_file())
        changed = False
        for rec in store.get("files", []):
            ext = rec.get("ext", "")
            # 一律用「键是否存在」判定，不能用取值判定：
            # pdf_size 恒为 None（未生成 PDF 时），用 rec.get(...) is None 会导致每次启动都误判需迁移。
            if "original_ext" not in rec or not rec.get("original_ext"):
                rec["original_ext"] = ext          # 旧记录：以现有落盘件为原件
                changed = True
            if "original_size" not in rec:
                rec["original_size"] = rec.get("size", 0)
                changed = True
            if "pdf_status" not in rec or not rec.get("pdf_status"):
                # 阶段 1 一律置 none；阶段 2 的启动补偿扫描「word/excel 类 + none + 无 pdf 文件」
                # 的记录入队补转，failed 不自动重试。
                # 阶段 3 起 Excel 不再生成 PDF：历史 pending/failed 记录复位为 none
                # （已有 PDF 文件的旧记录保留，不再消费），预览转由 html 管线接管。
                rec["pdf_status"] = "none"
                changed = True
            if "pdf_size" not in rec:
                rec["pdf_size"] = None
                changed = True
            if "pdf_error" not in rec:
                rec["pdf_error"] = ""
                changed = True
            if (rec.get("original_ext") or rec.get("ext") or "") in XLSX_SOURCE_EXTS \
                    and rec.get("pdf_status") in ("pending", "failed"):
                # 阶段 3 迁移：Excel 的 PDF 预览管线退役
                rec["pdf_status"] = "none"
                rec["pdf_error"] = ""
                changed = True
            if "html_status" not in rec or not rec.get("html_status"):
                # 阶段 3 新增：Excel 表格预览状态（none|pending|ok|failed）
                rec["html_status"] = "none"
                changed = True
            if "html_size" not in rec:
                rec["html_size"] = None
                changed = True
            if "html_error" not in rec:
                rec["html_error"] = ""
                changed = True
        if changed:
            _save_store(_files_file(), store)
        return changed


# ===================== PDF 预览转换管线（阶段 2，规范 8.5 异步任务） =====================

# 转换线程池：max_workers=1 → 全局串行（soffice CPU 开销大，串行最稳；
# pdf_convert 内部还有一把模块级锁，双重保险）
_PDF_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kb-pdf")

_PDF_NONE_ENGINE_MSG = "服务器未检测到 LibreOffice，无法生成 PDF 预览"


def _pdf_path(fid):
    """展示用 PDF 落盘路径（id 白名单校验同 SEC-1）。"""
    if not ID_RE.match(fid or ""):
        return None
    return os.path.join(FILES_DIR, f"{fid}.pdf")


def _needs_pdf(rec):
    """该记录是否需要生成 PDF 预览（word/excel 类原件）。"""
    return (rec.get("original_ext") or rec.get("ext") or "") in PDF_SOURCE_EXTS


def _pdf_finish(fid, ok, size, error):
    """回写转换结果（在 _LOCK 内重新读库，防止并发覆盖）。

    记录已被删除时静默放弃，并清理可能已生成的 PDF（避免孤儿文件）。
    """
    with _LOCK:
        store = _load_store(_files_file())
        rec = next((f for f in store.get("files", []) if f.get("id") == fid), None)
        if rec is None:
            p = _pdf_path(fid)
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return
        if ok:
            rec["pdf_status"] = "ok"
            rec["pdf_size"] = int(size or 0)
            rec["pdf_error"] = ""
        else:
            rec["pdf_status"] = "failed"
            rec["pdf_size"] = None
            # SEC-5：只存用户可读文案，不回传堆栈/路径
            rec["pdf_error"] = str(error or "PDF 生成失败")[:200]
        rec["updated_at"] = _now()
        _save_store(_files_file(), store)


def _pdf_task(fid):
    """转换任务体（线程池里跑，禁止抛异常出线程）。"""
    try:
        with _LOCK:
            store = _load_store(_files_file())
            rec = next((f for f in store.get("files", []) if f.get("id") == fid), None)
            if rec is None or rec.get("pdf_status") != "pending":
                return
            src_ext = rec.get("original_ext") or rec.get("ext") or ""
            src = _file_path(fid, src_ext)
            dst = _pdf_path(fid)
        if not src or not dst:
            _pdf_finish(fid, False, None, "文件标识不合法")
            return
        if not os.path.isfile(src):
            _pdf_finish(fid, False, None, "原件缺失，无法生成 PDF 预览")
            return
        if _pdf_convert is None:
            _pdf_finish(fid, False, None, _PDF_NONE_ENGINE_MSG)
            return
        try:
            _pdf_convert.convert_to_pdf(src, dst)
        except _pdf_convert.PdfConvertError as e:
            _pdf_finish(fid, False, None, str(e))
            return
        except Exception:
            _pdf_finish(fid, False, None, "PDF 生成失败（内部错误）")
            return
        size = os.path.getsize(dst) if os.path.isfile(dst) else 0
        if not size:
            _pdf_finish(fid, False, None, "PDF 生成失败（产物为空）")
            return
        _pdf_finish(fid, True, size, "")
    except Exception:
        # 兜底：任务体不得抛异常出线程（否则状态永远卡 pending）
        try:
            _pdf_finish(fid, False, None, "PDF 生成失败（内部错误）")
        except Exception:
            pass


def _enqueue_pdf(fid):
    """把一个转换任务提交到线程池（入队即返回，转换在后台）。"""
    if _pdf_convert is None:
        # 模块缺失 = 引擎不可用：直接置 failed，前端走降级链
        _pdf_finish(fid, False, None, _PDF_NONE_ENGINE_MSG)
        return False
    try:
        _PDF_POOL.submit(_pdf_task, fid)
    except Exception:
        _pdf_finish(fid, False, None, "PDF 转换服务不可用，请稍后重试")
        return False
    return True


def _recover_pending_pdfs():
    """启动补偿：把「应转未转」的记录重新入队（服务重启 / 版本升级后自动补齐）。

    命中条件：word/excel 类 + `pdf_status ∈ {none, pending}` + PDF 文件不存在。
    `failed` 不自动重试（防坏文件每次启动触发转换风暴），由管理员经 retry 端点手动重转。
    """
    queued = []
    with _LOCK:
        store = _load_store(_files_file())
        changed = False
        for rec in store.get("files", []):
            if not _needs_pdf(rec):
                continue
            if rec.get("pdf_status") not in (None, "", "none", "pending"):
                continue
            dst = _pdf_path(rec.get("id") or "")
            if dst and os.path.isfile(dst):
                continue
            rec["pdf_status"] = "pending"
            changed = True
            queued.append(rec.get("id"))
        if changed:
            _save_store(_files_file(), store)
    for fid in queued:
        if fid:
            _enqueue_pdf(fid)
    return queued


# ===================== Excel 表格预览管线（阶段 3，规范 8.5 异步任务） =====================
# 与 PDF 管线同构：上传置 pending → 线程池渲染 → ok/failed；失败前端回退 SheetJS。

# 渲染线程池：max_workers=1 → 全局串行（openpyxl 载入大表内存开销大，串行最稳；
# 与 PDF 池相互独立，Word 的 LibreOffice 长任务不阻塞 Excel 的秒级渲染）
_HTML_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kb-xlsx")


def _html_path(fid):
    """表格预览 JSON 落盘路径（id 白名单校验同 SEC-1）。"""
    if not ID_RE.match(fid or ""):
        return None
    return os.path.join(FILES_DIR, f"{fid}.json")


def _needs_html(rec):
    """该记录是否需要生成表格预览（Excel 类原件）。"""
    return (rec.get("original_ext") or rec.get("ext") or "") in XLSX_SOURCE_EXTS


def _html_finish(fid, ok, size, error):
    """回写渲染结果（在 _LOCK 内重新读库，防止并发覆盖）。

    记录已被删除时静默放弃，并清理可能已生成的 JSON（避免孤儿文件）。
    """
    with _LOCK:
        store = _load_store(_files_file())
        rec = next((f for f in store.get("files", []) if f.get("id") == fid), None)
        if rec is None:
            p = _html_path(fid)
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return
        if ok:
            rec["html_status"] = "ok"
            rec["html_size"] = int(size or 0)
            rec["html_error"] = ""
        else:
            rec["html_status"] = "failed"
            rec["html_size"] = None
            # SEC-5：只存用户可读文案，不回传堆栈/路径
            rec["html_error"] = str(error or "表格预览生成失败")[:200]
        rec["updated_at"] = _now()
        _save_store(_files_file(), store)


def _html_task(fid):
    """渲染任务体（线程池里跑，禁止抛异常出线程）。"""
    try:
        with _LOCK:
            store = _load_store(_files_file())
            rec = next((f for f in store.get("files", []) if f.get("id") == fid), None)
            if rec is None or rec.get("html_status") != "pending":
                return
            src_ext = rec.get("ext") or rec.get("original_ext") or ""
            # 渲染源用**渲染件**（doc/xls 旧格式上传时已转为 xlsx）；旧库里的
            # 裸 .xls 记录 openpyxl 读不了 → 渲染失败 → 前端回退 SheetJS（其可读 xls）
            src = _file_path(fid, src_ext)
            dst = _html_path(fid)
        if not src or not dst:
            _html_finish(fid, False, None, "文件标识不合法")
            return
        if not os.path.isfile(src):
            _html_finish(fid, False, None, "文件缺失，无法生成表格预览")
            return
        if _xlsx_render is None:
            _html_finish(fid, False, None, "服务器缺少 openpyxl 依赖，无法生成表格预览")
            return
        try:
            _xlsx_render.render_xlsx(src, dst)
        except _xlsx_render.XlsxRenderError as e:
            _html_finish(fid, False, None, str(e))
            return
        except Exception:
            _html_finish(fid, False, None, "表格预览生成失败（内部错误）")
            return
        size = os.path.getsize(dst) if os.path.isfile(dst) else 0
        if not size:
            _html_finish(fid, False, None, "表格预览生成失败（产物为空）")
            return
        _html_finish(fid, True, size, "")
    except Exception:
        # 兜底：任务体不得抛异常出线程（否则状态永远卡 pending）
        try:
            _html_finish(fid, False, None, "表格预览生成失败（内部错误）")
        except Exception:
            pass


def _enqueue_html(fid):
    """把一个渲染任务提交到线程池（入队即返回，渲染在后台）。"""
    if _xlsx_render is None:
        # 模块缺失 = 引擎不可用：直接置 failed，前端走降级链
        _html_finish(fid, False, None, "服务器缺少 openpyxl 依赖，无法生成表格预览")
        return False
    try:
        _HTML_POOL.submit(_html_task, fid)
    except Exception:
        _html_finish(fid, False, None, "表格预览服务不可用，请稍后重试")
        return False
    return True


def _recover_pending_htmls():
    """启动补偿：把「应渲未渲」的 Excel 记录重新入队（服务重启 / 版本升级后自动补齐）。

    命中条件：Excel 类 + `html_status ∈ {none, pending}` + JSON 文件不存在。
    JSON 已存在但状态未收敛 → 直接记 ok（上次渲染完成后写库前的异常中断残留）。
    `failed` 不自动重试（防坏文件每次启动触发渲染风暴），由管理员经 retry 端点手动重转。
    """
    queued = []
    with _LOCK:
        store = _load_store(_files_file())
        changed = False
        for rec in store.get("files", []):
            if not _needs_html(rec):
                continue
            dst = _html_path(rec.get("id") or "")
            if not dst:
                continue
            if os.path.isfile(dst):
                if rec.get("html_status") not in ("ok",):
                    rec["html_status"] = "ok"
                    try:
                        rec["html_size"] = os.path.getsize(dst)
                    except OSError:
                        rec["html_size"] = None
                    rec["html_error"] = ""
                    changed = True
                continue
            if rec.get("html_status") not in (None, "", "none", "pending"):
                continue
            rec["html_status"] = "pending"
            changed = True
            queued.append(rec.get("id"))
        if changed:
            _save_store(_files_file(), store)
    for fid in queued:
        if fid:
            _enqueue_html(fid)
    return queued


# ===================== 分类树 =====================

def _find_cat(cats, cid):
    for c in cats:
        if c.get("id") == cid:
            return c
    return None


def _descendant_ids(cats, cid):
    """收集某分类的全部子孙分类 id（移动防环 / 列表递归用）。"""
    children = {}
    for c in cats:
        children.setdefault(c.get("parent_id"), []).append(c.get("id"))
    out, stack = set(), [cid]
    while stack:
        cur = stack.pop()
        for ch in children.get(cur, []):
            if ch not in out:
                out.add(ch)
                stack.append(ch)
    return out


def _sibling_name_taken(cats, name, parent_id, exclude_id=None):
    n = (name or "").strip()
    for c in cats:
        if c.get("id") == exclude_id:
            continue
        if (c.get("name") or "").strip() == n and c.get("parent_id") == parent_id:
            return True
    return False


# ===================== 文件元数据 =====================

def _file_brief(rec):
    ext = rec.get("ext", "")
    # original_ext：上传时的扩展名（迁移后所有记录都有值；缺失时回落 ext 兼容旧数据）
    original_ext = rec.get("original_ext") or ext
    return {
        "id": rec.get("id"),
        "name": rec.get("name", ""),
        "summary": rec.get("summary", ""),
        "original_name": rec.get("original_name", ""),
        "ext": ext,
        "size": rec.get("size", 0),
        # 原件（下载端点的返回对象）：大小与扩展名
        "original_ext": original_ext,
        "original_size": rec.get("original_size", rec.get("size", 0)),
        # PDF 预览（阶段 2 生效，仅 Word 类）：pdf_status = none|pending|ok|failed
        "pdf_ready": rec.get("pdf_status") == "ok",
        "pdf_status": rec.get("pdf_status") or "none",
        # failed 时的用户可读原因（角标悬停展示；由服务端生成，不含堆栈/路径）
        "pdf_error": rec.get("pdf_error") or "",
        # Excel 表格预览（阶段 3 生效，仅 xlsx/xls 类）：html_status = none|pending|ok|failed
        "html_ready": rec.get("html_status") == "ok",
        "html_status": rec.get("html_status") or "none",
        "html_size": rec.get("html_size"),
        "html_error": rec.get("html_error") or "",
        "category_id": rec.get("category_id"),
        # converted：是否由服务端做过旧版格式转换（原格式 ≠ 落盘主格式）
        "converted": bool(original_ext and original_ext != ext),
        "created_by": rec.get("created_by", ""),
        "created_by_name": rec.get("created_by_name", ""),
        "created_at": rec.get("created_at", ""),
        "updated_at": rec.get("updated_at") or "",
    }


def register(app) -> None:
    """插件入口：由 JZToolsHub 主应用在启动时调用。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FILES_DIR, exist_ok=True)
    try:
        _migrate_files()   # 幂等补齐元数据新字段（旧数据升级）
    except Exception:
        pass               # 迁移失败不影响插件主体可用（下一轮启动继续尝试）
    try:
        _recover_pending_pdfs()   # 断点恢复：重启后补转 pending / 应转未转的 Word 类记录
    except Exception:
        pass
    try:
        _recover_pending_htmls()  # 断点恢复：重启后补渲 pending / 应渲未渲的 Excel 类记录
    except Exception:
        pass

    @app.get(f"{API_PREFIX}/status")
    def kb_status():
        """依赖自检（B-4）。

        - 核心功能（浏览/渲染现代格式）纯标准库实现，恒可用；
        - 旧版格式转换（.doc→.docx / .xls→.xlsx）依赖 openpyxl/xlrd/python-docx/olefile，
          缺失时仅"自动转换"能力不可用，插件其余功能不受影响（优雅降级）；
        - PDF 预览依赖**目标机安装 LibreOffice**（外部程序，非 pip 包），
          缺失时 `pdf_engine.available=false`，上传/阅读/下载照常，仅阅读回退降级渲染。

        `?refresh=1` 强制重新探测 LibreOffice —— 会派生子进程读版本号，
        因此**仅对管理员生效**（防未登录调用放大成探测风暴），普通用户看到缓存结果。
        """
        deps = _doc_convert.availability()
        refresh = str(request.args.get("refresh") or "").strip().lower() in ("1", "true", "yes")
        if refresh and not _can_manage(_viewer()):
            refresh = False
        if _pdf_convert is None:
            engine = {"available": False, "path": None, "version": None,
                      "error": "PDF 转换模块未加载"}
        else:
            try:
                engine = _pdf_convert.engine_status(refresh=refresh)
            except Exception:
                engine = {"available": False, "path": None, "version": None,
                          "error": "PDF 引擎自检失败"}
        if _xlsx_render is None:
            xlsx_engine = {"available": False, "error": "表格渲染模块未加载"}
        else:
            try:
                xlsx_engine = _xlsx_render.availability()
            except Exception:
                xlsx_engine = {"available": False, "error": "表格渲染自检失败"}
        return jsonify({
            "ok": True,
            "dependencies": deps,
            "convert_legacy": all(deps.values()),
            "pdf_engine": engine,
            "pdf_preview": bool(engine.get("available")),
            "xlsx_render": xlsx_engine,
            "xlsx_preview": bool(xlsx_engine.get("available")),
        })

    @app.get(f"{API_PREFIX}/config")
    def kb_config():
        """前端渲染开关：当前用户是否可管理（上传/分类维护）。"""
        user = _viewer()
        return jsonify({"ok": True, "can_manage": _can_manage(user)})

    # ---------------- 分类 ----------------

    @app.get(f"{API_PREFIX}/categories")
    def kb_categories():
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            store = _load_store(_categories_file())
        cats = sorted(store["categories"], key=lambda c: (c.get("order", 0), c.get("created_at", "")))
        return jsonify({"ok": True, "count": len(cats), "items": cats})

    @app.post(f"{API_PREFIX}/categories")
    def kb_category_create():
        _set_operation("新建知识库分类")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可管理知识库分类"}), 403
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        parent_id = str(body.get("parent_id") or "").strip() or None
        if not name:
            return jsonify({"ok": False, "error": "请输入分类名称"}), 400
        if len(name) > 40:
            return jsonify({"ok": False, "error": "分类名称不能超过 40 字"}), 400
        with _LOCK:
            store = _load_store(_categories_file())
            cats = store["categories"]
            if len(cats) >= MAX_CATEGORIES:
                return jsonify({"ok": False, "error": "分类数量已达上限"}), 409
            if parent_id and not _find_cat(cats, parent_id):
                return jsonify({"ok": False, "error": "上级分类不存在"}), 404
            if _sibling_name_taken(cats, name, parent_id):
                return jsonify({"ok": False, "error": "同级下已存在同名分类"}), 409
            order = 1
            siblings = [c for c in cats if c.get("parent_id") == parent_id]
            if siblings:
                order = max(int(c.get("order", 0) or 0) for c in siblings) + 1
            cat = {
                "id": _gen_id("cat-", {c.get("id") for c in cats}),
                "name": name,
                "parent_id": parent_id,
                "order": order,
                # 归属四字段：来源为会话（规范 9.2 铁律一）
                "created_by": user.get("username", ""),
                "created_by_name": user.get("name", ""),
                "unit_id": user.get("unit_id", ""),
                "department_id": user.get("department_id", ""),
                "created_at": _now(),
            }
            cats.append(cat)
            _save_store(_categories_file(), store)
        return jsonify({"ok": True, "id": cat["id"]})

    @app.put(f"{API_PREFIX}/categories/<cid>")
    def kb_category_update(cid):
        _set_operation("修改知识库分类")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可管理知识库分类"}), 403
        body = request.get_json(silent=True) or {}
        name = body.get("name")
        parent_id = body.get("parent_id")
        order = body.get("order")
        with _LOCK:
            store = _load_store(_categories_file())
            cats = store["categories"]
            cat = _find_cat(cats, cid)
            if not cat or not ID_RE.match(cid or ""):
                return jsonify({"ok": False, "error": "分类不存在或已被删除"}), 404
            if name is not None:
                name = str(name).strip()
                if not name:
                    return jsonify({"ok": False, "error": "请输入分类名称"}), 400
                if len(name) > 40:
                    return jsonify({"ok": False, "error": "分类名称不能超过 40 字"}), 400
                if _sibling_name_taken(cats, name, cat.get("parent_id"), exclude_id=cid):
                    return jsonify({"ok": False, "error": "同级下已存在同名分类"}), 409
                cat["name"] = name
            if parent_id is not None:
                parent_id = str(parent_id).strip() or None
                if parent_id == cid:
                    return jsonify({"ok": False, "error": "上级分类不能是自身"}), 400
                if parent_id and not _find_cat(cats, parent_id):
                    return jsonify({"ok": False, "error": "上级分类不存在"}), 404
                if parent_id and parent_id in _descendant_ids(cats, cid):
                    return jsonify({"ok": False, "error": "上级分类不能是自身的子分类"}), 400
                cat["parent_id"] = parent_id
            if order is not None:
                try:
                    cat["order"] = int(order)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "排序值不合法"}), 400
            _save_store(_categories_file(), store)
        return jsonify({"ok": True, "id": cid})

    @app.delete(f"{API_PREFIX}/categories/<cid>")
    def kb_category_delete(cid):
        _set_operation("删除知识库分类")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可管理知识库分类"}), 403
        with _LOCK:
            store = _load_store(_categories_file())
            cats = store["categories"]
            if not ID_RE.match(cid or "") or not _find_cat(cats, cid):
                return jsonify({"ok": False, "error": "分类不存在或已被删除"}), 404
            if any(c.get("parent_id") == cid for c in cats):
                return jsonify({"ok": False, "error": "该分类下存在子分类，请先删除子分类"}), 409
            fstore = _load_store(_files_file())
            if any(f.get("category_id") == cid for f in fstore["files"]):
                return jsonify({"ok": False, "error": "该分类下仍有文件，请先移出或删除文件"}), 409
            store["categories"] = [c for c in cats if c.get("id") != cid]
            _save_store(_categories_file(), store)
        return jsonify({"ok": True})

    # ---------------- 文件 ----------------

    @app.get(f"{API_PREFIX}/files")
    def kb_files():
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        category = (request.args.get("category") or "").strip()
        q = (request.args.get("q") or "").strip().lower()
        with _LOCK:
            fstore = _load_store(_files_file())
            cstore = _load_store(_categories_file())
        files = fstore["files"]
        if category and category != "all":
            # 选中分类 = 该分类及其全部子孙分类（浏览子分类时同步呈现父级归档内容）
            keep = _descendant_ids(cstore["categories"], category)
            keep.add(category)
            files = [f for f in files if f.get("category_id") in keep]
        if q:
            files = [f for f in files
                     if q in (f.get("name") or "").lower()
                     or q in (f.get("original_name") or "").lower()]
        items = sorted(files, key=lambda f: f.get("created_at", ""), reverse=True)
        return jsonify({"ok": True, "count": len(items),
                        "items": [_file_brief(f) for f in items]})

    @app.post(f"{API_PREFIX}/files")
    def kb_file_upload():
        _set_operation("上传知识库文件")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可上传知识库文件"}), 403

        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"ok": False, "error": "请选择要上传的文件"}), 400
        name = str(request.form.get("name") or "").strip()
        category_id = str(request.form.get("category_id") or "").strip()
        if not name:
            name = os.path.splitext(upload.filename)[0] or "未命名"
        if len(name) > MAX_NAME_LEN:
            return jsonify({"ok": False, "error": f"文件名不能超过 {MAX_NAME_LEN} 字"}), 400
        summary = str(request.form.get("summary") or "").strip()
        if len(summary) > MAX_SUMMARY_LEN:
            return jsonify({"ok": False, "error": f"简介不能超过 {MAX_SUMMARY_LEN} 字"}), 400

        ext = os.path.splitext(upload.filename)[1].lstrip(".").lower()
        if not ext:
            return jsonify({"ok": False, "error": "无法识别文件类型（缺少扩展名）"}), 415
        if ext not in ALLOWED_EXTS:
            return jsonify({"ok": False, "error": f"暂不支持 .{ext} 格式（支持：PDF/OFD/Word/Excel/Markdown/文本）"}), 415

        # 大小校验（SEC-3）：先查 Content-Length，再流式读取硬上限+1 字节防虚报
        if request.content_length and request.content_length > MAX_UPLOAD_BYTES + 1024 * 1024:
            return jsonify({"ok": False, "error": "文件超过 20MB 上限"}), 413
        blob = upload.stream.read(MAX_UPLOAD_BYTES + 1)
        if len(blob) > MAX_UPLOAD_BYTES:
            return jsonify({"ok": False, "error": "文件超过 20MB 上限"}), 413
        if not blob:
            return jsonify({"ok": False, "error": "文件内容为空"}), 400

        # 旧版格式（.doc / .xls）→ 服务端转换为 docx / xlsx 作为**渲染件**；
        # 原件始终单独保存（下载端点专用），不再被转换产物替换。
        # 转换失败处理（B-4）：PDF 引擎可用时降级件缺失不致命（仍可生成 PDF 预览）→ 不阻断；
        # 引擎不可用时降级件是唯一阅读途径 → 维持 4xx 明确提示。
        upload_ext = ext                    # 原始上传扩展名 = 原件扩展名（所有记录都记）
        converted_from = None               # 旧版转换来源（仅 doc/xls 有值，前端"已转换"提示用）
        render_blob, render_ext = blob, ext # 主落盘件（渲染件）
        if ext in LEGACY_CONVERT:
            converted = None
            try:
                converted = _doc_convert.convert_legacy(ext, blob)
            except _doc_convert.ConvertError as e:
                if not _pdf_engine_available():
                    return jsonify({"ok": False, "error": str(e)}), 422
            except Exception:
                if not _pdf_engine_available():
                    return jsonify({"ok": False, "error": f".{ext} 转换失败，请尝试另存为 "
                                                         f".{LEGACY_CONVERT[ext]} 后上传"}), 500
            if converted:
                new_blob, new_ext = converted
                if len(new_blob) > MAX_UPLOAD_BYTES:
                    return jsonify({"ok": False, "error": "文件转换后超过 20MB 上限，无法保存"}), 413
                render_blob, render_ext = new_blob, new_ext
                converted_from = ext

        with _LOCK:
            fstore = _load_store(_files_file())
            cstore = _load_store(_categories_file())
            if category_id:
                if not ID_RE.match(category_id) or not _find_cat(cstore["categories"], category_id):
                    return jsonify({"ok": False, "error": "目标分类不存在"}), 404
            if len(fstore["files"]) >= MAX_FILES:
                return jsonify({"ok": False, "error": "文件数量已达上限"}), 409
            fid = _gen_id("f-", {f.get("id") for f in fstore["files"]})
            # 1) 原件落盘（所有类型；下载端点返回它）
            orig_path = _file_path(fid, upload_ext)
            if orig_path is None:
                return jsonify({"ok": False, "error": "文件标识不合法"}), 400
            with open(orig_path, "wb") as f:
                f.write(blob)
            # 2) 渲染件落盘（与原件不同名时才额外写一份：doc/xls 的 docx/xlsx 转换产物）
            path = _file_path(fid, render_ext)
            if path is None:
                return jsonify({"ok": False, "error": "文件标识不合法"}), 400
            if os.path.abspath(path) != os.path.abspath(orig_path):
                with open(path, "wb") as f:
                    f.write(render_blob)
            # 3) 元数据
            rec = {
                "id": fid,
                "name": name,
                "summary": summary,
                "original_name": os.path.basename(upload.filename),
                "ext": render_ext,
                "size": len(render_blob),
                # 原件（下载用）：扩展名与字节数
                "original_ext": upload_ext,
                "original_size": len(blob),
                # PDF 预览：word/excel 类置 pending 由后台管线接管，其余类型恒 none
                "pdf_status": "pending" if upload_ext in PDF_SOURCE_EXTS else "none",
                "pdf_size": None,
                "pdf_error": "",
                # Excel 表格预览（阶段 3）：xlsx/xls 类置 pending 由渲染管线接管
                "html_status": "pending" if upload_ext in XLSX_SOURCE_EXTS else "none",
                "html_size": None,
                "html_error": "",
                "category_id": category_id or None,
                # 归属四字段：来源为会话（规范 9.2 铁律一）
                "created_by": user.get("username", ""),
                "created_by_name": user.get("name", ""),
                "unit_id": user.get("unit_id", ""),
                "department_id": user.get("department_id", ""),
                "created_at": _now(),
                "updated_at": None,
            }
            fstore["files"].append(rec)
            _save_store(_files_file(), fstore)
        resp = {"ok": True, "id": fid, "item": _file_brief(rec)}
        if converted_from:
            resp["converted_from"] = converted_from   # 前端据此提示"已自动转换"
        # 异步生成预览（入队即返回，转换/渲染在后台线程，规范 8.5）：
        # Word 类 → LibreOffice 转 PDF；Excel 类 → xlsx_render 渲染表格 HTML
        if rec.get("pdf_status") == "pending":
            resp["pdf_status"] = "pending"
            _enqueue_pdf(fid)
        else:
            resp["pdf_status"] = "none"
        if rec.get("html_status") == "pending":
            resp["html_status"] = "pending"
            _enqueue_html(fid)
        else:
            resp["html_status"] = "none"
        return jsonify(resp)

    @app.put(f"{API_PREFIX}/files/<fid>")
    def kb_file_update(fid):
        _set_operation("修改知识库文件信息")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可管理知识库文件"}), 403
        body = request.get_json(silent=True) or {}
        name = body.get("name")
        summary = body.get("summary")
        category_id = body.get("category_id")
        with _LOCK:
            fstore = _load_store(_files_file())
            cstore = _load_store(_categories_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
            if not rec:
                return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
            if name is not None:
                name = str(name).strip()
                if not name:
                    return jsonify({"ok": False, "error": "请输入文件名"}), 400
                if len(name) > MAX_NAME_LEN:
                    return jsonify({"ok": False, "error": f"文件名不能超过 {MAX_NAME_LEN} 字"}), 400
                rec["name"] = name
            if summary is not None:
                summary = str(summary).strip()
                if len(summary) > MAX_SUMMARY_LEN:
                    return jsonify({"ok": False, "error": f"简介不能超过 {MAX_SUMMARY_LEN} 字"}), 400
                rec["summary"] = summary
            if category_id is not None:
                category_id = str(category_id).strip()
                if category_id and (not ID_RE.match(category_id)
                                    or not _find_cat(cstore["categories"], category_id)):
                    return jsonify({"ok": False, "error": "目标分类不存在"}), 404
                rec["category_id"] = category_id or None
            rec["updated_at"] = _now()
            _save_store(_files_file(), fstore)
        return jsonify({"ok": True, "item": _file_brief(rec)})

    @app.delete(f"{API_PREFIX}/files/<fid>")
    def kb_file_delete(fid):
        _set_operation("删除知识库文件")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可管理知识库文件"}), 403
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
            if not rec:
                return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
            # 清理四件套：原件 + 渲染件 + 展示用 PDF + 表格预览 JSON（按绝对路径去重）
            seen, paths = set(), []
            for e in (rec.get("original_ext") or "", rec.get("ext") or "", "pdf"):
                p = _file_path(fid, e)
                if not p:
                    continue
                ap = os.path.abspath(p)
                if ap in seen:
                    continue
                seen.add(ap)
                paths.append(p)
            hp = _html_path(fid)
            if hp:
                ap = os.path.abspath(hp)
                if ap not in seen:
                    paths.append(hp)
            for p in paths:
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        return jsonify({"ok": False, "error": "删除失败，请稍后重试"}), 500
            fstore["files"] = [f for f in fstore["files"] if f.get("id") != fid]
            _save_store(_files_file(), fstore)
        return jsonify({"ok": True})

    @app.get(f"{API_PREFIX}/files/<fid>/raw")
    def kb_file_raw(fid):
        """内联返回文件内容（供前端渲染器 fetch 字节流）。

        - 不设置 attachment 头 → 浏览器不触发"另存为"，仅内联读取（只读定位）；
        - conditional=True → 支持 Range / ETag / 304（pdf.js 断点续读）；
        - ID 白名单正则 + 扩展名二次校验（SEC-1），不可读一律 404（不暴露存在性）。
        """
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
        if not rec:
            return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
        path = _file_path(fid, rec.get("ext", ""))
        if not path or not os.path.isfile(path):
            return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
        resp = send_file(path, mimetype=EXT_MIME.get(rec.get("ext", ""), "application/octet-stream"),
                         conditional=True)
        resp.headers["Content-Disposition"] = f'inline; filename="{fid}.{rec.get("ext", "")}"'
        return resp

    @app.get(f"{API_PREFIX}/files/<fid>/download")
    def kb_file_download(fid):
        """附件形式下载**原件**（全体登录用户可用）。

        - Word/Excel 下载的是用户上传的原始文档（doc/docx/xls/xlsx），不是 PDF 版；
        - 其余类型（pdf/ofd/md/txt/csv）原件即唯一文件，同一端点天然适用；
        - `download_name` 交给 Flask 处理 RFC 5987 编码（中文文件名老浏览器也可用）；
        - ID 白名单正则 + 扩展名二次校验（SEC-1），缺失/不可读一律 404（SEC-5）。
        """
        _set_operation("下载知识库文件")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
        if not rec:
            return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
        ext = rec.get("original_ext") or rec.get("ext") or ""
        path = _file_path(fid, ext)
        if not path or not os.path.isfile(path):
            return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
        name = rec.get("original_name") or f"{fid}.{ext}"
        return send_file(path, mimetype=EXT_MIME.get(ext, "application/octet-stream"),
                         as_attachment=True, download_name=name, conditional=True)

    @app.get(f"{API_PREFIX}/files/<fid>/pdf")
    def kb_file_pdf(fid):
        """内联返回展示用 PDF（供前端 pdf.js 渲染，Word/Excel 的"原样式"预览）。

        - 仅 `pdf_status=="ok"` 且文件存在才返回；未就绪/生成失败/不存在一律 404
          （与"不存在"同码，不暴露内部状态差异，SEC-5）；
        - `Content-Disposition: inline` + `conditional=True` → 支持 Range/ETag/304，
          pdf.js 翻页与断点续读开箱可用；
        - **不提供 PDF 版下载**（需求：Word/Excel 下载给原始文档，PDF 仅在线展示）。
        """
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
        if not rec or rec.get("pdf_status") != "ok":
            return jsonify({"ok": False, "error": "文件不存在或 PDF 预览尚未生成"}), 404
        path = _pdf_path(fid)
        if not path or not os.path.isfile(path):
            return jsonify({"ok": False, "error": "文件不存在或 PDF 预览尚未生成"}), 404
        resp = send_file(path, mimetype="application/pdf", conditional=True)
        resp.headers["Content-Disposition"] = f'inline; filename="{fid}.pdf"'
        return resp

    @app.post(f"{API_PREFIX}/files/<fid>/pdf-retry")
    def kb_file_pdf_retry(fid):
        """管理员手动重转 PDF（仅 `failed` 记录可重试，防转换风暴）。"""
        _set_operation("重试知识库PDF转换")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可重试 PDF 转换"}), 403
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
            if not rec:
                return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
            if rec.get("pdf_status") != "failed":
                return jsonify({"ok": False, "error": "仅生成失败的文件可重试"}), 409
            if not _needs_pdf(rec):
                return jsonify({"ok": False, "error": "该文件类型无需生成 PDF 预览"}), 409
            rec["pdf_status"] = "pending"
            rec["pdf_error"] = ""
            rec["updated_at"] = _now()
            _save_store(_files_file(), fstore)
        ok = _enqueue_pdf(fid)
        return jsonify({"ok": True, "pdf_status": "pending" if ok else "failed"})

    @app.get(f"{API_PREFIX}/files/<fid>/preview")
    def kb_file_preview(fid):
        """返回 Excel 表格预览 JSON（各 sheet 的 HTML 片段，前端手绘表格页签展示）。

        - 仅 `html_status=="ok"` 且文件存在才返回；未就绪/生成失败/不存在一律 404
          （与"不存在"同码，不暴露内部状态差异，SEC-5）；
        - 内容为服务端自产 JSON（单元格文本已转义），前端注入前仍过 DOMPurify；
        - 只读预览，不提供下载（原件下载走 /download）。
        """
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
        if not rec or rec.get("html_status") != "ok":
            return jsonify({"ok": False, "error": "文件不存在或表格预览尚未生成"}), 404
        path = _html_path(fid)
        if not path or not os.path.isfile(path):
            return jsonify({"ok": False, "error": "文件不存在或表格预览尚未生成"}), 404
        resp = send_file(path, mimetype="application/json", conditional=True)
        resp.headers["Content-Disposition"] = f'inline; filename="{fid}.json"'
        return resp

    @app.post(f"{API_PREFIX}/files/<fid>/preview-retry")
    def kb_file_preview_retry(fid):
        """管理员手动重转表格预览（仅 `failed` 记录可重试，防渲染风暴）。"""
        _set_operation("重试知识库表格预览")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可重试表格预览"}), 403
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
            if not rec:
                return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
            if rec.get("html_status") != "failed":
                return jsonify({"ok": False, "error": "仅生成失败的文件可重试"}), 409
            if not _needs_html(rec):
                return jsonify({"ok": False, "error": "该文件类型无需生成表格预览"}), 409
            rec["html_status"] = "pending"
            rec["html_error"] = ""
            rec["updated_at"] = _now()
            _save_store(_files_file(), fstore)
        ok = _enqueue_html(fid)
        return jsonify({"ok": True, "html_status": "pending" if ok else "failed"})
