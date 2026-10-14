# -*- coding: utf-8 -*-
r"""依赖卸载状态刷新 + 详情卡「检测本机依赖」台架（2026-10-12 需求①②）。

覆盖：
  A 卸载前：组件清单（runtime/pylibs/manifest.json）声明 openpyxl → 检测=ok；商店状态=已安装
  B 卸载：/local-api/store/uninstall-dep → 目录删 + **清单同步移除**（真机 bug 根因）
  C 卸载后：/local-api/plugins 的 components 不再含该组件（商店卡片翻未安装）
  D 卸载后：检测本机依赖 → missing + resolve=dep-package（映射到可装的依赖包条目）
  E 启动清扫：.pending-remove 残件在下次启动被删，且清单条目同步清（防旧版残留）
  F drop_component_entry 边界：幂等 / 无清单 / 单组件简写形态
  G 文件级卸载（载荷铺 pylibs 根、无 <cid> 目录）：删净 + 共享文件保留 + 登记清
  H 文件被占用 → pending_remove 台账 → 下次启动清扫销账
  I 清扫按登记收尾「散落载荷」孤儿（numpy.libs 实例）
  J 本地插件健康度「功能降级」角标数据 + A 类 informational
  K 卸载依赖 → 同一实例主界面即刻见降级（免重启）
  L 卸载进度台账（2026-10-13 需求①）：running 中间百分比单调 → done/100；未知 token=unknown；
    不带 progress_id 行为不变（旧前端兜底）
  M 「待重启」判据（2026-10-13 需求②）：暂存组件 components.pending=true（按钮定格「待重启」）；
    已生效组件 pending=false（「已安装」）；待重启组件可卸载撤销 → 回「安装」
  N 「待重启」升级场景（2026-10-14 需求③：依赖装完但用户未重启，按钮必须显示「待重启」）：
    登记有旧版 + 暂存区有新版的同 id → pending=true 且版本取暂存声明（此前只报旧版本的
    pending=false，商店把「装完待重启」显示成「已安装」/「升级到 vX」，看着像安装没生效）；
    暂存残骸（无 entry.json）→ 仍报 pending=true、版本退回登记值
  P 真实安装端点端到端（2026-10-14 需求③）：走 /local-api/store/install 装 DLL 类依赖 →
    restart_required=true（前端弹重启询问）+ components.pending=true（页面重载后的判据）；
    应用暂存（=重启）后 pending=false「已安装」；升级装完 → pending=true 且版本=新版本；
    联合安装一并装上的 DLL 依赖 → dep_results[].pending=true（前端据此定格依赖卡片）

用法：python tools\e2e\store-deps-bench.py   （退出码 0=全过）
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "client"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %s%s" % ("[OK]" if ok else "[!!]", name, ("  — " + detail) if detail else ""))


def catalog_fixture():
    return {"generated_at": "", "packages": [
        {"id": "depx", "runtime": "dep-slot", "name": "依赖X包", "version": "1.0.0",
         "requires": {}, "provides": ["openpyxl"], "size": 100,
         "sha256": "0" * 64, "file": "depx.zip"},
        {"id": "local-needs", "runtime": "local", "name": "需要 openpyxl 的插件", "version": "1.0.0",
         "requires": {"python_packages": [
             {"name": "openpyxl", "version": ">=3.1", "required": True,
              "hint": "缺少 openpyxl：xlsx 表格过滤不可用。请在插件商店「依赖」页签安装 openpyxl 组件包。"}]},
         "size": 100, "sha256": "1" * 64, "file": "needs.zip"},
        {"id": "svc-needs", "runtime": "server", "name": "服务端插件（需 openpyxl）", "version": "1.0.0",
         "requires": {"python_packages": [
             {"name": "openpyxl", "version": ">=3.1", "required": False,
              "hint": "缺少 openpyxl：服务端导出不可用。"}]},
         "size": 100, "sha256": "2" * 64, "file": "svc.zip",
         "frontend_file": "svc-fe.zip", "frontend_size": 50, "frontend_sha256": "3" * 64},
    ]}


def stub_server(catalog, packages_dir=None):
    from flask import Flask, jsonify, request as fq
    app = Flask("stub-deps")
    # 目录/包目录可在运行中替换（app._bench_catalog / app._bench_pkg_dir）：
    # 用于模拟「同一依赖条目出了新版本」与避免为一个桩再起一个服务。
    app._bench_catalog = catalog
    app._bench_pkg_dir = packages_dir

    @app.post("/api/login")
    def _login():
        resp = jsonify({"ok": True, "user": {"username": "bench"}})
        resp.set_cookie("stub_user", "bench", path="/")
        return resp

    @app.get("/api/session")
    def _session():
        u = fq.cookies.get("stub_user") or ""
        return jsonify({"user": {"username": u} if u else None})

    @app.get("/api/tools")
    def _tools():
        return jsonify({"tools": [], "categories": []})

    @app.get("/plugin/store/api/catalog")
    def _catalog():
        return jsonify(app._bench_catalog)

    @app.get("/plugin/store/api/download/<pid>/<ver>")
    def _download(pid, ver):
        """包分发桩（与商店插件同形）：按 catalog 声明的 file 从包目录回包。"""
        for p in (app._bench_catalog.get("packages") or []):
            if p.get("id") != pid:
                continue
            fname = p.get("file")
            if fq.args.get("artifact") == "frontend" and p.get("frontend_file"):
                fname = p["frontend_file"]
            pth = os.path.join(app._bench_pkg_dir or "", os.path.basename(str(fname or "")))
            if fname and os.path.isfile(pth):
                with open(pth, "rb") as f:
                    return app.response_class(f.read(), 200)
        return jsonify({"error": "no such package"}), 404

    @app.get("/plugin/store/api/installs")
    def _installs():
        return jsonify({"ok": True, "installs": dict(getattr(app, '_bench_installs', {}) or {}),
                        "updated_at": ""})

    @app.post("/plugin/store/api/installs")
    def _installs_write():
        """登记写（D18/升级补种会用到；真机 API 同形）。"""
        body = fq.get_json(force=True, silent=True) or {}
        action = body.get("action")
        mine = dict(getattr(app, '_bench_installs', {}) or {})
        for pid in [str(x) for x in (body.get("ids") or [body.get("id")]) if x]:
            if action == "install":
                mine[pid] = {"version": str(body.get("version") or ""),
                             "installed_at": "2026-01-01T00:00:00", "enabled": True,
                             "source": str(body.get("source") or "store")}
            elif action == "uninstall":
                mine.pop(pid, None)
            elif action in ("enable", "disable") and pid in mine:
                mine[pid] = dict(mine[pid], enabled=(action == "enable"))
        app._bench_installs = mine
        return jsonify({"ok": True, "installs": dict(mine)})

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_port


def write_manifest(pylibs, comps, shorthand=False):
    os.makedirs(pylibs, exist_ok=True)
    if shorthand:
        body = {"schema": 1, "id": comps[0]["id"], "name": comps[0].get("name", ""),
                "version": comps[0].get("version", ""), "packages": comps[0]["packages"]}
    else:
        body = {"schema": 1, "kind": "dep-components", "updated_at": "", "components": comps}
    with open(os.path.join(pylibs, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)


def comp_entry(cid, version="1.0.0", packages=None, files=None):
    # modules 的值是**发行包名**（契约：import 名 → 包名，如 {"cv2": "opencv-python"}）
    e = {"id": cid, "name": cid, "version": version,
         "packages": packages or {"openpyxl": "3.1.5"},
         "modules": {"openpyxl": "openpyxl"}}
    if files:
        e["files"] = files
    return e


# ---------------------------- 真实安装端点用的小包构造 ----------------------------

def make_zip(files):
    """最小 zip 构造（依赖组件包 / 本地插件包桩）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def put_pkg(pkg_dir, fname, body):
    """把包写进包目录并回 catalog 的 file/sha256/size 三件套。"""
    with open(os.path.join(pkg_dir, fname), "wb") as f:
        f.write(body)
    return {"file": fname, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}


def dep_component_zip(cid, version, native=True, provides=()):
    """依赖组件包桩：native=True 造 DLL 类载荷（暂存进 .pending，重启才生效）。"""
    files = ["%s.dll" % cid] if native else ["%s/__init__.py" % cid]
    payload = ({"payload/pylibs/%s.dll" % cid: "MZ"} if native
               else {"payload/pylibs/%s/__init__.py" % cid: "X = 1\n"})
    body = {"dep-component.json": json.dumps({"id": cid, "version": version}),
            "manifest.json": json.dumps({"components": [
                {"id": cid, "version": version, "provides": list(provides),
                 "packages": {"%s-pkg" % cid: version}, "modules": {},
                 "files": files}]})}
    body.update(payload)
    return make_zip(body)


def local_plugin_zip(pid, version, require_name):
    """本地插件包桩：requires 声明一个依赖名（联合安装用）。"""
    return make_zip({
        "manifest.json": json.dumps({
            "id": pid, "runtime": "local", "kind": "local", "version": version,
            "name": pid, "icon": "🧩", "entry": {"frontend": "index.html"},
            "requires": {"python_packages": [
                {"name": require_name, "version": ">=1.0", "required": True,
                 "hint": "缺少 %s：演示用。" % require_name}]}}),
        "frontend/index.html": "<h1>%s</h1>" % pid})


def make_app(data_root, addr, static_dir):
    """构造能力层 app（new_app / new_app_server 共用）。"""
    os.environ["JZCLIENT_DATA_DIR"] = data_root
    import capability_app
    build_cfg = {"_program_dir": os.path.join(data_root, "_program"),
                 "product": {"version": "0.0.1"}, "plugin_api": 1,
                 "paths": {"data_dirname": "d"}}
    os.makedirs(build_cfg["_program_dir"], exist_ok=True)
    app = capability_app.create_capability_app(
        build_cfg, {"remote_address": addr}, addr, static_dir,
        os.path.join(data_root, "local-plugins"),
        os.path.join(ROOT, "client", "fallback.html"),
        data_dir=data_root, log=lambda m: None, shell_token="deps-bench")
    return build_cfg, app


def new_app(data_root, addr, static_dir):
    build_cfg, app = make_app(data_root, addr, static_dir)
    c = app.test_client()
    c.get("/shell-boot?_shell=deps-bench&next=/")
    c.post("/api/login", json={"username": "bench"})
    return build_cfg, c


def new_app_server(data_root, addr, static_dir):
    """真 HTTP 版（卸载进度断言需跨线程并发读台账）：返回 (build_cfg, base, session, srv)。"""
    import time as _time
    import requests
    from werkzeug.serving import make_server
    build_cfg, app = make_app(data_root, addr, static_dir)
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_port
    c = requests.Session()
    for _ in range(50):                     # 起服要一小会儿
        try:
            c.get(base + "/shell-boot?_shell=deps-bench&next=/", timeout=2)
            break
        except Exception:
            _time.sleep(0.05)
    c.post(base + "/api/login", json={"username": "bench"}, timeout=5)
    return build_cfg, base, c, srv


def comps_of(c):
    d = c.get("/local-api/plugins").get_json()
    return [x.get("id") for x in (d.get("components") or [])], d


def main():
    work = tempfile.mkdtemp(prefix="deps-bench-")
    try:
        data_root = os.path.join(work, "A")
        static_dir = os.path.join(work, "static")
        os.makedirs(static_dir, exist_ok=True)
        with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html>home</html>")
        srv, addr = stub_server(catalog_fixture())
        try:
            # —— 环境：装好 depx（目录 + 清单条目），并留一个 .pending-remove 残件 depy ——
            prog = os.path.join(data_root, "_program")
            pylibs = os.path.join(prog, "runtime", "pylibs")
            os.makedirs(os.path.join(pylibs, "depx"), exist_ok=True)
            with open(os.path.join(pylibs, "depx", "payload.txt"), "w") as f:
                f.write("x")
            os.makedirs(os.path.join(pylibs, ".pending-remove", "depy"), exist_ok=True)
            write_manifest(pylibs, [comp_entry("depx"), comp_entry("depy")])
            # 主体已安装依赖清单（打包态必有；缺它判定侧一律 unknown——源码模式属正常）
            os.makedirs(os.path.join(prog, "config"), exist_ok=True)
            with open(os.path.join(prog, "config", "installed-deps.json"), "w", encoding="utf-8") as f:
                json.dump({"schema": 1, "packages": {"flask": "3.1.3"},
                           "modules": {"flask": "flask"}, "generated_at": "", "python": "3.14"}, f)

            build_cfg, c = new_app(data_root, addr, static_dir)

            ids, _ = comps_of(c)
            check("A1 卸载前：components 含 depx（商店显示已安装的依据）", "depx" in ids, str(ids))

            r = c.post("/local-api/store/deps-check", json={"id": "local-needs", "version": "1.0.0"})
            j = r.get_json()
            row = next((x for x in (j.get("rows") or []) if x.get("name") == "openpyxl"), None)
            check("A2 卸载前：检测本机依赖 → openpyxl=ok（组件清单声明 packages）",
                  j.get("ok") and row and row.get("state") == "ok", json.dumps(row, ensure_ascii=False))
            check("A2b ok 行不带 note（提示只在缺失时出现）", not (row or {}).get("note"),
                  json.dumps((row or {}).get("note", ""), ensure_ascii=False))

            r = c.post("/local-api/store/uninstall-dep", json={"id": "depx"})
            j = r.get_json()
            check("B1 卸载 depx → ok 且非 pending_remove", j.get("ok") and not j.get("pending_remove"),
                  json.dumps(j, ensure_ascii=False))
            check("B2 组件目录已删", not os.path.isdir(os.path.join(pylibs, "depx")))

            with open(os.path.join(pylibs, "manifest.json"), "r", encoding="utf-8") as f:
                m = json.load(f)
            ids2 = [x.get("id") for x in (m.get("components") or [])]
            check("B3 清单条目同步移除（真机 bug 根因修复点）", "depx" not in ids2, str(ids2))

            ids3, _ = comps_of(c)
            check("C1 卸载后：components 不再含 depx（商店卡片翻未安装）", "depx" not in ids3, str(ids3))

            r = c.post("/local-api/store/deps-check", json={"id": "local-needs", "version": "1.0.0"})
            j = r.get_json()
            row = next((x for x in (j.get("rows") or []) if x.get("name") == "openpyxl"), None)
            check("D1 卸载后：检测 → missing 且映射到依赖包 depx",
                  row and row.get("state") == "missing" and row.get("resolve") == "dep-package"
                  and row.get("dep_id") == "depx", json.dumps(row, ensure_ascii=False))
            check("D2 missing 行带插件自声明 hint（原样带回展示）",
                  bool((row or {}).get("note")) and "openpyxl" in str((row or {}).get("note")),
                  str((row or {}).get("note"))[:60])

            r = c.post("/local-api/store/uninstall-dep", json={"id": "depx"})
            check("B4 重复卸载 → 404「该依赖组件未安装」", r.status_code == 404,
                  str(r.status_code) + " " + str(r.get_json()))

            # —— E：启动清扫（新实例启动时执行）——
            ids4, _ = comps_of(c)   # 触发一次渲染；清扫发生在上一个实例创建时
            with open(os.path.join(pylibs, "manifest.json"), "r", encoding="utf-8") as f:
                m = json.load(f)
            ids5 = [x.get("id") for x in (m.get("components") or [])]
            check("E1 启动清扫：.pending-remove 残件目录已删",
                  not os.path.isdir(os.path.join(pylibs, ".pending-remove", "depy")))
            check("E2 启动清扫：残件清单条目同步清（防旧版卸载残留）", "depy" not in ids5, str(ids5))

            # —— F：drop_component_entry 边界 ——
            import jz_deps
            check("F1 幂等：对已移除的 id 再 drop → False",
                  jz_deps.drop_component_entry(prog, "depx") is False)
            check("F2 无清单：目录缺 manifest → False（无副作用）",
                  jz_deps.drop_component_entry(os.path.join(work, "nope"), "depx") is False)
            write_manifest(pylibs, [comp_entry("depz")], shorthand=True)
            got = jz_deps.drop_component_entry(prog, "depz")
            with open(os.path.join(pylibs, "manifest.json"), "r", encoding="utf-8") as f:
                m2 = json.load(f)
            check("F3 单组件简写形态：命中 id → 清空顶层 packages", got and "packages" not in m2,
                  json.dumps(m2, ensure_ascii=False))

            # —— G：文件级卸载（真机 docx 症状：载荷铺在 pylibs 根、无 <cid> 目录）——
            def put(rel):
                q = os.path.join(pylibs, *rel.split("/"))
                os.makedirs(os.path.dirname(q), exist_ok=True)
                with open(q, "w") as f:
                    f.write("x")
                return q
            d1 = put("docx/__init__.py")
            lx = put("lxml/etree.py")
            put("typing_extensions.py")
            sh = put("shared.dll")
            write_manifest(pylibs, [comp_entry("docx", files=["docx/__init__.py", "lxml/etree.py",
                                                              "typing_extensions.py", "shared.dll"]),
                                    comp_entry("other", files=["shared.dll"])])
            r = c.post("/local-api/store/uninstall-dep", json={"id": "docx"})
            j = r.get_json()
            check("G1 无 <cid> 目录也能卸载（真机 docx 症状修复）", bool(j.get("ok")),
                  json.dumps(j, ensure_ascii=False))
            check("G2 载荷文件已删 + 空目录剪枝",
                  not os.path.exists(d1) and not os.path.exists(lx)
                  and not os.path.isdir(os.path.join(pylibs, "docx")))
            check("G3 共享文件保留（other 仍声明 shared.dll）", os.path.isfile(sh))
            ids6, _ = comps_of(c)
            check("G4 登记清 → components 不含 docx", "docx" not in ids6, str(ids6))
            r = c.post("/local-api/store/uninstall-dep", json={"id": "docx"})
            check("G5 再卸载 → 404（确无登记无目录）", r.status_code == 404, str(r.status_code))

            write_manifest(pylibs, [comp_entry("ghost", files=["ghost/mod.py"])])
            r = c.post("/local-api/store/uninstall-dep", json={"id": "ghost"})
            j = r.get_json()
            check("G6 文件缺失但登记在 → 仍可卸载（清登记并说明）",
                  bool(j.get("ok")) and "已清除安装登记" in (j.get("note") or ""),
                  json.dumps(j, ensure_ascii=False))

            # —— H：文件被占用 → 台账 + 下次启动清扫收尾 ——
            locked = put("lockdir/mod.py")
            write_manifest(pylibs, [comp_entry("locky", files=["lockdir/mod.py"])])
            fh = open(locked, "r+b")          # 持有句柄：Windows 下删除会被拒
            r = c.post("/local-api/store/uninstall-dep", json={"id": "locky"})
            j = r.get_json()
            check("H1 被占用 → ok + pending_remove 台账", bool(j.get("ok")) and bool(j.get("pending_remove")),
                  json.dumps(j, ensure_ascii=False))
            jr = os.path.join(pylibs, ".pending-remove", "locky.json")
            check("H2 台账文件已写", os.path.isfile(jr))
            fh.close()
            _, c2 = new_app(data_root, addr, static_dir)      # 模拟下次启动（创建时清扫）
            check("H3 下次启动清扫：文件已删 + 台账销账",
                  not os.path.exists(locked) and not os.path.isfile(jr))
            ids7, _ = comps_of(c2)
            check("H4 清扫后 components 不含 locky", "locky" not in ids7, str(ids7))

            # —— I：清扫按登记收尾"散落载荷"（真机 numpy.libs 孤儿实例）——
            put("orpy/pkg/mod.py")
            put("orpy.libs/bin.dat")
            write_manifest(pylibs, [comp_entry("orpy", files=["orpy/pkg/mod.py", "orpy.libs/bin.dat"])])
            os.makedirs(os.path.join(pylibs, ".pending-remove", "orpy"), exist_ok=True)
            with open(os.path.join(pylibs, ".pending-remove", "orpy", "old.dll"), "w") as f:
                f.write("x")
            _, c3 = new_app(data_root, addr, static_dir)      # 启动清扫
            check("I1 清扫删目录 + 按登记收尾散落载荷（孤儿清零）",
                  not os.path.isdir(os.path.join(pylibs, "orpy"))
                  and not os.path.isdir(os.path.join(pylibs, "orpy.libs"))
                  and not os.path.isdir(os.path.join(pylibs, ".pending-remove", "orpy")))
            with open(os.path.join(pylibs, "manifest.json"), "r", encoding="utf-8") as f:
                m3 = json.load(f)
            ids8 = [x.get("id") for x in (m3.get("components") or [])]
            check("I2 清扫后登记已清", "orpy" not in ids8, str(ids8))
        finally:
            srv.shutdown()

        # —— J：本地插件健康度（「功能降级」角标数据）+ A 类依赖 informational ——
        jroot = os.path.join(work, "J")
        jlp = os.path.join(jroot, "local-plugins", "local-needs")
        os.makedirs(jlp, exist_ok=True)
        with open(os.path.join(jlp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "local-needs", "name": "需要 openpyxl 的插件", "version": "1.0.0",
                       "runtime": "local", "kind": "local",
                       "entry": {"frontend": "index.html"},
                       "requires": {"python_packages": [
                           {"name": "openpyxl", "version": ">=3.1", "required": False,
                            "hint": "缺少 openpyxl：xlsx 不可用。"}]}}, f, ensure_ascii=False)
        with open(os.path.join(jlp, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>needs</h1>")
        jstatic = os.path.join(work, "Jstatic")
        os.makedirs(jstatic, exist_ok=True)
        with open(os.path.join(jstatic, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html>home</html>")
        srv2, addr2 = stub_server(catalog_fixture())
        srv2.app._bench_installs = {"bench": {"local-needs": {
            "version": "1.0.0", "enabled": True, "source": "store", "installed_at": ""}}}
        try:
            jprog = os.path.join(jroot, "_program")
            jpy = os.path.join(jprog, "runtime", "pylibs")
            os.makedirs(os.path.join(jprog, "config"), exist_ok=True)
            with open(os.path.join(jprog, "config", "installed-deps.json"), "w", encoding="utf-8") as f:
                json.dump({"schema": 1, "packages": {"flask": "3.1.3"},
                           "modules": {"flask": "flask"}}, f)
            os.makedirs(jpy, exist_ok=True)
            write_manifest(jpy, [])

            _, cj = new_app(jroot, addr2, jstatic)
            d = cj.get("/api/tools").get_json()
            item = next((t for t in (d.get("tools") or []) if t.get("id") == "local-needs"), None)
            check("J1 缺依赖的本地插件 → 主界面网格标「功能降级」+ 具名清单",
                  bool(item) and item.get("health") == "degraded"
                  and "openpyxl" in (item.get("health_degraded") or []),
                  json.dumps(item, ensure_ascii=False)[:200])

            j1 = cj.post("/local-api/store/deps-check", json={"id": "svc-needs", "version": "1.0.0"}).get_json()
            row = (j1.get("rows") or [{}])[0]
            check("J2 A 类条目：依赖行标 informational + server_side（本机检测不决定可用性）",
                  j1.get("server_side") is True and row.get("informational") is True,
                  json.dumps({"server_side": j1.get("server_side"), "row": row}, ensure_ascii=False)[:200])
            j2 = cj.post("/local-api/store/deps-check", json={"id": "local-needs", "version": "1.0.0"}).get_json()
            r2 = (j2.get("rows") or [{}])[0]
            check("J3 本地条目：依赖行 informational=False（可联动本机安装）",
                  j2.get("server_side") is False and r2.get("informational") is False,
                  json.dumps(r2, ensure_ascii=False)[:160])

            write_manifest(jpy, [comp_entry("depx")])      # 装上提供方
            _, cj2 = new_app(jroot, addr2, jstatic)
            d2 = cj2.get("/api/tools").get_json()
            item2 = next((t for t in (d2.get("tools") or []) if t.get("id") == "local-needs"), None)
            check("J4 补装依赖后 → 健康度回 ok（角标消失）",
                  bool(item2) and (item2.get("health") or "ok") == "ok",
                  json.dumps(item2, ensure_ascii=False)[:160])

            # K：卸载依赖 → **同一实例**回主界面即刻见降级（免重启，2026-10-12 需求）
            r = cj2.post("/local-api/store/uninstall-dep", json={"id": "depx"})
            check("K1 卸载依赖端点 ok", bool((r.get_json() or {}).get("ok")),
                  json.dumps(r.get_json(), ensure_ascii=False)[:120])
            d3 = cj2.get("/api/tools").get_json()
            item3 = next((t for t in (d3.get("tools") or []) if t.get("id") == "local-needs"), None)
            check("K2 同一实例回主界面即见「功能降级」（无需重启）",
                  bool(item3) and item3.get("health") == "degraded"
                  and "openpyxl" in (item3.get("health_degraded") or []),
                  json.dumps(item3, ensure_ascii=False)[:160])

            # —— L：卸载进度台账（2026-10-13 需求①：依赖卸载按钮内进度条的服务端一半）——
            lroot = os.path.join(work, "L")
            lstatic = os.path.join(work, "Lstatic")
            os.makedirs(lstatic, exist_ok=True)
            with open(os.path.join(lstatic, "index.html"), "w", encoding="utf-8") as f:
                f.write("<html>home</html>")
            srv3, addr3 = stub_server(catalog_fixture())
            l_srv = None
            try:
                lpy = os.path.join(lroot, "_program", "runtime", "pylibs")
                n_files = 1200
                for i in range(n_files):          # 载荷铺 pylibs 根（真机 docx 同形）：逐项删除可被 5ms 轮询观测
                    q = os.path.join(lpy, "docs", "mod%02d" % (i % 20), "f%04d.py" % i)
                    os.makedirs(os.path.dirname(q), exist_ok=True)
                    with open(q, "w") as f:
                        f.write("x")
                os.makedirs(os.path.join(lroot, "_program", "config"), exist_ok=True)
                with open(os.path.join(lroot, "_program", "config", "installed-deps.json"),
                          "w", encoding="utf-8") as f:
                    json.dump({"schema": 1, "packages": {"flask": "3.1.3"}}, f)
                write_manifest(lpy, [comp_entry("bigpkg", files=["docs"])])
                _, lbase, cl, l_srv = new_app_server(lroot, addr3, lstatic)

                samples = []
                stop = threading.Event()

                def _poll():
                    while not stop.is_set():
                        try:
                            p = cl.get(lbase + "/local-api/store/uninstall-dep/progress?id=p1",
                                       timeout=5).json()
                        except Exception:
                            continue
                        samples.append(p)
                        if p.get("status") in ("done", "error"):
                            return
                        time.sleep(0.005)

                th = threading.Thread(target=_poll, daemon=True)
                th.start()
                r = cl.post(lbase + "/local-api/store/uninstall-dep",
                            json={"id": "bigpkg", "progress_id": "p1"}, timeout=60)
                j = r.json()
                stop.set()
                th.join(3)
                check("L1 带 progress_id 卸载 → ok + 载荷整树删净",
                      bool(j.get("ok")) and j.get("files_removed") == 1
                      and not os.path.isdir(os.path.join(lpy, "docs")),
                      json.dumps(j, ensure_ascii=False)[:160])
                running = [s for s in samples if s.get("status") == "running"]
                pcts = [int(s.get("pct") or 0) for s in running]
                check("L2 轮询观测到运行中百分比（0<pct≤99）——按钮内进度条的数据源",
                      0 < max(pcts) <= 99 and max(pcts) > 20,      # 越过目录阶段=载荷文件级推进在跑
                      str(pcts[:16]))
                check("L3 百分比单调不减",
                      all(pcts[i] <= pcts[i + 1] for i in range(len(pcts) - 1)), str(pcts[:16]))
                pfin = cl.get(lbase + "/local-api/store/uninstall-dep/progress?id=p1",
                              timeout=5).json()
                check("L4 终态 done/100", pfin.get("status") == "done" and pfin.get("pct") == 100,
                      json.dumps(pfin, ensure_ascii=False))
                pu = cl.get(lbase + "/local-api/store/uninstall-dep/progress?id=nope", timeout=5).json()
                check("L5 未知 token → unknown（前端据此切 .indet 不定态兜底）",
                      pu.get("status") == "unknown", json.dumps(pu, ensure_ascii=False))
                r2 = cl.post(lbase + "/local-api/store/uninstall-dep", json={"id": "bigpkg"},
                             timeout=30)
                check("L6 不带 progress_id（旧前端）→ 行为不变：404 未安装",
                      r2.status_code == 404, str(r2.status_code))

                # —— M：「待重启」契约（2026-10-13 需求②：装完定格「待重启」的判据）——
                staged = os.path.join(lpy, ".pending", "waitnative")
                os.makedirs(staged, exist_ok=True)
                d = cl.get(lbase + "/local-api/plugins", timeout=5).json()
                comp = next((x for x in (d.get("components") or [])
                             if x.get("id") == "waitnative"), None)
                check("M1 暂存组件（DLL 待重启）在 components 报 pending=true ——"
                      " 前端据此把按钮定格「待重启」而非翻回「安装」",
                      bool(comp) and comp.get("pending") is True,
                      json.dumps(comp, ensure_ascii=False))
                write_manifest(lpy, [comp_entry("ready")])
                d = cl.get(lbase + "/local-api/plugins", timeout=5).json()
                comp2 = next((x for x in (d.get("components") or []) if x.get("id") == "ready"), None)
                check("M2 已生效组件 pending=false —— 按钮显示「已安装」",
                      bool(comp2) and comp2.get("pending") is False,
                      json.dumps(comp2, ensure_ascii=False))
                r = cl.post(lbase + "/local-api/store/uninstall-dep",
                            json={"id": "waitnative", "progress_id": "p2"}, timeout=30)
                check("M3 待重启组件可直接卸载（撤销暂存）", bool((r.json() or {}).get("ok")),
                      str(r.status_code) + " " + json.dumps(r.json(), ensure_ascii=False)[:120])
                d = cl.get(lbase + "/local-api/plugins", timeout=5).json()
                check("M4 卸载后 pending 组件消失 —— 按钮回「安装」",
                      "waitnative" not in [x.get("id") for x in (d.get("components") or [])],
                      str([x.get("id") for x in (d.get("components") or [])]))

                # —— N：升级场景的「待重启」判据（2026-10-14 需求③）——
                # 依赖组件升级 = 新版本先暂存（.pending/<id>）、旧版本仍在登记里；此时商店
                # 卡片必须定格「待重启」——此前 installed_components 只看"id 是否已在登记里"，
                # 同 id 的暂存被跳过 → 报旧版本 + pending=false，卡片显示「已安装」/「升级到 x」。
                write_manifest(lpy, [comp_entry("gradeup", version="1.0.0")])
                gstage = os.path.join(lpy, ".pending", "gradeup")
                os.makedirs(os.path.join(gstage, "payload"), exist_ok=True)
                with open(os.path.join(gstage, "entry.json"), "w", encoding="utf-8") as f:
                    json.dump({"schema": 1, "id": "gradeup",
                               "entry": comp_entry("gradeup", version="2.0.0"),
                               "remove": [], "staged_by": "install-dep-component.ps1"}, f)
                d = cl.get(lbase + "/local-api/plugins", timeout=5).json()
                comp3 = next((x for x in (d.get("components") or [])
                              if x.get("id") == "gradeup"), None)
                check("N1 升级已暂存（登记 v1.0.0 + 暂存 v2.0.0）→ pending=true 且版本取暂存声明"
                      " —— 卡片定格「待重启」而不是「已安装」/「升级到 v2.0.0」",
                      bool(comp3) and comp3.get("pending") is True
                      and comp3.get("version") == "2.0.0",
                      json.dumps(comp3, ensure_ascii=False))
                # 暂存残骸（无 entry.json）：仍算待重启，版本退回登记值（不误报"未安装"）
                write_manifest(lpy, [comp_entry("gradeup", version="1.0.0"),
                                     comp_entry("graderaw", version="1.0.0")])
                os.makedirs(os.path.join(lpy, ".pending", "graderaw"), exist_ok=True)
                d = cl.get(lbase + "/local-api/plugins", timeout=5).json()
                comp4 = next((x for x in (d.get("components") or [])
                              if x.get("id") == "graderaw"), None)
                check("N2 暂存残骸（缺 entry.json）→ pending=true + 版本退回登记值",
                      bool(comp4) and comp4.get("pending") is True
                      and comp4.get("version") == "1.0.0",
                      json.dumps(comp4, ensure_ascii=False))

                # —— P：真实安装端点端到端（2026-10-14 需求③）——
                # 需求原文：依赖安装完成后，若用户未选择重启，依赖的「安装」按钮应显示「待重启」。
                # M/N 段只钉"暂存区 → components"这一段；这里从 /local-api/store/install 真装一个
                # **DLL 类依赖组件包**开始，逐段钉住前端两条判据的数据源：
                #   ① 安装响应 restart_required=true（前端据此弹「需要重启」询问）
                #   ② 未重启期间 /local-api/plugins 的 components[].pending=true（页面重载后的判据）
                #   ③ 应用暂存（=用户点「立即重启」）→ pending=false 翻「已安装」
                #   ④ 升级装完待重启 → pending=true 且版本取新版本（不显示「升级到 vX」）
                #   ⑤ 联合安装一并装上的 DLL 依赖 → dep_results[].pending=true（定格依赖卡片）
                p_dir = os.path.join(work, "P")
                p_pkgs = os.path.join(work, "Ppkgs")
                os.makedirs(p_pkgs, exist_ok=True)
                p_plug_zip = put_pkg(p_pkgs, "local-neednat-v1.0.0.zip",
                                     local_plugin_zip("local-neednat", "1.0.0", "natpkg2"))
                p_cat = catalog_fixture()
                p_cat["packages"] += [
                    {"id": "natdep", "runtime": "dep-slot", "name": "原生依赖包", "version": "1.0.0",
                     "requires": {}, "provides": ["natpkg"],
                     **put_pkg(p_pkgs, "natdep-v1.0.0.zip",
                               dep_component_zip("natdep", "1.0.0", provides=("natpkg",)))},
                    {"id": "natdep2", "runtime": "dep-slot", "name": "原生依赖包二", "version": "1.0.0",
                     "requires": {}, "provides": ["natpkg2"],
                     **put_pkg(p_pkgs, "natdep2-v1.0.0.zip",
                               dep_component_zip("natdep2", "1.0.0", provides=("natpkg2",)))},
                    {"id": "local-neednat", "runtime": "local", "name": "需要原生依赖的本地插件",
                     "version": "1.0.0",
                     "requires": {"python_packages": [
                         {"name": "natpkg2", "version": ">=1.0", "required": True,
                          "hint": "缺少 natpkg2：演示用。"}]},
                     **p_plug_zip},
                ]
                srv4, addr4 = stub_server(p_cat, p_pkgs)
                try:
                    p_prog = os.path.join(p_dir, "_program")
                    p_pylibs = os.path.join(p_prog, "runtime", "pylibs")
                    os.makedirs(p_dir, exist_ok=True)
                    _, pc = new_app(p_dir, addr4, static_dir)

                    r = pc.post("/local-api/store/install",
                                json={"id": "natdep", "version": "1.0.0"})
                    j = r.get_json()
                    check("P1 装 DLL 类依赖（真实端点）→ restart_required=true + hint"
                          "（前端据此弹「需要重启」询问）",
                          bool(j.get("ok")) and j.get("restart_required") is True
                          and bool(j.get("hint")),
                          json.dumps(j, ensure_ascii=False)[:200])
                    nat = next((x for x in (pc.get("/local-api/plugins").get_json()
                                            .get("components") or [])
                                if x.get("id") == "natdep"), None)
                    check("P2 未重启期间 components 报 pending=true —— 卡片定格「待重启」"
                          "（用户选「稍后」/刷新页面后仍成立）",
                          bool(nat) and nat.get("pending") is True
                          and nat.get("version") == "1.0.0",
                          json.dumps(nat, ensure_ascii=False))
                    import jz_deps
                    jz_deps.apply_pending_components(p_prog)      # = 用户点「立即重启」后能力层启动
                    nat = next((x for x in (pc.get("/local-api/plugins").get_json()
                                            .get("components") or [])
                                if x.get("id") == "natdep"), None)
                    check("P3 重启（应用暂存）后 pending=false —— 翻「已安装」",
                          bool(nat) and nat.get("pending") is False,
                          json.dumps(nat, ensure_ascii=False))

                    # ④ 升级：catalog 换 v2 → 装完暂存、旧版仍在登记 → 必须报待重启且取新版本
                    srv4.app._bench_catalog = dict(
                        p_cat,
                        packages=[dict(p) for p in p_cat["packages"] if p.get("id") != "natdep"]
                        + [{"id": "natdep", "runtime": "dep-slot", "name": "原生依赖包",
                            "version": "2.0.0", "requires": {}, "provides": ["natpkg"],
                            **put_pkg(p_pkgs, "natdep-v2.0.0.zip",
                                      dep_component_zip("natdep", "2.0.0", provides=("natpkg",)))}])
                    r = pc.post("/local-api/store/install",
                                json={"id": "natdep", "version": "2.0.0"})
                    j = r.get_json()
                    nat = next((x for x in (pc.get("/local-api/plugins").get_json()
                                            .get("components") or [])
                                if x.get("id") == "natdep"), None)
                    check("P4 依赖升级装完待重启 → pending=true 且版本=v2"
                          "（不再显示「已安装」/「升级到 v2.0.0」）",
                          bool(j.get("ok")) and bool(nat) and nat.get("pending") is True
                          and nat.get("version") == "2.0.0",
                          json.dumps({"resp": j, "comp": nat}, ensure_ascii=False)[:240])

                    # ⑤ 联合安装：插件的依赖随插件一起装（DLL 类）→ dep_results[].pending
                    r = pc.post("/local-api/store/install",
                                json={"id": "local-neednat", "version": "1.0.0",
                                      "include_deps": True,
                                      "deps": [{"dep_id": "natdep2", "dep_version": "1.0.0"}]})
                    j = r.get_json()
                    dr = next((x for x in (j.get("dep_results") or [])
                               if x.get("id") == "natdep2"), None)
                    check("P5 联合安装一并装上的 DLL 依赖 → dep_results[].pending=true"
                          "（前端据此把依赖卡片一并定格「待重启」）",
                          bool(dr) and dr.get("ok") is True and dr.get("pending") is True,
                          json.dumps(dr, ensure_ascii=False)[:200])
                    check("P5b 该依赖同时在 components 报 pending=true（页面重载后的持久判据）",
                          any(x.get("id") == "natdep2" and x.get("pending") is True
                              for x in (pc.get("/local-api/plugins").get_json()
                                        .get("components") or [])),
                          str([x.get("id") for x in (pc.get("/local-api/plugins").get_json()
                                                     .get("components") or [])]))
                    check("P5c 依赖载荷真落在暂存区（未重启前不覆盖运行中的 DLL）",
                          os.path.isdir(os.path.join(p_pylibs, ".pending", "natdep2")))
                finally:
                    srv4.shutdown()
            finally:
                if l_srv is not None:
                    l_srv.shutdown()
                srv3.shutdown()
        finally:
            srv2.shutdown()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n== 台架完成：%d 项，失败 %d 项 ==" % (len(RESULTS), len(failed)))
    for n in failed:
        print("  失败：", n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
