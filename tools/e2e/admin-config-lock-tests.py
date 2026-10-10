# -*- coding: utf-8 -*-
"""D1/D2 读改写全程锁 · 并发回归台架（落地方案 L1-3）。

两条锁面各跑「两线程并发写两条不同数据」，逐轮断言**两处改动都保留**（无丢失更新）：
  A. config/admin.json —— `plugins/admin/backend/routes.py` 的 `ADMIN_CFG_LOCK`
     （真入口 `save_user_llm`：两个账号各写自己的 `llm.model`）；
  B. config/tools.json —— `app.py` 的 `REGISTRY_LOCK`
     （真入口 `POST /api/tools/visibility`：两个工具各改自己的 `enabled`）。

每个场景跑两遍：
  · 正例：带真实的全程锁 —— 每轮两处改动都必须在盘上；
  · 反证：只把跨步锁临时换成空上下文（被测源文件一字不改）—— 同一注入下必须出现
    丢失更新，否则说明用例恒真、抓不到这个缺陷面。
    注：退化的是那一把 RLock 本身，所以反证轮里单步 I/O 也失去了互斥，症状可能是
    「读到上一轮的值」（丢失更新）或 Windows 上 `os.replace` 撞车导致的 500 ——
    两者同属「无锁读改写」的危害面，台架都计为一次 loss。
竞态放大：`load_*` 返回后注入 2ms 睡眠（只在台架进程内打补丁）。每轮写入值都带轮号，
丢失更新会显形成「上一轮的值」，不会被重复写同样内容掩盖。

运行：python tools/e2e/admin-config-lock-tests.py
隔离：JZTOOLS_DATA_ROOT 指向临时目录，绝不碰真实 admin.json / tools.json。
"""
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

# 与控制台编码和解：本台架打印 ✅/❌，GBK 控制台（Windows 缺省）会在 print 处抛
# UnicodeEncodeError 直接崩掉整个台架；与 lint-static-rules.py 同口径把流切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

WORK = tempfile.mkdtemp(prefix="jz-lock-")
os.environ["JZTOOLS_DATA_ROOT"] = WORK        # 必须在导入被测模块之前

ROUNDS = 40
SLEEP_MS = 2

FAILS = []
PASSES = [0]


def check(name, ok, detail=""):
    if ok:
        PASSES[0] += 1
        print("  ✅ %s" % name)
    else:
        FAILS.append(name)
        print("  ❌ %s %s" % (name, detail))


class _NoLock(object):
    """把跨步全程锁退化成了空实现（单步 I/O 仍原子）——只用于反证。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def acquire(self, *a, **k):
        return True

    def release(self):
        pass


def _amplify(fn):
    """load 之后注入睡眠，放大「load→改→save」的跨步窗口。"""

    def wrapped(*a, **k):
        r = fn(*a, **k)
        time.sleep(SLEEP_MS / 1000.0)
        return r

    return wrapped


def _two_threads(fn1, fn2):
    b = threading.Barrier(2)

    def run(fn):
        def inner():
            b.wait()
            fn()
        return inner

    ts = [threading.Thread(target=run(f)) for f in (fn1, fn2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


# ===================== A. admin.json（ADMIN_CFG_LOCK） =====================

def load_admin_routes():
    """按框架的加载方式（jztools_<id> 包 + backend/routes.py）载入 admin 后端。"""
    pkg = "jztools_admin"
    for name in [n for n in sys.modules if n == pkg or n.startswith(pkg + ".")]:
        sys.modules.pop(name, None)
    backend = os.path.join(REPO, "plugins", "admin", "backend")
    spec = importlib.util.spec_from_file_location(
        pkg, os.path.join(backend, "__init__.py"), submodule_search_locations=[backend])
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = module
    spec.loader.exec_module(module)
    return importlib.import_module(pkg + ".routes")


def _user_model(cfg, username):
    for unit in cfg.get("units", []):
        for dept in unit.get("departments", []):
            for user in dept.get("users", []):
                if user.get("username") == username:
                    return (user.get("llm") or {}).get("model")
    return None


def admin_round_values(r):
    return ("A-%d" % r, "B-%d" % r)


def run_admin(mod, locked):
    """两线程并发写两个不同账号的 llm.model，返回出现丢失更新的轮号列表。"""
    real_lock, real_load = mod.ADMIN_CFG_LOCK, mod.load_admin_config
    if not locked:
        mod.ADMIN_CFG_LOCK = _NoLock()
    mod.load_admin_config = _amplify(real_load)
    lost = []
    try:
        for r in range(ROUNDS):
            v1, v2 = admin_round_values(r)

            def w1():
                try:
                    mod.save_user_llm("lock-u1", {"format": "openai", "model": v1})
                except Exception:
                    pass          # 反证轮的写失败同样计为一次未保留（见 docstring 注）

            def w2():
                try:
                    mod.save_user_llm("lock-u2", {"format": "openai", "model": v2})
                except Exception:
                    pass

            _two_threads(w1, w2)
            try:
                with open(mod.ADMIN_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                kept = _user_model(cfg, "lock-u1") == v1 and _user_model(cfg, "lock-u2") == v2
            except Exception:
                kept = False
            if not kept:
                lost.append(r)
    finally:
        mod.ADMIN_CFG_LOCK = real_lock
        mod.load_admin_config = real_load
    return lost


def seed_admin(mod):
    cfg = mod.load_admin_config()
    cfg["units"] = (cfg.get("units") or []) + [{
        "id": "unit-lock", "name": "锁测单位", "description": "",
        "departments": [{
            "id": "dept-lock", "name": "锁测部门", "description": "",
            "users": [
                {"username": "lock-u1", "name": "甲", "password": "", "role": "",
                 "permissions": [], "llm": {"model": ""}},
                {"username": "lock-u2", "name": "乙", "password": "", "role": "",
                 "permissions": [], "llm": {"model": ""}},
            ],
        }],
    }]
    mod.save_admin_config(cfg)


# ===================== B. tools.json（REGISTRY_LOCK） =====================

def run_tools(A, locked):
    """两线程并发切换两个不同工具的 enabled，返回出现丢失更新的轮号列表。"""
    real_lock, real_load = A.REGISTRY_LOCK, A.load_registry
    if not locked:
        A.REGISTRY_LOCK = _NoLock()
    A.load_registry = _amplify(real_load)
    lost = []
    try:
        for r in range(ROUNDS):
            e1, e2 = tool_round_values(r)

            def w1():
                try:
                    A.app.test_client().post("/api/tools/visibility",
                                             json={"type": "tool", "id": "lock-t1", "enabled": e1})
                except Exception:
                    pass

            def w2():
                try:
                    A.app.test_client().post("/api/tools/visibility",
                                             json={"type": "tool", "id": "lock-t2", "enabled": e2})
                except Exception:
                    pass

            _two_threads(w1, w2)
            try:
                with open(A.CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                    reg = json.load(f)
                got = {t["id"]: t.get("enabled") for t in reg.get("tools", [])}
                kept = got.get("lock-t1") == e1 and got.get("lock-t2") == e2
            except Exception:
                kept = False
            if not kept:
                lost.append(r)
    finally:
        A.REGISTRY_LOCK = real_lock
        A.load_registry = real_load
    return lost


def tool_round_values(r):
    """每轮换一组值：丢失更新会显形成上一轮的值，而不是被同值重复写掩盖。"""
    return (r % 2 == 0, r % 2 == 1)


def seed_tools(A):
    reg = {"site": {}, "categories": [{"id": "cat-lock", "name": "锁测分类"}],
           "tools": [{"id": "lock-t1", "name": "工具一", "category": "cat-lock",
                      "enabled": True, "order": 1},
                     {"id": "lock-t2", "name": "工具二", "category": "cat-lock",
                      "enabled": True, "order": 2}]}
    A.save_registry(reg)


def main():
    print("== A 段：config/admin.json 读改写全程锁（ADMIN_CFG_LOCK）==")
    mod = load_admin_routes()
    seed_admin(mod)
    check("A0 跨步锁已就位（save_user_llm 的 load→改→save 整段在 ADMIN_CFG_LOCK 内）",
          isinstance(mod.ADMIN_CFG_LOCK, type(threading.RLock())))
    lost_off = run_admin(mod, locked=False)
    check("A1 反证：去掉跨步锁后并发写必然互相覆盖（缺陷面真实存在，用例非恒真）",
          len(lost_off) > 0, "%d/%d 轮均未复现丢失更新——放大窗口失效，判定不可信"
          % (len(lost_off), ROUNDS))
    lost_on = run_admin(mod, locked=True)
    check("A2 正证：全程锁下 %d 轮并发写两处改动都保留" % ROUNDS,
          not lost_on, "丢失更新轮=%s" % lost_on[:5])

    print("== B 段：config/tools.json 读改写全程锁（REGISTRY_LOCK）==")
    import app as A
    # 反证轮的 500 / 「tools.json 不可读」告警都属预期症状，只计数不刷日志
    A.app.logger.disabled = True
    A.app.config["PROPAGATE_EXCEPTIONS"] = False
    for _name in ("jztools.app", "werkzeug"):
        logging.getLogger(_name).setLevel(logging.CRITICAL)
    A.init_data_root()
    seed_tools(A)
    check("B0 跨步锁已就位（app.py 暴露模块级 REGISTRY_LOCK）",
          isinstance(A.REGISTRY_LOCK, type(threading.RLock())))
    lost_off_t = run_tools(A, locked=False)
    check("B1 反证：去掉跨步锁后 /api/tools/visibility 并发写互相覆盖",
          len(lost_off_t) > 0, "%d/%d 轮均未复现丢失更新——放大窗口失效，判定不可信"
          % (len(lost_off_t), ROUNDS))
    lost_on_t = run_tools(A, locked=True)
    check("B2 正证：全程锁下 %d 轮并发改两个不同工具两处改动都保留" % ROUNDS,
          not lost_on_t, "丢失更新轮=%s" % lost_on_t[:5])

    print("== C 段：/api/admin/plugins/index 两次进锁（裁定 6 方案 b）==")
    import threading as _th
    import jz_api
    from flask import Flask
    ROOT_USER = {"username": "root", "name": "超管", "super_admin": True, "permissions": []}
    jz_api.register_providers(get_session_user=lambda: dict(ROOT_USER),
                              set_operation=lambda op: None)
    capp = Flask("lock_probe_c")
    capp.config["TESTING"] = True
    mod.register(capp)
    # admin 的 login_required / _plugin_mgr_ok 走本插件自己的 get_session_user（读 flask 会话），
    # 台架不起真实登录流程，直接按同一签名打桩（与其余套件的 provider 打桩同口径）
    mod.get_session_user = lambda: dict(ROOT_USER)
    pa = sys.modules.get("jztools_admin.plugin_admin") or mod._load_plugin_admin()
    held, errbox = [], []
    gate = _th.Event()
    orig_cu = pa.check_updates

    def fake_check_updates(project, data_root, path):
        # 非阻塞 acquire 成功＝此刻没持锁（共享盘读取确实在锁外）
        held.append(mod.ADMIN_CFG_LOCK.acquire(blocking=False))
        if held[-1]:
            mod.ADMIN_CFG_LOCK.release()
        gate.set()
        time.sleep(0.4)                                    # 模拟共享盘读取耗时
        return {"ok": True, "updates": [], "checked": 0}

    def call_index():
        try:
            errbox.append(capp.test_client().get(
                "/api/admin/plugins/index?path=index-share-01").status_code)
        except Exception as exc:
            errbox.append(repr(exc))

    def find_model(cfg, username):
        for unit in cfg.get("units", []):
            for dept in unit.get("departments", []):
                for u in dept.get("users", []):
                    if u.get("username") == username:
                        return (u.get("llm") or {}).get("model")
        return None

    pa.check_updates = fake_check_updates
    try:
        t = _th.Thread(target=call_index)
        t.start()
        gate.wait(10)                                       # 已进入"读盘"窗口
        mod.save_user_llm("lock-u1", {"format": "openai", "model": "并发写不被覆盖"})
        t.join(30)
    finally:
        pa.check_updates = orig_cu
    cfg_now = json.load(open(mod.ADMIN_CONFIG_PATH, "r", encoding="utf-8"))
    check("C1 共享盘读取不在 ADMIN_CFG_LOCK 内（非阻塞 acquire 必成功）",
          held == [True], "held=%s" % held)
    check("C2 读盘期间另一字段的并发写未被覆盖（第二段锁内重新 load）",
          find_model(cfg_now, "lock-u1") == "并发写不被覆盖",
          "现值=%s" % find_model(cfg_now, "lock-u1"))
    check("C3 索引路径仍被记住（功能语义不变）",
          cfg_now.get("plugin_index_path") == "index-share-01",
          str(cfg_now.get("plugin_index_path")))
    check("C4 端点返回 200（登录态与错误语义原样）", errbox == [200], str(errbox))

    print("\n== 读改写全程锁台架：%d/%d 通过 ==" % (PASSES[0], PASSES[0] + len(FAILS)))
    for f in FAILS:
        print("  FAIL %s" % f)
    shutil.rmtree(WORK, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
