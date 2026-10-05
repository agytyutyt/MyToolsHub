# -*- coding: utf-8 -*-
"""客户端插件包安装单入口（批次 8 T-O2-4，接缝③收口）。

所有安装路径（本地上传 /local-api/plugins/install 与商店拉包 /local-api/store/install）
都收口到本模块的三个函数，校验口径与 admin 商店上架机器校验同源
（设计文档 §3.3 机器校验 / §3.5 安装闭环）：

    install_local_zip      本地插件包（zip 根即插件目录；六连校验，与原 lp_install 逐条一致）
    install_frontend_zip   server 前端包（frontend-manifest.json 文件清单圈定本地伺服范围）
    install_component_zip  依赖组件包（runtime/pylibs .pending 暂存语义，复用主包 jz_deps 机制）

哈希校验（sha256 对 catalog）由调用方在下载后执行——本模块只管「包 → 落位」。
"""

import json
import os
import re
import shutil
import zipfile

ID_RE = re.compile(r"^local-[A-Za-z0-9_-]+$")
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
FRONTEND_MANIFEST = "frontend-manifest.json"
NATIVE_EXTS = (".pyd", ".dll")


def sha256_file(path):
    h = __import__("hashlib").sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_zip_json(zf, name):
    try:
        return json.loads(zf.read(name).decode("utf-8-sig"))
    except Exception:
        return None


def _zip_names(zf):
    return [n.replace("\\", "/") for n in zf.namelist()]


def _paths_safe(names):
    for n in names:
        if n.startswith("/") or ".." in n.split("/"):
            return False, n
    return True, ""


# ===================== 本地插件包（类 B/C） =====================

def install_local_zip(zip_path, local_plugins_dir, manifests, log=None):
    """安装本地插件包（zip 根即插件目录）。六连校验与原 capability_app.lp_install 逐条一致；
    校验全过才落盘（升级 = 整目录替换）。调用方负责安装后 rescan。

    返回 {ok, id?, restart_required?, hint?, error?}。
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return {"ok": False, "error": "插件包不是有效的 zip"}
    try:
        return _install_local_zip_inner(zf, local_plugins_dir, manifests, log)
    finally:
        zf.close()


def _install_local_zip_inner(zf, local_plugins_dir, manifests, log=None):
    """install_local_zip 的实际逻辑（zf 生命周期由外层管理）。"""
    names = _zip_names(zf)
    if "manifest.json" not in names:
        return {"ok": False, "error": "包根缺少 manifest.json"}
    ok, bad = _paths_safe(names)
    if not ok:
        return {"ok": False, "error": "插件包含不安全路径：%s" % bad}
    m = read_zip_json(zf, "manifest.json")
    if not isinstance(m, dict):
        return {"ok": False, "error": "manifest.json 不可解析"}
    pid = m.get("id", "")
    if m.get("runtime") != "local" or not ID_RE.match(str(pid)):
        return {"ok": False, "error": "id 必须 local- 前缀且 runtime=local"}
    kind = m.get("kind", "local")
    if kind not in ("local", "hybrid"):
        return {"ok": False, "error": "kind 仅支持 local / hybrid"}
    frontend = (m.get("entry") or {}).get("frontend") or "index.html"
    if "frontend/" + frontend not in names:
        return {"ok": False, "error": "包内缺少 frontend/%s" % frontend}
    if kind == "hybrid":
        backend = (m.get("entry") or {}).get("backend")
        if not backend or "backend/" + backend.split("/")[-1] not in names:
            return {"ok": False, "error": "hybrid 插件缺少 backend"}
    mount = ((m.get("entry") or {}).get("mount") or "").rstrip("/")
    if kind == "hybrid" and not mount.startswith("/api/local/" + pid):
        return {"ok": False, "error": "mount 必须以 /api/local/%s 开头" % pid}
    for other in manifests:                              # mount 冲突检查（双方均非空才比）
        om = ((other.get("entry") or {}).get("mount") or "").rstrip("/")
        if other.get("id") != pid and om and mount and (
                mount == om or mount.startswith(om + "/") or om.startswith(mount + "/")):
            return {"ok": False, "error": "mount 与 %s 冲突" % other.get("id")}
    target = os.path.join(local_plugins_dir, pid)
    if os.path.isdir(target):
        shutil.rmtree(target)                            # 升级 = 覆盖替换（zip 根即包根）
    os.makedirs(local_plugins_dir, exist_ok=True)
    zf.extractall(target)
    return {"ok": True, "id": pid, "restart_required": kind == "hybrid",
            "hint": "类 C 插件需重启能力层生效（会话已持久化，免重登）"
            if kind == "hybrid" else ""}


# ===================== server 前端包（需求⑧） =====================

def scan_frontends(frontends_dir):
    """扫描已安装的 server 前端包 → [{id, version, files}]（本地伺服范围以清单为准）。"""
    out = []
    if not os.path.isdir(frontends_dir):
        return out
    for name in sorted(os.listdir(frontends_dir)):
        mp = os.path.join(frontends_dir, name, FRONTEND_MANIFEST)
        if not SERVER_ID_RE.match(name) or not os.path.isfile(mp):
            continue
        try:
            with open(mp, "r", encoding="utf-8-sig") as f:
                m = json.load(f)
        except Exception:
            continue
        if isinstance(m, dict) and m.get("id") == name:
            out.append({"id": name, "version": str(m.get("version") or ""),
                        "files": [str(x) for x in (m.get("files") or [])]})
    return out


def load_frontend_manifest(frontends_dir, pid):
    mp = os.path.join(frontends_dir, pid, FRONTEND_MANIFEST)
    if not os.path.isfile(mp):
        return None
    try:
        with open(mp, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def install_frontend_zip(zip_path, frontends_dir):
    """安装 server 前端包 → frontends_dir\\<id>\\（frontend/ 内容 + frontend-manifest.json）。

    校验：清单存在且可解析 / id 合法且非 local- 前缀（命名空间铁律）/ 路径安全 /
    清单声明的每个文件都真实存在于包内 frontend/（清单即本地伺服白名单）。
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return {"ok": False, "error": "前端包不是有效的 zip"}
    try:
        return _install_frontend_zip_inner(zf, frontends_dir)
    finally:
        zf.close()


def _install_frontend_zip_inner(zf, frontends_dir):
    """install_frontend_zip 的实际逻辑（zf 生命周期由外层管理）。"""
    names = _zip_names(zf)
    ok, bad = _paths_safe(names)
    if not ok:
        return {"ok": False, "error": "前端包含不安全路径：%s" % bad}
    m = read_zip_json(zf, FRONTEND_MANIFEST)
    if not isinstance(m, dict):
        return {"ok": False, "error": "包根缺少 %s" % FRONTEND_MANIFEST}
    pid = str(m.get("id") or "")
    if not SERVER_ID_RE.match(pid) or pid.startswith("local-"):
        return {"ok": False, "error": "前端包 id 非法（不得 local- 前缀）：%r" % pid}
    files = [str(x).replace("\\", "/") for x in (m.get("files") or []) if str(x).strip()]
    if not files:
        return {"ok": False, "error": "前端清单 files 为空"}
    for f in files:
        if f.startswith("/") or ".." in f.split("/"):
            return {"ok": False, "error": "前端清单含不安全路径：%s" % f}
        if "frontend/" + f not in names:
            return {"ok": False, "error": "清单声明文件缺失：frontend/%s" % f}
    target = os.path.join(frontends_dir, pid)
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)
    for info in zf.infolist():
        rel = info.filename.replace("\\", "/")
        if rel.endswith("/") or not rel.startswith("frontend/"):
            continue
        dest = os.path.join(target, *rel[len("frontend/"):].split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
    # 落盘清单（含 files 白名单与版本，供本地伺服路由与升级比对）
    with open(os.path.join(target, FRONTEND_MANIFEST), "w", encoding="utf-8") as f:
        json.dump({"id": pid, "version": str(m.get("version") or ""), "files": files},
                  f, ensure_ascii=False, indent=2)
    return {"ok": True, "id": pid, "version": str(m.get("version") or "")}


def remove_frontend(frontends_dir, pid):
    """卸载 server 前端本地副本（回退中继直出；数据根数据不在此处，无涉）。

    Windows 上刚被伺服过的文件句柄释放有延迟：短暂重试，删不掉如实报错（不静默假装成功）。
    """
    import time
    target = os.path.join(frontends_dir, pid)
    if not SERVER_ID_RE.match(pid) or not os.path.isdir(target):
        return {"ok": False, "error": "前端副本不存在"}
    for _ in range(4):
        shutil.rmtree(target, ignore_errors=True)
        if not os.path.isdir(target):
            return {"ok": True}
        time.sleep(0.25)
    return {"ok": False, "error": "目录删除失败（文件被占用，请稍后重试）：%s" % pid}


# ===================== 依赖组件包（dep-slot） =====================

def _component_entry(zf):
    """读依赖组件包清单（dep-component.json / manifest.json components），与主包同口径。

    zf 由调用方打开并管理生命周期（P2-3：原实现自开自漏，临时包文件会被句柄短暂锁住）。
    """
    meta = read_zip_json(zf, "dep-component.json") or {}
    inner = read_zip_json(zf, "manifest.json") or {}
    cs = inner.get("components")
    if isinstance(cs, dict):
        cs = [cs]
    entry = (cs or [None])[0]
    return meta, entry


def _extract_pylibs_payload(zf, dest_dir):
    """把 payload/pylibs/** 解压到 dest_dir（.pending 暂存语义的载荷部分）。"""
    n = 0
    for info in zf.infolist():
        rel = info.filename.replace("\\", "/")
        if rel.endswith("/") or not rel.startswith("payload/pylibs/"):
            continue
        tail = rel[len("payload/pylibs/"):]
        if not tail or tail == "manifest.json":
            continue
        dest = os.path.join(dest_dir, *tail.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        n += 1
    return n


def install_component_zip(zip_path, program_dir, apply_now=None):
    """依赖组件包 → runtime/pylibs/.pending/<id>/ 暂存（复用主包 jz_deps 机制）。

    apply_now：立即应用回调（缺省用 jz_deps.apply_pending_components）。纯 Python 组件
    （entry.files 无 .pyd/.dll）暂存后立即应用、刷新即生效；DLL 类留在暂存区，
    下次能力层启动时应用（应用逻辑同一份 jz_deps.apply_pending_components）。

    返回 {ok, id?, version?, pending?, native?, error?}。
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return {"ok": False, "error": "依赖包不是有效的 zip"}
    try:
        return _install_component_zip_inner(zf, program_dir, apply_now)
    finally:
        zf.close()


def _install_component_zip_inner(zf, program_dir, apply_now=None):
    """install_component_zip 的实际逻辑（zf 生命周期由外层管理）。"""
    import jz_deps
    meta, entry = _component_entry(zf)
    if not entry or not entry.get("id"):
        return {"ok": False, "error": "包内缺少组件清单（dep-component.json / manifest.json）"}
    cid = str(entry["id"])
    if not SERVER_ID_RE.match(cid):
        return {"ok": False, "error": "组件 id 非法：%r" % cid}
    pylibs = os.path.join(program_dir, "runtime", "pylibs")
    stage = os.path.join(pylibs, ".pending", cid)
    if os.path.isdir(stage):
        shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)
    try:
        n = _extract_pylibs_payload(zf, os.path.join(stage, "payload"))
    except Exception as e:
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": False, "error": "暂存组件载荷失败：%s" % e}
    if n <= 0:
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": False, "error": "包内没有可安装的载荷（payload/pylibs 为空）"}
    prev_files = list(entry.get("files") or [])
    info = {"schema": 1, "id": cid, "entry": entry,
            "remove": [str(x).lstrip("\\/") for x in prev_files if str(x).strip()],
            "staged_by": "client-store"}
    with open(os.path.join(stage, "entry.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    native = any(str(f2).lower().endswith(NATIVE_EXTS) for f2 in prev_files)
    applied = []
    if not native:
        apply = apply_now or (lambda: jz_deps.apply_pending_components(program_dir))
        try:
            applied = apply() or []
        except Exception:
            applied = []
    return {"ok": True, "id": cid, "version": str(entry.get("version") or ""),
            "pending": cid not in applied, "native": native}


def installed_components(program_dir):
    """已装依赖组件 [{id, version}]（runtime/pylibs/manifest.json）+ 暂存中标记。"""
    import jz_deps
    m_path = os.path.join(program_dir, "runtime", "pylibs", "manifest.json")
    comps = []
    try:
        with open(m_path, "r", encoding="utf-8-sig") as f:
            raw = (json.load(f) or {}).get("components") or []
    except Exception:
        raw = []
    for c in raw:
        if isinstance(c, dict) and c.get("id"):
            comps.append({"id": str(c["id"]), "version": str(c.get("version") or ""),
                          "pending": False})
    known = {c["id"] for c in comps}
    try:
        for cid in jz_deps.pending_components(program_dir):
            if cid not in known:
                comps.append({"id": cid, "version": "", "pending": True})
    except Exception:
        pass
    return comps
