#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""主包校验：结构 + 内容 + 包内文档一致性（S11）+ （可选）解压冒烟。出包后必跑。

用法：
    python tools\verify-package.py                          # 校验 deploy 下最新的 JZToolsHub-v*.zip
    python tools\verify-package.py deploy\JZToolsHub-v2.0.0.zip
    python tools\verify-package.py deploy\服务端\JZToolsHub   # 目标也可以是已解压的部署目录（暂存/冒烟目录）
    python tools\verify-package.py ... --smoke               # 再加「解压 → 隔离数据根启动 → HTTP 断言」
    python tools\verify-package.py ... --docs-only           # 只跑包内文档 ↔ 仓库源逐字节一致性校验

包内文档一致性（S11，2026-10-09）：目标 zip / 部署目录内的文档层——README.md、HANDOFF.md、
插件设计规范.md、移动端APP.md、docs/README.md、docs/项目管理手册.md、docs/guide/*.md、
docs/design/*.md——与仓库源按 CRLF→LF 归一后逐字节比对，列出全部不一致（包内缺失 / 内容滞后 /
增量合并残留的多余 .md）。**不一致即判失败（不降级 warning）：包内手册是交付物的一部分，
「包内文档滞后于源」= 用户按旧口径操作，必须拦下。** 组装目录是增量合并（build-deploy-local.py
从不删库），仓库删名/改名留下的旧文档只有这里能发现；处置 = 把源文档同步进组装目录后重新出包。

--smoke 会自动挑一个**空闲端口**（不碰本机 5000 上正在跑的服务），用
JZTOOLS_DATA_ROOT 指向临时目录（**不污染真实数据根** `%USERPROFILE%\.jztoolshub`），
断言首页 200 / 登录页 200 / 匿名 /api/* 401 后杀掉进程。冒烟目录默认保留（批量删除会撞
工具会话的 safe-delete），路径会在结尾打印。
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 与控制台编码和解：校验标签里有 ⊆ / ✅ 这类字符，GBK 控制台（Windows 缺省）会在 print 处抛
# UnicodeEncodeError——而且本次是在**打印一条通过项**时抛，等于把后面所有校验（含 --smoke 的
# 解压启动断言）整段吞掉，看起来像"校验提前结束"而不是"校验失败"。与 lint-static-rules.py /
# v-acceptance-audit.py 同口径处理。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REQUIRED = ["JZToolsHub.exe", "start.bat", "一键安装.bat", "一键卸载.bat", "install.ps1",
            "config/tools.json", "config/installed-deps.json", "version.json", "README.md", "HANDOFF.md",
            "插件设计规范.md", "移动端APP.md", "docs/README.md", "docs/项目管理手册.md",
            "_internal/python3*"]
TEMPLATE_MIN = 0   # 解耦后主包只带核心插件（admin 无 *.template.json）；模板随业务插件包分发
# 核心插件白名单：主包 plugins/ 内只允许出现它（口径与 build-deploy-local.py 的 CORE_PLUGINS 一致）
CORE_PLUGINS = ["admin", "store"]
# 不随主包（解耦后）：业务插件目录、wheels（无运行时消费方）、tools 下除 plugin-upgrade 的子目录
ALLOWED_TOOLS_SUBDIRS = ["plugin-upgrade"]
BAD_IN_PLUGINS = re.compile(r"(__pycache__|/data/|/out/|\.task_cache/|/config\.json$|\.pyc$)")
# 不随包清单：内部文档层（docs/eval|plan|archive）与插件内测试脚本（口径见
# tools/plugin-payload-rules.json 的 _comment 与 tools/build-deploy-local.py 的 COPY_DIRS）
FORBIDDEN_IN_PACKAGE = re.compile(r"^(docs/(eval|plan|archive)/|plugins/.+/test_.*\.py$|plugins/.+/conftest\.py$)")

# S11 包内文档层（口径 = tools/build-deploy-local.py 的 COPY_FILES + COPY_DIRS：4 份顶层 + 2 份 docs 索引 + guide/design 两目录）
DOC_SINGLES = ["README.md", "HANDOFF.md", "插件设计规范.md", "移动端APP.md",
               "docs/README.md", "docs/项目管理手册.md"]
DOC_DIRS = ["docs/guide", "docs/design"]


def norm_bytes(data):
    """CRLF→LF 归一（口径与 docs/design/同源副本台账.md §1 的 norm 一致），供文档逐字节比对。"""
    return data.replace(b"\r\n", b"\n")


def repo_doc_files():
    """仓库侧应随包的文档全集（相对路径，POSIX 分隔；真源 = 仓库，不以包内为准）。"""
    rels = list(DOC_SINGLES)
    for d in DOC_DIRS:
        for p in glob.glob(os.path.join(ROOT, d.replace("/", os.sep), "*.md")):
            rels.append(os.path.relpath(p, ROOT).replace(os.sep, "/"))
    return sorted(rels)


def docs_sync_check(src, check):
    """S11：包内文档层 ↔ 仓库源逐字节一致（CRLF→LF 归一后）。

    失败语义 = 报错拦下（check 记失败 → 非零退出），**不降级 warning**。理由：包内手册 /
    设计文档是交付物的一部分，「包内滞后于源」= 给用户发旧口径（S11 立项即按"应拦下"定性）；
    且组装目录为增量合并（build-deploy-local.py 从不删库），仓库删名/改名留下的旧文档只有
    这里能发现。处置：同步源文档进组装目录后重新压缩出包。
    """
    rels = repo_doc_files()
    pkg = set(src.files)
    mism = []
    for rel in rels:
        local = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(local):
            mism.append("仓库源缺失（glob 口径失真，请检查）：%s" % rel)
            continue
        if rel not in pkg:
            mism.append("包内缺：%s" % rel)
            continue
        with open(local, "rb") as f:
            a = norm_bytes(f.read())
        b = norm_bytes(src.read(rel))
        if a != b:
            mism.append("内容不一致：%s（包内 %d 字节 vs 源 %d 字节，归一后）" % (rel, len(b), len(a)))
    expect = set(rels)
    for n in sorted(pkg):
        if any(n.startswith(d + "/") for d in DOC_DIRS) and n.endswith(".md") and n not in expect:
            mism.append("包内多出（仓库已无此源，疑似增量合并残留）：%s" % n)
    if mism:
        for m in mism:
            print("       - %s" % m)
        check(False, "包内文档与仓库源逐字节一致（CRLF→LF 归一；%d 份）" % len(rels),
              "%d 处不一致（清单见上）" % len(mism))
    else:
        check(True, "包内文档与仓库源逐字节一致（CRLF→LF 归一；%d 份）" % len(rels))


class PkgSource:
    r"""校验目标抽象：zip 包 或 已解压的部署目录（暂存目录 / 冒烟目录）。

    两者对校验流程暴露同一组只读操作（names/files/read/testzip/extractall），让「部署目录 /
    zip」走同一套断言 —— S11 文档一致性校验也因此能直接对 deploy\服务端\JZToolsHub 暂存目录
    预检，不必先压包。
    """

    def __init__(self, path):
        self.path = path
        self.is_zip = os.path.isfile(path)
        if self.is_zip:
            self._zip = zipfile.ZipFile(path)
            self.names = self._zip.namelist()
        else:
            self._zip = None
            self.names = []
            for dirpath, dirnames, filenames in os.walk(path):
                rel = os.path.relpath(dirpath, path).replace(os.sep, "/")
                prefix = "" if rel == "." else rel + "/"
                for d in sorted(dirnames):
                    self.names.append(prefix + d + "/")
                for f in sorted(filenames):
                    self.names.append(prefix + f)
        self.files = [n for n in self.names if not n.endswith("/")]

    def read(self, name):
        if self.is_zip:
            return self._zip.read(name)
        with open(os.path.join(self.path, name.replace("/", os.sep)), "rb") as f:
            return f.read()

    def testzip(self):
        return self._zip.testzip() if self.is_zip else None

    def extractall(self, dest):
        if self.is_zip:
            self._zip.extractall(dest)
        else:
            shutil.copytree(self.path, dest)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(port, path, timeout=5):
    url = "http://127.0.0.1:%d%s" % (port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return None, b""


def main():
    ap = argparse.ArgumentParser(description="主包校验")
    ap.add_argument("zipfile", nargs="?", default="", help="zip 路径或已解压的部署目录（缺省取 deploy 下最新的 JZToolsHub-v*.zip）")
    ap.add_argument("--smoke", action="store_true", help="解压 + 隔离数据根启动 + HTTP 断言")
    ap.add_argument("--docs-only", dest="docs_only", action="store_true",
                    help="只跑包内文档 ↔ 仓库源一致性校验（S11）")
    args = ap.parse_args()

    path = args.zipfile
    if not path:
        cands = [p for p in glob.glob(os.path.join(ROOT, "deploy", "服务端", "JZToolsHub-v*.zip"))]
        if not cands:
            raise SystemExit("deploy 下没有 JZToolsHub-v*.zip")
        path = max(cands, key=os.path.getmtime)
    path = os.path.abspath(path)
    if os.path.isdir(path):
        size = sum(os.path.getsize(os.path.join(dp, f))
                   for dp, _dn, fn in os.walk(path) for f in fn)
        print("校验目标：%s（部署目录，%.1f MB）" % (path, size / 1048576))
    else:
        print("校验目标：%s（%.1f MB）" % (path, os.path.getsize(path) / 1048576))

    fails = []

    def check(ok, label, detail=""):
        print("  %s %s%s" % ("[OK]" if ok else "[!!]", label, ("  — " + detail) if detail else ""))
        if not ok:
            fails.append(label)

    z = PkgSource(path)
    names = z.names
    files = z.files

    if args.docs_only:
        docs_sync_check(z, check)
        print()
        if fails:
            print("校验失败：%d 项 —— %s" % (len(fails), "；".join(fails)))
            return 1
        print("校验通过：全部检查项绿。")
        return 0

    if z.is_zip:
        check(z.testzip() is None, "zip CRC 全量校验")
    else:
        print("  [--] 目录目标：跳过 zip CRC 全量校验（逐文件内容比对照常）")
    # 阈值随解耦下调：主包不再携带业务插件（-250 条目）、pandas（-900 条目）、cv2/numpy（-1500 条目）。
    # 实测 v2.2.0 主包 1095 个文件（解耦前 3031），当前实测 542 个文件；阈值取 250
    # 用于兜住"打错目录/漏拷"这类事故。
    check(len(files) > 250, "文件数合理", "%d 文件 / %d 条目" % (len(files), len(names)))
    top = set(n.split("/")[0] for n in names)
    check("JZToolsHub" not in top and "dist" not in top,
          "无顶层目录前缀（解压即见 JZToolsHub.exe）", "顶层：%d 项" % len(top))

    for pat in REQUIRED:
        if "*" in pat:
            check(any(fnmatch_like(n, pat) for n in names), "存在 %s" % pat)
        else:
            check(pat in names, "存在 %s" % pat)
    if "_internal/python3" not in names and not any(n.startswith("_internal/python3") for n in names):
        check(False, "存在 _internal/python3*.dll")

    v = {}
    try:
        v = json.loads(z.read("version.json").decode("utf-8-sig"))
        check(True, "version.json 可解析（UTF-8 无 BOM）", json.dumps(v, ensure_ascii=False))
    except Exception as e:
        check(False, "version.json 可解析", str(e))
    for k in ("app", "schema", "commit", "built_at", "python", "offline", "plugin_api"):
        check(k in v, "version.json 含字段 %s" % k)
    check(bool(re.match(r"^\d+\.\d+\.\d+$", str(v.get("app", "")))), "app 为三段式版本号", str(v.get("app")))
    check(isinstance(v.get("plugin_api"), int) and v["plugin_api"] >= 1,
          "plugin_api 为 >=1 的整数（插件 API 版本，真源 jz_deps.PLUGIN_API）", str(v.get("plugin_api")))
    check("-dirty" not in str(v.get("commit", "")), "commit 无 -dirty 干净戳", str(v.get("commit")))

    # 已安装依赖清单（插件依赖判定的真源，见 docs/design/主体与插件解耦-设计文档.md §5.3）
    deps = {}
    try:
        deps = json.loads(z.read("config/installed-deps.json").decode("utf-8-sig"))
        ok = isinstance(deps.get("packages"), dict) and bool(deps["packages"])
        check(ok, "已安装依赖清单可解析且非空",
              "%d 个包 / %d 条 import 映射" % (len(deps.get("packages", {})), len(deps.get("modules", {}))))
    except Exception as e:
        check(False, "已安装依赖清单可解析（config/installed-deps.json）", str(e))
    check(isinstance(deps.get("modules"), dict) and bool(deps.get("modules")),
          "已安装依赖清单含 import→发行包 映射（modules）")
    # 清单**只许**记录随包分发的包（scope=shipped）：若把构建机环境的包也写进来，
    # 干净机上插件声明的依赖会被误判为"已满足"，缺依赖提示与红 × 都不出现
    # （2026-09-20 修复：曾写入 93 个构建机包，实际随包只有 9 个）。
    check(deps.get("scope") == "shipped",
          "已安装依赖清单 scope=shipped（不得记录构建机独有包）", "scope=%r" % deps.get("scope"))
    # 只取**顶层** dist-info（`_internal/<name>-<ver>.dist-info/METADATA`）；
    # 嵌套的 vendored 副本（如 _internal/setuptools/_vendor/…）不算随包分发的包。
    shipped = sorted({n.split("/")[1][: -len(".dist-info")].rsplit("-", 1)[0].lower()
                      for n in names
                      if n.startswith("_internal/") and n.count("/") == 2
                      and n.endswith(".dist-info/METADATA")})
    listed = sorted({str(k).lower().replace("_", "-") for k in (deps.get("packages") or {})})
    extra = [x for x in listed if x not in shipped]
    missing = [x for x in shipped if x not in listed]
    check(not extra, "清单包集合 ⊆ 随包分发集合（_internal 的 dist-info）",
          ("清单多出：%s" % ", ".join(extra)) if extra else "实际：%d 个包，与 _internal 一致" % len(listed))
    check(not missing, "随包分发的包都在清单里", ("清单缺：%s" % ", ".join(missing)) if missing else "实际：%d 个" % len(shipped))

    tpl = [n for n in files if n.endswith(".template.json")]
    check(len(tpl) >= TEMPLATE_MIN, "配置模板 >= %d" % TEMPLATE_MIN, "实际 %d：%s" % (len(tpl), ", ".join(tpl)))

    # 主体与插件解耦（S1）：主包只带核心插件；业务插件走插件包 / 插件集
    # （见 docs/design/主体与插件解耦-设计文档.md §3.3 / §7 #4）
    plugin_dirs = sorted({n.split("/")[1] for n in names if n.startswith("plugins/") and n.count("/") >= 2})
    stray = [d for d in plugin_dirs if d not in CORE_PLUGINS]
    check(not stray, "主包 plugins/ 仅含核心插件 %s" % "/".join(CORE_PLUGINS),
          ("非核心插件：%s" % ", ".join(stray)) if stray else "实际：%s" % ", ".join(plugin_dirs))
    check("wheels/" not in names and not any(n.startswith("wheels/") for n in names),
          "不含 wheels/（无运行时消费方，解耦后移出主包）")
    bad_tools = sorted({n.split("/")[1] for n in names
                        if n.startswith("tools/") and n.count("/") >= 2
                        and n.split("/")[1] not in ALLOWED_TOOLS_SUBDIRS})
    check(not bad_tools, "tools/ 仅含 %s" % "/".join(ALLOWED_TOOLS_SUBDIRS),
          ("多余子目录：%s" % ", ".join(bad_tools)) if bad_tools else "")

    bad = [n for n in names if n.startswith("plugins/") and BAD_IN_PLUGINS.search(n)]
    check(not bad, "plugins/ 无运行态夹带（data/__pycache__/config.json/*.pyc）",
          ("夹带 %d 项：%s" % (len(bad), bad[:3])) if bad else "")

    leaked = [n for n in names if FORBIDDEN_IN_PACKAGE.search(n)]
    check(not leaked, "无内部文档层 / 插件测试脚本随包（docs eval·plan·archive、test_*.py）",
          ("夹带 %d 项：%s" % (len(leaked), leaked[:3])) if leaked else "")

    # 随包交付层非空断言（S11）：REQUIRED 只查索引与根契约文档，整目录漏拷 docs/guide、docs/design
    # 此前不会被拦下（S11 实证漏洞）。口径 = 仓库顶层 .md 实况（2026-10-09 复核：docs/guide 6 份，
    # 含新增《桌面端与商店真机验收清单》；docs/design 19 份；design 内 过滤器记忆机制图.svg 非 .md，不计）。
    # 阈值取比实况低一档的 >= 5 防「整目录漏拷」（该场景必红），份数增加不误报；
    # 本值按 docs/README.md §3.1「以定义处为准、不复述份数」的纪律 —— 口径即此注释，实况变化时更新注释。
    guide_md = [n for n in files if n.startswith("docs/guide/") and n.endswith(".md")]
    check(len(guide_md) >= 5, "docs/guide/ 随包 .md >= 5 份（交付层未整目录漏拷）",
          "实际 %d 份：%s" % (len(guide_md), ", ".join(sorted(guide_md))))
    design_md = [n for n in files if n.startswith("docs/design/") and n.endswith(".md")]
    check(len(design_md) >= 16, "docs/design/ 随包 .md >= 16 份（设计层未整目录漏拷）",
          "实际 %d 份" % len(design_md))

    # 包内文档 ↔ 仓库源（S11）：任何不一致拦下；失败语义与理由见 docs_sync_check docstring
    docs_sync_check(z, check)

    try:
        bat = z.read("start.bat").decode("gbk")
        check("JZToolsHub.exe" in bat, "start.bat 为 GBK 且内容正确")
    except Exception as e:
        check(False, "start.bat 为 GBK", str(e))

    print("  [--] zip sha256 = %s" % hashlib.sha256(open(path, "rb").read()).hexdigest())

    if args.smoke:
        print("==> 解压冒烟（端口自动挑选，数据根隔离）")
        work = os.path.join(os.environ.get("TEMP", "."), "jz_verify_%d" % os.getpid())
        droot = os.path.join(work, "_dataroot")
        dest = os.path.join(work, "app")
        t0 = time.time()
        z.extractall(dest)
        print("  解压完成 %.1fs → %s" % (time.time() - t0, dest))
        port = free_port()
        env = dict(os.environ, JZTOOLS_DATA_ROOT=droot, JZTOOLS_PORT=str(port), JZTOOLS_HOST="127.0.0.1")
        log = open(os.path.join(work, "smoke.log"), "wb")
        proc = subprocess.Popen([os.path.join(dest, "JZToolsHub.exe")], cwd=dest, env=env,
                               stdout=log, stderr=subprocess.STDOUT)
        up = None
        for i in range(30):
            code, _ = http(port, "/")
            if code == 200:
                up = i * 2
                break
            if proc.poll() is not None:
                break
            time.sleep(2)
        check(up is not None, "服务启动并响应首页 200", "%s 秒（端口 %d）" % (up, port) if up is not None else "启动失败/超时")
        if up is not None:
            check(http(port, "/login")[0] == 200, "登录页 200")
            check(http(port, "/api/tools")[0] == 401, "匿名 /api/tools 401（强制登录生效）")
            check(os.path.isdir(droot), "隔离数据根已生成", droot)
            check(os.path.exists(os.path.join(droot, "config", "admin.json")), "数据根 config/admin.json 已生成")
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        log.close()
        data = open(os.path.join(work, "smoke.log"), "rb").read()
        if data:
            print("  ---- 进程日志尾部 ----")
            print(data.decode("utf-8", "replace")[-800:])
        print("  冒烟目录（可手工删除）：%s" % work)

    print()
    if fails:
        print("校验失败：%d 项 —— %s" % (len(fails), "；".join(fails)))
        return 1
    print("校验通过：全部检查项绿。")
    return 0


def fnmatch_like(name, pat):
    import fnmatch
    return fnmatch.fnmatch(name, pat)


if __name__ == "__main__":
    sys.exit(main())
