# -*- coding: utf-8 -*-
r"""账号级插件安装与本地数据隔离台架（2026-10-11 需求①②③④⑤，plan §4 验收矩阵）。

覆盖（§4 用例表的可自动化子集 + D 决策点回归）：
  A  四象限网格：登记为显示全集（D1）/ 正常卡 / 云卡字段 / 未登记不显示 + 安装登记写链
     （D18 先登记后落盘）+ 卸载=账号级（副本保留为缓存，D3）+ 会话可用性门（D7）
  B  云卡下载闭环：登记有/副本缺 → 灰卡 → 「从云端下载」安装链 → 转正常卡（§4#3）
  C  数据隔离（G3-1/G3-2）：数据根随账号切换（users/<账号>）、hybrid 写数据落账号子根、
     切号后互不可见、登出切 _anonymous（D19）
  D  升级迁移（D2/D11）：遗留 plugins 数据归属首个登录账号、登记补种尊重 hidden_tools、
     hidden 键消费后清除、第二账号空白起步（§4#1/#6）
  E  停用/下架组合（D22，§4#11–13）：服务器停用 → 已装账号网格不显示 + 安装门拒绝；
     重启用自动恢复；下架（catalog 撤投影）不影响已装者
  F  离线快照（G3-5/D15）+ 降级兼容（G2-7，§4#8）：服务器不可达按快照显示 + 数据根保持；
     旧服务器（无 installs API）→ 机器级行为

双账号造法（§5.1 注意 4）：stub 上游持账号态，经 /api/login 换 Cookie 切账号；
每部独立数据根（JZCLIENT 测试隔离纪律，§5.1 注意 6），不触真实数据根。

用法：python tools\e2e\account-store-tests.py   （退出码 0=全过）
"""

import hashlib
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

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %s%s" % ("[OK]" if ok else "[!!]", name, ("  — " + detail) if detail else ""))


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            zf.writestr(name, data)
    buf.seek(0)
    return buf


def put_pkg(packages_dir, fname, body):
    with open(os.path.join(packages_dir, fname), "wb") as f:
        f.write(body)
    return {"file": fname, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}


def build_store_fixture(store_root):
    """商店夹具：local-foo（B 类纯本地）+ svc-acc（A 类前端包）真实落 packages 目录。"""
    packages_dir = os.path.join(store_root, "packages")
    os.makedirs(packages_dir, exist_ok=True)
    foo_body = make_zip({
        "manifest.json": json.dumps({"id": "local-foo", "runtime": "local", "kind": "local",
                                     "version": "1.0.0", "name": "本地示例", "icon": "🧩",
                                     "entry": {"frontend": "index.html"}}),
        "frontend/index.html": "<h1>local-foo</h1>"}).getvalue()
    foo_meta = put_pkg(packages_dir, "JZClient-本地插件-local-foo-v1.0.0.zip", foo_body)
    fe_body = make_zip({"frontend/index.html": "<h1>svc-acc</h1>",
                        "frontend-manifest.json": json.dumps(
                            {"id": "svc-acc", "version": "2.0.0", "files": ["index.html"]})}).getvalue()
    fe_meta = put_pkg(packages_dir, "JZToolsHub-svc-acc-前端-v2.0.0.zip", fe_body)
    catalog = {"schema": 2, "generated_at": "", "packages": [
        {"id": "local-foo", "name": "本地示例", "description": "B 类纯本地", "icon": "🧩",
         "runtime": "local", "deps_mode": "shared", "requires": {}, **foo_meta,
         "version": "1.0.0", "api_version": 1, "min_app_version": "",
         "requires_restart": False, "changelog": "", "published_at": "2026-10-11T10:00:00"},
        {"id": "svc-acc", "name": "服务插件", "description": "A 类在线", "icon": "☁",
         "runtime": "server", "deps_mode": "shared", "requires": {}, **fe_meta,
         "frontend_file": fe_meta["file"], "frontend_sha256": fe_meta["sha256"],
         "frontend_size": fe_meta["size"],
         "version": "2.0.0", "file": "backend.zip", "sha256": "", "size": 0,
         "api_version": 1, "min_app_version": "", "requires_restart": False,
         "changelog": "", "published_at": "2026-10-11T10:00:00"},
    ]}
    return catalog


HYBRID_BACKEND = (
    "import jztools_data\n"
    "from flask import jsonify\n"
    "\n"
    "def register(app):\n"
    "    @app.post('/write')\n"
    "    def iso_write():\n"
    "        p = jztools_data.get_data_root_file('plugins', 'local-isow', 'mark.txt')\n"
    "        with open(p, 'w', encoding='utf-8') as f:\n"
    "            f.write('x')\n"
    "        return jsonify({'ok': True, 'path': p})\n"
)


def make_hybrid_plugin(lp_dir):
    """隔离探针插件（hybrid）：经 jztools_data 写数据——落点应随账号数据根。"""
    d = os.path.join(lp_dir, "local-isow")
    os.makedirs(os.path.join(d, "backend"), exist_ok=True)
    os.makedirs(os.path.join(d, "frontend"), exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "local-isow", "name": "隔离探针", "version": "1.0.0",
                   "runtime": "local", "kind": "hybrid",
                   "entry": {"frontend": "index.html", "backend": "routes.py",
                             "mount": "/api/local/local-isow"}}, f, ensure_ascii=False)
    with open(os.path.join(d, "backend", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(d, "backend", "routes.py"), "w", encoding="utf-8") as f:
        f.write(HYBRID_BACKEND)
    with open(os.path.join(d, "frontend", "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>isow</html>")


class StubUpstream:
    """stub 服务器：账号态（Cookie 选人）+ installs API + 可清单（停用模拟）。"""

    def __init__(self, catalog, packages_dir, with_installs=True):
        from flask import Flask, jsonify, request as fq
        self.user = None            # 由 /api/login 写
        self.tools = []             # 服务器可见清单（停用模拟：从中摘除 id）
        self.installs = {}          # username → {id: rec}
        self.catalog = catalog
        self.packages_dir = packages_dir
        self.with_installs = with_installs
        app = Flask("stub-account")

        @app.post("/api/login")
        def _login():
            body = fq.get_json(force=True, silent=True) or {}
            self.user = str(body.get("username") or "")
            resp = jsonify({"ok": True})
            if self.user:
                resp.set_cookie("stub_user", self.user, path="/")
            return resp

        @app.post("/api/logout")
        def _logout():
            # 401 = 会话失效统一信号；同时删 Cookie（真机登出由服务端清会话 Cookie 完成，
            # 否则测试客户端仍持 stub_user 会被 /api/session 重新认回已登录）
            resp = jsonify({"ok": True})
            resp.delete_cookie("stub_user", path="/")
            return resp, 401

        @app.get("/api/session")
        def _session():
            u = fq.cookies.get("stub_user") or ""
            return jsonify({"user": {"username": u} if u else None})

        @app.get("/api/tools")
        def _tools():
            return jsonify({"tools": list(self.tools),
                            "categories": [{"id": "office", "name": "办公", "enabled": True}]})

        @app.get("/plugin/store/api/catalog")
        def _catalog():
            return jsonify(self.catalog)

        @app.get("/plugin/store/api/download/<pid>/<ver>")
        def _download(pid, ver):
            for p in self.catalog["packages"]:
                if p["id"] == pid:
                    fname = p["file"]
                    if fq.args.get("artifact") == "frontend" and p.get("frontend_file"):
                        fname = p["frontend_file"]
                    body = open(os.path.join(self.packages_dir, fname), "rb").read()
                    return app.response_class(body, 200)
            return jsonify({"error": "no package"}), 404

        if with_installs:
            @app.get("/plugin/store/api/installs")
            def _installs_get():
                u = fq.cookies.get("stub_user") or ""
                if not u:
                    return jsonify({"error": "未登录"}), 401
                return jsonify({"ok": True, "installs": dict(self.installs.get(u) or {}),
                                "updated_at": ""})

            @app.post("/plugin/store/api/installs")
            def _installs_post():
                u = fq.cookies.get("stub_user") or ""
                if not u:
                    return jsonify({"error": "未登录"}), 401
                body = fq.get_json(force=True, silent=True) or {}
                action = body.get("action")
                ids = [str(x) for x in (body.get("ids") or [body.get("id")]) if x]
                mine = dict(self.installs.get(u) or {})
                for pid in ids:
                    if action == "install":
                        mine[pid] = {"version": str(body.get("version") or ""),
                                     "installed_at": "", "enabled": True,
                                     "source": str(body.get("source") or "store")}
                    elif action == "uninstall":
                        mine.pop(pid, None)
                    elif action in ("enable", "disable") and pid in mine:
                        mine[pid] = dict(mine[pid], enabled=(action == "enable"))
                if mine:
                    self.installs[u] = mine
                else:
                    self.installs.pop(u, None)
                return jsonify({"ok": True, "installs": dict(self.installs.get(u) or {})})

        self.app = app
        self.server = None

    def start(self):
        from werkzeug.serving import make_server
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.server.server_port

    def stop(self):
        try:
            self.server.shutdown()
        except Exception:
            pass


def new_app(data_root, addr, static_dir):
    """起一个能力层实例（每部独立数据根；同部多实例共享数据目录=模拟重启/换机）。

    JZCLIENT_DATA_DIR 必须与 data_root 同源：能力层内部所有 client config 读写
    （hidden_tools/local_state 等）走 client_config.data_dir(build_cfg)，
    不设该 env 会落到真实用户目录（测试隔离纪律，§5.1 注意 6）。"""
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
        data_dir=data_root, log=lambda m: None, shell_token="acct-bench")
    c = app.test_client()
    c.get("/shell-boot?_shell=acct-bench&next=/")     # 壳自证（本机操作面门槛）
    return app, c


def login(c, name):
    r = c.post("/api/login", json={"username": name})
    assert r.status_code == 200, r.status_code


def tool_ids(c):
    d = c.get("/api/tools").get_json()
    return d, [t["id"] for t in (d.get("tools") or [])]


# ============================================================

def part_a(work):
    print("==> A. 四象限网格 + 登记写链 + 卸载账号级 + 可用性门（§4#1/#2/#4/#9 部分）")
    from flask import Flask
    data_root = os.path.join(work, "A")
    lp_dir = os.path.join(data_root, "local-plugins")
    os.makedirs(lp_dir, exist_ok=True)
    static_dir = os.path.join(work, "static")
    os.makedirs(static_dir, exist_ok=True)
    with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>home</html>")
    shutil.copyfile(os.path.join(ROOT, "static", "tool.html"),
                    os.path.join(static_dir, "tool.html"))
    catalog = build_store_fixture(os.path.join(work, "A_store"))
    stub = StubUpstream(catalog, os.path.join(work, "A_store", "packages"))
    stub.tools = [{"id": "remote-one", "name": "远程插件", "order": 1},
                  {"id": "svc-acc", "name": "服务插件", "order": 2}]
    addr = stub.start()
    try:
        app, c = new_app(data_root, addr, static_dir)
        login(c, "A")
        stub.installs["A"] = {}                     # 登记空 → 严格空网格（D1）

        d, ids = tool_ids(c)
        check("A1 登记为显示全集：远程条目不再直出（D1/需求①）",
              ids == [] and d.get("account_mode") is True, str(ids))

        r = c.post("/local-api/store/install", json={"id": "local-foo", "version": "1.0.0"})
        check("A2 安装成功且登记先写（D18）",
              r.get_json().get("ok") and "local-foo" in (stub.installs.get("A") or {}),
              str(r.get_json()))
        d, ids = tool_ids(c)
        foo = next((t for t in d["tools"] if t["id"] == "local-foo"), None)
        check("A2 正常卡（登记∧副本在 → 无云标记，§4#2）",
              foo is not None and not foo.get("cloud"), str(foo and foo.get("cloud")))

        r = c.get("/tool/local-foo")
        check("A3 已登记插件壳页可达", r.status_code == 200)
        r = c.get("/plugin/local-foo/index.html")
        check("A3 本地资产直出可达", r.status_code == 200 and b"local-foo" in r.data)

        # 账号级卸载：登记移除 + 副本保留为缓存（D3/需求④无「已从本机移除」）
        r = c.post("/local-api/plugins/uninstall", json={"id": "local-foo", "purge_data": False})
        check("A4 卸载成功（登记移除）",
              r.get_json().get("ok") and r.get_json().get("copy_kept") is True, str(r.get_json()))
        check("A4 机器副本保留为缓存（D3）", os.path.isdir(os.path.join(lp_dir, "local-foo")))
        d, ids = tool_ids(c)
        check("A4 卸载后网格不显示（需求①）", "local-foo" not in ids, str(ids))
        r = c.get("/tool/local-foo")
        body = r.data.decode("utf-8", "replace")
        check("A5 未登记壳页 404 + 可读提示（D7）",
              r.status_code == 404 and "未安装" in body and "返回主界面" in body,
              "status=%s" % r.status_code)
        r = c.get("/api/local/local-foo")
        check("A5 未登记本地 API 404（本会话隔离，§4#4）", r.status_code == 404)

        # A 类：登记 + 前端包装 + 停用求交（D22 规则 1 的网格面）
        r = c.post("/local-api/store/install", json={"id": "svc-acc", "version": "2.0.0"})
        check("A6 A 类安装成功（前端包+登记）",
              r.get_json().get("ok") and "svc-acc" in (stub.installs.get("A") or {}),
              str(r.get_json()))
        d, ids = tool_ids(c)
        check("A6 A 类正常卡在网格", "svc-acc" in ids, str(ids))

        # §4#1：A 卸 X → B 登录不显示 X（副本仍在=其他账号缓存）
        c.post("/local-api/plugins/uninstall", json={"id": "svc-acc"})
        login(c, "B")                                # B 无登记
        d, ids = tool_ids(c)
        check("A7 B 登录：网格不含 A 卸载的插件与远程条目（需求①病灶回归）",
              ids == [], str(ids))
        check("A7 B 登录后数据根切 users/B（G3-1）",
              os.environ.get("JZTOOLS_DATA_ROOT", "").lower()
              == os.path.join(data_root, "users", "B").lower().replace("/", os.sep)
              or os.path.normpath(os.environ.get("JZTOOLS_DATA_ROOT", ""))
              == os.path.normpath(os.path.join(data_root, "users", "B")),
              os.environ.get("JZTOOLS_DATA_ROOT"))
    finally:
        stub.stop()


def part_b(work):
    print("==> B. 云卡下载闭环（§4#3）")
    data_root = os.path.join(work, "B")
    lp_dir = os.path.join(data_root, "local-plugins")
    os.makedirs(lp_dir, exist_ok=True)
    static_dir = os.path.join(work, "B_static")
    os.makedirs(static_dir, exist_ok=True)
    with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>home</html>")
    catalog = build_store_fixture(os.path.join(work, "B_store"))
    stub = StubUpstream(catalog, os.path.join(work, "B_store", "packages"))
    stub.tools = [{"id": "svc-acc", "name": "服务插件", "order": 1}]
    addr = stub.start()
    try:
        # 预置：登记有 local-foo、本机无副本（模拟换机，§4#3）
        stub.installs["A"] = {"local-foo": {"version": "1.0.0", "installed_at": "",
                                            "enabled": True, "source": "migrated"}}
        app, c = new_app(data_root, addr, static_dir)
        login(c, "A")
        d, ids = tool_ids(c)
        foo = next((t for t in d["tools"] if t["id"] == "local-foo"), None)
        check("B1 登记∧副本缺 → 云卡字段齐备（cloud/machine_present/account_installed）",
              foo is not None and foo.get("cloud") is True
              and foo.get("machine_present") is False and foo.get("account_installed") is True
              and foo.get("name") == "本地示例",       # 元数据来自商店 flat
              str(foo))
        check("B1 登记无、副本无 → 不显示（D1）", "svc-acc" not in ids, str(ids))

        r = c.post("/local-api/store/install", json={"id": "local-foo"})
        check("B2 云卡「从云端下载」安装链成功（D21③取 catalog 最新版）",
              r.get_json().get("ok"), str(r.get_json()))
        d, ids = tool_ids(c)
        foo = next((t for t in d["tools"] if t["id"] == "local-foo"), None)
        check("B2 安装后转正常卡", foo is not None and not foo.get("cloud"), str(foo))
        check("B2 副本已落位", os.path.isdir(os.path.join(lp_dir, "local-foo")))

        # A 类云卡：登记有、前端包缺、服务器可见 → 云卡；下架（catalog 无投影）→ delisted 标注
        stub.installs["A"]["svc-acc"] = {"version": "2.0.0", "installed_at": "",
                                         "enabled": True, "source": "store"}
        app2, c2 = new_app(data_root, addr, static_dir)     # 新实例=强制刷新登记
        login(c2, "A")
        d, ids = tool_ids(c2)
        sc = next((t for t in d["tools"] if t["id"] == "svc-acc"), None)
        check("B3 A 类云卡（登记∧副本缺∧服务器可见，D22 规则 2）",
              sc is not None and sc.get("cloud") is True, str(sc))
        c2.post("/local-api/store/install", json={"id": "svc-acc", "version": "2.0.0"})
        d, _ = tool_ids(c2)
        sc = next((t for t in d["tools"] if t["id"] == "svc-acc"), None)
        check("B3 A 类云卡下载后转正常卡", sc is not None and not sc.get("cloud"), str(sc))
    finally:
        stub.stop()


def part_c(work):
    print("==> C. 数据按账号隔离（G3-1/G3-2，§4#5）")
    data_root = os.path.join(work, "C")
    lp_dir = os.path.join(data_root, "local-plugins")
    os.makedirs(lp_dir, exist_ok=True)
    make_hybrid_plugin(lp_dir)
    static_dir = os.path.join(work, "C_static")
    os.makedirs(static_dir, exist_ok=True)
    with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>home</html>")
    catalog = build_store_fixture(os.path.join(work, "C_store"))
    stub = StubUpstream(catalog, os.path.join(work, "C_store", "packages"))
    stub.tools = []
    addr = stub.start()
    rec = lambda: {"version": "1.0.0", "installed_at": "", "enabled": True, "source": "migrated"}
    try:
        stub.installs = {"A": {"local-isow": rec()}, "B": {"local-isow": rec()}}
        app, c = new_app(data_root, addr, static_dir)
        login(c, "A")
        d, ids = tool_ids(c)
        check("C1 A 网格出现登记的 hybrid 探针", "local-isow" in ids, str(ids))
        r = c.post("/api/local/local-isow/write")
        p = (r.get_json() or {}).get("path", "")
        check("C2 A 写数据落 users/A/plugins/<id>（hybrid env 现读）",
              r.status_code == 200 and os.path.isfile(os.path.join(
                  data_root, "users", "A", "plugins", "local-isow", "mark.txt")), p)

        login(c, "B")                                # 切号（同实例）
        d, ids = tool_ids(c)
        check("C3 切号 B 后探针仍在网格（B 也登记）", "local-isow" in ids, str(ids))
        r = c.post("/api/local/local-isow/write")
        check("C3 B 写数据落 users/B/plugins/<id>（互不可见）",
              r.status_code == 200 and os.path.isfile(os.path.join(
                  data_root, "users", "B", "plugins", "local-isow", "mark.txt")),
              str(r.get_json()))
        check("C3 A 的数据未被 B 触碰",
              os.path.isfile(os.path.join(data_root, "users", "A", "plugins",
                                          "local-isow", "mark.txt")))

        r = c.post("/api/logout")                    # 登出（stub 401 → 镜像清空 → 账号清空）
        check("C4 登出后数据根切 _anonymous（D19）",
              os.path.normpath(os.environ.get("JZTOOLS_DATA_ROOT", ""))
              == os.path.normpath(os.path.join(data_root, "users", "_anonymous")),
              os.environ.get("JZTOOLS_DATA_ROOT"))
        d, ids = tool_ids(c)
        check("C4 登出后网格空（快照保留不展示，D19）", ids == [], str(ids))
        check("C4 离线快照文件已落盘（G2-1）",
              os.path.isfile(os.path.join(data_root, "account-state.json")))
    finally:
        stub.stop()


def part_d(work):
    print("==> D. 升级迁移：数据归属 + 登记补种（D2/D11，§4#1/#6）")
    data_root = os.path.join(work, "D")
    lp_dir = os.path.join(data_root, "local-plugins")
    os.makedirs(os.path.join(lp_dir, "local-foo"), exist_ok=True)   # 遗留本机副本（B 类）
    with open(os.path.join(lp_dir, "local-foo", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "local-foo", "runtime": "local", "kind": "local",
                   "version": "1.0.0", "entry": {"frontend": "index.html"}}, f)
    make_hybrid_plugin(lp_dir)
    os.makedirs(os.path.join(data_root, "plugins", "local-isow"), exist_ok=True)  # 遗留数据
    with open(os.path.join(data_root, "plugins", "local-isow", "old.txt"), "w",
              encoding="utf-8") as f:
        f.write("legacy")
    fe_dir = os.path.join(data_root, "server-frontends", "svc-acc")
    os.makedirs(fe_dir, exist_ok=True)                              # 遗留前端副本（A 类）
    with open(os.path.join(fe_dir, "frontend-manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "svc-acc", "version": "2.0.0", "files": ["index.html"]}, f)
    with open(os.path.join(fe_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write("<h1>svc</h1>")
    cfg_path = os.path.join(data_root, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:                # 遗留 hidden（不补种）
        json.dump({"hidden_tools": ["local-foo"]}, f)
    static_dir = os.path.join(work, "D_static")
    os.makedirs(static_dir, exist_ok=True)
    with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>home</html>")
    catalog = build_store_fixture(os.path.join(work, "D_store"))
    stub = StubUpstream(catalog, os.path.join(work, "D_store", "packages"))
    stub.tools = [{"id": "svc-acc", "name": "服务插件", "order": 1}]
    addr = stub.start()
    try:
        app, c = new_app(data_root, addr, static_dir)
        login(c, "first")                            # 首个登录账号
        d, ids = tool_ids(c)
        inst = stub.installs.get("first") or {}
        check("D1 遗留数据 move 到首个登录账号（D2）",
              os.path.isfile(os.path.join(data_root, "users", "first", "plugins",
                                          "local-isow", "old.txt"))
              and not os.path.isdir(os.path.join(data_root, "plugins")))
        check("D2 登记补种：本机副本进登记（local-isow/svc-acc），hidden 尊重不补种（D11）",
              "local-isow" in inst and "svc-acc" in inst and "local-foo" not in inst,
              str(sorted(inst)))
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        check("D2 hidden_tools 键消费后清除（§2.4）", "hidden_tools" not in cfg, str(cfg))
        check("D2 补种/迁移标记文件就位（幂等）",
              os.path.isfile(os.path.join(data_root, "users", ".legacy-seeded"))
              and os.path.isfile(os.path.join(data_root, "users", ".legacy-migrated")))
        d, ids = tool_ids(c)
        check("D2 补种后网格=登记全集（hybrid+前端副本）",
              sorted(ids) == ["local-isow", "svc-acc"], str(ids))

        login(c, "second")                           # 第二账号：空白起步（D1/D13）
        d, ids = tool_ids(c)
        check("D3 第二账号网格空（不白捡机器副本）", ids == [], str(ids))
        check("D3 第二账号无遗留数据（空白起步）",
              not os.path.isdir(os.path.join(data_root, "users", "second", "plugins",
                                             "local-isow")))
        check("D3 first 账号数据仍在",
              os.path.isfile(os.path.join(data_root, "users", "first", "plugins",
                                          "local-isow", "old.txt")))
    finally:
        stub.stop()


def part_e(work):
    print("==> E. 停用/下架组合口径（D22，§4#11–13）")
    data_root = os.path.join(work, "E")
    lp_dir = os.path.join(data_root, "local-plugins")
    os.makedirs(lp_dir, exist_ok=True)
    static_dir = os.path.join(work, "E_static")
    os.makedirs(static_dir, exist_ok=True)
    with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>home</html>")
    catalog = build_store_fixture(os.path.join(work, "E_store"))
    stub = StubUpstream(catalog, os.path.join(work, "E_store", "packages"))
    stub.tools = [{"id": "svc-acc", "name": "服务插件", "order": 1}]
    addr = stub.start()
    try:
        app, c = new_app(data_root, addr, static_dir)
        login(c, "A")
        check("E1 A 类安装成功（服务器可见时）",
              c.post("/local-api/store/install",
                     json={"id": "svc-acc", "version": "2.0.0"}).get_json().get("ok"))
        # 服务器停用：从可见清单摘除（全局下线优先于账号登记，不做已装豁免）
        stub.tools = []
        app2, c2 = new_app(data_root, addr, static_dir)
        login(c2, "A")
        d, ids = tool_ids(c2)
        check("E2 停用 × 已装（副本在）：网格不显示（D22 规则 1，§4#11）",
              "svc-acc" not in ids, str(ids))
        check("E2 本机副本保留", os.path.isdir(os.path.join(data_root, "server-frontends",
                                                           "svc-acc")))
        r = c2.post("/local-api/store/preflight", json={"id": "svc-acc", "version": "2.0.0"})
        d = r.get_json()
        check("E3 停用 × 安装门拒绝 + 可读提示（D22 规则 3，§4#12）",
              d.get("installable") is False and d.get("server_disabled") is True
              and "停用" in d.get("error", ""), str(d))
        r = c2.post("/local-api/store/install", json={"id": "svc-acc", "version": "2.0.0"})
        d = r.get_json()
        check("E3 安装门拒绝安装请求", not d.get("ok") and "停用" in d.get("error", ""), str(d))
        d, ids = tool_ids(c2)
        check("E3 A 类云卡不合成（D22 规则 2，§4#12）",
              not any(t.get("cloud") and t["id"] == "svc-acc" for t in d["tools"]), str(ids))

        # 管理员重新启用：已装账号自动恢复显示（无需重下，§4#11）
        stub.tools = [{"id": "svc-acc", "name": "服务插件", "order": 1}]
        app3, c3 = new_app(data_root, addr, static_dir)
        login(c3, "A")
        d, ids = tool_ids(c3)
        sc = next((t for t in d["tools"] if t["id"] == "svc-acc"), None)
        check("E4 重新启用 → 已装账号自动恢复（无需重下）",
              sc is not None and not sc.get("cloud"), str(sc))

        # 账号停用/启用随账号（D4，§4#10）：A 停用 → A 网格无；B 不受影响
        c3.post("/plugin/store/api/installs", json={"action": "disable", "id": "svc-acc"})
        app4, c4 = new_app(data_root, addr, static_dir)
        login(c4, "A")
        d, ids = tool_ids(c4)
        check("E5 A 停用 → A 网格无（enabled=false）", "svc-acc" not in ids, str(ids))
        r = c4.get("/tool/svc-acc")
        check("E5 账号停用后壳页 404（已被停用，D7）",
              r.status_code == 404 and "停用" in r.data.decode("utf-8", "replace"))
        login(c4, "B")
        stub.installs.setdefault("B", {})["svc-acc"] = {
            "version": "2.0.0", "installed_at": "", "enabled": True, "source": "store"}
        app5, c5 = new_app(data_root, addr, static_dir)
        login(c5, "B")
        d, ids = tool_ids(c5)
        sc = next((t for t in d["tools"] if t["id"] == "svc-acc"), None)
        check("E5 B 不受 A 停用影响（副本共享缓存 → 正常卡，D3 秒装语义，§4#10）",
              sc is not None and not sc.get("cloud"), str(sc))

        # 下架 × 已装（副本在）→ 照常可用（D21①，§4#13）；catalog 撤投影在网格面无感
        # （A 类元数据来自 /api/tools 而非 catalog）——E4 已覆盖正常卡；
        # 下架 × 未装 → 商店不可见（catalog 无 published 投影，服务器侧投影行为，
        # 由 store-batch8 B5 覆盖），此处覆盖 B/C 云卡下架标注：
        stub.installs["B"]["local-gone"] = {"version": "1.0.0", "installed_at": "",
                                            "enabled": True, "source": "migrated"}
        app6, c6 = new_app(data_root, addr, static_dir)
        login(c6, "B")
        d, ids = tool_ids(c6)
        g = next((t for t in d["tools"] if t["id"] == "local-gone"), None)
        check("E6 B/C 下架（catalog 无投影）∧副本缺 → 云卡降级 + delisted 标注（D21①）",
              g is not None and g.get("cloud") is True and g.get("delisted") is True
              and g.get("name") == "local-gone",      # 元数据降级=登记记录
              str(g))
    finally:
        stub.stop()


def part_f(work):
    print("==> F. 离线快照（G3-5/D15）+ 降级兼容（G2-7，§4#7/#8）")
    data_root = os.path.join(work, "F")
    lp_dir = os.path.join(data_root, "local-plugins")
    os.makedirs(lp_dir, exist_ok=True)
    make_hybrid_plugin(lp_dir)
    static_dir = os.path.join(work, "F_static")
    os.makedirs(static_dir, exist_ok=True)
    with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>home</html>")
    catalog = build_store_fixture(os.path.join(work, "F_store"))
    stub = StubUpstream(catalog, os.path.join(work, "F_store", "packages"))
    stub.tools = []
    addr = stub.start()
    try:
        stub.installs["A"] = {"local-isow": {"version": "1.0.0", "installed_at": "",
                                             "enabled": True, "source": "migrated"}}
        app, c = new_app(data_root, addr, static_dir)
        login(c, "A")
        d, ids = tool_ids(c)                          # 触发登记拉取 + 快照落盘
        check("F1 快照文件已写（账号+installs+ts）",
              os.path.isfile(os.path.join(data_root, "account-state.json")))
        stub.stop()                                   # 断服务器

        app2, c2 = new_app(data_root, "http://127.0.0.1:1", static_dir)   # 离线实例
        # 不调用 login（服务器不可达，/api/login 502）：离线语义靠启动期快照恢复（G3-5）
        d2, ids2 = tool_ids(c2)
        check("F2 离线：按快照显示（登记∧副本在 → 正常卡，§4#7）",
              "local-isow" in ids2, str(ids2))
        check("F2 离线：degraded 标记", d2.get("degraded") is True)
        check("F2 离线：数据根仍指向快照账号（G3-5）",
              os.path.normpath(os.environ.get("JZTOOLS_DATA_ROOT", ""))
              == os.path.normpath(os.path.join(data_root, "users", "A")),
              os.environ.get("JZTOOLS_DATA_ROOT"))
    finally:
        pass

    # ---- G2-7：旧服务器（无 installs API）→ 机器级行为 ----
    data_root2 = os.path.join(work, "F2")
    lp_dir2 = os.path.join(data_root2, "local-plugins")
    os.makedirs(lp_dir2, exist_ok=True)
    make_hybrid_plugin(lp_dir2)
    stub2 = StubUpstream(catalog, os.path.join(work, "F_store", "packages"),
                         with_installs=False)
    stub2.tools = [{"id": "remote-one", "name": "远程插件", "order": 1}]
    addr2 = stub2.start()
    try:
        app3, c3 = new_app(data_root2, addr2, static_dir)
        login(c3, "A")
        d, ids = tool_ids(c3)
        check("F3 旧服务器：机器级行为（本机副本驱动网格，§4#8）",
              "local-isow" in ids and "remote-one" in ids and not d.get("account_mode"),
              str(ids))
        r = c3.post("/local-api/store/install", json={"id": "local-foo", "version": "1.0.0"})
        check("F3 旧服务器：安装照旧可用（不写登记不报错）",
              r.get_json().get("ok") and os.path.isdir(os.path.join(lp_dir2, "local-foo")),
              str(r.get_json()))
        r = c3.post("/local-api/plugins/uninstall", json={"id": "local-foo"})
        check("F3 旧服务器：卸载=删副本（legacy 语义保留）",
              r.get_json().get("ok") and not os.path.isdir(os.path.join(lp_dir2, "local-foo")),
              str(r.get_json()))
    finally:
        stub2.stop()


def main():
    work = tempfile.mkdtemp(prefix="jz_account_store_")
    print("工作目录：%s" % work)
    try:
        part_a(work)
        part_b(work)
        part_c(work)
        part_d(work)
        part_e(work)
        part_f(work)
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
