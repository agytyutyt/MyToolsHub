# -*- coding: utf-8 -*-
"""xlsx 失真 ``<dimension>`` 声明回归测试（过滤器 / 轨迹速写 / 轨迹转换 / 信息传输）。

现象
----
某些 Excel 文件（第三方导出工具写出的）在工作表 XML 里声明 ``<dimension ref="A1"/>``，
实际却有多行多列——该元素在 OOXML 里只是"提示"，Excel 自身容忍。而 openpyxl 的
``read_only=True`` 模式**以该声明为遍历范围上界**，于是 ``max_row=1``，只读出首格。
用户侧表现为两种：「只识别第一行第一列」/「提示只有表头没有数据」。

修复口径（四个插件各自一份同源实现——B-7 禁止跨插件 import，不抽公共模块）
------------------------------------------------------------------------
1. ``ws.reset_dimensions()``（openpyxl ≥3.0.4，``hasattr`` 守卫）清掉声明，改按
   ``sheetData`` 实际内容扫描；
2. **必须配套补齐行宽**：清掉声明后 openpyxl 不再按声明宽度补齐稀疏行
   （``['onlyA', None, None]`` 会变成 ``['onlyA']``），而下游按列下标取值，
   故统一补齐为矩形。只做第 1 步会引入新回归——第 3 节的稀疏行断言钉住这一点。

覆盖
----
1. 前提：夹具确实是一份"声明与实际不符"的文件，且 openpyxl 只读模式确实会因此截断；
2. 四种失真形态（声明过小 / 行列都过小 / 声明过大 / 无声明）+ 正确声明，读取结果
   与"普通模式基准"逐单元格一致（四个插件各测一遍）；
3. 稀疏行补齐后列下标不漂移；声明过大不产生幽灵列/幽灵行；
4. 端到端：失真文件走完上传 / 分析 / 下载全链路，不出现"只有表头没有数据"。

运行：
    python -m pytest test_xlsx_stale_dimension.py -q
    python -m unittest test_xlsx_stale_dimension -v        # 无需 pytest

真实样本 ``660.xlsx``（用户提供的失真文件，声明 A1、实际 A1:A4）位于
``.workbuddy/test-materials/xlsx-stale-dimension/``（该目录不入库），存在时一并校验。
"""

import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PLUGIN_DIRS = {
    "file-filter": os.path.join(HERE, "plugins", "file-filter"),
    "trajectory-sketch": os.path.join(HERE, "plugins", "trajectory-sketch"),
    "trajectory-convert": os.path.join(HERE, "plugins", "trajectory-convert"),
    "info-transfer": os.path.join(HERE, "plugins", "info-transfer"),
}

SHEET_PART = "xl/worksheets/sheet1.xml"
REAL_SAMPLE = os.path.join(HERE, ".workbuddy", "test-materials",
                           "xlsx-stale-dimension", "660.xlsx")
SKETCH_SAMPLE = os.path.join(HERE, "testdata", "trajectory-sketch", "轨迹表.xlsx")
FILTER_SAMPLE = os.path.join(HERE, "testdata", "file-filter", "花名册.xlsx")

USER = {"username": "tester", "role_id": "role-admin", "super_admin": True}

TRAJ_HEADERS = ["BEGINTIME", "USERNUM", "LAI", "CI", "ADDRESS", "LONGITUDE", "LATITUDE"]


# ==========================================================================
# 夹具：造 xlsx / 改声明 / 基准读数
# ==========================================================================

def make_xlsx(rows, sheet="Sheet1") -> bytes:
    """用 openpyxl 造一份**声明正确**的 xlsx（基准与失真改造的素材都来自它）。"""
    openpyxl = importlib.import_module("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def read_part(blob: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.read(name)


def rewrite_sheet(blob: bytes, transform) -> bytes:
    """只替换工作表 XML，其余部件逐字节保留（复刻"文件本身只有这一处失真"）。"""
    src = zipfile.ZipFile(io.BytesIO(blob))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == SHEET_PART:
                data = transform(data)
            dst.writestr(item, data)
    return out.getvalue()


def declared_dimension(blob: bytes) -> str:
    m = re.search(rb"<dimension[^>]*/>", read_part(blob, SHEET_PART))
    return m.group(0).decode() if m else ""


def with_declared(blob: bytes, ref):
    """把 ``<dimension>`` 改成 ref；ref 为 None 表示整段删除。"""
    def transform(data):
        if ref is None:
            return re.sub(rb"<dimension[^>]*/>", b"", data, count=1)
        return re.sub(rb"<dimension[^>]*/>",
                      ('<dimension ref="%s"/>' % ref).encode("utf-8"), data, count=1)
    return rewrite_sheet(blob, transform)


def baseline_rows(blob: bytes):
    """基准：普通模式（不信任任何声明）读出的二维表。"""
    openpyxl = importlib.import_module("openpyxl")
    wb = openpyxl.load_workbook(io.BytesIO(blob), data_only=True)
    try:
        return [[v for v in r] for r in wb.active.iter_rows(values_only=True)]
    finally:
        wb.close()


def raw_readonly_rows(blob: bytes):
    """未修复的取数口径（只读模式直接 iter_rows），用于钉住"前提"仍成立。"""
    openpyxl = importlib.import_module("openpyxl")
    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    try:
        return [[v for v in r] for r in wb.active.iter_rows(values_only=True)]
    finally:
        wb.close()


def minimal_rect(rows):
    """裁到「包含全部非空单元格的最小矩形」。

    基准（普通模式会按声明宽度补齐）与实测（按实际内容宽度补齐）都要先归一化，
    比较的才是**数据本身**：既不允许丢内容，也不允许凭空多出空行空列。
    """
    rows = [list(r) for r in (rows or [])]
    while rows and all(v is None or v == "" for v in rows[-1]):
        rows.pop()
    width = 0
    for r in rows:
        for i in range(len(r) - 1, -1, -1):
            if r[i] is not None and r[i] != "":
                width = max(width, i + 1)
                break
    return [r[:width] for r in rows]


def stale_shapes(blob: bytes):
    """同一份文件的五种声明形态 → {名称: 改造后的字节}。"""
    base = minimal_rect(baseline_rows(blob))
    width = max((len(r) for r in base), default=1) or 1
    return {
        "stale_A1": with_declared(blob, "A1"),          # 真实案例形态
        "under_2x2": with_declared(blob, "A1:B2"),      # 行列都小于实际
        "over_99": with_declared(blob, "A1:Z999"),      # 声明过大
        "no_decl": with_declared(blob, None),           # 完全没有声明
        "exact": with_declared(blob, "A1:%s%d" % (_col_letter(width), len(base) or 1)),
    }


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def trajectory_rows(n_stay=20, n_move=10, start="2026-09-10 04:00:00"):
    """标准表头的轨迹数据：先停留 → 长途移动 → 再停留（足以判定停留点/出行段）。

    步长 600s > 清洗网格 300s，故去重后点数 == 数据行数（便于精确断言）。
    """
    rows = [list(TRAJ_HEADERS)]
    t = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    step = timedelta(minutes=10)

    def add(addr, lon, lat, ci):
        nonlocal t
        rows.append([t.strftime("%Y-%m-%d %H:%M:%S"), "15700000007", "1335731",
                     ci, addr, lon, lat])
        t += step

    for i in range(n_stay):
        add("甲地", 109.9000 + 0.0002 * (i % 2), 21.6000 + 0.0002 * (i % 2), "1001")
    for i in range(1, n_move + 1):
        add("途经地", 109.9000 + 0.02 * i, 21.6000, "2000")
    for i in range(n_stay):
        add("乙地", 110.1000 + 0.0002 * (i % 2), 21.6000 + 0.0002 * (i % 2), "3001")
    return rows


def read_sample(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ==========================================================================
# 插件加载（复刻框架的 jztools_<id> 动态加载）
# ==========================================================================

def load_plugin(pkg_name: str, plugin_dir: str):
    backend = os.path.join(plugin_dir, "backend")
    spec = importlib.util.spec_from_file_location(
        pkg_name, os.path.join(backend, "__init__.py"), submodule_search_locations=[backend])
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(pkg_name + ".routes")


class Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="jz-dim-")
        cls.data_root = os.path.join(cls.tmp, "data")
        os.environ["JZTOOLS_DATA_ROOT"] = cls.data_root
        cls.ff = load_plugin("jztools_file_filter", PLUGIN_DIRS["file-filter"])
        cls.ts = load_plugin("jztools_trajectory_sketch", PLUGIN_DIRS["trajectory-sketch"])
        cls.tc = load_plugin("jztools_trajectory_convert", PLUGIN_DIRS["trajectory-convert"])
        cls.it = load_plugin("jztools_info_transfer", PLUGIN_DIRS["info-transfer"])
        # 框架把插件 backend/ 目录本身加载为包 jztools_<id>（见 load_plugin）
        cls.sketch_io = importlib.import_module("jztools_trajectory_sketch.excel_io")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("JZTOOLS_DATA_ROOT", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def write_tmp(self, name: str, blob: bytes) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(blob)
        return path


# ==========================================================================
# 1. 前提：夹具与故障机理
# ==========================================================================

class TestPremise(Base):

    def test_fixture_really_declares_stale_dimension(self):
        """夹具必须是"声明 A1、实际多行多列"——否则整套断言都失去意义。"""
        rows = [["时间", "经度", "纬度"], ["2026-09-10 04:30:38", 109.9, 21.6],
                ["2026-09-10 04:40:38", 109.91, 21.61]]
        stale = with_declared(make_xlsx(rows), "A1")
        self.assertEqual(declared_dimension(stale), '<dimension ref="A1"/>')
        sheet = read_part(stale, SHEET_PART)
        self.assertIn(b'<row r="3"', sheet)          # 实际有 3 行
        # 未修复的取数口径只拿到首格
        self.assertEqual(minimal_rect(raw_readonly_rows(stale)), [["时间"]])

    def test_openpyxl_readonly_still_trusts_declaration(self):
        """机理前提：只读模式仍以声明为界（上游若改掉，本套件其余断言依旧有效）。"""
        rows = [["a"], ["b"], ["c"]]
        stale = with_declared(make_xlsx(rows), "A1")
        if len(raw_readonly_rows(stale)) == len(rows):
            self.skipTest("openpyxl 已不再信任 <dimension> 声明（上游已修）")
        self.assertEqual(len(raw_readonly_rows(stale)), 1)

    def test_real_sample_660(self):
        """真实样本 660.xlsx：声明 A1、实际 A1:A4（不存在则跳过）。"""
        if not os.path.isfile(REAL_SAMPLE):
            self.skipTest("真实样本不存在：%s" % REAL_SAMPLE)
        blob = read_sample(REAL_SAMPLE)
        self.assertEqual(declared_dimension(blob), '<dimension ref="A1"/>')
        self.assertEqual(len(minimal_rect(raw_readonly_rows(blob))), 1)   # 未修复会截断
        self.assertEqual(len(minimal_rect(baseline_rows(blob))), 4)
        self.assertEqual(len(self.sketch_io.read_rows(REAL_SAMPLE, "660.xlsx")), 4)
        headers, rows = self.ff.core.read_table(REAL_SAMPLE, "660.xlsx")
        self.assertEqual((headers, len(rows)), (["近十五天凌晨所在位置"], 3))


# ==========================================================================
# 2. 四种失真形态 × 四个插件：结果与普通模式基准一致
# ==========================================================================

class TestReadersAgainstBaseline(Base):

    def _assert_matches(self, reader, blob, label):
        want = minimal_rect(baseline_rows(blob))
        got = minimal_rect(reader())
        self.assertEqual(got, want, "%s：失真声明下读数与普通模式基准不一致" % label)

    def test_file_filter(self):
        fixtures = {
            "轨迹表": read_sample(SKETCH_SAMPLE) if os.path.isfile(SKETCH_SAMPLE)
                    else make_xlsx(trajectory_rows()),
            "花名册": read_sample(FILTER_SAMPLE) if os.path.isfile(FILTER_SAMPLE)
                    else make_xlsx([["姓名", "单位", "电话"], ["张三", "甲", "13800000000"]]),
            "单列": make_xlsx([["地点"], ["甲地"], ["乙地"], ["丙地"]]),
        }
        for fname, blob in fixtures.items():
            for shape, mutated in stale_shapes(blob).items():
                path = self.write_tmp("ff-%s-%s.xlsx" % (fname, shape), mutated)

                def read(path=path):
                    headers, rows = self.ff.core.read_table(path, "x.xlsx")
                    return [headers] + rows

                self._assert_matches(read, blob, "过滤器/%s/%s" % (fname, shape))
                # 真实读盘路径也要走到（上面用 path，确保不是字节流的特例）
                self.assertTrue(os.path.getsize(path) > 0)

    def test_trajectory_sketch(self):
        fixtures = {
            "轨迹表": read_sample(SKETCH_SAMPLE) if os.path.isfile(SKETCH_SAMPLE)
                    else make_xlsx(trajectory_rows()),
            "单列": make_xlsx([["地点"], ["甲地"], ["乙地"], ["丙地"]]),
        }
        for fname, blob in fixtures.items():
            for shape, mutated in stale_shapes(blob).items():
                path = self.write_tmp("ts-%s-%s.xlsx" % (fname, shape), mutated)
                self._assert_matches(lambda p=path: self.sketch_io.read_rows(p, "x.xlsx"),
                                     blob, "轨迹速写/%s/%s" % (fname, shape))

    def test_trajectory_convert(self):
        blob = read_sample(SKETCH_SAMPLE) if os.path.isfile(SKETCH_SAMPLE) \
            else make_xlsx(trajectory_rows())
        for shape, mutated in stale_shapes(blob).items():
            path = self.write_tmp("tc-%s.xlsx" % shape, mutated)
            self._assert_matches(lambda p=path: self.tc._read_excel_rows(p, "x.xlsx"),
                                 blob, "轨迹转换/%s" % shape)

    def test_info_transfer(self):
        """信息传输：28b93d5 已修，本用例补回它随开发期测试文件一起删掉的回归钉子。"""
        blob = read_sample(SKETCH_SAMPLE) if os.path.isfile(SKETCH_SAMPLE) \
            else make_xlsx(trajectory_rows())
        for shape, mutated in stale_shapes(blob).items():
            self._assert_matches(lambda b=mutated: self.it._extract_xlsx_rows(b),
                                 blob, "信息传输/%s" % shape)


# ==========================================================================
# 3. 补齐行宽：列下标不漂移、不产生幽灵行/列
# ==========================================================================

class TestRectangularPadding(Base):

    def test_sparse_rows_keep_column_positions(self):
        """稀疏行必须补齐到最大列宽——只做 reset 不做补齐会让列下标左移（回归钉子）。"""
        rows = [["时间", None, "经度"],                    # 表头本身稀疏（B 列空）
                ["2026-09-10 04:30:38", None, 109.9],
                ["2026-09-10 04:40:38"],                  # 只填了 A 列
                [None, None, "只有经度"]]                  # 只填了 C 列
        blob = make_xlsx(rows)
        for shape, mutated in stale_shapes(blob).items():
            path = self.write_tmp("sparse-%s.xlsx" % shape, mutated)
            got = self.sketch_io.read_rows(path, "x.xlsx")
            self.assertEqual(got, minimal_rect(baseline_rows(blob)),
                             "稀疏行在 %s 下发生了列漂移" % shape)
            self.assertEqual(got[2], ["2026-09-10 04:40:38", None, None])
            self.assertEqual(got[3][2], "只有经度")
            headers, body = self.ff.core.read_table(path, "x.xlsx")
            self.assertEqual(headers, ["时间", "", "经度"])
            self.assertEqual([r[2] for r in body], [109.9, None, "只有经度"])

    def test_over_declared_dimension_adds_no_phantom(self):
        """声明过大（A1:Z999）不得凭空多出空行空列。"""
        rows = [["编号", "姓名"], ["T1", "张三"], ["T2", "李四"]]
        blob = make_xlsx(rows)
        path = self.write_tmp("phantom.xlsx", with_declared(blob, "A1:Z999"))
        self.assertEqual(self.sketch_io.read_rows(path, "x.xlsx"), rows)
        self.assertEqual(self.ff.core.read_table(path, "x.xlsx"), (["编号", "姓名"], rows[1:]))
        self.assertEqual(minimal_rect(self.tc._read_excel_rows(path, "x.xlsx")), rows)

    def test_wide_sheet_keeps_all_columns(self):
        """列数被低估时（声明 A1、实际 7 列）不得截掉列——用户报的"只剩第一列"。"""
        rows = [list(TRAJ_HEADERS), ["2026-09-10 04:30:38", "15700000007", "1335731",
                                     "1001", "甲地", 109.9, 21.6]]
        path = self.write_tmp("wide.xlsx", with_declared(make_xlsx(rows), "A1"))
        got = self.sketch_io.read_rows(path, "x.xlsx")
        self.assertEqual(len(got[0]), 7)
        self.assertEqual(got[1][6], 21.6)


# ==========================================================================
# 4. 轨迹类解析链路：失真文件不得"解析不出点"
# ==========================================================================

class TestTrajectoryPipelines(Base):

    def test_convert_parses_all_points(self):
        rows = trajectory_rows()
        n = len(rows) - 1
        path = self.write_tmp("conv.xlsx", with_declared(make_xlsx(rows), "A1"))
        parsed = self.tc._read_excel_rows(path, "conv.xlsx")
        self.assertEqual(len(parsed), n + 1)
        points = self.tc.parse_trajectory(parsed, {
            "time_field": "BEGINTIME", "lng_field": "LONGITUDE", "lat_field": "LATITUDE"})
        self.assertEqual(len(points), n)

    def test_sketch_analysis_not_header_only(self):
        """轨迹速写：失真文件必须能进入分析（旧行为会判"表格只有表头"）。"""
        rows = trajectory_rows()
        path = self.write_tmp("sketch.xlsx", with_declared(make_xlsx(rows), "A1"))
        raw = self.sketch_io.read_rows(path, "x.xlsx")
        self.assertEqual(len(raw), len(rows))             # 路由层 len(raw_rows) < 2 的门槛
        self.assertEqual(self.sketch_io.preview_headers(raw)[:3],
                         ["BEGINTIME", "USERNUM", "LAI"])


# ==========================================================================
# 5. 端到端：失真文件走完 HTTP 全链路
# ==========================================================================

class TestEndToEnd(Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from flask import Flask
        cls.app = Flask("dim-e2e")
        cls.ff.register(cls.app)
        cls.ts.register(cls.app)
        for pid in ("trajectory-sketch", "file-filter"):
            src = os.path.join(PLUGIN_DIRS[pid], "backend", "config.template.json")
            dst_dir = os.path.join(cls.data_root, "plugins", pid)
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy(src, os.path.join(dst_dir, "config.json"))
        cls.ts._get_session_user = lambda: dict(USER)
        cls.ff._get_session_user = lambda: dict(USER)
        cls.client = cls.app.test_client()

    def _post_file(self, url, blob, name, **form):
        return self.client.post(url, data={"file": (io.BytesIO(blob), name), **form},
                                content_type="multipart/form-data")

    def _wait(self, url, timeout=180.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
            payload = res.get_json()
            if payload["status"] in ("done", "error"):
                return payload
            time.sleep(0.3)
        self.fail("任务超时未结束：%s" % url)

    def test_trajectory_sketch_full_flow(self):
        rows = trajectory_rows()
        n = len(rows) - 1
        blob = with_declared(make_xlsx(rows), "A1")
        res = self._post_file("/api/trajectory-sketch/upload", blob, "轨迹表.xlsx")
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        up = res.get_json()
        self.assertEqual(up["row_count"], n, "上传阶段就丢行了（声明失真未被容错）")
        self.assertTrue(up["schema"]["can_analyze"], up["schema"].get("hint"))

        res = self.client.post("/api/trajectory-sketch/analyze",
                               json={"staged_id": up["staged_id"], "mode": "hard"})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        task_id = res.get_json()["task_id"]
        payload = self._wait("/api/trajectory-sketch/result/%s" % task_id)
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertEqual(payload["quality"]["去重后轨迹点数"], n)
        self.assertGreaterEqual(len(payload["stays"]), 1)
        self.assertGreaterEqual(len(payload["trips"]), 1)

        res = self.client.get("/api/trajectory-sketch/download/%s" % task_id)
        self.assertEqual(res.status_code, 200)
        blob = res.data
        res.close()                                       # 关掉 send_file 的文件句柄
        openpyxl = importlib.import_module("openpyxl")
        wb = openpyxl.load_workbook(io.BytesIO(blob))
        try:
            self.assertIn("速写报告", wb.sheetnames)
            text = "\n".join(str(c.value) for row in wb["速写报告"].iter_rows() for c in row)
        finally:
            wb.close()
        self.assertIn("甲地", text)
        self.assertIn("乙地", text)

    def test_file_filter_full_flow(self):
        blob = read_sample(FILTER_SAMPLE) if os.path.isfile(FILTER_SAMPLE) \
            else make_xlsx([["姓名", "单位", "电话"], ["张三", "甲", "13800000000"],
                            ["李四", "乙", "13900000000"]])
        want = minimal_rect(baseline_rows(blob))
        keep = [str(h) for h in want[0][:2]]              # 按真实表头的前两列保留
        stale = with_declared(blob, "A1")
        res = self._post_file("/api/file-filter/filter", stale, "花名册.xlsx",
                              mode="hard", columns=json.dumps(keep))
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        task_id = res.get_json()["task_id"]
        payload = self._wait("/api/file-filter/result/%s" % task_id)
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertEqual(payload["rows"], len(want) - 1, "过滤后行数不足（声明失真未被容错）")

        res = self.client.get("/api/file-filter/download/%s" % task_id)
        self.assertEqual(res.status_code, 200)
        blob = res.data
        res.close()                                       # 关掉 send_file 的文件句柄
        openpyxl = importlib.import_module("openpyxl")
        wb = openpyxl.load_workbook(io.BytesIO(blob))
        try:
            got = [list(r) for r in wb.active.iter_rows(values_only=True)]
        finally:
            wb.close()
        self.assertEqual(got[0], keep)
        self.assertEqual([r[0] for r in got[1:]], [r[0] for r in want[1:]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
