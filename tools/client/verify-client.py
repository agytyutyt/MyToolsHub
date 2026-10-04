# -*- coding: utf-8 -*-
"""JZClient 包校验（批次 1 T07）：结构断言 + --smoke（与主包 verify-package.py 同风格）。

结构断言：产物存在、构建配置/兜底页/主界面副本/内核模块随包、无主包业务插件混入。
--smoke：临时数据目录启动 exe → 等 client.log 的 CAPABILITY 行 → 探测
  / （未配地址 → 兜底页）与 /local-api/health → 退出码 0/1。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
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
    print("结构断言通过（%d 项必需 + 禁带检查）" % len(required))


def smoke(out, cfg):
    name = cfg["product"]["name"]
    exe = os.path.join(out, name + ".exe")
    tmp = tempfile.mkdtemp(prefix="jzclient-smoke-")
    env = dict(os.environ, JZCLIENT_DATA_DIR=tmp)
    log_path = os.path.join(out, "client.log")
    if os.path.isfile(log_path):
        os.remove(log_path)
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
        status, body = http_get(base + "/")
        if status != 503 or "JZCLIENT_FALLBACK" not in body:
            fail("未配地址时 / 应返回 503+本地兜底页（实际 %d）" % status)
        status, body = http_get(base + "/local-api/health")
        h = json.loads(body)
        if "remote_ok" not in h:
            fail("/local-api/health 返回异常：%s" % h)
        req = urllib.request.Request(
            base + "/local-api/config/address",
            data=json.dumps({"address": "192.168.1.10:5000"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        r = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if not r.get("ok"):
            fail("写地址失败：%s" % r)
        cfgfile = os.path.join(tmp, "config.json")
        if not os.path.isfile(cfgfile):
            fail("地址未落地到客户端配置文件 config.json")
        print("smoke 通过：端口 %d，兜底页/本地接口/配置落盘全过" % port)
        return 0
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                       capture_output=True)
        time.sleep(1)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    out = os.path.join(ROOT, "deploy", cfg["product"]["name"])
    if not os.path.isdir(out):
        fail("产物目录不存在（先跑 tools/client/build-client.py）：%s" % out)
    structure(out, cfg)
    if "--smoke" in sys.argv:
        smoke(out, cfg)
    print("校验通过：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
