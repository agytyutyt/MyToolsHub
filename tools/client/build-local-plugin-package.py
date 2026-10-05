# -*- coding: utf-8 -*-
r"""本地插件包出包工具（批次 8 T-O2-6，接缝④）：桌面端类 B/C 插件 → 商店上架包。

输入插件源码目录（manifest.runtime=local、id local- 前缀）→ 产出：

    deploy\本地插件包\JZClient-本地插件-<id>-v<版本>.zip     （zip 根即插件目录，T18 口径）
    同名 .sha256 旁挂                                        （介质传递后核对）
    登记片段（id/version/file/sha256/size，可直接粘入上架表单 / 共享盘 index.json）

校验（与客户端 pkg_install.install_local_zip、admin 上架机器校验同源口径）：
manifest 必填字段 / runtime=local / id local- 前缀 / kind ∈ {local, hybrid} /
frontend 入口存在 / hybrid 的 backend 与 mount 前缀 / 运行态数据零夹带（复用
tools/plugin-payload-rules.json 排除规则）。

用法：
    python tools/client/build-local-plugin-package.py local-report-suite
    python tools/client/build-local-plugin-package.py D:\path\to\local-xxx -OutDir D:\out
退出码：0 成功；1 校验失败。
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RULES_FILE = os.path.join(ROOT, "tools", "plugin-payload-rules.json")
DEFAULT_OUT = os.path.join(ROOT, "deploy", "本地插件包")


def load_rules():
    try:
        with open(RULES_FILE, encoding="utf-8") as f:
            r = json.load(f)
        return set(r.get("exclude_dirs") or []), set(r.get("exclude_files") or []), \
            list(r.get("exclude_globs") or [])
    except Exception:
        return set(), set(), []


def excluded(rel, rules):
    dirs, files, globs = rules
    parts = rel.split("/")
    for p in parts[:-1]:
        if p in dirs:
            return True
    name = parts[-1]
    if name in files:
        return True
    return any(fnmatch.fnmatch(name.lower(), g.lower()) for g in globs)


def die(msg):
    print("  [失败] %s" % msg)
    sys.exit(1)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="本地插件包出包（JZClient-本地插件-*.zip）")
    ap.add_argument("source", help="插件源码目录（plugins/<id> 或任意路径，目录名须= id）")
    ap.add_argument("-OutDir", dest="out_dir", default=DEFAULT_OUT)
    ap.add_argument("-Version", dest="version", default="", help="缺省取 manifest.version")
    args = ap.parse_args()

    src = os.path.abspath(args.source)
    pid = os.path.basename(src.rstrip("\\/"))
    if not re.match(r"^local-[A-Za-z0-9_-]+$", pid):
        die("目录名须为 local- 前缀的插件 id：%r" % pid)
    manifest_path = os.path.join(src, "manifest.json")
    if not os.path.isfile(manifest_path):
        die("缺少 manifest.json：%s" % manifest_path)
    with open(manifest_path, encoding="utf-8-sig") as f:
        manifest = json.load(f)
    if manifest.get("id") != pid:
        die("id 不一致：目录 %s，manifest %r" % (pid, manifest.get("id")))
    if manifest.get("runtime") != "local":
        die("manifest.runtime 必须 = local（实为 %r）" % manifest.get("runtime"))
    kind = manifest.get("kind", "local")
    if kind not in ("local", "hybrid"):
        die("kind 仅支持 local / hybrid：%r" % kind)
    entry = (manifest.get("entry") or {})
    frontend = entry.get("frontend") or "index.html"
    if not os.path.isfile(os.path.join(src, "frontend", *frontend.split("/"))):
        die("缺少 frontend/%s" % frontend)
    if kind == "hybrid":
        backend = entry.get("backend")
        mount = (entry.get("mount") or "").rstrip("/")
        if not backend or not os.path.isfile(os.path.join(src, "backend", *backend.split("/"))):
            die("hybrid 插件缺少 backend 入口文件")
        if not mount.startswith("/api/local/" + pid):
            die("hybrid 的 mount 必须以 /api/local/%s 开头（实为 %r）" % (pid, mount))

    version = args.version or str(manifest.get("version") or "")
    if not re.match(r"^\d+(\.\d+)+$", version):
        die("版本号不是语义化版本：%r（manifest.version 或 -Version 指定）" % version)

    rules = load_rules()
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    zip_name = "JZClient-本地插件-%s-v%s.zip" % (pid, version)
    zip_path = os.path.join(out_dir, zip_name)
    excluded_list, copied = [], 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src):
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, src).replace(os.sep, "/")
                if excluded(rel, rules):
                    excluded_list.append(rel)
                    continue
                zf.write(full, rel)
                copied += 1
    if copied == 0:
        die("没有可打包的文件（排除规则全部命中？）")
    digest = sha256_file(zip_path)
    size = os.path.getsize(zip_path)
    with open(zip_path + ".sha256", "w", encoding="utf-8") as f:
        f.write("%s  %s\n" % (digest, zip_name))

    print("==> 已产出本地插件包：%s" % zip_path)
    print("    体积 %.1f KB；sha256 %s" % (size / 1024, digest[:16] + "…"))
    if excluded_list:
        print("    已剔除运行态 %d 项（%s…）" % (len(excluded_list), excluded_list[0]))
    print("    登记片段（粘入上架表单校验用 / 共享盘 index.json）：")
    print("    " + json.dumps({
        "id": pid, "version": version, "file": zip_name,
        "sha256": digest, "size": size,
        "runtime": "local", "requires": manifest.get("requires") or {},
    }, ensure_ascii=False))
    print("    下一步：管理后台「商店管理」上架（runtime=local），审核通过后客户端商店可见。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
