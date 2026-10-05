# -*- coding: utf-8 -*-
r"""批次 8b（T-O2-11/12 插件运行架构 E）台架。

覆盖（对应运行架构设计文档 §2–§6 拍板口径与 C14）：
  A  注册表与端口：首次分配持久化（分配一次长期占用）/ 重启复用 / 冲突报错不起不换号
  B  worker 拉起与会话：健康探针 / 两级 provider（本地验签 + 回环 /api/session 取
     全量 dict）/ 无 Cookie 与回环失败 → fail-closed 401
  C  熔断（双计数器）：快速崩溃连击 3 次 → circuit-open 不再自动拉起；
     clear-circuit 后可启动
  D  升级编排：apply 停换起同端口版本生效 / 坏包结构校验失败不动旧版 /
     rollback 回旧版 / 全程不打 restart_pending（§2 拍板）
  E  控制接口：令牌 401 / status / stop-start / clear-circuit
  F  主应用桥 + C14：worker 化清单快照 / 反代转发 / worker 不可达 503 /
     统一 errorhandler（插件 503、框架 500）/ 毒插件熔断后主应用兜底不受影响

用法：python tools\e2e\supervisor-tests.py   （源码态；worker 为真实子进程）
退出码：0 = 全过；1 = 有 FAIL。
"""

import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s %s%s" % ("[OK]" if ok else "[!!]", name, ("  — " + detail) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


SECRET = "unit-test-secret-key-0123456789abcdef"
PLUGIN_ID = "fixture-p"


def write_backend(pkg_dir, version):
    """夹具插件：whoami（两级会话链+版本自报）/ die（进程自尽，供熔断组）。"""
    pdir = os.path.join(pkg_dir, "plugins", PLUGIN_ID, "backend")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pkg_dir, "plugins", PLUGIN_ID, "manifest.json"),
              "w", encoding="utf-8") as f:
        json.dump({"id": PLUGIN_ID, "version": version, "name": "夹具插件"}, f)
    with open(os.path.join(pdir, "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(pdir, "routes.py"), "w", encoding="utf-8") as f:
        f.write(
            "import json, os\n"
            "API_PREFIX = \"/api/fixture-p\"\n"
            "_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
            "def _ver():\n"
            "    try:\n"
            "        with open(os.path.join(_HERE, 'manifest.json'), encoding='utf-8-sig') as f:\n"
            "            return json.load(f).get('version') or '?'\n"
            "    except Exception:\n"
            "        return '?'\n"
            "def register(app):\n"
            "    import jz_api\n"
            "    from flask import jsonify\n"
            "    @app.get(API_PREFIX + '/whoami')\n"
            "    def fp_whoami():\n"
            "        user = jz_api.get_session_user()\n"
            "        if user is None:\n"
            "            return jsonify({'ok': False, 'error': '未登录'}), 401\n"
            "        return jsonify({'ok': True, 'user': user, 'version': _ver(), 'pid': os.getpid()})\n"
            "    @app.get(API_PREFIX + '/die')\n"
            "    def fp_die():\n"
            "        os._exit(1)\n"
            "    @app.get(API_PREFIX + '/depcheck')\n"
            "    def fp_depcheck():\n"
            "        from flask import jsonify\n"
            "        try:\n"
            "            import dbgmod\n"
            "            return jsonify({'ok': True, 'dbgmod': dbgmod.MARKER})\n"
            "        except Exception as e:\n"
            "            return jsonify({'ok': False, 'error': str(e)}), 500\n"
        )


def make_backend_zip(pkg_dir, version, venv_wheels=None):
    """server 后端包（plugin-package.json + payload/plugins/fixture-p/**）。

    包内 manifest.json 的 version 覆写为包版本（D 组「新版生效」断言的依据）。
    venv_wheels：{wheel 文件名: bytes}——deps_mode=venv 包的 wheel 闭包（§4）。
    """
    pdir = os.path.join(pkg_dir, "plugins", PLUGIN_ID)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        meta = {"schema": 1, "kind": "plugin-upgrade", "id": PLUGIN_ID,
                "version": version, "api_version": 1, "requires_restart": False}
        if venv_wheels:
            sha = {}
            for name, data in venv_wheels.items():
                sha[name] = hashlib.sha256(data).hexdigest()
                zf.writestr("payload/wheels/" + name, data)
            meta["venv"] = {"python": "%d.%d" % sys.version_info[:2],
                            "wheels": sorted(venv_wheels), "wheel_sha256": sha}
        zf.writestr("plugin-package.json", json.dumps(meta))
        for root, _dirs, files in os.walk(pdir):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, pdir).replace(os.sep, "/")
                if rel == "manifest.json":
                    continue                      # 根 manifest 按包版本覆写
                zf.write(full, ("payload/plugins/%s/" % PLUGIN_ID) + rel)
        zf.writestr("payload/plugins/%s/manifest.json" % PLUGIN_ID,
                    json.dumps({"id": PLUGIN_ID, "version": version,
                                "name": "夹具插件"}))
    buf.seek(0)
    out = os.path.join(pkg_dir, "JZToolsHub-插件-%s-v%s.zip" % (PLUGIN_ID, version))
    with open(out, "wb") as f:
        f.write(buf.getvalue())
    return out


def make_minimal_wheel(dest_dir, name="dbgmod", version="1.0.0"):
    """手工造一个最小合法 wheel（py3-none-any；RECORD 允许空哈希）——离线测试用。"""
    files = {
        "%s/__init__.py" % name: "MARKER = '%s-%s'\n" % (name, version),
        "%s-%s.dist-info/METADATA" % (name, version):
            "Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, version),
        "%s-%s.dist-info/WHEEL" % (name, version):
            "Wheel-Version: 1.0\nGenerator: rig\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "%s-%s.dist-info/RECORD" % (name, version):
            "%s/__init__.py,,\n%s-%s.dist-info/RECORD,,\n" % (name, name, version),
    }
    out = os.path.join(dest_dir, "%s-%s-py3-none-any.whl" % (name, version))
    with zipfile.ZipFile(out, "w") as zf:
        for arc, data in files.items():
            zf.writestr(arc, data)
    return out


def stub_main_app():
    """stub 主应用：/api/session（信息回环源）/ /api/org/tree。"""
    from flask import Flask, jsonify
    app = Flask("stub-main")

    @app.get("/api/session")
    def _session():
        return jsonify({"user": {"username": "tester", "name": "测试员",
                                 "role": "办案员", "role_id": "role-user",
                                 "unit": "单位", "unit_id": "u1",
                                 "department": "部门", "department_id": "d1",
                                 "permissions": [PLUGIN_ID], "super_admin": False}})

    @app.get("/api/org/tree")
    def _org():
        return jsonify([{"id": "u1", "name": "单位", "children": []}])

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_port


def signed_cookie():
    """与 worker 同密钥构造签名会话 Cookie（验签链的测试锚点）。"""
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface
    app = Flask(__name__)
    app.secret_key = SECRET
    ser = SecureCookieSessionInterface().get_signing_serializer(app)
    return ser.dumps({"user": "tester", "login_at": time.time(),
                      "last_active": time.time()})


def write_registry(data_root, units, main_url, plugins_dir, fast=False):
    os.makedirs(os.path.join(data_root, "config"), exist_ok=True)
    obj = {"schema": 1, "supervisor": {"port": free_port()},
           "port_range": [15101, 15199],
           "health_interval": 0.3 if fast else 10.0,
           "health_grace": 2.0 if fast else 8.0,
           "stable_window": 30.0 if fast else 60.0,   # 快速档：崩溃周期~4s 须远小于稳定窗
           "circuit_threshold": 3,
           "worker": {"python": sys.executable,
                      "worker_script": os.path.join(ROOT, "jz_worker.py"),
                      "main_url": main_url,
                      "plugins_dir": plugins_dir,
                      "main_pkg_dir": ROOT},
           "units": units}
    p = os.path.join(data_root, "config", "supervisor-units.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return obj


def wait_health(port, timeout=20.0):
    deadline = time.time() + timeout
    url = "http://127.0.0.1:%s/_worker/health" % port
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as r:
                if r.status == 200:
                    return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.3)
    return None


def whoami(port, cookie=None, timeout=10):
    req = urllib.request.Request("http://127.0.0.1:%d/api/%s/whoami"
                                 % (port, PLUGIN_ID))
    if cookie:
        req.add_header("Cookie", "session=" + cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as exc:
        return None, {"error": str(exc)}


def hit(port, path, timeout=5):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path),
                                    timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def wait_circuit(sup, name, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = sup.status()
        u = next((x for x in st["units"] if x["name"] == name), {})
        if u.get("circuit_open"):
            return u
        time.sleep(0.4)
    return st and next((x for x in st.get("units", []) if x["name"] == name), {})


def main():
    os.environ["JZTOOLS_DATA_ROOT"] = ""      # 清场：统一由 temp 数据根控制（后设）
    work = tempfile.mkdtemp(prefix="jz_sup_test_")
    data_root = os.path.join(work, "data")
    pkg_dir = os.path.join(work, "pkg")       # 夹具"主包"（plugins/ 所在）
    os.makedirs(os.path.join(data_root, "config"), exist_ok=True)
    os.makedirs(os.path.join(data_root, "logs"), exist_ok=True)
    write_backend(pkg_dir, "1.0.0")
    with open(os.path.join(data_root, "config", "admin.json"), "w", encoding="utf-8") as f:
        json.dump({"secret_key": SECRET}, f)
    srv, main_url = stub_main_app()
    print("工作目录：%s（stub 主应用 %s）" % (work, main_url))
    sup = None
    try:
        # ---------- A. 注册表与端口 ----------
        print("==> A. 注册表与端口（分配一次持久化 / 冲突不换号）")
        os.environ["JZTOOLS_DATA_ROOT"] = data_root   # 桥与 jztools_data 同根
        unit = {"name": PLUGIN_ID, "kind": "plugin", "plugin_id": PLUGIN_ID,
                "interpreter": "main", "enabled": True}
        write_registry(data_root, [unit], main_url,
                       os.path.join(pkg_dir, "plugins"), fast=True)
        from supervisor import supervisor_core as sc
        sup = sc.Supervisor(data_root)
        sup.start_monitor()
        r = sup.start_unit(PLUGIN_ID)
        check("A1 worker 拉起成功", r.get("ok"), str(r))
        port = r.get("port")
        reg_disk = sc.load_registry(data_root)
        u2 = sc.find_unit(reg_disk, PLUGIN_ID)
        check("A1 端口已分配并持久化（15101-15199）",
              u2.get("port") == port and 15101 <= port <= 15199, str(u2.get("port")))
        h = wait_health(port)
        check("A1 健康探针 200 + 插件标识",
              bool(h) and h.get("ok") is True and h.get("plugin") == PLUGIN_ID, str(h))

        sup.stop_unit(PLUGIN_ID)
        sup2 = sc.Supervisor(data_root)               # 重启 supervisor：端口复用
        sup2.start_monitor()
        r2 = sup2.start_unit(PLUGIN_ID)
        check("A2 重启后端口复用（不换号）",
              r2.get("ok") and r2.get("port") == port,
              "old=%s new=%s" % (port, r2.get("port")))
        check("A2 同端口健康恢复", bool(wait_health(port, 20)))

        busy = free_port()
        holder = socket.socket()
        holder.bind(("127.0.0.1", busy))
        holder.listen(1)
        reg3 = sc.load_registry(data_root)
        sc.find_unit(reg3, PLUGIN_ID)["port"] = busy
        sc.save_registry(data_root, reg3)
        sup3 = sc.Supervisor(data_root)
        r3 = sup3.start_unit(PLUGIN_ID)
        reg3 = sc.load_registry(data_root)
        check("A3 端口冲突 → 报错不起且不换号",
              not r3.get("ok") and "占用" in r3.get("error", "")
              and sc.find_unit(reg3, PLUGIN_ID)["port"] == busy, str(r3))
        holder.close()
        # A3 后恢复注册表端口为真实端口（sup2 后续 apply 从磁盘重读注册表）
        reg4 = sc.load_registry(data_root)
        sc.find_unit(reg4, PLUGIN_ID)["port"] = port
        sc.save_registry(data_root, reg4)
        sup3._registry = sc.load_registry(data_root)   # sup3 复位到无冲突视图（不再使用）
        check("A3 旧实例不受影响（同端口仍健康）", bool(wait_health(port, 10)))

        # ---------- B. 两级会话链 ----------
        print("==> B. worker 会话（本地验签 + 回环 /api/session，fail-closed）")
        ck = signed_cookie()
        code, d = whoami(port, ck)
        check("B1 本地验签+回环 → 全量信息 dict",
              code == 200 and d.get("ok") and d["user"].get("username") == "tester"
              and d["user"].get("name") == "测试员"
              and d["user"].get("unit_id") == "u1"
              and d["user"].get("department_id") == "d1",
              json.dumps(d.get("user"), ensure_ascii=False))
        check("B1 插件自报版本（register(app) 契约原样）",
              d.get("version") == "1.0.0", str(d.get("version")))
        code2, _ = whoami(port, None)
        check("B2 无 Cookie → fail-closed 401", code2 == 401, str(code2))

        # 回环失败（main_url 指向死端口）→ 同样 fail-closed
        dr2 = os.path.join(work, "data2")
        os.makedirs(os.path.join(dr2, "config"), exist_ok=True)
        with open(os.path.join(dr2, "config", "admin.json"), "w", encoding="utf-8") as f:
            json.dump({"secret_key": SECRET}, f)
        write_registry(dr2, [dict(unit)], "http://127.0.0.1:%d" % free_port(),
                       os.path.join(pkg_dir, "plugins"), fast=True)
        sup_bad = sc.Supervisor(dr2)
        rb = sup_bad.start_unit(PLUGIN_ID)
        hb = wait_health(rb["port"], 20) if rb.get("ok") else None
        check("B3 回环失败环境 worker 仍健康（信息面降级不影响存活）",
              rb.get("ok") and bool(hb), str(rb))
        code3, _ = whoami(rb.get("port") or 0, ck)
        check("B3 回环失败 → fail-closed 401（不回退半真身份）", code3 == 401, str(code3))
        sup_bad.shutdown()

        # ---------- C. 熔断（快速崩溃连击） ----------
        print("==> C. 熔断（双计数器：快速崩溃连击 3 次 → circuit-open）")
        for i in range(1, 4):
            if i > 1:
                wait_health(port, 20)                  # 上一轮已重启并恢复
            hit(port, "/api/%s/die" % PLUGIN_ID, timeout=5)
            if i < 3:
                time.sleep(1.0)                        # 等监控循环记录本次死亡
        u = wait_circuit(sup2, PLUGIN_ID, timeout=30)
        check("C1 连死 3 次 → 熔断下线",
              bool(u.get("circuit_open")), str(u))
        time.sleep(3.0)
        u = next((x for x in sup2.status()["units"] if x["name"] == PLUGIN_ID), {})
        check("C2 熔断中不自动拉起", not u.get("running") and u.get("circuit_open"), str(u))
        rc = sup2.start_unit(PLUGIN_ID)
        check("C3 熔断中 start 拒绝", not rc.get("ok") and "熔断" in rc.get("error", ""), str(rc))
        sup2.clear_circuit(PLUGIN_ID)
        r4 = sup2.start_unit(PLUGIN_ID)
        if not r4.get("ok"):
            import subprocess as _sp
            _ns = _sp.run(["netstat", "-ano"], capture_output=True, text=True).stdout
            print("    [诊断] C4 端口占用：",
                  [l.strip() for l in _ns.splitlines() if ":%d " % port in l][:4])
        check("C4 clear-circuit 后可启动",
              r4.get("ok") and bool(wait_health(port, 20)), str(r4))

        # ---------- D. 升级编排 ----------
        print("==> D. apply 停换起同端口 / 坏包不动 / rollback / 不打 restart_pending")
        zip2 = make_backend_zip(pkg_dir, "2.0.0")
        rd = sup2.apply_package(PLUGIN_ID, zip2)
        check("D1 apply 成功（同端口、版本登记）",
              rd.get("ok") and rd.get("version") == "2.0.0" and rd.get("port") == port,
              str(rd))
        check("D1 新版已生效（worker 重建 + 健康通过）",
              bool(wait_health(port, 20)) and whoami(port, ck)[1].get("version") == "2.0.0",
              str(whoami(port, ck)[1].get("version")))
        bad_zip = os.path.join(work, "bad.zip")
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("plugin-package.json", "{ 破 json")
        rd2 = sup2.apply_package(PLUGIN_ID, bad_zip)
        code4, d4 = whoami(port, ck)
        check("D2 坏包 → 结构校验拒绝且旧版继续服务",
              not rd2.get("ok") and code4 == 200 and d4.get("version") == "2.0.0",
              "%s / %s" % (rd2.get("error"), d4.get("version")))
        zip3 = make_backend_zip(pkg_dir, "3.0.0")
        rd3 = sup2.apply_package(PLUGIN_ID, zip3)
        check("D3 升到 3.0.0（为回滚做准备）",
              rd3.get("ok") and whoami(port, ck)[1].get("version") == "3.0.0", str(rd3))
        rr = sup2.rollback_unit(PLUGIN_ID)
        code5, d5 = whoami(port, ck)
        check("D3 rollback → 回到最近备份版 2.0.0 且服务恢复",
              rr.get("ok") and code5 == 200 and d5.get("version") == "2.0.0",
              "%s / %s" % (str(rr), d5.get("version")))
        state_file = os.path.join(data_root, "config", ".app_state.json")
        state = {}
        if os.path.isfile(state_file):
            with open(state_file, "r", encoding="utf-8-sig") as f:
                state = json.load(f) or {}
        pend = (state.get("plugins") or {}).get(PLUGIN_ID, {}).get("restart_pending")
        check("D4 全程不打 restart_pending（§2 拍板）", not pend, str(state.get("plugins")))

        # ---------- D5. venv 建环境（deps_mode=venv，§4） ----------
        print("==> D5. venv 建环境（wheel 闭包 → venv 解释器拉起 → 私有依赖生效）")
        wheels_dir = os.path.join(work, "wheels")
        os.makedirs(wheels_dir, exist_ok=True)
        make_minimal_wheel(wheels_dir, "dbgmod", "1.0.0")
        with open(os.path.join(wheels_dir, "dbgmod-1.0.0-py3-none-any.whl"), "rb") as f:
            whl_bytes = f.read()
        zipv = make_backend_zip(pkg_dir, "4.0.0",
                                venv_wheels={"dbgmod-1.0.0-py3-none-any.whl": whl_bytes})
        rd4 = sup2.apply_package(PLUGIN_ID, zipv)
        check("D5 venv 包 apply 成功", rd4.get("ok"), str(rd4))
        regv = sc.load_registry(data_root)
        uv = sc.find_unit(regv, PLUGIN_ID) or {}
        check("D5 单元 deps_mode=venv + venv 解释器登记",
              uv.get("deps_mode") == "venv" and "venvs" in str(uv.get("interpreter")),
              str(uv.get("deps_mode")) + " / " + str(uv.get("interpreter")))
        check("D5 venv worker 健康", bool(wait_health(port, 30)),
              str(wait_health(port, 10)))
        code7, d7 = whoami(port, ck)
        check("D5 venv 拉起下业务 API 仍走两级会话", code7 == 200 and d7.get("version") == "4.0.0",
              "%s %s" % (code7, d7.get("version")))
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/%s/depcheck" % (port, PLUGIN_ID),
                    timeout=10) as resp:
                dep = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            dep = {"error": str(e)}
        check("D5 插件私有依赖（dbgmod）venv 内可导入",
              dep.get("ok") and dep.get("dbgmod") == "dbgmod-1.0.0", str(dep))
        bad_wheel = os.path.join(work, "wheels-bad")
        os.makedirs(bad_wheel, exist_ok=True)
        make_minimal_wheel(bad_wheel, "dbgmod", "2.0.0")
        with open(os.path.join(bad_wheel, "dbgmod-2.0.0-py3-none-any.whl"), "rb") as f:
            bad_bytes = f.read()
        zipbad = make_backend_zip(pkg_dir, "5.0.0",
                                  venv_wheels={"dbgmod-1.0.0-py3-none-any.whl": bad_bytes})
        # 声明 sha 改成全零（模拟清单与介质不符）——make_backend_zip 的 sha 是诚实的
        _buf = io.BytesIO()
        with zipfile.ZipFile(zipbad) as zin, zipfile.ZipFile(_buf, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == "plugin-package.json":
                    m = json.loads(data.decode("utf-8"))
                    m["venv"]["wheel_sha256"] = {k: "0" * 64
                                                 for k in m["venv"]["wheels"]}
                    data = json.dumps(m).encode("utf-8")
                zout.writestr(info, data)
        with open(zipbad, "wb") as f:
            f.write(_buf.getvalue())
        rd5 = sup2.apply_package(PLUGIN_ID, zipbad)
        code8, d8 = whoami(port, ck)
        check("D5 wheel sha256 不符 → apply 拒绝且旧版继续服务",
              not rd5.get("ok") and code8 == 200 and d8.get("version") == "4.0.0",
              "%s / %s" % (rd5.get("error"), d8.get("version")))

        # ---------- E. 控制接口 ----------
        print("==> E. 控制接口（令牌 / status / stop-start / clear-circuit）")
        from supervisor import control_api
        token = control_api.ensure_token(data_root)
        ctl_port = sup2.supervisor_port()
        ctl = control_api.start_control_server(sup2, token, ctl_port)
        time.sleep(0.3)

        def ctl_req(method, path, body=None, tok=token):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (ctl_port, path),
                                         method=method,
                                         data=json.dumps(body or {}).encode("utf-8")
                                         if method == "POST" else None)
            if tok:
                req.add_header("X-Supervisor-Token", tok)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read().decode("utf-8"))
                except Exception:
                    return e.code, {}

        s0, _ = ctl_req("GET", "/status", tok="")
        check("E1 无令牌 → 401", s0 == 401, str(s0))
        s1, b1 = ctl_req("GET", "/status", tok="wrong")
        check("E1 错令牌 → 401", s1 == 401, str(s1))
        s2, b2 = ctl_req("GET", "/status")
        u_e = next((x for x in b2.get("units", []) if x["name"] == PLUGIN_ID), {})
        check("E2 status 200 且单元在列（含版本；venv 模式可见）",
              s2 == 200 and u_e.get("installed_version") == "4.0.0"
              and u_e.get("deps_mode") == "venv", str(b2)[:140])
        s3, _ = ctl_req("POST", "/stop", {"unit": PLUGIN_ID})
        time.sleep(0.5)
        u3 = next((x for x in sup2.status()["units"] if x["name"] == PLUGIN_ID), {})
        check("E3 stop → 停止", s3 == 200 and not u3.get("running"), str(u3))
        s4, _ = ctl_req("POST", "/start", {"unit": PLUGIN_ID})
        check("E3 start → 恢复健康",
              s4 == 200 and bool(wait_health(port, 20)), "")

        # ---------- F. 主应用桥 + C14 ----------
        print("==> F. 主应用桥（反代 / errorhandler / C14 毒插件兜底）")
        from supervisor.mainapp_bridge import (install_worker_proxy,
                                               worker_units_snapshot)
        units = worker_units_snapshot(data_root)
        check("F1 worker 化清单快照（mtime 缓存）", PLUGIN_ID in units, str(list(units)))
        from flask import Flask, jsonify
        bapp = Flask("bridge-test")

        @bapp.get("/api/selfcheck")
        def _self():
            return jsonify({"ok": True, "where": "main-app"})

        @bapp.get("/api/inproc-x/boom")
        def _boom():
            raise RuntimeError("进程内插件异常")

        @bapp.get("/api/selfcheck-bad")
        def _bad():
            raise RuntimeError("框架路径异常")   # 非插件前缀 → 维持 500 语义

        bapp.config["_PLUGIN_IDS"] = {"inproc-x"}
        install_worker_proxy(bapp, ROOT, register_errorhandler=True)
        bc = bapp.test_client()
        bc.set_cookie("session", ck)               # werkzeug 3.x 走 cookie jar（裸 Cookie 头会被吞）
        code6, d6 = whoami(port, ck)               # 直连 worker 正常
        r = bc.get("/api/%s/whoami" % PLUGIN_ID)
        check("F2 反代转发：主应用 → worker 同结果",
              r.status_code == 200 and (r.get_json() or {}).get("user", {}).get("name") == "测试员",
              "%s %s" % (r.status_code, str(r.get_data()[:120])))
        check("F2 直连同 cookie 参照（两路一致的前提）",
              code6 == 200 and d6.get("user", {}).get("name") == "测试员", str(code6))

        sup2.stop_unit(PLUGIN_ID)
        r = bc.get("/api/%s/whoami" % PLUGIN_ID)
        check("F3 worker 不可达 → 主应用桥 503 兜底",
              r.status_code == 503 and "不可用" in (r.get_json() or {}).get("error", ""),
              "%s %s" % (r.status_code, str(r.get_data()[:120])))
        r = bc.get("/api/selfcheck")
        check("F3 主应用其余路由不受影响", r.status_code == 200
              and r.get_json().get("where") == "main-app", str(r.status_code))

        r = bc.get("/api/inproc-x/boom")
        check("F4 errorhandler：进程内插件异常 → 该插件 503",
              r.status_code == 503 and (r.get_json() or {}).get("plugin") == "inproc-x",
              "%s %s" % (r.status_code, str(r.get_data()[:120])))
        r = bc.get("/api/selfcheck-bad")           # 框架路径异常 → 维持 500 语义
        check("F4 框架路径异常 → 500（语义不变）", r.status_code == 500, str(r.status_code))

        # C14 整合：worker 起来后经主应用桥打 die ×3 → 熔断 → 桥 503，主应用照常
        sup2.start_unit(PLUGIN_ID)
        check("C14 worker 重启健康", bool(wait_health(port, 20)), "")
        for i in range(3):
            if i > 0:
                wait_health(port, 20)
            bc.get("/api/%s/die" % PLUGIN_ID)
            if i < 2:
                time.sleep(1.0)
        u14 = wait_circuit(sup2, PLUGIN_ID, timeout=30)
        r = bc.get("/api/%s/whoami" % PLUGIN_ID)
        r2 = bc.get("/api/selfcheck")
        check("C14 毒插件经熔断下线 → 桥 503、主应用不受影响",
              bool(u14.get("circuit_open")) and r.status_code == 503
              and r2.status_code == 200
              and r2.get_json().get("where") == "main-app",
              "circuit=%s whoami=%s selfcheck=%s" % (bool(u14.get("circuit_open")),
                                                     r.status_code, r2.status_code))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("台架阶段异常", False, str(exc))
    finally:
        try:
            if sup is not None:
                sup.shutdown()
        except Exception:
            pass
        try:
            sup2.shutdown()
        except Exception:
            pass
        try:
            srv.shutdown()
        except Exception:
            pass

    print()
    fails = [r for r in RESULTS if not r[1]]
    print("结果：%d 项，FAIL %d" % (len(RESULTS), len(fails)))
    for name, _ok, detail in fails:
        print("  FAIL: %s  — %s" % (name, detail[:220]))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
