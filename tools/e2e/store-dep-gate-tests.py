# -*- coding: utf-8 -*-
r"""商店依赖发布门槛台架（批次 10，2026-10-07 流程改版）。

流程（需求方拍板）：包内声明的依赖逐项核验（✓ 主包自带 / 已上架依赖条目可满足 → ✓；
否则 ×）→ 依赖不满足**允许提交审核但审核不通过** → 待依赖组件包上架（任意用户）后发布。

链路：真服务 exe + 登录 + 上架(声明 cv2 的假本地插件) → approve 应 409 →
上架 cv2 组件包 → approve 应成功 → 列表依赖列应全 ✓。

用法：python tools/e2e/store-dep-gate-tests.py   （退出码 0=全过）
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER_PORT = 61799

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("  %s %s%s" % ("✅" if ok else "❌", name, (" ｜ " + str(detail)) if detail and not ok else ""))


def make_plugin_zip(path, pid, requires_names):
    """假本地插件包：manifest.requires 声明传入的依赖名。"""
    d = os.path.join(os.path.dirname(path), pid)
    os.makedirs(os.path.join(d, "frontend"), exist_ok=True)
    pkgs = [{"name": n, "version": "", "required": False,
             "hint": "依赖组件包：%s（商店依赖类目一键安装）" % n} for n in requires_names]
    m = {"id": pid, "name": pid, "icon": "🧪", "version": "1.0.0", "runtime": "local",
         "kind": "local", "entry": {"frontend": "index.html"},
         "requires": {"python_packages": pkgs}}
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    with open(os.path.join(d, "frontend", "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>%s</html>" % pid)
    with zipfile.ZipFile(path, "w") as zf:
        for root, _dirs, files in os.walk(d):
            for name in files:
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, d))
    shutil.rmtree(d, ignore_errors=True)


def main():
    work = tempfile.mkdtemp(prefix="dep-gate-")
    try:
        app_dir = os.path.abspath(os.path.join(ROOT, "deploy", "服务端", "JZToolsHub-v2.3.23"))
        env = dict(os.environ, JZTOOLS_DATA_ROOT=os.path.join(work, "d"),
                   JZTOOLS_PORT=str(SERVER_PORT), JZTOOLS_HOST="127.0.0.1")
        srv = subprocess.Popen([os.path.join(app_dir, "JZToolsHub.exe")], cwd=app_dir,
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        base = "http://127.0.0.1:%d" % SERVER_PORT
        for _ in range(60):
            time.sleep(0.5)
            try:
                requests.get(base + "/login", timeout=2)
                break
            except Exception:
                continue

        s = requests.Session()
        r = s.post(base + "/api/login", json={"username": "admin", "password": "admin123"}, timeout=5)
        check("登录", r.status_code == 200)

        def upload_zip(path, runtime, name=""):
            with open(path, "rb") as f:
                return s.post(base + "/plugin/admin/store/upload", timeout=30,
                              data={"runtime": runtime, "name": name},
                              files={"file": (os.path.basename(path), f)})

        def review(pid, version, action="approve"):
            return s.post(base + "/plugin/admin/store/review", timeout=60,
                          json={"id": pid, "version": version, "action": action})

        # 1) 上架声明 cv2 依赖的假插件 → 待审核
        fake = os.path.join(work, "local-depgate-1.0.0.zip")
        make_plugin_zip(fake, "local-depgate", ["cv2"])
        r = upload_zip(fake, "local")
        check("1a 上架声明 cv2 的插件", r.json().get("ok"), r.text[:160])

        lst = s.get(base + "/plugin/admin/store/list", timeout=10).json()
        entry = next((p for p in lst["packages"] if p["id"] == "local-depgate"), {})
        names = [x.get("name") for x in (entry.get("requires") or {}).get("python_packages", [])]
        check("1b list 返回依赖声明与主包自带集", names == ["cv2"] and "flask" in lst.get("installed_names", []),
              {"names": names, "installed": lst.get("installed_names", [])[:4]})

        # 2) 依赖不满足 → 审核不通过（409）
        r = review("local-depgate", "1.0.0", "approve")
        check("2 依赖不满足审核不通过（409）", r.status_code == 409 and "依赖不满足" in r.json().get("error", ""),
              (r.status_code, r.text[:120]))

        # 3) 其他用户上架依赖组件包（cv2）——待审核
        dep_zip = os.path.join(ROOT, "deploy", "依赖包", "组件包", "JZToolsHub-依赖-cv2-v5.0.0.93.zip")
        r = upload_zip(dep_zip, "dep-slot")
        check("3a 上架 cv2 依赖组件包", r.json().get("ok"), r.text[:160])
        prov = r.json().get("provides") or []
        check("3b 组件包 provides 自动提取（含 cv2）", "cv2" in prov, prov)
        dep_id, dep_ver = r.json().get("id"), r.json().get("version")

        # 3c) 插件发布仍被拦（依赖条目未发布，客户端不可见——门槛口径=已发布）
        r = review("local-depgate", "1.0.0", "approve")
        check("3c 依赖条目仅上架未发布时仍拦截", r.status_code == 409, (r.status_code, r.text[:100]))

        # 3d) 审核通过依赖条目（dep-slot 发布无部署环节）
        r = review(dep_id, dep_ver, "approve")
        check("3d 依赖条目审核通过", r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text[:120]))

        # 4) 依赖已满足 → 插件发布成功
        r = review("local-depgate", "1.0.0", "approve")
        check("4 依赖满足后发布成功", r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text[:160]))

        # 5) 列表依赖核验展示数据成立
        lst = s.get(base + "/plugin/admin/store/list", timeout=10).json()
        entry = next((p for p in lst["packages"] if p["id"] == "local-depgate"), {})
        check("5 已发布条目依赖声明保留", (entry.get("requires") or {}).get("python_packages", []) != [])

        ok = all(RESULTS)
        print("== 依赖发布门槛台架：%d/%d 通过 ==" % (sum(RESULTS), len(RESULTS)))
        return 0 if ok else 1
    finally:
        try:
            srv.terminate()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
