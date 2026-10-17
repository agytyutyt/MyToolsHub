# -*- coding: utf-8 -*-
"""S2-5 审计异步化：请求路径不触文件 IO + 批量落盘等价 + 队列满降级 + 退出 flush。

跑法：python -m unittest test_client_plugin_audit_async -v
"""
import io
import os
import queue
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, ROOT)

import plugin_executor as pe          # noqa: E402


class AuditAsyncTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="jz-audit-")
        self.ex = pe.PluginExecutor(sys.executable, os.path.join(ROOT, "client", "plugin_host.py"),
                                    os.path.join(self.work, "pylibs"), self.work, lambda m: None)

    def tearDown(self):
        try:
            self.ex.flush_audit()
        except Exception:
            pass
        shutil.rmtree(self.work, ignore_errors=True)

    def _lines(self):
        p = self.ex._audit_path
        if not os.path.isfile(p):
            return []
        with io.open(p, encoding="utf-8") as f:
            return [l for l in f.read().splitlines() if l.strip()]

    def test_request_path_enqueues_only(self):
        """契约：落盘**不得发生在调用线程**（请求路径只入队；落盘由 audit-flush 线程做）。

        注意不能断言"入队期间零落盘"——异步设计下后台线程可能已经刷了一批（那正是本意）。
        """
        threads = []
        real = self.ex._audit_write

        def spy(lines):
            threads.append(threading.current_thread().name)
            return real(lines)

        self.ex._audit_write = spy
        for i in range(20):
            self.ex._audit("p1", "GET", "/x/%d" % i, 0.01, "200", user="admin")
        self.assertNotIn(threading.current_thread().name, threads,
                         "请求线程不得落盘（应只入队）：%r" % threads)
        self.ex.flush_audit()
        lines = self._lines()
        self.assertEqual(len(lines), 20, "flush 后应恰好 20 行：%d" % len(lines))
        self.assertIn("user=admin p1 GET /x/19", lines[-1], lines[-1])
        self.assertTrue(all("user=admin" in l for l in lines))

    def test_order_and_content_preserved(self):
        for i in range(5):
            self.ex._audit("p2", "POST", "/y/%d" % i, 0.5, "ok", user="-")
        self.ex.flush_audit()
        lines = self._lines()
        self.assertEqual(len(lines), 5)
        for i, l in enumerate(lines):
            self.assertIn("/y/%d" % i, l, "顺序必须保持：%s" % l)
            self.assertIn("user=- ", l)

    def test_queue_full_degrades_without_blocking(self):
        """队列满：丢弃 + 计数，绝不阻塞（把 flusher 停掉以稳定复现）。"""
        self.ex._ensure_audit_flusher = lambda: None        # 不让后台线程掏空队列
        self.ex._audit_q = queue.Queue(maxsize=3)
        for i in range(10):
            self.ex._audit("p3", "GET", "/z/%d" % i, 0.01, "200", user="-")
        self.assertEqual(self.ex._audit_dropped, 7, "应丢弃 7 行（10 入队 / 容量 3）")

    def test_flush_is_idempotent_and_writes_remaining(self):
        self.ex._audit("p4", "GET", "/q", 0.01, "200", user="-")
        self.ex.flush_audit()
        n1 = len(self._lines())
        self.ex.flush_audit()                                # 再 flush 不应重复写
        self.assertEqual(len(self._lines()), n1)

    def test_shutdown_flushes_audit(self):
        self.ex._audit("p5", "GET", "/s", 0.01, "200", user="-")
        self.ex.shutdown()                                   # 退出路径：先 flush 审计再收割
        self.assertEqual(len(self._lines()), 1, "shutdown 必须先把审计落盘")


if __name__ == "__main__":
    unittest.main(verbosity=2)
