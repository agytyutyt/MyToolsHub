# -*- coding: utf-8 -*-
"""共享盘（UNC）加固单元测试（P2-2，2026-10-17）。

覆盖：
  ① 路径归一化——UNC 两形态（``\\\\?\\UNC\\`` / ``//?/UNC/``）、本地长路径前缀
     ``\\\\?\\C:\\``、混合分隔符、含空格与中文的路径、以及"未命中前缀一律不误改"；
  ② 限时 IO 与四类可区分文案（未配置 / 不存在 / 不可达 / 超时 / 解析失败）；
  ③ ShareSource 端到端（含中文+空格目录与文件名的索引读取 + 包复制 + sha256 透传）；
  ④ admin 侧 ``plugin_admin.read_index`` 的同一口径（长路径前缀可读、错误分类）。
  ⑤ 防分叉：同一纯函数在 client 与 admin 侧是逐字一致的兄弟副本（C-11 禁止跨树
     import），用"行为一致"锁定，任一侧改了另一侧没改即红。

跑法：
    python -m unittest test_plugin_source_unc -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "client"))
sys.path.insert(0, os.path.join(HERE, "plugins", "admin", "backend"))

import plugin_source as ps          # noqa: E402
import plugin_admin as pa           # noqa: E402

BS = chr(92)
UNC = BS + BS


def _make_index(tmp, pkg_name="包 名 v1.0.zip", payload=b"PK\x03\x04unc-test"):
    """建一个「中文+空格目录 / 中文+空格文件名」的共享盘索引，返回 (index_path, pkg_path)。"""
    d = os.path.join(tmp, "索 引 目 录")
    os.makedirs(d, exist_ok=True)
    pkg = os.path.join(d, pkg_name)
    with io.open(pkg, "wb") as f:
        f.write(payload)
    idx = os.path.join(d, "index.json")
    with io.open(idx, "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "packages": [
            {"id": "local-unc", "version": "1.0", "file": pkg_name,
             "sha256": pa.sha256_file(pkg), "size": os.path.getsize(pkg)}]},
            f, ensure_ascii=False)
    return idx, pkg


class NormalizeSharePathTest(unittest.TestCase):
    def test_long_unc_prefix_stripped(self):
        self.assertEqual(ps.normalize_share_path(UNC + "?" + BS + "UNC" + BS + "srv" + BS + "共享 盘" + BS + "dir" + BS + "i.json"),
                         UNC + "srv" + BS + "共享 盘" + BS + "dir" + BS + "i.json")

    def test_long_drive_prefix_stripped(self):
        self.assertEqual(ps.normalize_share_path(UNC + "?" + BS + "C:" + BS + "a b" + BS + "中文" + BS + "i.json"),
                         "C:" + BS + "a b" + BS + "中文" + BS + "i.json")

    def test_forward_slash_variant(self):
        self.assertEqual(ps.normalize_share_path("//?/UNC/srv/share/i.json"),
                         UNC + "srv" + BS + "share" + BS + "i.json")

    def test_mixed_separators_unified(self):
        self.assertEqual(ps.normalize_share_path(UNC + "?" + BS + "UNC/srv/混合/i.json"),
                         UNC + "srv" + BS + "混合" + BS + "i.json")

    def test_plain_paths_untouched(self):
        for p in (UNC + "srv" + BS + "share" + BS + "i.json",
                  "C:" + BS + "local" + BS + "i.json",
                  "D:" + BS + "中文 目录" + BS + "i.json",
                  "Z:" + BS + "映射盘" + BS + "i.json", "", None):
            self.assertEqual(ps.normalize_share_path(p), p)

    def test_sibling_copies_behave_identically(self):
        """client 侧与 admin 侧的纯函数是刻意的兄弟副本——行为必须一致（防分叉）。"""
        samples = [UNC + "?" + BS + "UNC" + BS + "s" + BS + "i.json",
                   UNC + "?" + BS + "C:" + BS + "d" + BS + "i.json",
                   "//?/UNC/s/i.json", UNC + "s" + BS + "i.json",
                   "C:" + BS + "d" + BS + "i.json", "", None]
        for p in samples:
            self.assertEqual(ps.normalize_share_path(p), pa.normalize_share_path(p), repr(p))


class TimedIoTest(unittest.TestCase):
    def test_helper_returns_timeout(self):
        t0 = time.time()
        ok, val = ps._with_timeout(lambda: (time.sleep(3), "x")[1], 0.2)
        self.assertFalse(ok)
        self.assertEqual(val, "timeout")
        self.assertLess(time.time() - t0, 2.0, "限时应立即返回，不等待被阻塞线程")

    def test_helper_returns_value_and_exception(self):
        ok, val = ps._with_timeout(lambda: 42, 1)
        self.assertTrue(ok)
        self.assertEqual(val, 42)
        ok, val = ps._with_timeout(lambda: (_ for _ in ()).throw(OSError(53, "网络")), 1)
        self.assertFalse(ok)
        self.assertIsInstance(val, OSError)

    def test_four_message_buckets(self):
        self.assertIn("超时", ps._io_reason("索引", "X", "timeout", 3))
        self.assertIn("不存在", ps._io_reason("索引", "X", FileNotFoundError(2, "no"), 20))
        self.assertIn("不可达", ps._io_reason("索引", "X", OSError(53, "网络路径"), 20))
        self.assertIn("不是合法 JSON",
                      ps._io_reason("索引", "X", json.JSONDecodeError("x", "y", 0), 20))
        self.assertIn("读取失败", ps._io_reason("索引", "X", ValueError("boom"), 20))


class ShareSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="unc 测 试_")
        self.index, self.pkg = _make_index(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_not_configured(self):
        cat, err = ps.ShareSource("").get_catalog()
        self.assertIsNone(cat)
        self.assertIn("未配置", err)

    def test_missing_index(self):
        cat, err = ps.ShareSource(os.path.join(self.tmp, "nope.json")).get_catalog()
        self.assertIsNone(cat)
        self.assertTrue("不存在" in err or "不可达" in err, err)

    def test_bad_json(self):
        bad = os.path.join(self.tmp, "bad.json")
        with io.open(bad, "w", encoding="utf-8") as f:
            f.write("{ not json")
        cat, err = ps.ShareSource(bad).get_catalog()
        self.assertIsNone(cat)
        self.assertIn("不是合法 JSON", err)

    def test_cjk_space_path_roundtrip(self):
        """含中文与空格目录/文件名：索引可读、条目路径正确、包可复制、sha256 透传。"""
        src = ps.ShareSource(self.index)
        cat, err = src.get_catalog()
        self.assertIsNone(err, err)
        entry = cat["packages"][0]
        self.assertTrue(os.path.isfile(entry["path"]), entry["path"])
        self.assertTrue(entry["path"].endswith("包 名 v1.0.zip"))
        dest = os.path.join(self.tmp, "落 地 目 录", "out.zip")
        p, err2 = src.download("local-unc", "1.0", dest)
        self.assertIsNone(err2, err2)
        self.assertTrue(os.path.isfile(p))
        self.assertEqual(pa.sha256_file(p), entry["sha256"])

    def test_download_missing_package(self):
        src = ps.ShareSource(self.index)
        p, err = src.download("local-nothere", None, os.path.join(self.tmp, "o.zip"))
        self.assertIsNone(p)
        self.assertIn("找不到该包文件", err)

    def test_index_path_is_normalized(self):
        long_idx = UNC + "?" + BS + self.index.lstrip(BS)
        self.assertEqual(ps.ShareSource(long_idx)._index_path, self.index)

    def test_timeout_reported_not_hung(self):
        """不可达/慢盘：限时返回「超时」文案，而不是无限等待。"""
        def slow(_p):
            time.sleep(5)
            return True
        with mock.patch.object(ps.os.path, "isfile", slow):
            t0 = time.time()
            cat, err = ps.ShareSource(self.index, io_timeout=0.2).get_catalog()
            elapsed = time.time() - t0
        self.assertIsNone(cat)
        self.assertIn("超时", err)
        self.assertLess(elapsed, 3.0, "必须限时返回")


class AdminReadIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="unc admin_")
        self.index, self.pkg = _make_index(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cjk_space_index_ok(self):
        res = pa.read_index(self.index)
        self.assertTrue(res["ok"], res.get("error"))
        item = res["packages"][0]
        self.assertTrue(item["exists"])
        self.assertTrue(item["hash_ok"])
        self.assertEqual(item["id"], "local-unc")

    def test_long_path_prefix_accepted(self):
        """``\\\\?\\C:\\...`` 长路径前缀形态：经归一化后照常可读。"""
        long_idx = UNC + "?" + BS + self.index.lstrip(BS)
        self.assertEqual(pa.normalize_share_path(long_idx), self.index)
        res = pa.read_index(long_idx)
        self.assertTrue(res["ok"], res.get("error"))

    def test_error_buckets(self):
        res = pa.read_index(os.path.join(self.tmp, "nope.json"))
        self.assertFalse(res["ok"])
        self.assertTrue("不存在" in res["error"] or "不可达" in res["error"], res["error"])
        bad = os.path.join(self.tmp, "bad.json")
        with io.open(bad, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertIn("不是合法 JSON", pa.read_index(bad)["error"])
        wrong = os.path.join(self.tmp, "schema.json")
        with io.open(wrong, "w", encoding="utf-8") as f:
            json.dump({"schema": 9, "packages": []}, f)
        self.assertIn("schema 不受支持", pa.read_index(wrong)["error"])

    def test_empty_path(self):
        self.assertIn("未配置", pa.read_index("")["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
