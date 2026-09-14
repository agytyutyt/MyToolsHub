"""jztools_data.sync_plugin_templates 的单元测试。

覆盖「插件经插件包单独升级后，新增配置键自动下发」这条链路（设计见
docs/插件独立升级方案-设计文档.md §8）：只补缺失键、保留用户已有值、幂等、
overwrite 模式、跳过 vendor/__pycache__ 等目录。

运行：
    python -m pytest test_plugin_templates.py -q
    python -m unittest test_plugin_templates -v      # 无需 pytest
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jztools_data as jd  # noqa: E402


class PluginTemplateSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jz-tpl-")
        self.base = os.path.join(self.tmp, "app")
        self.root = os.path.join(self.tmp, "data")
        self.pdir = os.path.join(self.base, "plugins", "demo")
        os.makedirs(self.pdir)
        os.makedirs(os.path.join(self.pdir, "backend"))
        os.makedirs(os.path.join(self.root, "config"))
        self.tmpl = os.path.join(self.pdir, "backend", "config.template.json")
        self.user = os.path.join(self.root, "plugins", "demo", "config.json")
        self.write_tmpl({"a": 1, "nested": {"x": 1, "y": 2}})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 辅助 ----
    def write_tmpl(self, obj):
        with open(self.tmpl, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def write_user(self, obj):
        os.makedirs(os.path.dirname(self.user), exist_ok=True)
        with open(self.user, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def read_user(self):
        with open(self.user, encoding="utf-8") as f:
            return json.load(f)

    # ---- 用例 ----
    def test_creates_config_and_records_fingerprint(self):
        """首次：数据根没有运行配置 → 用模板初始化，并登记模板指纹。"""
        n = jd.sync_plugin_templates(self.base, self.root)
        self.assertEqual(n, 1)
        self.assertEqual(self.read_user(), {"a": 1, "nested": {"x": 1, "y": 2}})
        state = jd._read_state_at(self.root)
        self.assertIn("backend/config.template.json",
                      state["plugins"]["demo"]["templates"])

    def test_idempotent_when_template_unchanged(self):
        """模板没变 → 第二次不再同步（不打扰用户已保存的配置）。"""
        jd.sync_plugin_templates(self.base, self.root)
        self.write_user({"a": 42, "custom": "keep"})
        self.assertEqual(jd.sync_plugin_templates(self.base, self.root), 0)
        got = self.read_user()
        self.assertEqual(got["a"], 42)
        self.assertEqual(got["custom"], "keep")

    def test_new_key_added_by_plugin_upgrade(self):
        """插件升级（模板新增键）→ 键补上、用户已有值保留。"""
        jd.sync_plugin_templates(self.base, self.root)
        self.write_user({"a": 42, "nested": {"x": 7}})
        self.write_tmpl({"a": 1, "nested": {"x": 1, "y": 2, "z": 3}, "new": True})
        self.assertEqual(jd.sync_plugin_templates(self.base, self.root), 1)
        got = self.read_user()
        self.assertEqual(got["a"], 42)             # 用户值保留
        self.assertEqual(got["nested"]["x"], 7)    # 用户值保留（嵌套）
        self.assertEqual(got["nested"]["z"], 3)    # 模板新增键补上
        self.assertTrue(got["new"])

    def test_overwrite_mode(self):
        """_mode: overwrite → 覆盖运行配置（旧文件先备份 .bak-old），_mode 不落盘。"""
        self.write_tmpl({"_mode": "overwrite", "p": 1})
        jd.sync_plugin_templates(self.base, self.root)
        self.assertNotIn("_mode", self.read_user())
        self.write_user({"p": 9})
        self.write_tmpl({"_mode": "overwrite", "p": 2})
        jd.sync_plugin_templates(self.base, self.root)
        self.assertEqual(self.read_user()["p"], 2)
        self.assertTrue(os.path.isfile(self.user + ".bak-old"))

    def test_skips_vendor_and_pycache_dirs(self):
        """vendor/__pycache__ 下的同名文件不算模板（避免把引擎内部文件当配置）。"""
        cache = os.path.join(self.pdir, "backend", "__pycache__")
        vend = os.path.join(self.pdir, "backend", "vendor")
        os.makedirs(cache)
        os.makedirs(vend)
        for d in (cache, vend):
            with open(os.path.join(d, "x.template.json"), "w", encoding="utf-8") as f:
                f.write("{}")
        jd.sync_plugin_templates(self.base, self.root)
        state = jd._read_state_at(self.root)
        self.assertEqual(list(state["plugins"]["demo"]["templates"]),
                         ["backend/config.template.json"])

    def test_missing_template_dir_is_noop(self):
        """没有模板的插件不产生状态登记（不做无谓写入）。"""
        os.remove(self.tmpl)
        self.assertEqual(jd.sync_plugin_templates(self.base, self.root), 0)
        state = jd._read_state_at(self.root)
        self.assertNotIn("demo", state.get("plugins") or {})

    def test_real_repo_templates_sync_into_temp_root(self):
        """用仓库真实模板（4 个）跑一遍：全部同步成功、目标落在数据根插件目录。"""
        repo = os.path.dirname(os.path.abspath(__file__))
        n = jd.sync_plugin_templates(repo, self.root)
        self.assertGreaterEqual(n, 4)
        for pid in ("case-report", "character-graph", "file-search", "trajectory-sketch", "file-filter"):
            p = os.path.join(self.root, "plugins", pid, "config.json")
            if os.path.isdir(os.path.join(repo, "plugins", pid)):
                self.assertTrue(os.path.isfile(p), p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
