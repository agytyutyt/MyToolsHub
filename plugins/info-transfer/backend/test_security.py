# -*- coding: utf-8 -*-
"""信息传输 —— 批次 8 安全加固（T26–T30）专项测试。

覆盖（每条均为「负向用例」优先，附必要的正例对照）：
  T26 ZIP 解析资源上限：条目数 / 单条声明尺寸 / 解压总量 / 压缩比四道闸门，
      解压炸弹在读取任何条目之前被 400 拒绝，且进程峰值内存 <100MB；
      合法 ZIP（本插件自产静态码多页包）照常解析（零误杀）。
  T27 上传前置校验：Content-Length 预判（明显超限）与流式限量读取（谎报/未声明
      Content-Length 的 20MB+1 形态）两条路径均提前 400，不再先整读再判。
  T28 文档解析 XXE 防护：含 DTD/外部实体的 docx / xlsx 被拒绝精简解析并回退原件
      （/encode 任务落 note），正常 docx/xlsx 零影响。
  T29 PDF 解密与提取 CPU 护栏：加密 PDF 体积/页数门槛（超限不做解密尝试）、
      非加密 PDF 页数上限与提取耗时预算；正常文本 PDF 照常提取。
  T30 响应头与落盘治理：框架级安全响应头（nosniff / SAMEORIGIN / Referrer-Policy /
      保守 CSP）、MAX_CONTENT_LENGTH 兜底 413；任务临时目录容量配额按 mtime 回收。

运行：python test_security.py（unittest 风格；失败非零退出）
依赖：flask（必需）；qrcode / zxing-cpp / opencv-python / numpy（T26 正例）；
      python-docx / openpyxl（T28）、pypdf（T29）——缺依赖的用例自动 skip。
说明：test_*.py 在插件包排除清单内，不随包分发。
"""
import io
import os
import shutil
import struct
import sys
import tempfile
import time
import tracemalloc
import unittest
import warnings
import zipfile
from unittest import mock

# pypdf 对超大流使用内部临时文件（由 GC 回收），ResourceWarning 与用例无关
warnings.filterwarnings("ignore", category=ResourceWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, REPO)

TMP_ROOT = tempfile.mkdtemp(prefix="it_sec_")
os.environ["JZTOOLS_DATA_ROOT"] = TMP_ROOT  # 隔离真实数据根（routes 导入前设置）

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "it_routes_sec", os.path.join(HERE, "routes.py"))
it = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(it)

import jz_api  # noqa: E402

# D5（任务归属校验统一收紧）之后，无主任务仅超管可见 —— 全套件以一个固定登录身份跑，
# 使任务 created_by 与轮询 `/task/<id>` 的身份同口径（等价于真实部署下的已登录会话）。
TEST_USER = {"username": "it-sec-tester", "super_admin": False}
jz_api.register_providers(get_session_user=lambda: dict(TEST_USER))

from flask import Flask  # noqa: E402

API = "/api/info-transfer"


def make_app():
    app = Flask("it_sec_test")
    app.config["TESTING"] = True
    it.register(app)
    return app


CLIENT = make_app().test_client()


def post_file(path, blob, filename, extra=None):
    """multipart 上传（file=blob，filename 决定扩展名/白名单）。"""
    data = {"file": (io.BytesIO(blob), filename)}
    if extra:
        data.update(extra)
    return CLIENT.post(path, data=data, content_type="multipart/form-data")


def wait_task(client, task_id, timeout=180):
    """轮询 /task/<id> 到终态，返回最终 JSON。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get(f"{API}/task/{task_id}")
        j = r.get_json() or {}
        if j.get("status") in ("done", "error", "canceled"):
            return j
        time.sleep(0.1)
    raise AssertionError(f"任务 {task_id} 超时未完成")


def build_zero_bomb(entries=200, size_mb=5):
    """解压炸弹：entries 条、每条声明解压 size_mb（全零，压缩比 ~1000×）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(entries):
            z.writestr("page_%03d.png" % i, b"\0" * (size_mb * 1024 * 1024))
    return buf.getvalue()


# ===================== T26 · ZIP 解析资源上限 =====================

class TestZipResourceLimits(unittest.TestCase):
    """T26：解压炸弹在读取任何条目之前被拒；合法 ZIP 零误杀。"""

    @classmethod
    def setUpClass(cls):
        # 1MB 压缩包 → 声明解压 1000MB（对齐 0917 方案 S1 的实测样本形态）
        cls.bomb = build_zero_bomb()
        cls.assert_bomb_shape = (len(cls.bomb) < 2 * 1024 * 1024)

    def _decode_zip(self, blob):
        return post_file(f"{API}/decode", blob, "pages.zip", {"type": "zip"})

    def test_bomb_is_compressed_within_upload_limit(self):
        """前提：炸弹本身 <2MB（合法通过上传上限，只能靠解压闸门拦）。"""
        self.assertTrue(self.assert_bomb_shape,
                        "炸弹样本应 <2MB，否则不是有效的 T26 用例")

    def test_ratio_gate_rejects_bomb(self):
        """压缩比闸门：全零条目 file/compress ≈1000× > 上限 200×。"""
        r = self._decode_zip(self.bomb)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertIn("压缩比", r.get_json()["detail"])

    def test_total_bytes_gate_rejects_bomb(self):
        """解压总量闸门：声明总量 1000MB > 上限 128MB（关掉其余闸门以定位）。"""
        with mock.patch.object(it, "MAX_ZIP_RATIO", 10 ** 9), \
             mock.patch.object(it, "MAX_ZIP_ENTRIES", 10 ** 9):
            r = self._decode_zip(self.bomb)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertIn("解压后总量过大", r.get_json()["detail"])

    def test_entry_count_gate(self):
        """条目数闸门：MAX_ZIP_ENTRIES+1 个条目 → 400。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for i in range(it.MAX_ZIP_ENTRIES + 1):
                z.writestr("e%04d.png" % i, b"x" * 64)
        r = self._decode_zip(buf.getvalue())
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertIn("条目过多", r.get_json()["detail"])

    def test_single_entry_size_gate(self):
        """单条声明解压尺寸闸门：17MB > 上限 16MB → 400（先于压缩比判定）。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("huge.png", b"\0" * (17 * 1024 * 1024))
        r = self._decode_zip(buf.getvalue())
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertIn("超大条目", r.get_json()["detail"])

    def test_bomb_not_inflated_in_memory(self):
        """验收口径：炸弹请求下进程峰值 <100MB（不炸内存即被拒）。"""
        tracemalloc.start()
        try:
            r = self._decode_zip(self.bomb)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(r.status_code, 400)
        self.assertLess(peak, 100 * 1024 * 1024,
                        "峰值内存 %.1fMB 超 100MB（疑似触发了实际解压）"
                        % (peak / 1048576.0))

    def test_lying_declared_size_is_bounded(self):
        """声明尺寸说谎（改小 central directory 的 uncompressed size）：限量读取
        不炸内存、按坏页跳过，整体以 400 收口（不 500）。"""
        one = io.BytesIO()
        with zipfile.ZipFile(one, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("a.png", b"\0" * (5 * 1024 * 1024))
        raw = bytearray(one.getvalue())
        off = raw.find(b"PK\x01\x02")
        self.assertGreater(off, 0)
        struct.pack_into("<I", raw, off + 24, 1024)   # 声明解压尺寸改小为 1KB
        r = self._decode_zip(bytes(raw))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        detail = r.get_json()["detail"]
        self.assertTrue("识别失败" in detail or "未找到" in detail, detail)

    @unittest.skipUnless(it.QRCODE_AVAILABLE and it.ZXING_AVAILABLE,
                         "需要 qrcode / zxing-cpp 组件包（静态码正例）")
    def test_legit_zip_still_decodes(self):
        """正例对照：本插件自产的静态码 ZIP 不受闸门影响。"""
        payload = "安全加固批次8 正例对照 hello"
        env = it.build_envelope("text", "安全测试", payload, None)
        out_dir = tempfile.mkdtemp(prefix="it_sec_qr_", dir=TMP_ROOT)
        try:
            paths, pages, _v = it.encode_static_output("text", "安全测试", env, 10,
                                                  out_dir, "sec")
        except RuntimeError as e:            # cv2 等运行期缺依赖
            self.skipTest(str(e))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in paths:
                z.write(p, os.path.basename(p))
        r = self._decode_zip(buf.getvalue())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        self.assertEqual(r.get_json()["payload"]["data"], payload)
        self.assertEqual(r.get_json()["pages"], pages)


# ===================== T27 · 上传前置校验 =====================

class TestUploadPrechecks(unittest.TestCase):
    """T27：Content-Length 预判 + 流式限量读取，两条路径都提前拒绝。"""

    def test_encode_rejects_oversize_by_content_length(self):
        """明显超限（22MB）：content_length > limit+1MB → 读取前 400。"""
        blob = b"x" * (22 * 1024 * 1024)
        r = post_file(f"{API}/encode", blob, "big.txt", {"mode": "static"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertIn("文件过大", r.get_json()["detail"])

    def test_decode_rejects_oversize_by_content_length(self):
        blob = b"x" * (22 * 1024 * 1024)
        r = post_file(f"{API}/decode", blob, "big.zip", {"type": "zip"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertIn("文件过大", r.get_json()["detail"])

    def test_encode_rejects_oversize_by_stream_count(self):
        """临界形态（20MB+1，content_length 仍在 1MB 容差内）：
        走流式计数路径拒绝——旧实现会先把 20MB+1 整读入内存再判。"""
        blob = b"y" * (it.MAX_UPLOAD_BYTES + 1)
        r = post_file(f"{API}/encode", blob, "edge.txt", {"mode": "static"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertIn("文件过大", r.get_json()["detail"])

    def test_decode_rejects_oversize_by_stream_count(self):
        blob = b"y" * (it.MAX_UPLOAD_BYTES + 1)
        r = post_file(f"{API}/decode", blob, "edge.png", {"type": "image"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertIn("文件过大", r.get_json()["detail"])

    def test_content_length_precheck_does_not_read_body(self):
        """预判路径：Content-Length 超 limit+1MB 时读取前即 400（构造原始
        multipart 请求体验证声明值判定，不依赖测试客户端的分块行为）。"""
        raw = b"A" * (21 * 1024 * 1024 + 1024)   # > limit + 1MB slack
        boundary = b"----itsec"
        body = (b"--" + boundary + b"\r\n"
                b'Content-Disposition: form-data; name="file"; filename="big.txt"\r\n'
                b"Content-Type: text/plain\r\n\r\n" + raw + b"\r\n"
                b"--" + boundary + b"--\r\n")
        r = CLIENT.post(f"{API}/encode", data=io.BytesIO(body),
                        content_type=("multipart/form-data; boundary="
                                      + boundary.decode()),
                        content_length=len(body))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertIn("文件过大", r.get_json()["detail"])

    def test_small_upload_still_accepted(self):
        """正例对照：正常小文件照常进入任务队列。"""
        r = post_file(f"{API}/encode", "批次8 正常上传".encode("utf-8"),
                      "ok.txt", {"mode": "static"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        self.assertTrue(r.get_json().get("task_id"))

    def test_empty_file_rejected(self):
        r = post_file(f"{API}/decode", b"", "empty.png", {"type": "image"})
        self.assertEqual(r.status_code, 400)


# ===================== T28 · 文档解析 XXE 防护 =====================

DTD_PREFIX = (b'<!DOCTYPE %s [\n'
              b'  <!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini">\n'
              b'  <!ENTITY lol "lol">\n'
              b'  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
              b']>')


def _inject_xxe(part_bytes, root_prefix, root_name):
    """在部件 XML 的根元素前插入含外部实体的 DTD，并在根元素内容处引用它。"""
    dtd = DTD_PREFIX % root_name.encode("ascii")
    idx = part_bytes.find(root_prefix)
    if idx < 0:
        raise AssertionError("测试构造失败：未找到根元素 %r" % root_prefix)
    end = part_bytes.find(b">", idx)
    return (part_bytes[:idx] + dtd + part_bytes[idx:end + 1]
            + b"&xxe;" + part_bytes[end + 1:])


def _repack(blob, patch):
    """按 {部件名: 变换函数} 重写 OOXML 容器（deflate 重压，模拟真实传输形态）。

    patch 目标部件缺失时直接报错——否则「注入未生效」会让用例静默空转。
    """
    src = zipfile.ZipFile(io.BytesIO(blob))
    names = set(src.namelist())
    missing = set(patch) - names
    if missing:
        src.close()
        raise AssertionError("测试构造失败：OOXML 容器缺少部件 %s" % sorted(missing))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            data = src.read(info)
            if info.filename in patch:
                data = patch[info.filename](data)
            z.writestr(info, data)
    src.close()
    return out.getvalue()


def _docx_xxe_patcher(part):
    """docx 指定部件的 XXE 注入器（按部件根元素名配对）。"""
    roots = {"word/document.xml": (b"<w:document", "w:document"),
             "word/styles.xml": (b"<w:styles", "w:styles")}
    root_prefix, root_name = roots[part]
    return lambda b: _inject_xxe(b, root_prefix, root_name)


def make_docx_with_xxe(part="word/document.xml"):
    import docx as docx_mod
    d = docx_mod.Document()
    d.add_paragraph("正常段落内容")
    bio = io.BytesIO()
    d.save(bio)
    return _repack(bio.getvalue(), {part: _docx_xxe_patcher(part)})


def make_xlsx_with_xxe(part="xl/workbook.xml"):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active["A1"] = "正常单元格"
    bio = io.BytesIO()
    wb.save(bio)
    return _repack(bio.getvalue(),
                   {part: lambda b: _inject_xxe(b, b"<workbook", "workbook")})


class TestOoxmlXxe(unittest.TestCase):
    """T28：含 DTD/实体的 OOXML 被拒；正常文档零影响。"""

    def test_docx_with_dtd_rejected(self):
        blob = make_docx_with_xxe()
        # 前提：载荷 DTD 处于 deflate 压缩后的部件内（容器字节级扫描扫不到，
        # 证明「按部件解压扫头部」是必要的）
        self.assertNotIn(b"<!DOCTYPE", blob)
        with self.assertRaises(it.XxeSuspected) as ctx:
            it._extract_docx_text(blob)
        self.assertIn("DTD", str(ctx.exception))
        self.assertIsInstance(ctx.exception, it.LeanUnsupported)  # 回退原件机制复用

    def test_docx_dtd_in_non_parsed_part_still_rejected(self):
        """DTD 藏在 styles.xml（提取正文不会解析的部件）也必须被拦（全包含扫描）。"""
        blob = make_docx_with_xxe(part="word/styles.xml")
        with self.assertRaises(it.XxeSuspected):
            it._extract_docx_text(blob)

    def test_xlsx_with_dtd_rejected(self):
        blob = make_xlsx_with_xxe()
        self.assertNotIn(b"<!DOCTYPE", blob)
        with self.assertRaises(it.XxeSuspected) as ctx:
            it._extract_xlsx_rows(blob)
        self.assertIn("DTD", str(ctx.exception))

    def test_billion_laughs_entity_rejected(self):
        """内部实体膨胀（billion laughs，DTD_PREFIX 的 lol2 链）同样被拦。"""
        blob = make_docx_with_xxe()
        with self.assertRaises(it.XxeSuspected):
            it._extract_docx_text(blob)

    def test_entry_count_gate_for_pathological_package(self):
        """病态部件数（>MAX_XXE_PARTS）：拒绝精简解析而非逐部件扫描拖死。"""
        blob = make_docx_with_xxe()
        with mock.patch.object(it, "MAX_XXE_PARTS", 2):
            with self.assertRaises(it.XxeSuspected) as ctx:
                it._extract_docx_text(blob)
        self.assertIn("部件数", str(ctx.exception))

    def test_upstream_parsers_disable_entity_resolution(self):
        """第二层（配置层）：本仓库 python-docx / openpyxl 的 lxml 解析器均按
        resolve_entities=False 构造（行为式断言；下游依赖升级若改默认值，
        本用例会失败并提醒收回该假设）。"""
        from lxml import etree
        marker = "EXPANDED_MARKER"
        xml = ('<!DOCTYPE r [<!ENTITY xxe "%s">]><r>&xxe;</r>' % marker).encode()
        try:
            from docx.oxml.parser import parse_xml
        except Exception:
            self.skipTest("缺 python-docx / lxml")
        el = parse_xml(xml)
        self.assertNotIn(marker, etree.tostring(el).decode(),
                         "python-docx 解析器展开了实体（上游默认值已变）")
        try:
            import openpyxl
            if openpyxl.LXML:
                from openpyxl.xml.functions import fromstring
                el2 = fromstring(xml)
                self.assertNotIn(marker, etree.tostring(el2).decode(),
                                 "openpyxl 解析器展开了实体（上游默认值已变）")
        except ImportError:
            pass    # openpyxl 走 stdlib ElementTree 分支（不解析外部实体）

    def test_clean_docx_and_xlsx_unaffected(self):
        """正例对照：正常 docx/xlsx 提取结果不变（零误杀）。"""
        try:
            import docx as docx_mod
            import openpyxl
        except Exception:
            self.skipTest("缺 python-docx / openpyxl")
        d = docx_mod.Document()
        d.add_paragraph("正常段落内容")
        bio = io.BytesIO()
        d.save(bio)
        self.assertIn("正常段落内容", it._extract_docx_text(bio.getvalue()))
        wb = openpyxl.Workbook()
        wb.active["A1"] = "正常单元格"
        wb.active["A2"] = 42
        bio2 = io.BytesIO()
        wb.save(bio2)
        rows = it._extract_xlsx_rows(bio2.getvalue())
        self.assertEqual(rows[0], ["正常单元格"])
        self.assertEqual(rows[1], [42])

    def test_scan_tolerates_non_ooxml_bytes(self):
        """非 ZIP 容器（损坏文件）不在扫描层拦截，交由原解析器按既有逻辑处理。"""
        it._scan_ooxml_xxe(b"not a zip at all")   # 不抛异常
        with self.assertRaises(it.LeanUnsupported):
            it._extract_docx_text(b"not a zip at all")

    @unittest.skipUnless(it.QRCODE_AVAILABLE, "需要 qrcode 组件包")
    def test_encode_task_falls_back_to_raw_with_note(self):
        """集成：含 XXE 载荷的 docx 走 /encode → 任务降级原件传输（fmt=file）并给文案。"""
        blob = make_docx_with_xxe()
        r = post_file(f"{API}/encode", blob, "xxe.docx", {"mode": "static"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        task = wait_task(CLIENT, r.get_json()["task_id"])
        self.assertEqual(task.get("status"), "done", task)
        self.assertEqual(task.get("fmt"), "file")           # 降级为原件传输
        self.assertIn("DTD", task.get("note") or "")
        self.assertIn("已按原件传输", task.get("note") or "")


# ===================== T29 · PDF 解密与提取护栏 =====================

def build_min_pdf(text=b"Hello JZ", dummy_stream_mb=0):
    """手写最小 PDF（含正确 xref）：1 页含文本；dummy_stream_mb 把页面内容流
    放大到指定 MB（内容流被页面引用，pypdf clone/encrypt 会原样保留）。"""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    content = b"BT /F1 14 Tf 20 100 Td (" + text + b") Tj ET"
    if dummy_stream_mb:
        content += b"\n%" + b"A" * (dummy_stream_mb * 1024 * 1024)
    objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)


def encrypt_pdf(blob, user_password="secret", owner_password="owner"):
    import pypdf
    w = pypdf.PdfWriter(clone_from=io.BytesIO(blob))
    w.encrypt(user_password=user_password, owner_password=owner_password)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def blank_encrypted_pdf(pages, user_password="secret"):
    import pypdf
    w = pypdf.PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    w.encrypt(user_password=user_password, owner_password="owner")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class TestPdfGuards(unittest.TestCase):
    """T29：加密 PDF 体积/页数门槛、提取页数上限与耗时预算。"""

    @classmethod
    def setUpClass(cls):
        try:
            import pypdf  # noqa: F401
        except Exception:
            raise unittest.SkipTest("需要 pypdf 组件包")
        cls.plain = build_min_pdf()
        cls.big_encrypted = encrypt_pdf(build_min_pdf(dummy_stream_mb=6))

    def test_big_encrypted_pdf_rejected_by_size_gate(self):
        """加密且 >5MB：不做解密尝试，直接回退原件（明确文案）。"""
        self.assertGreater(len(self.big_encrypted), it.MAX_PDF_ENCRYPTED_BYTES)
        with self.assertRaises(it.LeanUnsupported) as ctx:
            it._extract_pdf_text(self.big_encrypted)
        msg = str(ctx.exception)
        self.assertIn("门槛", msg)
        self.assertIn("已加密", msg)

    def test_many_pages_encrypted_pdf_rejected_by_page_gate(self):
        """加密且 >50 页：拒绝解密尝试（页数由 /Type /Page 明文启发式得出）。"""
        blob = blank_encrypted_pdf(it.MAX_PDF_ENCRYPTED_PAGES + 1)
        hint = it._pdf_page_hint(blob)
        self.assertGreater(hint, it.MAX_PDF_ENCRYPTED_PAGES,
                           "页数启发式应数出 %d 页，实际 %d" %
                           (it.MAX_PDF_ENCRYPTED_PAGES + 1, hint))
        with self.assertRaises(it.LeanUnsupported) as ctx:
            it._extract_pdf_text(blob)
        self.assertIn("门槛", str(ctx.exception))

    def test_small_encrypted_pdf_still_attempts_decrypt(self):
        """正例对照：加密但小且页少 → 过门槛、照常尝试空密码解密（失败才回退）。"""
        blob = blank_encrypted_pdf(2)
        self.assertLess(len(blob), it.MAX_PDF_ENCRYPTED_BYTES)
        with self.assertRaises(it.LeanUnsupported) as ctx:
            it._extract_pdf_text(blob)
        msg = str(ctx.exception)
        self.assertIn("已加密", msg)
        self.assertNotIn("门槛", msg)          # 未命中 T29 门槛

    def test_empty_password_encrypted_pdf_passes_gate(self):
        """非空门槛内的空密码加密 PDF：解密成功 → 落到「无文本」分支而非门槛分支。"""
        blob = blank_encrypted_pdf(1, user_password="")
        with self.assertRaises(it.LeanUnsupported) as ctx:
            it._extract_pdf_text(blob)
        self.assertIn("无可提取文本", str(ctx.exception))

    def test_plain_pdf_still_extracts(self):
        """正例对照：正常文本 PDF 提取结果不变。"""
        self.assertEqual(it._extract_pdf_text(self.plain), "Hello JZ")

    def test_plain_pdf_page_count_gate(self):
        """非加密 PDF 页数上限：301 页 > 300 → 拒绝（超长提取不占死工作线程）。"""
        import pypdf
        w = pypdf.PdfWriter()
        for _ in range(it.MAX_PDF_PAGES + 1):
            w.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        w.write(buf)
        with self.assertRaises(it.LeanUnsupported) as ctx:
            it._extract_pdf_text(buf.getvalue())
        self.assertIn("页数过多", str(ctx.exception))

    def test_extract_time_budget(self):
        """耗时预算：预算置 0 时提取立即回退（不产出半截文本）。"""
        with mock.patch.object(it, "PDF_EXTRACT_SECONDS", 0.0):
            with self.assertRaises(it.LeanUnsupported) as ctx:
                it._extract_pdf_text(self.plain)
        self.assertIn("超时", str(ctx.exception))

    def test_progress_callback_reports_pages(self):
        """耗时可见（T29 第 2 点）：progress_cb 收到 (done, total, elapsed)。"""
        seen = []
        text = it._extract_pdf_text(self.plain,
                                    progress_cb=lambda d, t, e: seen.append((d, t, e)))
        self.assertEqual(text, "Hello JZ")
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], seen[-1][1])     # done == total
        self.assertGreaterEqual(seen[-1][2], 0.0)


# ===================== T30 · 响应头与落盘治理 =====================

class TestFrameworkSecurity(unittest.TestCase):
    """T30：框架级安全响应头与 MAX_CONTENT_LENGTH 兜底（导入主程序 app）。"""

    @classmethod
    def setUpClass(cls):
        try:
            import app as hub_app
        except Exception as e:          # 主程序导入失败（缺依赖等）→ skip 不误报
            raise unittest.SkipTest(f"主程序 app.py 导入失败：{type(e).__name__}: {e}")
        cls.hub = hub_app
        # Flask 3 首个请求后禁止再注册路由 → 探针在 setUpClass 注册（早于任何请求）。
        # 探针只用于让 Werkzeug 真正读取请求体，以验证 MAX_CONTENT_LENGTH 生效。
        def _probe():
            from flask import request as _rq
            return _rq.get_data() and "ok"
        hub_app.app.add_url_rule("/__t30_probe", "__t30_probe", _probe,
                                 methods=["POST"], provide_automatic_options=False)

    def test_security_headers_present(self):
        r = self.hub.app.test_client().get("/api/tools")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        # 插件页被同源 iframe 嵌入 → 必须 SAMEORIGIN，不能 DENY
        self.assertEqual(r.headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertEqual(r.headers.get("Referrer-Policy"),
                         "strict-origin-when-cross-origin")
        csp = r.headers.get("Content-Security-Policy") or ""
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertNotIn("script-src", csp)   # 保守处理：不收紧脚本源（防白屏）

    def test_headers_on_error_responses(self):
        r = self.hub.app.test_client().get("/api/definitely-not-exists")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("X-Frame-Options"), "SAMEORIGIN")

    def test_max_content_length_backstop(self):
        """MAX_CONTENT_LENGTH 兜底：超限请求体在视图读取前被 413 拦截。"""
        self.assertEqual(self.hub.app.config.get("MAX_CONTENT_LENGTH"), 640 * 1024 * 1024)
        client = self.hub.app.test_client()
        with mock.patch.dict(self.hub.app.config, {"MAX_CONTENT_LENGTH": 1024}):
            r = client.post("/__t30_probe", data=b"x" * 4096,
                            content_type="application/octet-stream")
        self.assertEqual(r.status_code, 413)


class TestTaskDirQuota(unittest.TestCase):
    """T30：任务临时目录容量配额（按 mtime 从旧到新回收 + 活跃产物豁免）。"""

    def setUp(self):
        self.dir = os.path.join(TMP_ROOT, "quota_dir")
        os.makedirs(self.dir, exist_ok=True)
        for name in os.listdir(self.dir):
            p = os.path.join(self.dir, name)
            if os.path.isdir(p):
                import shutil
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        self._orig_dir = it._TASK_DIR
        it._TASK_DIR = self.dir
        self.addCleanup(lambda: setattr(it, "_TASK_DIR", self._orig_dir))

    @staticmethod
    def _write(path, size, mtime):
        with open(path, "wb") as f:
            f.write(b"z" * size)
        os.utime(path, (mtime, mtime))

    def test_quota_recycles_oldest_first(self):
        now = time.time()
        old = os.path.join(self.dir, "old_task.bin")
        new = os.path.join(self.dir, "new_task.bin")
        self._write(old, 4096, now - 3600)
        self._write(new, 4096, now - 10)
        with mock.patch.object(it, "TASK_DIR_MAX_BYTES", 4096), \
             mock.patch.object(it, "TASK_DIR_QUOTA_MIN_AGE", 0):
            it._enforce_task_dir_quota()
        self.assertFalse(os.path.exists(old), "最旧条目应先被回收")
        self.assertTrue(os.path.exists(new), "回收至配额以下即停，不应多删")

    def test_quota_spares_active_task_products(self):
        """豁免窗口内的产物（活跃任务的帧/视频）不被回收。"""
        now = time.time()
        fresh = os.path.join(self.dir, "active_frames.bin")
        self._write(fresh, 8192, now)
        with mock.patch.object(it, "TASK_DIR_MAX_BYTES", 1024), \
             mock.patch.object(it, "TASK_DIR_QUOTA_MIN_AGE", 60):
            it._enforce_task_dir_quota()
        self.assertTrue(os.path.exists(fresh))

    def test_dir_usage_counts_directory_tree(self):
        d = os.path.join(self.dir, "task_frames")
        os.makedirs(d, exist_ok=True)
        self._write(os.path.join(d, "frame_0001.png"), 1000, time.time())
        self._write(os.path.join(d, "frame_0002.png"), 500, time.time())
        self.assertEqual(it._path_usage_bytes(d), 1500)

    def test_clean_task_files_enforces_quota(self):
        """接线：_clean_task_files（encode/decode 入口都会调用）内含配额回收。"""
        now = time.time()
        stale = os.path.join(self.dir, "stale.bin")
        self._write(stale, 4096, now - 7200)          # 超 TTL，走 TTL 路径
        big_old = os.path.join(self.dir, "big_old.bin")
        self._write(big_old, 8192, now - 1800)        # 未超 TTL，走配额路径
        with mock.patch.object(it, "TASK_DIR_MAX_BYTES", 1024), \
             mock.patch.object(it, "TASK_DIR_QUOTA_MIN_AGE", 0):
            it._clean_task_files()
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(big_old))

    def test_zz_encode_runs_cleanup(self):
        """接线：/encode 入口触发 _clean_task_files（配额在每个新任务前生效）。

        排在配额回收用例之后运行：本用例会真起后台编码任务，避免其落盘产物
        干扰前面的配额断言。
        """
        calls = []
        orig = it._clean_task_files

        def spy():
            calls.append(1)
            return orig()

        with mock.patch.object(it, "_clean_task_files", spy):
            r = post_file(f"{API}/encode", "配额接线".encode("utf-8"),
                          "quota.txt", {"mode": "static"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(calls, "/encode 应调用 _clean_task_files")


class TestTaskOwnership(unittest.TestCase):
    """D5：任务归属校验统一收紧——无主任务（升级前创建）与已归属任务同口径（仅本人或超管）。"""

    NORMAL = {"username": "alice", "super_admin": False}
    OTHER = {"username": "bob", "super_admin": False}
    ADMIN = {"username": "root", "super_admin": True}

    def test_orphan_task_rejects_normal_user(self):
        """无主任务 + 普通用户 → 拒绝（收紧前此分支无条件放行）。"""
        for task in ({}, {"created_by": ""}, {"created_by": None}):
            self.assertFalse(it._task_owned_by(task, self.NORMAL), task)
        self.assertFalse(it._task_owned_by({}, None))

    def test_orphan_task_allows_super_admin(self):
        """无主任务 + 超管 → 放行。"""
        self.assertTrue(it._task_owned_by({}, self.ADMIN))
        self.assertTrue(it._task_owned_by({"created_by": ""}, self.ADMIN))

    def test_owned_task_unchanged(self):
        """已归属任务：本人放行、他人拒绝、超管放行、未登录拒绝（回归）。"""
        task = {"created_by": "alice"}
        self.assertTrue(it._task_owned_by(task, self.NORMAL))
        self.assertFalse(it._task_owned_by(task, self.OTHER))
        self.assertTrue(it._task_owned_by(task, self.ADMIN))
        self.assertFalse(it._task_owned_by(task, None))


class TestErrorCodesAndCancel(unittest.TestCase):
    """T32（E2/E4/E5）错误码与文案脱敏 + T33（E3）解析取消与临时文件清理。"""

    def _task_dir(self):
        return it._TASK_DIR

    def test_01_internal_text_is_scrubbed(self):
        """第三方异常文案（含绝对路径/errno）不得透出，只给通用文案 + INTERNAL。"""
        leak = ValueError('attempt to open "D:\\JZToolsHub\\config\\admin.json" failed [WinError 5]')
        code, detail = it._error_of(leak, "INTERNAL", "封装失败，请重试或联系管理员")
        self.assertEqual(code, "INTERNAL")
        self.assertEqual(detail, "封装失败，请重试或联系管理员")
        self.assertNotIn("JZToolsHub", detail)
        self.assertNotIn("\\", detail)

    def test_02_own_message_passes_through_with_code(self):
        """本插件自己的中文提示原样透出，并映射到具体错误码。"""
        cases = [
            ("后端缺少 cv2 / numpy，无法解析视频。请管理员在「管理后台 → 插件管理」按依赖徽标安装"
             "「依赖组件包 JZToolsHub-依赖-cv2-v*.zip」（解压后双击「安装依赖组件.bat」，免重启生效）",
             "DEPENDENCY_MISSING"),
            ("第 1/2 组有效分块不足（1 < 2），无法纠错", "FEC_INSUFFICIENT"),
            ("JZ2 帧 CRC32 校验失败（数据损坏）", "INTEGRITY_UNVERIFIED"),
            ("数据量过大，无法拆分为静态二维码", "TOO_LARGE"),
            ("视频中未识别到二维码帧", "DECODE_FAILED"),
        ]
        for text, code in cases:
            got_code, got_detail = it._error_of(ValueError(text), "DECODE_FAILED", "解析失败")
            self.assertEqual(got_code, code, text)
            self.assertEqual(got_detail, text)
        self.assertIn("INTERNAL", it.ERR_CODES)
        self.assertEqual(set(it.ERR_CODES), set(it.ERR_CODES))     # 枚举去重自检
        for _frag, c in it._ERR_CODE_RULES:
            self.assertIn(c, it.ERR_CODES, c)                       # 规则表不产出未定义码

    def test_03_decode_failure_carries_code_and_no_path(self):
        """视频解析失败：/decode/<id> 回 code，detail 不含服务器路径；临时原件已清理（T33）。"""
        tmpv = os.path.join(self._task_dir(), "t33-bad.mp4")
        os.makedirs(self._task_dir(), exist_ok=True)
        with open(tmpv, "wb") as f:
            f.write(b"not a video at all")
        tid = "t33task001"
        it.set_task(tid, status="pending", progress=0.0, created_at=time.time(),
                    created_by=TEST_USER["username"])
        it._run_decode(tid, tmpv)
        task = it.get_task(tid)
        self.assertEqual(task.get("status"), "error", task)
        self.assertIn(task.get("code"), it.ERR_CODES)
        self.assertNotIn(tmpv, task.get("detail", ""))
        self.assertNotIn("JZToolsHub", task.get("detail", ""))
        self.assertFalse(os.path.exists(tmpv), "T33：解析失败后临时视频原件必须被 finally 删除")
        r = CLIENT.get(f"{API}/decode/{tid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json().get("code"), task.get("code"))

    def test_04_decode_cancel_stops_task_and_cleans_up(self):
        """T33：取消标志让 _run_decode 在入口即终止，状态为 canceled 且不留临时文件。"""
        tmpv = os.path.join(self._task_dir(), "t33-cancel.mp4")
        with open(tmpv, "wb") as f:
            f.write(b"whatever")
        tid = "t33task002"
        it.set_task(tid, status="pending", progress=0.0, created_at=time.time(),
                    created_by=TEST_USER["username"], cancel=True)
        it._run_decode(tid, tmpv)
        task = it.get_task(tid)
        self.assertEqual(task.get("status"), "canceled", task)
        self.assertEqual(task.get("detail"), "已停止解析")
        self.assertFalse(os.path.exists(tmpv))

    def test_05_decode_cancel_endpoint_contract(self):
        """取消接口与封装端同构：未知 404 / 已结束 409 / 进行中 ok=true。"""
        self.assertEqual(CLIENT.post(f"{API}/decode/nosuchtask/cancel").status_code, 404)
        tid = "t33task003"
        it.set_task(tid, status="done", progress=1.0, created_at=time.time(),
                    created_by=TEST_USER["username"])
        r = CLIENT.post(f"{API}/decode/{tid}/cancel")
        self.assertEqual(r.status_code, 409)
        it.set_task(tid, status="running", created_at=time.time())
        r = CLIENT.post(f"{API}/decode/{tid}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get("ok"))
        self.assertTrue(it.get_task(tid).get("cancel"))


class TestSelfcheckVisibility(unittest.TestCase):
    """T18（R3）：出码自检可见性——缺桌面解码器不再伪装「已自检」，也不再静默出坏码。

    裁定口径：缺 zxingcpp 属**优雅降级 + 明示**（规范 B-4；manifest 里 zxingcpp 为 required:false），
    不做 503；缺解码器时 `verified:false` + note 明示，齐备而所有 mask 都读不通才抛错。
    """

    def _env(self):
        return it.build_envelope_v2("text", "自检可见性", "信息传输自检文案" * 30, ".txt")

    def test_01_status_exposes_zxing_capability(self):
        """缺依赖在 /status 上可见（前端据此提示装依赖组件包），不是只在失败时才暴露。"""
        j = CLIENT.get(f"{API}/status").get_json()
        self.assertIn("zxing", j)
        self.assertIsInstance(j["zxing"], bool)

    def test_02_missing_zxing_marks_unverified_not_passed(self):
        """三态自检：在位→verified True；缺 zxing→False（旧实现是缺也报"验过"）。"""
        env = self._env()
        self.assertTrue(env.startswith(it.JZ2_MAGIC))
        _p, _c, ok_on = it.encode_static_output("text", "自检", env, 15, TMP_ROOT, "t18on")
        with mock.patch.object(it, "ZXING_AVAILABLE", False):
            self.assertFalse(it._selfcheck_available(True))
            self.assertIsNone(it._zxing_can_decode(b"not a png"))      # None＝无从验证
            _p2, _c2, ok_off = it.encode_static_output("text", "自检", env, 15, TMP_ROOT, "t18off")
        self.assertTrue(ok_on, "zxing 在位时静态出码应通过自检")
        self.assertFalse(ok_off, "缺 zxing 必须报「未经自检」，不得当作通过")

    def test_03_encode_task_carries_verified_and_note(self):
        """任务与响应透出 verified:false + 明示文案（缺依赖不阻断出码）。"""
        tid = "t18task0001"
        it.set_task(tid, status="pending", progress=0.0, created_at=time.time(),
                    created_by=TEST_USER["username"])
        with mock.patch.object(it, "ZXING_AVAILABLE", False):
            it._run_encode(tid, "static", None, None, "信息传输自检" * 40, 15)
        task = it.get_task(tid)
        self.assertEqual(task.get("status"), "done", task)
        self.assertFalse(task.get("verified"))
        self.assertIn("未经", task.get("note", ""))
        self.assertIn("zxingcpp", task.get("note", ""))
        r = CLIENT.get(f"{API}/task/{tid}")
        self.assertIs(r.get_json().get("verified"), False)

        tid2 = "t18task0002"
        it.set_task(tid2, status="pending", progress=0.0, created_at=time.time(),
                    created_by=TEST_USER["username"])
        it._run_encode(tid2, "static", None, None, "信息传输自检" * 40, 15)
        task2 = it.get_task(tid2)
        self.assertEqual(task2.get("status"), "done", task2)
        self.assertTrue(task2.get("verified"), "zxing 在位时应完成自检")
        self.assertIs(CLIENT.get(f"{API}/task/{tid2}").get_json().get("verified"), True)


if __name__ == "__main__":
    try:
        # warnings="ignore"：屏蔽第三方库（werkzeug 上传落盘 / pypdf）在 GC 时
        # 抛出的临时文件 ResourceWarning——与本套件的判定无关，避免淹没用例输出。
        program = unittest.main(verbosity=2, exit=False, warnings="ignore")
        result = program.result
    finally:
        it._executor.shutdown(wait=True)          # 等后台编码任务收尾
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    sys.exit(0 if result.wasSuccessful() else 1)
