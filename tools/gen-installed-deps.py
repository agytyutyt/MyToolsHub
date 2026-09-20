#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""生成「已安装依赖清单」—— 主体侧"当前装了哪些依赖、什么版本"的唯一真源。

为什么需要（见 `docs/design/主体与插件解耦-设计文档.md` §5.3）：
    冻结 exe 运行时不带 pip，`_internal` 内的 `.dist-info` 也不保证完整，现场无法
    `pip list`。因此由**构建期**在打包解释器上采集一次，写成
    `config/installed-deps.json` 随主包分发；运行期只读该文件即可在后台展示
    "已安装依赖及版本"，并与插件声明的 `requires` 比对得出可运行性结论。

产物结构：
    {
      "schema": 1,
      "generated_at": "2026-09-19T10:00:00+08:00",
      "python": "3.14.7",
      "packages": { "flask": "3.1.3", "opencv-python": "5.0.0.93", … },   # 发行包名 → 版本
      "modules":  { "cv2": "opencv-python", "docx": "python-docx", … },   # import 名 → 发行包名
      "whitelist": ["flask", "cv2", …]                                    # JZToolsHub.spec 的 PACKAGES
    }

用法（构建期，见 build-deploy.ps1 §3.2.3）：
    python tools\gen-installed-deps.py --out deploy\JZToolsHub\config\installed-deps.json
    python tools\gen-installed-deps.py --check --out <path>    # 仅校验既有清单可解析
"""

import argparse
import datetime
import importlib.metadata as md
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_spec_whitelist():
    """从 JZToolsHub.spec 的 PACKAGES 读出依赖白名单（唯一真源，不另存一份）。"""
    spec = os.path.join(ROOT, "JZToolsHub.spec")
    try:
        src = open(spec, encoding="utf-8").read()
    except OSError:
        return []
    m = re.search(r"PACKAGES\s*=\s*\[(.*?)\]", src, re.S)
    if not m:
        return []
    return sorted({x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()})


def collect():
    """采集打包解释器上已安装的全部发行包与 import 名映射。"""
    packages = {}
    for dist in md.distributions():
        try:
            name = (dist.metadata or {}).get("Name")
            version = dist.version
        except Exception:
            continue
        if name and version:
            packages[str(name)] = str(version)
    modules = {}
    try:
        for mod, dists in (md.packages_distributions() or {}).items():
            if dists:
                modules[str(mod)] = sorted(dists)[0]
    except Exception:
        pass
    return packages, modules


def main():
    ap = argparse.ArgumentParser(description="生成/校验已安装依赖清单")
    ap.add_argument("--out", default=os.path.join(ROOT, "config", "installed-deps.json"),
                    help="输出路径（部署目录 config\\installed-deps.json）")
    ap.add_argument("--check", action="store_true", help="只校验既有清单可解析，不重写")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.check:
        try:
            obj = json.load(open(args.out, encoding="utf-8-sig"))
            ok = isinstance(obj.get("packages"), dict) and bool(obj["packages"])
            print("  [%s] 已安装依赖清单：%s（%d 个包）"
                  % ("OK" if ok else "!!", args.out, len(obj.get("packages", {}))))
            return 0 if ok else 1
        except Exception as exc:
            print("  [!!] 已安装依赖清单不可解析：%s（%s）" % (args.out, exc))
            return 1

    packages, modules = collect()
    obj = {
        "schema": 1,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": "%d.%d.%d" % sys.version_info[:3],
        "packages": dict(sorted(packages.items(), key=lambda kv: kv[0].lower())),
        "modules": dict(sorted(modules.items())),
        "whitelist": read_spec_whitelist(),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if not args.quiet:
        print("  已安装依赖清单：%s（%d 个包 / %d 条 import 映射，Python %s）"
              % (args.out, len(packages), len(modules), obj["python"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
