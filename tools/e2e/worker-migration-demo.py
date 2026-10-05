# -*- coding: utf-8 -*-
r"""试点迁移演练（T-O2-12d）：真实主应用 + 真实插件（notice-board）的 E 架构全链。

这是「真机迁移流程」的源码态彩排，与生产迁移步骤一一对应：
    1. 注册表登记单元（<数据根>\config\supervisor-units.json 加 notice-board）
    2. 主应用重启（源码态=重新装配；notice-board 跳过进程内加载 → 反代桥接管）
    3. supervisor 启动编排拉起 notice-board worker（jz_worker 引导件）
    4. 真实登录（admin/admin123，临时数据根自动初始化）→ 经主应用反代访问公告板 API：
       主应用鉴权管线（会话/权限点）→ 反代 → worker 两级会话（回环 /api/session
       取**真实**身份 dict）→ notice-board register(app) 原样路由
    5. 隔离验证：停 worker → 该插件 503 兜底、主应用与其余路由照常、登录态不丢

用法：python tools\e2e\worker-migration-demo.py
退出码：0 = 全过；1 = 有 FAIL。
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PILOT = "notice-board"          # 试点插件：无 native 依赖、低频（迁移选型口径）
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


def main():
    import requests

    work = tempfile.mkdtemp(prefix="jz_migrate_")
    data_root = os.path.join(work, "data")
    pkg_dir = os.path.join(work, "pkg")
    os.makedirs(os.path.join(data_root, "config"), exist_ok=True)
    # 试点插件随"主包"分发：<pkg>\plugins\notice-board（复制真实插件，一字不改）
    shutil.copytree(os.path.join(ROOT, "plugins", PILOT),
                    os.path.join(pkg_dir, "plugins", PILOT),
                    ignore=shutil.ignore_patterns("__pycache__", "data"))

    # ① 注册表登记单元（迁移流程第 1 步）
    os.makedirs(os.path.join(data_root, "config"), exist_ok=True)
    main_port = free_port()
    registry = {
        "schema": 1, "supervisor": {"port": free_port()},
        "port_range": [15101, 15199],
        "health_interval": 2.0, "health_grace": 8.0,
        "stable_window": 60.0, "circuit_threshold": 3,
        "worker": {"python": sys.executable,
                   "worker_script": os.path.join(ROOT, "jz_worker.py"),
                   "main_url": "http://127.0.0.1:%d" % main_port,
                   "plugins_dir": os.path.join(pkg_dir, "plugins"),
                   "main_pkg_dir": ROOT},
        "units": [{"name": PILOT, "kind": "plugin", "plugin_id": PILOT,
                   "interpreter": "main", "enabled": True}],
    }
    with open(os.path.join(data_root, "config", "supervisor-units.json"),
              "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.environ["JZTOOLS_DATA_ROOT"] = data_root

    # ② 真实主应用（迁移流程第 2 步：重启后 worker 化插件跳过进程内加载）
    import app as main_app
    main_app.register_plugin_backends(main_app.app)
    from supervisor.mainapp_bridge import install_worker_proxy
    install_worker_proxy(main_app.app, ROOT)
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", main_port, main_app.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True, name="main-app").start()
    base = "http://127.0.0.1:%d" % main_port
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if requests.get(base + "/login", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    print("工作目录：%s；主应用 %s（试点插件 %s）" % (work, base, PILOT))

    # ③ supervisor 启动编排（迁移流程第 3 步）
    from supervisor import supervisor_core as sc
    sup = sc.Supervisor(data_root)
    sup.start_monitor()
    r = sup.start_unit(PILOT)
    check("S1 supervisor 拉起 %s worker" % PILOT, r.get("ok"), str(r))
    wport = r.get("port")
    hh = None
    for _ in range(40):
        try:
            import urllib.request
            with urllib.request.urlopen(
                    "http://127.0.0.1:%s/_worker/health" % wport, timeout=2) as resp:
                hh = json.loads(resp.read().decode("utf-8"))
            break
        except Exception:
            time.sleep(0.5)
    check("S1 worker 健康（register(app) 原样契约）",
          bool(hh) and hh.get("ok") is True and hh.get("plugin") == PILOT, str(hh))

    # ④ 真实登录 + 全链 API（迁移流程第 4 步）
    s = requests.Session()
    r_login = s.post(base + "/api/login",
                     json={"username": "admin", "password": "admin123"}, timeout=10)
    check("S2 真实登录成功（默认 admin 账号）",
          r_login.status_code == 200 and r_login.json().get("ok") is True,
          r_login.text[:100])
    r_api = s.get(base + "/api/%s/announcements" % PILOT, timeout=15)
    body = {}
    try:
        body = r_api.json()
    except Exception:
        pass
    check("S2 经反代访问公告板 API → 200（鉴权管线→反代→worker→回环取真实身份）",
          r_api.status_code == 200 and body.get("ok") is True,
          "%s %s" % (r_api.status_code, str(body)[:120]))
    r_sess = s.get(base + "/api/session", timeout=10)
    check("S2 会话回环取到真实身份（非夹具 dict）",
          r_sess.json().get("user", {}).get("username") == "admin", r_sess.text[:100])

    # 反代路径核验：请求确实经过 worker（停 worker 后 503；进程内加载时不会 503）
    # ⑤ 隔离验证（迁移流程第 5 步 / C14 语义）
    sup.stop_unit(PILOT)
    r_down = s.get(base + "/api/%s/announcements" % PILOT, timeout=15)
    check("S3 worker 停止 → 该插件 503 兜底（证明服务路径经 worker）",
          r_down.status_code == 503, str(r_down.status_code))
    r_self = s.get(base + "/api/session", timeout=10)
    check("S3 主应用照常 + 登录态不丢",
          r_self.status_code == 200
          and r_self.json().get("user", {}).get("username") == "admin", str(r_self.status_code))
    r_tools = s.get(base + "/api/tools", timeout=10)
    ids = [t.get("id") for t in (r_tools.json() or {}).get("tools") or []]
    check("S3 工具清单照常（公告板卡片不消失）",
          r_tools.status_code == 200 and PILOT in ids, str(ids[:6]))

    sup.shutdown()
    srv.shutdown()
    print()
    fails = [r for r in RESULTS if not r[1]]
    print("结果：%d 项，FAIL %d" % (len(RESULTS), len(fails)))
    for name, _ok, detail in fails:
        print("  FAIL: %s  — %s" % (name, detail[:200]))
    shutil.rmtree(work, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
