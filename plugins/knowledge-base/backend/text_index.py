# -*- coding: utf-8 -*-
r"""全文检索：文本抽取 + 内存倒排索引（L4-4，《知识库插件-设计文档》§11 遗留增强）。

**索引范围有意收窄**：只索引本插件 `requirements.txt` 已声明依赖能直接抽取的格式
—— txt / md / markdown / csv / docx / xlsx / xls。
不做 pdf / ofd / doc：
  - 抽 PDF 文本要新增 `pypdf`，而框架随包集合里并没有它（随包的 9 个包只有 Flask 栈 +
    cryptography / pillow / pystray / waitress），等于要求每台离线目标机多装一个「依赖组件包」；
  - 卡片口径本来就是「索引 md/txt/docx 文本」；
  - 扫描件 PDF 根本没有文本层，索引了也是空。
  取舍写在这里而不是悄悄扩大依赖面。`/search/status` 会如实报告 skipped（未索引）的文件数。

**中文分词**：不引第三方分词库（同样是为离线交付省依赖）。用「CJK 单字 + 相邻双字」+
「ASCII 词」的集合式索引，查询侧同一套切分并要求**全部命中（AND）**：
  - 双字组提精度（"通知" 不会命中只有"通"字的文档）；
  - 单字兜底，避免两字组漏掉跨边界的查询；
  - 代价是索引条目比词库法多，靠 MAX_TERMS 护栏封顶。

**并发**：本模块自带 `_IDX_LOCK`（RLock）。**禁止在持有 routes._LOCK（存储锁）时调用**——
抽取要读盘、可能耗时数十毫秒，把存储锁按住会让上传/列表一起卡住（同 D1 的锁纪律：
锁内不做盘 IO）。调用方一律"先取快照 → 放锁 → 再喂本模块"。
"""
import json
import os
import re
import threading

# 索引格式版本：分词规则或存储结构变化时递增 → 旧索引整体作废重建
INDEX_VERSION = 1

# 护栏（离线机器上宁可少索引，也不能把内存/磁盘吃满）
MAX_FILE_BYTES = 8 * 1024 * 1024      # 单文件超过即跳过（不抽不索引）
MAX_DOC_CHARS = 400_000               # 单文件最多取这么多字符
MAX_TERMS = 300_000                   # 索引词表上限，超出后停止收录新词并标 truncated

INDEXABLE_EXTS = ("txt", "md", "markdown", "csv", "docx", "xlsx", "xls")

_IDX_LOCK = threading.RLock()

_CJK_RUN = re.compile(r"[一-鿿㐀-䶿]+")   # CJK 基本区 + 扩展 A
_ASCII_WORD = re.compile(r"[a-z0-9_]+")


# ===================== 文本抽取 =====================

def _read_text(path):
    with open(path, "rb") as f:
        raw = f.read(MAX_DOC_CHARS + 1)
    # 依次试：UTF-8 → GBK（政务文档里 GBK/GB18030 极常见）→ 容错 UTF-8
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _extract_docx(path):
    import docx                                  # python-docx（已声明依赖）
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs]
    for tb in d.tables:                          # 表格正文是政务文档的大头，不能只读段落
        for row in tb.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(p for p in parts if p)


def _extract_xlsx(path):
    import openpyxl                              # 已声明依赖
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            out.append(ws.title or "")
            for row in ws.iter_rows(values_only=True):
                for v in row:
                    if v is not None:
                        out.append(str(v))
                    if sum(len(x) for x in out) > MAX_DOC_CHARS:
                        return "\n".join(out)     # 提前收口，超大表不吃满
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return "\n".join(out)


def _extract_xls(path):
    import xlrd                                  # 已声明依赖
    book = xlrd.open_workbook(path)
    out = []
    for sh in book.sheets():
        out.append(sh.name or "")
        for r in range(sh.nrows):
            for c in range(sh.ncols):
                try:
                    out.append(str(sh.cell_value(r, c)))
                except Exception:
                    continue
            if sum(len(x) for x in out) > MAX_DOC_CHARS:
                break
    return "\n".join(out)


def extract_text(path, ext):
    """按扩展名抽取纯文本；任何失败都返回 None（该文件记为未索引，绝不让搜索整体失败）。"""
    try:
        if not path or not os.path.isfile(path):
            return None
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        if ext in ("txt", "md", "markdown", "csv"):
            return _read_text(path)
        if ext == "docx":
            return _extract_docx(path)
        if ext in ("xlsx", "xlsm"):
            return _extract_xlsx(path)
        if ext == "xls":
            return _extract_xls(path)
        return None
    except Exception:
        return None


# ===================== 分词 =====================

def tokenize(text):
    """集合式切分：ASCII 词 + CJK 单字 + CJK 相邻双字。返回 set。"""
    toks = set()
    if not text:
        return toks
    low = text.lower()
    for w in _ASCII_WORD.findall(low):
        toks.add(w)
    for run in _CJK_RUN.findall(low):
        n = len(run)
        for i in range(n):
            toks.add(run[i])
            if i + 1 < n:
                toks.add(run[i:i + 2])
    return toks


# ===================== 索引 =====================

class SearchIndex:
    """倒排索引 + 磁盘缓存（按内容指纹增量重建）。

    存储结构：{"version", "terms": {term: [fid...]}, "docs": {fid: {"fp","chars","skipped"}}}
    `docs[fid].skipped` 为真表示"该格式不支持/太大/抽取失败"，与"还没轮到它建索引"区分开。
    """

    def __init__(self, path):
        self.path = path
        self.terms = {}
        self.docs = {}
        self.truncated = False
        self.loaded = False

    # ---- 持久化 ----
    def load(self):
        with _IDX_LOCK:
            if self.loaded:
                return
            self.loaded = True
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:
                return                              # 无缓存/损坏 → 空索引，下次 ensure 全量建
            if not isinstance(rec, dict) or rec.get("version") != INDEX_VERSION:
                return                              # 格式换代：作废旧索引
            terms = rec.get("terms")
            docs = rec.get("docs")
            if isinstance(terms, dict) and isinstance(docs, dict):
                self.terms = {k: set(v) for k, v in terms.items() if isinstance(v, list)}
                self.docs = {k: v for k, v in docs.items() if isinstance(v, dict)}

    def save(self):
        with _IDX_LOCK:
            rec = {
                "version": INDEX_VERSION,
                "terms": {k: sorted(v) for k, v in self.terms.items()},
                "docs": self.docs,
            }
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.remove(tmp)
                except Exception:
                    pass                            # 缓存写失败不影响内存索引可用

    # ---- 维护 ----
    def fingerprint(self, path):
        """文档"变没变"的判据：**stat 签名**（大小 + mtime），不是内容哈希。

        为什么不做内容哈希：ensure() 每次查询都要对全库跑一遍判据，sha256 等于把每个文件
        完整读一次——100 篇 5MB 文档就是每次搜索读 500MB。本插件的落盘文件只在上传时写入
        （改名/移动只动元数据，重新上传是新 id），mtime 必然随之变化，所以 stat 判据足够。
        """
        try:
            st = os.stat(path)
            return "%d:%d" % (st.st_size, int(st.st_mtime))
        except Exception:
            return ""

    def ensure(self, docs):
        """按 (id, ext, path) 补齐索引。docs: [{"id","ext","path"}]。返回统计 dict。

        只对"指纹变了或没见过"的文档做抽取，其余直接跳过——这是搜索首屏不卡的关键。
        """
        self.load()
        added = updated = skipped = 0
        with _IDX_LOCK:
            live = set()
            for d in docs:
                fid = d.get("id")
                path = d.get("path")
                if not fid or not path:
                    continue
                live.add(fid)
                fp = self.fingerprint(path)
                old = self.docs.get(fid)
                if old and old.get("fp") == fp:
                    continue
                if old:
                    self._drop(fid)
                    updated += 1
                else:
                    added += 1
                text = extract_text(path, d.get("ext") or "")
                if text is None:
                    self.docs[fid] = {"fp": fp, "chars": 0, "skipped": True}
                    skipped += 1
                    continue
                if len(text) > MAX_DOC_CHARS:
                    text = text[:MAX_DOC_CHARS]
                toks = tokenize(text)
                for t in toks:
                    if len(self.terms) >= MAX_TERMS and t not in self.terms:
                        self.truncated = True
                        continue
                    self.terms.setdefault(t, set()).add(fid)
                self.docs[fid] = {"fp": fp, "chars": len(text), "skipped": False}
            stale = [fid for fid in self.docs if fid not in live]
            for fid in stale:
                self._drop(fid)
            changed = bool(added or updated or stale)
        if changed:
            self.save()
        return {"added": added, "updated": updated, "skipped": skipped,
                "dropped": len(stale), "changed": changed}

    def _drop(self, fid):
        self.docs.pop(fid, None)
        for t in list(self.terms.keys()):
            s = self.terms.get(t)
            if s and fid in s:
                s.discard(fid)
                if not s:
                    del self.terms[t]

    def forget(self, fid):
        with _IDX_LOCK:
            if fid not in self.docs:
                return False
            self._drop(fid)
        self.save()
        return True

    # ---- 查询 ----
    def search(self, q, limit=500):
        """返回按"命中词数"降序、再按 id 稳定排序的 fid 列表。"""
        self.load()          # 必须先 load：否则进程重启后、首次 ensure() 之前一律查不到
        toks = tokenize(q)
        if not toks:
            return []
        with _IDX_LOCK:
            hits = {}
            for t in toks:
                for fid in self.terms.get(t, ()):
                    hits[fid] = hits.get(fid, 0) + 1
            # 查询词全部命中才算结果（AND）：CJK 双字组已保证精度，OR 会淹掉噪声
            want = len(toks)
            out = [(c, fid) for fid, c in hits.items() if c >= want]
        out.sort(key=lambda x: (-x[0], x[1]))
        return [fid for _c, fid in out[:limit]]

    def status(self):
        self.load()
        with _IDX_LOCK:
            indexed = sum(1 for v in self.docs.values() if not v.get("skipped"))
            skipped = sum(1 for v in self.docs.values() if v.get("skipped"))
            return {"version": INDEX_VERSION, "indexed": indexed, "skipped": skipped,
                    "terms": len(self.terms), "truncated": self.truncated,
                    "max_terms": MAX_TERMS, "max_file_bytes": MAX_FILE_BYTES,
                    "exts": list(INDEXABLE_EXTS)}

    def snippet(self, path, ext, q, width=90):
        """命中片段：重新抽一次文本取窗口（只对结果页的少数文件做，不预存全文）。"""
        text = extract_text(path, ext)
        if not text:
            return ""
        low = text.lower()
        needle = (q or "").strip().lower()
        pos = low.find(needle) if needle else -1
        if pos < 0:
            for t in sorted(tokenize(q), key=len, reverse=True):
                pos = low.find(t)
                if pos >= 0:
                    break
        if pos < 0:
            return text[:width].replace("\n", " ").strip()
        start = max(0, pos - width // 2)
        frag = text[start:start + width].replace("\n", " ").strip()
        return ("…" if start > 0 else "") + frag + ("…" if start + width < len(text) else "")
