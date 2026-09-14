"""管理后台 · 插件管理（阶段二/三）：插件包的上传校验 / 应用 / 回滚 / 批量升级。

与目标机离线安装器 ``tools/plugin-upgrade/install-plugin.ps1`` **共用同一套规则**
（规则单点定义见 ``docs/插件独立升级方案-设计文档.md`` §4 / §5.3，两处实现必须逐条一致）：

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


def list_plugins(base_dir, data_root, tools_cfg=None):
    """列出所有插件的 展示名 / 代码版本 / 登记版本 / 待重启 / 备份 / 数据占用 / 启停状态。"""
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
