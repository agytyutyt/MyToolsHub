# -*- coding: utf-8 -*-
"""过滤器「上传预览 + 按列开关 + 后处理开关」回归测试（file-filter v1.2.0）。

背景
----
办案员上传表格后应当先看到**识别到的列名**与**硬过滤的预计结果**，据此按列决定
保留/删除，并决定这次是否跑后处理（默认跑）。为此后端新增：

1. ``POST /api/file-filter/preview``（**同步**）：上传 → 识别列名 → 返回硬过滤预判
   （哪些列会被删、匹配到名单里哪个字段）与后处理预判（表头新名、该列预计替换处数），
   并把上传件**暂存**下来返回 ``staged_id``；
2. ``POST /api/file-filter/filter`` 接受 ``staged_id``（复用暂存件，用户反复调整字段
   不必重复上传）、``exclude``（用户手动关闭的列，两种模式下一律删除）、
   ``post_process=0``（显式关闭后处理）；
3. 大模型模式补一条保底：名单内**同名**字段直接保留，不把"用户点名要的列"交给模型判断。

本套件钉住的正是这几条契约与它们的边界（空数组报错、暂存过期/越权、口径一致性）。

运行：
    python -m pytest test_filter_preview.py -q
    python -m unittest test_filter_preview -v        # 无需 pytest
"""

import importlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PLUGIN_DIR = os.path.join(HERE, "plugins", "file-filter")
SAMPLE_XLSX = os.path.join(HERE, "testdata", "file-filter", "花名册.xlsx")

USER = {"username": "tester", "role_id": "role-admin", "super_admin": True}
OTHER = {"username": "someone-else", "role_id": "role-user", "super_admin": False}

# 夹具：表头含「开始时间」便于验证"后处理改表头"，数据含「开始」便于验证逐列计数
FIXTURE_HEADERS = ["开始时间", "姓名", "手机号", "备注"]
FIXTURE_ROWS = [
    ["2026-01-01 09:00", "张三", "13800001001", "开始检查"],
    ["2026-01-02 10:00", "李四", "13800001002", "开始复核"],
    ["2026-01-03 11:00", "王五", "13800001003", "正常"],
]
# 规则一改表头（开始时间→时间），规则二改数据（手机号打码）
FIXTURE_RULES = [
    {"pattern": "开始", "replacement": "", "is_regex": False, "enabled": True},
    {"pattern": r"(\d{3})\d{4}(\d{4})", "replacement": r"\1****\2",
     "is_regex": True, "enabled": True},
]


def make_xlsx(rows, sheet="Sheet1") -> bytes:
    openpyxl = importlib.import_module("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def load_plugin(pkg_name, plugin_dir):
    """复刻框架的 jztools_<id> 动态加载（与 test_xlsx_stale_dimension.py 同口径）。"""
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
        from flask import Flask
        cls.tmp = tempfile.mkdtemp(prefix="jz-ffprev-")
        cls.data_root = os.path.join(cls.tmp, "data")
        os.environ["JZTOOLS_DATA_ROOT"] = cls.data_root
        cls.ff = load_plugin("jztools_file_filter", PLUGIN_DIR)
        cls.core = cls.ff.core
        cls.ff._get_session_user = lambda: dict(USER)
        cls.app = Flask("ff-preview")
        cls.ff.register(cls.app)
        cls.client = cls.app.test_client()
        cls.write_config({"keep_columns": ["姓名", "所属单位"],
                          "post_rules": list(FIXTURE_RULES)})

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("JZTOOLS_DATA_ROOT", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def write_config(cls, cfg):
        """直写数据根目录配置（隔离在临时目录，绝不碰真实数据根）。"""
        path = cls.ff.CONFIG_FILE
        os.makedirs(os.path.dirname(path), exist_ok=True)
        full = {"llm": {"base_url": "", "api_key": "", "model": ""},
                "keep_columns": [], "post_rules": []}
        full.update(cfg)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(full, f, ensure_ascii=False)

    # ---- 请求助手 ----

    def post_file(self, url, blob, name, **form):
        return self.client.post(url, data={"file": (io.BytesIO(blob), name), **form},
                                content_type="multipart/form-data")

    def preview(self, blob=None, name="夹具.xlsx", **form):
        blob = make_xlsx([FIXTURE_HEADERS] + FIXTURE_ROWS) if blob is None else blob
        res = self.post_file("/api/file-filter/preview", blob, name, **form)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:400])
        return res.get_json()

    def wait(self, task_id, timeout=180.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.client.get("/api/file-filter/result/%s" % task_id)
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
            payload = res.get_json()
            if payload["status"] in ("done", "error"):
                return payload
            time.sleep(0.2)
        self.fail("任务超时未结束：%s" % task_id)

    def filter_and_wait(self, **form):
        """提交过滤并等结果（默认复用 /preview 的暂存件，不重复上传）。"""
        res = self.client.post("/api/file-filter/filter", data=form)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:400])
        return self.wait(res.get_json()["task_id"])

    def download_rows(self, task_id):
        openpyxl = importlib.import_module("openpyxl")
        res = self.client.get("/api/file-filter/download/%s" % task_id)
        self.assertEqual(res.status_code, 200)
        blob = res.data
        res.close()                                   # 关掉 send_file 的文件句柄
        wb = openpyxl.load_workbook(io.BytesIO(blob))
        try:
            return [list(r) for r in wb.active.iter_rows(values_only=True)]
        finally:
            wb.close()

    def cols(self, preview, name):
        return [c for c in preview["columns"] if c["name"] == name][0]

    def _reset_store(self):
        # clear() 按设计保留 stats（历史计数），用例隔离必须连文件一起删
        if os.path.isfile(self.ff.MAPPINGS_FILE):
            os.remove(self.ff.MAPPINGS_FILE)
        self.ff.MAPPINGS.reload()


# ==========================================================================
# 1. 上传预览：识别到的列名 + 硬过滤预判
# ==========================================================================

class TestPreview(Base):

    def test_recognizes_columns_and_predicts(self):
        """识别到全部列名，并给出"哪些会被删"的预判（按名单精确匹配）。"""
        p = self.preview()
        self.assertEqual([c["name"] for c in p["columns"]], FIXTURE_HEADERS)
        self.assertEqual([c["keep"] for c in p["columns"]], [False, True, False, False])
        self.assertEqual(self.cols(p, "姓名")["matched"], "姓名")
        self.assertEqual(self.cols(p, "手机号")["matched"], "")
        self.assertEqual(p["summary"], {"total": 4, "keep": 1, "drop": 3, "replace": 0})
        self.assertEqual(p["rows"], len(FIXTURE_ROWS))
        self.assertTrue(p["staged_id"])

    def test_post_rules_shown_with_per_column_effect(self):
        """后处理规则随预览回传，且逐列给出"表头新名 + 预计替换处数"。"""
        p = self.preview()
        self.assertEqual([r["pattern"] for r in p["post_rules"]],
                         [r["pattern"] for r in FIXTURE_RULES])
        # 开始时间：表头「开始时间」→「时间」1 处
        self.assertEqual(self.cols(p, "开始时间")["post_name"], "时间")
        self.assertEqual(self.cols(p, "开始时间")["replace_count"], 1)
        # 手机号：3 行打码 3 处（表头不含 11 位数字）
        self.assertEqual(self.cols(p, "手机号")["replace_count"], 3)
        # 备注：2 行含「开始」→ 删 2 处
        self.assertEqual(self.cols(p, "备注")["post_name"], "备注")
        self.assertEqual(self.cols(p, "备注")["replace_count"], 2)
        # 汇总只统计**会被保留**的列（口径与运行结果一致：后处理跑在过滤后的表上）
        self.assertEqual(p["summary"]["replace"], 0)          # 汇总只算保留列，「姓名」不含规则命中

    def test_keep_none_locks_blank_headers_and_flags_dups(self):
        """空表头列不可切换（按名字保留不可能）；同名重复列标记 dup（点击会一起切换）。"""
        blob = make_xlsx([["姓名", "", "姓名"], ["张三", "x", "李四"]])
        p = self.preview(blob)
        blank = p["columns"][1]
        self.assertEqual(blank["name"], "")
        self.assertTrue(blank["locked"])
        self.assertFalse(blank["keep"])
        self.assertTrue(p["columns"][0]["dup"])
        self.assertTrue(p["columns"][2]["dup"])
        self.assertFalse(p["columns"][0]["locked"])

    def test_preview_rejects_bad_input(self):
        """预览与过滤同一套上传校验（扩展名 / 空文件 / 大小）。"""
        res = self.post_file("/api/file-filter/preview", b"hello", "x.txt")
        self.assertEqual(res.status_code, 400)
        res = self.post_file("/api/file-filter/preview", b"", "empty.xlsx")
        self.assertEqual(res.status_code, 400)
        res = self.client.post("/api/file-filter/preview", data={})
        self.assertEqual(res.status_code, 400)

    def test_preview_failure_drops_stage(self):
        """预览失败（表格为空）不留暂存件，也不占 TTL。"""
        blob = make_xlsx([])                       # 空工作表：连表头都没有
        before = len(self.ff.STAGES)
        res = self.post_file("/api/file-filter/preview", blob, "空表.xlsx")
        self.assertEqual(res.status_code, 400, res.get_data(as_text=True)[:200])
        self.assertEqual(len(self.ff.STAGES), before, "预览失败却留下了暂存件")


# ==========================================================================
# 2. 按列开关：点击胶囊决定"这一列是否过滤"
# ==========================================================================

class TestColumnToggles(Base):

    def test_on_list_keeps_exactly_clicked_columns(self):
        """点开的列全保留（含管理员名单之外的列），点掉的列全删除。"""
        p = self.preview()
        on = ["姓名", "备注"]                       # 备注不在名单里，用户点开即保留
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="hard",
                                  columns=json.dumps(on, ensure_ascii=False))
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        rows = self.download_rows(payload["task_id"])
        self.assertEqual(rows[0], on)
        self.assertEqual([r[0] for r in rows[1:]], ["张三", "李四", "王五"])

    def test_exclude_wins_over_keep_list(self):
        """名单里有、但用户手动关闭的列 → 删除（用户决定优先于配置）。"""
        p = self.preview()
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="hard",
                                  columns=json.dumps(["姓名"], ensure_ascii=False),
                                  exclude=json.dumps(["所属单位"], ensure_ascii=False))
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertEqual([k["column"] for k in payload["kept"]], ["姓名"])
        self.assertNotIn("所属单位", self.download_rows(payload["task_id"])[0])

    def test_explicit_empty_columns_is_rejected(self):
        """显式传空数组 = 用户把字段全关了：报错，不静默回退管理员名单。"""
        res = self.client.post("/api/file-filter/filter",
                               data={"staged_id": self.preview()["staged_id"],
                                     "mode": "hard", "columns": "[]"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("未选择任何保留字段", res.get_json()["error"])

    def test_missing_columns_falls_back_to_config(self):
        """没传 columns（老调用方）仍按管理员名单过滤——兼容口径不许变。"""
        payload = self.filter_and_wait(staged_id=self.preview()["staged_id"], mode="hard")
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertEqual([k["column"] for k in payload["kept"]], ["姓名"])

    def test_legacy_upload_without_preview_still_works(self):
        """不经过预览、直接传文件的老流程照常可用（既有调用方与验收脚本依赖）。"""
        blob = make_xlsx([FIXTURE_HEADERS] + FIXTURE_ROWS)
        res = self.post_file("/api/file-filter/filter", blob, "夹具.xlsx",
                             mode="hard", columns=json.dumps(["姓名", "手机号"],
                                                             ensure_ascii=False))
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        payload = self.wait(res.get_json()["task_id"])
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        rows = self.download_rows(payload["task_id"])
        self.assertEqual(rows[0], ["姓名", "手机号"])


# ==========================================================================
# 3. 后处理开关（默认开启）
# ==========================================================================

class TestPostProcessToggle(Base):

    def test_enabled_by_default(self):
        p = self.preview()
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="hard",
                                  columns=json.dumps(["开始时间", "手机号"], ensure_ascii=False))
        self.assertTrue(payload["post_enabled"])
        rows = self.download_rows(payload["task_id"])
        self.assertEqual(rows[0], ["时间", "手机号"])              # 表头被规则改写
        self.assertEqual(rows[1][1], "138****1001")               # 单元格被打码
        self.assertEqual(payload["replace_count"], 1 + 3)         # 表头 1 处 + 数据 3 处

    def test_disabled_keeps_text_intact(self):
        p = self.preview()
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="hard",
                                  columns=json.dumps(["开始时间", "手机号"], ensure_ascii=False),
                                  post_process="0")
        self.assertFalse(payload["post_enabled"])
        self.assertEqual(payload["replace_count"], 0)
        rows = self.download_rows(payload["task_id"])
        self.assertEqual(rows[0], ["开始时间", "手机号"])
        self.assertEqual(rows[1][1], "13800001001")

    def test_apply_endpoint_honors_flags(self):
        """/apply（程序化接口）同样支持 exclude 与 post_process=false。"""
        body = {"rows": [FIXTURE_HEADERS] + FIXTURE_ROWS, "mode": "hard",
                "columns": ["姓名", "手机号"], "exclude": ["手机号"],
                "post_rules": FIXTURE_RULES, "post_process": False}
        res = self.client.post("/api/file-filter/apply", json=body)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        data = res.get_json()
        self.assertEqual(data["rows"][0], ["姓名"])               # exclude 生效
        self.assertEqual(data["replace_count"], 0)                # 后处理被关掉
        self.assertEqual(data["rows"][1][0], "张三")


# ==========================================================================
# 4. 暂存复用与边界（越权 / 过期 / 非法 ID）
# ==========================================================================

class TestStage(Base):

    def test_stage_reused_without_reupload(self):
        """带 staged_id 提交（不带文件）即可过滤，且沿用了预览阶段的背景图结论。"""
        p = self.preview()
        res = self.client.post("/api/file-filter/filter",
                               data={"staged_id": p["staged_id"], "mode": "hard",
                                     "columns": json.dumps(["姓名"], ensure_ascii=False)})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        self.assertEqual(res.get_json()["sanitize"], p["sanitize"])
        payload = self.wait(res.get_json()["task_id"])
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertEqual(payload["rows"], len(FIXTURE_ROWS))

    def test_stage_is_owner_scoped(self):
        """别人的 staged_id 一律按"不存在"处理（404，不暴露资源存在性）。"""
        p = self.preview()
        self.ff._get_session_user = lambda: dict(OTHER)
        try:
            res = self.client.post("/api/file-filter/filter",
                                   data={"staged_id": p["staged_id"], "mode": "hard",
                                         "columns": json.dumps(["姓名"], ensure_ascii=False)})
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.get_json().get("code"), "staged_expired")
        finally:
            self.ff._get_session_user = lambda: dict(USER)

    def test_unknown_stage_reports_expired(self):
        """过期/伪造的 staged_id → 404 + code=staged_expired（前端据此自动改传文件）。"""
        res = self.client.post("/api/file-filter/filter",
                               data={"staged_id": "deadbeef0000", "mode": "hard",
                                     "columns": json.dumps(["姓名"], ensure_ascii=False)})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.get_json().get("code"), "staged_expired")
        res = self.client.post("/api/file-filter/filter",
                               data={"staged_id": "../../etc/passwd", "mode": "hard"})
        self.assertEqual(res.status_code, 400)                    # 非法 ID → 未选择文件


# ==========================================================================
# 5. 大模型模式：同名保底 + exclude 优先
# ==========================================================================

class TestLLMMode(Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 大模型模式有"未配置不进队列"的前置校验（8.5-3）；本组只验匹配与开关。
        # 统一大模型落地后凭据归框架模块 jz_llm（插件不再保存 Key），
        # 故这里配置**框架模块**的全局接入信息（仍落在临时数据根内），
        # 并 monkeypatch 掉真实网络调用。
        import jz_llm
        cls.jz_llm = jz_llm
        jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "http://127.0.0.1:9/v1/chat/completions",
            "api_key": "sk-test", "model": "test-model"}})

    @classmethod
    def tearDownClass(cls):
        # 还原为"未配置"，避免影响后续用例（同一临时数据根内）
        cls.jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "", "api_key": "", "model": ""}})
        super().tearDownClass()

    def setUp(self):
        self._orig = self.ff.llm_client.match_columns

    def tearDown(self):
        self.ff.llm_client.match_columns = self._orig

    def test_same_name_kept_even_if_model_says_nothing(self):
        """模型对名单内同名字段返回空值（模型噪声）时，用户点名要的列仍保留。"""
        self.ff.llm_client.match_columns = lambda *a, **k: {h: "" for h in FIXTURE_HEADERS}
        payload = self.filter_and_wait(staged_id=self.preview()["staged_id"], mode="llm",
                                  columns=json.dumps(["姓名"], ensure_ascii=False))
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertTrue(payload["llm_used"])
        self.assertEqual([k["column"] for k in payload["kept"]], ["姓名"])

    def test_exclude_beats_semantic_match(self):
        """模型语义命中的列，被用户手动关闭 → 删除（用户决定优先）。"""
        self.ff.llm_client.match_columns = lambda headers, *a, **k: {
            h: ("姓名" if h in ("姓名", "备注") else "") for h in headers}
        p = self.preview()
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="llm",
                                  columns=json.dumps(["姓名", "备注"], ensure_ascii=False),
                                  exclude=json.dumps(["备注"], ensure_ascii=False))
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertEqual([k["column"] for k in payload["kept"]], ["姓名"])
        self.assertEqual(payload["removed"], ["开始时间", "手机号", "备注"])


# ==========================================================================
# 6. 预演口径与实际结果一致 + 端到端
# ==========================================================================

class TestConsistencyAndEndToEnd(Base):

    def test_preview_counts_match_actual_run(self):
        """预览给的「预计替换 N 处」必须等于实际运行报出的 replace_count（保留列口径）。"""
        p = self.preview()
        on = ["开始时间", "手机号", "备注"]
        want = sum(c["replace_count"] for c in p["columns"] if c["name"] in on)
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="hard",
                                  columns=json.dumps(on, ensure_ascii=False))
        self.assertEqual(payload["replace_count"], want)
        self.assertEqual(want, 1 + 3 + 2)

    def test_post_process_columns_matches_post_process(self):
        """逐列预演与整表后处理的计数口径一致（两处实现不许漂移）。"""
        headers, rows = FIXTURE_HEADERS, [list(r) for r in FIXTURE_ROWS]
        per_col = self.core.post_process_columns(headers, rows, FIXTURE_RULES)
        headers2, _, count = self.core.post_process(headers, rows, FIXTURE_RULES)
        self.assertEqual(sum(c for _, c in per_col), count)
        self.assertEqual([h for h, _ in per_col], headers2)

    def test_real_sample_full_flow(self):
        """真实样本（testdata/花名册.xlsx）：预览 → 按列选择 → 过滤 → 下载全链路。"""
        if not os.path.isfile(SAMPLE_XLSX):
            self.skipTest("真实样本不存在：%s" % SAMPLE_XLSX)
        with open(SAMPLE_XLSX, "rb") as f:
            blob = f.read()
        p = self.preview(blob, "花名册.xlsx")
        self.assertIn("身份证号码", [c["name"] for c in p["columns"]])
        self.assertEqual(self.cols(p, "姓名")["keep"], True)
        self.assertEqual(self.cols(p, "身份证号码")["keep"], False)

        on = ["姓名", "所属单位", "手机号"]
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="hard",
                                       columns=json.dumps(on, ensure_ascii=False))
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertEqual(payload["rows"], 20)
        rows = self.download_rows(payload["task_id"])
        # 输出列序 = 文档原列序（与用户点击顺序无关）
        self.assertEqual(rows[0], ["姓名", "手机号", "所属单位"])
        self.assertEqual(rows[1][1], "138****1001")               # 手机号已脱敏
        self.assertEqual(len(rows), 21)


# ==========================================================================
# 7. 自学习映射缓存（P0）：二次运行免 LLM、用户把关升级、/apply 保守口径
# ==========================================================================

class TestLLMMappingCache(Base):
    """mapping_store 接入过滤管线的端到端契约（stub 掉网络，LLM 调用计数断言）。

    钉住：① 未命中子集才进 LLM（名单内同名不进）；② 同表头二次运行不再调 LLM；
    ③ confirm 后来源升级为 confirmed；④ reject 后同一建议对不回锅；⑤ /apply 缺省
    只吃 confirmed（use_suggested 显式开启才放宽）；⑥ /mappings 接口权限口径。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # /filter 的 llm 模式有「未配置不进队列」前置校验；匹配本身由 stub 替换
        import jz_llm
        cls.jz_llm = jz_llm
        jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "http://127.0.0.1:9/v1/chat/completions",
            "api_key": "sk-test", "model": "test-model"}})

    @classmethod
    def tearDownClass(cls):
        cls.jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "", "api_key": "", "model": ""}})
        super().tearDownClass()

    def setUp(self):
        self._reset_store()
        self.calls = []
        self._orig_match = self.ff.llm_client.match_columns
        test = self

        def fake_match(headers, *args, **kwargs):
            test.calls.append(list(headers))
            # 语义匹配口径：备注 → 姓名（开始时间/手机号与名单无关联）
            return {h: ("姓名" if h == "备注" else "") for h in headers}

        self.ff.llm_client.match_columns = fake_match

    def tearDown(self):
        self.ff.llm_client.match_columns = self._orig_match
        self._reset_store()

    def _run_llm_filter(self):
        p = self.preview()
        return self.filter_and_wait(staged_id=p["staged_id"], mode="llm")

    def test_first_run_uses_llm_for_misses_only(self):
        """首次运行：名单内同名（姓名）不进 LLM，未命中子集（3 列）才进。"""
        payload = self._run_llm_filter()
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertTrue(payload["llm_used"])
        self.assertEqual(self.calls, [["开始时间", "手机号", "备注"]])
        self.assertEqual([k["column"] for k in payload["kept"]], ["姓名", "备注"])
        self.assertEqual(payload["kept"][0]["source"], "exact")
        self.assertEqual(payload["kept"][1]["source"], "llm")
        self.assertEqual(payload["removed"], ["开始时间", "手机号"])
        stats = self.ff.MAPPINGS.stats()
        self.assertEqual(stats["llm_calls"], 1)
        self.assertEqual(stats["misses"], 3)

    def test_second_run_skips_llm_and_reports_suggested(self):
        """同表头二次运行直接读建议映射（含「无关联→删除」的负建议），不再调 LLM。"""
        self._run_llm_filter()
        payload = self._run_llm_filter()
        self.assertEqual(len(self.calls), 1)  # 第二次没有再调 LLM
        self.assertFalse(payload["llm_used"])
        self.assertEqual([k["column"] for k in payload["kept"]], ["姓名", "备注"])
        self.assertEqual(payload["kept"][1]["source"], "suggested")
        # 负建议（开始时间/手机号确认无关）与正建议（备注→姓名）都在映射明细里可复核
        self.assertEqual(payload["mappings_used"], [
            {"key": "开始时间", "sample": "开始时间", "target": "",
             "status": "suggested", "source": "suggested"},
            {"key": "手机号", "sample": "手机号", "target": "",
             "status": "suggested", "source": "suggested"},
            {"key": "备注", "sample": "备注", "target": "姓名",
             "status": "suggested", "source": "suggested"},
        ])
        stats = self.ff.MAPPINGS.stats()
        self.assertEqual(stats["llm_calls"], 1)
        self.assertGreaterEqual(stats["llm_saved"], 1)
        self.assertGreaterEqual(stats["hit_suggested"], 1)

    def test_confirm_upgrades_source_and_reject_blocks_pair(self):
        """✅确认后来源升级 confirmed；❌否决后同一建议对不再回锅（删除、不再记录）。"""
        self._run_llm_filter()
        res = self.client.post("/api/file-filter/mappings/confirm",
                               json={"items": [{"key": "备注", "target": "姓名"}]})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        payload = self._run_llm_filter()
        self.assertEqual(len(self.calls), 1)  # confirmed 命中，仍只有首次那次 LLM
        self.assertEqual(payload["kept"][1]["source"], "confirmed")
        used = {m["key"]: m for m in payload["mappings_used"]}
        self.assertEqual(used["备注"]["status"], "confirmed")

        # 否决后：条目删除 → 备注变 miss → LLM 再答同一对 → 拦截（不入库也不生效）
        res = self.client.post("/api/file-filter/mappings/reject",
                               json={"key": "备注", "target": "姓名"})
        self.assertEqual(res.status_code, 200)
        payload = self._run_llm_filter()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([k["column"] for k in payload["kept"]], ["姓名"])
        self.assertEqual(payload["removed"], ["开始时间", "手机号", "备注"])
        stats = self.ff.MAPPINGS.stats()
        self.assertEqual(stats["llm_calls"], 2)

    def test_apply_default_confirmed_only_use_suggested_opt_in(self):
        """/apply 缺省只吃 confirmed（建议不参与、每次都调 LLM）；显式开启才放宽。"""
        rows = [FIXTURE_HEADERS] + FIXTURE_ROWS
        body = {"rows": rows, "mode": "llm"}
        r1 = self.client.post("/api/file-filter/apply", json=body)
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True)[:300])
        kept = {k["column"]: k["source"] for k in r1.get_json()["kept"]}
        self.assertEqual(kept, {"姓名": "exact", "备注": "llm"})
        self.assertEqual(len(self.calls), 1)

        r2 = self.client.post("/api/file-filter/apply", json=body)
        kept = {k["column"]: k["source"] for k in r2.get_json()["kept"]}
        self.assertEqual(kept["备注"], "llm")
        self.assertEqual(len(self.calls), 2)  # 建议存在但缺省不吃 → 再次调 LLM

        r3 = self.client.post("/api/file-filter/apply", json={**body, "use_suggested": True})
        kept = {k["column"]: k["source"] for k in r3.get_json()["kept"]}
        self.assertEqual(kept["备注"], "suggested")
        self.assertEqual(len(self.calls), 2)  # 建议命中，LLM 没有再调

    def test_mappings_endpoint_permissions(self):
        """权限口径：确认/否决=登录操作员；列表/删除/清空/导出=管理员。"""
        self._run_llm_filter()
        orig = self.ff._get_session_user
        try:
            self.ff._get_session_user = lambda: dict(OTHER)
            # 操作员（登录非管理员）可以确认与否决
            res = self.client.post("/api/file-filter/mappings/confirm",
                                   json={"items": [{"key": "备注", "target": "姓名"}]})
            self.assertEqual(res.status_code, 200)
            res = self.client.post("/api/file-filter/mappings/reject",
                                   json={"key": "备注", "target": "姓名"})
            self.assertEqual(res.status_code, 200)
            # 管理动作对操作员一律 403
            for method, url, kwargs in (
                    ("get", "/api/file-filter/mappings", {}),
                    ("post", "/api/file-filter/mappings/delete",
                     {"json": {"key": "备注"}}),
                    ("post", "/api/file-filter/mappings/clear", {}),
                    ("get", "/api/file-filter/mappings/export", {})):
                res = getattr(self.client, method)(url, **kwargs)
                self.assertEqual(res.status_code, 403, url)
        finally:
            self.ff._get_session_user = orig
        # 管理员可用
        res = self.client.get("/api/file-filter/mappings")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertIn("entries", payload)
        self.assertIn("stats", payload)
        self.assertEqual(payload["keep_columns"], ["姓名", "所属单位"])

    def test_preview_prefills_cached_matches(self):
        """/preview 的 match 预填：缓存命中列带 {target,status}（含负映射），
        名单内同名与无缓存列保持 None；硬过滤预判 keep/matched 口径不变（向后兼容）。"""
        # 无缓存时：match 全空
        p = self.preview()
        for c in p["columns"]:
            self.assertIsNone(c["match"], c["name"])
        self._run_llm_filter()  # 产生建议：备注→姓名、开始时间/手机号→删除
        p = self.preview()
        by = {c["name"]: c for c in p["columns"]}
        self.assertIsNone(by["姓名"]["match"])  # 名单内同名不走缓存
        self.assertTrue(by["姓名"]["keep"])     # 硬过滤预判口径不变
        self.assertEqual(by["备注"]["match"], {"target": "姓名", "status": "suggested"})
        self.assertEqual(by["开始时间"]["match"], {"target": "", "status": "suggested"})
        self.assertEqual(by["手机号"]["match"], {"target": "", "status": "suggested"})
        # 确认后升级为 confirmed
        self.client.post("/api/file-filter/mappings/confirm",
                         json={"items": [{"key": "备注", "target": "姓名"}]})
        p = self.preview()
        by = {c["name"]: c for c in p["columns"]}
        self.assertEqual(by["备注"]["match"], {"target": "姓名", "status": "confirmed"})

    def test_mapping_limit_configurable(self):
        """自动学习上限管理员可配：POST /config 校验/夹紧，/mappings 回传 limit 与 count。"""
        res = self.client.post("/api/file-filter/config", json={"mapping_limit": 500})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        self.assertEqual(self.client.get("/api/file-filter/config").get_json()["mapping_limit"],
                         500)
        self._run_llm_filter()
        payload = self.client.get("/api/file-filter/mappings").get_json()
        self.assertEqual(payload["limit"], 500)
        self.assertEqual(payload["count"], 3)  # 备注 + 开始时间 + 手机号（负映射）
        # 越界夹紧到下界；非整数 → 400
        res = self.client.post("/api/file-filter/config", json={"mapping_limit": 5})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get("/api/file-filter/config").get_json()["mapping_limit"],
                         100)
        res = self.client.post("/api/file-filter/config", json={"mapping_limit": "abc"})
        self.assertEqual(res.status_code, 400)

    def test_export_golden_set(self):
        """黄金评测集导出：默认只含 confirmed；orphan（名单外目标）不进评测集。"""
        self._run_llm_filter()
        self.client.post("/api/file-filter/mappings/confirm",
                         json={"items": [{"key": "备注", "target": "姓名"}]})
        res = self.client.get("/api/file-filter/mappings/export")
        self.assertEqual(res.status_code, 200)
        self.assertIn("姓名".encode("utf-8"), res.data)
        res = self.client.get("/api/file-filter/mappings/export?format=jsonl")
        self.assertEqual(res.status_code, 200)
        first = json.loads(res.get_data(as_text=True).splitlines()[0])
        self.assertEqual(first, {"header": "备注", "target": "姓名", "status": "confirmed"})


# ==========================================================================
# 8. 保留字段名单批量导入（管理员）：解析第一列、跳过表头位/重复，不入库
# ==========================================================================

class TestKeepColumnsImport(Base):
    """POST /config/import-columns 契约：只解析返回清单，保存仍走「保存配置」。"""

    def test_import_parses_first_column(self):
        rows = [["字段名称"], ["姓名"], ["所属单位"], ["姓名"], ["手机号"]]
        blob = make_xlsx(rows)
        res = self.post_file("/api/file-filter/config/import-columns", blob, "名单.xlsx")
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        data = res.get_json()
        self.assertEqual(data["columns"], ["姓名", "所属单位", "手机号"])
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["skipped"], 2)       # 表头位 + 重复
        # 只解析不入库：配置仍是原名单，等「保存配置」才生效
        self.assertEqual(self.client.get("/api/file-filter/config").get_json()["keep_columns"],
                         ["姓名", "所属单位"])

    def test_import_csv_with_bom(self):
        blob = "字段名称\n姓名\n开始时间\n".encode("utf-8-sig")
        res = self.post_file("/api/file-filter/config/import-columns", blob, "名单.csv")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["columns"], ["姓名", "开始时间"])

    def test_import_requires_admin_and_valid_ext(self):
        orig = self.ff._get_session_user
        try:
            self.ff._get_session_user = lambda: dict(OTHER)
            res = self.client.post("/api/file-filter/config/import-columns")
            self.assertEqual(res.status_code, 403)
        finally:
            self.ff._get_session_user = orig
        res = self.post_file("/api/file-filter/config/import-columns", b"x", "名单.txt")
        self.assertEqual(res.status_code, 400)


# ==========================================================================
# 9. 匹配修正（P3b）：范围只含建议级（confirmed 不动）、分批重判、写回与 diff
# ==========================================================================

class TestMappingRecheck(Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import jz_llm
        cls.jz_llm = jz_llm
        jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "http://127.0.0.1:9/v1/chat/completions",
            "api_key": "sk-test", "model": "test-model"}})

    @classmethod
    def tearDownClass(cls):
        cls.jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "", "api_key": "", "model": ""}})
        super().tearDownClass()

    def setUp(self):
        self._reset_store()
        self._orig_match = self.ff.llm_client.match_columns
        self._orig_recheck = self.ff.llm_client.recheck_mappings
        self.ff.llm_client.match_columns = lambda headers, *a, **k: {
            h: ("姓名" if h == "备注" else "") for h in headers}
        # 复核重判：备注 → 所属单位（制造 diff），其余维持原判
        self.ff.llm_client.recheck_mappings = lambda pairs, keep, **k: {
            p["key"]: ("所属单位" if p["key"] == "备注" else p["target"]) for p in pairs}

    def tearDown(self):
        self.ff.llm_client.match_columns = self._orig_match
        self.ff.llm_client.recheck_mappings = self._orig_recheck
        self._reset_store()

    def _seed(self):
        """首轮过滤产生建议（备注→姓名；开始时间/手机号→删除），并把删除采纳为已确认。"""
        p = self.preview()
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="llm")
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        res = self.client.post("/api/file-filter/mappings/confirm", json={"items": [
            {"key": "开始时间", "target": ""}, {"key": "手机号", "target": ""}]})
        self.assertEqual(res.status_code, 200)

    def _poll_recheck(self, task_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.client.get("/api/file-filter/mappings/recheck/" + task_id)
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
            payload = res.get_json()
            if payload["status"] in ("done", "error"):
                return payload
            time.sleep(0.2)
        self.fail("匹配修正任务超时")

    def test_recheck_scope_diff_and_adopt(self):
        """范围只含建议级（confirmed 不进 pairs）；diff 写回建议级；采纳升级 confirmed。"""
        self._seed()
        res = self.client.post("/api/file-filter/mappings/recheck",
                               json={"scope": "all_suggested"})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        payload = self._poll_recheck(res.get_json()["task_id"])
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertEqual(payload["total"], 1)          # 仅备注（suggested）
        self.assertEqual([d["key"] for d in payload["diffs"]], ["备注"])
        self.assertEqual((payload["diffs"][0]["old_target"],
                          payload["diffs"][0]["new_target"]), ("姓名", "所属单位"))
        self.assertEqual(payload["outcomes"]["updated"], 1)
        entry = self.ff.MAPPINGS.get_entry("备注")
        self.assertEqual(entry["target"], "所属单位")
        self.assertEqual(entry["status"], "suggested")  # 写回建议级，未升级
        # 采纳 → confirmed
        res = self.client.post("/api/file-filter/mappings/confirm",
                               json={"items": [{"key": "备注", "target": "所属单位"}]})
        self.assertEqual(res.status_code, 200)
        entry = self.ff.MAPPINGS.get_entry("备注")
        self.assertEqual(entry["status"], "confirmed")
        # confirmed 条目未被复核翻案
        confirmed = self.ff.MAPPINGS.get_entry("开始时间")
        self.assertEqual((confirmed["target"], confirmed["status"]), ("", "confirmed"))

    def test_recheck_permission_empty_and_bad_id(self):
        self._seed()
        orig = self.ff._get_session_user
        try:
            self.ff._get_session_user = lambda: dict(OTHER)
            self.assertEqual(
                self.client.post("/api/file-filter/mappings/recheck").status_code, 403)
            self.assertEqual(
                self.client.get("/api/file-filter/mappings/recheck/abc123").status_code, 403)
        finally:
            self.ff._get_session_user = orig
        # 清空后无建议条目 → 400
        self.ff.MAPPINGS.clear()
        self.assertEqual(
            self.client.post("/api/file-filter/mappings/recheck").status_code, 400)
        # 非法任务 ID → 400
        self.assertEqual(
            self.client.get("/api/file-filter/mappings/recheck/!!bad!").status_code, 400)

    def test_recheck_single_flight_409(self):
        """同一时刻只允许一个复核任务：占住锁再提交 → 409。"""
        self._seed()
        if not self.ff._recheck_lock.acquire(False):
            self.fail("锁应空闲")
        try:
            res = self.client.post("/api/file-filter/mappings/recheck")
            self.assertEqual(res.status_code, 409)
        finally:
            self.ff._recheck_lock.release()


# ==========================================================================
# 10. 双重过滤缺省化（v1.5）：不再传 mode 时默认双重过滤；LLM 未配置自动降级
# ==========================================================================

class TestDoubleFilterDefault(Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import jz_llm
        cls.jz_llm = jz_llm
        jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "http://127.0.0.1:9/v1/chat/completions",
            "api_key": "sk-test", "model": "test-model"}})

    @classmethod
    def tearDownClass(cls):
        cls.jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "", "api_key": "", "model": ""}})
        super().tearDownClass()

    def setUp(self):
        self._reset_store()
        self._orig_match = self.ff.llm_client.match_columns
        self.calls = []
        test = self

        def fake_match(headers, *args, **kwargs):
            test.calls.append(list(headers))
            return {h: ("姓名" if h == "备注" else "") for h in headers}

        self.ff.llm_client.match_columns = fake_match

    def tearDown(self):
        self.ff.llm_client.match_columns = self._orig_match
        self._reset_store()

    def test_filter_without_mode_runs_double_filter(self):
        """/filter 不传 mode → 默认双重过滤：精确直接保留，未命中子集进大模型。"""
        p = self.preview()
        payload = self.filter_and_wait(staged_id=p["staged_id"])   # 不传 mode
        self.assertEqual(payload["status"], "done", payload.get("detail"))
        self.assertTrue(payload["llm_used"])
        self.assertIsNone(payload.get("degraded"))
        self.assertEqual([k["column"] for k in payload["kept"]], ["姓名", "备注"])
        self.assertEqual(payload["kept"][0]["source"], "exact")
        self.assertEqual(payload["kept"][1]["source"], "llm")
        self.assertEqual(self.calls, [["开始时间", "手机号", "备注"]])

    def test_degrade_to_hard_when_llm_unconfigured(self):
        """大模型未配置：自动降级为仅精确匹配，degraded 标记与原因随结果提示。"""
        self.jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "", "api_key": "", "model": ""}})
        try:
            p = self.preview()
            payload = self.filter_and_wait(staged_id=p["staged_id"])   # 不传 mode
            self.assertEqual(payload["status"], "done", payload.get("detail"))
            self.assertFalse(payload["llm_used"])
            self.assertTrue(payload["degraded"])
            self.assertTrue(payload["degraded_reason"])
            # 仅精确匹配：备注/开始时间/手机号全部删除（hard_unmatched；夹具无「所属单位」列）
            self.assertEqual([k["column"] for k in payload["kept"]], ["姓名"])
            sources = {d["column"]: d["source"] for d in payload["removed_detail"]}
            self.assertEqual(sources["备注"], "hard_unmatched")
            self.assertEqual(sources["开始时间"], "hard_unmatched")
            # 降级路径不产生任何记忆写入（建议级不参与）
            self.assertEqual(self.ff.MAPPINGS.count(), 0)
        finally:
            self.jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
                "format": "openai", "url": "http://127.0.0.1:9/v1/chat/completions",
                "api_key": "sk-test", "model": "test-model"}})


# ==========================================================================
# 11. P4：remap 端点与管线临时保留、计票端点、recheck 范围、样本值注入
# ==========================================================================

class TestMappingP4(Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import jz_llm
        cls.jz_llm = jz_llm
        jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "http://127.0.0.1:9/v1/chat/completions",
            "api_key": "sk-test", "model": "test-model"}})

    @classmethod
    def tearDownClass(cls):
        cls.jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "", "api_key": "", "model": ""}})
        super().tearDownClass()

    def setUp(self):
        self._reset_store()
        self._orig_match = self.ff.llm_client.match_columns
        self._orig_recheck = self.ff.llm_client.recheck_mappings
        self.samples_seen = []
        test = self

        def fake_match(headers, *args, **kwargs):
            test.samples_seen.append(kwargs.get("samples"))
            return {h: ("姓名" if h == "备注" else "") for h in headers}

        self.ff.llm_client.match_columns = fake_match
        self.ff.llm_client.recheck_mappings = lambda pairs, keep, **k: {
            p["key"]: p["target"] for p in pairs}

    def tearDown(self):
        self.ff.llm_client.match_columns = self._orig_match
        self.ff.llm_client.recheck_mappings = self._orig_recheck
        self._reset_store()

    def _seed_suggestions(self):
        p = self.preview()
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="llm")
        self.assertEqual(payload["status"], "done", payload.get("detail"))

    def _poll_recheck(self, task_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.client.get("/api/file-filter/mappings/recheck/" + task_id)
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
            payload = res.get_json()
            if payload["status"] in ("done", "error"):
                return payload
            time.sleep(0.2)
        self.fail("匹配修正任务超时")

    def test_samples_injected_into_match_prompt(self):
        """样本值注入（PROMPT_VER=2）：LLM 收到每个未命中表头的前 3 个非空样本值。"""
        self._seed_suggestions()
        self.assertEqual(self.ff.llm_client.PROMPT_VER, 2)
        samples = self.samples_seen[0] or {}
        self.assertEqual(samples.get("备注"), ["开始检查", "开始复核", "正常"])
        self.assertNotIn("姓名", samples)               # 精确命中列不进 LLM
        entry = self.ff.MAPPINGS.get_entry("备注")
        self.assertEqual(entry["prompt_ver"], 2)        # 落库归因

    def test_remap_flow_temporary_keep_without_llm(self):
        """删除→保留翻转：转 remap → 二轮管线临时保留（remap_keep）且不再进 LLM。"""
        self._seed_suggestions()
        res = self.client.post("/api/file-filter/mappings/remap",
                               json={"key": "开始时间", "sample": "开始时间"})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        p = self.preview()
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="llm")
        self.assertEqual(payload["status"], "done")
        kept = {k["column"]: k["source"] for k in payload["kept"]}
        self.assertEqual(kept.get("开始时间"), "remap_keep")
        # remap 不进 LLM：二轮全部缓存命中（备注建议、开始时间 remap、手机号建议删）
        # → LLM 一次都没调（样本捕获列表长度不变）
        self.assertEqual(len(self.samples_seen), 1)
        self.assertFalse(payload["llm_used"])
        entries = {e["key"]: e for e in
                   self.client.get("/api/file-filter/mappings").get_json()["entries"]}
        self.assertEqual(entries["开始时间"]["status"], "remap")

    def test_remap_endpoint_guards(self):
        res = self.client.post("/api/file-filter/mappings/remap", json={"key": "不存在"})
        self.assertEqual(res.status_code, 409)
        orig = self.ff._get_session_user
        try:
            self.ff._get_session_user = lambda: None
            res = self.client.post("/api/file-filter/mappings/remap", json={"key": "备注"})
            self.assertEqual(res.status_code, 401)
        finally:
            self.ff._get_session_user = orig

    def test_vote_endpoint_and_promotion(self):
        """计票端点：按用户去重；5 个不同用户同向 → 加权转正，确认块不再出现该条。"""
        self._seed_suggestions()                        # 备注 suggested 姓名
        orig = self.ff._get_session_user
        try:
            for i in range(5):
                self.ff._get_session_user = lambda i=i: {
                    "username": "op%d" % i, "role_id": "role-user", "super_admin": False}
                res = self.client.post("/api/file-filter/mappings/vote",
                                       json={"items": [{"key": "备注", "dir": "keep"}]})
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.get_json()["voted"], 1)
            # 同用户重复采纳 → 去重不计
            self.ff._get_session_user = lambda: {
                "username": "op0", "role_id": "role-user", "super_admin": False}
            dup = self.client.post("/api/file-filter/mappings/vote",
                                   json={"items": [{"key": "备注", "dir": "keep"}]})
            self.assertEqual(dup.get_json()["voted"], 0)
        finally:
            self.ff._get_session_user = orig
        entry = self.ff.MAPPINGS.get_entry("备注")
        self.assertEqual((entry["status"], entry["confirmed_by"]), ("confirmed", "加权转正"))
        p = self.preview()
        payload = self.filter_and_wait(staged_id=p["staged_id"], mode="llm")
        self.assertEqual(payload["kept"][-1]["source"], "confirmed")

    def test_recheck_scope_mismatch_default(self):
        """recheck 范围校准：缺省只复核失配（remap/orphan），全部建议级需显式扩展。"""
        vt = self.ff._mapping_valid_targets()
        M = self.ff.MAPPINGS
        M.record_suggestions(["临时A"], {"临时A": "姓名"}, vt, user="t")   # 非失配建议
        M.record_suggestions(["临时B"], {"临时B": ""}, vt, user="t")
        M.flip_to_remap("临时B", user="t")                                  # 失配（待重映射）
        res = self.client.post("/api/file-filter/mappings/recheck")
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        payload = self._poll_recheck(res.get_json()["task_id"])
        self.assertEqual(payload["total"], 1)               # 默认只含 remap（临时B）
        res = self.client.post("/api/file-filter/mappings/recheck",
                               json={"scope": "all_suggested"})
        payload = self._poll_recheck(res.get_json()["task_id"])
        self.assertEqual(payload["total"], 2)               # 扩展后含全部建议级
        res = self.client.post("/api/file-filter/mappings/recheck",
                               json={"scope": "bogus"})
        self.assertEqual(res.status_code, 400)


    def test_vote_dissent_demotes_confirmed_then_repromotes(self):
        """非管理员对已确认映射的异议（决策 11）：降级重议；连续同向 5 票重新转正。"""
        self._seed_suggestions()
        res = self.client.post("/api/file-filter/mappings/confirm",
                               json={"items": [{"key": "备注", "target": "姓名"}]})
        self.assertEqual(res.status_code, 200)
        # 异议（该列应删）：confirmed 降级回建议级，现任目标不变
        res = self.client.post("/api/file-filter/mappings/vote",
                               json={"items": [{"key": "备注", "dir": "drop", "dissent": True}]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["dissented"], 1)
        entry = self.ff.MAPPINGS.get_entry("备注")
        self.assertEqual((entry["status"], entry["target"]), ("suggested", "姓名"))
        self.assertEqual(entry["dissent_by"], "tester")
        # 后续 5 连 keep（被动采纳，按用户去重 → 5 个不同用户）→ 重新转正
        for i in range(5):
            self.ff._get_session_user = lambda i=i: {
                "username": "voter%d" % i, "role_id": "role-user", "super_admin": False}
            self.client.post("/api/file-filter/mappings/vote",
                             json={"items": [{"key": "备注", "dir": "keep"}]})
        self.ff._get_session_user = lambda: dict(USER)
        entry = self.ff.MAPPINGS.get_entry("备注")
        self.assertEqual((entry["status"], entry["target"]), ("confirmed", "姓名"))

    def test_vote_dissent_agreement_is_noop(self):
        """异议方向与现行确认一致 → dissented=0，条目保持 confirmed。"""
        self._seed_suggestions()
        self.client.post("/api/file-filter/mappings/confirm",
                         json={"items": [{"key": "备注", "target": "姓名"}]})
        res = self.client.post("/api/file-filter/mappings/vote",
                               json={"items": [{"key": "备注", "dir": "keep", "dissent": True}]})
        self.assertEqual(res.get_json()["dissented"], 0)
        self.assertEqual(self.ff.MAPPINGS.get_entry("备注")["status"], "confirmed")


    def test_preview_remap_match_and_governance(self):
        """预览 remap 预填：待重映射列 match={target:"",status:"remap"}（预览-结果一致）；
        /mappings 回传治理统计（加权转正/重议中/有翻转）。"""
        self._seed_suggestions()
        self.ff.MAPPINGS.flip_to_remap("开始时间", user="tester")
        p = self.preview()
        by = {c["name"]: c for c in p["columns"]}
        self.assertEqual(by["开始时间"]["match"], {"target": "", "status": "remap"})
        self.assertIsNone(by["姓名"]["match"])
        res = self.client.get("/api/file-filter/mappings")
        payload = res.get_json()
        gov = payload.get("governance") or {}
        self.assertIn("auto_promoted", gov)
        self.assertIn("revising", gov)
        self.assertIn("flipped", gov)

    def test_vote_dissent_demotes_confirmed_then_repromotes(self):
        """非管理员对已确认映射的异议（决策 11）：降级重议；连续同向 5 票重新转正。"""
        self._seed_suggestions()
        res = self.client.post("/api/file-filter/mappings/confirm",
                               json={"items": [{"key": "备注", "target": "姓名"}]})
        self.assertEqual(res.status_code, 200)
        res = self.client.post("/api/file-filter/mappings/vote",
                               json={"items": [{"key": "备注", "dir": "drop", "dissent": True}]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["dissented"], 1)
        entry = self.ff.MAPPINGS.get_entry("备注")
        self.assertEqual((entry["status"], entry["target"]), ("suggested", "姓名"))
        self.assertEqual(entry["dissent_by"], "tester")
        orig = self.ff._get_session_user
        try:
            for i in range(5):
                self.ff._get_session_user = lambda i=i: {
                    "username": "voter%d" % i, "role_id": "role-user", "super_admin": False}
                self.client.post("/api/file-filter/mappings/vote",
                                 json={"items": [{"key": "备注", "dir": "keep"}]})
        finally:
            self.ff._get_session_user = orig
        entry = self.ff.MAPPINGS.get_entry("备注")
        self.assertEqual((entry["status"], entry["target"]), ("confirmed", "姓名"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
