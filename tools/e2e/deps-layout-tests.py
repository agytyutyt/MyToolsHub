# -*- coding: utf-8 -*-
r"""jz_deps 布局候选台架（2026-10-08）：客户端打包态也能找到「主体自带清单」与「组件清单」。

背景：清单在客户端打包态位于 <程序目录>/_internal/kernel/config/installed-deps.json，
而预检按 app_dir=<程序目录> 找 <程序目录>/config/ → 找不到 → 依赖全判 unknown →
商店安装必弹「缺少依赖」（真机报告：装 shared-docs）。修法=jz_deps._layout_roots 候选。

三种形态临时模拟（jz_deps.py 拷贝到对应位置再 import，__file__ 即该位置）：
  ① 主包/服务端：config/ 与 runtime/pylibs/ 都在根，调用 app_dir=<root>；
  ② 客户端打包态：清单在 <root>/_internal/kernel/config/，pylibs 在 <root>/runtime/pylibs/，
     调用 app_dir=<root>（预检口径）——两者都要找到；
  ③ worker 形态：jz_deps 从 <root>/_internal/kernel/ 加载、app_dir 缺省（=内核目录）——
     清单在旁要找到，pylibs 经父级两层要找到。
另证负例：裸目录（什么都没有）→ available=False（不误报）。
"""
import importlib.util
import io
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JZ_DEPS_SRC = os.path.join(ROOT, "jz_deps.py")
TMP = os.path.join(ROOT, ".workbuddy", "tmp", "deps-layout")

FAILS = []
TOTAL = []


def check(name, cond, extra=""):
    TOTAL.append(name)
    print(("ok   " if cond else "FAIL ") + name + (("   " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(name)


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def put_jz_deps(dst_dir):
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "jz_deps.py")
    shutil.copy2(JZ_DEPS_SRC, dst)
    return dst


def load_mod(jz_path, name):
    spec = importlib.util.spec_from_file_location(name, jz_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


INSTALLED = {"schema": 1, "packages": {"Flask": "3.1.3"}, "modules": {"flask": "Flask"}}
COMPONENT = {"components": [{"id": "cv2", "version": "5.0.0.93",
                             "packages": {"opencv-python": "5.0.0.93"},
                             "modules": {"cv2": "opencv-python"}}]}


def assert_full(d, tag):
    check(tag + "：available", d.get("available") is True, d.get("path"))
    check(tag + "：flask→Flask 映射", (d.get("modules") or {}).get("flask") == "Flask", d.get("modules"))
    check(tag + "：Flask 版本", (d.get("packages") or {}).get("flask") == "3.1.3", d.get("packages"))
    check(tag + "：组件 cv2 合并", (d.get("modules") or {}).get("cv2") == "opencv-python", d.get("modules"))
    check(tag + "：组件包名合并", (d.get("packages") or {}).get("opencv-python") == "5.0.0.93", d.get("packages"))


if os.path.isdir(TMP):
    shutil.rmtree(TMP)
try:
    # ① 主包/服务端形态
    r1 = os.path.join(TMP, "mainpkg")
    m1_path = put_jz_deps(r1)
    write(os.path.join(r1, "config", "installed-deps.json"), INSTALLED)
    write(os.path.join(r1, "runtime", "pylibs", "manifest.json"), COMPONENT)
    m1 = load_mod(m1_path, "jz_deps_case1")
    assert_full(m1.load_installed_deps(app_dir=r1), "①主包形态")

    # ② 客户端打包态：内核在 _internal/kernel/，pylibs 在程序目录
    r2 = os.path.join(TMP, "client")
    k2 = os.path.join(r2, "_internal", "kernel")
    m2_path = put_jz_deps(k2)
    write(os.path.join(k2, "config", "installed-deps.json"), INSTALLED)
    write(os.path.join(r2, "runtime", "pylibs", "manifest.json"), COMPONENT)
    m2 = load_mod(m2_path, "jz_deps_case2")
    assert_full(m2.load_installed_deps(app_dir=r2), "②客户端预检口径(app_dir=程序目录)")
    assert_full(m2.load_installed_deps(), "②客户端 worker 口径(app_dir 缺省=内核目录)")

    # ③ worker 形态（内核在 _internal/kernel，父级两层才到程序根）
    assert_full(m2.load_installed_deps(app_dir=k2), "③worker 显式 app_dir=内核目录")

    # 负例：裸目录
    r3 = os.path.join(TMP, "bare")
    m3_path = put_jz_deps(r3)
    m3 = load_mod(m3_path, "jz_deps_case3")
    d3 = m3.load_installed_deps(app_dir=r3)
    check("负例：裸目录 available=False", d3.get("available") is False, d3.get("path"))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("-" * 46)
print("deps-layout：%d/%d 项通过，%d 项失败" % (len(TOTAL) - len(FAILS), len(TOTAL), len(FAILS)))
sys.exit(1 if FAILS else 0)
