# -*- coding: utf-8 -*-
"""管理后台 · 商店管理（O2 写面）：上架 / 审核 / 发布 / 下架 / 统计 + 数据契约维护。

与 store 插件（读面）之间**唯一的交互面是数据契约** `数据根\\store\\`（设计文档 §3.4）：
零跨插件 import/调用（插件间独立铁律 / C-11）。本模块只写契约物：

    catalog.json        商店目录（schema 2，versions[]/status/review/history，§4.1）
    packages\\<规范文件名>  包文件原样（上架即原包，不改内容）
    index-server.json   E1：schema-1 flat 投影（仅 server 条目）——admin 既有
                        read_index/batch_apply 零改造可把商店目录当索引源（§3.7）
    stats.jsonl         **只读**（写方是 store 插件；本模块只聚合展示）

版本状态机（§3.7）：pending → published / rejected；published → delisted；
delisted →（重新上架）→ pending。版本占号：同 id 同版本号只要存在记录（含已驳回/
已下架）即拒绝重提。原子写（tmp + os.replace）；单一写方，低频，不做文件锁。

机器校验按包形态分派（§3.3 上架）：本地包六连断言等价检查 / 服务器包
plugin-package.json 结构 + SHA256SUMS 全量核对 / 依赖包组件清单 + wheel tag 预检。
同包内 plugin_admin 的 zip 工具复用（同一插件，无跨插件问题）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile

try:
    from . import plugin_admin            # 同包相对导入（app.py 以包名动态加载）
except Exception:                          # 单元测试环境可能按路径单独加载
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "jztools_admin_plugin_admin",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin_admin.py"))
    plugin_admin = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(plugin_admin)

# ---- 复用同包 plugin_admin 的工具（单一实现，避免两份漂移） ----
read_json = plugin_admin.read_json
write_json = plugin_admin.write_json
sha256_file = plugin_admin.sha256_file
now_iso = plugin_admin.now_iso
now_stamp = plugin_admin.now_stamp
zip_entries_safe = plugin_admin.zip_entries_safe
semver_cmp = plugin_admin.semver_cmp
PLUGIN_ID_RE = plugin_admin.PLUGIN_ID_RE

CATALOG_SCHEMA = 2
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
VERSION_RE = re.compile(r"^\d+(\.\d+)+$")
RUNTIMES = ("local", "server", "dep-slot")
STATUSES = ("pending", "rejected", "published", "delisted")

# 包文件命名族（设计文档 §4.1 注）：上架落 packages\ 时统一改名，消费方按名辨形态
NAME_TEMPLATES = {
    "local": "JZClient-本地插件-{id}-v{version}.zip",
    "server": "JZToolsHub-插件-{id}-v{version}.zip",
    "server_frontend": "JZToolsHub-前端-{id}-v{version}.zip",
    "dep-slot": "JZToolsHub-依赖-{id}-v{version}.zip",
}
# 依赖包 wheel tag 预检（O1 口径）：仅接受 cp314 / py3-none-any
WHEEL_TAG_RE = re.compile(r"^(cp314[^-]*|py3)-none-any\.whl$", re.IGNORECASE)


# ======================== 路径与目录 ========================

def store_dir(data_root):
    return os.path.join(data_root, "store")


def catalog_path(data_root):
    return os.path.join(store_dir(data_root), "catalog.json")


def packages_dir(data_root):
    return os.path.join(store_dir(data_root), "packages")


def stats_path(data_root):
    return os.path.join(store_dir(data_root), "stats.jsonl")


def index_server_path(data_root):
    return os.path.join(store_dir(data_root), "index-server.json")


def ensure_dirs(data_root):
    os.makedirs(packages_dir(data_root), exist_ok=True)


# ======================== catalog 读写 ========================

def load_catalog(data_root):
    """读 catalog.json（写方视角同样防御：外部手工改坏时不至于全盘不可用）。"""
    obj = read_json(catalog_path(data_root))
    if not isinstance(obj, dict) or obj.get("schema") != CATALOG_SCHEMA:
        return {"schema": CATALOG_SCHEMA, "generated_at": now_iso(), "packages": []}
    if not isinstance(obj.get("packages"), list):
        obj["packages"] = []
    return obj


def save_catalog(data_root, obj):
    obj["schema"] = CATALOG_SCHEMA
    obj["generated_at"] = now_iso()
    write_json(catalog_path(data_root), obj)          # 原子写（tmp + os.replace）


def find_entry(catalog, pid):
    for entry in catalog.get("packages") or []:
        if isinstance(entry, dict) and entry.get("id") == pid:
            return entry
    return None


def find_version_record(entry, version):
    for v in entry.get("versions") or []:
        if isinstance(v, dict) and str(v.get("version")) == str(version):
            return v
    return None


def latest_published(entry):
    pub = [v for v in entry.get("versions") or [] if v.get("status") == "published"]
    if not pub:
        return None
    return max(pub, key=lambda v: (tuple(int(x) for x in re.findall(r"\d+", str(v.get("version") or ""))) or (0,),
                                   str(v.get("published_at") or "")))


def _append_history(record, action, by, note=""):
    record.setdefault("history", []).append(
        {"action": action, "by": by, "at": now_iso(), "note": str(note or "")})


# ======================== 机器校验（按包形态） ========================

def _zip_json(zip_path, name):
    return plugin_admin._zip_json(zip_path, name)


def check_local_plugin_zip(zip_path):
    """本地插件包（runtime=local）六连断言等价检查（与客户端 pkg_install.install_local_zip 同源）。"""
    errors, warnings = [], []
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return {"ok": False, "errors": ["不是有效的 zip"], "warnings": warnings}
    names = [n.replace("\\", "/") for n in zf.namelist()]
    ok, bad = zip_entries_safe(zf)
    if not ok:
        errors.append("包内含不安全路径：%s" % bad)
    manifest = _zip_json(zip_path, "manifest.json")
    if not isinstance(manifest, dict):
        errors.append("包根缺少或无法解析 manifest.json")
        return {"ok": False, "errors": errors, "warnings": warnings}
    pid = str(manifest.get("id") or "")
    if manifest.get("runtime") != "local" or not re.match(r"^local-[A-Za-z0-9_-]+$", pid):
        errors.append("manifest 必须 runtime=local 且 id local- 前缀（实为 %r / %r）"
                      % (manifest.get("runtime"), pid))
    if manifest.get("kind", "local") not in ("local", "hybrid"):
        errors.append("kind 仅支持 local / hybrid：%r" % manifest.get("kind"))
    frontend = (manifest.get("entry") or {}).get("frontend") or "index.html"
    if "frontend/" + frontend not in names:
        errors.append("包内缺少 frontend/%s" % frontend)
    if manifest.get("kind") == "hybrid":
        backend = (manifest.get("entry") or {}).get("backend")
        mount = ((manifest.get("entry") or {}).get("mount") or "").rstrip("/")
        if not backend or "backend/" + backend.split("/")[-1] not in names:
            errors.append("hybrid 插件缺少 backend")
        if not mount.startswith("/api/local/" + pid):
            errors.append("mount 必须以 /api/local/%s 开头" % pid)
    if not manifest.get("version") or not VERSION_RE.match(str(manifest.get("version"))):
        errors.append("manifest.version 不是语义化版本号：%r" % manifest.get("version"))
    return {"ok": not errors, "errors": errors, "warnings": warnings, "manifest": manifest}


def check_server_backend_zip(zip_path):
    """服务器插件后端包：plugin-package.json 结构 + SHA256SUMS 全量核对（含 frontend/）。

    不做「与目标机已装版本比对」（那是部署时 inspect/apply 的职责）——商店上架只管包本身。
    """
    errors, warnings = [], []
    size = os.path.getsize(zip_path)
    if size > plugin_admin.MAX_ZIP_BYTES:
        return {"ok": False, "errors": ["包体积超过上限"], "warnings": warnings}
    meta = _zip_json(zip_path, "plugin-package.json")
    if not isinstance(meta, dict):
        return {"ok": False, "errors": ["包内缺少或无法解析 plugin-package.json"], "warnings": warnings}
    if meta.get("schema") != 1:
        errors.append("plugin-package.json schema 不受支持：%r" % meta.get("schema"))
    if meta.get("kind") != "plugin-upgrade":
        errors.append("包类型不是 plugin-upgrade：%r" % meta.get("kind"))
    pid = str(meta.get("id") or "")
    version = str(meta.get("version") or "")
    if not PLUGIN_ID_RE.match(pid):
        errors.append("包清单 id 非法：%r" % pid)
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        errors.append("包清单版本号不是 主.次.修订：%r" % version)
    manifest = _zip_json(zip_path, "payload/plugins/%s/manifest.json" % pid)
    if not isinstance(manifest, dict):
        errors.append("包结构不符：缺少 payload/plugins/%s/manifest.json" % pid)
    else:
        if str(manifest.get("id")) != pid:
            errors.append("id 不一致：包清单 %s，manifest.json %s" % (pid, manifest.get("id")))
        if str(manifest.get("version")) != version:
            errors.append("版本不一致：包清单 %s，manifest.json %s" % (version, manifest.get("version")))
        if not isinstance(manifest.get("frontend"), (str, type(None))) and manifest.get("entry"):
            warnings.append("manifest.entry 形态异常：%r" % manifest.get("entry"))
    if not any(n.replace("\\", "/").startswith("payload/plugins/%s/frontend/" % pid)
               for n in _zip_names(zip_path)):
        warnings.append("后端包内没有 frontend/（服务器无法服务浏览器路径）")
    # SHA256SUMS 全量核对（比安装器更严：上架即冻结，装哪台机都过）
    try:
        zf = zipfile.ZipFile(zip_path)
        ok, bad = zip_entries_safe(zf)
        if not ok:
            errors.append("包内含不安全路径：%s" % bad)
        sums = None
        for n in zf.namelist():
            if n.replace("\\", "/") == "SHA256SUMS":
                sums = zf.read(n).decode("utf-8-sig")
                break
        if sums is None:
            errors.append("包内缺少 SHA256SUMS")
        else:
            bad_files = []
            count = 0
            for line in sums.splitlines():
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                if len(line) < 66 or line[64:66] != "  " or not re.match(r"^[0-9a-f]{64}$", line[:64]):
                    errors.append("SHA256SUMS 行格式不合法：%s" % line[:80])
                    continue
                rel, digest = line[66:].replace("\\", "/"), line[:64]
                if not rel.startswith("plugins/"):
                    errors.append("SHA256SUMS 含 plugins 外路径：%s" % rel)
                    continue
                try:
                    actual = __import__("hashlib").sha256(
                        zf.read("payload/" + rel)).hexdigest()
                except KeyError:
                    bad_files.append("%s（包内缺失）" % rel)
                    continue
                if actual != digest:
                    bad_files.append("%s（期望 %s，实际 %s）" % (rel, digest[:12], actual[:12]))
                count += 1
            if bad_files:
                errors.append("文件哈希校验未通过：%s" % "；".join(bad_files[:6]))
            if count == 0:
                errors.append("SHA256SUMS 没有可用条目")
    except zipfile.BadZipFile:
        return {"ok": False, "errors": ["不是有效的 zip"], "warnings": warnings}
    return {"ok": not errors, "errors": errors, "warnings": warnings, "meta": meta, "manifest": manifest}


def _zip_names(zip_path):
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return [n.replace("\\", "/") for n in zf.namelist()]
    except Exception:
        return []


def check_frontend_zip(zip_path, expect_id="", expect_version=""):
    """server 前端包：frontend-manifest.json（文件清单）结构 + 清单文件存在性核对。"""
    errors, warnings = [], []
    names = _zip_names(zip_path)
    manifest = _zip_json(zip_path, "frontend-manifest.json")
    if not isinstance(manifest, dict):
        return {"ok": False, "errors": ["前端包根缺少或无法解析 frontend-manifest.json"], "warnings": warnings}
    pid = str(manifest.get("id") or "")
    version = str(manifest.get("version") or "")
    if not ID_RE.match(pid) or pid.startswith("local-"):
        errors.append("前端清单 id 非法（须服务器插件 id，不得 local- 前缀）：%r" % pid)
    if not VERSION_RE.match(version):
        errors.append("前端清单版本号不是语义化版本：%r" % version)
    if expect_id and pid != expect_id:
        errors.append("前端包 id 与后端包不一致：%s vs %s" % (pid, expect_id))
    if expect_version and version != expect_version:
        errors.append("前后端包版本必须成对（同版本号）：前端 %s，后端 %s" % (version, expect_version))
    files = [str(x).replace("\\", "/") for x in (manifest.get("files") or []) if str(x).strip()]
    if not files:
        errors.append("前端清单 files 为空（本地伺服范围无从圈定）")
    for f in files:
        if f.startswith("/") or ".." in f.split("/"):
            errors.append("前端清单含不安全路径：%s" % f)
        elif "frontend/" + f not in names:
            errors.append("清单声明文件缺失：frontend/%s" % f)
    listed = set(files)
    for n in names:
        if n.endswith("/") or not n.startswith("frontend/"):
            continue
        rel = n[len("frontend/"):]
        if rel and rel not in listed:
            warnings.append("包内文件未列入清单（本地不会伺服该文件）：%s" % rel)
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "manifest": manifest if not errors else None}


def check_dep_zip(zip_path):
    """依赖包（dep-slot）：组件清单结构 + wheel tag 预检（cp314 / py3-none-any）。"""
    errors, warnings = [], []
    meta = _zip_json(zip_path, "dep-component.json") or {}
    inner = _zip_json(zip_path, "manifest.json") or {}
    cs = inner.get("components")
    if isinstance(cs, dict):
        cs = [cs]
    entry = (cs or [None])[0]
    if not isinstance(entry, dict) or not entry.get("id"):
        errors.append("包内缺少组件清单（dep-component.json / manifest.json components）")
        return {"ok": False, "errors": errors, "warnings": warnings, "entry": None}
    cid = str(entry["id"])
    if not PLUGIN_ID_RE.match(cid):
        errors.append("组件 id 非法：%r" % cid)
    if not str(entry.get("version") or ""):
        errors.append("组件清单缺少 version")
    if not entry.get("provides"):
        warnings.append("组件清单未声明 provides（客户端预检无法把缺依赖映射到本包）")
    names = _zip_names(zip_path)
    if not any(n.startswith("payload/pylibs/") for n in names):
        errors.append("包内没有 payload/pylibs 载荷")
    wheels = [n.split("/")[-1] for n in names if n.lower().endswith(".whl")]
    bad_tags = [w for w in wheels if not WHEEL_TAG_RE.match(w)]
    if bad_tags:
        errors.append("wheel tag 预检未通过（仅接受 cp314*/py3-none-any，O1 口径）：%s"
                      % "、".join(bad_tags[:5]))
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "entry": entry, "meta": meta}


# ======================== 上架（submit） ========================

def canonical_filename(runtime, pid, version, frontend=False):
    key = "server_frontend" if frontend else runtime
    return NAME_TEMPLATES[key].format(id=pid, version=version)


def submit(data_root, runtime, zip_path, form, by, frontend_zip_path=None):
    """上架：机器校验 → 元数据落库（pending）→ 包文件落 packages\\（规范名）。

    form：{name, description, changelog, icon, deps_mode, provides(仅 dep-slot，可选)}
    frontend_zip_path：server 条目必传（双包模型，同版本号成对）。
    返回 {ok, errors?, warnings?, id?, version?, frontend?}
    """
    ensure_dirs(data_root)
    runtime = str(runtime or "")
    if runtime not in RUNTIMES:
        return {"ok": False, "errors": ["runtime 必须是 %s 之一" % " / ".join(RUNTIMES)]}
    form = form or {}
    report = {"ok": False, "errors": [], "warnings": []}

    # ---- 机器校验 ----
    version = ""
    pid = ""
    requires = {}
    requires_restart = False
    min_app_version = ""
    api_version = 1
    frontend_fields = {}
    if runtime == "local":
        r = check_local_plugin_zip(zip_path)
        report["errors"] += r["errors"]
        report["warnings"] += r["warnings"]
        if r["ok"]:
            m = r["manifest"]
            pid = str(m.get("id"))
            version = str(m.get("version"))
            requires = m.get("requires") or {}
            requires_restart = False          # 客户端 hybrid 升级走秒级重启，无需服务重启
            api_version = 1
    elif runtime == "server":
        r = check_server_backend_zip(zip_path)
        report["errors"] += r["errors"]
        report["warnings"] += r["warnings"]
        if not frontend_zip_path:
            report["errors"].append("server 条目须成对上传前端包（双包模型，需求⑧）")
            r2 = {"ok": False}
        else:
            r2 = check_frontend_zip(frontend_zip_path, expect_id="", expect_version="")
            report["warnings"] += r2["warnings"]
        if r["ok"] and r2.get("ok"):
            meta = r["meta"]
            pid = str(meta.get("id"))
            version = str(meta.get("version"))
            requires = meta.get("requires") or {}
            requires_restart = bool(meta.get("requires_restart"))
            min_app_version = str(meta.get("min_app_version") or "")
            api_version = int(meta.get("api_version") or 1)
            # 前端包 id/版本与后端一致性再核（上一步不知道后端 id）
            r3 = check_frontend_zip(frontend_zip_path, expect_id=pid, expect_version=version)
            if not r3["ok"]:
                report["errors"] += r3["errors"]
            else:
                fm = r3["manifest"]
                fstat = os.stat(frontend_zip_path)
                frontend_fields = {
                    "frontend_file": canonical_filename("server", pid, version, frontend=True),
                    "frontend_sha256": sha256_file(frontend_zip_path),
                    "frontend_size": fstat.st_size,
                }
                frontend_fields["_frontend_tmp"] = frontend_zip_path
    else:  # dep-slot
        r = check_dep_zip(zip_path)
        report["errors"] += r["errors"]
        report["warnings"] += r["warnings"]
        if r["ok"]:
            entry = r["entry"]
            pid = str(entry.get("id"))
            version = str(entry.get("version") or "")
            requires_restart = False

    if report["errors"]:
        return report
    if not pid or not version:
        report["errors"].append("无法从包中确定 id / 版本号")
        return report

    # ---- 版本占号（§3.7：同 id 同版本号只要存在记录即拒绝重提） ----
    catalog = load_catalog(data_root)
    entry = find_entry(catalog, pid)
    if entry is None:
        entry = {"id": pid, "versions": []}
        catalog["packages"].append(entry)
    if find_version_record(entry, version) is not None:
        report["errors"].append("版本占号：%s v%s 已存在记录（含已驳回/已下架）——问题包请升版本号重提"
                                % (pid, version))
        return report

    # ---- 包文件落 packages\\（规范名；前端包随后） ----
    fname = canonical_filename(runtime, pid, version)
    dest = os.path.join(packages_dir(data_root), fname)
    shutil.copyfile(zip_path, dest)
    if frontend_fields.get("_frontend_tmp"):
        fdest = os.path.join(packages_dir(data_root), frontend_fields["frontend_file"])
        shutil.copyfile(frontend_fields.pop("_frontend_tmp"), fdest)

    record = {
        "version": version,
        "file": fname,
        "sha256": sha256_file(dest),
        "size": os.path.getsize(dest),
        "api_version": api_version,
        "min_app_version": min_app_version,
        "requires_restart": requires_restart,
        "changelog": str(form.get("changelog") or ""),
        "status": "pending",
        "submit": {"by": by, "at": now_iso(),
                   "machine_report": {"ok": True,
                                      "checks": "机器校验通过（%s）" % runtime
                                      + ("；警告 %d 项" % len(report["warnings"]) if report["warnings"] else "")}},
        "review": {},
        "published_at": "",
    }
    record.update(frontend_fields)
    _append_history(record, "submit", by,
                    "机器校验通过，等待审核（%s 条警告）" % len(report["warnings"]))
    # 上架声明字段（需求④）：runtime / 依赖版本（自动提取，表单可覆盖）/ deps_mode / provides
    entry.setdefault("versions", []).append(record)
    for key in ("name", "description", "icon"):
        if form.get(key):
            entry[key] = str(form[key])
    entry["runtime"] = runtime
    entry["deps_mode"] = str(form.get("deps_mode") or "shared")
    if form.get("provides"):
        entry["provides"] = [str(x).strip() for x in str(form["provides"]).replace("，", ",").split(",")
                             if str(x).strip()]
    elif runtime == "dep-slot":
        try:
            with zipfile.ZipFile(dest) as zf:
                inner = _zip_json(dest, "manifest.json") or {}
            cs = inner.get("components")
            if isinstance(cs, dict):
                cs = [cs]
            e0 = (cs or [{}])[0]
            entry["provides"] = [str(x) for x in (e0.get("provides") or [])]
        except Exception:
            entry["provides"] = []
    # 依赖版本声明自动提取（python_packages 的 name+version），保留人工确认结果：
    # 表单传 requires_json 则用之，否则用包内声明
    if form.get("requires_json"):
        try:
            entry["requires"] = json.loads(form["requires_json"])
        except Exception:
            entry["requires"] = requires
    else:
        entry["requires"] = requires

    save_catalog(data_root, catalog)
    _write_index_server(data_root, catalog)
    report["ok"] = True
    report.update({"id": pid, "version": version, "file": fname,
                   "frontend": frontend_fields.get("frontend_file")})
    return report


# ======================== 审核 / 发布 / 下架 ========================

def review(data_root, pid, version, action, by, note=""):
    """审核动作：approve（发布）/ reject / republish（已下架重新上架）。

    server 条目发布时的自动部署由 HTTP 层在 approve 前执行（需要 base_dir）：
    部署失败则保持 pending 并把失败写入 review/history（设计文档：部署失败=发布失败回退）。
    """
    catalog = load_catalog(data_root)
    entry = find_entry(catalog, pid)
    if entry is None:
        return {"ok": False, "error": "条目不存在：%s" % pid}
    record = find_version_record(entry, version)
    if record is None:
        return {"ok": False, "error": "版本不存在：%s v%s" % (pid, version)}
    if action == "approve":
        if record.get("status") != "pending":
            return {"ok": False, "error": "只有待审核版本可发布（当前 %s）" % record.get("status")}
        record["status"] = "published"
        record["review"] = {"reviewer": by, "at": now_iso(), "note": str(note or "")}
        record["published_at"] = now_iso()
        _append_history(record, "approve", by, note)
    elif action == "reject":
        if record.get("status") not in ("pending", "delisted"):
            return {"ok": False, "error": "当前状态不可驳回（%s）" % record.get("status")}
        record["status"] = "rejected"
        record["review"] = {"reviewer": by, "at": now_iso(), "note": str(note or "")}
        _append_history(record, "reject", by, note)
    elif action == "republish":
        if record.get("status") != "delisted":
            return {"ok": False, "error": "只有已下架版本可重新上架（当前 %s）" % record.get("status")}
        record["status"] = "pending"
        _append_history(record, "republish", by, note or "重新上架，再走审核")
    else:
        return {"ok": False, "error": "未知审核动作：%r" % action}
    save_catalog(data_root, catalog)
    _write_index_server(data_root, catalog)
    return {"ok": True, "id": pid, "version": version, "status": record["status"]}


def delist(data_root, pid, version, by, note=""):
    """下架：按版本置 delisted；回退语义 = 下架最新版后投影自动回落最近已上架旧版。"""
    catalog = load_catalog(data_root)
    entry = find_entry(catalog, pid)
    if entry is None:
        return {"ok": False, "error": "条目不存在：%s" % pid}
    target = version
    if not target:
        pub = latest_published(entry)
        if pub is None:
            return {"ok": False, "error": "该条目没有已上架版本"}
        target = pub.get("version")
    record = find_version_record(entry, target)
    if record is None:
        return {"ok": False, "error": "版本不存在：%s v%s" % (pid, target)}
    if record.get("status") != "published":
        return {"ok": False, "error": "只有已上架版本可下架（当前 %s）" % record.get("status")}
    record["status"] = "delisted"
    _append_history(record, "delist", by, note)
    save_catalog(data_root, catalog)
    _write_index_server(data_root, catalog)
    return {"ok": True, "id": pid, "version": target, "status": "delisted"}


def meta_edit(data_root, pid, form, by):
    """元数据编辑（§3.7）：name/description/icon 可改，记 history（挂最新版本记录）。"""
    catalog = load_catalog(data_root)
    entry = find_entry(catalog, pid)
    if entry is None:
        return {"ok": False, "error": "条目不存在：%s" % pid}
    changed = []
    for key in ("name", "description", "icon"):
        if form.get(key) is not None and str(form[key]) != str(entry.get(key, "")):
            entry[key] = str(form[key])
            changed.append(key)
    if not changed:
        return {"ok": True, "changed": []}
    versions = entry.get("versions") or []
    if versions:
        _append_history(versions[-1], "meta_edit", by, "条目元数据更新：%s" % "、".join(changed))
    save_catalog(data_root, catalog)
    return {"ok": True, "changed": changed}


def mark_deploy_result(data_root, pid, version, ok, detail, by):
    """server 条目发布时的部署结果回写（approve 后调用）：失败=发布回退待审核。"""
    catalog = load_catalog(data_root)
    entry = find_entry(catalog, pid)
    record = find_version_record(entry, version) if entry else None
    if record is None:
        return {"ok": False, "error": "版本不存在：%s v%s" % (pid, version)}
    if ok:
        _append_history(record, "deploy", by, "后端包部署成功：%s" % detail)
    else:
        record["status"] = "pending"
        record["published_at"] = ""
        _append_history(record, "deploy_failed", by, "后端包部署失败，发布回退待审核：%s" % detail)
    save_catalog(data_root, catalog)
    _write_index_server(data_root, catalog)
    return {"ok": True, "status": record["status"]}


def deploy_backend_package(base_dir, data_root, pid, version):
    """server 后端包发布即部署（当前形态：进程内——复用 admin inspect/apply 全链）。

    校验 → 备份 → 替换 → 登记（可回滚）；失败返回 {ok:False, error}，由调用方回退发布状态。
    E 架构（supervisor）落地后此函数换为控制接口编排，契约不变。
    """
    path = os.path.join(packages_dir(data_root),
                        canonical_filename("server", pid, version))
    if not os.path.isfile(path):
        return {"ok": False, "error": "包文件缺失：%s" % path}
    inspected = plugin_admin.inspect_package(path, base_dir, data_root, force=True)
    if not inspected.get("ok"):
        return {"ok": False, "error": "；".join(inspected.get("errors") or ["校验失败"])}
    report = plugin_admin.apply_package(base_dir, data_root, inspected)
    if not report.get("ok"):
        return {"ok": False, "error": report.get("error", "部署失败")}
    return {"ok": True, "report": report}


# ======================== E1：index-server.json 投影 ========================

def _write_index_server(data_root, catalog):
    """E1（§3.7）：仅 server 条目的 schema-1 flat 投影——admin read_index/batch_apply 零改造消费。"""
    pkgs = []
    for entry in catalog.get("packages") or []:
        if entry.get("runtime") != "server":
            continue
        ver = latest_published(entry)
        if ver is None:
            continue
        pkgs.append({
            "id": entry.get("id"), "version": ver.get("version"),
            "file": ver.get("file", ""), "sha256": ver.get("sha256", ""),
            "size": ver.get("size") or 0,
            "requires_restart": bool(ver.get("requires_restart")),
            "min_app_version": str(ver.get("min_app_version") or ""),
            "changelog": str(ver.get("changelog") or ""),
        })
    write_json(index_server_path(data_root),
               {"schema": 1, "updated_at": now_iso(), "packages": pkgs})


# ======================== 统计（读 stats.jsonl，只读） ========================

def read_stats(data_root):
    rows = []
    try:
        with open(stats_path(data_root), "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except Exception:
        pass
    return rows


def stats_summary(data_root):
    """聚合：按条目 / 版本 / 日 / 用户 下载量（口径=仅下载事件，管理页注明下载≠安装）。"""
    rows = read_stats(data_root)
    by_id, by_version, by_day, by_user, by_via = {}, {}, {}, {}, {}
    for r in rows:
        pid = str(r.get("id") or "?")
        by_id[pid] = by_id.get(pid, 0) + 1
        key = "%s@%s" % (pid, r.get("version") or "?")
        by_version[key] = by_version.get(key, 0) + 1
        day = str(r.get("ts") or "")[:10]
        if day:
            by_day[day] = by_day.get(day, 0) + 1
        user = str(r.get("user") or "（匿名）")
        by_user[user] = by_user.get(user, 0) + 1
        via = str(r.get("via") or "browser")
        by_via[via] = by_via.get(via, 0) + 1
    return {"total": len(rows),
            "by_package": dict(sorted(by_id.items(), key=lambda kv: -kv[1])),
            "by_version": dict(sorted(by_version.items(), key=lambda kv: -kv[1])),
            "by_day": dict(sorted(by_day.items())),
            "by_user": dict(sorted(by_user.items(), key=lambda kv: -kv[1])),
            "by_via": by_via}
