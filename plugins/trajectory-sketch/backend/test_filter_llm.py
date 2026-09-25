# -*- coding: utf-8 -*-
"""轨迹速写「大模型辅助判断表头」恢复后的契约回归测试（v1.3.0）。

背景
----
2026-09-19 的解耦把本插件的 ``mode=llm``（大模型辅助匹配）一并移除，理由写的是
"依赖「过滤器」插件自己的大模型配置"。统一大模型模块（主体模块 ``jz_llm``）落地后该前提
失效——接入信息归框架，``jz_llm.resolve()`` 的 ``plugin_id`` 只用于日志定位，
且 ``jz_llm`` 属 ``FRAMEWORK_MODULES`` 白名单（插件 import 它是正规出口）。
本套件钉住的三件事：

1. **能力真的回来了**：``mode=llm`` 能走通语义匹配，且 ``filter_llm_configured()``
   反映真实配置状态（不再是硬编码 ``False``——那是"开关永远置灰"的真因）；
2. **口径与 file-filter 对齐**：名单内同名字段**直接保留**（不把用户点名要的列交给模型），
   模型返回名单外的值按未匹配处理（防幻觉）；
3. **不再有跨插件耦合**：插件不 import 过滤器插件、不直接 import ``requests``，
   大模型调用走框架模块。

运行（开发机）：
    python -m unittest plugins.trajectory_sketch_backend_test -v   # 见文件末尾说明
    python plugins/trajectory-sketch/backend/test_filter_llm.py    # 直接跑也支持
"""

import os
import sys
import tempfile
import types
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(BACKEND, "..", "..", ".."))
sys.path.insert(0, REPO)

# 数据根隔离：必须在 import jztools_data 之前设好，避免动到开发机的真实数据
_TMP_DATA = tempfile.mkdtemp(prefix="ts-filter-test-")
os.environ["JZTOOLS_DATA_ROOT"] = _TMP_DATA

# 以「垫片包」方式导入带连字符的插件目录（与 knowledge-base 的测试同法）
_PKG = "ts_backend_test"
_pkg = types.ModuleType(_PKG)
_pkg.__path__ = [BACKEND]
sys.modules[_PKG] = _pkg

import importlib  # noqa: E402

filter_local = importlib.import_module(_PKG + ".filter_local")
filter_bridge = importlib.import_module(_PKG + ".filter_bridge")
llm_client = importlib.import_module(_PKG + ".llm_client")
routes = importlib.import_module(_PKG + ".routes")

PLUGIN_DIR = os.path.dirname(BACKEND)


def _read(rel):
    with open(os.path.join(PLUGIN_DIR, rel), encoding="utf-8") as f:
        return f.read()


class StubSession:
    """``jz_llm.LLMSession`` 的最小替身：只要 ``configured()``。"""

    def __init__(self, configured=True):
        self._configured = configured
        self.source = "global" if configured else ""

    def configured(self):
        return self._configured


class MatchStub:
    """替换 ``llm_client.match_columns``，按预设映射返回，并记录收到的入参。"""

    def __init__(self, mapping=None, exc=None):
        self.mapping = mapping or {}
        self.exc = exc
        self.calls = []

    def __call__(self, headers, keep_columns, session=None, timeout=None):
        self.calls.append({"headers": list(headers),
                           "keep": list(keep_columns or []),
                           "session": session})
        if self.exc is not None:
            raise self.exc
        return {str(h): self.mapping.get(str(h), "") for h in headers}


class Base(unittest.TestCase):
    """统一保存/还原 ``llm_client`` 与 ``jz_llm`` 上被测试替换掉的属性。"""

    _PATCHED_LLM = ("resolve", "chat_json", "render", "load_prompt")

    def setUp(self):
        self._orig = {"match": llm_client.match_columns}
        for name in self._PATCHED_LLM:
            if hasattr(llm_client.jz_llm, name):
                self._orig[name] = getattr(llm_client.jz_llm, name)

    def tearDown(self):
        llm_client.match_columns = self._orig["match"]
        for name in self._PATCHED_LLM:
            if name in self._orig:
                setattr(llm_client.jz_llm, name, self._orig[name])
            elif hasattr(llm_client.jz_llm, name):
                delattr(llm_client.jz_llm, name)


# ==========================================================================
# 1) 硬过滤口径未被改动（回归护栏）
# ==========================================================================

class HardModeTest(Base):

    KEEP = ["BEGINTIME", "开始时间", "LONGITUDE", "经度"]

    def test_exact_match_and_order_follow_header(self):
        rows = [["开始时间", "姓名", "经度"],
                ["2026-01-01 09:00", "张三", 116.4],
                ["2026-01-01 10:00", "李四", 116.5]]
        out = filter_local.apply_filter(rows, mode="hard", columns=self.KEEP)
        self.assertEqual(out["mode"], "hard")
        self.assertEqual(out["rows"][0], ["开始时间", "经度"])
        self.assertEqual(out["removed"], ["姓名"])
        self.assertEqual(out["kept"], [{"column": "开始时间", "matched": "开始时间"},
                                       {"column": "经度", "matched": "经度"}])

    def test_blank_rows_skipped_and_count_matches_body(self):
        rows = [["开始时间"], ["a"], [None], [""], ["b"]]
        out = filter_local.apply_filter(rows, mode="hard", columns=["开始时间"])
        self.assertEqual(len(out["rows"]) - 1, 2)     # 全空行被跳过

    def test_unknown_mode_falls_back_to_hard(self):
        rows = [["开始时间"], ["a"]]
        out = filter_local.apply_filter(rows, mode="bogus", columns=["开始时间"])
        self.assertEqual(out["mode"], "hard")
        self.assertNotIn("warning", out)             # 旧的"已移除"告警不再出现

    def test_empty_keep_list_rejected(self):
        with self.assertRaises(filter_local.FilterError) as cm:
            filter_local.apply_filter([["a"], ["1"]], mode="hard", columns=[])
        self.assertEqual(cm.exception.status, 400)

    def test_post_process_runs_in_both_modes(self):
        rows = [["开始时间"], ["2026-01-01"]]
        rules = [{"pattern": "开始", "replacement": "", "is_regex": False, "enabled": True}]
        self.assertEqual(filter_local.apply_filter(rows, "hard", ["开始时间"], rules)["rows"][0],
                         ["时间"])
        llm_client.match_columns = MatchStub({"开始时间": "开始时间"})
        out = filter_local.apply_filter(rows, "llm", ["开始时间"], rules,
                                        session=StubSession())
        self.assertEqual(out["rows"][0], ["时间"])


# ==========================================================================
# 2) 大模型辅助：语义匹配 + 两条防幻觉/防误删口径
# ==========================================================================

class LlmModeTest(Base):

    HEADERS = ["采集起始时刻", "用户号码", "备注说明"]
    KEEP = ["开始时间", "USERNUM", "用户号码", "经度"]
    ROWS = [HEADERS,
            ["2026-01-01 09:00", "13800001001", "无关"],
            ["2026-01-02 10:00", "13800001002", "无关"]]

    def test_semantic_mapping_keeps_matched_columns(self):
        llm_client.match_columns = MatchStub({"采集起始时刻": "开始时间",
                                              "用户号码": "用户号码"})
        out = filter_local.apply_filter(self.ROWS, mode="llm", columns=self.KEEP,
                                        session=StubSession())
        self.assertEqual(out["mode"], "llm")
        self.assertEqual(out["rows"][0], ["采集起始时刻", "用户号码"])
        self.assertEqual(out["removed"], ["备注说明"])
        self.assertEqual(out["kept"], [{"column": "采集起始时刻", "matched": "开始时间"},
                                       {"column": "用户号码", "matched": "用户号码"}])
        self.assertEqual(out["rows"][1], ["2026-01-01 09:00", "13800001001"])

    def test_same_name_in_keep_list_survives_empty_model_answer(self):
        """口径：名单内的同名字段直接保留——模型返回空值也不删。

        （File-filter 的同款行为；否则"明明开着却被删"会被当成 bug。）
        """
        llm_client.match_columns = MatchStub({})          # 模型对一切返回空
        out = filter_local.apply_filter(self.ROWS, mode="llm", columns=self.KEEP,
                                        session=StubSession())
        self.assertEqual(out["rows"][0], ["用户号码"])
        self.assertEqual(out["kept"], [{"column": "用户号码", "matched": "用户号码"}])

    def test_off_list_value_is_trusted_only_after_upstream_validation(self):
        """过滤层只消费**已校验**的映射值；防幻觉在 ``llm_client.match_columns`` 里做
        （见 LlmClientTest）。这里钉住：映射值为真即保留，为假即删除。"""
        llm_client.match_columns = MatchStub({})       # 全部为空 → 全部删除
        out = filter_local.apply_filter(self.ROWS, mode="llm",
                                        columns=["开始时间", "经度"], session=StubSession())
        self.assertEqual(out["rows"][0], [])
        self.assertEqual(sorted(out["removed"]), sorted(self.HEADERS))
        # 名单内单列真值 → 仅该列保留（顺序随原表头）
        llm_client.match_columns = MatchStub({"备注说明": "开始时间"})
        out = filter_local.apply_filter(self.ROWS, mode="llm",
                                        columns=["开始时间", "经度"], session=StubSession())
        self.assertEqual(out["rows"][0], ["备注说明"])
        self.assertEqual(out["kept"], [{"column": "备注说明", "matched": "开始时间"}])

    def test_session_is_forwarded_to_llm_client(self):
        stub = MatchStub({"用户号码": "用户号码"})
        llm_client.match_columns = stub
        sess = StubSession()
        filter_local.apply_filter(self.ROWS, mode="llm", columns=self.KEEP, session=sess)
        self.assertIs(stub.calls[0]["session"], sess)

    def test_llm_error_becomes_filter_error_with_readable_message(self):
        llm_client.match_columns = MatchStub(exc=llm_client.LLMError("模型超时"))
        with self.assertRaises(filter_local.FilterError) as cm:
            filter_local.apply_filter(self.ROWS, mode="llm", columns=self.KEEP,
                                      session=StubSession())
        self.assertIn("大模型辅助过滤失败", cm.exception.message)
        self.assertIn("模型超时", cm.exception.message)
        self.assertEqual(cm.exception.status, 400)


# ==========================================================================
# 2b) 适配层：防幻觉约束 + 提示词走 jz_llm（真正的校验发生在这里）
# ==========================================================================

class LlmClientTest(Base):

    HEADERS = ["采集起始时刻", "用户号码"]
    KEEP = ["开始时间", "用户号码", "经度"]

    def _patch_chat(self, data):
        """把 jz_llm 的三个入口换成替身，返回记录调用参数的容器。"""
        rec = {}

        def fake_load_prompt(plugin_id, default):
            rec["plugin_id"] = plugin_id
            return {}                                   # 走模块内置提示词回退

        def fake_render(template, mapping):
            rec["rendered"] = (template, mapping)
            return "USER-MSG"

        def fake_chat_json(system, user, **kw):
            rec["chat"] = {"system": system, "user": user, "kw": kw}
            if isinstance(data, Exception):
                raise data
            return data

        llm_client.jz_llm.load_prompt = fake_load_prompt
        llm_client.jz_llm.render = fake_render
        llm_client.jz_llm.chat_json = fake_chat_json
        return rec

    def test_plugin_id_and_params_are_passed_to_framework(self):
        rec = self._patch_chat({"mappings": {}})
        llm_client.match_columns(self.HEADERS, self.KEEP, session=StubSession())
        self.assertEqual(rec["plugin_id"], "trajectory-sketch")
        self.assertEqual(rec["chat"]["kw"]["plugin_id"], "trajectory-sketch")
        self.assertEqual(rec["chat"]["kw"]["temperature"], 0.1)
        # 表头与名单以 JSON 文本喂给提示词，中文不转义
        self.assertIn("采集起始时刻", rec["rendered"][1]["headers"])
        self.assertIn("开始时间", rec["rendered"][1]["keep"])

    def test_value_outside_keep_list_is_dropped(self):
        """防幻觉：模型返回名单外的字段名 → 归一为未匹配（空串）。"""
        self._patch_chat({"mappings": {"采集起始时刻": "不存在的字段",
                                      "用户号码": "用户号码"}})
        out = llm_client.match_columns(self.HEADERS, self.KEEP)
        self.assertEqual(out["采集起始时刻"], "")
        self.assertEqual(out["用户号码"], "用户号码")

    def test_case_insensitive_value_matching(self):
        self._patch_chat({"mappings": {"用户号码": "usernum"}})
        self.assertEqual(llm_client.match_columns(self.HEADERS,
                                                 ["USERNUM"])["用户号码"], "USERNUM")

    def test_missing_key_treated_as_unmatched(self):
        self._patch_chat({"mappings": {}})
        out = llm_client.match_columns(self.HEADERS, self.KEEP)
        self.assertEqual(out, {"采集起始时刻": "", "用户号码": ""})

    def test_bad_model_output_raises(self):
        self._patch_chat({"nope": 1})
        with self.assertRaises(llm_client.LLMError):
            llm_client.match_columns(self.HEADERS, self.KEEP)
        self._patch_chat(llm_client.LLMError("超时"))
        with self.assertRaises(llm_client.LLMError):
            llm_client.match_columns(self.HEADERS, self.KEEP)

    def test_empty_inputs_rejected(self):
        with self.assertRaises(llm_client.LLMError):
            llm_client.match_columns([], self.KEEP)
        with self.assertRaises(llm_client.LLMError):
            llm_client.match_columns(self.HEADERS, [])

    def test_default_prompt_contract(self):
        p = llm_client.default_prompt()
        self.assertIn("mappings", p["system"])
        self.assertIn("{headers}", p["user_template"])
        self.assertIn("{keep}", p["user_template"])


# ==========================================================================
# 3) 可用性自检：反映真实状态，不再硬编码
# ==========================================================================

class AvailabilityTest(Base):

    def test_filter_llm_configured_follows_jz_llm(self):
        for flag in (True, False):
            llm_client.jz_llm.resolve = lambda *a, **k: StubSession(flag)
            self.assertIs(filter_bridge.filter_llm_configured(), flag)
            self.assertIs(filter_bridge.filter_status()["llm"], flag)

    def test_resolve_failure_degrades_to_false(self):
        def boom(*a, **k):
            raise RuntimeError("no request context")

        llm_client.jz_llm.resolve = boom
        self.assertFalse(filter_bridge.filter_llm_configured())
        self.assertFalse(filter_bridge.filter_status()["llm"])

    def test_filter_status_reports_local_availability(self):
        llm_client.jz_llm.resolve = lambda *a, **k: StubSession(False)
        st = filter_bridge.filter_status()
        self.assertTrue(st["available"])              # 硬过滤恒可用（不依赖其他插件）
        self.assertTrue(st["local"])

    def test_llm_session_returns_snapshot(self):
        sess = StubSession()
        llm_client.jz_llm.resolve = lambda *a, **k: sess
        self.assertIs(filter_bridge.llm_session(), sess)


# ==========================================================================
# 4) 路由层：/status 的 requests 标记与 mode=llm 前置校验
# ==========================================================================

class RoutesWiringTest(Base):

    @classmethod
    def setUpClass(cls):
        from flask import Flask
        cls.app = Flask(__name__)
        routes.register(cls.app)
        cls.client = cls.app.test_client()

    def test_status_reports_real_requests_flag(self):
        r = self.client.get("/api/trajectory-sketch/status")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        # 关键：不再是硬编码 False（那会让插件页常驻红色误报且重装不消失）
        self.assertEqual(body["dependencies"]["requests"], llm_client.REQUESTS_AVAILABLE,
                         "requests 自检项必须如实转发 llm_client 的值")
        self.assertIn("llm", body["filter_plugin"])

    def test_analyze_llm_without_config_returns_400_with_new_wording(self):
        # 未走上传自检时任何非法 staged_id 都会先 404/400；这里只钉"文案不再指向过滤器插件"
        src = _read(os.path.join("backend", "routes.py"))
        self.assertNotIn("请管理员在过滤器插件页面完成 API 地址", src)
        self.assertIn("大模型设置", src)

    def test_analyze_gate_uses_live_session_not_stale_stage_flag(self):
        """mode=llm 的前置校验必须现场解析接入配置，而不是读上传时盖的旧标记。"""
        src = _read(os.path.join("backend", "routes.py"))
        self.assertIn("session = filter_bridge.llm_session()", src)
        self.assertIn("if not session.configured():", src)
        self.assertNotIn('if mode == "llm" and not stage.get("llm_configured"):', src)

    def test_worker_receives_session(self):
        src = _read(os.path.join("backend", "routes.py"))
        self.assertIn("_cookie(), _host_url(), session)", src)
        self.assertIn("session: Any = None", src)


# ==========================================================================
# 5) 解耦纪律与依赖声明（静态契约）
# ==========================================================================

class DecouplingContractTest(Base):

    def test_no_cross_plugin_import_or_direct_requests(self):
        """解耦纪律：不 import 过滤器插件的模块、不直接引用 requests。

        用 AST 取真实 import 与属性访问，而不是查字符串——docstring 里**叙述历史**
        （"过去调 /api/file-filter/apply"、"不再直接 import requests"）不算耦合。
        与出包工具 C-4/C-11 的扫描口径一致（见 tools/build-plugin-package.ps1）。
        """
        import ast
        forbidden_mods = {"requests", "jztools_file_filter"}
        for rel in ("backend/filter_local.py", "backend/filter_bridge.py",
                    "backend/llm_client.py", "backend/routes.py"):
            src = _read(rel)
            mods = set()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        mods.add(a.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        mods.add(node.module.split(".")[0])
                elif (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "requests"):
                    self.fail("%s 直接使用了 requests.%s（HTTP 应由 jz_llm 发起）"
                              % (rel, node.attr))
            self.assertFalse(mods & forbidden_mods,
                             "%s 出现了禁止的 import：%s" % (rel, sorted(mods & forbidden_mods)))
            self.assertNotIn("REQUESTS_AVAILABLE = False", src,
                             "%s 不得把 requests 可用性写成常量假值" % rel)

    def test_llm_client_uses_framework_module(self):
        self.assertIn("import jz_llm", _read(os.path.join("backend", "llm_client.py")))

    def test_removed_note_gone(self):
        src = _read(os.path.join("backend", "filter_local.py"))
        self.assertNotIn("LLM_REMOVED_NOTE", src)
        self.assertNotIn("已随插件解耦移除", src)

    def test_requirements_declares_requests(self):
        self.assertIn("requests", _read(os.path.join("backend", "requirements.txt")))

    def test_manifest_declares_requests_with_hint(self):
        import json
        mf = json.loads(_read("manifest.json"))
        pkgs = {p["name"]: p for p in mf["requires"]["python_packages"]}
        self.assertIn("requests", pkgs, "有 LLM 功能就必须声明 requests（U-7/U-8）")
        self.assertFalse(pkgs["requests"]["required"])
        self.assertIn("大模型", pkgs["requests"]["hint"])
        # 由「依赖组件包」提供的（required=false）必须写对入口名与包名
        for name, p in pkgs.items():
            if p.get("required"):
                continue                      # 随主包的必需依赖另有提示口径
            self.assertIn("安装依赖组件.bat", p["hint"],
                          "%s 的 hint 应使用依赖组件包的真实入口名" % name)
            self.assertIn("JZToolsHub-依赖-%s-v" % name, p["hint"],
                          "%s 的 hint 包名应为 JZToolsHub-依赖-<pkg>-v*.zip" % name)

    def test_frontend_copy_no_longer_points_at_filter_plugin(self):
        for rel in ("frontend/app.js", "frontend/index.html"):
            src = _read(rel)
            self.assertNotIn("需过滤器插件已配置大模型", src)
            self.assertNotIn("已连接「过滤器」插件", src)
            self.assertNotIn("大模型辅助匹配已随", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
