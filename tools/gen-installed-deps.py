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
      "scope": "shipped",                                                # 只记随包分发的包
      "generated_at": "2026-09-19T10:00:00+08:00",
      "python": "3.14.7",
      "packages": { "flask": "3.1.3", "pillow": "12.3.0", … },           # 发行包名 → 版本
      "modules":  { "PIL": "pillow", "docx": "python-docx", … },          # import 名 → 发行包名
      "whitelist": ["flask", "cv2", …]                                    # JZToolsHub.spec 的 PACKAGES
    }

**只记随包分发的包**（scope=shipped）：包集合取自冻结目录 `_internal` 的 `*.dist-info`，
即"目标机上真有的包"。构建机环境里的其它包（fastapi、pytest…）**不能**写进来——否则目标机
（尤其干净机）会把这些并不存在的包判为"已安装"，插件声明的依赖被误判为满足、缺依赖提示不出现。
`--all` 保留全量采集，仅供排障。

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
    """从 JZToolsHub.spec 的 PACKAGES 读出依赖白名单（唯一真源，不另存一份）。

    spec 里的 PACKAGES 逐项带行内注释（`"flask",  # Web 框架本体`），故先按行去掉
    `#` 之后的内容再切分——否则注释会被当成包名（曾导致白名单核验整片误报）。
    """
    spec = os.path.join(ROOT, "JZToolsHub.spec")
    try:
        src = open(spec, encoding="utf-8").read()
    except OSError:
        return []
    m = re.search(r"PACKAGES\s*=\s*\[(.*?)\]", src, re.S)
    if not m:
        return []
    body = "\n".join(line.split("#", 1)[0] for line in m.group(1).splitlines())
    return sorted({x.strip().strip('"\'') for x in body.split(",") if x.strip()})


def shipped_from_internal(internal_dir):
    """读冻结目录 `_internal` 的 *.dist-info，返回**实际随主包分发**的发行包 → 版本。

    PyInstaller 只把 spec 白名单（及其传递依赖）收进 `_internal`，因此这里的
    dist-info 集合就是"目标机上真有的包"。返回 None 表示目录不可用（回退到全量采集）。
    """
    if not internal_dir or not os.path.isdir(internal_dir):
        return None
    out = {}
    for name in sorted(os.listdir(internal_dir)):
        if not name.endswith(".dist-info"):
            continue
        base = name[: -len(".dist-info")]
        dist_name = dist_ver = ""
        try:
            with open(os.path.join(internal_dir, name, "METADATA"), encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        break
                    if line.startswith("Name: ") and not dist_name:
                        dist_name = line[6:].strip()
                    elif line.startswith("Version: ") and not dist_ver:
                        dist_ver = line[9:].strip()
        except OSError:
            pass
        if not (dist_name and dist_ver):
            dist_name, _, dist_ver = base.rpartition("-")
        if dist_name and dist_ver:
            out[dist_name] = dist_ver
    return out or None


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
    ap.add_argument("--internal", default="",
                    help="冻结目录 _internal（缺省：<out 所在程序目录>\\_internal）；"
                         "清单只记录该目录里真实存在的发行包")
    ap.add_argument("--all", action="store_true",
                    help="记录打包解释器上的**全部**发行包（排障用，勿用于发版：会把构建机"
                         "独有、目标机没有的包也写成「已安装」）")
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
    all_modules = dict(modules)
    scope, filtered_out, wl_uncovered, internal_dir = "all", 0, [], ""
    if not args.all:
        internal_dir = args.internal or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(args.out))), "_internal")
        shipped = shipped_from_internal(internal_dir)
        if shipped is None:
            print("  [!!] 未找到冻结目录（%s）：无法判定「随包分发」的包集合。\n"
                  "       请先跑 PyInstaller 再生成，或用 --all 显式接受全量清单。" % internal_dir)
            return 1
        by_low = {k.lower(): k for k in packages}
        kept = {}
        for nm, ver in shipped.items():
            kept[by_low.get(nm.lower(), nm)] = packages.get(by_low.get(nm.lower(), ""), ver)
        scope, filtered_out = "shipped", len(packages) - len(kept)
        packages = kept
        kept_low = {k.lower() for k in packages}
        modules = {m: d for m, d in modules.items() if str(d).lower() in kept_low}
        # 白名单项可能是 import 名（PIL），先经 modules 归一到发行包名再核验
        for w in read_spec_whitelist():
            dist = str(all_modules.get(w, w)).lower()
            if dist not in kept_low:
                wl_uncovered.append("%s→%s" % (w, dist) if dist != w.lower() else w)

    obj = {
        "schema": 1,
        "scope": scope,                     # shipped=只记随包分发的包；all=构建机全量（排障）
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
        if scope == "shipped":
            print("  已安装依赖清单：%s（%d 个包随包分发 / %d 条 import 映射，Python %s；"
                  "已过滤构建机独有包 %d 个）"
                  % (args.out, len(packages), len(modules), obj["python"], filtered_out))
        else:
            print("  已安装依赖清单：%s（--all：构建机全量 %d 个包 / %d 条 import 映射，Python %s）"
                  % (args.out, len(packages), len(modules), obj["python"]))
    if wl_uncovered:
        print("  [!!] 白名单中的包未随包分发：%s（_internal 里没有，检查 JZToolsHub.spec 的 "
              "PACKAGES 与 excludes）" % "、".join(wl_uncovered))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
