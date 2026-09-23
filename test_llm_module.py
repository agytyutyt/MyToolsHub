# -*- coding: utf-8 -*-
"""统一大模型模块（jz_llm）回归测试。

覆盖范围
--------
1. **全局配置**：默认值、保存/读取往返、API Key 落盘加密、对外脱敏、字段钳制、原子写；
2. **调用格式**：openai / anthropic / ollama / custom 四种的请求构造（认证头、报文形态、
   取值路径），以及自定义模板的占位符转义（提示词里的引号与换行不能把 JSON 打坏）；
3. **配置解析**：管理员模式 / 用户各自设置 / 回退开关 / 插件历史配置兼容桥 / 未配置原因；
4. **真实调用**：起一个本地 HTTP 桩服务，验证 chat / chat_json / chat_text / test_connection
   的正文提取、JSON 解析、HTTP 错误文案；
5. **线程纪律**：请求线程内 resolve() 得到的会话可跨线程使用（后台任务模式），
   这是"用户各自设置"模式下最容易踩的坑（后台线程读不到会话 → 被误判为未配置）；
6. **提示词**：load_prompt/save_prompt 往返、文件损坏回退内置默认、render 占位符；
7. **框架与后台路由**：/api/llm/settings（含匿名 401、管理员模式下个人保存 403）、
   /api/admin/llm-settings（角色门控、脱敏回显），以及三个插件迁移后的 /config 口径。

运行：
    python -m pytest test_llm_module.py -q
    python -m unittest test_llm_module -v        # 无需 pytest
"""

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import jz_llm  # noqa: E402


# ==========================================================================
# 本地 HTTP 桩服务：不联网，按路径返回各格式的典型响应，并记录收到的请求
# ==========================================================================

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音访问日志
        pass

    def _reply(self, code, obj, raw=None):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {"_raw": raw.decode("utf-8", "replace")}
        record = {
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "payload": payload,
        }
        self.server.requests.append(record)
        path = self.path

        if path.startswith("/err401"):
            return self._reply(401, {"error": {"message": "invalid api key"}})
        if path.startswith("/err500"):
            return self._reply(500, {"error": {"message": "boom"}})
        if path.startswith("/openai/mappings"):
            # 字段语义匹配场景：返回 file-filter 期望的 mappings 结构
            return self._reply(200, {
                "choices": [{"message": {"content": '{"mappings": {"姓名": "姓名"}}'}}],
            })
        if path.startswith("/openai"):
            return self._reply(200, {
                "choices": [{"message": {"content": '```json\n{"案件名": "8·16 案"}\n```'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5},
            })
        if path.startswith("/anthropic"):
            return self._reply(200, {"content": [{"type": "text", "text": "anthropic-ok"}]})
        if path.startswith("/ollama"):
            return self._reply(200, {"message": {"role": "assistant", "content": "ollama-ok"}})
        if path.startswith("/custom"):
            return self._reply(200, {"data": {"choices": [{"text": "custom-ok"}]}})
        if path.startswith("/plain"):
            return self._reply(200, None, raw="纯文本正文".encode("utf-8"))
        if path.startswith("/empty"):
            return self._reply(200, {"unexpected": True})
        return self._reply(404, {"error": "not found"})


class MockServer:
    """本地 HTTP 桩（端口自动分配）。"""

    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self):
        return self.httpd.requests

    def last(self):
        return self.requests[-1] if self.requests else None

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ==========================================================================
# 1. 全局配置
# ==========================================================================

class TestGlobalConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jz-llm-")
        self._env = os.environ.get("JZTOOLS_DATA_ROOT")
        os.environ["JZTOOLS_DATA_ROOT"] = self.tmp

    def tearDown(self):
        if self._env is None:
            os.environ.pop("JZTOOLS_DATA_ROOT", None)
        else:
            os.environ["JZTOOLS_DATA_ROOT"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults(self):
        cfg = jz_llm.load_global()
        self.assertEqual(cfg["mode"], "admin")
        self.assertTrue(cfg["fallback"])
        self.assertEqual(cfg["provider"]["format"], "openai")
        self.assertFalse(jz_llm.provider_usable(cfg["provider"]))
        self.assertIn("API 地址", jz_llm.provider_problem(cfg["provider"]))

    def test_roundtrip_and_key_encrypted_on_disk(self):
        jz_llm.save_global({"mode": "user", "fallback": False, "provider": {
            "format": "anthropic", "url": "https://api.anthropic.com/v1/messages",
            "api_key": "sk-super-secret", "model": "claude-x", "timeout": 30}})
        raw = open(jz_llm.config_path(), encoding="utf-8").read()
        self.assertNotIn("sk-super-secret", raw, "API Key 不得明文落盘")
        cfg = jz_llm.load_global()
        self.assertEqual(cfg["mode"], "user")
        self.assertFalse(cfg["fallback"])
        self.assertEqual(cfg["provider"]["api_key"], "sk-super-secret")
        self.assertEqual(cfg["provider"]["timeout"], 30)
        self.assertEqual(cfg["provider"]["format"], "anthropic")

    def test_public_masks_key(self):
        jz_llm.save_global({"provider": {"format": "openai", "url": "http://x/chat/completions",
                                         "api_key": "sk-abc", "model": "m"}})
        pub = jz_llm.public_global()
        self.assertEqual(pub["provider"]["api_key"], "••••••••")
        self.assertTrue(pub["provider"]["configured"])
        self.assertNotIn("sk-abc", json.dumps(pub, ensure_ascii=False))

    def test_clamping_and_unknown_format(self):
        jz_llm.save_global({"provider": {
            "format": "不存在的格式", "url": " http://x/chat/completions ",
            "temperature": 99, "max_tokens": -5, "timeout": 99999, "model": "  m  "}})
        p = jz_llm.load_global()["provider"]
        self.assertEqual(p["format"], "openai")       # 非法格式回落默认
        self.assertEqual(p["url"], "http://x/chat/completions")   # 去空白
        self.assertEqual(p["model"], "m")
        self.assertEqual(p["temperature"], 2.0)       # 钳到上限
        self.assertIsNone(p["max_tokens"])            # 非法 → None（用服务默认）
        self.assertEqual(p["timeout"], 3600)          # 钳到上限

    def test_mode_and_user_mode(self):
        jz_llm.save_global({"mode": "USER"})          # 大小写不敏感
        self.assertEqual(jz_llm.mode(), "user")
        self.assertTrue(jz_llm.user_mode())
        jz_llm.save_global({"mode": "乱写"})
        self.assertEqual(jz_llm.mode(), "admin")      # 非法回落 admin

    def test_broken_file_falls_back_to_defaults(self):
        os.makedirs(os.path.dirname(jz_llm.config_path()), exist_ok=True)
        with open(jz_llm.config_path(), "w", encoding="utf-8") as f:
            f.write("{不是 JSON")
        cfg = jz_llm.load_global()
        self.assertEqual(cfg["mode"], "admin")
        self.assertEqual(cfg["provider"]["url"], "")

    def test_ollama_needs_no_key(self):
        p = jz_llm.normalize_provider({"format": "ollama", "url": "http://127.0.0.1:11434/api/chat"})
        self.assertTrue(jz_llm.provider_usable(p))
        self.assertEqual(jz_llm.provider_problem(p), "")

    def test_openai_requires_key(self):
        p = jz_llm.normalize_provider({"format": "openai", "url": "http://x/chat/completions"})
        self.assertFalse(jz_llm.provider_usable(p))
        self.assertIn("API Key", jz_llm.provider_problem(p))


# ==========================================================================
# 2. 调用格式：请求构造
# ==========================================================================

class TestFormatRequest(unittest.TestCase):
    def _build(self, **provider):
        p = jz_llm.normalize_provider(provider)
        return jz_llm._build_request(p, "SYS", "USER", None, 30)

    def test_openai(self):
        method, url, headers, body, paths, err = self._build(
            format="openai", url="http://x/v1/chat/completions", api_key="K", model="m")
        self.assertEqual(method, "POST")
        self.assertEqual(headers["Authorization"], "Bearer K")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "USER"})
        self.assertFalse(body["stream"])
        self.assertEqual(paths, [("choices", 0, "message", "content")])

    def test_anthropic_system_is_top_level(self):
        method, url, headers, body, paths, err = self._build(
            format="anthropic", url="http://x/v1/messages", api_key="K", model="m")
        self.assertEqual(headers["x-api-key"], "K")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["system"], "SYS")                      # 顶层，不在 messages 里
        self.assertEqual([m["role"] for m in body["messages"]], ["user"])
        self.assertEqual(body["max_tokens"], jz_llm.DEFAULT_ANTHROPIC_MAX_TOKENS)  # 该接口必填
        self.assertEqual(paths, [("content", 0, "text")])

    def test_ollama_paths_and_options(self):
        method, url, headers, body, paths, err = self._build(
            format="ollama", url="http://x/api/chat", model="m", temperature=0.5)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(body["options"]["temperature"], 0.5)
        self.assertEqual(paths, [("message", "content"), ("response",)])   # chat 与 generate 两种都认

    def test_custom_template_escaping_keeps_json_valid(self):
        """提示词里的引号/换行必须转义，否则自定义模板拼出的 JSON 会坏。"""
        p = jz_llm.normalize_provider({
            "format": "custom", "url": "http://gw/v1/chat", "api_key": "K", "model": "m",
            "headers": {"Authorization": "Bearer {{api_key}}", "X-Model": "{{model}}"},
            "body": '{"model": "{{model}}", "prompt": "{{system}}|{{user}}", "n": {{max_tokens}}}',
            "text_path": "data.choices[0].text",
        })
        method, url, headers, body, paths, err = jz_llm._build_request(
            p, 'SYS "引号"\n第二行', 'USER\n换行"引号"', None, 30)
        self.assertEqual(headers["Authorization"], "Bearer K")
        self.assertEqual(headers["X-Model"], "m")
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["prompt"], 'SYS "引号"\n第二行|USER\n换行"引号"')
        self.assertEqual(body["n"], 0)                      # 未配置 max_tokens → 0
        self.assertEqual(paths, [("data", "choices", "0", "text")])

    def test_custom_without_body_is_readable_error(self):
        p = jz_llm.normalize_provider({"format": "custom", "url": "http://gw"})
        with self.assertRaises(jz_llm.LLMError) as ctx:
            jz_llm._build_request(p, "s", "u", None, 30)
        self.assertIn("请求体模板", str(ctx.exception))

    def test_custom_broken_json_is_readable_error(self):
        p = jz_llm.normalize_provider({"format": "custom", "url": "http://gw",
                                       "body": '{"a": "{{model}}" 少个括号}'})
        with self.assertRaises(jz_llm.LLMError) as ctx:
            jz_llm._build_request(p, "s", "u", None, 30)
        self.assertIn("不是合法 JSON", str(ctx.exception))

    def test_path_notations_equivalent(self):
        self.assertEqual(jz_llm._split_path("a.b.0.c"), ["a", "b", "0", "c"])
        self.assertEqual(jz_llm._split_path("a.b[0].c"), ["a", "b", "0", "c"])


# ==========================================================================
# 3. 配置解析（模式 / 回退 / 兼容桥）
# ==========================================================================

class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jz-llm-res-")
        self._env = os.environ.get("JZTOOLS_DATA_ROOT")
        os.environ["JZTOOLS_DATA_ROOT"] = self.tmp
        self._user = None
        self._logged_in = True
        # 用 provider 冒充 admin 插件（框架 API 门面），避免依赖真实后台：
        # 会话给用户名、get_user_llm 给该用户自己的接入配置
        import jz_api
        self.jz_api = jz_api
        jz_api.register_providers(
            get_session_user=lambda: ({"username": "tester"} if self._logged_in else None),
            get_user_llm=lambda u: self._user)

    def tearDown(self):
        import jz_api
        jz_api.register_providers(get_session_user=lambda: None,
                                  get_user_llm=lambda u: None)
        if self._env is None:
            os.environ.pop("JZTOOLS_DATA_ROOT", None)
        else:
            os.environ["JZTOOLS_DATA_ROOT"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _global(self, **kw):
        jz_llm.save_global(kw)

    def test_admin_mode_uses_global(self):
        self._global(mode="admin", provider={"format": "openai",
                                             "url": "http://g/chat/completions", "api_key": "gk"})
        self._user = {"format": "openai", "url": "http://u/chat/completions", "api_key": "uk"}
        s = jz_llm.resolve()
        self.assertEqual(s.source, "global")
        self.assertEqual(s.provider["url"], "http://g/chat/completions")

    def test_user_mode_prefers_own(self):
        self._global(mode="user", provider={"format": "openai",
                                            "url": "http://g/chat/completions", "api_key": "gk"})
        self._user = {"format": "ollama", "url": "http://127.0.0.1:11434/api/chat", "model": "q"}
        s = jz_llm.resolve()
        self.assertEqual(s.source, "user")
        self.assertEqual(s.provider["format"], "ollama")

    def test_user_mode_fallback_to_global(self):
        self._global(mode="user", fallback=True, provider={
            "format": "openai", "url": "http://g/chat/completions", "api_key": "gk"})
        self._user = {}                                    # 用户没配
        self.assertEqual(jz_llm.resolve().source, "global")

    def test_user_mode_no_fallback_reports_actionable_reason(self):
        self._global(mode="user", fallback=False, provider={
            "format": "openai", "url": "http://g/chat/completions", "api_key": "gk"})
        self._user = {}
        s = jz_llm.resolve()
        self.assertEqual(s.source, "")
        self.assertFalse(s.configured())
        self.assertIn("⋯", s.reason)                        # 告诉用户去哪里配

    def test_admin_mode_unconfigured_reason_points_to_backend(self):
        s = jz_llm.resolve()
        self.assertEqual(s.source, "")
        self.assertIn("管理后台", s.reason)

    def test_plugin_legacy_config_is_not_read_anymore(self):
        """插件历史 config.json 里的 llm 段**不再被读取**（兼容桥已移除）。

        统一大模型落地前的旧 Key 由 admin 的启动迁移收编进统一配置后清除（见
        test_data_migrate.py 的迁移用例）；解析层不再有"按插件回退"这一级——
        否则插件侧会长期留着一份可被读取的明文 Key。
        """
        pdir = os.path.join(self.tmp, "plugins", "demo")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"llm": {"base_url": "http://old.example.com", "api_key": "old-key",
                               "model": "old-model"}}, f)
        s = jz_llm.resolve("demo")
        self.assertEqual(s.source, "")            # 不再回退读插件历史配置
        self.assertFalse(s.configured())
        # 只有统一配置（全局 / 用户自己那份）才是有效来源
        self._global(provider={"format": "openai", "url": "http://new/chat/completions",
                               "api_key": "new-key"})
        self.assertEqual(jz_llm.resolve("demo").source, "global")

    def test_public_snapshot_masks_key(self):
        self._global(provider={"format": "openai", "url": "http://g/chat/completions",
                               "api_key": "gk"})
        pub = jz_llm.resolve().public()
        self.assertEqual(pub["provider"]["api_key"], "••••••••")
        self.assertNotIn("gk", json.dumps(pub))


# ==========================================================================
# 4. 真实调用（本地桩）
# ==========================================================================

class TestChat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = MockServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jz-llm-chat-")
        self._env = os.environ.get("JZTOOLS_DATA_ROOT")
        os.environ["JZTOOLS_DATA_ROOT"] = self.tmp
        self.srv.requests.clear()

    def tearDown(self):
        if self._env is None:
            os.environ.pop("JZTOOLS_DATA_ROOT", None)
        else:
            os.environ["JZTOOLS_DATA_ROOT"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _provider(self, path, fmt="openai", key="K", **extra):
        prov = {"format": fmt, "url": self.srv.base + path, "api_key": key, "model": "m"}
        prov.update(extra)
        return prov

    def _session(self, path, fmt="openai", key="K", **extra):
        """构造一份"已解析好的接入配置"快照（等价于请求线程里 resolve() 的产物）。"""
        return jz_llm.LLMSession(jz_llm.normalize_provider(
            self._provider(path, fmt, key, **extra)), "user")

    def test_chat_json_parses_fenced_output(self):
        res = jz_llm.chat_json("S", "U", session=self._session("/openai/chat/completions"))
        self.assertEqual(res, {"案件名": "8·16 案"})
        rec = self.srv.last()
        self.assertEqual(rec["path"], "/openai/chat/completions")
        self.assertEqual(rec["headers"]["authorization"], "Bearer K")
        self.assertEqual(rec["payload"]["messages"][1]["content"], "U")

    def test_chat_result_shape(self):
        r = jz_llm.chat("S", "U", session=self._session("/openai/chat/completions"),
                        json_mode=True)
        self.assertEqual(r["format"], "openai")
        self.assertEqual(r["model"], "m")
        self.assertEqual(r["usage"]["prompt_tokens"], 3)
        self.assertIn("8·16", r["text"])

    def test_chat_text_anthropic(self):
        text = jz_llm.chat_text("S", "U", session=self._session("/anthropic/v1/messages",
                                                                 fmt="anthropic"))
        self.assertEqual(text, "anthropic-ok")
        self.assertEqual(self.srv.last()["headers"]["x-api-key"], "K")

    def test_chat_text_ollama(self):
        text = jz_llm.chat_text("S", "U", session=self._session("/ollama/api/chat", fmt="ollama"))
        self.assertEqual(text, "ollama-ok")

    def test_chat_custom_format(self):
        session = self._session("/custom/v1/chat", fmt="custom",
                                body='{"model": "{{model}}", "q": "{{user}}"}',
                                text_path="data.choices.0.text")
        self.assertEqual(jz_llm.chat_text(None, "U", session=session), "custom-ok")

    def test_http_error_is_readable(self):
        with self.assertRaises(jz_llm.LLMError) as ctx:
            jz_llm.chat_text("S", "U", session=self._session("/err401/chat/completions"))
        msg = str(ctx.exception)
        self.assertIn("401", msg)
        self.assertIn("API Key", msg)
        self.assertIn("invalid api key", msg)

    def test_structural_error_is_readable(self):
        with self.assertRaises(jz_llm.LLMError) as ctx:
            jz_llm.chat_text("S", "U", session=self._session("/empty/chat/completions"))
        self.assertIn("返回结构异常", str(ctx.exception))

    def test_unconfigured_raises_reason(self):
        with self.assertRaises(jz_llm.LLMError) as ctx:
            jz_llm.chat_text("S", "U")
        self.assertIn("尚未配置大模型", str(ctx.exception))

    def test_session_crosses_thread_boundary(self):
        """请求线程 resolve() 的会话必须能跨线程使用（插件后台任务的正确用法）。"""
        jz_llm.save_global({"provider": self._provider("/openai/chat/completions")})
        session = jz_llm.resolve()                     # 相当于请求线程内
        out = {}

        def worker():
            # 后台线程里现解析会读不到会话（等价于未登录），只有显式传 session 才可靠
            out["live"] = jz_llm.chat_json("S", "U", session=session)
            out["fresh"] = jz_llm.resolve().source      # 无会话上下文 → 回退全局配置

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=30)
        self.assertEqual(out["live"], {"案件名": "8·16 案"})
        self.assertEqual(out["fresh"], "global")

    def test_connection_ok_and_failure(self):
        ok, detail = jz_llm.test_connection(session=self._session("/openai/chat/completions"))
        self.assertTrue(ok, detail)
        self.assertIn("连通正常", detail)

        ok, detail = jz_llm.test_connection(session=self._session("/err401/chat/completions"))
        self.assertFalse(ok)
        self.assertIn("401", detail)

        ok, detail = jz_llm.test_connection(provider={"format": "openai", "url": ""})
        self.assertFalse(ok)
        self.assertIn("API 地址", detail)

    def test_connection_reports_plain_text_response(self):
        ok, detail = jz_llm.test_connection(session=self._session("/plain/chat/completions"))
        self.assertTrue(ok, detail)
        self.assertIn("不是 JSON", detail)


# ==========================================================================
# 5. 提示词
# ==========================================================================

class TestPrompts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jz-llm-prompt-")
        self._env = os.environ.get("JZTOOLS_DATA_ROOT")
        os.environ["JZTOOLS_DATA_ROOT"] = self.tmp

    def tearDown(self):
        if self._env is None:
            os.environ.pop("JZTOOLS_DATA_ROOT", None)
        else:
            os.environ["JZTOOLS_DATA_ROOT"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_and_default_fallback(self):
        defaults = {"system": "内置系统", "user_template": "{text}"}
        self.assertEqual(jz_llm.load_prompt("demo", defaults), defaults)
        jz_llm.save_prompt("demo", {"system": "改过的"})
        merged = jz_llm.load_prompt("demo", defaults)
        self.assertEqual(merged["system"], "改过的")
        self.assertEqual(merged["user_template"], "{text}")     # 未保存的键仍取默认

    def test_corrupt_file_falls_back(self):
        path = jz_llm.prompt_path("demo")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("[不是对象]")
        self.assertEqual(jz_llm.load_prompt("demo", {"system": "默认"}), {"system": "默认"})

    def test_render_only_replaces_given_keys(self):
        out = jz_llm.render("a {x} b {y} {未提供}", {"x": 1, "y": None})
        self.assertEqual(out, "a 1 b  {未提供}")

    def test_plugin_prompt_files_are_shared_with_legacy_location(self):
        """三个插件的提示词文件路径必须与历史位置一致（不产生数据迁移）。"""
        for pid in ("case-report", "character-graph", "file-filter"):
            self.assertTrue(jz_llm.prompt_path(pid).replace("\\", "/")
                            .endswith(f"plugins/{pid}/prompt.json"))


# ==========================================================================
# 6. 框架路由 / 后台路由 / 插件迁移口径（装配一个真实应用）
# ==========================================================================

# 本组要覆盖"框架路由 + admin + 三个业务插件"的完整链路。与 test_bg_image.py 同口径：
# 插件后端在框架里以 jztools_<插件ID> 为包名动态加载，这里用 importlib 复刻该加载方式，
# 挂到**本组自建的 Flask app** 上。
#
# 为什么不直接用 app.py 里那个模块级 app：Flask 的 app 是单例，一旦处理过请求就不能再
# 注册 before_request（插件 register() 与 register_plugin_backends 都要注册）。在同进程里
# 复用它会让**其它测试文件**后续装配时炸掉（test_admin_plugin_manager 就要再装配一次），
# 属于测试之间的相互污染。这里只从 app.py 取框架级视图函数重新挂载，并在 tearDown 里
# 清掉本组载入的插件模块，保证互不影响。
FRAMEWORK_LLM_ROUTES = (
    ("/api/llm/settings", "api_llm_settings", ("GET",)),
    ("/api/llm/settings", "api_llm_settings_save", ("POST",)),
    ("/api/llm/test", "api_llm_test", ("POST",)),
)
PLUGINS_UNDER_TEST = ("admin", "case-report", "character-graph", "file-filter")


def _load_plugin(pid):
    """按框架的加载方式（jztools_<id> 包 + backend/routes.py）载入插件后端。

    先清掉同名旧模块：其它测试文件（test_bg_image / test_filter_preview）也以同样的包名
    加载过插件，其模块级常量（如 CONFIG_FILE）指向各自的临时数据根；不清会拿到那份
    实例，读到已删除目录下的路径。
    """
    import importlib
    import importlib.util
    pkg_name = "jztools_" + pid.replace("-", "_")
    for name in [n for n in sys.modules
                 if n == pkg_name or n.startswith(pkg_name + ".")]:
        sys.modules.pop(name, None)
    backend = os.path.join(HERE, "plugins", pid, "backend")
    spec = importlib.util.spec_from_file_location(
        pkg_name, os.path.join(backend, "__init__.py"),
        submodule_search_locations=[backend])
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(pkg_name + ".routes")


class TestRoutesAndPlugins(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from flask import Flask
        cls.tmp = tempfile.mkdtemp(prefix="jz-llm-routes-")
        cls._env = os.environ.get("JZTOOLS_DATA_ROOT")
        os.environ["JZTOOLS_DATA_ROOT"] = cls.tmp
        import app as A
        cls.A = A
        A.init_data_root()                       # 把 tools.json 模板铺进临时数据根
        cls.app = Flask("jz-llm-routes-test")
        for rule, view_name, methods in FRAMEWORK_LLM_ROUTES:
            cls.app.add_url_rule(rule, view_func=getattr(A, view_name),
                                 methods=list(methods))
        cls.routes = {}
        for pid in PLUGINS_UNDER_TEST:
            routes = _load_plugin(pid)
            cls.routes[pid] = routes
            routes.register(cls.app)

    @classmethod
    def tearDownClass(cls):
        # 收拾本组留下的模块缓存，避免污染其它测试文件：
        # ① 插件模块（jztools_*）——它们指向本组的临时数据根（即将被删），
        #    且 test_bg_image / test_filter_preview 会自行重新加载；
        # ② app —— 沙箱型测试（test_admin_plugin_manager）靠"sys.path 优先 + sys.modules
        #    里没有 app"来加载**沙箱副本**的 app.py；本组 import 过仓库的 app.py，
        #    不清掉会让它拿到仓库那份（程序目录指向仓库而非沙箱）。
        for name in list(sys.modules):
            if name.startswith("jztools_"):
                sys.modules.pop(name, None)
        mod = sys.modules.get("app")
        if mod and os.path.abspath(getattr(mod, "__file__", "") or "").startswith(HERE):
            del sys.modules["app"]
        if cls._env is None:
            os.environ.pop("JZTOOLS_DATA_ROOT", None)
        else:
            os.environ["JZTOOLS_DATA_ROOT"] = cls._env
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _client(self, username="admin", password="admin123"):
        c = self.app.test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        return c

    def test_plugins_registered(self):
        for pid in PLUGINS_UNDER_TEST:
            self.assertIn(pid, self.routes)

    def test_anonymous_gets_401(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/api/llm/settings").status_code, 401)
        self.assertEqual(c.post("/api/llm/test", json={}).status_code, 401)

    def test_admin_mode_blocks_self_service(self):
        admin = self._client()
        admin.post("/api/admin/llm-settings", json={
            "mode": "admin", "fallback": True,
            "provider": {"format": "openai", "url": "http://g/chat/completions",
                         "api_key": "gk", "model": "gm"}})
        d = admin.get("/api/llm/settings").get_json()
        self.assertFalse(d["can_self_config"])
        self.assertEqual(d["mode"], "admin")
        r = admin.post("/api/llm/settings", json={"format": "openai",
                                                  "url": "http://u/x", "api_key": "uk"})
        self.assertEqual(r.status_code, 403)

    def test_user_mode_self_service_and_masking(self):
        admin = self._client()
        admin.post("/api/admin/llm-settings", json={
            "mode": "user", "fallback": True,
            "provider": {"format": "openai", "url": "http://g/chat/completions",
                         "api_key": "gk", "model": "gm"}})
        d = admin.get("/api/llm/settings").get_json()
        self.assertTrue(d["can_self_config"])
        self.assertEqual([f["id"] for f in d["formats"]],
                         ["openai", "anthropic", "ollama", "custom"])
        # 保存个人配置
        r = admin.post("/api/llm/settings", json={
            "format": "ollama", "url": "http://127.0.0.1:11434/api/chat",
            "api_key": "sk-mine", "model": "qwen"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        d = admin.get("/api/llm/settings").get_json()
        self.assertEqual(d["mine"]["format"], "ollama")
        self.assertEqual(d["mine"]["api_key"], "••••••••")     # 不回明文
        self.assertTrue(d["mine"]["api_key_set"])
        self.assertEqual(d["effective"]["source"], "user")
        # 掩码回传 = 不修改已保存的 Key
        admin.post("/api/llm/settings", json={
            "format": "ollama", "url": "http://127.0.0.1:11434/api/chat",
            "api_key": "••••••••", "model": "qwen2"})
        import jz_api
        self.assertEqual(jz_api.get_user_llm("admin")["api_key"], "sk-mine")

    def test_admin_settings_requires_admin_role(self):
        import json as _json
        p = os.path.join(self.tmp, "config", "admin.json")
        cfg = _json.load(open(p, encoding="utf-8"))
        users = cfg["units"][0]["departments"][0]["users"]
        if not any(u["username"] == "zhangsan" for u in users):
            users.append({"username": "zhangsan", "password": users[0]["password"],
                          "name": "张三", "idcard": "", "role": "role-case-handler",
                          "permissions": ["case-report"],
                          "llm": {"format": "openai", "base_url": "", "api_key": "", "model": ""}})
            with open(p, "w", encoding="utf-8") as f:
                _json.dump(cfg, f, ensure_ascii=False, indent=2)
        c = self._client("zhangsan")
        self.assertEqual(c.get("/api/admin/llm-settings").status_code, 403)
        self.assertEqual(c.get("/admin/llm").status_code, 302)
        # 普通用户的卡片列表里 llm 项不可访问
        mods = {m["id"]: m for m in c.get("/api/admin/summary").get_json()["modules"]}
        self.assertFalse(mods["llm"]["allowed"])

    def test_admin_settings_masks_key_and_keeps_on_blank(self):
        admin = self._client()
        admin.post("/api/admin/llm-settings", json={
            "mode": "admin", "fallback": True,
            "provider": {"format": "openai", "url": "http://g/chat/completions",
                         "api_key": "sk-global", "model": "gm"}})
        d = admin.get("/api/admin/llm-settings").get_json()
        self.assertEqual(d["provider"]["api_key"], "••••••••")
        self.assertTrue(d["provider"]["configured"])
        self.assertEqual([f["id"] for f in d["formats"]],
                         ["openai", "anthropic", "ollama", "custom"])
        # 留空/掩码 → 保留原 Key
        admin.post("/api/admin/llm-settings", json={
            "mode": "admin", "fallback": True,
            "provider": {"format": "openai", "url": "http://g2/chat/completions",
                         "api_key": "", "model": "gm2"}})
        self.assertEqual(jz_llm.load_global()["provider"]["api_key"], "sk-global")
        self.assertEqual(jz_llm.load_global()["provider"]["url"], "http://g2/chat/completions")

    def test_settings_partial_update_keeps_other_card(self):
        """后台两张卡各存各的：局部更新只改传进来的字段（界面上「调用模式」自动保存
        与「全局接入配置」的保存按钮必须互不覆盖）。"""
        admin = self._client()
        admin.post("/api/admin/llm-settings", json={
            "mode": "admin", "fallback": True,
            "provider": {"format": "openai", "url": "http://g/chat/completions",
                         "api_key": "sk-global", "model": "gm"}})
        # ① 调用模式卡自动保存：只提交 mode → 接入配置原样不动
        r = admin.post("/api/admin/llm-settings", json={"mode": "user"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        cfg = jz_llm.load_global()
        self.assertEqual(cfg["mode"], "user")
        self.assertEqual(cfg["provider"]["url"], "http://g/chat/completions")
        self.assertEqual(cfg["provider"]["api_key"], "sk-global")
        self.assertEqual(cfg["provider"]["model"], "gm")
        # ② 回退开关同卡自动保存：只提交 fallback
        admin.post("/api/admin/llm-settings", json={"fallback": False})
        cfg = jz_llm.load_global()
        self.assertFalse(cfg["fallback"])
        self.assertEqual(cfg["mode"], "user")                    # 模式没被带回去
        self.assertEqual(cfg["provider"]["api_key"], "sk-global")
        # ③ 全局接入配置卡的保存按钮：只提交 provider → **不影响调用模式**
        r = admin.post("/api/admin/llm-settings", json={
            "provider": {"format": "ollama", "url": "http://127.0.0.1:11434/api/chat",
                         "api_key": "", "model": "qwen"}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        cfg = jz_llm.load_global()
        self.assertEqual(cfg["provider"]["format"], "ollama")
        self.assertEqual(cfg["provider"]["url"], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(cfg["mode"], "user")                    # 模式仍是上一步的值
        self.assertFalse(cfg["fallback"])
        # ④ 非法模式仍然拒绝，且不落盘
        r = admin.post("/api/admin/llm-settings", json={"mode": "乱写"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(jz_llm.load_global()["mode"], "user")

    # ---------- 用户级配置的三条同步链路（同一份数据源：admin.json 的 user.llm）----------

    def _ensure_user(self, username, llm=None):
        """建一个普通账号（用于验证"管理员建号时配置的 LLM"与"用户自己改"的双向同步）。"""
        admin = self._client()
        cfg = admin.get("/api/admin/users").get_json()
        if any(u["username"] == username for u in cfg["users"]):
            return admin
        unit_id = cfg["units"][0]["id"]
        dept = next(d for d in cfg["departments"] if d["unit_id"] == unit_id)
        r = admin.post("/api/admin/users", json={
            "username": username, "name": username, "password": "user123456",
            "unit_id": unit_id, "department_id": dept["id"], "role": "role-case-handler",
            "permissions": ["case-report"], "llm": llm or {},
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        return admin

    def _as_user(self, username, password="user123456"):
        c = self.app.test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        return c

    def test_1_admin_created_llm_visible_to_user(self):
        """链路①：管理员建号时配的大模型信息 → 用户端「⋯ → 大模型设置」能看到。"""
        admin = self._client()
        admin.post("/api/admin/llm-settings", json={"mode": "user", "fallback": True})
        self._ensure_user("u_sync1", llm={
            "format": "anthropic", "base_url": "https://api.anthropic.com/v1/messages",
            "api_key": "sk-created-by-admin", "model": "claude-x"})
        c = self._as_user("u_sync1")
        d = c.get("/api/llm/settings").get_json()
        self.assertTrue(d["can_self_config"])                 # user 模式 → 自助入口可见
        self.assertEqual(d["mine"]["format"], "anthropic")
        self.assertEqual(d["mine"]["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(d["mine"]["model"], "claude-x")
        self.assertTrue(d["mine"]["api_key_set"])             # Key 只回"已配置"，不回明文
        self.assertEqual(d["mine"]["api_key"], "••••••••")
        self.assertTrue(d["mine"]["configured"])
        self.assertEqual(d["effective"]["source"], "user")    # 生效来源就是本人那份

    def test_2_user_save_visible_in_admin_user_list(self):
        """链路②：用户自己保存 → 管理员「人员管理」列表看到的就是新值。"""
        admin = self._client()
        admin.post("/api/admin/llm-settings", json={"mode": "user", "fallback": True})
        self._ensure_user("u_sync2")
        c = self._as_user("u_sync2")
        r = c.post("/api/llm/settings", json={
            "format": "ollama", "url": "http://127.0.0.1:11434/api/chat",
            "api_key": "sk-set-by-user", "model": "qwen2.5"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        row = next(u for u in admin.get("/api/admin/users").get_json()["users"]
                   if u["username"] == "u_sync2")
        self.assertEqual(row["llm"]["format"], "ollama")
        self.assertEqual(row["llm"]["base_url"], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(row["llm"]["model"], "qwen2.5")
        # API Key 只回掩码（明文不进浏览器）；真正落盘的值用 provider 侧读出来核
        self.assertEqual(row["llm"]["api_key"], "••••••••")
        self.assertTrue(row["llm"]["api_key_set"])
        self.assertNotIn("sk-set-by-user", json.dumps(row, ensure_ascii=False))
        import jz_api
        self.assertEqual(jz_api.get_user_llm("u_sync2")["api_key"], "sk-set-by-user")

    def test_3_admin_edit_visible_to_user(self):
        """链路③：管理员在「人员管理」改用户的配置 → 用户端立刻看到新值。"""
        admin = self._client()
        admin.post("/api/admin/llm-settings", json={"mode": "user", "fallback": True})
        self._ensure_user("u_sync3", llm={
            "format": "openai", "base_url": "http://old/chat/completions",
            "api_key": "sk-old", "model": "old-model"})
        r = admin.put("/api/admin/users/u_sync3", json={
            "name": "u_sync3", "role": "role-case-handler", "permissions": ["case-report"],
            "llm": {"format": "openai", "base_url": "http://new/chat/completions",
                    "api_key": "sk-new", "model": "new-model"}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        d = self._as_user("u_sync3").get("/api/llm/settings").get_json()
        self.assertEqual(d["mine"]["url"], "http://new/chat/completions")
        self.assertEqual(d["mine"]["model"], "new-model")
        self.assertTrue(d["mine"]["api_key_set"])
        # 只改地址/模型、Key 回传掩码 → 保留原 Key（不是清空）
        admin.put("/api/admin/users/u_sync3", json={
            "name": "u_sync3", "role": "role-case-handler", "permissions": ["case-report"],
            "llm": {"format": "openai", "base_url": "http://new2/chat/completions",
                    "api_key": "••••••••", "model": "new-model-2"}})
        import jz_api
        self.assertEqual(jz_api.get_user_llm("u_sync3")["api_key"], "sk-new")
        self.assertEqual(jz_api.get_user_llm("u_sync3")["url"], "http://new2/chat/completions")

    def test_plugin_legacy_llm_migration(self):
        """插件历史 llm 段：**先收编进统一配置（加密）再清除插件侧明文副本**，且幂等。"""
        import json as _json
        pdir = os.path.join(self.tmp, "plugins", "case-report")
        os.makedirs(pdir, exist_ok=True)
        cfg_path = os.path.join(pdir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            _json.dump({"llm": {"base_url": "https://api.deepseek.com/v1/chat/completions",
                                "api_key": "sk-legacy-plaintext-0001",
                                "model": "deepseek-v4-flash"}}, f)
        # 统一配置清空，模拟"还没配过"的现场
        jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {}})
        rep = self.routes["admin"].migrate_plugin_legacy_llm()
        self.assertEqual(rep["adopted_from"], "case-report")
        self.assertIn("case-report", rep["cleared"])
        # ① 收编：统一配置可用，且**落盘是密文**
        provider = jz_llm.load_global()["provider"]
        self.assertTrue(jz_llm.provider_usable(provider))
        self.assertEqual(provider["url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(provider["model"], "deepseek-v4-flash")
        self.assertEqual(provider["api_key"], "sk-legacy-plaintext-0001")
        with open(jz_llm.config_path(), encoding="utf-8") as f:
            self.assertNotIn("sk-legacy-plaintext-0001", f.read())
        # ② 清除：插件侧不再有 llm 段，也没有明文 Key
        with open(cfg_path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("llm", _json.loads(raw))
        self.assertNotIn("sk-legacy-plaintext-0001", raw)
        # ③ 幂等
        self.assertIsNone(self.routes["admin"].migrate_plugin_legacy_llm())
        # 复原统一配置，避免影响同类其它用例
        jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {}})

    def test_plugin_config_reports_unified_status_without_credentials(self):
        admin = self._client()
        jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
            "format": "openai", "url": "http://g/chat/completions",
            "api_key": "sk-global", "model": "gm"}})
        for pid in ("case-report", "character-graph", "file-filter"):
            d = admin.get(f"/api/{pid}/config").get_json()
            self.assertTrue(d.get("llm_configured"), pid)
            self.assertEqual(d.get("llm_source"), "global", pid)
            blob = json.dumps(d, ensure_ascii=False)
            self.assertNotIn("sk-global", blob, pid)         # 插件接口不得回传凭据
            self.assertNotIn("api_key", blob, pid)
            self.assertNotIn("base_url", blob, pid)

    def test_plugin_llm_call_uses_unified_config(self):
        """插件调用必须落到统一配置指向的地址（桩服务上可见），而不是插件自有配置。"""
        import io as _io
        srv = MockServer()
        try:
            admin = self._client()
            jz_llm.save_global({"mode": "admin", "fallback": True, "provider": {
                "format": "openai", "url": srv.base + "/openai/mappings/chat/completions",
                "api_key": "sk-global", "model": "gm"}})
            # 过滤器：同步程序化接口（/apply），最直接地验证"统一配置被用上"
            body = {"rows": [["开始时间", "姓名"], ["2026-01-01", "张三"]],
                    "mode": "llm", "columns": ["姓名"]}
            r = admin.post("/api/file-filter/apply", json=body)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
            self.assertTrue(r.get_json()["mode"] == "llm")
            self.assertTrue(srv.requests, "统一大模型未被调用")
            self.assertEqual(srv.last()["path"], "/openai/mappings/chat/completions")
            # 凭据来自统一配置（插件配置里已不再保存 Key）
            self.assertEqual(srv.last()["headers"]["authorization"], "Bearer sk-global")
            # 人物星图：后台任务路径（验证会话跨线程传递）
            data = {"file": (_io.BytesIO("张三与李四是同案。".encode("utf-8")), "t.txt")}
            r = admin.post("/api/character-graph/analyze", data=data,
                           content_type="multipart/form-data")
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
            tid = r.get_json()["task_id"]
            for _ in range(60):
                time.sleep(0.2)
                d = admin.get(f"/api/character-graph/result/{tid}").get_json()
                if d["status"] in ("done", "error"):
                    break
            # 桩返回的不是人物 JSON → 任务 error，但错误必须是"领域校验"而非"未配置"
            self.assertEqual(d["status"], "error", d)
            self.assertNotIn("尚未配置", d.get("detail", ""))
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
