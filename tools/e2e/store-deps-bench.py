# -*- coding: utf-8 -*-
r"""依赖卸载状态刷新 + 详情卡「检测本机依赖」台架（2026-10-12 需求①②）。

覆盖：
  A 卸载前：组件清单（runtime/pylibs/manifest.json）声明 openpyxl → 检测=ok；商店状态=已安装
  B 卸载：/local-api/store/uninstall-dep → 目录删 + **清单同步移除**（真机 bug 根因）
  C 卸载后：/local-api/plugins 的 components 不再含该组件（商店卡片翻未安装）
  D 卸载后：检测本机依赖 → missing + resolve=dep-package（映射到可装的依赖包条目）
  E 启动清扫：.pending-remove 残件在下次启动被删，且清单条目同步清（防旧版残留）
  F drop_component_entry 边界：幂等 / 无清单 / 单组件简写形态

用法：python tools\e2e\store-deps-bench.py   （退出码 0=全过）
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading

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
    ]}


def stub_server(catalog):
    from flask import Flask, jsonify, request as fq
    app = Flask("stub-deps")

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
        return jsonify(catalog)

    @app.get("/plugin/store/api/installs")
    def _installs():
        return jsonify({"ok": True, "installs": {}, "updated_at": ""})

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


def new_app(data_root, addr, static_dir):
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
    c = app.test_client()
    c.get("/shell-boot?_shell=deps-bench&next=/")
    c.post("/api/login", json={"username": "bench"})
    return build_cfg, c


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
    finally:
        shutil.rmtree(work, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n== 台架完成：%d 项，失败 %d 项 ==" % (len(RESULTS), len(failed)))
    for n in failed:
        print("  失败：", n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
