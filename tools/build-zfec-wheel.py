r"""为 zfec 生成预编译 wheel（Windows / 目标 Python 版本）。

为什么需要这个脚本
------------------
`zfec` 是**版本锁定型 C 扩展**（wheel 标签形如 `cp314-cp314-win_amd64`，不是 abi3），
而 PyPI 上并没有每个新 Python 版本都跟进：

- `zfec 1.6.0.0`（2024-11 发布）的 win_amd64 wheel 只到 cp39/310/311/312/313；
- 最新的 `1.6.0.1.post0` win_amd64 wheel **最高仍只到 cp313**。

因此在 Python 3.14 上 `pip install zfec` 会回退到**源码编译**，要求构建机装有
MSVC Build Tools。为了让打包可复现、让没有编译器的机器也能装依赖，本仓库
`wheels/` 下随包提供一份已构建好的 wheel（用法见 `wheels/README.md`）。

本脚本做什么
------------
把**当前解释器已安装的 zfec** 重打包成标准 wheel。适用于：
- 你在有编译器的机器上已经装好了 zfec（例如 `pip install zfec` 自动源码编译成功），
  想把这份编译产物固化成可分发/可复现的 wheel；
- 或者 3.8 那类"PyPI 只有旧版 wheel"的场景，想固定到源码编译出来的新版本。

若你想从官方 sdist 重新编译而不是重打现有安装，用标准流程即可：
    python -m pip wheel zfec==1.6.0.0 --no-deps -w wheels
（需先进入 vcvars 环境；若报 WinError 2，是 setuptools 在 DISTUTILS_USE_SDK 模式下
剔除了子进程 PATH，改用 cmd + vcvarsall.bat 的标准环境即可。）

用法
----
    python tools/build-zfec-wheel.py                  # 重打 + 校验安装
    python tools/build-zfec-wheel.py --no-verify      # 只重打
    python tools/build-zfec-wheel.py --version 1.6.0.0 --out wheels

注意
----
zfec 采用 **GPL-2+** 许可（元数据 `License: GPL-2+`，另附 Tahoe-LAFS 的
transitory patent policy）。随仓库分发其二进制 wheel 属于"再分发"，请确认
你们的合规口径；若不便于再分发二进制，可去掉 `wheels/` 改为在构建机上现场编译。
许可证原文已随 wheel 一并打包在 `zfec-<ver>.dist-info/licenses/` 下。
"""

import argparse
import base64
import csv
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_INFO_FILES = {"METADATA", "WHEEL", "top_level.txt", "entry_points.txt"}
# 这些是"安装记录"，不属于 wheel 内容
INSTALL_ONLY = {"INSTALLER", "REQUESTED", "RECORD"}


def _b64_sha256(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _find_installed(name: str, version: str | None):
    """定位已安装包与 dist-info 目录。"""
    import importlib.util
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.origin:
        raise SystemExit(
            f"未找到已安装的 {name}。请先在**有 C 编译器的机器**上安装它，"
            f"例如：python -m pip install {name}=={version or ''}".strip("=")
        )
    pkg_dir = os.path.dirname(os.path.abspath(spec.origin))
    site_dir = os.path.dirname(pkg_dir)
    prefix = f"{name}-{version}" if version else f"{name}-"
    cand = [d for d in os.listdir(site_dir)
            if d.startswith(prefix) and d.endswith(".dist-info")]
    if not cand:
        raise SystemExit(f"未找到 {name} 的 dist-info（site-packages: {site_dir}）")
    dist_dir = os.path.join(site_dir, cand[0])
    real_ver = cand[0][len(name) + 1:-len(".dist-info")]
    return pkg_dir, dist_dir, real_ver


def build(out_dir: str, version: str | None) -> str:
    pkg_dir, dist_dir, ver = _find_installed("zfec", version)
    name = os.path.basename(pkg_dir)
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}-cp{sys.version_info.major}{sys.version_info.minor}-win_amd64"
    sp = os.path.dirname(pkg_dir)
    dist_name = os.path.basename(dist_dir)
    print(f"[i] 解释器: {sys.version.split()[0]}")
    print(f"[i] 源包目录: {pkg_dir}")
    print(f"[i] 版本: {ver}  目标标签: {tag}")

    entries: list[tuple[str, bytes]] = []
    for dirpath, dirnames, filenames in os.walk(pkg_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, fn)
            arc = os.path.relpath(full, sp).replace("\\", "/")
            entries.append((arc, open(full, "rb").read()))

    # dist-info：保留元数据、WHEEL、top_level、entry_points 与 licenses/（PEP 639）
    for dirpath, _dirnames, filenames in os.walk(dist_dir):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, dist_dir).replace("\\", "/")
            top = rel.split("/")[0]
            if top in INSTALL_ONLY:
                continue
            if "/" not in rel and rel not in DIST_INFO_FILES:
                continue          # 忽略安装记录类的散落文件
            entries.append((f"{dist_name}/{rel}", open(full, "rb").read()))

    # 归一化 WHEEL
    for i, (arc, data) in enumerate(entries):
        if arc == f"{dist_name}/WHEEL":
            lines = [ln for ln in data.decode("utf-8").splitlines() if ln.strip()]
            lines = [ln for ln in lines
                     if not ln.lower().startswith(("tag:", "root-is-purelib:"))]
            lines += [f"Tag: {tag}", "Root-Is-Purelib: false"]
            entries[i] = (arc, ("\n".join(lines) + "\n").encode("utf-8"))

    # RECORD（含自身空记录）
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for arc, data in sorted(entries, key=lambda x: x[0]):
        w.writerow([arc, f"sha256={_b64_sha256(data)}", str(len(data))])
    w.writerow([f"{dist_name}/RECORD", "", ""])
    entries.append((f"{dist_name}/RECORD", buf.getvalue().encode("utf-8")))

    os.makedirs(out_dir, exist_ok=True)
    whl = os.path.join(out_dir, f"zfec-{ver}-{tag}.whl")
    if os.path.isfile(whl):
        os.remove(whl)
    with zipfile.ZipFile(whl, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, data in entries:
            z.writestr(arc, data)
    print(f"[✓] 生成 {whl}（{os.path.getsize(whl) / 1024:.1f} KB，{len(entries)} 个条目）")
    print("    含扩展:", [a for a, _ in entries if a.endswith(".pyd")])
    print("    含许可证:", [a for a, _ in entries if "/licenses/" in a])
    return whl


def verify(whl: str, ver: str) -> None:
    """在干净目录里真实安装，并跑一次 zfec 纠错往返。"""
    tgt = tempfile.mkdtemp(prefix="jz_zfec_verify_")
    print(f"[i] 验证安装到 {tgt}")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
                        "--target", tgt, whl],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit("安装失败:\n" + (r.stderr or "")[-1500:])
    print("    pip:", (r.stdout or "").strip().splitlines()[-1])

    code = f'''
import sys, hashlib
sys.path.insert(0, r"{tgt}")
import zfec
data = b"JZToolsHub zfec round-trip " * 40
k, m = 8, 12                      # 8 块原始 + 12 块总数（4 块冗余）
padded = data + b"\\0" * ((k - len(data) % k) % k)
chunk = len(padded) // k
enc = zfec.Encoder(k, m)
blocks = enc.encode(padded[i*chunk:(i+1)*chunk] for i in range(k))
dec = zfec.Decoder(k, m)
keep = list(range(k - 4)) + list(range(k, m))     # 故意丢 4 块
rebuilt = b"".join(dec.decode([blocks[i] for i in keep], keep))
ok = rebuilt[:len(data)] == data
print("    zfec:", zfec.__version__)
print("    纠错还原:", "OK" if ok else "失败",
      "| sha256", hashlib.sha256(data).hexdigest()[:16],
      "==", hashlib.sha256(rebuilt[:len(data)]).hexdigest()[:16])
assert ok, "以保留块还原失败"
'''
    r2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    print((r2.stdout or "").rstrip())
    if r2.returncode != 0:
        raise SystemExit("功能验证失败:\n" + (r2.stderr or "")[-1500:])
    print("[✓] 验证通过：该 wheel 可在无编译器的机器上直接安装使用")


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 zfec 预编译 wheel")
    ap.add_argument("--out", default=os.path.join(REPO, "wheels"),
                    help="输出目录（默认 <仓库>/wheels）")
    ap.add_argument("--version", default=None, help="指定版本（默认取已安装版本）")
    ap.add_argument("--no-verify", action="store_true", help="跳过安装+功能验证")
    args = ap.parse_args()

    whl = build(args.out, args.version)
    if not args.no_verify:
        ver = os.path.basename(whl).split("-")[1]
        verify(whl, ver)


if __name__ == "__main__":
    main()
