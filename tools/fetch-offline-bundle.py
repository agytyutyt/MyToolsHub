#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线部署组件获取器 —— 下载随包分发的第三方组件（Chrome / LibreOffice 等）。

为什么需要它
------------
目标机（内网、无外网）无法在线安装 Chrome / LibreOffice，而这两者分别是
「浏览器基线」和「知识库 Office 预览」的前置条件。本脚本在**有网**的打包机
上把官方安装包拉到 runtime/，由 build-deploy.ps1 复制进部署包，实现离线可部署。

设计要点
--------
* 组件清单放在 tools/offline-components.json（**入库**，体积很小、可评审）；
  二进制本身放 runtime/（**不入库**，可由本脚本随时重建）。
* 支持断点续传：未完成的下载留在 <file>.part，重跑时带 Range 续传。
* sha256：清单里填了就强校验；没填则首次下载后写进 runtime/manifest.json，
  可用 --pin 回写进 tools/offline-components.json 形成「冻结值」。
  注意 sha256 只能证明「与打包时字节一致」，不能证明上游来源真实性。

用法
----
    python tools/fetch-offline-bundle.py                 # 下载全部组件到 runtime/
    python tools/fetch-offline-bundle.py --only chrome   # 只下载指定组件
    python tools/fetch-offline-bundle.py --verify-only   # 只校验已有文件
    python tools/fetch-offline-bundle.py --pin           # 把实测 sha256 回写进清单
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SPEC = os.path.join(REPO, "tools", "offline-components.json")
CHUNK = 1024 * 256
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JZToolsHub-offline-fetch"

_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler(urllib.request.getproxies())
)


def human(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


def probe_size(url):
    """HEAD 取 Content-Length（拿不到返回 None）。"""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with _opener.open(req, timeout=30) as r:
            cl = r.headers.get("Content-Length")
            return int(cl) if cl else None
    except Exception as e:
        print("    [警告] HEAD 失败：%r" % (e,))
        return None


def download(url, dest, expect_size=None):
    """下载到 dest（支持断点续传）。返回 (ok, message)。"""
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    done = os.path.getsize(part) if os.path.exists(part) else 0

    if expect_size and done == expect_size:
        os.replace(part, dest)
        return True, "复用已下载的分片"

    headers = {"User-Agent": UA}
    if done:
        headers["Range"] = "bytes=%d-" % done
        print("    续传：从 %s 继续" % human(done))

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = _opener.open(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and done:      # 分片已完整
            os.replace(part, dest)
            return True, "分片已完整（HTTP 416）"
        return False, "HTTP %s %s" % (e.code, e.reason)
    except Exception as e:
        return False, "连接失败：%r" % (e,)

    with resp:
        resumed = resp.status == 206
        if done and not resumed:
            done = 0              # 服务器不支持 Range，从头来
            print("    服务器不支持续传，从头下载")
        total = None
        cl = resp.headers.get("Content-Length")
        if cl:
            try:
                total = int(cl) + done
            except ValueError:
                total = None
        mode = "ab" if resumed else "wb"
        got = done
        last = time.time()
        with open(part, mode) as f:
            while True:
                blk = resp.read(CHUNK)
                if not blk:
                    break
                f.write(blk)
                got += len(blk)
                if time.time() - last > 3:
                    last = time.time()
                    if total:
                        print("      %s / %s (%.0f%%)" % (human(got), human(total),
                                                          100.0 * got / total))
                    else:
                        print("      %s" % human(got))

    real = os.path.getsize(part)
    if expect_size and real != expect_size:
        return False, "大小不符：期望 %s，实得 %s（保留 .part 以便续传）" % (
            human(expect_size), human(real))
    os.replace(part, dest)
    return True, "完成（%s）" % human(real)


def main():
    ap = argparse.ArgumentParser(description="下载离线部署所需的第三方组件")
    ap.add_argument("--components", default=DEFAULT_SPEC, help="组件清单 JSON 路径")
    ap.add_argument("--dest", default=None, help="输出根目录（默认 <仓库>/runtime）")
    ap.add_argument("--only", default="", help="只处理这些组件 id（逗号分隔）")
    ap.add_argument("--verify-only", action="store_true", help="只校验，不下载")
    ap.add_argument("--pin", action="store_true",
                    help="把实测 sha256 回写进组件清单（形成冻结值）")
    args = ap.parse_args()

    with open(args.components, "r", encoding="utf-8") as f:
        spec = json.load(f)

    dest_root = args.dest or os.path.join(REPO, spec.get("dest_dir", "runtime"))
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    comps = spec.get("components", [])
    if only:
        comps = [c for c in comps if c["id"] in only]

    print("输出目录：%s" % dest_root)
    proxies = urllib.request.getproxies()
    print("代理：%s" % (proxies if proxies else "（直连）"))
    print("")

    manifest = {"schema": 1, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "dest_root": dest_root, "components": []}
    failed = []
    pinned = {}

    for c in comps:
        sub = os.path.join(dest_root, c["subdir"])
        path = os.path.join(sub, c["file"])
        expect = c.get("size") if isinstance(c.get("size"), int) else None
        print("[%s] %s" % (c["id"], c["name"]))
        print("    版本 %s  → %s" % (c.get("version", "?"),
                                    os.path.relpath(path, REPO)))

        if os.path.exists(path):
            size = os.path.getsize(path)
            print("    已存在：%s，校验中…" % human(size))
        elif args.verify_only:
            print("    [缺失] 未下载，--verify-only 模式跳过")
            failed.append((c["id"], "文件缺失"))
            continue
        else:
            if expect is None:
                expect = probe_size(c["url"])
                print("    远端大小：%s" % human(expect))
            ok, msg = download(c["url"], path, expect)
            print("    %s" % msg)
            if not ok:
                failed.append((c["id"], msg))
                continue
            size = os.path.getsize(path)

        digest = sha256_of(path)
        want = c.get("sha256")
        if want and want.lower() != digest:
            print("    [失败] sha256 不匹配！期望 %s，实得 %s" % (want[:16], digest[:16]))
            failed.append((c["id"], "sha256 不匹配"))
            continue
        if not want:
            print("    sha256 %s（清单未固定%s）" % (
                digest[:16], "，--pin 已回写" if args.pin else ""))
            if args.pin:
                pinned[c["id"]] = digest
        print("    OK  %s  sha256=%s" % (human(size), digest[:16]))
        print("")

        manifest["components"].append({
            "id": c["id"], "name": c["name"], "version": c.get("version"),
            "file": c["file"], "subdir": c["subdir"],
            "size": size, "sha256": digest, "url": c["url"],
            "license": c.get("license", ""), "install": c.get("install", "manual"),
            "required": bool(c.get("required", False)),
        })

    if not args.verify_only:
        if manifest["components"]:
            mpath = os.path.join(dest_root, "manifest.json")
            os.makedirs(dest_root, exist_ok=True)
            with open(mpath, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            print("清单已写入：%s" % mpath)

    if args.pin and pinned:
        with open(args.components, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for c in raw["components"]:
            if c["id"] in pinned:
                c["sha256"] = pinned[c["id"]]
                c["size"] = os.path.getsize(
                    os.path.join(dest_root, c["subdir"], c["file"]))
        with open(args.components, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("已回写冻结值到：%s" % args.components)

    total = sum(x["size"] for x in manifest["components"])
    print("")
    print("成功 %d / %d，合计 %s" % (len(manifest["components"]), len(comps), human(total)))
    if failed:
        print("失败项：")
        for cid, why in failed:
            print("  - %s：%s" % (cid, why))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
