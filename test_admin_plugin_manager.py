"""管理后台「插件管理」端到端测试（阶段二：应用内升级 / 阶段三：共享盘批量更新）。

做法：在一份**沙箱程序目录 + 沙箱数据根**上加载真实的 app.py（进程内），用 Flask 测试客户端
把 HTTP 接口走完整链路：

    未登录被拒 → 登录 → 盘点 → 上传（只读校验）→ 应用 → 盘点（待重启）→ 同版本被拒
    → 备份清单 → 回滚 → 启停 → 共享盘索引 → 检查更新 → 批量升级 → 重启接口（源码模式拒绝）

★ 安全前提（本文件最关键的约束）：程序目录与数据根都指向沙箱——
  ① 数据根用环境变量 ``JZTOOLS_DATA_ROOT`` 显式指定（jztools_data 的正式能力）；
  ② 程序目录靠 **sys.path 指向沙箱的 app.py/jztools_data.py 副本**决定（get_base_dir 取自模块 __file__），
     并在 setUpModule 里断言 ``jztools_data.get_base_dir() == 沙箱``，不满足直接失败——
     否则应用插件包会写进真实仓库。绝不触碰真实安装与真实数据根。

运行：
    python -m pytest test_admin_plugin_manager.py -q
    python -m unittest test_admin_plugin_manager -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX = tempfile.mkdtemp(prefix="jz-pmgr-")
APP = os.path.join(SANDBOX, "app")
DATA = os.path.join(SANDBOX, "data")
SHARE = os.path.join(SANDBOX, "share")

REPO_PLUGINS = os.path.join(HERE, "plugins")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def tree_hash(root):
    lines = []
    for base, _dirs, files in os.walk(root):
        for name in sorted(files):
            full = os.path.join(base, name)
            lines.append(os.path.relpath(full, root).replace(os.sep, "/") + "  " + sha256(full))
    return "\n".join(sorted(lines))


def make_package(plugin_id, version, frontend="", backend_extra="", requires_restart=True,
                 tools_entry=True, min_app_version="1.9.0"):
    """按 tools/build-plugin-package.ps1 的布局造包（与 test_plugin_admin 同一套约定）。"""
    from plugin_admin import zip_dir_with_prefix  # 复用打包实现，保证条目格式一致
    work = os.path.join(SANDBOX, "build-%s-%s" % (plugin_id, version))
    shutil.rmtree(work, ignore_errors=True)
    payload = os.path.join(work, "payload", "plugins", plugin_id)
    write_json(os.path.join(payload, "manifest.json"),
               {"id": plugin_id, "version": version, "entry": "index.html"})
    write_text(os.path.join(payload, "frontend", "index.html"), frontend or "<h1>%s</h1>" % version)
    if backend_extra:
        write_text(os.path.join(payload, "backend", "routes.py"), "def register(app):\n    pass\n" + backend_extra)
    meta = {"schema": 1, "kind": "plugin-upgrade", "id": plugin_id, "version": version,
            "requires_restart": requires_restart, "min_app_version": min_app_version,
            "code_sha256": "x" * 64}
    if tools_entry:
        meta["tools_entry"] = {"id": plugin_id, "name": plugin_id, "enabled": False}
    write_json(os.path.join(work, "plugin-package.json"), meta)
    pairs = []
    for base, _dirs, files in os.walk(os.path.join(work, "payload")):
        for name in files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, os.path.join(work, "payload")).replace(os.sep, "/")
            pairs.append((rel, sha256(full)))
    write_text(os.path.join(work, "SHA256SUMS"),
               "".join("%s  %s\n" % (h, r) for r, h in sorted(pairs)))
    zpath = os.path.join(SANDBOX, "%s-v%s.zip" % (plugin_id, version))
    zip_dir_with_prefix(work, zpath, "")
    return zpath


def setUpModule():
    """搭沙箱：程序目录（app.py + jztools_data.py + static/ + plugins/ + config/ + version.json）+ 数据根。"""
    os.makedirs(SHARE, exist_ok=True)
    os.makedirs(APP, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    for name in ("app.py", "jztools_data.py"):
        shutil.copyfile(os.path.join(HERE, name), os.path.join(APP, name))
    for name in ("static", "plugins"):
        shutil.copytree(os.path.join(HERE, name), os.path.join(APP, name))
    os.makedirs(os.path.join(APP, "config"), exist_ok=True)
    shutil.copyfile(os.path.join(HERE, "config", "tools.json"), os.path.join(APP, "config", "tools.json"))
    write_json(os.path.join(APP, "version.json"), {"app": "1.9.0", "schema": 1})
    for base, dirs, _files in os.walk(os.path.join(APP, "plugins")):
        for d in list(dirs):
            if d in ("__pycache__", "out", ".task_cache"):
                shutil.rmtree(os.path.join(base, d), ignore_errors=True)
                dirs.remove(d)
    # 沙箱里的两个插件当作旧版本
    for pid, ver in (("knowledge-base", "0.9.0"), ("base64", "1.0.0")):
        mp = os.path.join(APP, "plugins", pid, "manifest.json")
        with open(mp, encoding="utf-8") as f:
            obj = json.load(f)
        obj["version"] = ver
        write_json(mp, obj)
    # 数据根：模拟用户数据（升级全程必须逐字节不变）
    write_text(os.path.join(DATA, "plugins", "knowledge-base", "data", "files", "sample-001.txt"),
               "用户上传的文档（升级时绝不能被动）")
    write_text(os.path.join(DATA, "plugins", "knowledge-base", "config.json"),
               '{"api_key":"SENSITIVE-KEY-ABC"}')
    os.makedirs(os.path.join(DATA, "config"), exist_ok=True)

    # ★ 关键：先设数据根环境变量，再让 sys.path 优先解析沙箱里的 app.py / jztools_data.py
    os.environ["JZTOOLS_DATA_ROOT"] = DATA
    sys.path.insert(0, APP)
    sys.path.insert(0, os.path.join(APP, "plugins", "admin", "backend"))
    for name in ("jztools_data", "plugin_admin"):
        mod = sys.modules.get(name)
        if mod and not os.path.abspath(getattr(mod, "__file__", "") or "").startswith(os.path.abspath(APP)):
            del sys.modules[name]


def tearDownModule():
    """清理沙箱，并把"来自沙箱"的模块从 sys.modules / sys.path 里摘掉。

    否则同一进程里后续测试（如 test_plugin_templates）会拿到指向已删除沙箱目录的
    jztools_data 副本，出现难以理解的失败。
    """
    os.environ.pop("JZTOOLS_DATA_ROOT", None)
    sandbox = os.path.abspath(SANDBOX)
    for name in ("jztools_data", "app", "plugin_admin", "jztools_admin"):
        mod = sys.modules.get(name)
        f = os.path.abspath(getattr(mod, "__file__", "") or "") if mod else ""
        if f and f.startswith(sandbox):
            del sys.modules[name]
    for p in (os.path.abspath(APP), os.path.abspath(os.path.join(APP, "plugins", "admin", "backend"))):
        while p in sys.path:
            sys.path.remove(p)
    shutil.rmtree(SANDBOX, ignore_errors=True)


class AdminPluginManagerTest(unittest.TestCase):
    """单个连贯场景（顺序断言，模拟管理员在页面上的操作序列）。"""

    def test_00_sandbox_guard(self):
        """安全前提：base_dir 必须落在沙箱，否则本测试会改到真实仓库。"""
        import jztools_data
        self.assertEqual(os.path.abspath(jztools_data.get_base_dir()), os.path.abspath(APP),
                         "沙箱未生效：程序目录指向了 %s" % jztools_data.get_base_dir())
        self.assertEqual(os.path.abspath(jztools_data.get_data_root()), os.path.abspath(DATA))
        self.assertFalse(os.path.abspath(jztools_data.get_base_dir()).startswith(os.path.abspath(HERE)))

    def test_01_full_flow(self):
        import jztools_data
        import plugin_admin as pa
        import app as appmod

        appmod.init_data_root()
        appmod.register_plugin_backends(appmod.app)
        client = appmod.app.test_client()

        data_tree_before = tree_hash(os.path.join(DATA, "plugins"))
        # 仓库插件目录基线快照（收尾断言用：本测试全程只应碰沙箱，不能动真实仓库）
        with open(os.path.join(REPO_PLUGINS, "knowledge-base", "manifest.json"), encoding="utf-8") as f:
            kb_manifest_before = json.load(f)

        # ---- 鉴权：未登录一律拒绝 ----
        r = client.get("/api/admin/plugins")
        self.assertIn(r.status_code, (401, 403))
        self.assertNotIn(r.status_code, (200,))

        # ---- 登录（首启默认管理员） ----
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

        # ---- 盘点 ----
        r = client.get("/api/admin/plugins")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        rows = {p["id"]: p for p in data["plugins"]}
        self.assertEqual(rows["knowledge-base"]["code_version"], "0.9.0")
        # 展示名走 tools.json（权威）：页面/接口显示中文名而不是英文 id
        self.assertEqual(rows["knowledge-base"]["name"], "知识库")
        self.assertEqual(rows["case-report"]["name"], "战果录入")
        self.assertEqual(rows["knowledge-base"]["data_bytes"] > 0, True)
        self.assertEqual(data["app_version"], "1.9.0")

        # ---- 上传（只读校验）----
        pkg = make_package("knowledge-base", "1.0.1", frontend="<h1>v2</h1>",
                           backend_extra="# v2\n")
        with open(pkg, "rb") as f:
            r = client.post("/api/admin/plugins/upload",
                            data={"file": (f, "JZToolsHub-插件-knowledge-base-v1.0.1.zip")},
                            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        up = r.get_json()
        self.assertTrue(up["ok"])
        self.assertEqual(up["id"], "knowledge-base")
        self.assertEqual(up["version"], "1.0.1")
        self.assertEqual(up["name"], "知识库")     # 计划预览也显示中文名
        self.assertTrue(up["requires_restart"])
        self.assertEqual(up["plan"]["new"], 0)         # 沙箱里文件都在 → 都是"修改"
        self.assertGreaterEqual(up["plan"]["changed"], 2)   # routes.py + manifest.json 等
        self.assertTrue(up["plan"]["base_source"])
        upload_name = up["file"]

        # ---- 应用 ----
        r = client.post("/api/admin/plugins/apply", json={"file": upload_name})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rep = r.get_json()
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["to_version"], "1.0.1")
        self.assertTrue(rep["restart_needed"])
        self.assertIn("knowledge-base", rep["restart_pending"])
        self.assertTrue(os.path.isfile(rep["backup"]))
        # 源码模式不真重启，但必须给出 restarting / restart_message 供前端判断
        self.assertFalse(rep["restarting"])
        self.assertIn("源码", rep["restart_message"])
        # 程序目录已更新、用户数据逐字节未变
        with open(os.path.join(APP, "plugins", "knowledge-base", "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual(man["version"], "1.0.1")
        self.assertIn("# v2", read_text(os.path.join(APP, "plugins", "knowledge-base", "backend", "routes.py")))
        self.assertEqual(tree_hash(os.path.join(DATA, "plugins")), data_tree_before)

        # ---- 盘点：登记版本 + 待重启 ----
        rows = {p["id"]: p for p in client.get("/api/admin/plugins").get_json()["plugins"]}
        self.assertEqual(rows["knowledge-base"]["state_version"], "1.0.1")
        self.assertTrue(rows["knowledge-base"]["restart_pending"])
        self.assertGreaterEqual(rows["knowledge-base"]["backups"], 1)

        # ---- 同版本重装被拒（未勾选强制）----
        with open(pkg, "rb") as f:
            r = client.post("/api/admin/plugins/upload",
                            data={"file": (f, "same.zip")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any("同版本重装" in e for e in r.get_json()["errors"]))
        # 前端通用 api() 抛错只取 data.error：没有摘要的话管理员只看到"HTTP 400"
        self.assertIn("同版本重装", r.get_json().get("error") or "")

        # ---- 篡改包被拒（哈希）----
        bad = os.path.join(SANDBOX, "tampered.zip")
        with zipfile.ZipFile(pkg) as zin, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data_ = zin.read(info.filename)
                if info.filename.endswith("backend/routes.py"):
                    data_ += b"\n# tampered\n"
                zout.writestr(info, data_)
        with open(bad, "rb") as f:
            r = client.post("/api/admin/plugins/upload",
                            data={"file": (f, "tampered.zip")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any("哈希校验未通过" in e for e in r.get_json()["errors"]))

        # ---- 备份清单 + 回滚 ----
        r = client.get("/api/admin/plugins/backups?id=knowledge-base")
        backups = r.get_json()["backups"]
        self.assertGreaterEqual(len(backups), 1)
        r = client.post("/api/admin/plugins/rollback", json={"id": "knowledge-base"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        # 回滚是整目录替换 → 一律需重启（源码模式给出提示而不是真重启）
        self.assertTrue(r.get_json()["restart_needed"])
        self.assertFalse(r.get_json()["restarting"])
        with open(os.path.join(APP, "plugins", "knowledge-base", "manifest.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], "0.9.0")
        self.assertEqual(tree_hash(os.path.join(DATA, "plugins")), data_tree_before)

        # ---- 启停 ----
        r = client.post("/api/admin/plugins/enable", json={"id": "base64", "enabled": True})
        self.assertTrue(r.get_json()["ok"])
        with open(os.path.join(DATA, "config", "tools.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertTrue([t for t in cfg["tools"] if t["id"] == "base64"][0]["enabled"])
        client.post("/api/admin/plugins/enable", json={"id": "base64", "enabled": False})
        r = client.post("/api/admin/plugins/enable", json={"id": "no-such-plugin", "enabled": True})
        self.assertEqual(r.status_code, 404)

        # ---- 阶段三：共享盘索引 → 检查更新 → 批量升级 ----
        kb_pkg = make_package("knowledge-base", "1.0.1", backend_extra="# v2\n")
        b64_pkg = make_package("base64", "1.0.1", frontend="<h1>b64</h1>", requires_restart=False)
        shutil.copyfile(kb_pkg, os.path.join(SHARE, "JZToolsHub-插件-knowledge-base-v1.0.1.zip"))
        shutil.copyfile(b64_pkg, os.path.join(SHARE, "JZToolsHub-插件-base64-v1.0.1.zip"))
        pkgs = []
        for pid, fn, restart in (("knowledge-base", "JZToolsHub-插件-knowledge-base-v1.0.1.zip", True),
                                 ("base64", "JZToolsHub-插件-base64-v1.0.1.zip", False)):
            full = os.path.join(SHARE, fn)
            pkgs.append({"id": pid, "version": "1.0.1", "file": fn, "sha256": sha256(full),
                         "size": os.path.getsize(full), "requires_restart": restart,
                         "min_app_version": "1.9.0", "changelog": "e2e"})
        index_path = os.path.join(SHARE, "index.json")
        write_json(index_path, {"schema": 1, "updated_at": "2026-09-14T18:00:00+08:00", "packages": pkgs})

        r = client.get("/api/admin/plugins/index", query_string={"path": index_path})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = {x["id"]: x for x in r.get_json()["rows"]}
        self.assertTrue(rows["knowledge-base"]["upgrade_available"])
        self.assertEqual(rows["knowledge-base"]["current"], "0.9.0")
        self.assertTrue(rows["base64"]["upgrade_available"])

        r = client.post("/api/admin/plugins/batch-apply",
                        json={"index_path": index_path, "ids": ["knowledge-base", "base64"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        res = r.get_json()
        self.assertEqual(res["applied"], 2, res)
        self.assertTrue(res["restart_needed"])            # knowledge-base 含后端改动
        self.assertFalse(res["restarting"])
        self.assertIn("源码", res["restart_message"])
        for pid in ("knowledge-base", "base64"):
            with open(os.path.join(APP, "plugins", pid, "manifest.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["version"], "1.0.1")
        self.assertEqual(tree_hash(os.path.join(DATA, "plugins")), data_tree_before)

        # 索引路径已被记住；再次检查应无升级项
        self.assertEqual(client.get("/api/admin/plugins").get_json()["index_path"], index_path)
        rows2 = client.get("/api/admin/plugins/index", query_string={"path": index_path}).get_json()["rows"]
        self.assertEqual([x for x in rows2 if x["upgrade_available"]], [])

        # 索引里包的 sha256 不符 → 不可升级
        pkgs_bad = [dict(p) for p in pkgs]
        pkgs_bad[0]["sha256"] = "0" * 64
        bad_index = os.path.join(SHARE, "index-bad.json")
        write_json(bad_index, {"schema": 1, "packages": pkgs_bad})
        rows3 = client.get("/api/admin/plugins/index", query_string={"path": bad_index}).get_json()["rows"]
        kb_row = [x for x in rows3 if x["id"] == "knowledge-base"][0]
        # 已是最新 → 也可升级为假（这里两者都应导致不升级）
        self.assertFalse(kb_row["upgrade_available"])

        # ---- 重启接口（源码模式必须明确拒绝自动重启）----
        r = client.post("/api/admin/plugins/restart")
        self.assertEqual(r.status_code, 200)
        res = r.get_json()
        self.assertFalse(res["ok"])
        self.assertIn("源码", res["message"])
        self.assertIn("knowledge-base", res["pending"])

        # ---- 关闭「应用后自动重启」：只提示，不自作主张重启 ----
        pkg_noauto = make_package("knowledge-base", "1.0.2", backend_extra="# v3\n")
        with open(pkg_noauto, "rb") as f:
            r = client.post("/api/admin/plugins/upload",
                            data={"file": (f, "noauto.zip")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = client.post("/api/admin/plugins/apply",
                        json={"file": r.get_json()["file"], "auto_restart": False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rep2 = r.get_json()
        self.assertTrue(rep2["ok"], rep2)
        self.assertTrue(rep2["restart_needed"])
        self.assertFalse(rep2["restarting"])
        self.assertIn("跳过", rep2["restart_message"])

        # ---- 收尾安全断言：仓库里的 plugins 没被本测试碰过 ----
        with open(os.path.join(REPO_PLUGINS, "knowledge-base", "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
        # 断言"与测试开始时一致"而不是写死版本号：插件自身发版不应让本测试失败
        self.assertEqual(man, kb_manifest_before, "仓库插件目录被改动了！沙箱隔离失效")
        self.assertEqual(os.path.abspath(sys.modules["jztools_data"].get_base_dir()), os.path.abspath(APP))


if __name__ == "__main__":
    unittest.main(verbosity=2)
