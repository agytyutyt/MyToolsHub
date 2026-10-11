# -*- coding: utf-8 -*-
"""JZClient 包校验（批次 1 T07）：结构断言 + --smoke（与主包 verify-package.py 同风格）。

结构断言：产物存在、构建配置/兜底页/主界面副本/内核模块随包、无主包业务插件混入。
--smoke：临时数据目录启动 exe → 等 client.log 的 CAPABILITY 行 → 探测
  / （未配地址 → 兜底页）与 /local-api/health → 退出码 0/1。
"""
import base64
import http.cookiejar
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


def http_get(url, timeout=10):
    """GET 并返回 (status, body)；非 2xx 不抛异常（兜底页语义就是 503）。"""
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CFG_PATH = os.path.join(HERE, "client-build-config.json")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _minimize_windows_of(pid):
    """把指定进程的可见顶层窗口全部最小化（SW_MINIMIZE）；返回命中窗口数。

    供「最小化态几何不得落盘」断言用（真机曾把 159x27 存成真几何）。
    """
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    hits = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        wpid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and u.IsWindowVisible(hwnd):
            u.ShowWindow(hwnd, 6)                 # SW_MINIMIZE
            hits.append(int(hwnd))
        return True

    u.EnumWindows(WNDENUMPROC(_cb), 0)
    return hits

REQUIRED_BODY = [
    "_internal/client-build-config.json",
    "_internal/fallback.html",
    "_internal/relay_whitelist.json",
    "_internal/static/index.html",
    "_internal/static/tool.html",
    "_internal/kernel/jz_deps.py",
    "_internal/kernel/jz_api.py",
    "_internal/kernel/jztools_data.py",
]


def required_files(cfg):
    # exe 名从构建配置推导（产品名可改，不能写死）
    return [cfg["product"]["name"] + ".exe"] + REQUIRED_BODY
FORBIDDEN = [
    "_internal/static/../plugins",          # 主包业务插件不进客户端包
    "plugins/admin",
]


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def structure(out, cfg):
    required = required_files(cfg)
    for rel in required:
        if not os.path.isfile(os.path.join(out, rel.replace("/", os.sep))):
            fail("缺文件：%s" % rel)
    if os.path.isdir(os.path.join(out, "plugins")):
        fail("客户端包不应含 plugins/ 目录")
    # 插件 worker 运行时分件（T07 遗留销项，2026-10-08）：打包态 worker 靠它拉起
    if not os.path.isfile(os.path.join(out, "runtime", "python", "python.exe")):
        fail("缺插件 worker 运行时：runtime/python/python.exe（先跑 tools/client/build-client-runtime.py）")
    if not os.path.isdir(os.path.join(out, "runtime", "python", "Lib", "site-packages", "flask")):
        fail("worker 运行时缺 Flask 栈：runtime/python/Lib/site-packages/flask")
    print("结构断言通过（%d 项必需 + worker 运行时 + 禁带检查）" % len(required))


def smoke(out, cfg):
    name = cfg["product"]["name"]
    exe = os.path.join(out, name + ".exe")
    tmp = tempfile.mkdtemp(prefix="jzclient-smoke-")
    # 壳自证 run token（2026-10-11 需求① G2-6/G2-7）：台架固定注入，供 boot 断言
    shell_token = "smoke-" + secrets.token_hex(12)
    env = dict(os.environ, JZCLIENT_DATA_DIR=tmp, JZCLIENT_SHELL_TOKEN=shell_token)
    log_path = os.path.join(out, "client.log")
    for stale in ("client.log", "port.txt", "client.pid"):   # 硬杀残留会误导探针（陈旧端口）
        p = os.path.join(out, stale)
        if os.path.isfile(p):
            os.remove(p)

    def _log_tail():
        return open(log_path, encoding="utf-8", errors="replace").read()

    def _pid_file():
        try:
            t = open(os.path.join(out, "client.pid"), encoding="utf-8").read().strip()
            return int(t) if t.isdigit() else None
        except OSError:
            return None

    # 基准几何（供最小化纪律断言）：写一个正常尺寸，最小化后不得被「最小化尺寸」写脏
    with open(os.path.join(tmp, "geometry.json"), "w", encoding="utf-8") as f:
        json.dump({"x": 0, "y": 0, "width": 1200, "height": 800}, f)
    proc = subprocess.Popen([exe], env=env, cwd=out)
    try:
        port = None
        deadline = time.time() + 30
        while time.time() < deadline:
            # 优先读 port.txt（当前实例端口；client.log 是追加式，历史行会误导）
            port_file = os.path.join(out, "port.txt")
            if os.path.isfile(port_file):
                port = int(open(port_file, "r", encoding="utf-8").read().strip())
                break
            if proc.poll() is not None:
                fail("进程提前退出（code=%s），日志：\n%s" % (
                    proc.returncode, open(log_path, encoding="utf-8", errors="replace").read()
                    if os.path.isfile(log_path) else "<无>"))
            time.sleep(0.5)
        if not port:
            fail("30s 内未出现 port.txt")

        # UI 层健康断言（批次 2 教训：能力层先起、UI 层导入失败 → 进程数秒内自退）
        time.sleep(3)
        if proc.poll() is not None:
            fail("进程在能力层启动后退出（UI 层异常），日志尾部：\n" +
                 open(log_path, encoding="utf-8", errors="replace").read()[-800:])
        if "UI 层异常退出" in open(log_path, encoding="utf-8", errors="replace").read():
            fail("日志出现 UI 层异常退出")

        base = "http://127.0.0.1:%d" % port

        # 本地端口壳自证（2026-10-11 需求① G2-6）：无 Cookie 直连必须被拒；
        # 再走 /shell-boot（对/错 token 两例）种 Cookie，后续探针全部带 Cookie（与壳内同款）。
        code, body = http_get(base + "/local-api/health")
        if code != 403:
            fail("本地端口壳自证未生效：无 Cookie 直连 /local-api/health 应 403（实际 %d）" % code)
        code, body = http_get(base + "/shell-boot?_shell=wrong-token")
        if code != 403:
            fail("shell-boot 错误 token 应 403（实际 %d）" % code)
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        try:
            rb = opener.open(base + "/shell-boot?_shell="
                             + urllib.parse.quote(shell_token) + "&next=/", timeout=10)
            code, body = rb.status, rb.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:      # 兜底页语义就是 503（未配地址）
            code, body = e.code, e.read().decode("utf-8", "replace")
        if code != 503 or "JZCLIENT_FALLBACK" not in body:
            fail("shell-boot 应种 Cookie 后落回主页（未配地址=503 兜底页），实际 %d" % code)
        print("smoke：本地端口壳自证通过（无 Cookie 403 / 错 token 403 / boot 落回主页）")

        status, body = http_get(base + "/")      # 无 Cookie 直连（旧口径探针应同样被拒）
        if status != 403:
            fail("无 Cookie 直连 / 应 403（实际 %d）" % status)
        try:
            rb = opener.open(base + "/", timeout=10)
            status, body = rb.status, rb.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read().decode("utf-8", "replace")
        if status != 503 or "JZCLIENT_FALLBACK" not in body:
            fail("带壳 Cookie 访问 / 应返回 503+本地兜底页（实际 %d）" % status)
        try:
            r = opener.open(base + "/local-api/health", timeout=10)
            h = json.loads(r.read())
        except urllib.error.HTTPError as e:
            fail("/local-api/health 返回异常：%d" % e.code)
        if "remote_ok" not in h:
            fail("/local-api/health 返回异常：%s" % h)
        req = urllib.request.Request(
            base + "/local-api/config/address",
            data=json.dumps({"address": "192.168.1.10:5000"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        r = json.loads(opener.open(req, timeout=10).read())
        if not r.get("ok"):
            fail("写地址失败：%s" % r)
        cfgfile = os.path.join(tmp, "config.json")
        if not os.path.isfile(cfgfile):
            fail("地址未落地到客户端配置文件 config.json")

        # 窗口几何纪律（2026-10-08）：最小化态尺寸不得落盘——真机曾把 159x27 存成真几何，
        # 下次启动窗口只剩钳制下限。最小化本实例 → 等防抖落盘 → 断言未写脏。
        geom = os.path.join(tmp, "geometry.json")
        time.sleep(0.8)                       # 防抖 300ms：先让启动期的正常落盘完成
        before = _read_json(geom)
        hits = _minimize_windows_of(proc.pid)
        if hits:
            time.sleep(1.5)                   # 最小化 resized 事件 + 防抖窗口
            after = _read_json(geom)
            if after.get("width", 0) < 400 or after.get("height", 0) < 300:
                fail("最小化态几何被落盘：%s（最小化前 %s）" % (after, before))
            print("smoke：窗口最小化几何纪律通过（%s → %s，未被写脏）" % (before, after))
        else:
            print("smoke：未找到可最小化的窗口，跳过几何纪律断言")

        # 打包态 worker 冒烟（T07 遗留销项，2026-10-08）：自带运行时 python + 包内
        # plugin_host.py + 最小插件（flask 栈）走一次 stdio-json-v1 往返。这条路径过去只在
        # 源码态被台架覆盖——打包态曾因「exe 当不了脚本解释器」整体哑火而无人察觉。
        rt_py = os.path.join(out, "runtime", "python", "python.exe")
        host = os.path.join(out, "_internal", "plugin_host.py")
        pdir = os.path.join(tmp, "worker-probe", "local-probe")
        os.makedirs(os.path.join(pdir, "backend"), exist_ok=True)
        with open(os.path.join(pdir, "backend", "__init__.py"), "w", encoding="utf-8") as f:
            f.write("")
        with open(os.path.join(pdir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "local-probe", "runtime": "local", "kind": "local",
                       "entry": {"frontend": "index.html", "backend": "routes.py"}}, f)
        with open(os.path.join(pdir, "backend", "routes.py"), "w", encoding="utf-8") as f:
            f.write("import os\nfrom flask import jsonify\n\ndef register(app):\n"
                    "    @app.get('/ping')\n    def ping():\n"
                    "        return jsonify({'ok': True, 'pid': os.getpid()})\n")
        argv = [rt_py, host, "--plugin", pdir]
        pylibs = os.path.join(out, "runtime", "pylibs")
        if os.path.isdir(pylibs):
            argv += ["--pylibs", pylibs]        # 与执行器 _spawn 同口径
        wp = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, cwd=out)
        try:
            ob, eb = wp.communicate(input=(json.dumps({
                "request_id": "probe", "method": "GET", "path": "/ping",
                "headers": {}, "body_b64": ""}) + "\n").encode(), timeout=30)
        except subprocess.TimeoutExpired:
            wp.kill()
            fail("打包态 worker 超时无响应（T07 遗留销项回归）")
        lines = (ob or b"").decode("utf-8", "replace").strip().splitlines()
        if not lines:
            fail("打包态 worker 无响应（T07 遗留销项回归）：stderr=%s"
                 % (eb or b"").decode("utf-8", "replace")[-400:])
        resp = json.loads(lines[0])
        wbody = base64.b64decode(resp.get("body_b64") or "").decode("utf-8", "replace")
        if resp.get("status") != 200 or '"ok"' not in wbody:
            fail("打包态 worker 往返异常：status=%s body=%s" % (resp.get("status"), wbody[:200]))
        print("smoke：打包态 worker 往返通过（运行时 python + plugin_host + Flask 栈，status 200）")

        # 本机工具显示状态（2026-10-08）：桌面端「卸载」写 hidden_tools、「安装」恢复，
        # /api/tools 合并时按本机清单过滤——冒烟走一次写入 / 回传 / 清除。
        def _post_hidden(hidden):
            rq = urllib.request.Request(
                base + "/local-api/plugins/hidden",
                data=json.dumps({"id": "smoke-probe", "hidden": hidden}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            return json.loads(opener.open(rq, timeout=10).read())
        r = _post_hidden(True)
        if not r.get("ok") or "smoke-probe" not in (r.get("hidden_tools") or []):
            fail("本机显示状态写入失败：%s" % r)
        d = json.loads(opener.open(base + "/local-api/plugins", timeout=10).read())
        if "smoke-probe" not in (d.get("hidden_tools") or []):
            fail("/local-api/plugins 未回传 hidden_tools：%s" % d.get("hidden_tools"))
        r = _post_hidden(False)
        if not r.get("ok") or "smoke-probe" in (r.get("hidden_tools") or []):
            fail("本机显示状态清除失败：%s" % r)
        print("smoke：本机工具显示状态往返通过（写入 / 回传 / 清除）")

        # 自重启链路（2026-10-08 加固）：该端点曾因裸名 sys（NameError→500 HTML）从未
        # 生效过，前端只见「自动重启失败」。断言：旧进程退出 → 新实例带 --await-pid
        # 接棒（等旧进程真实退出再抢单实例互斥）→ 新实例起能力层。
        print("smoke：自重启链路断言（旧 pid=%s）…" % proc.pid)
        req = urllib.request.Request(base + "/local-api/restart", data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        r = json.loads(opener.open(req, timeout=10).read())
        if not r.get("ok"):
            fail("重启端点拒绝：%s" % r)

        old_pid = proc.pid
        new_pid = None
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:              # 旧进程已退出
                cur = _pid_file()
                if cur and cur != old_pid:
                    new_pid = cur
                    break
            time.sleep(0.5)
        if new_pid is None:
            fail("重启后 40s 内未见新实例接棒（旧进程 %s，日志尾部：\n%s）"
                 % ("已退出" if proc.poll() is not None else "仍在运行", _log_tail()[-600:]))
        deadline = time.time() + 30
        ok_new = False
        while time.time() < deadline:
            t = _log_tail()
            if "重启接棒" in t and "CAPABILITY listening" in t.split("重启接棒")[-1]:
                ok_new = True
                break
            time.sleep(0.5)
        if not ok_new:
            fail("新实例未在 30s 内完成能力层启动（日志尾部：\n%s）" % _log_tail()[-600:])

        print("smoke 通过：端口 %d，兜底页/本地接口/配置落盘/自重启接棒（%s→%s）全过"
              % (port, old_pid, new_pid))
        return 0
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                       capture_output=True)
        try:                                          # 重启后 proc 已死：按 pid 文件收尾新实例
            cur = _pid_file()
            if cur and cur != proc.pid:
                subprocess.run(["taskkill", "/PID", str(cur), "/F", "/T"], capture_output=True)
        except (OSError, ValueError):
            pass
        time.sleep(1)
        try:                                          # 兜底：smoke 只认自己的实例，残留即清
            p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s.exe" % name,
                                "/FO", "CSV", "/NH"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            tl = p.stdout or ""                       # tasklist 出 OEM 编码，按 utf-8 replace 兜底
            if name.lower() in tl.lower():
                print("注意：仍有 %s.exe 残留，按镜像名清理（smoke 专属实例）" % name)
                subprocess.run(["taskkill", "/IM", name + ".exe", "/F", "/T"], capture_output=True)
        except OSError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    out = os.path.join(ROOT, "deploy", "桌面端", cfg["product"]["name"])
    if not os.path.isdir(out):
        fail("产物目录不存在（先跑 tools/client/build-client.py）：%s" % out)
    structure(out, cfg)
    if "--smoke" in sys.argv:
        smoke(out, cfg)
    print("校验通过：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
