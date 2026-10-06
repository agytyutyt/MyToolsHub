# -*- coding: utf-8 -*-
"""JZClient 出包脚本（批次 1 T07）。

用法（打包机）：python tools/client/build-client.py [--skip-build]
流程：读构建配置单源 → PyInstaller 跑 client/JZClient.spec → 组装 deploy/JZClient/
→ 写 build-info.json。**不改主包出包链**（docs/guide/打包部署手册.md 命令真源不受影响）。
内核包（webview2 fixed）与组件包为独立分件，另行分发（T07 三分件口径）。
"""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CFG_PATH = os.path.join(HERE, "client-build-config.json")
SPEC = os.path.join(ROOT, "client", "JZClient.spec")


def main():
    skip_build = "--skip-build" in sys.argv
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    product = cfg["product"]
    name = product["name"]
    version = product["version"]

    dist = os.path.join(ROOT, "client", "dist", name)
    out = os.path.join(ROOT, "deploy", name)

    if not skip_build:
        print("[1/3] PyInstaller 打包…")
        cmd = [sys.executable, "-m", "PyInstaller",
               "--noconfirm", "--clean", "--distpath",
               os.path.join(ROOT, "client", "dist"),
               "--workpath", os.path.join(ROOT, "client", "build"),
               SPEC]
        r = subprocess.run(cmd, cwd=ROOT)
        if r.returncode != 0:
            print("PyInstaller 失败，退出码 %d" % r.returncode)
            return 1
    else:
        print("[1/3] 跳过构建（--skip-build）")

    if not os.path.isfile(os.path.join(dist, name + ".exe")):
        print("产物不存在：%s" % dist)
        return 1

    print("[2/3] 组装 %s" % out)
    # 内核/程序解耦（T31）：runtime\ 子树（webview2 fixed 内核、embeddable python、pylibs）
    # 属独立分件，程序更新**不得触碰**——rmtree 前搬到 out 之外的同级暂存目录、拷贝后还原。
    runtime_src = os.path.join(out, "runtime")
    runtime_keep = os.path.join(os.path.dirname(out),
                                os.path.basename(out) + "-runtime-keep")
    if os.path.isdir(runtime_src):
        if os.path.isdir(runtime_keep):
            shutil.rmtree(runtime_keep)
        shutil.move(runtime_src, runtime_keep)
    # 目标目录可能被资源管理器/杀软短暂占用：宽容清理后按覆盖方式拷贝
    for _ in range(3):
        try:
            if os.path.isdir(out):
                shutil.rmtree(out)
            break
        except PermissionError:
            time.sleep(2)
    else:
        print("警告：%s 清理失败（可能被占用），改为覆盖拷贝" % out)
    shutil.copytree(dist, out, dirs_exist_ok=True)
    if os.path.isdir(runtime_keep):
        if os.path.isdir(runtime_src):
            shutil.rmtree(runtime_keep, ignore_errors=True)   # dist 自带 runtime 时以新为准
        else:
            shutil.move(runtime_keep, runtime_src)
            print("runtime\\ 子树已保留（内核与程序解耦）")

    print("[3/3] 写 build-info")
    info = {
        "product": product,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "webview2": cfg.get("webview2"),
    }
    with open(os.path.join(out, "build-info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    # F2（批次 9 T-O9-6）：客户端运行时依赖清单——同源 gen-installed-deps 按客户端冻结
    # 目录采集（jz_deps 三态判定数据源；kernel/config/installed-deps.json）。B-heavy 插件
    # requires 的 flask 在此落"已安装"，否则装包预检误报缺依赖。
    # 注意：该工具退出码含「服务器 spec 白名单核对」（PIL/pystray/waitress 等服务器侧包
    # 不在客户端 _internal 属预期），故成功判据=产物可解析，白名单警告随 stdout 可见。
    kernel_cfg = os.path.join(out, "_internal", "kernel", "config")
    os.makedirs(kernel_cfg, exist_ok=True)
    lock = os.path.join(kernel_cfg, "installed-deps.json")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "gen-installed-deps.py"),
                        "--internal", os.path.join(out, "_internal"), "--out", lock],
                       capture_output=True, text=True)
    print((r.stdout or "").strip())
    lock_ok = False
    try:
        with open(lock, encoding="utf-8-sig") as f:
            lock_ok = bool(json.load(f).get("packages"))
    except Exception:
        pass
    if not lock_ok:
        print("警告：客户端 installed-deps 未生成（依赖三态判定将按缺失处理）：%s" % (r.stderr or "").strip())

    total = sum(os.path.getsize(os.path.join(dp, fn))
                for dp, _, fns in os.walk(out) for fn in fns)
    print("完成：%s（%.1f MB）" % (out, total / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
