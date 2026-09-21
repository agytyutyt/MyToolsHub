"""管理后台 · 插件管理（阶段二/三）：插件包的上传校验 / 应用 / 回滚 / 批量升级。

与目标机离线安装器 ``tools/plugin-upgrade/install-plugin.ps1`` **共用同一套规则**
（规则单点定义见 ``docs/design/插件独立升级方案-设计文档.md`` §4 / §5.3，两处实现必须逐条一致）：

    只读校验（包结构 → schema/kind/id → 逐文件 SHA256 → 版本规则 → min_app_version）
      → 备份旧版  → 三分法替换代码  → 登记状态（含 restart_pending）
    任何拒绝都发生在第一次写操作之前；数据根 plugins/<id>/ 全程只读。

本模块只做纯逻辑（所有路径由调用方显式传入），便于单元测试；
HTTP 层在 ``routes.py`` 里，鉴权与审计（set_operation）也在那一层。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import zipfile

try:                                  # 框架模块：状态登记 / 模板同步 / 待重启标记
    import jztools_data
except Exception:                     # 单元测试环境可能未在 sys.path 上
    jztools_data = None

try:                                  # 框架模块：依赖判定引擎（三态结论，见设计文档 §5.3）
    import jz_deps
except Exception:                     # 单元测试环境可能未在 sys.path 上
    jz_deps = None

PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PLUGIN_PACKAGE_KIND = "plugin-upgrade"
PACKAGE_SCHEMA = 1

MAX_ZIP_BYTES = 300 * 1024 * 1024          # PU-3：包体积上限
MAX_EXTRACTED_BYTES = 600 * 1024 * 1024    # PU-3：解压后上限
MAX_ENTRIES = 5000                         # PU-3：条目数上限

BACKUP_KEEP = 3                            # 与安装器一致：默认保留最近 3 份


# ======================== 小工具 ========================

def now_stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hash16(path):
    """16 位内容指纹（MD5 前 16 位；与 jztools_data._file_hash16 / 安装器同语义）。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def semver_cmp(a, b):
    """比较 主.次.修订；任一侧不合法返回 None。"""
    def parts(v):
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(v or ""))
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    pa, pb = parts(a), parts(b)
    if pa is None or pb is None:
        return None
    return (pa > pb) - (pa < pb)


# ======================== 路径 ========================

def plugin_dir(base_dir, plugin_id):
    return os.path.join(base_dir, "plugins", plugin_id)


def plugin_manifest(base_dir, plugin_id):
    return os.path.join(plugin_dir(base_dir, plugin_id), "manifest.json")


def installed_version(base_dir, plugin_id):
    obj = read_json(plugin_manifest(base_dir, plugin_id))
    if isinstance(obj, dict) and obj.get("version"):
        return str(obj["version"])
    return None


def app_version(base_dir):
    obj = read_json(os.path.join(base_dir, "version.json"))
    if isinstance(obj, dict) and obj.get("app"):
        return str(obj["app"])
    return None


def backups_dir(data_root, plugin_id):
    return os.path.join(data_root, "backups", "plugins", plugin_id)


def staging_root(data_root):
    return os.path.join(data_root, ".staging")


def plugin_data_dir(data_root, plugin_id):
    return os.path.join(data_root, "plugins", plugin_id)


def state_path(data_root):
    return os.path.join(data_root, "config", ".app_state.json")


def read_state(data_root):
    obj = read_json(state_path(data_root))
    return obj if isinstance(obj, dict) else {}


def write_state(data_root, state):
    write_json(state_path(data_root), state)


def _state_entry(state, plugin_id):
    plugins = state.get("plugins")
    if isinstance(plugins, dict):
        entry = plugins.get(plugin_id)
        if isinstance(entry, dict):
            return entry
    return None


def _set_state_entry(data_root, plugin_id, entry):
    state = read_state(data_root)
    plugins = state.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        state["plugins"] = plugins
    plugins[plugin_id] = entry
    write_state(data_root, state)


# ======================== 体积 / 目录 ========================

def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _rm(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ======================== zip：安全校验 / 解压 / 打包 ========================

def zip_entries_safe(zf):
    """逐条校验条目名（PU-2）：拒绝绝对路径 / 盘符 / 上跳 / 含冒号。"""
    for info in zf.infolist():
        name = (info.filename or "").replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ":" in name:
            return False, name
        if any(part == ".." for part in name.split("/")):
            return False, name
    return True, ""


def extract_zip_safe(zip_path, dest_dir, prefix_filter=None):
    """安全解压（逐条、限条目数、限总量）；返回 (文件数, 解压字节数, 相对路径列表)。"""
    os.makedirs(dest_dir, exist_ok=True)
    count = 0
    total = 0
    rels = []
    with zipfile.ZipFile(zip_path) as zf:
        ok, bad = zip_entries_safe(zf)
        if not ok:
            raise ValueError("包内含不安全路径：%s" % bad)
        if len(zf.infolist()) > MAX_ENTRIES:
            raise ValueError("包内条目数超过上限 %d" % MAX_ENTRIES)
        for info in zf.infolist():
            name = (info.filename or "").replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            if prefix_filter and not name.startswith(prefix_filter):
                continue
            total += int(info.file_size or 0)
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("解压后体积超过上限 %d 字节" % MAX_EXTRACTED_BYTES)
            target = os.path.join(dest_dir, *name.split("/"))
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            count += 1
            rels.append(name)
    return count, total, rels


def zip_dir_with_prefix(src_dir, zip_path, prefix):
    """把目录打成 zip，条目统一带 prefix（如 plugins/<id>/）——与安装器同格式。"""
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, src_dir).replace(os.sep, "/")
                zf.write(full, prefix.rstrip("/") + "/" + rel)
    return zip_path


def zip_entry_list(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        return [i.filename.replace("\\", "/") for i in zf.infolist() if not i.filename.endswith("/")]


# ======================== 包校验（inspect） ========================

def _parse_sums(path):
    """解析 SHA256SUMS → [(rel, sha256)]；格式错误抛 ValueError。"""
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if len(line) < 66 or line[64:66] != "  ":
                raise ValueError("SHA256SUMS 行格式不合法：%s" % line[:80])
            digest = line[:64].lower()
            rel = line[66:].replace("\\", "/")
            if not re.match(r"^[0-9a-f]{64}$", digest):
                raise ValueError("SHA256SUMS 摘要不是 64 位十六进制：%s" % line[:80])
            items.append((rel, digest))
    return items


def _dep_check_for_package(meta, staging, base_dir, pid):
    """按包内 `requires` 评估本机依赖满足情况（供上传计划弹窗；不阻断添加）。"""
    if jz_deps is None or not pid:
        return None
    try:
        req = (meta or {}).get("requires")
        manifest = {"requires": req} if isinstance(req, dict) else {}
        pdir = os.path.join(staging or "", "payload", "plugins", pid)
        if not manifest:
            manifest = read_json(os.path.join(pdir, "manifest.json")) or {}
        vj = jz_deps.read_version_json(base_dir)
        v = jz_deps.evaluate(pid, pdir, manifest, app_dir=base_dir,
                             app_version=vj.get("app"), plugin_api=vj.get("plugin_api"))
        return {"status": v["status"], "missing": v["missing"], "degraded": v.get("degraded", []),
                "reason": v.get("reason", ""), "detail": v.get("detail", []),
                "lock_available": v.get("lock_available", False)}
    except Exception:
        return None


def inspect_package(zip_path, base_dir, data_root, force=False, staging=None):
    """只读校验插件包（不写程序目录）；返回 dict：

    {ok, errors[], warnings[], meta, files[(rel,sha)], plan{new,changed,deleted,unknown},
     restart_needed, restart_reason, staging, from_version, to_version}
    校验失败的 staging 会被清理（keep_staging=True 时保留，便于排查）。
    """
    result = {
        "ok": False, "errors": [], "warnings": [], "meta": None, "files": [],
        "plan": {"new": [], "changed": [], "deleted": [], "unknown": []},
        "restart_needed": False, "restart_reason": "", "staging": None,
        "from_version": None, "to_version": None,
    }
    try:
        size = os.path.getsize(zip_path)
    except OSError as e:
        result["errors"].append("包文件不可读：%s" % e)
        return result
    if size > MAX_ZIP_BYTES:
        result["errors"].append("包体积 %.1f MB 超过上限 %.0f MB" % (size / 1048576, MAX_ZIP_BYTES / 1048576))
        return result

    if staging is None:
        staging = os.path.join(staging_root(data_root), "upload-%s" % now_stamp())
    _rm(staging)
    result["staging"] = staging
    try:
        count, total, _rels = extract_zip_safe(zip_path, staging)
    except Exception as e:
        result["errors"].append("解压失败：%s" % e)
        _rm(staging)
        return result

    meta = read_json(os.path.join(staging, "plugin-package.json"))
    if not isinstance(meta, dict):
        result["errors"].append("包内缺少或无法解析 plugin-package.json")
        _rm(staging)
        return result
    result["meta"] = meta
    if meta.get("schema") != PACKAGE_SCHEMA:
        result["errors"].append("包清单 schema 不受支持：%r（本版本支持 1）" % meta.get("schema"))
    if meta.get("kind") != PLUGIN_PACKAGE_KIND:
        result["errors"].append("包类型不是 %s：%r" % (PLUGIN_PACKAGE_KIND, meta.get("kind")))
    pid = str(meta.get("id") or "")
    version = str(meta.get("version") or "")
    result["to_version"] = version or None
    if not PLUGIN_ID_RE.match(pid):
        result["errors"].append("包清单 id 非法：%r" % pid)
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        result["errors"].append("包清单版本号不是 主.次.修订：%r" % version)

    # 依赖检测（设计文档 §5.3 D-8：**提示但不阻断**）——包内声明 vs 本机现状，
    # 结果放进计划供前端弹窗（缺失只提示，apply 照常执行：环境问题允许"先装插件、后补依赖"）。
    result["dep_check"] = _dep_check_for_package(meta, staging, base_dir, pid)

    payload_plugin = os.path.join(staging, "payload", "plugins", pid) if pid else ""
    manifest = read_json(os.path.join(payload_plugin, "manifest.json")) if payload_plugin else None
    if not isinstance(manifest, dict):
        result["errors"].append("包结构不符：缺少 payload/plugins/%s/manifest.json" % pid)
    else:
        if str(manifest.get("id")) != pid:
            result["errors"].append("id 不一致：包清单 %s，manifest.json %s" % (pid, manifest.get("id")))
        if str(manifest.get("version")) != version:
            result["errors"].append("版本不一致：包清单 %s，manifest.json %s" % (version, manifest.get("version")))

    # 逐文件 SHA256（PU-2：全部通过才允许后续写入）
    sums_path = os.path.join(staging, "SHA256SUMS")
    if not os.path.isfile(sums_path):
        result["errors"].append("包内缺少 SHA256SUMS")
    else:
        try:
            items = _parse_sums(sums_path)
        except ValueError as e:
            result["errors"].append(str(e))
            items = []
        bad = []
        for rel, digest in items:
            if not rel.startswith("plugins/%s/" % pid):
                result["errors"].append("SHA256SUMS 含包外路径：%s" % rel)
                continue
            full = os.path.join(staging, "payload", *rel.split("/"))
            if not os.path.isfile(full):
                bad.append("%s（包内缺失）" % rel)
                continue
            actual = sha256_file(full)
            if actual != digest:
                bad.append("%s（期望 %s，实际 %s）" % (rel, digest[:12], actual[:12]))
            else:
                result["files"].append((rel, digest))
        if bad:
            result["errors"].append("文件哈希校验未通过：%s" % "；".join(bad[:6]))

    if result["errors"]:
        _rm(staging)
        result["staging"] = None
        return result

    # 版本规则（与安装器 §5.3 一致）
    from_ver = installed_version(base_dir, pid)
    result["from_version"] = from_ver
    is_fresh = from_ver is None
    if not is_fresh:
        c = semver_cmp(version, from_ver)
        if c is None:
            result["warnings"].append("版本号无法比较（包 %s，已装 %s），跳过版本规则" % (version, from_ver))
        elif c == 0 and not force:
            result["errors"].append("同版本重装：包与已装版本都是 %s（确需重装请勾选「强制」）" % from_ver)
        elif c < 0 and not force:
            result["errors"].append("拒绝降级：包版本 %s 低于已装版本 %s（确需降级请勾选「强制」）" % (version, from_ver))
        if not result["errors"]:
            lo = str(meta.get("upgrade_from_min") or "")
            hi = str(meta.get("upgrade_from_max") or "")
            if lo and not force:
                c = semver_cmp(from_ver, lo)
                if c is not None and c < 0:
                    result["errors"].append("不允许跳版升级：已装 %s < 本包最低起升版本 %s" % (from_ver, lo))
            if hi and not force:
                c = semver_cmp(from_ver, hi)
                if c is not None and c > 0:
                    result["errors"].append("已装版本 %s 高于本包最高起升版本 %s（属降级路径）" % (from_ver, hi))
    else:
        entry = meta.get("tools_entry")
        if not isinstance(entry, dict) and not force:
            result["errors"].append("全新安装但包内没有 tools_entry（注册条目），装上去不会出现在工具列表中")

    min_app = str(meta.get("min_app_version") or "")
    if min_app:
        app_ver = app_version(base_dir)
        if not app_ver:
            if not force:
                result["errors"].append("无法读取主程序版本（缺 version.json.app），而本包要求 min_app_version=%s" % min_app)
        else:
            c = semver_cmp(app_ver, min_app)
            if c is None:
                result["warnings"].append("主程序版本号无法比较（%s vs %s），跳过该项校验" % (app_ver, min_app))
            elif c < 0:
                result["errors"].append("主程序版本过低：本机 %s < 本包要求的 %s（请先升级主程序）" % (app_ver, min_app))

    if result["errors"]:
        _rm(staging)
        result["staging"] = None
        return result

    # 计划（三分法）+ 重启判定（都用"状态登记的上次已装清单"作基线）
    state = read_state(data_root)
    entry = _state_entry(state, pid)
    base_files = set()
    if entry and isinstance(entry.get("installed_files"), list):
        base_files = {str(x) for x in entry["installed_files"]}
    has_baseline = bool(base_files)
    base_source = "状态登记的已装文件清单" if has_baseline else "无登记：只清理 backend/*.py 残留，其它文件一律保留"

    new_files = [rel for rel, _sha in result["files"]]
    new_set = set(new_files)
    cur_dir = plugin_dir(base_dir, pid)
    cur_files = []
    if os.path.isdir(cur_dir):
        for root, _dirs, files in os.walk(cur_dir):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, base_dir).replace(os.sep, "/")
                cur_files.append(rel)

    changed, unchanged = [], 0
    for rel in new_files:
        full = os.path.join(cur_dir, *rel.split("/")[2:]) if cur_dir else None
        if cur_dir and os.path.isfile(full):
            if hash16(full) == hash16(os.path.join(staging, "payload", *rel.split("/"))):
                unchanged += 1
            else:
                changed.append(rel)
        else:
            result["plan"]["new"].append(rel)
    result["plan"]["changed"] = changed
    result["plan"]["unchanged"] = unchanged
    stale_prefix = "plugins/%s/backend/" % pid
    for rel in cur_files:
        if rel in new_set:
            continue
        if has_baseline:
            if rel in base_files:
                result["plan"]["deleted"].append(rel)
            else:
                result["plan"]["unknown"].append(rel)
        else:
            # 无基线（插件由整包安装、从未经插件包升级）：只清理 backend/*.py 残留
            # （旧模块被 importlib 扫到会出怪问题），其余一律保留——绝不删用户放进来的文件
            if rel.startswith(stale_prefix) and rel.endswith(".py"):
                result["plan"]["deleted"].append(rel)
            else:
                result["plan"]["unknown"].append(rel)

    restart = bool(meta.get("requires_restart"))
    reason = "包声明" if restart else ""
    if not restart:
        # 只有后端代码（backend/**）需要重启才生效：routes.py 等在启动时经 importlib 加载一次。
        # manifest.json 每次请求都会重读（app.load_manifests），前端资源也是磁盘直读 —— 都不需要重启。
        def _code_rel(rel):
            return re.match(r"^plugins/[^/]+/backend/", rel) is not None
        for rel in new_files:
            if not _code_rel(rel):
                continue
            full = os.path.join(cur_dir, *rel.split("/")[2:])
            src = os.path.join(staging, "payload", *rel.split("/"))
            if not os.path.isfile(full):
                restart, reason = True, "新增后端文件 %s" % rel
                break
            if hash16(full) != hash16(src):
                restart, reason = True, "后端文件有变化 %s" % rel
                break
        if not restart:
            for rel in result["plan"]["deleted"]:
                if _code_rel(rel):
                    restart, reason = True, "将删除后端文件 %s" % rel
                    break
    result["restart_needed"] = restart
    result["restart_reason"] = reason
    result["plan"]["base_source"] = base_source
    result["ok"] = True
    return result


# ======================== 应用（apply） ========================

def apply_package(base_dir, data_root, inspected, purge_unknown=False, update_entry=False,
                  tools_cfg_path=None):
    """应用一个已通过校验的包（inspected 为 inspect_package 的返回值）。"""
    if not inspected or not inspected.get("ok"):
        return {"ok": False, "error": "包未通过校验，拒绝应用"}
    meta = inspected["meta"]
    pid = str(meta["id"])
    version = str(meta["version"])
    staging = inspected["staging"]
    if not staging or not os.path.isdir(staging):
        return {"ok": False, "error": "上传暂存目录已失效，请重新上传"}

    payload_plugin = os.path.join(staging, "payload", "plugins", pid)
    cur_dir = plugin_dir(base_dir, pid)
    from_ver = inspected.get("from_version")

    # ① 备份（底线三：没有备份不替换）
    backup_path = None
    bdir = backups_dir(data_root, pid)
    os.makedirs(bdir, exist_ok=True)
    if from_ver:
        backup_path = os.path.join(bdir, "%s-%s-%s.zip" % (pid, from_ver, now_stamp()))
        try:
            zip_dir_with_prefix(cur_dir, backup_path, "plugins/%s" % pid)
        except Exception as e:
            _rm(backup_path)
            return {"ok": False, "error": "备份失败，已中止（未做任何替换）：%s" % e}
        if not os.path.isfile(backup_path):
            return {"ok": False, "error": "备份失败（未生成 %s），已中止" % backup_path}

    # ② 三分法替换
    deleted, unknown = inspected["plan"]["deleted"], inspected["plan"]["unknown"]
    written = 0
    try:
        for rel in deleted:
            _rm(os.path.join(base_dir, *rel.split("/")))
        if unknown and purge_unknown:
            for rel in unknown:
                _rm(os.path.join(base_dir, *rel.split("/")))
        for rel in [r for r, _ in inspected["files"]]:
            src = os.path.join(staging, "payload", *rel.split("/"))
            dst = os.path.join(base_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            written += 1
        # 清理因此空掉的目录（仅限插件目录内）
        if os.path.isdir(cur_dir):
            for root, dirs, files in os.walk(cur_dir, topdown=False):
                for d in dirs:
                    full = os.path.join(root, d)
                    try:
                        if not os.listdir(full):
                            os.rmdir(full)
                    except OSError:
                        pass
    except Exception as e:
        return {"ok": False, "error": "替换代码时出错（可从备份回滚）：%s" % e,
                "backup": backup_path}

    # ③ 注册条目（仅全新安装；-UpdateEntry 时更新文案）
    entry_result = None
    tools_entry = meta.get("tools_entry")
    if isinstance(tools_entry, dict):
        entry_result = merge_tools_entry(data_root, tools_entry, update=update_entry, tools_cfg_path=tools_cfg_path)

    # ④ 状态登记
    code_files = [r for r, _ in inspected["files"]]
    state_entry = {
        "version": version,
        "installed_at": now_iso(),
        "installed_by": "admin-web",
        "code_sha256": meta.get("code_sha256"),
        "backup": backup_path,
        "templates": {},
        "data_dir": "plugins/%s" % pid,
        "installed_files": sorted(code_files),
    }
    _set_state_entry(data_root, pid, state_entry)

    # ⑤ 待重启标记（后端代码换了但进程还是旧的）
    if inspected.get("restart_needed"):
        if jztools_data is not None:
            try:
                jztools_data.mark_plugin_restart_pending(pid, inspected.get("restart_reason", ""), root=data_root)
            except TypeError:      # 兼容旧签名
                try:
                    jztools_data.mark_plugin_restart_pending(pid, inspected.get("restart_reason", ""))
                except Exception:
                    pass
            except Exception:
                pass

    # ⑥ 配置模板就地补键（不等重启；同一份 sync_plugin_templates 实现）
    if jztools_data is not None:
        try:
            jztools_data.sync_plugin_templates(base_dir, data_root)
        except Exception:
            pass

    # ⑦ 备份轮转 + 清理暂存
    prune_backups(data_root, pid)
    _rm(staging)

    return {
        "ok": True, "id": pid, "from_version": from_ver, "to_version": version,
        "written": written, "deleted": len(deleted),
        "kept_unknown": 0 if purge_unknown else len(unknown),
        "backup": backup_path, "restart_needed": bool(inspected.get("restart_needed")),
        "restart_reason": inspected.get("restart_reason", ""),
        "tools_entry": entry_result,
    }


def prune_backups(data_root, pid, keep=BACKUP_KEEP):
    bdir = backups_dir(data_root, pid)
    if not os.path.isdir(bdir):
        return 0
    items = []
    for name in os.listdir(bdir):
        if name.lower().endswith(".zip"):
            full = os.path.join(bdir, name)
            items.append((os.path.getmtime(full), full))
    items.sort(reverse=True)
    dropped = 0
    for _mtime, full in items[keep:]:
        _rm(full)
        dropped += 1
    return dropped


def merge_tools_entry(data_root, entry, update=False, tools_cfg_path=None):
    """把注册条目合并进数据根 config/tools.json（保留用户启停/排序）。

    返回 "added" / "updated" / "skip-exists" / "no-tools-json"。
    """
    path = tools_cfg_path or os.path.join(data_root, "config", "tools.json")
    cfg = read_json(path)
    if not isinstance(cfg, dict):
        return "no-tools-json"
    tools = cfg.get("tools")
    if not isinstance(tools, list):
        tools = []
    idx = None
    for i, item in enumerate(tools):
        if isinstance(item, dict) and item.get("id") == entry.get("id"):
            idx = i
            break
    if idx is not None:
        if not update:
            return "skip-exists"
        for key in ("name", "description", "category"):
            if key in entry:
                tools[idx][key] = entry[key]
        cfg["tools"] = tools
        write_json(path, cfg)
        return "updated"
    tools.append(entry)
    cfg["tools"] = tools
    write_json(path, cfg)
    return "added"


# ======================== 回滚 / 启停 ========================

def list_backups(data_root, pid):
    bdir = backups_dir(data_root, pid)
    if not os.path.isdir(bdir):
        return []
    out = []
    for name in os.listdir(bdir):
        if name.lower().endswith(".zip"):
            full = os.path.join(bdir, name)
            out.append({"file": full, "name": name,
                        "size": os.path.getsize(full), "mtime": os.path.getmtime(full)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def rollback_plugin(base_dir, data_root, pid, backup_file=None):
    """回滚到指定（缺省=最近）备份：整目录替换语义，回滚前先备份当前版本。"""
    if not PLUGIN_ID_RE.match(pid or ""):
        return {"ok": False, "error": "插件 id 非法"}
    backups = list_backups(data_root, pid)
    if backup_file:
        if not os.path.isfile(backup_file):
            return {"ok": False, "error": "指定的备份不存在：%s" % backup_file}
        chosen = backup_file
    else:
        if not backups:
            return {"ok": False, "error": "该插件没有任何备份"}
        chosen = backups[0]["file"]

    try:
        entries = zip_entry_list(chosen)
    except Exception as e:
        return {"ok": False, "error": "备份包不可读：%s" % e}
    if "plugins/%s/manifest.json" % pid not in entries:
        return {"ok": False, "error": "备份结构不符（缺少 plugins/%s/manifest.json）" % pid}

    cur_dir = plugin_dir(base_dir, pid)
    restore_ver = installed_version(base_dir, pid) or "unknown"

    # 回滚动作本身也要可回退：先把当前版本备份一份
    pre_backup = None
    if os.path.isdir(cur_dir):
        bdir = backups_dir(data_root, pid)
        os.makedirs(bdir, exist_ok=True)
        pre_backup = os.path.join(bdir, "%s-%s-pre-rollback-%s.zip" % (pid, restore_ver, now_stamp()))
        try:
            zip_dir_with_prefix(cur_dir, pre_backup, "plugins/%s" % pid)
        except Exception as e:
            return {"ok": False, "error": "回滚前的当前版本备份失败，已中止：%s" % e}

    tmp = os.path.join(staging_root(data_root), "rollback-%s-%s" % (pid, now_stamp()))
    try:
        extract_zip_safe(chosen, tmp, prefix_filter="plugins/%s/" % pid)
    except Exception as e:
        _rm(tmp)
        return {"ok": False, "error": "备份解压失败：%s" % e}
    src_plugin = os.path.join(tmp, "plugins", pid)
    if not os.path.isdir(src_plugin):
        _rm(tmp)
        return {"ok": False, "error": "备份结构不符（解压后缺少 plugins/%s/）" % pid}

    # 当前目录独有、备份里没有的文件会被丢弃 —— 先列出来，避免静默丢失
    new_files = set(zip_entry_list(chosen))
    dropped = []
    if os.path.isdir(cur_dir):
        for root, _dirs, files in os.walk(cur_dir):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), base_dir).replace(os.sep, "/")
                if rel not in new_files:
                    dropped.append(rel)

    try:
        _rm(cur_dir)
        shutil.copytree(src_plugin, cur_dir)
    except Exception as e:
        _rm(tmp)
        return {"ok": False, "error": "替换目录失败：%s" % e, "backup": chosen, "pre_backup": pre_backup}
    finally:
        _rm(tmp)

    to_ver = installed_version(base_dir, pid) or "unknown"
    entry = _state_entry(read_state(data_root), pid) or {}
    entry.update({
        "version": to_ver,
        "installed_at": now_iso(),
        "installed_by": "admin-web (rollback)",
        "backup": chosen,
        "installed_files": sorted(new_files),
    })
    _set_state_entry(data_root, pid, entry)
    if jztools_data is not None:
        try:
            jztools_data.mark_plugin_restart_pending(pid, "回滚后需重启生效", root=data_root)
        except TypeError:
            try:
                jztools_data.mark_plugin_restart_pending(pid, "回滚后需重启生效")
            except Exception:
                pass
        except Exception:
            pass
    return {"ok": True, "id": pid, "from_version": restore_ver, "to_version": to_ver,
            "backup": chosen, "pre_backup": pre_backup, "dropped": dropped}


def set_plugin_enabled(data_root, pid, enabled, tools_cfg_path=None):
    """启用/停用插件（写数据根 tools.json 的 enabled；不动代码与数据）。"""
    path = tools_cfg_path or os.path.join(data_root, "config", "tools.json")
    cfg = read_json(path)
    if not isinstance(cfg, dict):
        return False
    tools = cfg.get("tools")
    if not isinstance(tools, list):
        return False
    hit = False
    for item in tools:
        if isinstance(item, dict) and item.get("id") == pid:
            item["enabled"] = bool(enabled)
            hit = True
    if hit:
        cfg["tools"] = tools
        write_json(path, cfg)
    return hit


# ======================== 总览（list） ========================

def uninstall_plugin(base_dir, data_root, pid, purge_entry=False, backup_data=False):
    """卸载插件（规范 §11 顺序铁律：先删/停注册条目 → 备份数据 → 再删目录）。

    - **核心插件（manifest.core = true）拒绝卸载**（设计文档 §3.5）：admin 承担登录 / 鉴权 /
      权限 / 后台 / 插件管理本身，卸载即"自锁"（没有管理入口、也没有鉴权）。
    - 用户数据默认**保留**（数据根 plugins/<id>/ 全程只读）；`backup_data=True` 时先把数据目录
      只读打包到 backups/plugins/<id>/data-<时间戳>.zip（不删除原数据）。
    - 与目标机安装器 `-Uninstall` 同一顺序与同一拒绝规则（两处实现同源）。

    返回 {"ok", "steps": [...], "error"?}
    """
    if not PLUGIN_ID_RE.match(pid or ""):
        return {"ok": False, "error": "插件 id 非法"}
    manifest = read_json(plugin_manifest(base_dir, pid))     # 注意：plugin_manifest 返回**路径**
    manifest = manifest if isinstance(manifest, dict) else {}
    if manifest.get("core"):
        return {"ok": False, "error": "「%s」是核心插件（随主体分发），不可卸载。" % pid}
    pdir = plugin_dir(base_dir, pid)
    steps = []

    # ① 注册条目：先停用（默认）或删除（purge_entry）——避免目录删掉后留下悬空引用
    if purge_entry:
        path = os.path.join(data_root, "config", "tools.json")
        cfg = read_json(path)
        if isinstance(cfg, dict) and isinstance(cfg.get("tools"), list):
            kept = [t for t in cfg["tools"] if not (isinstance(t, dict) and t.get("id") == pid)]
            if len(kept) != len(cfg["tools"]):
                cfg["tools"] = kept
                write_json(path, cfg)
                steps.append("已删除注册条目（tools.json）")
            else:
                steps.append("tools.json 中没有该插件的注册条目（跳过）")
        else:
            steps.append("tools.json 不可读（跳过注册条目处理）")
    else:
        if set_plugin_enabled(data_root, pid, False):
            steps.append("已停用注册条目（enabled:false；用 -PurgeEntry 语义可删除）")
        else:
            steps.append("tools.json 中没有该插件的注册条目（跳过）")

    # ② 数据备份（只读打包，不删除数据）——放数据根 backups/，与程序目录解耦
    data_dir = plugin_data_dir(data_root, pid)
    if backup_data and os.path.isdir(data_dir) and dir_size(data_dir) > 0:
        bdir = backups_dir(data_root, pid)
        zip_path = os.path.join(bdir, "data-%s.zip" % now_stamp())
        try:
            zip_dir_with_prefix(data_dir, zip_path, "plugins/%s/" % pid)
            steps.append("已备份插件数据 → %s" % zip_path)
        except Exception as exc:
            return {"ok": False, "error": "数据备份失败，已中止卸载（不留备份不删数据）：%s" % exc,
                    "steps": steps}
    elif os.path.isdir(data_dir):
        steps.append("插件数据保留在数据根（%s），未打包（未勾选备份）" % data_dir)

    # ③ 删程序目录（插件代码）
    if os.path.isdir(pdir):
        _rm(pdir)
        steps.append("已删除插件代码目录 plugins/%s" % pid)
    else:
        steps.append("插件代码目录不存在（可能已删除）")

    # ④ 清状态登记（避免下次安装被旧登记误判）
    state = read_state(data_root)
    if _state_entry(state, pid) is not None:
        try:
            state.get("plugins", {}).pop(pid, None)
            write_state(data_root, state)
            steps.append("已清除状态登记（.app_state.json）")
        except Exception:
            steps.append("状态登记清理失败（不影响卸载）")
    return {"ok": True, "steps": steps, "data_dir": data_dir,
            "note": "用户数据默认保留；如需彻底清理，请手工删除数据根下的该目录。"}


def plugin_names(cfg):
    """从 tools.json 取 id → 展示名 映射（规范 M-2：展示名的权威来源是 config/tools.json）。"""
    names = {}
    if isinstance(cfg, dict) and isinstance(cfg.get("tools"), list):
        for item in cfg["tools"]:
            if isinstance(item, dict) and item.get("id"):
                nm = str(item.get("name") or "").strip()
                if nm:
                    names[item["id"]] = nm
    return names


def display_name(names, plugin_id, manifest=None, extra=None):
    """插件展示名取值顺序：tools.json（权威）→ 调用方兜底（如包内 tools_entry）→
    manifest.json 的 name（跨项目迁移兜底，规范 §5.1）→ id。"""
    for src in (names.get(plugin_id), (extra or {}).get("name"), (manifest or {}).get("name")):
        if src and str(src).strip():
            return str(src).strip()
    return plugin_id


def align_state_version(base_dir, data_root, pid):
    """把状态登记的版本对齐到**程序目录的实际代码版本**（登记漂移的一键修复）。

    为什么需要：install.ps1 的插件防回退比较的是**登记版本**（`config/.app_state.json`），
    所以"代码比登记新"时保护会失效——下次主包升级可能覆盖较新的插件代码（静默降级）。
    本操作只写数据根的登记（版本 + installed_by/at），**不动代码、不动用户数据**；
    判定仍以程序目录 manifest 为准（设计文档 §5.3 / R-8）。
    """
    if not PLUGIN_ID_RE.match(pid or ""):
        return {"ok": False, "error": "插件 id 非法"}
    ver = installed_version(base_dir, pid)
    if not ver:
        return {"ok": False, "error": "读不到该插件的 manifest.json（程序目录 plugins/%s）" % pid}
    state = read_state(data_root)
    entry = dict(_state_entry(state, pid) or {})
    old = str(entry.get("version") or "")
    if old == ver:
        return {"ok": True, "id": pid, "from_version": old, "to_version": ver, "changed": False}
    entry["version"] = ver
    entry["installed_by"] = "admin-web/align"
    entry["installed_at"] = now_iso()
    _set_state_entry(data_root, pid, entry)
    return {"ok": True, "id": pid, "from_version": old, "to_version": ver, "changed": True}


def align_state_versions(base_dir, data_root):
    """一键对齐：把**所有**"登记版本 ≠ 程序目录实际版本"的插件登记批量对齐。

    返回 {"ok", "aligned":[{id, from_version, to_version}], "total", "failed":[{id,error}]}。
    与单插件版同一条路径（逐个调用 align_state_version），因此行为完全一致、可审计。
    """
    plugins_root = os.path.join(base_dir, "plugins")
    aligned, failed, total = [], [], 0
    if not os.path.isdir(plugins_root):
        return {"ok": True, "aligned": [], "total": 0, "failed": []}
    for pid in sorted(os.listdir(plugins_root)):
        pdir = os.path.join(plugins_root, pid)
        if not PLUGIN_ID_RE.match(pid or "") or not os.path.isdir(pdir):
            continue
        total += 1
        res = align_state_version(base_dir, data_root, pid)
        if not res.get("ok"):
            failed.append({"id": pid, "error": res.get("error", "")})
        elif res.get("changed"):
            aligned.append({"id": pid, "from_version": res["from_version"], "to_version": res["to_version"]})
    return {"ok": True, "aligned": aligned, "total": total, "failed": failed}


def plugin_deps(base_dir, pid, pdir, manifest=None):
    """单插件的依赖判定（版本门控 + 三类依赖 → 三态），与主体启动门控同一引擎。

    供后台「插件管理」列表展示依赖需求与可运行性（设计文档 §5.3 展示面）。
    引擎不可用（测试环境）时返回 None，前端按"未判定"展示。
    """
    if jz_deps is None:
        return None
    try:
        if manifest is None:
            manifest = read_json(os.path.join(pdir, "manifest.json")) or {}
        vj = jz_deps.read_version_json(base_dir)
        verdict = jz_deps.evaluate(pid, pdir, manifest, app_dir=base_dir,
                                   app_version=vj.get("app"), plugin_api=vj.get("plugin_api"))
        return {
            "status": verdict["status"],
            "missing": verdict["missing"],
            "degraded": verdict.get("degraded", []),
            "reason": verdict.get("reason", ""),
            "lock_available": verdict.get("lock_available", False),
            "detail": verdict.get("detail", []),
        }
    except Exception:
        return None


def installed_deps(base_dir):
    """本机「已安装依赖」清单（分类 + 版本），供后台「插件管理」展示（设计文档 §5.3 D-7）。

    三类来源：① 框架包（随主包的第三方库，白名单优先）② 外部程序组件 ③ 插件自带 vendor/。
    真源是 config/installed-deps.json（构建期由 tools/gen-installed-deps.py 生成）。
    """
    raw = read_json(os.path.join(base_dir, "config", "installed-deps.json")) or {}
    lock = jz_deps.load_installed_deps(base_dir) if jz_deps else {"available": False, "packages": {}, "path": ""}
    pkgs = lock.get("packages", {})
    raw_mods = raw.get("modules") or {}
    # 白名单项写的是 import 名（PIL），先经 modules 归一到发行包名（pillow）再比对，
    # 否则白名单包会被算进"其它包"、面板上看不到它。
    whitelist = set()
    for x in (raw.get("whitelist") or []):
        whitelist.add(str(raw_mods.get(str(x), x)).lower())
    import_of = {}
    for mod, dist in raw_mods.items():
        import_of.setdefault(str(dist).lower(), []).append(str(mod))
    # 依赖组件包（cv2/numpy/openpyxl…）装在 runtime/pylibs/，由「依赖组件」单独安装：
    # 这些包要逐项列出（要看"装了哪些组件、什么版本"），不能只算进 other_count。
    comp_of = {}
    for c in (lock.get("components") or []):
        for d in (c.get("packages") or {}):
            comp_of[str(d).lower()] = str(c.get("id") or c.get("name") or "component")
        for mod, dist in (c.get("modules") or {}).items():
            import_of.setdefault(str(dist).lower(), []).append(str(mod))
    wl_rows, comp_rows, other_rows = [], [], []
    for dist, ver in sorted(pkgs.items(), key=lambda kv: kv[0].lower()):
        row = {"dist": dist, "version": ver, "imports": sorted(import_of.get(dist.lower(), []))[:3]}
        if dist.lower() in whitelist:
            wl_rows.append(row)
        elif dist.lower() in comp_of:
            row["component"] = comp_of[dist.lower()]
            comp_rows.append(row)
        else:
            other_rows.append(row)
    items = ([dict(r, source="framework") for r in wl_rows]
             + [dict(r, source="component") for r in comp_rows])
    groups = [{"id": "framework", "name": "框架包（随主包 / 依赖组件）", "available": lock.get("available", False),
               "items": items, "other_count": len(other_rows),
               "generated_at": lock.get("generated_at", ""), "python": lock.get("python", "")}]
    # 外部程序组件：探测标准位置（与 jz_deps._probe_external 同口径）
    ext_items = []
    for dep_id, label in (("libreoffice", "LibreOffice 核心"), ("chrome", "Chrome 浏览器")):
        state, actual, note = ("missing", None, "")
        if jz_deps is not None:
            state, actual, note = jz_deps._probe_external(dep_id, base_dir)
        ext_items.append({"id": dep_id, "name": label, "state": state, "path": actual or "", "note": note})
    groups.append({"id": "external", "name": "外部程序组件", "available": True, "items": ext_items})
    # 插件自带（backend/vendor/）
    vend_items = []
    plugins_root = os.path.join(base_dir, "plugins")
    if os.path.isdir(plugins_root):
        for pid in sorted(os.listdir(plugins_root)):
            vend = os.path.join(plugins_root, pid, "backend", "vendor")
            if os.path.isdir(vend):
                for name in sorted(os.listdir(vend)):
                    # 只列真正的自带依赖（目录或 .py），跳过 README 之类的说明文件
                    if name.startswith("__") or name.startswith("."):
                        continue
                    if not (os.path.isdir(os.path.join(vend, name)) or name.endswith(".py")):
                        continue
                    vend_items.append({"id": "%s/%s" % (pid, name), "name": name, "plugin": pid,
                                       "note": "插件自带（版本由插件声明）"})
    groups.append({"id": "vendored", "name": "插件自带（vendor/）", "available": True, "items": vend_items})
    return {"ok": True, "lock_path": lock.get("path", ""), "groups": groups}


def list_plugins(base_dir, data_root, tools_cfg=None):
    """列出所有插件的 展示名 / 代码版本 / 登记版本 / 待重启 / 备份 / 数据占用 / 启停状态。

    解耦后新增 `deps`（依赖判定三态 + 逐依赖明细）与 `core`（核心插件标记，不可卸载），
    供后台列表展示"依赖需求情况 + 是否可运行"（设计文档 §5.3 展示面 / §3.5）。
    """
    plugins_root = os.path.join(base_dir, "plugins")
    state = read_state(data_root)
    cfg = read_json(tools_cfg) if tools_cfg else read_json(os.path.join(data_root, "config", "tools.json"))
    names = plugin_names(cfg)
    enabled_map = {}
    hidden_map = {}
    if isinstance(cfg, dict) and isinstance(cfg.get("tools"), list):
        for item in cfg["tools"]:
            if isinstance(item, dict):
                enabled_map[item.get("id")] = bool(item.get("enabled", False))
                hidden_map[item.get("id")] = bool(item.get("hidden", False))
    rows = []
    if not os.path.isdir(plugins_root):
        return rows
    for pid in sorted(os.listdir(plugins_root)):
        pdir = os.path.join(plugins_root, pid)
        if not PLUGIN_ID_RE.match(pid or "") or not os.path.isdir(pdir):
            continue
        manifest = read_json(os.path.join(pdir, "manifest.json"))
        entry = _state_entry(state, pid) or {}
        rows.append({
            "id": pid,
            "name": display_name(names, pid, manifest),
            "icon": (manifest or {}).get("icon") or "🧩",
            "code_version": str((manifest or {}).get("version") or ""),
            "state_version": str(entry.get("version") or ""),
            "installed_at": entry.get("installed_at") or "",
            "installed_by": entry.get("installed_by") or "",
            "restart_pending": bool(entry.get("restart_pending")),
            "restart_reason": entry.get("restart_pending_note") or "",
            "registered": pid in enabled_map,
            "enabled": enabled_map.get(pid, False),
            "hidden": hidden_map.get(pid, False),
            "has_backend": os.path.isdir(os.path.join(pdir, "backend")),
            "backups": len(list_backups(data_root, pid)),
            "data_bytes": dir_size(plugin_data_dir(data_root, pid)),
            "core": bool((manifest or {}).get("core")),      # 核心插件：不可卸载（§3.5）
            "deps": plugin_deps(base_dir, pid, pdir, manifest),
        })
    return rows


# ======================== 阶段三：共享盘索引 / 检查更新 / 批量升级 ========================

def read_index(index_path, verify_hash=True):
    """读取共享盘索引（index.json）；返回 {ok, error, base_dir, packages[]}。

    每个包条目：{id, version, file, sha256, size, requires_restart, min_app_version,
               changelog, path, exists, hash_ok}
    """
    obj = read_json(index_path)
    if not isinstance(obj, dict):
        return {"ok": False, "error": "索引文件不存在或不是合法 JSON：%s" % index_path, "packages": []}
    if obj.get("schema") != 1:
        return {"ok": False, "error": "索引 schema 不受支持：%r" % obj.get("schema"), "packages": []}
    base = os.path.dirname(os.path.abspath(index_path))
    packages = []
    for raw in obj.get("packages") or []:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id") or "")
        if not PLUGIN_ID_RE.match(pid):
            continue
        item = dict(raw)
        rel = str(raw.get("file") or "")
        item["path"] = os.path.join(base, *rel.replace("/", os.sep).split(os.sep)) if rel else ""
        item["exists"] = bool(item["path"]) and os.path.isfile(item["path"])
        item["hash_ok"] = None
        if item["exists"] and verify_hash and raw.get("sha256"):
            try:
                item["hash_ok"] = (sha256_file(item["path"]).lower() == str(raw["sha256"]).lower())
            except Exception:
                item["hash_ok"] = False
        packages.append(item)
    return {"ok": True, "error": "", "base_dir": base, "packages": packages}


def check_updates(base_dir, data_root, index_path, verify_hash=True):
    """把索引与已装版本比对，返回可升级/受阻清单（每项含中文展示名与原因）。"""
    idx = read_index(index_path, verify_hash=verify_hash)
    rows = []
    if not idx["ok"]:
        return {"ok": False, "error": idx["error"], "rows": []}
    state = read_state(data_root)
    cfg = read_json(os.path.join(data_root, "config", "tools.json"))
    names = plugin_names(cfg)
    for pkg in idx["packages"]:
        pid = pkg["id"]
        cur = installed_version(base_dir, pid)
        manifest = read_json(plugin_manifest(base_dir, pid))
        row = {
            "id": pid,
            "name": display_name(names, pid, manifest, extra=pkg),
            "current": cur or "",
            "available": str(pkg.get("version") or ""),
            "requires_restart": bool(pkg.get("requires_restart")),
            "min_app_version": str(pkg.get("min_app_version") or ""),
            "changelog": str(pkg.get("changelog") or ""),
            "size": pkg.get("size") or 0,
            "file": pkg.get("file") or "",
            "package_path": pkg.get("path") or "",
            "upgrade_available": False, "blocked_reason": "",
        }
        c = semver_cmp(row["available"], cur) if cur else 1
        if cur is None:
            row["upgrade_available"] = True
            row["blocked_reason"] = "未安装（将作为全新安装）" if row["available"] else ""
        elif c is None:
            row["blocked_reason"] = "版本号无法比较"
        elif c <= 0:
            row["blocked_reason"] = "已是最新"
        else:
            row["upgrade_available"] = True
        if row["upgrade_available"]:
            if not pkg.get("exists"):
                row["upgrade_available"] = False
                row["blocked_reason"] = "索引指向的包文件不存在（%s）" % row["file"]
            elif pkg.get("hash_ok") is False:
                row["upgrade_available"] = False
                row["blocked_reason"] = "包 sha256 与索引不符（介质损坏或被篡改）"
            elif row["min_app_version"]:
                app_ver = app_version(base_dir)
                if app_ver and semver_cmp(app_ver, row["min_app_version"]) == -1:
                    row["upgrade_available"] = False
                    row["blocked_reason"] = "需先升级主程序（本机 %s < %s）" % (app_ver, row["min_app_version"])
        rows.append(row)
    rows.sort(key=lambda r: (not r["upgrade_available"], r["id"]))
    return {"ok": True, "error": "", "rows": rows, "index_dir": idx.get("base_dir", "")}


def batch_apply(base_dir, data_root, index_path, ids, verify_hash=True):
    """按索引顺序应用多个插件包；返回逐项结果（含中文展示名）+ 是否需要重启。"""
    idx = read_index(index_path, verify_hash=verify_hash)
    if not idx["ok"]:
        return {"ok": False, "error": idx["error"], "results": [], "restart_needed": False}
    by_id = {p["id"]: p for p in idx["packages"]}
    names = plugin_names(read_json(os.path.join(data_root, "config", "tools.json")))
    results = []
    restart_needed = False
    for pid in ids:
        pkg = by_id.get(pid)
        shown = display_name(names, pid, read_json(plugin_manifest(base_dir, pid)), extra=pkg or {})
        if not pkg or not pkg.get("exists"):
            results.append({"id": pid, "name": shown, "ok": False,
                            "error": "索引中找不到该插件的包（或文件不存在）"})
            continue
        inspected = inspect_package(pkg["path"], base_dir, data_root)
        if not inspected.get("ok"):
            results.append({"id": pid, "name": shown, "ok": False,
                            "error": "；".join(inspected.get("errors") or ["校验失败"])})
            continue
        report = apply_package(base_dir, data_root, inspected)
        if report.get("ok"):
            restart_needed = restart_needed or bool(report.get("restart_needed"))
        results.append(dict(report, id=pid, name=shown))
    return {
        "ok": all(r.get("ok") for r in results) if results else False,
        "results": results,
        "restart_needed": restart_needed,
        "applied": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
    }


# ==================== 一键扫描安装（插件包 / 依赖组件包） ====================
# 场景：目标机离线部署时，介质（插件包、依赖组件包）解压在某目录里，逐个双击安装很繁琐。
# 这里让后台直接扫描目录 → 列出可安装项（含版本比对与 ABI 判定）→ 一键装齐。
#   · 插件包：复用 inspect_package / apply_package（校验、备份、登记、可回滚全走同一条路）
#   · 依赖组件包：Python 侧按 install-dep-component.ps1 同口径实现
#     （按组件清理旧文件 → 覆盖解压 payload → 合并 pylibs/manifest.json → 清空目录），
#     装完由主体在下次请求时自动补注入 sys.path / DLL 目录（jz_deps.refresh_flags），**无需重启**。

PKG_PLUGIN_RE = re.compile(r"^JZToolsHub-插件-(?P<id>[A-Za-z0-9._-]+)-v(?P<ver>\d+(?:\.\d+)*)\.zip$")
PKG_COMPONENT_RE = re.compile(r"^JZToolsHub-依赖-(?P<id>[A-Za-z0-9._-]+)-v(?P<ver>\d+(?:\.\d+)*)\.zip$")
COMPONENT_REL = os.path.join("runtime", "pylibs")


def default_scan_dirs(base_dir):
    """默认扫描目录：程序目录、程序目录的上级、桌面、下载（只返回存在且可读的）。"""
    base_dir = os.path.abspath(base_dir)
    home = os.path.expanduser("~")
    cands = [base_dir, os.path.dirname(base_dir)]
    for name in ("Desktop", "桌面", "Downloads", "下载"):
        cands.append(os.path.join(home, name))
    out, seen = [], set()
    for d in cands:
        try:
            d = os.path.abspath(d)
        except Exception:
            continue
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        out.append(d)
    return out


def _zip_json(zip_path, entry):
    """读 zip 内某个 JSON（缺失/不可解析返回 None）。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            name = next((n for n in zf.namelist() if n.replace("\\", "/") == entry), None)
            if not name:
                return None
            return json.loads(zf.read(name).decode("utf-8-sig"))
    except Exception:
        return None


def _app_abi(base_dir):
    """主程序的 ABI 契约（python / app 版本），用于组件与插件包的适用性判定。"""
    vj = read_json(os.path.join(base_dir, "version.json")) or {}
    return str(vj.get("python") or ""), str(vj.get("app") or "")


def _installed_components(base_dir):
    """已装依赖组件 {id: {version, files, requires_components}}（读 runtime/pylibs/manifest.json）。"""
    man = read_json(os.path.join(base_dir, COMPONENT_REL, "manifest.json")) or {}
    out = {}
    comps = man.get("components") if isinstance(man, dict) else None
    if isinstance(comps, dict):                 # 单组件简写形态
        comps = [comps]
    for c in (comps or []):
        if isinstance(c, dict) and c.get("id"):
            out[str(c["id"])] = {"version": str(c.get("version") or ""),
                                 "files": c.get("files") or [],
                                 "requires_components": c.get("requires_components") or []}
    return out


def _verdict(cur, new, abi_ok=True, abi_reason="", force=False):
    """版本比对给出状态：install / uptodate / downgrade / abi / unknown。"""
    if not abi_ok and not force:
        return "abi", abi_reason
    if not new:
        return "unknown", "包内版本号不可读"
    if not cur:
        return "install", "本机未安装"
    c = semver_cmp(str(new), str(cur))
    if c is None:
        return "unknown", "版本号不可比较"
    if c > 0:
        return "install", "可升级 %s → %s" % (cur, new)
    if c == 0:
        return ("install" if force else "uptodate"), ("同版本重装" if force else "已是最新")
    return ("install" if force else "downgrade"), ("降级 %s → %s" % (cur, new))


def scan_packages(base_dir, data_root=None, dirs=None, max_files=600, force=False):
    """扫描目录（含一级子目录）里的插件包与依赖组件包，返回可安装清单。

    只读：不写程序目录、不解压到磁盘（只读 zip 内清单）。安装由 install_scanned() 负责。
    """
    if dirs is None:
        dirs = default_scan_dirs(base_dir)
    app_py, app_ver = _app_abi(base_dir)
    comps = _installed_components(base_dir)
    items, seen_paths, scanned = [], set(), 0
    for root_dir in dirs:
        if not root_dir or not os.path.isdir(root_dir):
            continue
        stack = [(root_dir, 0)]
        while stack:
            cur_dir, depth = stack.pop()
            try:
                entries = sorted(os.listdir(cur_dir))
            except OSError:
                continue
            for name in entries:
                full = os.path.join(cur_dir, name)
                if os.path.isdir(full):
                    if depth < 1:               # 只看一级子目录（介质常解压成一层）
                        stack.append((full, depth + 1))
                    continue
                if not name.lower().endswith(".zip") or scanned >= max_files:
                    continue
                m_pl = PKG_PLUGIN_RE.match(name)
                m_cp = PKG_COMPONENT_RE.match(name)
                if not m_pl and not m_cp:
                    continue
                if full in seen_paths:
                    continue
                seen_paths.add(full)
                scanned += 1
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if m_pl:
                    meta = _zip_json(full, "plugin-package.json") or {}
                    pid = str(meta.get("id") or m_pl.group("id"))
                    cur_ver = installed_version(base_dir, pid)
                    abi_ok, abi_reason = True, ""
                    api_req = meta.get("api_version")
                    try:
                        if api_req is not None and jz_deps and int(api_req) > int(jz_deps.PLUGIN_API):
                            abi_ok = False
                            abi_reason = "需要更新的主体（plugin_api ≤ %s）" % api_req
                    except (TypeError, ValueError):
                        pass
                    min_app = str(meta.get("min_app_version") or "")
                    if abi_ok and min_app and app_ver:
                        cmp_app = semver_cmp(app_ver, min_app)
                        if cmp_app is not None and cmp_app < 0:
                            abi_ok = False
                            abi_reason = "需要主体 ≥ %s（当前 %s）" % (min_app, app_ver)
                    new_ver = str(meta.get("version") or m_pl.group("ver"))
                    status, reason = _verdict(cur_ver, new_ver, abi_ok, abi_reason, force)
                    items.append({
                        "kind": "plugin", "file": name, "path": full, "id": pid,
                        "name": display_name(plugin_names(None), pid, meta),
                        "version": new_ver, "installed": cur_ver,
                        "status": status, "reason": reason, "size": size,
                        "requires_restart": bool(meta.get("requires_restart")),
                    })
                else:
                    meta = _zip_json(full, "dep-component.json") or {}
                    inner = _zip_json(full, "manifest.json") or {}
                    cs = inner.get("components") if isinstance(inner, dict) else None
                    if isinstance(cs, dict):
                        cs = [cs]
                    entry = (cs or [{}])[0] if cs else {}
                    cid = str(meta.get("id") or entry.get("id") or m_cp.group("id"))
                    cur_ver = (comps.get(cid) or {}).get("version") or ""
                    abi_ok, abi_reason = True, ""
                    cpy = str(meta.get("python") or entry.get("python") or "")
                    if cpy and app_py:
                        mm = lambda v: ".".join(str(v).split(".")[:2])   # noqa: E731
                        if mm(cpy) != mm(app_py):
                            abi_ok = False
                            abi_reason = "为 Python %s 构建（当前主程序 %s）" % (cpy, app_py)
                    new_ver = str(meta.get("version") or entry.get("version") or m_cp.group("ver"))
                    status, reason = _verdict(cur_ver, new_ver, abi_ok, abi_reason, force)
                    need = [r for r in (meta.get("requires_components")
                                        or entry.get("requires_components") or []) if r not in comps]
                    items.append({
                        "kind": "component", "file": name, "path": full, "id": cid,
                        "name": str(meta.get("name") or entry.get("name") or cid),
                        "version": new_ver, "installed": cur_ver,
                        "status": status, "reason": reason, "size": size,
                        "requires_components": need,
                    })
    order = {"install": 0, "abi": 1, "downgrade": 2, "unknown": 3, "uptodate": 4}
    items.sort(key=lambda x: (order.get(x["status"], 9), x["kind"], x["id"]))
    return {"dirs": list(dirs), "items": items, "scanned": scanned}


def install_component_package(base_dir, zip_path, force=False):
    """安装依赖组件包（Python 侧，与 install-dep-component.ps1 同口径；无需重启即生效）。"""
    meta = _zip_json(zip_path, "dep-component.json") or {}
    inner = _zip_json(zip_path, "manifest.json") or {}
    cs = inner.get("components") if isinstance(inner, dict) else None
    if isinstance(cs, dict):
        cs = [cs]
    entry = (cs or [None])[0]
    if not entry or not entry.get("id"):
        return {"ok": False, "error": "包内缺少组件清单（dep-component.json / manifest.json）"}
    cid = str(entry["id"])
    app_py, _app_ver = _app_abi(base_dir)
    cpy = str(meta.get("python") or entry.get("python") or "")
    if cpy and app_py:
        mm = lambda v: ".".join(str(v).split(".")[:2])                   # noqa: E731
        if mm(cpy) != mm(app_py) and not force:
            return {"ok": False, "error": "本组件是为 Python %s 构建的，当前主程序内置 Python %s"
                                          "——请取匹配的组件包" % (cpy, app_py)}
    pylibs = os.path.join(base_dir, COMPONENT_REL)
    os.makedirs(pylibs, exist_ok=True)
    m_path = os.path.join(pylibs, "manifest.json")
    # ★ 旧登记必须在解压**之前**读：载荷里自带 manifest.json（本组件的单组件清单），
    #   解压会把它覆盖掉——解压后再读就只能看到本组件，"合并"变成"只剩最后一个组件"。
    installed_before = _installed_components(base_dir)
    old_components = []
    if os.path.isfile(m_path):
        old_components = [c for c in ((read_json(m_path) or {}).get("components") or [])
                          if isinstance(c, dict)]
    # ① 按组件清理上一版文件（旧清单没有 files 时退化为按 provides 顶层名清理）
    prev = installed_before.get(cid) or {}
    prev_files = list(prev.get("files") or [])
    if not prev_files:
        for pv in (entry.get("provides") or []):
            prev_files += [pv, "%s.libs" % pv]
    removed = 0
    for rel in prev_files:
        rel = str(rel).lstrip("\\/")
        if not rel or rel == "manifest.json":
            continue
        abs_p = os.path.join(pylibs, rel)
        if os.path.exists(abs_p):
            _rm(abs_p)
            removed += 1
    # ② 覆盖式解压本组件载荷（只取 payload/pylibs/**）
    written = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            ok_names, bad = zip_entries_safe(zf)      # PU-2：条目名安全校验（不是条目列表）
            if not ok_names:
                return {"ok": False, "error": "包内条目名不安全：%s" % bad}
            for info in zf.infolist():
                if info.filename.endswith("/"):
                    continue
                rel = info.filename.replace("\\", "/")
                if not rel.startswith("payload/pylibs/"):
                    continue
                tail = rel[len("payload/pylibs/"):]
                if not tail or tail.endswith("/") or tail.replace("\\", "/") == "manifest.json":
                    continue                     # 清单由下面的合并步骤统一写，不随载荷覆盖
                dest = os.path.join(pylibs, *tail.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                written += 1
    except Exception as e:
        return {"ok": False, "error": "解压组件载荷失败：%s" % e}
    # ③ 合并登记（同 id 覆盖、其它组件保留）
    keep = [c for c in old_components if str(c.get("id")) != cid]
    merged = {"schema": 1, "kind": "dep-components", "updated_at": now_iso(),
              "python": str(inner.get("python") or app_py),
              "platform": str(inner.get("platform") or ""),
              "components": keep + [entry]}
    write_json(m_path, merged)
    # ④ 清空目录（删文件会留下空壳，而"已装组件"是按目录列举的）
    for cur_dir, sub_dirs, _files in os.walk(pylibs, topdown=False):
        for d in sub_dirs:
            p_d = os.path.join(cur_dir, d)
            try:
                if not os.listdir(p_d):
                    os.rmdir(p_d)
            except OSError:
                pass
    after_ids = set(installed_before) | {cid}
    need = [r for r in (entry.get("requires_components") or []) if r not in after_ids]
    native = any(str(f).lower().endswith((".pyd", ".dll")) for f in (entry.get("files") or []))
    return {"ok": True, "kind": "component", "id": cid,
            "version": str(entry.get("version") or meta.get("version") or ""),
            "files_written": written, "files_removed": removed,
            "requires_components_missing": need, "native": native}


def install_scanned(base_dir, data_root, files, force=False):
    """安装扫描结果里的若干项（files 为扫描返回的 path 列表）；逐项返回结果。"""
    results = []
    for path in files:
        name = os.path.basename(path)
        m_pl = PKG_PLUGIN_RE.match(name)
        m_cp = PKG_COMPONENT_RE.match(name)
        if not m_pl and not m_cp:
            results.append({"file": name, "ok": False, "error": "不是可识别的包名"})
            continue
        if not os.path.isfile(path):
            results.append({"file": name, "ok": False, "error": "文件不存在"})
            continue
        if m_cp:
            try:
                r = install_component_package(base_dir, path, force=force)
            except Exception as e:
                r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            r["file"] = name
            results.append(r)
            continue
        # 插件包：走既有校验 + 应用（含备份、登记、回滚点）
        try:
            inspected = inspect_package(path, base_dir, data_root, force=force)
        except Exception as e:
            results.append({"file": name, "ok": False, "error": "校验出错：%s" % e})
            continue
        if not inspected["ok"]:
            results.append({"file": name, "ok": False, "error": "；".join(inspected["errors"])})
            continue
        try:
            rep = apply_package(base_dir, data_root, inspected)
        except Exception as e:
            results.append({"file": name, "ok": False, "error": "应用出错：%s" % e})
            continue
        results.append({"file": name, "ok": bool(rep.get("ok")), "kind": "plugin",
                        "id": inspected["meta"].get("id"),
                        "version": inspected["meta"].get("version"),
                        "from_version": inspected.get("from_version"),
                        "requires_restart": bool(inspected.get("restart_needed")),
                        "error": rep.get("error")})
    ok = [r for r in results if r.get("ok")]
    return {"ok": True, "results": results, "installed": len(ok),
            "failed": len(results) - len(ok),
            "restart_pending": [r["id"] for r in list_plugins(base_dir, data_root)
                                if r.get("restart_pending")]}
