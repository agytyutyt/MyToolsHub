# -*- coding: utf-8 -*-
"""V1–V10 验收审计（机械项，2026-10-09）。逐项打印 通过/缺口，缺口列证据路径。"""
import glob
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile

ROOT = r"D:\JZToolsHub"
FAIL = []


def ck(item, name, ok, detail=""):
    print("  %s [%s] %s%s" % ("✅" if ok else "❌", item, name,
                             (" ｜ " + str(detail)[:160]) if detail else ""))
    if not ok:
        FAIL.append("%s %s" % (item, name))


# ---------- V1 分类一致性 ----------
CLASSES = {
    "admin": "A", "store": "A", "knowledge-base": "A", "shared-docs": "A",
    "notice-board": "A", "case-report": "A",
    "base64": "B", "color-picker": "B", "json-formatter": "B", "md5-generator": "B",
    "map-marker": "B", "info-transfer": "B", "trajectory-convert": "B", "qr-video-decode": "B",
    "file-filter": "C", "trajectory-sketch": "C", "character-graph": "C",
}
print("== V1 分类一致性 ==")
catalog = json.load(io.open(os.path.join(os.path.expanduser("~"), ".jztoolshub", "store",
                                         "catalog.json"), encoding="utf-8-sig"))
cat_ids = {p.get("id"): p for p in catalog.get("packages") or []}
ck("V1", "核心插件（admin/store）不入商店目录",
   "admin" not in cat_ids and "store" not in cat_ids, sorted(set(cat_ids) & {"admin", "store"}))
for pid, cls in sorted(CLASSES.items()):
    # 服务器双工件（后端+前端）按新命名族
    srv = glob.glob(os.path.join(ROOT, "deploy", "插件包", "服务器插件",
                                 "JZToolsHub-%s-后端-v*.zip" % pid))
    old = glob.glob(os.path.join(ROOT, "deploy", "插件包", "服务器插件",
                                 "JZToolsHub-插件-%s-v*.zip" % pid))
    local = glob.glob(os.path.join(ROOT, "deploy", "插件包", "本地插件",
                                   "JZClient-本地插件-local-%s-v*.zip" % pid))
    if cls == "A":
        if pid in ("admin", "store"):
            ck("V1", "核心插件 %s 随主体分发（包件豁免）" % pid, True)
        else:
            ck("V1", "A 类 %s 有服务器包" % pid, bool(srv or old), srv or old)
    elif cls == "B":
        ck("V1", "B 类 %s 服务器包+本地包齐" % pid, bool(srv or old) and bool(local),
           "srv=%s local=%s" % (bool(srv or old), bool(local)))
    else:
        if pid == "character-graph":     # Q5：维持 A 直至明确需求，不拆分
            ck("V1", "C 类 character-graph 按 Q5 维持 A（不拆分）", bool(srv or old),
               srv or old)
        else:
            ck("V1", "C 类 %s 双包齐（含本地客户端工件）" % pid, bool(srv or old) and bool(local),
               "local=%s" % (local[:1] or "缺"))
    # 商店条目 runtime 归属（上架了才查；未上架登记为走查项）
    ent = cat_ids.get(pid)
    if ent is None:
        print("  ⏳ [V1] 商店条目 %s 未上架（走查项，不计失败）" % pid)
    else:
        ck("V1", "商店条目 %s runtime=server" % pid, ent.get("runtime") == "server",
           ent.get("runtime"))

# ---------- V2 源码单一真源 ----------
print("== V2 单一真源 ==")


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def derive(args, out):
    r = subprocess.run([sys.executable,
                        os.path.join(ROOT, "tools", "client", "build-local-plugin-package.py")]
                       + args + ["-OutDir", out], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT)
    zs = glob.glob(os.path.join(out, "*.zip"))
    return r.returncode, (zs[0] if zs else "")


tmp = os.path.join(ROOT, ".workbuddy", "tmp", "v2-audit")
os.makedirs(tmp, exist_ok=True)
for f in glob.glob(os.path.join(tmp, "*.zip")):
    os.remove(f)
rc1, z1 = derive(["plugins/base64", "--derive-local", "--api-rewrite"], tmp)
h1 = sha(z1) if z1 else ""
rc2, z2 = derive(["plugins/base64", "--derive-local", "--api-rewrite"], tmp)
h2 = sha(z2) if z2 else ""
ck("V2", "同源派生可重复构建（两次 sha256 一致）", bool(h1) and h1 == h2, (h1[:16], h2[:16]))
store_pkg = glob.glob(os.path.join(os.path.expanduser("~"), ".jztoolshub", "store", "packages",
                                   "JZClient-本地插件-local-base64-v*.zip"))
ck("V2", "派生包与商店上架包 sha256 一致",
   bool(store_pkg) and h1 == sha(store_pkg[0]), ("store", sha(store_pkg[0])[:16]) if store_pkg else "商店无该包")
# 第二源码副本：client-plugins 之外的本地插件源码目录
dup = []
for base in ("plugins",):
    for d in glob.glob(os.path.join(ROOT, base, "*", "client")):
        dup.append(d)
ck("V2", "无 client-plugins/ 之外的本地插件第二源码副本", not dup, dup)
# C 类引擎副本同源登记（文件头注释）
for pid in ("file-filter", "trajectory-sketch"):
    cps = glob.glob(os.path.join(ROOT, "client-plugins", "local-" + pid, "backend", "**", "*.py"),
                    recursive=True)
    cps = [p for p in cps]
    hits = 0
    for p in cps:
        head = io.open(p, encoding="utf-8", errors="replace").read(600)
        if "同源" in head:
            hits += 1
    ck("V2", "C 类 %s 引擎副本带同源登记" % pid, hits > 0 or not cps,
       "副本 %d 个，带同源登记 %d 个" % (len(cps), hits))

# ---------- V4 红线 3 + L-13 ----------
print("== V4 页面一份 + L-13 ==")
# 前端零 fork：本地包 frontend 与服务器 frontend 关系（改写允许）
def norm_api(t):
    return t.replace("api/local/local-", "api/").replace("/api/local/local-", "/api/")


for pid, base_id in (("base64", "base64"), ("info-transfer", "info-transfer")):
    zp = glob.glob(os.path.join(ROOT, "deploy", "插件包", "本地插件",
                                "JZClient-本地插件-local-%s-v*.zip" % pid))
    if not zp:
        ck("V4", "%s 本地包前端零 fork" % pid, False, "无本地包")
        continue
    zf = zipfile.ZipFile(sorted(zp)[-1])
    js = [n for n in zf.namelist() if n.startswith("frontend/") and n.endswith(".js")]
    src_dir = os.path.join(ROOT, "plugins", base_id, "frontend")
    ok = True
    det = []
    for n in js[:4]:
        a = zf.read(n).decode("utf-8", "replace")
        p2 = os.path.join(src_dir, os.path.relpath(n, "frontend"))
        if not os.path.isfile(p2):
            continue
        b = io.open(p2, encoding="utf-8", errors="replace").read()
        if norm_api(a) != norm_api(b):
            ok = False
            det.append(n)
    ck("V4", "%s 本地包前端=服务器 frontend（仅基址改写）" % pid, ok, det)
# 出包负路径：漏开 --api-rewrite 且前端有引用 → 红
r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "client",
                                                 "build-local-plugin-package.py"),
                    "client-plugins/local-trajectory-sketch",
                    "-FrontendFrom", "plugins/trajectory-sketch/frontend",
                    "-OutDir", tmp], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", cwd=ROOT)
ck("V4", "漏开 --api-rewrite 出包拦截（负路径）", r.returncode != 0 and "api-rewrite" in (r.stdout + r.stderr),
   (r.stdout + r.stderr)[-120:])

# ---------- V5 数据出机明示 ----------
print("== V5 数据出机明示 ==")
sj = io.open(os.path.join(ROOT, "plugins", "store", "frontend", "store.js"), encoding="utf-8").read()
ck("V5", "商店页对 server_data 条目有出机明示", "server_data" in sj,
   "store.js 引用 server_data=%s" % ("server_data" in sj))

# ---------- V9 审计身份 ----------
print("== V9 审计身份 ==")
ex = io.open(os.path.join(ROOT, "client", "plugin_executor.py"), encoding="utf-8").read()
ck("V9", "审计行含真实身份字段", ("user" in ex.split("def _audit")[1][:600]) if "def _audit" in ex else False,
   "见 client/plugin_executor.py:_audit")

print("=" * 46)
print("审计：%d 项未过（含走查项/待修项）" % len(FAIL))
for f in FAIL:
    print("  ❌", f)
