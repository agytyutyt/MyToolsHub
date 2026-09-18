# -*- coding: utf-8 -*-
"""把 LibreOffice 官方 MSI 裁剪成「知识库预览够用」的便携核心包。

背景（2026-09-14 实测，见 docs/guide/离线部署包说明.md）：
  * 应用只用到 soffice 的两条转换 —— `.doc → .docx`（dhr 引擎）与 `.xls → .xlsx`
    （xhr 引擎），都是 Writer/Calc 过滤器；不需要 UI、帮助、拼写词典、界面语言包、
    图标主题，也不需要 Draw/Impress/Math/Base 的运行时。
  * 官方 MSI 解包 1522.6 MB / 19418 文件，随包分发是 357.5 MB —— 大头全是死重。
  * 裁剪后 551.6 MB / 8000+ 文件，压缩成 zip 约 166 MB。

⚠️ 绝不能删的目录：`presets`（删掉 soffice 直接 `Fatal Error ... 安装无法完成`，退出码 77）。
   这个坑极其隐蔽：**只有在「全新 user profile」下才暴露**。若复用已初始化好的
   profile，裁剪后的树看起来一切正常；而应用每次转换用的都是唯一临时 profile，
   等于每次都是「首次启动」，所以线上必崩。
   （`help` / `readmes` 已实测可删；注意别把这三个当成一类一起处理。）
   （另：`msiexec /a` 的 ADDLOCAL 功能选择被忽略，实测仍解出全量 19418 文件，
   所以只能「先全量解包、再裁剪」，不能靠官方功能选择省事。）

产物（均不入库，与源 MSI 同级放在 runtime/libreoffice/）：
    libreoffice-core.zip   裁剪后的便携树。包内路径以 `libreoffice/` 打头，
                           解压到部署包的 runtime/ 即得 runtime/libreoffice/program/soffice.exe
    libreoffice-core.json  元数据：源 MSI 与 zip 的 sha256/体积、裁剪前后体积、
                           裁剪清单版本、被剔除的条目

用法：
    python tools/build-libreoffice-core.py              # 有缓存则跳过（源 MSI 与清单版本都没变）
    python tools/build-libreoffice-core.py --force      # 强制重建
    python tools/build-libreoffice-core.py --verify-only
    python tools/build-libreoffice-core.py --no-smoke   # 跳过构建期冒烟测试（不推荐）
    python tools/build-libreoffice-core.py --msi <path> # 指定源 MSI
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # 避免中文在 GBK 控制台乱码

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LO_DIR = os.path.join(ROOT, "runtime", "libreoffice")
CORE_ZIP = os.path.join(LO_DIR, "libreoffice-core.zip")
CORE_META = os.path.join(LO_DIR, "libreoffice-core.json")

#: 裁剪清单版本 —— 改动下面的清单必须 +1，否则缓存不会失效
PRUNE_VERSION = 3

#: 整体删除的目录/文件（相对解包树根）
PRUNE_RELS = [
    # 拼写词典：约 471 MB，纯粹是各语言 hunspell 词库
    "share/extensions",
    # Python-UNO 运行时与 Java 桥（转换链路用不到）
    "program/classes",
    "program/wizards",
    # 剪贴图集 / 文档模板 / 文档向导
    "share/gallery",
    "share/template",
    "share/wizards",
    # Basic 标准宏库
    "share/basic",
    # PDF 导入filter（我们只做 office→office，不做 PDF 导入）
    "share/xpdfimport",
    # 字体：转换只搬格式不排版，字体名原样保留，系统字体负责最终显示
    "Fonts",
    # 离线帮助与 readme（headless 转换完全用不到；已实测单独删除安全）
    "help",
    "readmes",
    # 其它可选小目录（自动更正/自动图文集/词库/调色板/主题等）
    "share/autotext",
    "share/autocorr",
    "share/wordbook",
    "share/labels",
    "share/numbertext",
    "share/palette",
    "share/theme_definitions",
    "share/toolbarmode",
    "share/tipoftheday",
    "share/skia",
    "share/opengl",
    "share/glade",
    "share/dtd",
    "share/fingerprint",
    "share/firebird",
    "share/classification",
    "share/config/wizard",
]

#: 通配删除：(相对目录, basename 前缀, 是否按 KEEP_LOCALES 保留, 说明)
PRUNE_GLOBS = [
    ("program/resource", "", True, "界面语言资源目录"),
    ("share/registry/res", "fcfg_langpack_", True, "界面语言注册表包"),
    ("share/config", "images_", False, "图标主题 zip"),
    ("program", "python-core-", False, "PyUNO 内嵌 Python"),
]
#: 解包树根下的内嵌 MSI（管理安装会回填一份，对便携树无用）
PRUNE_GLOB_FILES = [("", "LibreOffice_", ".msi")]

#: 语言保留名单（basename 命中即保留）。注意两处命名不同：
#:   program/resource/<locale>        用下划线（zh_CN / zh_TW），实测**没有 en-US 目录**
#:   share/registry/res/fcfg_langpack_<locale>.xcd  用连字符（zh-CN / en-US）
#: 另含 "common"（program/resource/common 是共享字体目录，不是语言包，稳妥起见留着）
KEEP_LOCALES = {"en-US", "en_US", "zh-CN", "zh_CN", "zh-TW", "zh_TW", "common"}

#: 删完必须仍在的关键文件/目录 —— 缺一个就说明踩了 profile 引导的坑，直接失败
MUST_KEEP = [
    "program/soffice.exe", "program/soffice.bin", "program/mergedlo.dll",
    "share/registry/main.xcd", "share/config/soffice.cfg",
    "presets",   # ← 唯一一个删了会让 soffice 以退出码 77 启动失败的目录
    "System64",  # ← VC++ 2015-2022 运行库所在的目录：管理安装解包不会把它们放进 System32，
                 #    目标机缺运行库时，安装器靠这里补齐到 program\ 才救得回来（0xC0000142）。
                 #    删掉它 = 组件失去自足能力，只能要求目标机自行安装 VC++ 可再发行组件。
]

#: 许可文件必须保留（MPL-2.0 / LGPL 再分发要求）
KEEP_LICENSE = ["license.txt", "LICENSE.html", "NOTICE", "CREDITS.fodt"]


def say(msg=""):
    print(msg, flush=True)


def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def dir_stat(path):
    total = files = 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
                files += 1
            except OSError:
                pass
    return total, files


def mb(n):
    return n / 1048576.0


def find_msi(explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    if not os.path.isdir(LO_DIR):
        return None
    cands = sorted(f for f in os.listdir(LO_DIR)
                   if f.lower().endswith(".msi") and f.lower().startswith("libreoffice"))
    return os.path.join(LO_DIR, cands[0]) if cands else None


# --------------------------------------------------------------------------
# 1) 管理安装解包（msiexec /a：免管理员、不写注册表、解出即可运行）
# --------------------------------------------------------------------------
def admin_unpack(msi, target):
    os.makedirs(target, exist_ok=True)
    args = ["msiexec", "/a", msi, "/qn", "/norestart", "TARGETDIR=%s" % target]
    say("  执行：msiexec /a %s /qn TARGETDIR=%s" % (os.path.basename(msi), target))
    t = time.time()
    # 注意：这里必须用 subprocess 等待真实退出；PowerShell 里 & 调 msiexec 会提前返回
    p = subprocess.run(args, capture_output=True)
    say("  msiexec 退出码=%s 耗时 %.1fs" % (p.returncode, time.time() - t))
    if p.returncode not in (0, 3010):
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        raise SystemExit("管理安装失败（退出码 %s）%s %s" % (p.returncode, out[-400:], err[-400:]))


# --------------------------------------------------------------------------
# 2) 裁剪
# --------------------------------------------------------------------------
def prune(root):
    removed = []

    def rm(rel):
        p = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.exists(p):
            return False
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            os.remove(p)
        removed.append(rel)
        return True

    for rel in PRUNE_RELS:
        rm(rel)

    for rel_dir, prefix, keep_locales, _desc in PRUNE_GLOBS:
        d = os.path.join(root, rel_dir.replace("/", os.sep))
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if prefix and not name.startswith(prefix):
                continue
            if not prefix and not os.path.isdir(os.path.join(d, name)):
                continue
            if keep_locales:
                loc = name[len(prefix):] if prefix else name
                if loc.endswith(".xcd"):
                    loc = loc[:-4]
                if loc in KEEP_LOCALES:
                    continue
            rm("%s/%s" % (rel_dir, name))

    for rel_dir, prefix, suffix in PRUNE_GLOB_FILES:
        d = os.path.join(root, rel_dir.replace("/", os.sep)) if rel_dir else root
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.startswith(prefix) and name.endswith(suffix):
                rm(os.path.join(rel_dir, name).replace("\\", "/"))

    return removed


def check_invariants(root):
    missing = [r for r in MUST_KEEP if not os.path.exists(os.path.join(root, r.replace("/", os.sep)))]
    missing += [r for r in KEEP_LICENSE if not os.path.exists(os.path.join(root, r))]
    if missing:
        raise SystemExit(
            "裁剪后缺少必须保留的条目：%s\n"
            "（help / readmes / presets 缺失会让 soffice 在全新 profile 下以退出码 77 启动失败）" % missing)


# --------------------------------------------------------------------------
# 3) 构建期冒烟测试：全新 profile 下 CSV → XLSX
#    （这一步专抓「profile 引导被删坏」——复用旧 profile 测不出来）
# --------------------------------------------------------------------------
def smoke_test(root):
    soffice = os.path.join(root, "program", "soffice.exe")
    if not os.path.isfile(soffice):
        return False, "未找到 program\\soffice.exe"
    with tempfile.TemporaryDirectory(prefix="lo-smoke-") as tmp:
        csv = os.path.join(tmp, "smoke.csv")
        with io.open(csv, "w", encoding="utf-8", newline="") as f:
            f.write("单位,数量\n测试科,12\n")
        prof = os.path.join(tmp, "profile")
        outd = os.path.join(tmp, "out")
        os.makedirs(outd, exist_ok=True)
        args = [soffice, "-env:UserInstallation=file:///%s" % prof.replace("\\", "/"),
                "--headless", "--norestore", "--nolockcheck",
                "--convert-to", "xlsx", "--outdir", outd, csv]
        try:
            p = subprocess.run(args, capture_output=True, timeout=300)
            rc = p.returncode
            err = (p.stderr or b"").decode("utf-8", "replace").strip().replace("\n", " ")[:200]
        except subprocess.TimeoutExpired:
            return False, "冒烟测试超时（300s）"
        made = os.listdir(outd)
        if rc != 0 or "smoke.xlsx" not in made:
            return False, "rc=%s 产物=%s %s" % (rc, made, err)
    return True, "全新 profile 下 CSV→XLSX 成功"


# --------------------------------------------------------------------------
# 4) 打包
# --------------------------------------------------------------------------
def make_zip(root, zip_path, progress_every=2000):
    tmp_zip = zip_path + ".part"
    if os.path.exists(tmp_zip):
        os.remove(tmp_zip)
    n = 0
    t = time.time()
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dp, _dn, fn in os.walk(root):
            for f in fn:
                fp = os.path.join(dp, f)
                arc = os.path.join("libreoffice", os.path.relpath(fp, root)).replace("\\", "/")
                z.write(fp, arc)
                n += 1
                if progress_every and n % progress_every == 0:
                    say("    已打包 %d 个文件…" % n)
    # 原子替换
    if os.path.exists(zip_path):
        os.remove(zip_path)
    os.rename(tmp_zip, zip_path)
    return n, time.time() - t


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def load_meta():
    if not os.path.isfile(CORE_META):
        return None
    try:
        with io.open(CORE_META, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="把 LibreOffice MSI 裁剪成便携核心包")
    ap.add_argument("--msi", default=None, help="源 MSI 路径（默认取 runtime/libreoffice/LibreOffice_*.msi）")
    ap.add_argument("--force", action="store_true", help="忽略缓存，强制重建")
    ap.add_argument("--verify-only", action="store_true", help="只校验已有产物，不重建")
    ap.add_argument("--no-smoke", action="store_true", help="跳过构建期冒烟测试（不推荐）")
    ap.add_argument("--keep-work", action="store_true", help="保留中间解包目录，便于排查")
    args = ap.parse_args()

    msi = find_msi(args.msi)
    if not msi:
        raise SystemExit("未找到源 MSI（先跑 python tools/fetch-offline-bundle.py）")
    say("源 MSI：%s（%.1f MB）" % (os.path.basename(msi), mb(os.path.getsize(msi))))
    msi_sha = sha256_of(msi)

    meta = load_meta()
    if args.verify_only:
        if not (meta and os.path.isfile(CORE_ZIP)):
            raise SystemExit("产物不存在，无法校验")
        ok = (meta.get("source", {}).get("sha256") == msi_sha
              and meta.get("prune_version") == PRUNE_VERSION
              and meta.get("artifact", {}).get("size") == os.path.getsize(CORE_ZIP)
              and meta.get("artifact", {}).get("sha256") == sha256_of(CORE_ZIP))
        say("校验：%s" % ("通过" if ok else "不通过（源 MSI 或清单版本已变，需重建）"))
        return 0 if ok else 2

    if (not args.force and meta
            and meta.get("source", {}).get("sha256") == msi_sha
            and meta.get("prune_version") == PRUNE_VERSION
            and os.path.isfile(CORE_ZIP)
            and meta.get("artifact", {}).get("size") == os.path.getsize(CORE_ZIP)):
        say("已是最新（源 MSI 未变、裁剪清单 v%d 未变）→ 跳过。用 --force 可强制重建。"
            % PRUNE_VERSION)
        return 0

    work = tempfile.mkdtemp(prefix="lo-core-")
    tree = os.path.join(work, "runtime", "libreoffice")   # 预留 libreoffice\ 一层，与部署形态一致
    try:
        say()
        say("[1/4] 管理安装解包…")
        admin_unpack(msi, tree)
        full_bytes, full_files = dir_stat(tree)
        say("      解出 %.1f MB / %d 文件" % (mb(full_bytes), full_files))

        say()
        say("[2/4] 裁剪…")
        removed = prune(tree)
        check_invariants(tree)
        core_bytes, core_files = dir_stat(tree)
        say("      剔除 %d 项；%.1f MB → %.1f MB（-%d 文件，省 %.1f MB / %.0f%%）"
            % (len(removed), mb(full_bytes), mb(core_bytes), full_files - core_files,
               mb(full_bytes - core_bytes), (full_bytes - core_bytes) * 100.0 / max(full_bytes, 1)))
        for r in removed:
            say("        - %s" % r)

        if args.no_smoke:
            say()
            say("[3/4] 冒烟测试：已按 --no-smoke 跳过")
        else:
            say()
            say("[3/4] 冒烟测试（全新 profile，CSV→XLSX）…")
            ok, why = smoke_test(tree)
            say("      %s：%s" % ("通过" if ok else "失败", why))
            if not ok:
                raise SystemExit(
                    "冒烟测试失败 —— 裁剪把 soffice 弄坏了，请检查裁剪清单（尤其 help/readmes/presets）。\n"
                    "中间目录已保留：%s" % tree)

        say()
        say("[4/4] 打包 zip…")
        os.makedirs(LO_DIR, exist_ok=True)
        n, dt = make_zip(tree, CORE_ZIP)
        zip_size = os.path.getsize(CORE_ZIP)
        say("      %s：%.1f MB / %d 条目（%.0fs，压缩比 %.2f）"
            % (os.path.basename(CORE_ZIP), mb(zip_size), n, dt, core_bytes / max(zip_size, 1)))

        meta_out = {
            "schema": 1,
            "prune_version": PRUNE_VERSION,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "target": "libreoffice",
            "source": {"file": os.path.basename(msi), "size": os.path.getsize(msi), "sha256": msi_sha},
            "artifact": {
                "file": os.path.basename(CORE_ZIP), "size": zip_size,
                "sha256": sha256_of(CORE_ZIP),
                "unpacked_bytes": core_bytes, "unpacked_files": core_files,
                "zip_root": "libreoffice/",
            },
            "full": {"unpacked_bytes": full_bytes, "unpacked_files": full_files},
            "removed": removed,
            "must_keep": MUST_KEEP,
            "note": ("裁剪后的便携 LibreOffice 核心。解压到部署包的 runtime/ 即得 "
                     "runtime/libreoffice/program/soffice.exe；应用会自动探测（零配置）。"),
        }
        with io.open(CORE_META, "w", encoding="utf-8") as f:
            json.dump(meta_out, f, ensure_ascii=False, indent=2)

        say()
        say("完成：")
        say("  %s" % CORE_ZIP)
        say("  %s" % CORE_META)
        say("  部署包内占比：由源 MSI %.1f MB 降为 %.1f MB（省 %.1f MB）"
            % (mb(os.path.getsize(msi)), mb(zip_size), mb(os.path.getsize(msi) - zip_size)))
        return 0
    finally:
        if args.keep_work:
            say("中间目录保留在：%s" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
