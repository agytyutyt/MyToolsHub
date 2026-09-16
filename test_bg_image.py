"""背景图片清理（bg_image）单元测试 + 「过滤器」/「轨迹速写」端到端回归。

运行：
    python -m pytest test_bg_image.py -q
    python -m unittest test_bg_image -v        # 无需 pytest

覆盖范围
--------
1. 两份副本逐字节一致（B-7 禁止插件间 import，故刻意双份，防单边改动漂移）；
2. xlsx 识别 + 删除：``<picture>`` 引用 / Relationship / 图片本体三件套一起摘掉，
   包内其余部件逐字节不变，数据可用（openpyxl 读出的行与原始一致）；
3. 未发现背景图片 → **原字节返回**（连 zip 都不重写，对存量文件零风险）；
4. 图片本体被别处引用（同一张图既当背景又当浮动图）→ 只解除背景引用、保留本体；
5. 多工作表共用一张图 / 外部链接背景 / 无背景但有普通图片 / 非法包 / 加密包 → 不崩、如实报告；
6. 真实样本 ``D:\\SQLRewrite\\demoData_real.xlsx``（不存在则跳过）；
7. 端到端：``/api/file-filter/filter`` 与 ``/api/trajectory-sketch/upload → analyze → download``
   全链路（临时数据根，不碰真实运行数据），断言"处理流程开始前"落盘的就是干净文件。

端到端点位说明：两个插件的后端模块在框架里以 ``jztools_<插件ID>`` 为包名动态加载，
本测试用 importlib 复刻该加载方式，并把会话函数替换为固定用户（框架的登录态由
admin 插件提供，单测里不引入）。
"""

import importlib
import importlib.util
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PLUGIN_DIRS = {
    "file-filter": os.path.join(HERE, "plugins", "file-filter"),
    "trajectory-sketch": os.path.join(HERE, "plugins", "trajectory-sketch"),
}
REAL_SAMPLE = r"D:\SQLRewrite\demoData_real.xlsx"

USER = {"username": "tester", "role_id": "role-admin", "super_admin": True}


# ==========================================================================
# 最小 xlsx 构造器（只造测试需要的那几个部件）
# ==========================================================================

def png_1x1() -> bytes:
    """生成一张真实可解码的 1×1 PNG（测试夹具用，避免手写可疑的十六进制）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    '{overrides}'
    '</Types>'
)


def build_xlsx(sheets=("Sheet1",), *, background=True, shared_with_drawing=False,
               external_background=False, other_image=False, bad_zip=False) -> bytes:
    """造一个最小 xlsx。

    background=True 时每张工作表末尾带 ``<picture r:id="rId1"/>``（Excel 的"背景图片"）；
    shared_with_drawing=True 时同一张图还被 ``xl/drawings/drawing1.xml`` 引用（本体应保留）；
    external_background=True 时背景关系指向文件外的 URL（无本体可删）。
    """
    if bad_zip:
        return b"this is not a zip at all"

    media = png_1x1()
    overrides = ['<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
                 'officedocument.spreadsheetml.sheet.main+xml"/>',
                 '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-'
                 'package.core-properties+xml"/>']
    parts = {}
    for i in range(1, len(sheets) + 1):
        overrides.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/'
                         'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i)
    parts["[Content_Types].xml"] = _CONTENT_TYPES.format(overrides="".join(overrides)).encode()
    parts["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/'
        'metadata/core-properties" Target="docProps/core.xml"/></Relationships>').encode()
    parts["docProps/core.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties"><cp:lastModifiedBy>tester</cp:lastModifiedBy></cp:coreProperties>').encode()

    sheet_els, wb_rels = [], []
    for i, name in enumerate(sheets, start=1):
        sheet_els.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (name, i, i))
        wb_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                       'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>'
                       % (i, i))
    parts["xl/workbook.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>%s</sheets></workbook>' % "".join(sheet_els)).encode()
    parts["xl/_rels/workbook.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(wb_rels) + '</Relationships>').encode()

    own_rels = {}
    for i in range(1, len(sheets) + 1):
        body = ('<row r="1"><c r="A1" t="inlineStr"><is><t>时间</t></is></c>'
                '<c r="B1" t="inlineStr"><is><t>经度</t></is></c></row>'
                '<row r="2"><c r="A2" t="inlineStr"><is><t>2026-09-10 04:30:38</t></is></c>'
                '<c r="B2"><v>109.92401</v></c></row>'
                '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
                '<headerFooter/>')
        rels = []
        if background:
            if external_background:
                rels.append('<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                            'officeDocument/2006/relationships/image" Target="http://x/bg.png" '
                            'TargetMode="External"/>')
                body += '<picture r:id="rId1"/>'
            else:
                rels.append('<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                            'officeDocument/2006/relationships/image" Target="../media/image1.png"/>')
                body += '<picture r:id="rId1"/>'
        if shared_with_drawing or (other_image and i == 1):
            rels.append('<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
                        'officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/>')
            body += '<drawing r:id="rId2"/>'
        parts["xl/worksheets/sheet%d.xml" % i] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheetData>%s</sheetData></worksheet>' % body).encode()
        if rels:
            own_rels["xl/worksheets/_rels/sheet%d.xml.rels" % i] = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(rels) + '</Relationships>').encode()
    parts.update(own_rels)

    if background and not external_background:
        parts["xl/media/image1.png"] = media
    if shared_with_drawing:
        parts["xl/media/image1.png"] = media
        parts["xl/drawings/drawing1.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing">'
            '<xdr:twoCellAnchor><xdr:pic><xdr:blipFill><a:blip xmlns:a="http://schemas.openxmlformats.'
            'org/drawingml/2006/main" r:embed="rId1"/></xdr:blipFill></xdr:pic></xdr:twoCellAnchor>'
            '</xdr:wsDr>').encode()
        parts["xl/drawings/_rels/drawing1.xml.rels"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/image" Target="../media/image1.png"/></Relationships>').encode()
    elif other_image:
        parts["xl/media/image1.png"] = media
        parts["xl/drawings/drawing1.xml"] = b"<xdr:wsDr/>"
        parts["xl/drawings/_rels/drawing1.xml.rels"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/image" Target="../media/image1.png"/></Relationships>').encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", parts.pop("[Content_Types].xml"))
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


# ==========================================================================
# 夹具与辅助
# ==========================================================================

def load_plugin(pkg_name: str, plugin_dir: str):
    """按框架的方式把插件后端加载为包（``jztools_<id>``）并返回其 routes 模块。"""
    backend = os.path.join(plugin_dir, "backend")
    spec = importlib.util.spec_from_file_location(
        pkg_name, os.path.join(backend, "__init__.py"), submodule_search_locations=[backend])
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(pkg_name + ".routes")


def zip_names(blob: bytes):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.namelist()


def read_part(blob: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.read(name)


class Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="jz-bgimg-")
        cls.data_root = os.path.join(cls.tmp, "data")
        os.environ["JZTOOLS_DATA_ROOT"] = cls.data_root
        # 每个插件在临时数据根里独立加载，避免互相污染
        cls.ff = load_plugin("jztools_file_filter", PLUGIN_DIRS["file-filter"])
        cls.ts = load_plugin("jztools_trajectory_sketch", PLUGIN_DIRS["trajectory-sketch"])
        cls.ff._get_session_user = lambda: dict(USER)
        cls.ts._get_session_user = lambda: dict(USER)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("JZTOOLS_DATA_ROOT", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)


# ==========================================================================
# 1. 两份副本一致
# ==========================================================================

class TestCopies(Base):

    def test_two_copies_identical(self):
        """B-7 禁止插件间 import → 刻意双份；两份必须逐字节一致（防单边改动漂移）。"""
        paths = [os.path.join(d, "backend", "bg_image.py") for d in PLUGIN_DIRS.values()]
        blobs = [open(p, "rb").read() for p in paths]
        self.assertEqual(blobs[0], blobs[1],
                         "plugins/file-filter 与 plugins/trajectory-sketch 下的 bg_image.py "
                         "内容不一致，请同步两份副本")
        self.assertTrue(blobs[0])

    def test_module_is_pure_stdlib(self):
        """模块必须零第三方依赖（两个插件都可能在精简环境下跑）。"""
        src = open(os.path.join(PLUGIN_DIRS["file-filter"], "backend", "bg_image.py"),
                   encoding="utf-8").read()
        for banned in ("import openpyxl", "import xlrd", "import flask", "import requests",
                       "from flask", "import pandas", "import numpy"):
            self.assertNotIn(banned, src)


# ==========================================================================
# 2/3. 识别 + 删除、无背景时零改动
# ==========================================================================

class TestStripXlsx(Base):

    def test_remove_background_three_parts(self):
        """背景图片 = 引用 + 关系 + 本体，三者都要摘掉。"""
        blob = build_xlsx()
        self.assertIn(b"<picture", read_part(blob, "xl/worksheets/sheet1.xml"))
        clean, rep = self.ff.bg_image.strip_background_images(blob, "xlsx")

        self.assertTrue(rep["scanned"])
        self.assertEqual((rep["found"], rep["removed"]), (1, 1))
        self.assertIn("已删除背景图片 1 张", rep["note"])
        self.assertIn("Sheet1", rep["note"])
        self.assertNotIn(b"<picture", read_part(clean, "xl/worksheets/sheet1.xml"))
        self.assertNotIn("xl/media/image1.png", zip_names(clean))
        self.assertNotIn("xl/worksheets/_rels/sheet1.xml.rels", zip_names(clean))
        # 其余部件逐字节不变
        for name in ("[Content_Types].xml", "xl/workbook.xml", "_rels/.rels", "docProps/core.xml"):
            self.assertEqual(read_part(blob, name), read_part(clean, name), name)

    def test_noop_when_no_background(self):
        """没有背景图片 → 原字节返回（不做任何改写）。"""
        blob = build_xlsx(background=False)
        clean, rep = self.ff.bg_image.strip_background_images(blob, "xlsx")
        self.assertIs(clean, blob)
        self.assertTrue(rep["scanned"])
        self.assertEqual((rep["found"], rep["removed"]), (0, 0))
        self.assertEqual(rep["note"], "未发现背景图片")

    def test_noop_reports_other_images(self):
        """没有背景但有普通嵌入图片 → 不删，只在结论里提示。"""
        blob = build_xlsx(background=False, other_image=True)
        clean, rep = self.ff.bg_image.strip_background_images(blob, "xlsx")
        self.assertIs(clean, blob)
        self.assertEqual(rep["other_images"], 1)
        self.assertIn("普通嵌入图片", rep["note"])
        self.assertNotIn("已删除", rep["note"])

    def test_shared_image_body_kept(self):
        """同一张图既当背景又被浮动图引用 → 只解除背景引用，本体保留。"""
        blob = build_xlsx(shared_with_drawing=True)
        clean, rep = self.ff.bg_image.strip_background_images(blob, "xlsx")
        self.assertIn("xl/media/image1.png", zip_names(clean))
        self.assertEqual(rep["removed"], 0)
        self.assertEqual(rep["kept_parts"], 1)
        self.assertTrue(rep["images"][0]["kept"])
        self.assertEqual(rep["bytes"], 0)
        self.assertIn("已保留", rep["note"])
        # 背景引用已解除，浮动图的那份关系保留（背景关系在工作表 rels，浮动图关系在 drawing rels）
        self.assertNotIn(b"<picture", read_part(clean, "xl/worksheets/sheet1.xml"))
        sheet_rels = read_part(clean, "xl/worksheets/_rels/sheet1.xml.rels")
        self.assertNotIn(b'rId1', sheet_rels)
        self.assertIn(b"drawing1.xml", sheet_rels)
        self.assertIn(b"image1.png", read_part(clean, "xl/drawings/_rels/drawing1.xml.rels"))

    def test_shared_media_across_sheets_removed_once(self):
        """两张工作表共用一张背景图 → 两处引用都摘掉，本体只删一次。"""
        blob = build_xlsx(sheets=("S1", "S2"))
        media_size = len(png_1x1())
        clean, rep = self.ff.bg_image.strip_background_images(blob, "xlsx")
        self.assertEqual(rep["found"], 2)
        self.assertEqual(rep["removed"], 1)
        self.assertEqual(rep["bytes"], media_size)     # 体积按本体算一次，不随引用数翻倍
        self.assertNotIn("xl/media/image1.png", zip_names(clean))
        for i in (1, 2):
            self.assertNotIn(b"<picture", read_part(clean, "xl/worksheets/sheet%d.xml" % i))

    def test_sheet_names_in_report(self):
        """两张工作表都有背景图 → 结论里点出工作表名。"""
        blob = build_xlsx(sheets=("台账", "明细"))
        _clean, rep = self.ff.bg_image.strip_background_images(blob, "xlsx")
        self.assertIn("台账", rep["note"])
        self.assertIn("明细", rep["note"])
        self.assertEqual([img["sheets"] for img in rep["images"]], [["台账"], ["明细"]])

    def test_external_background(self):
        """背景指向文件外的 URL → 解除引用，报告说明没有本体可删。"""
        blob = build_xlsx(external_background=True)
        clean, rep = self.ff.bg_image.strip_background_images(blob, "xlsx")
        self.assertNotIn(b"<picture", read_part(clean, "xl/worksheets/sheet1.xml"))
        self.assertEqual(rep["removed"], 0)
        self.assertEqual(rep["found"], 1)
        self.assertIn("已解除引用", rep["note"])

    def test_workbook_still_readable_and_data_intact(self):
        """改包后 Excel 仍能正常解析，且数据一行不差。"""
        openpyxl = importlib.import_module("openpyxl")
        blob = build_xlsx()
        clean, _rep = self.ff.bg_image.strip_background_images(blob, "xlsx")

        def grid(data):
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
            try:
                return [tuple(r) for r in wb.active.iter_rows(values_only=True)]
            finally:
                wb.close()

        self.assertEqual(grid(blob), grid(clean))
        with zipfile.ZipFile(io.BytesIO(clean)) as z:
            self.assertIsNone(z.testzip())

    def test_idempotent(self):
        blob = build_xlsx()
        once, _ = self.ff.bg_image.strip_background_images(blob, "xlsx")
        twice, rep = self.ff.bg_image.strip_background_images(once, "xlsx")
        self.assertIs(twice, once)
        self.assertEqual(rep["note"], "未发现背景图片")

    def test_bad_zip_and_encrypted_fail_open(self):
        """非法包 / 加密包：不抛异常、原字节返回、结论里说明原因。"""
        blob = build_xlsx(bad_zip=True)
        clean, rep = self.ff.bg_image.strip_background_images(blob, "xlsx")
        self.assertIs(clean, blob)
        self.assertFalse(rep["scanned"])
        self.assertIn("不是有效的 xlsx", rep["note"])

        enc = build_xlsx()
        marked = bytearray(enc)                       # 把中央目录里的第 1 个条目标记为加密
        idx = enc.find(b"PK\x01\x02")
        marked[idx + 8] |= 0x1
        enc_blob = bytes(marked)
        clean2, rep2 = self.ff.bg_image.strip_background_images(enc_blob, "xlsx")
        self.assertIs(clean2, enc_blob)
        self.assertIn("加密", rep2["note"])

    def test_zip_bomb_guard(self):
        original = self.ff.bg_image.MAX_TOTAL_UNCOMPRESSED
        self.ff.bg_image.MAX_TOTAL_UNCOMPRESSED = 10
        try:
            blob = build_xlsx()
            clean, rep = self.ff.bg_image.strip_background_images(blob, "xlsx")
            self.assertIs(clean, blob)
            self.assertIn("256MB", rep["note"])
        finally:
            self.ff.bg_image.MAX_TOTAL_UNCOMPRESSED = original

    def test_csv_and_xls_not_scanned(self):
        for ext, keyword in (("csv", "纯文本"), ("xls", "旧版 .xls")):
            clean, rep = self.ff.bg_image.strip_background_images(b"whatever", ext)
            self.assertEqual(clean, b"whatever")
            self.assertFalse(rep["scanned"])
            self.assertIn(keyword, rep["note"])

    def test_real_sample(self):
        """真实样本：D:\\SQLRewrite\\demoData_real.xlsx（不存在则跳过）。"""
        if not os.path.isfile(REAL_SAMPLE):
            self.skipTest("真实样本不存在：%s" % REAL_SAMPLE)
        openpyxl = importlib.import_module("openpyxl")
        blob = open(REAL_SAMPLE, "rb").read()
        self.assertIn(b"<picture", read_part(blob, "xl/worksheets/sheet1.xml"))
        clean, rep = self.ff.bg_image.strip_background_images(blob, "xlsx")

        self.assertEqual((rep["found"], rep["removed"]), (1, 1))
        self.assertEqual(rep["images"][0]["part"], "xl/media/image1.png")
        self.assertIn("Sheet1", rep["images"][0]["sheets"])
        self.assertNotIn("xl/media/image1.png", zip_names(clean))
        self.assertNotIn(b"<picture", read_part(clean, "xl/worksheets/sheet1.xml"))

        def rows(data):
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            try:
                return [tuple(r) for r in wb.active.iter_rows(values_only=True)]
            finally:
                wb.close()
        a = rows(blob)
        self.assertEqual(a, rows(clean))
        self.assertGreater(len(a), 100)               # 真实台账，不是空表
        # 产物比原件小，且差值就是那张图（去掉的是图片本体）
        self.assertLess(len(clean), len(blob))


# ==========================================================================
# 7. 端到端：两个插件的"处理流程开始前"落盘的就是干净文件
# ==========================================================================

class TestEndToEnd(Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not os.path.isfile(REAL_SAMPLE):
            raise unittest.SkipTest("真实样本不存在：%s" % REAL_SAMPLE)
        from flask import Flask
        cls.app = Flask("bgimg-e2e")
        cls.ff.register(cls.app)
        cls.ts.register(cls.app)
        # 配置：把插件自带的模板搬进临时数据根（keep_columns 与真实样本表头一致）
        for pid in ("trajectory-sketch", "file-filter"):
            src = os.path.join(PLUGIN_DIRS[pid], "backend", "config.template.json")
            dst_dir = os.path.join(cls.data_root, "plugins", pid)
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy(src, os.path.join(dst_dir, "config.json"))
        # 工具登记表：轨迹速写的字段过滤依赖「过滤器」插件，须登记为启用
        cfg_dir = os.path.join(cls.data_root, "config")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "tools.json"), "w", encoding="utf-8") as f:
            json.dump({"tools": [{"id": "file-filter", "name": "过滤器", "enabled": True},
                                 {"id": "trajectory-sketch", "name": "轨迹速写", "enabled": True}]}, f)
        cls.client = cls.app.test_client()
        cls.upload_bytes = open(REAL_SAMPLE, "rb").read()

    def _upload(self, url, **form):
        return self.client.post(url, data={"file": (io.BytesIO(self.upload_bytes),
                                                    "demoData_real.xlsx"), **form},
                                content_type="multipart/form-data")

    def _wait(self, url, timeout=120.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
            payload = res.get_json()
            if payload["status"] in ("done", "error"):
                return payload
            time.sleep(0.3)
        self.fail("任务超时未结束：%s" % url)

    def test_file_filter_flow(self):
        """过滤器：上传 → 落盘的输入件已无背景图片 → 结果里带预处理结论。"""
        res = self._upload("/api/file-filter/filter", mode="hard",
                           columns=json.dumps(["BEGINTIME", "USERNUM", "LONGITUDE", "LATITUDE"]))
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        body = res.get_json()
        self.assertEqual(body["sanitize"]["removed"], 1)
        self.assertIn("已删除背景图片 1 张", body["sanitize"]["note"])

        payload = self._wait("/api/file-filter/result/%s" % body["task_id"])
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertEqual(payload["sanitize"]["removed"], 1)
        self.assertIn("Sheet1", payload["sanitize"]["note"])
        # 关键断言：任务落盘的输入件就是"干净文件"（后续过滤读的是它）
        in_path = os.path.join(self.ff.TASK_DIR, "%s_input.xlsx" % body["task_id"])
        blob = open(in_path, "rb").read()
        self.assertNotIn(b"<picture", read_part(blob, "xl/worksheets/sheet1.xml"))
        self.assertNotIn("xl/media/image1.png", zip_names(blob))

    def test_trajectory_sketch_flow(self):
        """轨迹速写：上传（预处理）→ 自检 → 分析 → 报告里可追溯。"""
        res = self._upload("/api/trajectory-sketch/upload")
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        up = res.get_json()
        self.assertEqual(up["sanitize"]["removed"], 1)
        self.assertTrue(up["schema"]["can_analyze"], up["schema"].get("hint"))
        self.assertEqual(up["row_count"], 454)

        res = self.client.post("/api/trajectory-sketch/analyze",
                               json={"staged_id": up["staged_id"], "mode": "hard"})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        task_id = res.get_json()["task_id"]
        payload = self._wait("/api/trajectory-sketch/result/%s" % task_id)
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertEqual(payload["sanitize"]["removed"], 1)
        self.assertIn("已删除背景图片 1 张", payload["sanitize"]["note"])

        # 暂存的原始上传件在预处理之后落盘
        staged = os.path.join(self.ts.report_store.cache_dir(), "%s_upload.xlsx" % up["staged_id"])
        self.assertNotIn(b"<picture", read_part(open(staged, "rb").read(),
                                                "xl/worksheets/sheet1.xml"))

        # 报告工作簿「数据质量」sheet 记录这条预处理结论（可追溯）
        res = self.client.get("/api/trajectory-sketch/download/%s" % task_id)
        self.assertEqual(res.status_code, 200)
        openpyxl = importlib.import_module("openpyxl")
        wb = openpyxl.load_workbook(io.BytesIO(res.data))
        try:
            text = "\n".join(str(c.value) for row in wb["数据质量"].iter_rows() for c in row)
        finally:
            wb.close()
        self.assertIn("源文件预处理", text)
        self.assertIn("已删除背景图片 1 张", text)
        self.assertIn("xl/media/image1.png", text)
        # 报告自身不含背景图片
        self.assertNotIn(b"<picture", read_part(res.data, "xl/worksheets/sheet1.xml"))


if __name__ == "__main__":
    unittest.main(verbosity=2)