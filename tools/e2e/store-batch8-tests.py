# -*- coding: utf-8 -*-
r"""批次 8（插件商店 S1）台架：契约 / 状态机 / 安装闭环 / 能力层端到端。

覆盖（对应设计文档 §6 验收标准的可自动化子集）：
  A  store 插件读面：flat 投影（最新 published / pending 隐藏）/ ?all=1 / 下载流 +
     stats 事件（via 校验）/ 未登录 401
  B  admin store_admin 写面：三形态机器校验 + 上架落盘（规范名）/ 版本占号 /
     审核 approve·reject·republish / 下架回退（投影回落）/ E1 index-server.json /
     部署（inspect/apply 链，真实落 base_dir）/ 统计聚合
  C  客户端 pkg_install：本地包六连校验（含 mount 冲突/路径穿越）/ 前端包清单核对 /
     组件包 .pending 语义（纯 Python 立即生效、DLL 类留暂存）
  D  能力层端到端（stub 上游 + test_client）：安装→合并清单出现 / 停用→网格隐藏+API 404 /
     preflight 缺依赖映射依赖包 + 联合安装 / server 前端包本地直出↔停用回退中继 /
     sha256 拒装 / plugin-source 切源 / check-update 经 Source 抽象

用法：
    python tools\e2e\store-batch8-tests.py            # 全部
退出码：0 = 全过；1 = 有 FAIL（SKIP 不计失败）。
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "client"))

import jztools_data  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %s%s" % ("[OK]" if ok else "[!!]", name, ("  — " + detail) if detail else ""))


def make_zip(entries):
    """entries: {arcname: bytes|str} → BytesIO。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            zf.writestr(name, data)
    buf.seek(0)
    return buf


# ============================================================
# A/B 公共：临时数据根 + store 读面 app
# ============================================================

def build_catalog_fixture(packages_dir):
    """catalog.json 夹具：local-foo（1.0.1/1.1.0 published）、svc-bar（1.9.0 published，
    2.1.0 pending——不可见）、dep-mod（1.0.0 published）。包文件真落在 packages_dir。"""
    files = {}
    versions = {
        "local-foo": {"1.0.1": "foo-101", "1.1.0": "foo-110"},
        "svc-bar": {"1.9.0": "bar-190", "2.1.0": "bar-210-pending"},
        "dep-mod": {"1.0.0": "dep-100"},
    }
    entries = []
    for pid, vers in versions.items():
        runtime = "local" if pid.startswith("local-") else "server" if pid == "svc-bar" else "dep-slot"
        vlist = []
        for ver, tag in vers.items():
            fname = "pkg-%s-%s.zip" % (pid, ver)
            body = ("package %s" % tag).encode("utf-8")
            with open(os.path.join(packages_dir, fname), "wb") as f:
                f.write(body)
            files[(pid, ver)] = (fname, body)
            published = "pending" if tag.endswith("pending") else "published"
            vlist.append({"version": ver, "file": fname,
                          "sha256": __import__("hashlib").sha256(body).hexdigest(),
                          "size": len(body), "status": published,
                          "published_at": "2026-10-05T10:00:00" if published == "published" else "",
                          "requires_restart": False, "min_app_version": "", "api_version": 1,
                          "changelog": "%s %s" % (pid, ver),
                          "submit": {"by": "tester", "at": ""}, "review": {}, "history": []})
        entry = {"id": pid, "name": pid, "description": "测试条目", "icon": "🧩",
                 "runtime": runtime, "deps_mode": "shared", "requires": {},
                 "versions": vlist}
        if runtime == "dep-slot":
            entry["provides"] = ["no_such_dep_xyz"]
        if runtime == "server":
            entry["versions"][0]["frontend_file"] = "fe-svc-bar.zip"
        entries.append(entry)
    catalog = {"schema": 2, "generated_at": "2026-10-05T10:00:00", "packages": entries}
    return catalog, files


def load_store_routes(data_root):
    """在指定数据根下加载 store 读面路由模块（模块导入期即定位 store 目录）。"""
    os.environ["JZTOOLS_DATA_ROOT"] = data_root
    for mod in [m for m in list(sys.modules) if m in ("jztools_data",)]:
        del sys.modules[mod]
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "store_routes_u%d" % threading.get_ident(),
        os.path.join(ROOT, "plugins", "store", "backend", "routes.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def part_a_and_store_readface(work):
    print("==> A. store 插件读面（catalog 投影 / 下载 / 统计 / 401）")
    from flask import Flask
    data_root = os.path.join(work, "A_dataroot")
    os.makedirs(os.path.join(data_root, "store", "packages"), exist_ok=True)
    catalog, files = build_catalog_fixture(os.path.join(data_root, "store", "packages"))
    with open(os.path.join(data_root, "store", "catalog.json"), "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False)

    store_mod = load_store_routes(data_root)
    app = Flask(__name__)
    store_mod.register(app)
    store_mod._get_session_user = lambda: {"username": "tester"}   # 会话夹具
    c = app.test_client()

    r = c.get("/plugin/store/api/catalog")
    d = r.get_json()
    ids = {p["id"]: p for p in d["packages"]}
    check("A1 flat 投影：本地条目取最新 published", ids["local-foo"]["version"] == "1.1.0")
    check("A1 pending 版本不可见", ids["svc-bar"]["version"] == "1.9.0")
    check("A1 投影含 runtime/requires 扩展",
          ids["local-foo"]["runtime"] == "local" and "requires" in ids["local-foo"])
    r = c.get("/plugin/store/api/catalog?all=1")
    vers = {p["id"]: p.get("versions") for p in r.get_json()["packages"]}
    check("A1 ?all=1 含版本历史（含 pending 标记）",
          any(v.get("status") == "pending" for v in vers["svc-bar"]))

    fname, body = files[("local-foo", "1.1.0")]
    r = c.get("/plugin/store/api/download/local-foo/1.1.0?via=client")
    check("A2 下载 published 版本 200 且字节一致", r.status_code == 200 and r.data == body)
    r = c.get("/plugin/store/api/download/svc-bar/2.1.0")
    check("A2 pending 版本拒下载 404", r.status_code == 404)
    r = c.get("/plugin/store/api/download/local-foo/9.9.9")
    check("A2 不存在版本 404", r.status_code == 404)
    stats_file = os.path.join(data_root, "store", "stats.jsonl")
    rows = [json.loads(x) for x in open(stats_file, encoding="utf-8") if x.strip()]
    check("A2 下载落统计（via=client、user=tester）",
          len(rows) == 1 and rows[0]["via"] == "client" and rows[0]["user"] == "tester"
          and rows[0]["id"] == "local-foo" and rows[0]["version"] == "1.1.0")

    store_mod._get_session_user = lambda: None
    r = c.get("/plugin/store/api/catalog")
    check("A3 未登录 401", r.status_code == 401)


# ============================================================
# B. admin store_admin（写面状态机）
# ============================================================

def part_b(work):
    print("==> B. admin store_admin（三形态校验 / 占号 / 审核 / 下架回退 / E1 / 部署 / 统计）")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "store_admin_u",
        os.path.join(ROOT, "plugins", "admin", "backend", "store_admin.py"))
    sa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sa)
    root = os.path.join(work, "B_dataroot")
    os.makedirs(root, exist_ok=True)

    # ---- 本地包夹具 ----
    local_zip = os.path.join(work, "local-foo-v1.0.0.zip")
    buf = make_zip({
        "manifest.json": json.dumps({"id": "local-foo", "runtime": "local", "kind": "local",
                                     "version": "1.0.0", "name": "台账报表", "icon": "🧾",
                                     "entry": {"frontend": "index.html"},
                                     "requires": {"python_packages": [
                                         {"name": "flask", "version": ">=3.0"}]}}),
        "frontend/index.html": "<h1>foo</h1>",
    })
    with open(local_zip, "wb") as f:
        f.write(buf.getvalue())

    form = {"name": "台账报表（本地）", "description": "本地报表", "changelog": "首个版本",
            "icon": "🧾", "deps_mode": "shared"}
    r = sa.submit(root, "local", local_zip, form, "alice")
    check("B1 本地包上架：机器校验过 → pending", r.get("ok"))
    check("B1 规范文件名落 packages\\", r.get("file") == "JZClient-本地插件-local-foo-v1.0.0.zip"
          and os.path.isfile(os.path.join(sa.packages_dir(root), r["file"])))
    catalog = sa.load_catalog(root)
    entry = sa.find_entry(catalog, "local-foo")
    rec = sa.find_version_record(entry, "1.0.0")
    check("B1 依赖版本声明自动提取 + submit 台账",
          entry["requires"]["python_packages"][0]["name"] == "flask"
          and rec["status"] == "pending" and rec["submit"]["by"] == "alice"
          and rec["history"][0]["action"] == "submit")

    r = sa.submit(root, "local", local_zip, form, "alice")
    check("B2 版本占号：同 id 同版本重提被拒", not r.get("ok")
          and any("占号" in e for e in r.get("errors", [])))

    r = sa.review(root, "local-foo", "1.0.0", "approve", "bob", "冒烟通过")
    check("B3 审核 approve → published + review 记录", r.get("ok")
          and sa.find_version_record(sa.find_entry(sa.load_catalog(root), "local-foo"),
                                     "1.0.0")["review"]["reviewer"] == "bob")

    # ---- server 双包夹具 ----
    def build_server_pair(base_dir, pid, version, fe_version=None):
        fe_version = fe_version or version
        manifest = {"id": pid, "version": version, "name": "服务插件", "entry": "index.html"}
        payload_plugin = "payload/plugins/%s" % pid
        files = {
            "plugin-package.json": json.dumps({"schema": 1, "kind": "plugin-upgrade",
                                               "id": pid, "version": version,
                                               "api_version": 1, "requires_restart": True}),
            "%s/manifest.json" % payload_plugin: json.dumps(manifest),
            "%s/frontend/index.html" % payload_plugin: "<h1>svc</h1>",
            "%s/backend/__init__.py" % payload_plugin: "",
            "%s/backend/routes.py" % payload_plugin: "def register(app):\n    pass\n",
        }
        import hashlib
        sums = []
        for name, data in files.items():
            if name == "plugin-package.json":
                continue                     # 真实链：sums 只列 payload/**（相对 payload 的路径）
            body = data.encode("utf-8") if isinstance(data, str) else data
            sums.append("%s  %s" % (hashlib.sha256(body).hexdigest(), name[len("payload/"):]))
        files["SHA256SUMS"] = "\n".join(sums) + "\n"
        backend_zip = os.path.join(work, "JZToolsHub-%s-后端-v%s.zip" % (pid, version))
        with open(backend_zip, "wb") as f:
            f.write(make_zip(files).getvalue())
        fe_files = {"frontend/index.html": "<h1>svc</h1>",
                    "frontend-manifest.json": json.dumps(
                        {"id": pid, "version": fe_version, "files": ["index.html"]})}
        fe_zip = os.path.join(work, "JZToolsHub-%s-前端-v%s.zip" % (pid, fe_version))
        with open(fe_zip, "wb") as f:
            f.write(make_zip(fe_files).getvalue())
        return backend_zip, fe_zip

    bz, fz = build_server_pair(work, "svc-bar", "2.0.0")
    r = sa.submit(root, "server", bz, {"name": "服务插件", "changelog": "svc v2"}, "alice",
                  frontend_zip_path=fz)
    check("B4 server 双包上架（成对同版本）", r.get("ok") and r.get("frontend"))
    bz2, fz2 = build_server_pair(work, "svc-baz", "1.0.0", fe_version="0.9.0")
    r = sa.submit(root, "server", bz2, {"name": "x"}, "alice", frontend_zip_path=fz2)
    check("B4 前后端版本不一致被拒", not r.get("ok")
          and any("成对" in e for e in r.get("errors", [])))
    bzl, fzl = build_server_pair(work, "svc-bad", "1.0.0")
    bpath = os.path.join(work, "tampered.zip")
    with zipfile.ZipFile(bzl) as zin, zipfile.ZipFile(bpath, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith("index.html"):
                data += b"<!-- tampered -->"
            zout.writestr(item, data)
    r = sa.submit(root, "server", bpath, {"name": "y"}, "alice", frontend_zip_path=fzl)
    check("B4 后端包被篡改 → SHA256SUMS 核对拒绝", not r.get("ok")
          and any("哈希" in e or "校验" in e for e in r.get("errors", [])))

    # ---- E1 投影：server published 才进 index-server.json；read_index 闭环消费 ----
    r = sa.review(root, "svc-bar", "2.0.0", "approve", "bob")
    idx = sa.read_json(sa.index_server_path(root))
    check("B5 E1 投影：published server 条目进入（schema-1 同构）",
          idx and [p["id"] for p in idx["packages"]] == ["svc-bar"]
          and idx["packages"][0]["file"] == "packages/JZToolsHub-svc-bar-后端-v2.0.0.zip")
    idx_read = sa.plugin_admin.read_index(sa.index_server_path(root))
    svc_row = next((p for p in idx_read["packages"] if p["id"] == "svc-bar"), None)
    check("B5 E1→read_index 闭环：包文件可解析且哈希对上（P1 回归位）",
          idx_read["ok"] and svc_row is not None and svc_row["exists"]
          and svc_row["hash_ok"] is True,
          str(svc_row and (svc_row["exists"], svc_row["hash_ok"])))

    # ---- 下架回退：再发一个旧版本，下架新版后投影回落 ----
    bz3, fz3 = build_server_pair(work, "svc-bar", "2.0.0")   # 同版本占号应拒绝
    r = sa.submit(root, "server", bz3, {"name": "x"}, "alice", frontend_zip_path=fz3)
    check("B5 server 版本占号同源生效", not r.get("ok"))
    bz4, fz4 = build_server_pair(work, "svc-bar", "1.9.0")
    r = sa.submit(root, "server", bz4, {"name": "svc", "changelog": "old"}, "alice",
                  frontend_zip_path=fz4)
    check("B5 追加旧版本号上架允许（记录独立）", r.get("ok"))
    sa.review(root, "svc-bar", "1.9.0", "approve", "bob")
    r = sa.delist(root, "svc-bar", "2.0.0", "bob", "发现缺陷")
    idx = sa.read_json(sa.index_server_path(root))
    proj = {p["id"]: p["version"] for p in idx["packages"]}
    check("B5 下架新版 → 投影自动回落最近已上架旧版", r.get("ok") and proj.get("svc-bar") == "1.9.0")
    rec = sa.find_version_record(sa.find_entry(sa.load_catalog(root), "svc-bar"), "2.0.0")
    check("B5 下架记 history 台账", any(h["action"] == "delist" for h in rec["history"]))
    r = sa.review(root, "svc-bar", "2.0.0", "republish", "alice")
    rec = sa.find_version_record(sa.find_entry(sa.load_catalog(root), "svc-bar"), "2.0.0")
    check("B5 已下架重新上架 → 回到 pending 再审",
          r.get("ok") and rec["status"] == "pending")

    # ---- 部署链（当前形态 = admin inspect/apply）----
    base_dir = os.path.join(work, "B_base")
    os.makedirs(os.path.join(base_dir, "plugins"), exist_ok=True)
    with open(os.path.join(base_dir, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"app": "2.3.3", "schema": 1, "plugin_api": 1}, f)
    sa.review(root, "svc-bar", "1.9.0", "approve", "bob")     # 发布旧版用于部署
    r = sa.deploy_backend_package(base_dir, root, "svc-bar", "1.9.0")
    check("B6 发布即部署：inspect/apply 落 base_dir（含备份登记）", r.get("ok")
          and os.path.isfile(os.path.join(base_dir, "plugins", "svc-bar", "manifest.json")))
    r = sa.deploy_backend_package(base_dir, root, "svc-bar", "1.9.0")
    check("B6 重新部署（同版本 force）可用", r.get("ok"))

    # ---- 统计聚合 ----
    with open(sa.stats_path(root), "w", encoding="utf-8") as f:
        for row in ({"ts": "2026-10-05T09:00:00", "user": "u1", "id": "local-foo",
                     "version": "1.0.0", "via": "client"},
                    {"ts": "2026-10-05T10:00:00", "user": "u2", "id": "local-foo",
                     "version": "1.1.0", "via": "browser"},
                    {"ts": "2026-10-04T10:00:00", "user": "u1", "id": "svc-bar",
                     "version": "1.9.0", "via": "client"}):
            f.write(json.dumps(row) + "\n")
    s = sa.stats_summary(root)
    check("B7 统计聚合：按条目/日/via", s["total"] == 3
          and s["by_package"]["local-foo"] == 2
          and s["by_day"]["2026-10-05"] == 2 and s["by_via"]["client"] == 2)


# ============================================================
# C. 客户端 pkg_install
# ============================================================

def part_c(work):
    print("==> C. 客户端 pkg_install（六连校验 / 前端包 / 组件包 .pending）")
    import pkg_install
    lp_dir = os.path.join(work, "local-plugins")
    os.makedirs(lp_dir, exist_ok=True)

    def local_zip(pid="local-foo", kind="local", mount=None):
        entry = {"frontend": "index.html"}
        if kind == "hybrid":
            entry["backend"] = "routes.py"
            entry["mount"] = mount or ("/api/local/" + pid)
        files = {
            "manifest.json": json.dumps({"id": pid, "runtime": "local", "kind": kind,
                                         "version": "1.0.0", "entry": entry}),
            "frontend/index.html": "<h1>ok</h1>"}
        if kind == "hybrid":
            files["backend/routes.py"] = "def register(app):\n    pass\n"
        return make_zip(files)

    r = pkg_install.install_local_zip(_to_file(work, "c1.zip", local_zip()), lp_dir, [])
    check("C1 合法本地包装装成功", r.get("ok") and r.get("id") == "local-foo")
    r = pkg_install.install_local_zip(_to_file(work, "c2.zip", local_zip(pid="foo")), lp_dir, [])
    check("C1 非 local- 前缀 id 拒装", not r.get("ok"))
    r = pkg_install.install_local_zip(
        _to_file(work, "c3.zip", local_zip(kind="hybrid", mount="/api/other/x")), lp_dir, [])
    check("C1 hybrid mount 前缀不符拒装", not r.get("ok"))
    r = pkg_install.install_local_zip(
        _to_file(work, "c4.zip", local_zip(kind="hybrid")), lp_dir,
        [{"id": "local-bar", "entry": {"mount": "/api/local/local-foo"}}])
    check("C1 mount 冲突拒装", not r.get("ok") and "冲突" in r.get("error", ""))
    evil = make_zip({"manifest.json": json.dumps({"id": "local-foo", "runtime": "local",
                                                  "kind": "local", "version": "1.0.0"}),
                     "../evil.txt": "x"})
    r = pkg_install.install_local_zip(_to_file(work, "c5.zip", evil), lp_dir, [])
    check("C1 路径穿越拒装", not r.get("ok") and "不安全路径" in r.get("error", ""))

    fe_dir = os.path.join(work, "server-frontends")
    fe_ok = make_zip({"frontend/index.html": "<h1>svc</h1>",
                      "frontend/js/app.js": "console.log(1)",
                      "frontend-manifest.json": json.dumps(
                          {"id": "svc-bar", "version": "2.0.0", "files": ["index.html", "js/app.js"]})})
    r = pkg_install.install_frontend_zip(_to_file(work, "c6.zip", fe_ok), fe_dir)
    check("C2 前端包装装：清单圈定伺服范围", r.get("ok") and r.get("id") == "svc-bar")
    check("C2 scan_frontends 回读", len(pkg_install.scan_frontends(fe_dir)) == 1)
    fe_missing = make_zip({"frontend/index.html": "<h1>x</h1>",
                           "frontend-manifest.json": json.dumps(
                               {"id": "svc-bar", "version": "1.0.0", "files": ["index.html", "gone.js"]})})
    r = pkg_install.install_frontend_zip(_to_file(work, "c7.zip", fe_missing), fe_dir)
    check("C2 清单声明文件缺失拒装", not r.get("ok"))
    fe_local = make_zip({"frontend/index.html": "x",
                         "frontend-manifest.json": json.dumps(
                             {"id": "local-foo", "version": "1.0.0", "files": ["index.html"]})})
    r = pkg_install.install_frontend_zip(_to_file(work, "c8.zip", fe_local), fe_dir)
    check("C2 local- 前缀 id 拒装（命名空间铁律）", not r.get("ok"))

    prog = os.path.join(work, "C_program")
    pure = make_zip({"dep-component.json": json.dumps({"id": "puremod", "version": "1.0.0"}),
                     "manifest.json": json.dumps({"components": [
                         {"id": "puremod", "version": "1.0.0", "provides": ["puremod"],
                          "files": ["puremod/__init__.py"]}]}),
                     "payload/pylibs/puremod/__init__.py": "X = 1\n"})
    r = pkg_install.install_component_zip(_to_file(work, "c9.zip", pure), prog)
    check("C3 纯 Python 组件暂存后立即应用（刷新即生效）",
          r.get("ok") and r.get("pending") is False
          and os.path.isfile(os.path.join(prog, "runtime", "pylibs", "puremod", "__init__.py")))
    native = make_zip({"dep-component.json": json.dumps({"id": "natmod", "version": "1.0.0"}),
                       "manifest.json": json.dumps({"components": [
                           {"id": "natmod", "version": "1.0.0", "provides": ["natmod"],
                            "files": ["natmod.dll"]}]}),
                       "payload/pylibs/natmod.dll": "MZ"})
    r = pkg_install.install_component_zip(_to_file(work, "c10.zip", native), prog)
    check("C3 DLL 类组件留暂存（下次启动生效）",
          r.get("ok") and r.get("pending") is True
          and os.path.isdir(os.path.join(prog, "runtime", "pylibs", ".pending", "natmod")))
    comps = pkg_install.installed_components(prog)
    check("C3 installed_components 含已装+暂存",
          {c["id"] for c in comps} >= {"puremod", "natmod"})


def _to_file(work, name, buf):
    path = os.path.join(work, name)
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    return path


# ============================================================
# D. 能力层端到端（stub 上游 + test_client）
# ============================================================

def part_d(work):
    print("==> D. 能力层端到端（安装 / 停用过滤 / preflight / 前端直出 / 切源）")
    from flask import Flask, jsonify, request as fq
    from werkzeug.serving import make_server

    data_root = os.path.join(work, "D_client_data")
    lp_dir = os.path.join(data_root, "local-plugins")
    fe_dir = os.path.join(data_root, "server-frontends")
    static_dir = os.path.join(work, "static")
    os.makedirs(lp_dir, exist_ok=True)
    os.makedirs(os.path.join(static_dir), exist_ok=True)
    with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>static home</html>")

    # ---- 商店夹具目录（stub 上游的 catalog/download）----
    store_root = os.path.join(work, "D_store")
    packages_dir = os.path.join(store_root, "packages")
    os.makedirs(packages_dir, exist_ok=True)
    import hashlib

    def put_pkg(fname, body):
        with open(os.path.join(packages_dir, fname), "wb") as f:
            f.write(body)
        return {"file": fname, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}

    foo_body = make_zip({
        "manifest.json": json.dumps({"id": "local-foo", "runtime": "local", "kind": "local",
                                     "version": "1.0.0", "name": "本地示例", "icon": "🧩",
                                     "entry": {"frontend": "index.html"}}),
        "frontend/index.html": "<h1>local-foo</h1>"}).getvalue()
    foo_meta = put_pkg("JZClient-本地插件-local-foo-v1.0.0.zip", foo_body)
    fe_zip_name = "JZToolsHub-svc-bar-前端-v2.0.0.zip"
    fe_body = make_zip({"frontend/index.html": "<h1>LOCAL svc-bar</h1>",
                        "frontend-manifest.json": json.dumps(
                            {"id": "svc-bar", "version": "2.0.0", "files": ["index.html"]})}).getvalue()
    fe_meta = put_pkg(fe_zip_name, fe_body)
    dep_body = make_zip({"dep-component.json": json.dumps({"id": "depmod", "version": "1.0.0"}),
                         "manifest.json": json.dumps({"components": [
                             {"id": "depmod", "version": "1.0.0", "provides": ["no_such_dep_xyz"],
                              "files": ["depmod/__init__.py"]}]}),
                         "payload/pylibs/depmod/__init__.py": "Y = 2\n"}).getvalue()
    dep_meta = put_pkg("JZToolsHub-依赖-depmod-v1.0.0.zip", dep_body)

    catalog = {"schema": 2, "generated_at": "", "packages": [
        {"id": "local-foo", "name": "本地示例", "runtime": "local", "deps_mode": "shared",
         "requires": {}, **foo_meta, "version": "1.0.0", "api_version": 1,
         "min_app_version": "", "requires_restart": False, "changelog": "",
         "published_at": "2026-10-05T10:00:00"},
        {"id": "svc-bar", "name": "服务插件", "runtime": "server", "deps_mode": "shared",
         "requires": {}, **fe_meta, "frontend_file": fe_zip_name,
         "frontend_sha256": hashlib.sha256(fe_body).hexdigest(), "frontend_size": len(fe_body),
         "version": "2.0.0", "file": "backend.zip", "sha256": "", "size": 0, "api_version": 1,
         "min_app_version": "", "requires_restart": True, "changelog": "",
         "published_at": "2026-10-05T10:00:00"},
        {"id": "depmod", "name": "缺失依赖组件", "runtime": "dep-slot",
         "provides": ["no_such_dep_xyz"], "requires": {}, **dep_meta, "version": "1.0.0",
         "api_version": 1, "min_app_version": "", "requires_restart": False, "changelog": "",
         "published_at": "2026-10-05T10:00:00"},
        {"id": "local-needs", "name": "带缺失依赖的插件", "runtime": "local", "deps_mode": "shared",
         "requires": {"python_packages": [{"name": "no_such_dep_xyz", "version": ">=1.0"}],
                      "external": [{"id": "no-such-ext-app", "version": "",
                                    "required": True,
                                    "hint": "测试用外部程序组件"}]},
         **{k: v for k, v in put_pkg("pkg-local-needs.zip", foo_body).items()},
         "version": "1.0.0", "api_version": 1, "min_app_version": "",
         "requires_restart": False, "changelog": "", "published_at": "2026-10-05T10:00:00"},
    ]}
    tamper_flag = {"on": False}          # D5 用：把 sha256 改错模拟传输损坏

    upstream = Flask(__name__)

    @upstream.get("/login")
    def _login():
        return "login page"

    @upstream.get("/api/tools")
    def _tools():
        return jsonify({"tools": [{"id": "remote-one", "name": "远程插件", "order": 1}],
                        "categories": [{"id": "office", "name": "办公", "enabled": True}]})

    @upstream.get("/plugin/store/api/catalog")
    def _catalog():
        cat = json.loads(json.dumps(catalog))
        if tamper_flag["on"]:
            for p in cat["packages"]:
                if p["id"] == "local-foo":
                    p["sha256"] = "0" * 64
        return jsonify(cat)

    @upstream.get("/plugin/store/api/download/<pid>/<ver>")
    def _download(pid, ver):
        via = fq.args.get("via")
        artifact = fq.args.get("artifact") or "backend"
        stats.append((pid, ver, via))
        for p in catalog["packages"]:
            if p["id"] == pid:
                fname = p["file"]
                if artifact == "frontend" and p.get("frontend_file"):
                    fname = p["frontend_file"]
                body = open(os.path.join(packages_dir, fname), "rb").read()
                return upstream.response_class(body, 200)
        return jsonify({"error": "no such package"}), 404

    @upstream.get("/plugin/svc-bar/index.html")
    def _svc_page():
        return "REMOTE svc-bar"

    stats = []
    srv = make_server("127.0.0.1", 0, upstream, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    addr = "http://127.0.0.1:%d" % srv.server_port

    import capability_app
    build_cfg = {"_program_dir": os.path.join(work, "D_program"),
                 "product": {"version": "0.0.1"}, "plugin_api": 1,
                 "paths": {"data_dirname": "D_client_data"}}
    os.makedirs(build_cfg["_program_dir"], exist_ok=True)
    client_cfg = {"remote_address": addr}
    app = capability_app.create_capability_app(
        build_cfg, client_cfg, addr, static_dir,
        lp_dir, os.path.join(ROOT, "client", "fallback.html"),
        data_dir=data_root, log=lambda m: None,
        shell_token="bench-shell-1")
    c = app.test_client()
    # 壳自证（2026-10-11 需求① G2-6）：台架模拟壳——先 boot 种 Cookie 再探测
    c.get("/shell-boot?_shell=bench-shell-1&next=/")

    # D1 商店安装 local 插件 → 合并清单出现
    r = c.post("/local-api/store/install", json={"id": "local-foo", "version": "1.0.0"})
    check("D1 商店安装 local 插件成功", r.get_json().get("ok"), str(r.get_json()))
    r = c.get("/api/tools")
    ids = [t["id"] for t in r.get_json()["tools"]]
    check("D1 合并清单出现新装本地插件", "local-foo" in ids and "remote-one" in ids, str(ids))
    check("D1 下载统计 via=client 已计", any(x[2] == "client" for x in stats))

    # D2 停用 → 网格隐藏 + 本地 API 404
    c.post("/local-api/plugins/state", json={"id": "local-foo", "enabled": False})
    ids = [t["id"] for t in c.get("/api/tools").get_json()["tools"]]
    check("D2 停用后工具网格隐藏", "local-foo" not in ids)
    r = c.get("/api/local/local-foo")
    check("D2 停用后本地 API 404（兜底语义）", r.status_code == 404)
    c.post("/local-api/plugins/state", json={"id": "local-foo", "enabled": True})
    ids = [t["id"] for t in c.get("/api/tools").get_json()["tools"]]
    check("D2 启用恢复", "local-foo" in ids)

    # D3 preflight：缺依赖映射到依赖包 → 联合安装
    r = c.post("/local-api/store/preflight", json={"id": "local-needs", "version": "1.0.0"})
    d = r.get_json()
    dep_rows = [x for x in d.get("deps", []) if x["name"] == "no_such_dep_xyz"]
    check("D3 preflight 缺依赖 → resolve=dep-package（O1 会合点）",
          bool(dep_rows) and dep_rows[0]["resolve"] == "dep-package"
          and dep_rows[0].get("dep_id") == "depmod", json.dumps(d.get("deps", []), ensure_ascii=False))
    info_rows = [x for x in d.get("deps", []) if x.get("informational")]
    check("D3 外部依赖作提示性行（manual，不触发确认联动）",
          len(info_rows) >= 1 and all(x.get("resolve") == "manual" for x in info_rows)
          and info_rows[0]["name"] == "no-such-ext-app",
          json.dumps(info_rows, ensure_ascii=False))
    r = c.post("/local-api/store/install",
               json={"id": "local-needs", "version": "1.0.0", "include_deps": True,
                     "deps": dep_rows})
    d = r.get_json()
    check("D3 联合安装：依赖包落地（纯 Python 即时生效）",
          d.get("ok") and any(x.get("id") == "depmod" and x.get("ok") and not x.get("pending")
                              for x in d.get("dep_results", [])), str(d))
    check("D3 组件真落在 pylibs",
          os.path.isfile(os.path.join(build_cfg["_program_dir"], "runtime", "pylibs",
                                      "depmod", "__init__.py")))

    # D4 server 前端包：安装 → 本地直出；停用 → 回退中继
    r = c.post("/local-api/store/install", json={"id": "svc-bar", "version": "2.0.0"})
    check("D4 server 条目安装=前端包成功", r.get_json().get("ok")
          and r.get_json().get("frontend") is True, str(r.get_json()))
    r = c.get("/plugin/svc-bar/index.html")
    check("D4 本地直出命中清单文件", b"LOCAL svc-bar" in r.data)
    r = c.get("/local-api/plugins")
    fe = r.get_json().get("server_frontends", [])
    check("D4 lp_list 暴露 server_frontends", any(f["id"] == "svc-bar" for f in fe))
    c.post("/local-api/plugins/state", json={"id": "svc-bar", "enabled": False})
    r = c.get("/plugin/svc-bar/index.html")
    check("D4 停用 → 回退中继直出（副本保留）", b"REMOTE svc-bar" in r.data
          and os.path.isdir(os.path.join(fe_dir, "svc-bar")))
    c.post("/local-api/plugins/state", json={"id": "svc-bar", "enabled": True})
    r = c.get("/plugin/svc-bar/index.html")
    check("D4 启用 → 本地直出恢复", b"LOCAL svc-bar" in r.data)
    r.close()                                        # Windows：释放本地伺服句柄再卸载
    ru = c.post("/local-api/plugins/uninstall", json={"id": "svc-bar"})
    r = c.get("/plugin/svc-bar/index.html")
    check("D4 卸载本地副本 → 中继直出兜底", b"REMOTE svc-bar" in r.data
          and not os.path.isdir(os.path.join(fe_dir, "svc-bar")),
          "status=%s body=%r fe_dir_exists=%s uninstall=%s" % (
              r.status_code, r.data[:60],
              os.path.isdir(os.path.join(fe_dir, "svc-bar")), ru.get_json()))

    # D5 sha256 拒装（C7）
    tamper_flag["on"] = True
    r = c.post("/local-api/store/install", json={"id": "local-foo", "version": "1.0.0"})
    check("D5 包哈希不符拒装（介质损坏或被篡改）", not r.get_json().get("ok")
          and "sha256" in r.get_json().get("error", ""))
    tamper_flag["on"] = False

    # D6 切源：share（无索引）报错可恢复；plugin-source 读写免重启
    r = c.post("/local-api/config/plugin-source",
               json={"type": "share", "index_path": os.path.join(work, "no-index.json")})
    check("D6 plugin-source 写入成功", r.get_json().get("ok"))
    r = c.post("/local-api/plugins/check-update")
    d = r.get_json()
    check("D6 切 share 后走共享盘语义（无索引报错）", d.get("source") == "share"
          and bool(d.get("error")))
    r = c.post("/local-api/config/plugin-source", json={"type": "server"})
    r = c.post("/local-api/plugins/check-update")
    d = r.get_json()
    upd = {u["id"] for u in d.get("updates", [])}
    check("D6 切回 server：check-update 经商店目录（local-needs 可升级）",
          d.get("source") == "server" and "local-needs" in upd, str(d))

    srv.shutdown()


def main():
    work = tempfile.mkdtemp(prefix="jz_store_batch8_")
    print("工作目录：%s" % work)
    try:
        part_a_and_store_readface(work)
        part_b(work)
        part_c(work)
        part_d(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print()
    fails = [r for r in RESULTS if not r[1]]
    print("结果：%d 项，FAIL %d" % (len(RESULTS), len(fails)))
    for name, _ok, detail in fails:
        print("  FAIL: %s  — %s" % (name, detail[:200]))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
