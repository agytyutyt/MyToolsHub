# -*- coding: utf-8 -*-
"""S2-3 worker 并发：声明 concurrency>1 并行、默认 1 仍串行、响应按 request_id 正确配对。

跑法：python -m unittest test_client_plugin_concurrency -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, ROOT)

ROUTES = """import time as _t
from flask import jsonify


def register(app):
    @app.get('/count')
    def _c():
        return jsonify({'n': 1})

    @app.get('/slow/<float:s>')
    def _slow(s):
        _t.sleep(s)
        return jsonify({'slept': s})
"""


def _mk(lp, pid, concurrency=None):
    d = os.path.join(lp, pid)
    os.makedirs(os.path.join(d, "backend"), exist_ok=True)
    os.makedirs(os.path.join(d, "frontend"), exist_ok=True)
    m = {"id": pid, "name": pid, "icon": "x", "version": "1.0.0",
         "runtime": "local", "kind": "local",
         "entry": {"frontend": "index.html", "backend": "routes.py"},
         "timeout_ms": 60000, "resident": True}
    if concurrency is not None:
        m["concurrency"] = concurrency
    with io.open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    open(os.path.join(d, "backend", "__init__.py"), "w").close()
    with io.open(os.path.join(d, "backend", "routes.py"), "w", encoding="utf-8") as f:
        f.write(ROUTES)
    with io.open(os.path.join(d, "frontend", "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>x</html>")
    return d


class ConcurrencyTest(unittest.TestCase):
    def _boot(self, concurrency):
        work = tempfile.mkdtemp(prefix="jz-conc-")
        lp = os.path.join(work, "lp")
        os.makedirs(lp, exist_ok=True)
        d = _mk(lp, "local-conc", concurrency)
        from plugin_executor import PluginExecutor
        pylibs = os.path.join(work, "pylibs")
        os.makedirs(pylibs, exist_ok=True)
        ex = PluginExecutor(sys.executable, os.path.join(ROOT, "client", "plugin_host.py"),
                            pylibs, work, lambda m: None)
        with io.open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["_dir_abs"] = d
        self.work = work
        return ex, manifest

    def tearDown(self):
        ex = getattr(self, "ex", None)
        if ex is not None:
            try:
                ex.shutdown()
            except Exception:
                pass
        shutil.rmtree(getattr(self, "work", ""), ignore_errors=True)

    def _three_slow(self, ex, manifest):
        """并发跑 0.3 / 0.6 / 0.9 三个请求，返回 (总耗时, {sec: (status, body)})。"""
        res = {}
        def run(sec):
            st, _hd, body = ex.call(manifest, "GET", "/slow/%s" % sec, {}, b"", 30000)
            res[sec] = (st, json.loads(body))
        ths = [threading.Thread(target=run, args=(s,)) for s in ("0.9", "0.6", "0.3")]
        t0 = time.perf_counter()
        for t in ths:
            t.start()
        for t in ths:
            t.join(30)
        return time.perf_counter() - t0, res

    def test_declared_concurrency_runs_parallel_and_pairs_by_id(self):
        """声明 concurrency=3：三个并发请求应并行（总耗时≈最慢那个），且各自拿到自己的结果。"""
        self.ex, manifest = self._boot(3)
        ex = self.ex
        ex.call(manifest, "GET", "/count", {}, b"", 30000)      # 预热，隔离冷启
        dt, res = self._three_slow(ex, manifest)
        self.assertLess(dt, 2.4, "并发 3 时应≈0.9s 完成，实测 %.2fs" % dt)
        for sec in ("0.3", "0.6", "0.9"):
            self.assertIn(sec, res, "缺结果：%s" % sec)
            st, body = res[sec]
            self.assertEqual(st, 200, (sec, body))
            self.assertAlmostEqual(float(body.get("slept", -1)), float(sec), places=1,
                                   msg="request_id 配对错位：请求 %s 拿到 %r" % (sec, body))

    def test_default_concurrency_is_still_serial(self):
        """不声明 → 默认 1：必须与历史一致（串行），三个 0.3+0.6+0.9 请求≈1.8s。"""
        self.ex, manifest = self._boot(None)
        ex = self.ex
        ex.call(manifest, "GET", "/count", {}, b"", 30000)
        dt, res = self._three_slow(ex, manifest)
        self.assertGreater(dt, 1.6, "默认 1 必须串行（≈1.8s），实测 %.2fs" % dt)
        self.assertEqual(sorted(res), ["0.3", "0.6", "0.9"])

    def test_timeout_still_applies_in_concurrent_mode(self):
        """并发模式下的超时仍生效（不能因为换路径就把超时丢了）。"""
        self.ex, manifest = self._boot(2)
        ex = self.ex
        ex.call(manifest, "GET", "/count", {}, b"", 30000)
        t0 = time.perf_counter()
        st, _hd, body = ex.call(manifest, "GET", "/slow/5.0", {}, b"", 1500)   # 1.5s 超时
        dt = time.perf_counter() - t0
        self.assertEqual(st, 504, (st, body))
        self.assertLess(dt, 3.5, "超时应按时返回，实测 %.2fs" % dt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
