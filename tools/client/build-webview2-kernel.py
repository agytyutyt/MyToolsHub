# -*- coding: utf-8 -*-
r"""WebView2 Fixed Version 内核获取 / 裁剪 / marker 分件（审计 D-5，T07 遗留销项；2026-10-09）。

背景：`client/client_config.py:webview2_runtime_dir()` 要求 `runtime\webview2\` 内含
`msedgewebview2.exe` 才启用 fixed 内核，否则恒回落系统常青（`client.log` 留痕
「webview2 runtime: 系统常青」）。此前部署目录只有一个 27 字节 `KERNEL_MARKER` 占位。
本脚本产出真正可用的内核目录（=T07「内核分件」的一半；另一半是 install.ps1 的目标机装载）。

用法：
  python tools/client/build-webview2-kernel.py --list
      核验：拉官方固定版清单（下载页版本下拉同源 API），打印当前可得版本与 CAB 直链，只读退出。
  python tools/client/build-webview2-kernel.py [--dry-run]
      取包 → 解包 → 裁语言 → 写 marker → 断言 → 原子替换 `runtime\webview2\`。
      源优先级：--from-local > 本地缓存 > --url > 按版本从官方清单匹配下载。
  python tools/client/build-webview2-kernel.py --from-local <cab|zip|已解包目录>
      离线打包机：用人工带进来的包/目录（目录=已解包内核，含 msedgewebview2.exe）。
  python tools/client/build-webview2-kernel.py --url <URL>
      指定下载地址（覆盖按版本匹配；如官方已下架时代用存档副本，见下方「可得性结论」）。
  python tools/client/build-webview2-kernel.py --zip
      另产出可分发的内核分件 zip（deploy/桌面端/运行时/），供 install.ps1 在目标机装载。

可得性结论（2026-10-09 核验，证据见 client-build-config.json 的 webview2.source 字段）：
  官方固定版清单只保留**最近 5 个版本**（现为 151.0.4129.107 ~ 155.0.4283.45，x64/x86/arm64 CAB）。
  备用阶梯 100.x→109.x→92.x 三线的 CDN 直链均已 404（末版分别为 100.0.1185.50 / 109.0.1518.78 /
  92.0.902.84）；唯一可得渠道是 Web Archive 的存档副本（非官方）——**109.0.1518.78 已于 2026-10-11 法务审核定版并实测通过（分发自此放行）；其余老版本存档仍须先过法务**。

参数：
  --version 目标版本（缺省读 client-build-config.json 的 webview2.version；含 `x` 段=前缀匹配）
  --arch    x64（缺省）/ x86 / arm64
  --out     输出目录（缺省 deploy/桌面端/<product>/runtime/webview2）
  --cache-dir 包缓存目录（缺省 .workbuddy/cache，与 build-client-runtime.py 同）
  --keep-langs 保留语言包（缺省 zh-CN,en-US）
  --zip     另出分件 zip；--zip-dir 指定其落点（缺省 deploy/桌面端/运行时/）
  --allow-version-mismatch 跳过「产物版本 ≠ 目标版本」红灯（调试用；marker 仍写实际版本）
  --dry-run 只打印将做的事，不下载/不解包/不落盘
  --list    只核验官方当前可得固定版清单（只读）

实测（2026-10-09，x64）：官方 100.x 直链 404；用 Wayback 存档副本取出 100.0.1185.50 →
解包 expand.exe → 裁 82 个语言包（-62.9MB）→ 产物 305.6MB / 89 文件、KERNEL_MARKER 首行
kernel-marker-100.0.1185.50；--zip 分件 153.4MB。目标机装载见 tools/client/install.ps1。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CFG = os.path.join(HERE, "client-build-config.json")
CACHE_DIR = os.path.join(ROOT, ".workbuddy", "cache")
LIST_API = "https://developer.microsoft.com/microsoft-edge/api/webview2"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
EXE = "msedgewebview2.exe"


def log(msg=""):
    print(msg, flush=True)


def fail(msg, code=1):
    log("")
    log("[失败] " + msg)
    sys.exit(code)


def dir_size(path):
    total = n = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
                n += 1
            except OSError:
                pass
    return total, n


def rel(p):
    """跨盘符安全的相对路径（test/打包机 out 可能在别的盘）。"""
    try:
        return os.path.relpath(p, ROOT)
    except ValueError:
        return p


def load_cfg():
    with open(CFG, "r", encoding="utf-8") as f:
        return json.load(f)


def is_placeholder(ver):
    """占位/前缀版本（如 100.0.1185.x）→ 按前缀匹配实际产物。"""
    return bool(re.search(r"[^0-9.]", ver or ""))


def prefix_of(ver):
    """前缀匹配口径：占位 `100.0.1185.x` → `100.0.1185.`。"""
    if not ver:
        return ""
    parts = ver.split(".")
    keep = []
    for p in parts:
        if not p.isdigit():
            break
        keep.append(p)
    return ".".join(keep) + "." if keep else ""


def version_matches(requested, actual):
    if not requested or not actual:
        return None                      # 无从判定
    if is_placeholder(requested):
        pre = prefix_of(requested)
        return actual.startswith(pre) if pre else None
    return actual == requested


# ---------------- 官方固定版清单 ----------------
def fetch_official_list():
    req = urllib.request.Request(LIST_API, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for e in data:
        urls = {b.get("architecture"): b.get("url") for b in (e.get("builds") or [])}
        out.append((e.get("version"), urls))
    return out


def cmd_list():
    log("== 官方 WebView2 固定版清单（%s）==" % LIST_API)
    try:
        items = fetch_official_list()
    except Exception as exc:
        fail("清单拉取失败：%r\n"
             "       人工核验：浏览器开 https://developer.microsoft.com/en-us/microsoft-edge/webview2/\n"
             "       展开 Fixed Version 的 Select Version 下拉，逐项核对版本号与架构；" 
             "直链形态 https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/<guid>/"
             "Microsoft.WebView2.FixedVersionRuntime.<版本>.<架构>.cab" % (exc,))
    for ver, urls in items:
        log("  %s" % ver)
        for arch in ("x64", "x86", "arm64"):
            if urls.get(arch):
                log("      %-5s %s" % (arch, urls[arch]))
    log("")
    log("  共 %d 个版本（官方只保留最近 5 个）。核验日期 %s。" % (len(items), time.strftime("%Y-%m-%d")))
    return 0


# ---------------- 源解析 ----------------
def cab_name(ver, arch):
    return "Microsoft.WebView2.FixedVersionRuntime.%s.%s.cab" % (ver, arch)


def cache_hits(cache_dir, requested, arch):
    """缓存命中：完整版本按名精确找；占位版本按前缀扫（100.0.1185.x → 100.0.1185.*.x64.cab）。"""
    if not os.path.isdir(cache_dir):
        return []
    hits = []
    for f in sorted(os.listdir(cache_dir)):
        low = f.lower()
        if not low.startswith("microsoft.webview2.fixedversionruntime.") or not low.endswith(".cab"):
            continue
        if not low.endswith("." + arch + ".cab"):
            continue
        ver = f[len("Microsoft.WebView2.FixedVersionRuntime."):-len(".%s.cab" % arch)]
        if version_matches(requested, ver):
            hits.append(os.path.join(cache_dir, f))
    return hits


def cache_name_for(url, requested, arch):
    """--url/官方清单 URL → 缓存文件名：优先取 URL 里的规范版本号。"""
    m = re.search(r"FixedVersionRuntime\.(\d+\.\d+\.\d+\.\d+)\.%s\.cab" % arch, url or "")
    if m:
        return cab_name(m.group(1), arch)
    return cab_name(prefix_of(requested).rstrip(".") or (requested or "unknown"), arch)


def read_marker_version(root):
    p = os.path.join(root, "KERNEL_MARKER")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            m = re.search(r"kernel-marker-(\S+)", f.read(4096))
        return m.group(1) if m else None
    except OSError:
        return None


def file_sha(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def download(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    prev = None
    if os.path.isfile(dest):
        # 同名缓存已在（如 --url 指向与缓存同名的另一来源）：先挪开，绝不静默覆盖既有包
        prev = dest + ".prev-" + time.strftime("%Y%m%d%H%M%S")
        try:
            os.replace(dest, prev)
            log("      （同名缓存已存在 → 暂存为 %s，下载完成后再比对）" % os.path.basename(prev))
        except OSError:
            prev = None
    tmp = dest + ".part"
    log("      下载：%s" % url)
    req = urllib.request.Request(url, headers=UA)
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if got % (16 << 20) < (1 << 20):
                    log("        %.0f MB%s" % (got / 1048576.0,
                        (" / %.0f MB" % (total / 1048576.0)) if total else ""))
    except urllib.error.HTTPError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if prev:                                   # 下载失败：把挪开的原缓存放回去
            try:
                os.replace(prev, dest)
            except OSError:
                pass
        if exc.code == 404:
            fail("下载 404：该版本已从官方 CDN 下架（核验 2026-10-09）。\n"
                 "       ① 先用 --list 看当前可得版本；\n"
                 "       ② 若必须钉旧版本：在可上网机器人工下载后放缓存再跑（或 --from-local）：\n"
                 "          %s\n"
                 "       ③ Web Archive 存档副本（非官方，仅测试/应急；--url 直传即可）：\n"
                 "          https://web.archive.org/web/<时间戳>if_/<官方直链>\n"
                 "       ④ 或用 --url 指定其它可信来源。" % dest)
        fail("下载失败 HTTP %s：%s" % (exc.code, url))
    except Exception as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if prev:
            try:
                os.replace(prev, dest)
            except OSError:
                pass
        fail("下载失败：%r\n       离线环境请人工下载后放：%s\n       或用 --from-local <路径>。"
             % (exc, dest))
    os.replace(tmp, dest)
    if prev:
        if file_sha(prev) == file_sha(dest):
            os.remove(prev)      # 同名同内容：没必要留副本
        else:
            log("      [注意] 同名缓存原内容与本次下载不同，旧包已保留：%s" % os.path.basename(prev))
    return dest


def build_plan(args, requested, arch, allow_net=True):
    """→ dict(kind, src, cache_path, note)。kind ∈ local / cache / url / download。"""
    if args.from_local:
        p = os.path.abspath(args.from_local)
        if not os.path.exists(p):
            fail("--from-local 不存在：%s\n"
                 "       若给 cab/zip：确认包已拷到本机（可分发的内核分件 zip 名字形如\n"
                 "       JZClient-内核-webview2-<版本>-<架构>-v1.zip）；若给已解包目录：目录需含 %s。" % (p, EXE))
        return {"kind": "local", "src": p, "cache_path": None, "note": "人工指定（离线）"}
    if args.url:
        return {"kind": "url", "src": args.url, "cache_path": os.path.join(
            args.cache_dir, cache_name_for(args.url, requested, arch)), "note": "人工指定 URL"}
    hits = cache_hits(args.cache_dir, requested, arch)
    if hits:
        return {"kind": "cache", "src": hits[0], "cache_path": hits[0],
                "note": "缓存命中（%s）" % rel(hits[0])}
    try:
        ver, url = pick_from_official(requested, arch, soft=not allow_net)
    except SystemExit:
        if allow_net:
            raise
        return {"kind": "download", "src": None, "cache_path": None,
                "note": "官方清单无匹配/拉取失败（dry-run 不中止）"}
    except Exception as exc:
        if allow_net:
            raise
        return {"kind": "download", "src": None, "cache_path": None,
                "note": "官方清单拉取失败（%r）——联网后再跑或改用 --from-local/--url" % (exc,)}
    if not url:
        return {"kind": "download", "src": None, "cache_path": None,
                "note": "官方清单无匹配/拉取失败（dry-run 不中止）"}   # soft 模式
    return {"kind": "download", "src": url,
            "cache_path": os.path.join(args.cache_dir, cab_name(ver, arch)),
            "note": "官方清单匹配到 %s" % ver}


def pick_from_official(requested_ver, arch, soft=False):
    items = fetch_official_list()
    cands = [(v, u.get(arch)) for v, u in items if version_matches(requested_ver, v)]
    cands = [(v, u) for v, u in cands if u]
    if not cands:
        avail = "、".join(v for v, _ in items) or "（清单为空）"
        msg = ("官方清单无匹配版本：目标 %s（%s）。当前官方只保留最近 5 个：%s\n"
               "       旧版本（100.x/109.x/92.x 等）官方已下架，CDN 直链 404（2026-10-09 核验）。\n"
               "       可选：① 钉当前可得版本（改 client-build-config.json 的 webview2.version）；\n"
               "             ② 人工下载存档副本 → 放缓存或用 --from-local；\n"
               "             ③ --url 指定可信来源。" % (requested_ver, arch, avail))
        if soft:
            log("      [dry-run] " + msg.splitlines()[0])
            return None, None
        fail(msg)
    return cands[0]


# ---------------- 解包 ----------------
def win_tool(name):
    """取 Windows 系统自带的解包器绝对路径（Git Bash 的 GNU expand/tar 同名会抢 PATH，
    实测 `expand.exe -F:*` 会被 GNU coreutils 的 expand 当非法参数拒绝 → 必须直取 System32）。"""
    cands = []
    if os.name == "nt":
        sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
        cands.append(os.path.join(sysroot, "System32", name))
    cands.append(shutil.which(name) or "")
    for c in cands:
        if not c or not os.path.isfile(c):
            continue
        low = c.replace("/", "\\").lower()
        if "\\git\\" in low or "\\usr\\bin\\" in low or "\\msys" in low:
            continue
        return c
    return None


def extract_pkg(src, staging):
    """cab/zip/目录 → staging。"""
    os.makedirs(staging, exist_ok=True)
    if os.path.isdir(src):
        alt = os.path.join(src, "runtime", "webview2")     # 由 install.ps1 分件目录/程序包目录兜底
        root = alt if os.path.isdir(alt) else src
        log("      复制目录：%s" % root)
        for name in os.listdir(root):
            s = os.path.join(root, name)
            d = os.path.join(staging, name)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        return
    low = src.lower()
    if low.endswith(".zip"):
        with zipfile.ZipFile(src) as zf:
            zf.extractall(staging)
        return
    if low.endswith(".cab"):
        for tool, exe_name, pargs in (
            ("expand.exe", "expand.exe", ["-F:*", src, staging]),
            ("tar(bsdtar)", "tar.exe", ["-xf", src, "-C", staging]),
            ("7z", "7z.exe", ["x", "-y", "-o" + staging, src]),
        ):
            exe = win_tool(exe_name)
            if not exe and tool == "7z":
                exe = shutil.which("7z.exe") or shutil.which("7z")
            if not exe:
                continue
            log("      解包：%s（%s）" % (tool, exe))
            r = subprocess.run([exe] + pargs, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode == 0 and os.listdir(staging):
                return
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
            log("      %s 失败（退出码 %s）：%s" % (tool, r.returncode, " / ".join(tail)))
        fail("CAB 解包失败：expand.exe / tar(bsdtar) / 7z 均失败或不存在。\n"
             "       ① 确认包完整（缓存里的 sha256 见构建日志）；\n"
             "       ② 手工解包：expand.exe -F:* \"<包>\" \"<目标目录>\"，然后 --from-local 指向\n"
             "          解出的含 msedgewebview2.exe 的那层目录（或它的上级目录）；\n"
             "       ③ 或装 7-Zip 后重跑。")
    fail("不认识的包类型（需 .cab/.zip/目录）：%s\n"
         "       evergreen standalone 安装器（MicrosoftEdgeWebView2RuntimeInstallerX64.exe）不是 fixed\n"
         "       内核包，不能用于本流程；请取 FixedVersionRuntime 的 CAB。" % src)


def hoist_kernel_root(staging):
    """cab/zip 内可能套一层目录：找到含 msedgewebview2.exe 的层级并提升到根。→ 被提升目录名。"""
    if os.path.isfile(os.path.join(staging, EXE)):
        return None
    for depth in (1, 2):
        pats = []
        for base, dirs, files in os.walk(staging):
            rel = os.path.relpath(base, staging)
            if rel == ".":
                continue
            if len(rel.split(os.sep)) != depth:
                continue
            if EXE in [f.lower() for f in files]:
                pats.append(base)
        if len(pats) == 1:
            src = pats[0]
            log("      提升内核根：%s → 根" % os.path.relpath(src, staging))
            for name in os.listdir(src):
                shutil.move(os.path.join(src, name), os.path.join(staging, name))
            shutil.rmtree(src, ignore_errors=True)
            return os.path.basename(src)
        if len(pats) > 1:
            fail("包内含多个 %s（%s），无法判定内核根：%s" % (EXE, ", ".join(pats), staging))
    fail("包内未找到 %s（也不是已解包内核目录）：%s" % (EXE, staging))


# ---------------- 裁剪 / 校验 ----------------
def trim_languages(root, keep):
    keep_l = {k.lower() for k in keep}
    removed = kept = 0
    freed = 0
    for base, dirs, files in os.walk(root):
        if os.path.basename(base).lower() != "locales":
            continue
        for f in files:
            if not f.lower().endswith(".pak"):
                continue
            if f[:-len(".pak")].lower() in keep_l:      # 文件名 stem ↔ 语言名（zh-CN.pak ↔ zh-CN）
                kept += 1
                continue
            p = os.path.join(base, f)
            try:
                freed += os.path.getsize(p)
                os.remove(p)
                removed += 1
            except OSError as exc:
                fail("语言包裁剪失败：%s（%r）\n       内核目录可能被占用（客户端在运行）？" % (p, exc))
    if not kept:
        log("      [警告] 没找到 Locales\\*.pak（该包语言布局可能不同）——语言裁剪为空操作")
    return removed, kept, freed


def read_exe_version(exe):
    """PE FileVersion（Windows 内置 PowerShell；读不到返回 None）。"""
    if os.name != "nt":
        return None
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps:
        return None
    try:
        r = subprocess.run([ps, "-NoProfile", "-Command",
                            "(Get-Item -LiteralPath '%s').VersionInfo.FileVersion" % exe],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        v = (r.stdout or "").strip()
        m = re.search(r"\d+\.\d+\.\d+\.\d+", v)
        return m.group(0) if m else None
    except Exception:
        return None


def write_marker(root, ver, arch, source, extra):
    lines = ["kernel-marker-%s" % ver, "arch=%s" % arch, "source=%s" % source,
             "built=%s" % time.strftime("%Y-%m-%d %H:%M:%S")]
    lines += ["%s=%s" % (k, v) for k, v in extra.items() if v]
    with open(os.path.join(root, "KERNEL_MARKER"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")


def replace_out(staging, out):
    """原子替换：旧目录先改名，成功后删；失败回滚。"""
    old = None
    if os.path.exists(out):
        old = out + ".old-" + time.strftime("%Y%m%d%H%M%S")
        try:
            os.replace(out, old)
        except OSError as exc:
            fail("旧内核目录改名失败：%s（%r）\n       客户端/资源管理器可能占用 msedgewebview2.exe，请先关闭客户端。" % (out, exc))
    try:
        os.replace(staging, out)
    except OSError as exc:
        if old:
            try:
                os.replace(old, out)
            except OSError:
                pass
            fail("落盘失败，已回滚旧目录：%s（%r）" % (out, exc))
        fail("落盘失败：%s（%r）" % (out, exc))
    if old:
        shutil.rmtree(old, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(add_help=True, description="WebView2 Fixed Version 内核获取/裁剪/marker")
    ap.add_argument("--list", action="store_true", help="只核验官方当前可得固定版清单")
    ap.add_argument("--version", default="", help="目标版本（缺省读构建配置；含 x 段=前缀匹配）")
    ap.add_argument("--arch", default="x64", choices=("x64", "x86", "arm64"))
    ap.add_argument("--out", default="", help="输出目录（缺省 deploy/桌面端/<product>/runtime/webview2）")
    ap.add_argument("--cache-dir", default=CACHE_DIR, help="包缓存目录（缺省 .workbuddy/cache）")
    ap.add_argument("--from-local", default="", help="本地 cab/zip/已解包目录（离线路径）")
    ap.add_argument("--url", default="", help="下载地址（覆盖官方清单匹配）")
    ap.add_argument("--keep-langs", default="zh-CN,en-US", help="保留语言包（缺省 zh-CN,en-US）")
    ap.add_argument("--allow-version-mismatch", action="store_true", help="版本不符不红灯")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不下载/不解包/不落盘")
    ap.add_argument("--zip", action="store_true", dest="make_zip", help="另出可分发的内核分件 zip")
    ap.add_argument("--zip-dir", default="", help="分件 zip 输出目录（缺省 deploy/桌面端/运行时）")
    args = ap.parse_args()

    if args.list:
        return cmd_list()

    cfg = load_cfg()
    product = cfg["product"]["name"]
    requested = args.version or (cfg.get("webview2") or {}).get("version") or ""
    keep = [s for s in (args.keep_langs or "").split(",") if s]
    out = args.out or os.path.join(ROOT, "deploy", "桌面端", product, "runtime", "webview2")

    log("== WebView2 内核分件（固定版）==")
    log("   目标版本：%s（%s）%s" % (requested or "未指定", args.arch,
        "［前缀匹配：配置里是占位版本］" if is_placeholder(requested) else ""))
    log("   输出目录：%s" % out)
    log("   保留语言：%s" % "、".join(keep))

    plan = build_plan(args, requested, args.arch, allow_net=not args.dry_run)
    kind, src = plan["kind"], plan["src"]
    log("   取包来源：%s" % plan["note"])
    if src:
        log("             %s" % src)

    if args.dry_run:
        if kind == "download":
            if src:
                log("  [dry-run] 将下载：%s" % src)
            log("  [dry-run] 缓存落点：%s" % plan["cache_path"])
        elif kind == "url":
            log("  [dry-run] 将下载并缓存：%s → %s" % (src, plan["cache_path"]))
        else:
            log("      %s存在，直接使用" % ("缓存" if kind == "cache" else "本地包/目录"))
        steps = ["解包 → 暂存目录", "提升内核根（若包内套目录）",
                 "语言裁剪：Locales 只留 %s" % "、".join(keep),
                 "断言 %s 存在 + 版本核验" % EXE,
                 "写 KERNEL_MARKER（首行 kernel-marker-<实际版本>）",
                 "原子替换：%s" % out]
        if args.make_zip:
            steps.append("另出分件 zip：deploy/桌面端/运行时/JZClient-内核-webview2-*.zip")
        log("")
        for i, s in enumerate(steps, 1):
            log("  [dry-run] %d) %s" % (i, s))
        log("")
        log("  去掉 --dry-run 即按上述执行。")
        return 0

    staging = os.path.join(out + ".staging")
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)

    log("[1/6] 取包")
    cab_sha = ""
    origin = src
    if kind in ("download", "url"):
        src = download(src, plan["cache_path"])
        log("      已缓存：%s（%.1f MB）" % (rel(src), os.path.getsize(src) / 1048576.0))
    if os.path.isfile(src):
        cab_sha = file_sha(src)
        log("      sha256：%s" % cab_sha)

    log("[2/6] 解包 → %s" % rel(staging))
    hoisted = None
    try:
        extract_pkg(src, staging)
        hoisted = hoist_kernel_root(staging)
    except SystemExit:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        fail("解包失败：%r（包：%s）" % (exc, src))

    log("[3/6] 语言裁剪（只留 %s）" % "、".join(keep))
    removed, kept, freed = trim_languages(staging, keep)
    log("      删除 %d 个语言包（%.1f MB），保留 %d 个" % (removed, freed / 1048576.0, kept))

    log("[4/6] 断言与版本核验")
    exe = os.path.join(staging, EXE)
    if not os.path.isfile(exe):
        shutil.rmtree(staging, ignore_errors=True)
        fail("产物缺 %s：%s\n       fixed 内核必须含该可执行文件，否则 client_config 不会启用 fixed。" % (EXE, staging))
    exe_ver = read_exe_version(exe)
    actual = exe_ver
    if exe_ver:
        log("      exe FileVersion：%s" % exe_ver)
    else:
        mk = read_marker_version(staging)
        hint = None
        if hoisted:
            m = re.search(r"FixedVersionRuntime\.(\d+\.\d+\.\d+\.\d+)", hoisted)
            hint = m.group(1) if m else None
        actual = mk or hint
        if actual:
            log("      版本取自%s：%s（exe 读不到 FileVersion）"
                % ("包内 KERNEL_MARKER" if mk else "包内目录名", actual))
        else:
            log("      [警告] 读不到 exe FileVersion，包内也无 KERNEL_MARKER/版本目录名——版本核验按「未确认」放行")
    m = version_matches(requested, actual) if actual else None
    if m is False and not args.allow_version_mismatch:
        shutil.rmtree(staging, ignore_errors=True)
        fail("版本不符：目标 %s，产物 %s。\n"
             "       确认要用该包：--allow-version-mismatch（marker 仍写实际版本）；\n"
             "       或改 client-build-config.json 的 webview2.version 为 %s。" % (requested, actual, actual))
    if m:
        log("      版本核验通过（目标 %s）" % requested)
    total, files = dir_size(staging)
    log("      内核体积：%.1f MB / %d 个文件" % (total / 1048576.0, files))

    log("[5/6] 写 marker 并落盘")
    source_note = {"local": "local:%s" % os.path.basename(src),
                   "cache": "cache:%s" % os.path.basename(src)}.get(kind, origin or src)
    write_marker(staging, actual or (prefix_of(requested).rstrip(".") or requested), args.arch,
                 source_note, {"exe_version": exe_ver or "", "cab_sha256": cab_sha,
                               "trimmed_langs": str(removed), "files": str(files),
                               "size_mb": "%.1f" % (total / 1048576.0)})
    replace_out(staging, out)
    log("      已落盘：%s" % out)
    with open(os.path.join(out, "KERNEL_MARKER"), encoding="utf-8") as f:
        log("      KERNEL_MARKER：" + f.readline().strip())

    log("[6/6] 自检")
    ok_exe = os.path.isfile(os.path.join(out, EXE))
    ok_marker = os.path.isfile(os.path.join(out, "KERNEL_MARKER"))
    leftover = []
    kept_paks = 0
    for base, _dirs, fs in os.walk(out):
        if os.path.basename(base).lower() == "locales":
            for f in fs:
                if not f.lower().endswith(".pak"):
                    continue
                if f[:-len(".pak")].lower() in {k.lower() for k in keep}:
                    kept_paks += 1
                else:
                    leftover.append(f)
    if not (ok_exe and ok_marker) or leftover:
        fail("自检未过：exe=%s marker=%s 多余语言包=%s" % (ok_exe, ok_marker, leftover))
    log("      %s + KERNEL_MARKER 就位；Locales 保留 %d 个（%s）" % (EXE, kept_paks, "、".join(keep)))

    if args.make_zip:
        zdir = args.zip_dir or os.path.join(ROOT, "deploy", "桌面端", "运行时")
        os.makedirs(zdir, exist_ok=True)
        tag = re.sub(r"[^0-9A-Za-z._-]", "_", actual or requested)
        zpath = os.path.join(zdir, "JZClient-内核-webview2-%s-%s-v1.zip" % (tag, args.arch))
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for base, _dirs, fs in os.walk(out):
                for fn in fs:
                    full = os.path.join(base, fn)
                    arc = os.path.join("webview2", os.path.relpath(full, out)).replace("\\", "/")
                    zf.write(full, arc)
        log("      分件：%s（%.1f MB）" % (zpath, os.path.getsize(zpath) / 1048576.0))
        log("      目标机装载：install.ps1 -Kernel <该 zip>")

    log("完成：%s（%.1f MB）" % (out, dir_size(out)[0] / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
