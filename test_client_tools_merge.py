# -*- coding: utf-8 -*-
"""能力层 `/api/tools` 的**内容级**门禁（S1-2 前置，2026-10-17）。

为什么需要它：t13 的 D1 只断言「死地址下仍 200 且快」，客户端 smoke 只断言本机显示退役与
worker 往返 —— **都不校验网格内容**；而这个端点历史上出过 F5 事故（辅助函数插错位置 →
`/api/tools` 全局误走降级分支）。S1-2 要动「取远程 + 合并」这一段，没有内容级断言就是盲改。

本文件用**假上游**（Flask + `werkzeug.serving.make_server`，随机端口）提供真实 `/api/tools`，
并用 `user: None` 让账号模式自动关闭（走 legacy 合并分支）。断言：

  ① 合并结果**同时**含远程条目与本机条目；
  ② 上游未给 `local` 分类时自动补上，且给了就不重复补；
  ③ 上游不可达时**降级为仅本地**（`degraded=True`）而不是报错；
  ④ 上游返回 403 壳门语义时降级并带 `gate_blocked`。

跑法：
    python -m unittest test_client_tools_merge -v
"""
import json
import os
import shutil
import sys
import tempfile
import time
import threading
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, ROOT)

from werkzeug.serving import make_server          # noqa: E402


def _mk_local_plugin(lp_dir, pid):
    """最小本机插件（kind=local，无需真实 worker——本测试只验清单合并）。"""
    d = os.path.join(lp_dir, pid)
    os.makedirs(os.path.join(d, "backend"), exist_ok=True)
    os.makedirs(os.path.join(d, "frontend"), exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"id": pid, "name": pid, "icon": "🧪", "version": "1.0.0",
                   "runtime": "local", "kind": "local",
                   "entry": {"frontend": "index.html", "backend": "routes.py"}},
                  f, ensure_ascii=False)
    open(os.path.join(d, "backend", "__init__.py"), "w").close()
    with open(os.path.join(d, "backend", "routes.py"), "w", encoding="utf-8") as f:
        f.write("from flask import jsonify\n\n\n"
                "def register(app):\n"
                "    @app.get('/count')\n"
                "    def _c():\n"
                "        return jsonify({'n': 1})\n")
    with open(os.path.join(d, "frontend", "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>x</html>")


class _FakeUpstream:
    """假上游：/api/tools 返回给定载荷；/api/session 回 user=None（关掉账号模式）。"""

    def __init__(self, tools_payload, status=200, gate=False):
        from flask import Flask, jsonify
        app = Flask("fake-upstream-%d" % id(self))

        @app.get("/login")
        def _login():
            return "ok"

        @app.get("/api/session")
        def _session():
            return jsonify({"user": None})

        @app.get("/api/tools")
        def _tools():
            self.hits += 1
            if gate:
                return jsonify({"error": "shell_required"}), 403, {"X-JZ-Gate": "shell_required"}
            return jsonify(tools_payload), status

        self.hits = 0
        self._srv = make_server("127.0.0.1", 0, app, threaded=True)
        self.port = self._srv.server_port
        threading.Thread(target=self._srv.serve_forever, daemon=True,
                         name="fake-upstream").start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        try:
            self._srv.shutdown()
        except Exception:
            pass


class ToolsMergeTest(unittest.TestCase):
    REMOTE = {"tools": [{"id": "remote-a", "name": "远程A", "icon": "A", "order": 10,
                         "entry": "index.html", "features": []}],
              "categories": [{"id": "cat1", "name": "分类一"}]}

    def _boot(self, tools_payload=None, dead=False, gate=False):
        self.work = tempfile.mkdtemp(prefix="jz-tools-merge-")
        os.environ["JZCLIENT_DATA_DIR"] = os.path.join(self.work, "clientdata")
        lp_dir = os.path.join(self.work, "lp")
        os.makedirs(lp_dir, exist_ok=True)
        _mk_local_plugin(lp_dir, "local-x")
        static_dir = os.path.join(self.work, "static")
        os.makedirs(static_dir, exist_ok=True)
        self.up = None
        if dead:
            address = "http://127.0.0.1:59999"      # 恒不可达（t13 同款）
        else:
            self.up = _FakeUpstream(tools_payload if tools_payload is not None else self.REMOTE,
                                    gate=gate)
            address = self.up.base
        from plugin_executor import PluginExecutor
        import capability_app
        build_cfg = {"_program_dir": os.path.join(self.work, "prog"),
                     "product": {"version": "0.0.1"}, "plugin_api": 1,
                     "paths": {"data_dirname": "D_client_data"}}
        os.makedirs(build_cfg["_program_dir"], exist_ok=True)
        pylibs = os.path.join(self.work, "pylibs")
        os.makedirs(pylibs, exist_ok=True)
        executor = PluginExecutor(sys.executable, os.path.join(ROOT, "client", "plugin_host.py"),
                                  pylibs, self.work, lambda m: None)
        app = capability_app.create_capability_app(
            build_cfg, {"remote_address": address}, address, static_dir, lp_dir,
            os.path.join(ROOT, "client", "fallback.html"),
            data_dir=os.path.join(self.work, "data"), executor=executor, log=lambda m: None,
            shell_token="merge-test-shell")
        c = app.test_client()
        c.get("/shell-boot?_shell=merge-test-shell&next=/")   # 壳自证种 Cookie
        return c, executor

    def tearDown(self):
        if getattr(self, "up", None):
            self.up.stop()
        shutil.rmtree(getattr(self, "work", ""), ignore_errors=True)

    # ---- ① 远程 + 本机 合并 ----
    def test_merged_contains_remote_and_local(self):
        c, _ex = self._boot()
        r = c.get("/api/tools")
        self.assertEqual(r.status_code, 200, r.data[:200])
        d = r.get_json()
        ids = [t.get("id") for t in d.get("tools") or []]
        self.assertIn("remote-a", ids, "远程条目应在合并结果里：%s" % ids)
        self.assertIn("local-x", ids, "本机条目应在合并结果里：%s" % ids)
        self.assertNotEqual(d.get("degraded"), True, "上游可达时不应标记降级")

    # ---- ② local 分类补齐且不重复 ----
    def test_local_category_appended_once(self):
        c, _ex = self._boot()
        d = c.get("/api/tools").get_json()
        cats = [x.get("id") for x in (d.get("categories") or [])]
        self.assertEqual(cats.count("local"), 1, "local 分类应恰好出现一次：%s" % cats)
        self.assertIn("cat1", cats, "上游分类应保留：%s" % cats)

    def test_local_category_not_duplicated_when_upstream_has_it(self):
        payload = dict(self.REMOTE)
        payload["categories"] = [{"id": "local", "name": "本地插件"}]
        c, _ex = self._boot(tools_payload=payload)
        d = c.get("/api/tools").get_json()
        cats = [x.get("id") for x in (d.get("categories") or [])]
        self.assertEqual(cats.count("local"), 1, "上游已给 local 时不应再补一份：%s" % cats)

    # ---- ③ 上游不可达 → 仅本地 + degraded ----
    def test_remote_down_degrades_to_local_only(self):
        c, _ex = self._boot(dead=True)
        r = c.get("/api/tools")
        self.assertEqual(r.status_code, 200, r.data[:200])
        d = r.get_json()
        ids = [t.get("id") for t in d.get("tools") or []]
        self.assertIn("local-x", ids)
        self.assertNotIn("remote-a", ids)
        self.assertEqual(d.get("degraded"), True, "不可达应标记 degraded：%s" % d)

    # ---- ④ 上游壳门 403 → 降级 + gate_blocked ----
    def test_upstream_gate_blocked_marks_flag(self):
        c, _ex = self._boot(gate=True)
        d = c.get("/api/tools").get_json()
        ids = [t.get("id") for t in d.get("tools") or []]
        self.assertIn("local-x", ids)
        self.assertEqual(d.get("gate_blocked"), True, "壳门拒绝应带 gate_blocked：%s" % d)


    # ---- S1-2：短 TTL 缓存（TTL 内第 2 次不再打上游）----
    def test_second_call_hits_cache_and_skips_upstream(self):
        c, _ex = self._boot()
        r1 = c.get("/api/tools")
        self.assertEqual(r1.headers.get("X-JZ-Tools-Cache"), "miss", r1.headers)
        hits_first = self.up.hits
        time.sleep(0.2)                       # 仍在 5s TTL 内
        r2 = c.get("/api/tools")
        self.assertEqual(r2.headers.get("X-JZ-Tools-Cache"), "hit", r2.headers)
        self.assertEqual(self.up.hits, hits_first, "TTL 内不应再打上游")
        self.assertEqual(r2.get_json(), r1.get_json(), "缓存命中应返回同一载荷")

    # ---- S1-2：装卸/启停立即失效缓存（不能等 TTL）----
    def test_plugin_state_change_invalidates_cache(self):
        c, _ex = self._boot()
        ids = [t.get("id") for t in c.get("/api/tools").get_json()["tools"]]
        self.assertIn("local-x", ids)
        r = c.post("/local-api/plugins/state", json={"id": "local-x", "enabled": False})
        self.assertEqual(r.status_code, 200, r.data[:200])
        ids2 = [t.get("id") for t in c.get("/api/tools").get_json()["tools"]]
        self.assertNotIn("local-x", ids2, "停用后应立即消失（缓存须被失效）：%s" % ids2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
