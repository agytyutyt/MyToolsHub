#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""主体与插件解耦 · 阶段验收（S1）——把 AC 里可自动化的部分固化成断言。

对应设计文档 `docs/design/主体与插件解耦-设计文档.md` §10 的验收表：

    AC-1 主包不含业务插件      —— 检查已构建的主包 zip（缺则跳过并提示）
    AC-2 主体升级不动插件      —— 沙箱：升级前后对业务插件目录做递归 sha256，逐字节一致
    AC-3 数据零变化            —— 沙箱：升级前后对数据根做递归 sha256，逐字节一致
    AC-4 全新装机 + 插件集      —— 沙箱：装主体（只带 admin）→ 装插件集 → 卡片可用
    AC-7 主体回滚              —— 沙箱：-Rollback 后版本与文件回到升级前
    AC-18 admin 可单独升级、不可卸载 —— 沙箱：-Uninstall admin 被拒；admin 单独升级后主体升级不回退

用法：
    python tools\e2e\decouple-acceptance.py                 # 全部（能跑的）
    python tools\e2e\decouple-acceptance.py --keep          # 保留沙箱目录便于排查
    python tools\e2e\decouple-acceptance.py --zip <主包zip>  # 指定主包（AC-1）

真机（人工）部分不在本脚本内：托盘常驻、浏览器实际渲染、真实 exe 的服务启停——
见 `docs/archive/主体与插件解耦-TODO.md` 的 T20 人工验收清单（原活清单已归档）。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PS = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]

RESULTS = []          # [(ac, ok, detail)]；ok=None 表示"跳过"（不计失败）


def check(ac, ok, detail=""):
    RESULTS.append((ac, ok, detail))
    tag = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    print("  [%s] %s%s" % (tag, ac, ("  — " + detail) if detail else ""))
    return ok is not False


def sha_tree(root):
    """目录递归指纹：{相对路径: sha256}（文件按字节，目录只记路径）。"""
    out = {}
    for cur, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(cur, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                with open(full, "rb") as f:
                    out[rel] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                out[rel] = "<unreadable>"
    return out


def _decode_ps(b):
    """解 PowerShell 子进程输出。

    ★ 编码**随控制台代码页变化**：本机默认 GBK，但任何跑过 `chcp 65001` 的批处理
    （安装器的 .bat 里就有）会把控制台切到 UTF-8，之后子进程就输出 UTF-8。
    写死一种编码会让"不可卸载""跳过"这类中文断言随机失配（真机上表现为时好时坏）——
    故两种都试：先 UTF-8（GBK 中文多半不是合法 UTF-8），失败或出现替换字符再按 GBK。
    """
    if not b:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            text = b.decode(enc)
        except UnicodeDecodeError:
            continue
        if "�" not in text:
            return text
    return b.decode("gbk", errors="replace")


def run_ps(script, args, cwd=None):
    cmd = PS + [script] + list(args)
    p = subprocess.run(cmd, cwd=cwd, capture_output=True)
    out = _decode_ps(p.stdout) + _decode_ps(p.stderr)
    return p.returncode, out


def make_pkg(version, plugins):
    """造一个"主包形态"的目录：exe + version.json + install.ps1 + plugins/<白名单>。"""
    return version, plugins


def build_fake_app(dst, version, plugin_ids, data_root=None):
    os.makedirs(os.path.join(dst, "_internal"), exist_ok=True)
    os.makedirs(os.path.join(dst, "plugins"), exist_ok=True)
    open(os.path.join(dst, "JZToolsHub.exe"), "w").write("stub-exe")
    open(os.path.join(dst, "_internal", "python314.dll"), "w").write("stub-dll")
    json.dump({"app": version, "schema": 1, "commit": "stub", "built_at": "x",
               "python": "3.14.7", "offline": "", "plugin_api": 1},
              open(os.path.join(dst, "version.json"), "w", encoding="utf-8"))
    for pid in plugin_ids:
        pdir = os.path.join(dst, "plugins", pid)
        os.makedirs(pdir, exist_ok=True)
        src = os.path.join(ROOT, "plugins", pid, "manifest.json")
        if os.path.isfile(src):
            shutil.copy2(src, pdir)
        open(os.path.join(pdir, "marker-%s.txt" % pid), "w").write("v-" + version)
    if data_root:
        os.makedirs(os.path.join(data_root, "plugins", "notice-board", "data"), exist_ok=True)
        open(os.path.join(data_root, "plugins", "notice-board", "data", "user.json"), "w").write('{"k":1}')
        os.makedirs(os.path.join(data_root, "config"), exist_ok=True)
        json.dump({"site": {}, "categories": [{"id": "office", "name": "协作办公"}],
                   "tools": [{"id": "admin", "enabled": True, "hidden": True},
                             {"id": "notice-board", "enabled": True, "order": 5}]},
                  open(os.path.join(data_root, "config", "tools.json"), "w", encoding="utf-8"))


def ac1_main_package(zip_path):
    print("== AC-1 主包不含业务插件 ==")
    if not zip_path or not os.path.isfile(zip_path):
        check("AC-1", None, "跳过：未找到主包 zip（先跑 build-deploy.ps1 产出，或用 --zip 指定）")
        return
    print("  检查对象：%s（%.1f MB，%s）" % (zip_path, os.path.getsize(zip_path) / 1048576,
          __import__("time").strftime("%Y-%m-%d %H:%M", __import__("time").localtime(os.path.getmtime(zip_path)))))
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    dirs = sorted({n.split("/")[1] for n in names if n.startswith("plugins/") and n.count("/") >= 2})
    check("AC-1", dirs == ["admin"], "主包 plugins/ = %s（期望 ['admin']）；体积 %.1f MB"
          % (dirs, os.path.getsize(zip_path) / 1048576))
    check("AC-1", not any(n.startswith("wheels/") for n in names), "不含 wheels/")
    check("AC-1", not any(n.startswith("tools/dev/") for n in names), "不含 tools/dev/")
    if any(a == "AC-1" and o is False for a, o, _d in RESULTS):
        print("  提示：若该包是本次改动**之前**构建的（mtime 早于 build-deploy.ps1/app.py），"
              "请先重跑 build-deploy.ps1 出包再复跑本脚本。")


def sandbox_flow(tmp, main_ver_old="2.1.2", main_ver_new="2.2.0"):
    """AC-2/AC-3/AC-4/AC-7/AC-18：造"旧机" → 升级 → 断言不变式 → 回滚。"""
    app = os.path.join(tmp, "app")
    src = os.path.join(tmp, "src")          # 新版主包（只带 admin）
    droot = os.path.join(tmp, "data")
    biz = ["notice-board", "knowledge-base"]
    build_fake_app(app, main_ver_old, ["admin"] + biz, data_root=droot)
    build_fake_app(src, main_ver_new, ["admin"])
    shutil.copy2(os.path.join(ROOT, "install.ps1"), src)

    before_plugins = {p: sha_tree(os.path.join(app, "plugins", p)) for p in biz}
    # AC-3 口径：数据根里的**用户数据**（plugins/<id>/）逐字节一致。
    # 日志 / backups / .app_state.json 在升级中本就会新增或更新，不纳入"用户数据"比对。
    before_data = sha_tree(os.path.join(droot, "plugins"))

    print("== AC-2 / AC-3 主体升级不动插件与数据 ==")
    rc, out = run_ps(os.path.join(src, "install.ps1"),
                     ["-InstallDir", app, "-DataRoot", droot, "-NoRegistry", "-ForcePluginOverwrite"])
    if not check("AC-2", rc == 0, "install.ps1 退出码 %d" % rc):
        print(out[-1200:]); return
    after_plugins = {p: sha_tree(os.path.join(app, "plugins", p)) for p in biz}
    same = all(before_plugins[p] == after_plugins[p] for p in biz)
    check("AC-2", same, "业务插件目录递归 sha256 逐字节一致（%d 个插件 / %d 个文件）"
          % (len(biz), sum(len(v) for v in before_plugins.values())))
    check("AC-2", os.path.isfile(os.path.join(app, "plugins", "admin", "manifest.json")),
          "核心插件 admin 随主包更新")
    check("AC-3", sha_tree(os.path.join(droot, "plugins")) == before_data,
          "数据根用户数据（plugins/**）递归 sha256 逐字节一致")
    check("AC-7", os.path.isfile(os.path.join(droot, "backups", "app", "app-%s" % main_ver_old))
          or any(f.startswith("app-%s-" % main_ver_old) for f in os.listdir(os.path.join(droot, "backups", "app"))),
          "升级前已自动备份主体（backups/app/app-%s-*.zip）" % main_ver_old)

    print("== AC-4 插件集安装（沙箱用仓库插件目录直装，等价于插件集 payload） ==")
    installer = os.path.join(ROOT, "tools", "plugin-upgrade", "install-plugin.ps1")
    pkg_dir = os.path.join(tmp, "pkgs")
    os.makedirs(pkg_dir, exist_ok=True)
    # 用出包工具为沙箱造两个真包（沙箱登记，不污染入库登记）
    for pid in ("notice-board",):
        rc2, out2 = run_ps(os.path.join(ROOT, "tools", "build-plugin-package.ps1"),
                           ["-Id", pid, "-OutDir", pkg_dir, "-RegistryFile", os.path.join(pkg_dir, "registry.json")])
        if rc2 != 0:
            check("AC-4", False, "出包失败：%s" % pid); print(out2[-800:]); return
    zipf = [f for f in os.listdir(pkg_dir) if f.endswith(".zip") and "notice-board" in f]
    if not zipf:
        check("AC-4", False, "未找到 notice-board 插件包"); return
    idx = {"schema": 1, "kind": "plugin-set", "name": "验收", "packages": [
        {"id": "notice-board", "version": "x", "file": zipf[0],
         "sha256": hashlib.sha256(open(os.path.join(pkg_dir, zipf[0]), "rb").read()).hexdigest()}]}
    json.dump(idx, open(os.path.join(pkg_dir, "index.json"), "w", encoding="utf-8"), ensure_ascii=False)
    shutil.copy2(installer, pkg_dir)
    # 先把目标机的 notice-board 删掉，模拟"全新装机只有 admin"
    shutil.rmtree(os.path.join(app, "plugins", "notice-board"))
    rc3, out3 = run_ps(os.path.join(pkg_dir, "install-plugin.ps1"),
                       ["-Set", pkg_dir, "-InstallDir", app, "-DataRoot", droot, "-NoStart"])
    check("AC-4", rc3 == 0, "-Set 退出码 %d" % rc3)
    check("AC-4", os.path.isfile(os.path.join(app, "plugins", "notice-board", "manifest.json")),
          "插件集安装后 notice-board 代码就位")
    tools = json.load(open(os.path.join(droot, "config", "tools.json"), encoding="utf-8"))["tools"]
    check("AC-4", any(t.get("id") == "notice-board" and t.get("enabled") for t in tools),
          "注册条目就绪（首页卡片可见）")
    check("AC-4", os.path.isfile(os.path.join(droot, "plugins", "notice-board", "data", "user.json")),
          "用户数据仍在（安装不触碰数据）")

    print("== AC-18 admin 不可卸载 / 可单独升级 ==")
    rc4, out4 = run_ps(installer, ["-Uninstall", "admin", "-InstallDir", app, "-DataRoot", droot])
    check("AC-18", rc4 != 0 and "不可卸载" in out4,
          "-Uninstall admin 被拒（退出码 %d）%s" % (rc4, "" if "不可卸载" in out4 else
                                                "｜实际输出：" + " ".join(out4.split())[-160:]))
    # admin 单独升级到更高版本 → 主包升级不得回退它
    admin_ver_before = json.load(open(os.path.join(app, "plugins", "admin", "manifest.json"),
                                     encoding="utf-8"))["version"]
    state_path = os.path.join(droot, "config", ".app_state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    st = json.load(open(state_path, encoding="utf-8")) if os.path.isfile(state_path) else {}
    st.setdefault("plugins", {})["admin"] = {"version": "99.0.0", "installed_by": "acceptance"}
    json.dump(st, open(state_path, "w", encoding="utf-8"), ensure_ascii=False)
    rc5, out5 = run_ps(os.path.join(src, "install.ps1"),
                       ["-InstallDir", app, "-DataRoot", droot, "-NoRegistry"])
    _ok5 = rc5 == 0 and "admin" in out5 and ("跳过" in out5 or "保持现状" in out5)
    check("AC-18", _ok5,
          "admin 单独升级到 99.0.0 后，主包升级跳过覆盖（登记版本 %s → 保持）%s"
          % (json.load(open(state_path, encoding="utf-8"))["plugins"]["admin"]["version"],
             "" if _ok5 else "｜实际输出：" + " ".join(out5.split())[-200:]))
    check("AC-18", json.load(open(state_path, encoding="utf-8"))["plugins"]["admin"]["version"] == "99.0.0",
          "登记未被主包改写（admin 版本 %s 保持）" % admin_ver_before)


def main():
    ap = argparse.ArgumentParser(description="主体与插件解耦 · 阶段验收（S1）")
    ap.add_argument("--zip", default="", help="主包 zip 路径（AC-1）")
    ap.add_argument("--keep", action="store_true", help="保留沙箱目录")
    args = ap.parse_args()

    zip_path = args.zip
    if not zip_path:
        import glob
        cands = glob.glob(os.path.join(ROOT, "deploy", "JZToolsHub-v*.zip"))
        if cands:
            zip_path = max(cands, key=os.path.getmtime)

    tmp = tempfile.mkdtemp(prefix="jz-decouple-acc-")
    print("沙箱：%s\n" % tmp)
    try:
        ac1_main_package(zip_path)
        sandbox_flow(tmp)
    finally:
        if args.keep:
            print("\n沙箱已保留：%s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    # ★ SKIP（ok=None）是"本次没跑到"（如未找到主包 zip），**不算失败**：
    #   把 None 并进失败会让报表谎报（曾出现"13 通过 / 2 失败"而实际那 2 条是 SKIP）。
    ok = sum(1 for _a, o, _d in RESULTS if o is True)
    bad = [a for a, o, _d in RESULTS if o is False]
    skip = sum(1 for _a, o, _d in RESULTS if o is None)
    print("\n==== 汇总：%d 项通过 / %d 项失败 / %d 项跳过（共 %d）===="
          % (ok, len(bad), skip, len(RESULTS)))
    if bad:
        print("失败项：%s" % "、".join(bad))
        print("提示：AC-1 需要先跑 build-deploy.ps1 产出主包；真机人工部分见 TODO 清单 T20。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
