"""plugin_admin（管理后台插件管理，阶段二/三）单元测试。

覆盖：包校验（结构/哈希/路径安全/版本规则/min_app）→ 应用（三分法 + 数据零触碰 + 登记）
→ 回滚 → 盘点 → 索引检查更新 → 批量升级。

运行：
    python -m pytest test_plugin_admin.py -q
    python -m unittest test_plugin_admin -v      # 无需 pytest
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "plugins", "admin", "backend"))

import plugin_admin as pa  # noqa: E402


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


class Base(unittest.TestCase):
    """沙箱：<tmp>/app（程序目录，含一个 demo 插件） + <tmp>/data（数据根）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jz-padmin-")
        self.base = os.path.join(self.tmp, "app")
        self.root = os.path.join(self.tmp, "data")
        self.pdir = os.path.join(self.base, "plugins", "demo")
        os.makedirs(os.path.join(self.pdir, "backend"))
        os.makedirs(os.path.join(self.pdir, "frontend"))
        os.makedirs(os.path.join(self.root, "config"))
        os.makedirs(os.path.join(self.root, "plugins", "demo", "data"))
        self.write_json(os.path.join(self.base, "version.json"), {"app": "1.9.0", "schema": 1})
        self.write_json(os.path.join(self.pdir, "manifest.json"),
                        {"id": "demo", "version": "1.0.0", "entry": "index.html"})
        self.write_text(os.path.join(self.pdir, "frontend", "index.html"), "<h1>v1</h1>")
        self.write_text(os.path.join(self.pdir, "backend", "routes.py"), "def register(app):\n    pass\n")
        # 用户数据（升级全程必须逐字节不变）
        self.write_text(os.path.join(self.root, "plugins", "demo", "data", "user.txt"), "用户数据")
        # 注册表（含 demo，enabled=false）
        self.write_json(os.path.join(self.root, "config", "tools.json"),
                        {"tools": [{"id": "demo", "name": "演示", "enabled": False, "order": 5}]})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 辅助 ----
    def write_json(self, path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def write_text(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read_json(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def read_text(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def user_data_hash(self):
        return pa.dir_size(os.path.join(self.root, "plugins", "demo")) and sha256(
            os.path.join(self.root, "plugins", "demo", "data", "user.txt"))

    def make_package(self, version="1.1.0", *, pid="demo", extra_meta=None, tamper=False,
                     unsafe_entry=None, frontend="<h1>v2</h1>", backend_extra="# v2\n",
                     omit_sums=False, tools_entry=True):
        """按与 tools/build-plugin-package.ps1 相同的布局造一个插件包 zip。"""
        work = os.path.join(self.tmp, "build-%s-%s" % (pid, version))
        shutil.rmtree(work, ignore_errors=True)
        payload = os.path.join(work, "payload", "plugins", pid)
        self.write_json(os.path.join(payload, "manifest.json"),
                        {"id": pid, "version": version, "entry": "index.html"})
        self.write_text(os.path.join(payload, "frontend", "index.html"), frontend)
        self.write_text(os.path.join(payload, "backend", "routes.py"),
                        "def register(app):\n    pass\n" + backend_extra)
        meta = {"schema": 1, "kind": "plugin-upgrade", "id": pid, "version": version,
                "requires_restart": True, "min_app_version": "1.9.0", "code_sha256": "x" * 64}
        if tools_entry:
            meta["tools_entry"] = {"id": pid, "name": "演示", "enabled": False}
        meta.update(extra_meta or {})
        self.write_json(os.path.join(work, "plugin-package.json"), meta)

        pairs = []
        for root, _dirs, files in os.walk(payload):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, os.path.join(work, "payload")).replace(os.sep, "/")
                pairs.append((rel, sha256(full)))
        pairs.sort()
        if not omit_sums:
            lines = "".join("%s  %s\n" % (h, r) for r, h in pairs)
            if tamper:
                # 让清单里某个文件的哈希与实际内容不符
                first_rel = pairs[0][0]
                lines = lines.replace(pairs[0][1], "0" * 64, 1)
                _ = first_rel
            self.write_text(os.path.join(work, "SHA256SUMS"), lines)
        zpath = os.path.join(self.tmp, "pkg-%s-%s.zip" % (pid, version))
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(work):
                for name in files:
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, work).replace(os.sep, "/")
                    zf.write(full, rel)
        if unsafe_entry:
            with zipfile.ZipFile(zpath, "a") as zf:
                zf.writestr(unsafe_entry, "x")
        return zpath

    # ---- 包校验 ----
    def test_inspect_ok_and_plan(self):
        pkg = self.make_package("1.1.0")
        ins = pa.inspect_package(pkg, self.base, self.root)
        self.assertTrue(ins["ok"], ins["errors"])
        self.assertEqual(ins["from_version"], "1.0.0")
        self.assertEqual(ins["to_version"], "1.1.0")
        self.assertTrue(ins["restart_needed"])          # backend 有变化
        self.assertEqual(len(ins["plan"]["changed"]), 3)  # index.html + manifest.json + routes.py 都变了
        self.assertEqual(ins["plan"]["unknown"], [])

    def test_inspect_rejects_tampered_hash(self):
        pkg = self.make_package("1.1.0", tamper=True)
        ins = pa.inspect_package(pkg, self.base, self.root)
        self.assertFalse(ins["ok"])
        self.assertTrue(any("哈希校验未通过" in e for e in ins["errors"]), ins["errors"])

    def test_inspect_rejects_unsafe_zip_path(self):
        pkg = self.make_package("1.1.0", unsafe_entry="../../evil.txt")
        ins = pa.inspect_package(pkg, self.base, self.root)
        self.assertFalse(ins["ok"])
        self.assertTrue(any("不安全路径" in e for e in ins["errors"]), ins["errors"])

    def test_inspect_rejects_same_version(self):
        pkg = self.make_package("1.0.0")
        ins = pa.inspect_package(pkg, self.base, self.root)
        self.assertFalse(ins["ok"])
        self.assertTrue(any("同版本重装" in e for e in ins["errors"]), ins["errors"])
        # 勾选「强制」后可放行
        ins2 = pa.inspect_package(pkg, self.base, self.root, force=True)
        self.assertTrue(ins2["ok"], ins2["errors"])

    def test_inspect_rejects_min_app_and_version_window(self):
        pkg = self.make_package("1.1.0", extra_meta={"min_app_version": "99.0.0"})
        ins = pa.inspect_package(pkg, self.base, self.root)
        self.assertFalse(ins["ok"])
        self.assertTrue(any("主程序版本过低" in e for e in ins["errors"]), ins["errors"])
        pkg2 = self.make_package("1.2.0", extra_meta={"upgrade_from_min": "1.1.0"})
        ins2 = pa.inspect_package(pkg2, self.base, self.root)
        self.assertFalse(ins2["ok"])
        self.assertTrue(any("不允许跳版升级" in e for e in ins2["errors"]), ins2["errors"])

    def test_inspect_rejects_missing_tools_entry_on_fresh_install(self):
        shutil.rmtree(self.pdir)                       # 变成"全新安装"
        pkg = self.make_package("1.1.0", tools_entry=False)
        ins = pa.inspect_package(pkg, self.base, self.root)
        self.assertFalse(ins["ok"])
        self.assertTrue(any("tools_entry" in e for e in ins["errors"]), ins["errors"])

    # ---- 应用 ----
    def test_apply_updates_code_and_keeps_data(self):
        before = self.user_data_hash()
        pkg = self.make_package("1.1.0")
        ins = pa.inspect_package(pkg, self.base, self.root)
        rep = pa.apply_package(self.base, self.root, ins)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(pa.installed_version(self.base, "demo"), "1.1.0")
        self.assertIn("v2", self.read_text(os.path.join(self.pdir, "frontend", "index.html")))
        self.assertEqual(self.user_data_hash(), before)          # ★ 用户数据未变
        self.assertTrue(os.path.isfile(rep["backup"]))            # 有备份
        st = pa.read_state(self.root)["plugins"]["demo"]
        self.assertEqual(st["version"], "1.1.0")
        self.assertTrue(st["installed_files"])
        self.assertFalse(os.path.isdir(ins["staging"]))           # 暂存已清理

    def test_apply_keeps_unknown_files_and_can_purge(self):
        self.write_text(os.path.join(self.pdir, "user-notes.txt"), "人工放入")
        ins = pa.inspect_package(self.make_package("1.1.0"), self.base, self.root)
        self.assertIn("plugins/demo/user-notes.txt", ins["plan"]["unknown"])
        rep = pa.apply_package(self.base, self.root, ins)
        self.assertTrue(rep["ok"])
        self.assertTrue(os.path.isfile(os.path.join(self.pdir, "user-notes.txt")))
        self.assertEqual(rep["kept_unknown"], 1)
        # 再升一版并显式清理
        ins2 = pa.inspect_package(self.make_package("1.2.0"), self.base, self.root)
        rep2 = pa.apply_package(self.base, self.root, ins2, purge_unknown=True)
        self.assertTrue(rep2["ok"])
        self.assertFalse(os.path.isfile(os.path.join(self.pdir, "user-notes.txt")))

    def test_apply_deletes_files_removed_in_new_version(self):
        # 1.1.0 装上（登记 installed_files），再出 1.2.0 且删掉 frontend/old.js
        ins = pa.inspect_package(self.make_package("1.1.0"), self.base, self.root)
        pa.apply_package(self.base, self.root, ins)
        self.write_text(os.path.join(self.pdir, "frontend", "old.js"), "old")
        state = pa.read_state(self.root)
        st = state["plugins"]["demo"]
        st["installed_files"] = sorted(set(st["installed_files"]) | {"plugins/demo/frontend/old.js"})
        pa.write_state(self.root, state)
        ins2 = pa.inspect_package(self.make_package("1.2.0"), self.base, self.root)
        self.assertIn("plugins/demo/frontend/old.js", ins2["plan"]["deleted"])
        pa.apply_package(self.base, self.root, ins2)
        self.assertFalse(os.path.isfile(os.path.join(self.pdir, "frontend", "old.js")))

    def test_apply_fresh_install_adds_registry_entry(self):
        shutil.rmtree(self.pdir)
        pa.set_plugin_enabled(self.root, "demo", False)          # demo 仍在表里
        cfg_path = os.path.join(self.root, "config", "tools.json")
        cfg = self.read_json(cfg_path)
        cfg["tools"] = [t for t in cfg["tools"] if t["id"] != "demo"]
        self.write_json(cfg_path, cfg)
        ins = pa.inspect_package(self.make_package("1.1.0"), self.base, self.root)
        rep = pa.apply_package(self.base, self.root, ins)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["tools_entry"], "added")
        cfg = self.read_json(cfg_path)
        self.assertTrue(any(t["id"] == "demo" for t in cfg["tools"]))

    def test_apply_marks_restart_pending(self):
        ins = pa.inspect_package(self.make_package("1.1.0"), self.base, self.root)
        rep = pa.apply_package(self.base, self.root, ins)
        self.assertTrue(rep["restart_needed"])
        st = pa.read_state(self.root)["plugins"]["demo"]
        self.assertTrue(st.get("restart_pending"), "后端有变化应标记待重启")

    # ---- 回滚 ----
    def test_rollback_restores_previous_version(self):
        ins = pa.inspect_package(self.make_package("1.1.0"), self.base, self.root)
        pa.apply_package(self.base, self.root, ins)
        rb = pa.rollback_plugin(self.base, self.root, "demo")
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(pa.installed_version(self.base, "demo"), "1.0.0")
        self.assertIn("v1", self.read_text(os.path.join(self.pdir, "frontend", "index.html")))
        self.assertTrue(os.path.isfile(rb["pre_backup"]))         # 回滚本身可再回退

    def test_rollback_without_backup_fails(self):
        rb = pa.rollback_plugin(self.base, self.root, "demo")
        self.assertFalse(rb["ok"])
        self.assertIn("没有任何备份", rb["error"])

    # ---- 盘点 / 启停 ----
    def test_list_and_toggle(self):
        rows = pa.list_plugins(self.base, self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "demo")
        # 展示名：tools.json 权威（规范 M-2）——沙箱里注册条目 name=演示，manifest 无 name
        self.assertEqual(rows[0]["name"], "演示")
        self.assertEqual(rows[0]["code_version"], "1.0.0")
        self.assertTrue(rows[0]["registered"])
        self.assertFalse(rows[0]["enabled"])
        self.assertTrue(pa.set_plugin_enabled(self.root, "demo", True))
        self.assertTrue(pa.list_plugins(self.base, self.root)[0]["enabled"])
        self.assertFalse(pa.set_plugin_enabled(self.root, "no-such", True))

    def test_display_name_priority(self):
        """展示名优先级：tools.json（权威）→ manifest.name → id（规范 M-2 / §5.1）。"""
        # 去掉 tools.json 里的 name → 回退 manifest.name
        cfg_path = os.path.join(self.root, "config", "tools.json")
        cfg = self.read_json(cfg_path)
        for item in cfg["tools"]:
            item.pop("name", None)
        self.write_json(cfg_path, cfg)
        self.write_json(os.path.join(self.pdir, "manifest.json"),
                        {"id": "demo", "name": "演示插件", "version": "1.0.0", "entry": "index.html"})
        self.assertEqual(pa.list_plugins(self.base, self.root)[0]["name"], "演示插件")
        # 两者都没有 → 回退 id
        self.write_json(os.path.join(self.pdir, "manifest.json"),
                        {"id": "demo", "version": "1.0.0", "entry": "index.html"})
        self.assertEqual(pa.list_plugins(self.base, self.root)[0]["name"], "demo")

    # ---- 阶段三：索引 / 检查更新 / 批量升级 ----
    def make_index(self, specs):
        """specs: [(version, requires_restart)] → 共享盘目录 + index.json"""
        share = os.path.join(self.tmp, "share")
        os.makedirs(share, exist_ok=True)
        pkgs = []
        for version, restart in specs:
            src = self.make_package(version, extra_meta={"requires_restart": restart})
            dst = os.path.join(share, "JZToolsHub-插件-demo-v%s.zip" % version)
            shutil.copyfile(src, dst)
            pkgs.append({"id": "demo", "version": version, "file": os.path.basename(dst),
                         "sha256": sha256(dst), "size": os.path.getsize(dst),
                         "requires_restart": restart, "min_app_version": "1.9.0",
                         "changelog": "v%s 更新" % version})
        index_path = os.path.join(share, "index.json")
        self.write_json(index_path, {"schema": 1, "updated_at": "2026-09-14T18:00:00+08:00", "packages": pkgs})
        return index_path, share

    def test_check_updates_reports_available_and_blocked(self):
        index_path, _share = self.make_index([("1.1.0", True)])
        res = pa.check_updates(self.base, self.root, index_path)
        self.assertTrue(res["ok"], res)
        row = res["rows"][0]
        self.assertTrue(row["upgrade_available"])
        self.assertEqual(row["current"], "1.0.0")
        self.assertEqual(row["available"], "1.1.0")
        # 升上去之后应报"已是最新"
        ins = pa.inspect_package(row["package_path"], self.base, self.root)
        pa.apply_package(self.base, self.root, ins)
        res2 = pa.check_updates(self.base, self.root, index_path)
        self.assertFalse(res2["rows"][0]["upgrade_available"])
        self.assertIn("已是最新", res2["rows"][0]["blocked_reason"])

    def test_check_updates_detects_hash_mismatch(self):
        index_path, share = self.make_index([("1.1.0", True)])
        # 篡改共享盘上的包
        with open(os.path.join(share, "JZToolsHub-插件-demo-v1.1.0.zip"), "ab") as f:
            f.write(b"tampered")
        res = pa.check_updates(self.base, self.root, index_path)
        row = res["rows"][0]
        self.assertFalse(row["upgrade_available"])
        self.assertIn("sha256", row["blocked_reason"])

    def test_batch_apply_multiple_packages(self):
        """两个插件的包在一次批量升级里顺序应用（一个升级、一个全新安装）。"""
        second = os.path.join(self.base, "plugins", "second")
        os.makedirs(os.path.join(second, "frontend"))
        self.write_json(os.path.join(second, "manifest.json"),
                        {"id": "second", "version": "1.0.0", "entry": "index.html"})
        self.write_text(os.path.join(second, "frontend", "index.html"), "s1")
        share = os.path.join(self.tmp, "share2")
        os.makedirs(share, exist_ok=True)
        pkgs = []
        for pid in ("demo", "second"):
            src = self.make_package("1.1.0", pid=pid)
            dst = os.path.join(share, "JZToolsHub-插件-%s-v1.1.0.zip" % pid)
            shutil.copyfile(src, dst)
            pkgs.append({"id": pid, "version": "1.1.0", "file": os.path.basename(dst),
                         "sha256": sha256(dst), "size": os.path.getsize(dst),
                         "requires_restart": True, "changelog": "x"})
        index_path = os.path.join(share, "index.json")
        self.write_json(index_path, {"schema": 1, "packages": pkgs})
        res = pa.batch_apply(self.base, self.root, index_path, ["demo", "second"])
        self.assertEqual(res["applied"], 2, res)
        self.assertTrue(res["restart_needed"])
        self.assertEqual(pa.installed_version(self.base, "demo"), "1.1.0")
        self.assertEqual(pa.installed_version(self.base, "second"), "1.1.0")

    # ---- 无基线（整包安装的插件首次走插件包升级）----
    def test_no_baseline_deletes_only_stale_backend_py(self):
        """无 installed_files 登记时：只清理 backend/*.py 残留，用户文件一律保留。"""
        self.write_text(os.path.join(self.pdir, "backend", "old_module.py"), "old")
        self.write_text(os.path.join(self.pdir, "frontend", "user-asset.json"), "{}")
        self.write_text(os.path.join(self.pdir, "user-notes.txt"), "人工放入")
        ins = pa.inspect_package(self.make_package("1.1.0"), self.base, self.root)
        self.assertTrue(ins["ok"], ins["errors"])
        self.assertIn("plugins/demo/backend/old_module.py", ins["plan"]["deleted"])
        self.assertIn("plugins/demo/frontend/user-asset.json", ins["plan"]["unknown"])
        self.assertIn("plugins/demo/user-notes.txt", ins["plan"]["unknown"])
        rep = pa.apply_package(self.base, self.root, ins)
        self.assertTrue(rep["ok"])
        self.assertFalse(os.path.isfile(os.path.join(self.pdir, "backend", "old_module.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.pdir, "user-notes.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.pdir, "frontend", "user-asset.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RealBuilderCrossCheck(unittest.TestCase):
    """交叉验证：出包工具（PowerShell）与插件管理模块（Python）对同一份包的理解一致。

    两处实现同一套规则（设计文档 §4/§5.3 单点定义），最容易出的问题是"格式约定漂移"——
    这里用真实构建器产出的包跑一遍 inspect/apply，把两侧钉在一起。
    没有 PowerShell（非 Windows）时自动跳过。
    """

    def setUp(self):
        import shutil as _shutil
        self.tmp = tempfile.mkdtemp(prefix="jz-xcheck-")
        self.repo = os.path.dirname(os.path.abspath(__file__))
        self.base = os.path.join(self.tmp, "app")
        self.root = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.base, "config"))
        os.makedirs(os.path.join(self.root, "config"))
        shutil.copytree(os.path.join(self.repo, "plugins", "base64"),
                        os.path.join(self.base, "plugins", "base64"))
        # 当作目标机上的旧版本 1.0.0（与仓库一致），并造一个"新版"夹具
        self.write_json(os.path.join(self.base, "version.json"), {"app": "1.9.0", "schema": 1})
        self.write_json(os.path.join(self.root, "config", "tools.json"), {"tools": []})
        self.fixture = os.path.join(self.tmp, "fixtures", "base64")
        shutil.copytree(os.path.join(self.repo, "plugins", "base64"), self.fixture)
        man = json.load(open(os.path.join(self.fixture, "manifest.json"), encoding="utf-8"))
        man["version"] = "1.0.1"
        self.write_json(os.path.join(self.fixture, "manifest.json"), man)
        with open(os.path.join(self.fixture, "frontend", "index.html"), "a", encoding="utf-8") as f:
            f.write("\n<!-- cross-check -->\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_json(self, path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def test_builder_package_is_understood_by_plugin_admin(self):
        import subprocess
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", "exit 0"], timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        except Exception:
            self.skipTest("未检测到 PowerShell，跳过交叉验证")
        out_dir = os.path.join(self.tmp, "packages")
        reg = os.path.join(self.tmp, "registry.json")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             os.path.join(self.repo, "tools", "build-plugin-package.ps1"),
             "-Id", "base64", "-From", self.fixture, "-OutDir", out_dir, "-RegistryFile", reg],
            capture_output=True, text=True, encoding="gbk", errors="replace", timeout=300)
        self.assertEqual(r.returncode, 0, "构建失败：%s\n%s" % (r.stdout[-1500:], r.stderr[-500:]))
        pkg = os.path.join(out_dir, "JZToolsHub-插件-base64-v1.0.1.zip")
        self.assertTrue(os.path.isfile(pkg), r.stdout[-800:])

        ins = pa.inspect_package(pkg, self.base, self.root)
        self.assertTrue(ins["ok"], ins["errors"])
        self.assertEqual(ins["to_version"], "1.0.1")
        self.assertEqual(ins["from_version"], "1.0.0")
        self.assertFalse(ins["restart_needed"])          # base64 是纯前端包
        rep = pa.apply_package(self.base, self.root, ins)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(pa.installed_version(self.base, "base64"), "1.0.1")
        # 再来一次真机安装器（PS）应能识别 Python 侧登记的 installed_files 基线（状态文件同格式）
        state = pa.read_state(self.root)
        self.assertTrue(state["plugins"]["base64"]["installed_files"])
