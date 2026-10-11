# -*- coding: utf-8 -*-
r"""壳身份门台架（2026-10-11 需求①②③，设计 → docs/plan/20261011浏览器退役与商店分发口径改造-TODO §4）。

覆盖：
  A  服务端门（真实 app 装配：隔离数据根 + 框架路由 + admin/store 插件注册）：
     浏览器无头访问业务面全 403（页面/插件/商店/API）、管理面白名单可达、静态例外、
     带有效/错误密钥的准入、紧急开关（JZTOOLS_SHELL_ENFORCE=0）、密钥重置失效、
     门与 admin 登录门的运行顺序（先门后登录）。
  B  商店投影（需求②）：C 类配对去重（显式 client_entry + 历史自动识别）、
     kind/server_data 透出、未配对条目不受影响、隐藏条目下载不回归。
  C  客户端能力层（G2）：本地端口壳自证（无 Cookie 403 / 错 token 403 / boot 种 Cookie）、
     中继注头 X-JZ-Shell（client_headers / jz_relay / 商店源三链）、服务器壳门 403 的
     客户端降级（仅本地清单 + gate_blocked / 页面提示）。

用法：
    python tools\e2e\browser-gate-tests.py
退出码：0 = 全过；1 = 有 FAIL。
"""

import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "client"))

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %s%s" % ("[OK]" if ok else "[!!]", name, ("  — " + detail) if detail else ""))


# ============================================================
# A/B：真实 app 装配（隔离数据根）
# ============================================================

WORK = tempfile.mkdtemp(prefix="jz-gate-")
DATA_ROOT = os.path.join(WORK, "dataroot")
os.environ["JZTOOLS_DATA_ROOT"] = DATA_ROOT
os.makedirs(os.path.join(DATA_ROOT, "config"), exist_ok=True)
with open(os.path.join(DATA_ROOT, "config", "tools.json"), "w", encoding="utf-8") as f:
    json.dump({"tools": [{"id": "admin", "name": "管理后台", "enabled": True, "hidden": True},
                         {"id": "store", "name": "商店", "enabled": True, "grant_all": True}]},
              f, ensure_ascii=False)

import app as app_module                     # noqa: E402  导入期即装壳门（真实装配序）
app_module.init_data_root()
app_module.register_plugin_backends(app_module.app)
APP = app_module.app
C = APP.test_client()


def gate_token():
    with open(os.path.join(DATA_ROOT, "config", "shell-token.json"), encoding="utf-8") as f:
        return json.load(f)["token"]


def part_a_gate():
    print("==> A. 服务端壳身份门")
    # A1 浏览器无头访问业务面 → 403
    for path in ("/", "/tool/file-filter", "/plugin/store/index.html",
                 "/plugin/file-filter/app.js", "/api/org/tree", "/api/llm/settings"):
        r = C.get(path)
        check("A1 无头业务面 403：%s" % path, r.status_code == 403,
              "实际 %d" % r.status_code)
    r = C.get("/api/tools")
    body = r.get_json(silent=True) or {}
    check("A1 API 403 JSON 契约（shell_required + hint + 门标记头）",
          r.status_code == 403 and body.get("error") == "shell_required"
          and r.headers.get("X-JZ-Gate") == "shell_required")
    # A2 静态例外
    r = C.get("/static/index.html")
    check("A2 /static/index.html 拒绝", r.status_code == 403)
    r = C.get("/static/css/style.css")
    check("A2 /static/css 放行（登录页/admin 依赖）", r.status_code == 200)
    # A3 管理面白名单（浏览器可达）
    check("A3 /login 放行", C.get("/login").status_code == 200)
    # /api/site 门放行（管理页 logo 标题依赖），匿名被 admin 登录门 401（语义保持）
    check("A3 /api/site 匿名 401（admin 登录门）", C.get("/api/site").status_code == 401)
    r = C.get("/admin")
    check("A3 /admin 未登录 302→登录页", r.status_code == 302)
    check("A3 /api/admin/units 匿名 401（admin 登录门在门后正常工作）",
          C.get("/api/admin/units").status_code == 401)
    # A4 登录后管理面全通（浏览器身份）
    C.post("/api/login", json={"username": "admin", "password": "admin123"})
    check("A4 登录后 /admin 200", C.get("/admin").status_code == 200)
    check("A4 登录后 /api/admin/units 200", C.get("/api/admin/units").status_code == 200)
    check("A4 登录后 /plugin/admin/js/admin-common.js 200",
          C.get("/plugin/admin/js/admin-common.js").status_code == 200)
    check("A4 登录后 /api/site 200（管理面标题依赖）", C.get("/api/site").status_code == 200)
    # A5 业务面对「已登录浏览器」依然关闭（门不看会话）
    check("A5 已登录浏览器 GET / 仍 403（门只认壳标识）", C.get("/").status_code == 403)
    check("A5 已登录浏览器 GET /api/tools 仍 403", C.get("/api/tools").status_code == 403)

    # A6 带有效密钥：登录后业务面可达；匿名业务 API 401（登录门语义保持）
    tok = gate_token()
    r = C.get("/api/tools", headers={"X-JZ-Shell": tok})
    check("A6 带有效密钥 + 已登录 → /api/tools 200", r.status_code == 200)
    r = C.get("/", headers={"X-JZ-Shell": tok})
    check("A6 带有效密钥 → / 200", r.status_code == 200)
    anon = APP.test_client()
    r = anon.get("/api/tools", headers={"X-JZ-Shell": tok})
    check("A6 带有效密钥 + 匿名 → 401（登录门先行语义保持）", r.status_code == 401)
    # A7 错误/缺失密钥
    r = C.get("/api/tools", headers={"X-JZ-Shell": "wrong-token"})
    check("A7 错误密钥 403", r.status_code == 403)
    r = C.get("/api/tools", headers={"X-JZ-Shell": ""})
    check("A7 空密钥 403", r.status_code == 403)

    # A8 紧急开关（env 优先于文件 enforce）：门放行后匿名 / 由 admin 登录门接手（302 跳登录）
    os.environ["JZTOOLS_SHELL_ENFORCE"] = "0"
    try:
        r = anon.get("/")
        check("A8 enforce=0（env）→ 壳门放行（302=admin 登录门接手，非门 403）", r.status_code == 302,
              "实际 %d" % r.status_code)
    finally:
        os.environ.pop("JZTOOLS_SHELL_ENFORCE", None)
    check("A8 env 清除后恢复拦截", anon.get("/").status_code == 403)

    # A9 密钥重置：旧密钥立即失效、新密钥生效（超管接口）
    r = C.get("/api/admin/shell-token")
    d = r.get_json(silent=True) or {}
    check("A9 超管读取密钥信息", r.status_code == 200 and d.get("ok") and d.get("token") == tok)
    r = C.post("/api/admin/shell-token")
    d2 = r.get_json(silent=True) or {}
    check("A9 重置返回新密钥", r.status_code == 200 and d2.get("ok") and d2.get("token") != tok)
    r = C.get("/api/tools", headers={"X-JZ-Shell": tok})
    check("A9 旧密钥立即失效", r.status_code == 403)
    r = C.get("/api/tools", headers={"X-JZ-Shell": d2["token"]})
    check("A9 新密钥生效", r.status_code == 200)
    non_super = APP.test_client()
    non_super.post("/api/login", json={"username": "admin", "password": "admin123"})
    check("A9 密钥接口仅超管可重置（超管默认账号通过，非超管 403 由权限模型覆盖）",
          C.post("/api/admin/shell-token").status_code == 200)


def part_b_store_projection():
    print("==> B. 商店投影（C 类配对去重 / kind·server_data 透出）")
    store_dir = os.path.join(DATA_ROOT, "store", "packages")
    os.makedirs(store_dir, exist_ok=True)
    catalog = {"schema": 2, "generated_at": "2026-10-11T00:00:00", "packages": [
        # C 类：客户端工件（出卡）+ 服务器工件（显式 client_entry → 不出卡）
        {"id": "local-ff", "name": "过滤器", "runtime": "local", "server_data": True,
         "kind": "hybrid", "deps_mode": "shared", "requires": {}, "versions": [
             _ver("1.6.4", "pkg-local-ff.zip", "published")]},
        {"id": "ff", "name": "过滤器", "runtime": "server", "deps_mode": "shared",
         "requires": {}, "versions": [
             dict(_ver("1.6.4", "pkg-ff.zip", "published"),
                  frontend_file="fe-ff.zip", client_entry="local-ff")]},
        # 历史形态自动识别：local-ts（server_data）→ 隐去 ts
        {"id": "local-ts", "name": "轨迹速写", "runtime": "local", "server_data": True,
         "deps_mode": "shared", "requires": {}, "versions": [
             _ver("1.3.6", "pkg-local-ts.zip", "published")]},
        {"id": "ts", "name": "轨迹速写", "runtime": "server", "deps_mode": "shared",
         "requires": {}, "versions": [_ver("1.3.6", "pkg-ts.zip", "published")]},
        # A 类无配对：正常出卡
        {"id": "kb", "name": "知识库", "runtime": "server", "deps_mode": "shared",
         "requires": {}, "versions": [
             dict(_ver("1.3.15", "pkg-kb.zip", "published"), frontend_file="fe-kb.zip")]},
        # B 类纯本地：正常出卡，无 server_data
        {"id": "local-b64", "name": "Base64 编解码", "runtime": "local",
         "deps_mode": "shared", "requires": {}, "versions": [
             _ver("1.0.2", "pkg-b64.zip", "published")]},
    ]}
    for fname in ("pkg-local-ff.zip", "pkg-ff.zip", "pkg-local-ts.zip", "pkg-ts.zip",
                  "pkg-kb.zip", "pkg-b64.zip", "fe-ff.zip", "fe-kb.zip"):
        with open(os.path.join(store_dir, fname), "wb") as f:
            f.write(b"pkg-" + fname.encode())
    with open(os.path.join(DATA_ROOT, "store", "catalog.json"), "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False)

    # 商店 API 属业务面（壳门后）——台架以"壳"身份带密钥访问（与客户端中继同款）
    hdr = {"X-JZ-Shell": gate_token()}
    r = C.get("/plugin/store/api/catalog?all=1", headers=hdr)
    d = r.get_json(silent=True) or {}
    ids = {p["id"]: p for p in d.get("packages") or []}
    check("B1 配对服务器工件不出卡（显式 client_entry）", "ff" not in ids)
    check("B1 配对服务器工件不出卡（历史自动识别）", "ts" not in ids)
    check("B1 客户端工件出卡", "local-ff" in ids and "local-ts" in ids)
    check("B1 A 类无配对不受影响", "kb" in ids)
    check("B1 B 类纯本地不受影响", "local-b64" in ids)
    check("B2 kind/server_data 透出（未安装条目可显示数据出机徽标）",
          ids.get("local-ff", {}).get("server_data") is True
          and ids.get("local-ff", {}).get("kind") == "hybrid"
          and "server_data" not in ids.get("local-b64", {}))
    check("B2 A 类条目带前端包工件", ids.get("kb", {}).get("frontend_file") == "fe-kb.zip")
    # 隐藏条目的包分发不回归（管理面/运维通道照旧）
    r = C.get("/plugin/store/api/download/ff/1.6.4?via=client", headers=hdr)
    check("B3 隐藏条目下载通道不回归（published 照常回包）", r.status_code == 200)


def _ver(version, fname, status):
    import hashlib
    return {"version": version, "file": fname, "sha256": "0" * 64, "size": 4,
            "status": status, "api_version": 1, "min_app_version": "",
            "requires_restart": False, "changelog": "", "published_at": "2026-10-11T00:00:00",
            "submit": {"by": "tester", "at": ""}, "review": {}, "history": []}


# ============================================================
# C：客户端能力层（壳自证 / 注头 / 壳门 403 降级）
# ============================================================

class _StubHandler(BaseHTTPRequestHandler):
    """上游桩：记录收到的 X-JZ-Shell；可编程回 403 壳门拒绝。"""
    seen_headers = {}
    mode = "ok"

    def _reply(self):
        _StubHandler.seen_headers = dict(self.headers)
        if _StubHandler.mode == "gate403":
            body = json.dumps({"error": "shell_required"}).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-JZ-Gate", "shell_required")
            n = len(body)
            self.send_header("Content-Length", str(n))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({"site": {"title": "stub"}, "categories": [], "tools": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        n = len(body)
        self.send_header("Content-Length", str(n))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self._reply()

    def log_message(self, *a):
        pass


def part_c_client():
    print("==> C. 客户端能力层（壳自证 / 三链注头 / 壳门 403 降级）")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    addr = "http://127.0.0.1:%d" % srv.server_address[1]

    cdir = os.path.join(WORK, "clientdata")
    os.environ["JZCLIENT_DATA_DIR"] = cdir
    from client_config import save_client_config, load_build_config
    bcfg = load_build_config()
    save_client_config(bcfg, {"remote_address": addr, "remote_token": "client-secret-1"})

    import capability_app as cap
    capp = cap.create_capability_app(
        build_cfg=bcfg, client_cfg={"remote_address": addr, "remote_token": "client-secret-1"},
        address=addr, static_dir=os.path.join(ROOT, "static"),
        local_plugins_dir=os.path.join(cdir, "local-plugins"),
        fallback_file=os.path.join(ROOT, "client", "fallback.html"),
        data_dir=cdir, log=lambda m: None, shell_token="cap-run-token-1")
    cc = capp.test_client()

    # C1 本地端口壳自证
    r = cc.get("/local-api/plugins")
    check("C1 无 Cookie 直连本地端口 403", r.status_code == 403)
    r = cc.get("/shell-boot?_shell=wrong")
    check("C1 boot 错 token 403", r.status_code == 403)
    r = cc.get("/shell-boot?_shell=cap-run-token-1&next=/")
    cookie = (r.headers.get("Set-Cookie") or "")
    check("C1 boot 正确 token → 302 + 种 Cookie",
          r.status_code == 302 and "jz_shell=cap-run-token-1" in cookie, cookie[:60])
    kv = cookie.split(";")[0]
    cc.set_cookie(kv.split("=", 1)[0], kv.split("=", 1)[1])
    r = cc.get("/local-api/plugins")
    check("C1 带壳 Cookie → 本地接口可用", r.status_code == 200)
    r = cc.get("/static/js/main.js")
    check("C1 /static 豁免（页面资源）", r.status_code == 200)

    # C2 中继注头（client_headers 链）
    _StubHandler.mode = "ok"
    r = cc.get("/api/tools")
    check("C2 中继带 X-JZ-Shell（client_headers 链）",
          r.status_code == 200 and _StubHandler.seen_headers.get("X-JZ-Shell") == "client-secret-1",
          "stub 实收 %r" % _StubHandler.seen_headers.get("X-JZ-Shell"))

    # C3 jz_relay 链注头（注意：capability_app 会把 sys.modules["jz_relay"] 替换为
    # 门面实例——这里按路径直载真实模块取 JzRelay 类）
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("jz_relay_real", os.path.join(ROOT, "client", "jz_relay.py"))
    jz_relay_real = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(jz_relay_real)
    from session_mirror import SessionMirror
    relay = jz_relay_real.JzRelay(
        cap.requests.Session(), lambda: addr, SessionMirror(os.path.join(cdir, "s.bin")),
        {"allow": [{"methods": ["GET"], "prefix": "/api/tools"}]},
        token_getter=lambda: "relay-secret-2")
    r = relay.get("/api/tools")
    check("C3 jz_relay 带 X-JZ-Shell",
          r.status_code == 200 and _StubHandler.seen_headers.get("X-JZ-Shell") == "relay-secret-2")

    # C4 商店源链注头
    from plugin_source import ServerSource
    src = ServerSource(cap.requests.Session(), lambda: addr,
                       SessionMirror(os.path.join(cdir, "s2.bin")),
                       token_getter=lambda: "source-secret-3")
    catalog, err = src.get_catalog()
    check("C4 商店源带 X-JZ-Shell",
          catalog is not None and _StubHandler.seen_headers.get("X-JZ-Shell") == "source-secret-3",
          err or "")

    # C5 服务器壳门 403 → 客户端降级（清单：仅本地 + gate_blocked；页面：提示页）
    _StubHandler.mode = "gate403"
    r = cc.get("/api/tools")
    d = r.get_json(silent=True) or {}
    check("C5 壳门 403 → 清单降级仅本地 + gate_blocked 标记",
          r.status_code == 200 and d.get("degraded") is True and d.get("gate_blocked") is True
          and "接入密钥" in (d.get("gate_message") or ""))
    r = cc.get("/plugin/kb/index.html")
    check("C5 壳门 403 → 页面提示（指向连接设置）",
          r.status_code == 403 and b"/connection" in r.data)
    r = cc.get("/local-api/config")
    d = r.get_json(silent=True) or {}
    check("C5 /local-api/config 回传 gate_blocked 与 token_set",
          d.get("gate_blocked") is True and d.get("token_set") is True)
    # 配置保存后门状态复位（下一次请求见分晓）
    r = cc.post("/local-api/config/address", json={"address": addr, "token": "client-secret-1"})
    check("C5 保存密钥回传 token_set", (r.get_json(silent=True) or {}).get("token_set") is True)
    srv.shutdown()


def main():
    try:
        part_a_gate()
        part_b_store_projection()
        part_c_client()
    finally:
        failed = [r for r in RESULTS if not r[1]]
        print()
        print("通过 %d / %d" % (len(RESULTS) - len(failed), len(RESULTS)))
        if failed:
            print("失败项：")
            for name, _, detail in failed:
                print("  - %s  %s" % (name, detail))
            return 1
    print("browser-gate-tests：全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
