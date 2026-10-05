# -*- coding: utf-8 -*-
"""supervisor 升级编排（T-O2-11c）：发布即部署的 E 形态实现。

口径（运行架构设计文档 §2 拍板）：
- **停换起同端口**：停旧 → 替换代码 → 同端口起新 → 健康探针确认；失败自动回滚
  到备份并再起旧版——对主应用反代完全透明（端口不变，缓存恒正确）；
- **不打 restart_pending**：worker 由本编排即时重启、即时生效；该标记语义是
  「代码已换、进程未换」（进程内形态），打了会让管理页出现假「待重启」徽标；
- **校验分工**：完整机器校验（SHA256SUMS 全量核对等）在**调用方**（admin 上架
  时已做）；本模块只做结构自保（zip 安全 + payload 结构 + manifest 一致）——
  supervisor 仅标准库、不 import 主包模块（轻量纪律）；
- **备份格式与 plugin_admin 同构**（zip 带 `plugins/<id>/` 前缀，落
  数据根\\backups\\plugins\\<id>\\）——admin 回滚 UI 与 supervisor 回滚互通；
- **控制接口的 apply 传包文件路径**（已上架 packages\\ 下的原包），上传/待审核
  阶段绝不调用本模块（保审核闸门）。
"""

import json
import os
import re
import shutil
import time
import zipfile

HEALTH_WAIT_SECONDS = 20.0
HEALTH_POLL = 0.5


def _log(sup, msg):
    try:
        sup._log(msg)
    except Exception:
        pass


def _zip_entries_safe(zf):
    """路径安全校验（与 plugin_admin.zip_entries_safe 同规则，stdlib 独立实现）。"""
    for info in zf.infolist():
        name = (info.filename or "").replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ":" in name:
            return False, name
        if any(part == ".." for part in name.split("/")):
            return False, name
    return True, ""


def _read_pkg_meta(package_path):
    """读包内 plugin-package.json + manifest.json；结构不符抛 ValueError。"""
    with zipfile.ZipFile(package_path) as zf:
        ok, bad = _zip_entries_safe(zf)
        if not ok:
            raise ValueError("包内含不安全路径：%s" % bad)
        try:
            meta = json.loads(zf.read("plugin-package.json").decode("utf-8-sig"))
        except KeyError:
            raise ValueError("包内缺少 plugin-package.json")
        except Exception as exc:
            raise ValueError("plugin-package.json 不可解析：%s" % exc)
        pid = str(meta.get("id") or "")
        if not re.match(r"^[A-Za-z0-9_-]+$", pid):
            raise ValueError("包清单 id 非法：%r" % pid)
        prefix = "payload/plugins/%s/" % pid
        names = [n.replace("\\", "/") for n in zf.namelist()]
        if not any(n == prefix + "manifest.json" for n in names):
            raise ValueError("包结构不符：缺少 %smanifest.json" % prefix)
        if not any(n.startswith(prefix + "backend/") for n in names):
            raise ValueError("包结构不符：缺少后端载荷 %sbackend/" % prefix)
        return meta, pid


def _backups_dir(data_root, plugin_id):
    return os.path.join(data_root, "backups", "plugins", plugin_id)


def _backup_plugin_dir(plugins_dir, data_root, plugin_id):
    """把当前插件目录打成备份 zip（plugins/<id>/ 前缀，与 admin 回滚同格式）。"""
    src = os.path.join(plugins_dir, plugin_id)
    if not os.path.isdir(src):
        return None                       # 全新安装无备份
    bdir = _backups_dir(data_root, plugin_id)
    os.makedirs(bdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(bdir, "%s-%s-supervisor.zip" % (plugin_id, stamp))
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, src).replace(os.sep, "/")
                zf.write(full, "plugins/%s/%s" % (plugin_id, rel))
    return dst


def _extract_payload_plugin(package_path, plugin_id, dest_dir):
    """把包内 payload/plugins/<id>/** 解压到 dest_dir（先临时解压，替换前再就位）。"""
    with zipfile.ZipFile(package_path) as zf:
        for info in zf.infolist():
            rel = info.filename.replace("\\", "/")
            if rel.endswith("/") or not rel.startswith("payload/plugins/%s/" % plugin_id):
                continue
            tail = rel[len("payload/plugins/%s/" % plugin_id):]
            if not tail:
                continue
            target = os.path.join(dest_dir, *tail.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)


def _health_ok(sup, unit, timeout=HEALTH_WAIT_SECONDS):
    """起新版后等健康探针通过；超时返回 False（由调用方回滚）。"""
    import urllib.request
    port = unit.get("port")
    url = "http://127.0.0.1:%s/_worker/health" % port
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3.0) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(HEALTH_POLL)
    return False


def apply_package(sup, plugin_id, package_path, timeout=HEALTH_WAIT_SECONDS):
    """发布即部署：校验结构 → 备份 → 停旧 → 替换 → 同端口起新 → 健康；失败回滚。

    返回 {ok, id, version, backup?, error?, rolled_back?}。
    """
    try:
        meta, pkg_pid = _read_pkg_meta(package_path)
    except Exception as exc:
        return {"ok": False, "error": "包结构校验失败：%s" % exc}
    if plugin_id and pkg_pid != plugin_id:
        return {"ok": False, "error": "插件 id 不一致：请求 %s，包 %s" % (plugin_id, pkg_pid)}
    plugin_id = pkg_pid
    version = str(meta.get("version") or "")
    plugins_dir = sup.worker_cfg().get("plugins_dir") or ""
    if not plugins_dir or not os.path.isdir(plugins_dir):
        return {"ok": False, "error": "注册表 worker.plugins_dir 未配置"}
    data_root = sup.data_root
    plugin_dir = os.path.join(plugins_dir, plugin_id)

    # ① 注册表登记（存在则复用其端口；不存在则建单元——发布即上线）
    from supervisor import supervisor_core as sc
    registry = sc.load_registry(data_root)
    unit = sc.find_unit(registry, plugin_id)
    if unit is None:
        unit = {"name": plugin_id, "kind": "plugin", "plugin_id": plugin_id,
                "interpreter": "main", "enabled": True}
        registry["units"].append(unit)

    # ② 备份（无备份不替换——与 admin 同底线）
    backup = _backup_plugin_dir(plugins_dir, data_root, plugin_id)

    # ③-⑤ 全程抑制监控（停换起窗口的探针失败不是健康事件——否则编排必被误熔断）
    sup._applying.add(plugin_id)
    try:
        # 先停旧 worker，再做端口复检——否则端口被自己的运行中 worker 占住，
        # ensure_unit_port 会误判冲突（拍板的「冲突报错不起」指外来占用）。
        sup.stop_unit(plugin_id)
        try:
            port = sc.ensure_unit_port(data_root, registry, unit)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "backup": backup}
        if not unit.get("enabled", True):
            unit["enabled"] = True
        sc.save_registry(data_root, registry)
        sup._registry = registry            # 编排后主循环用新表

        # ④ 替换（先解到临时目录，整目录就位——半成品不可见）
        tmp = os.path.join(data_root, ".staging", "supervisor-apply",
                           "%s-%s" % (plugin_id, time.strftime("%Y%m%d-%H%M%S")))
        try:
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            _extract_payload_plugin(package_path, plugin_id, tmp)
            if os.path.isdir(plugin_dir):
                shutil.rmtree(plugin_dir)
            shutil.move(tmp, plugin_dir)
        except Exception as exc:
            _rm(tmp)
            return {"ok": False, "error": "替换代码失败（可回滚）：%s" % exc, "backup": backup}

        # ⑤ 同端口起新 + 健康；失败 → 回滚备份再起旧版
        res = sup.start_unit(plugin_id)
        if not res.get("ok"):
            return _rollback_after_failed_apply(sup, data_root, plugins_dir, plugin_id,
                                                backup, "起新失败：%s" % res.get("error"))
        if not _health_ok(sup, unit, timeout):
            return _rollback_after_failed_apply(sup, data_root, plugins_dir, plugin_id,
                                                backup, "健康探针超时（%ss）" % timeout)
    finally:
        sup._applying.discard(plugin_id)
    # ⑥ 成功：登记版本（不打 restart_pending——worker 已即时生效）
    registry = sc.load_registry(data_root)
    unit = sc.find_unit(registry, plugin_id) or {}
    unit["installed_version"] = version
    sc.save_registry(data_root, registry)
    sup._registry = registry
    _log(sup, "单元 %s 已升级到 v%s（备份：%s）" % (plugin_id, version, backup or "无"))
    return {"ok": True, "id": plugin_id, "version": version,
            "port": unit.get("port"), "backup": backup}


def _rollback_after_failed_apply(sup, data_root, plugins_dir, plugin_id, backup, reason):
    err = {"ok": False, "error": "部署失败，已回滚：%s" % reason, "backup": backup,
           "rolled_back": False}
    if backup and os.path.isfile(backup):
        plugin_dir = os.path.join(plugins_dir, plugin_id)
        tmp = plugin_dir + ".restore-tmp"
        try:
            with zipfile.ZipFile(backup) as zf:
                ok, bad = _zip_entries_safe(zf)
                if not ok:
                    raise ValueError("备份含不安全路径：%s" % bad)
                os.makedirs(tmp, exist_ok=True)
                prefix = "plugins/%s/" % plugin_id
                for info in zf.infolist():
                    rel = info.filename.replace("\\", "/")
                    if rel.endswith("/") or not rel.startswith(prefix):
                        continue
                    target = os.path.join(tmp, *rel[len(prefix):].split("/"))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
            if os.path.isdir(plugin_dir):
                shutil.rmtree(plugin_dir)
            shutil.move(tmp, plugin_dir)
            err["rolled_back"] = True
        except Exception as exc:
            _log(sup, "!! 回滚失败：%s（备份保留：%s）" % (exc, backup))
            err["error"] += "；回滚也失败：%s" % exc
        finally:
            _rm(tmp)
    else:
        # 无备份（全新安装失败）→ 摘除半成品目录
        plugin_dir = os.path.join(plugins_dir, plugin_id)
        if os.path.isdir(plugin_dir):
            shutil.rmtree(plugin_dir, ignore_errors=True)
        err["rolled_back"] = True
        err["note"] = "全新安装失败：已摘除半成品（无旧版可回滚）"
    # 回滚后把旧版再拉起来（同端口）
    r = sup.start_unit(plugin_id)
    err["restart_old"] = r.get("ok")
    _log(sup, "单元 %s 部署失败回滚完成（旧版重启：%s）" % (plugin_id, r.get("ok")))
    return err


def rollback_unit(sup, plugin_id):
    """回滚到最近一份 supervisor/admin 备份（停 → 恢复 → 同端口起）。"""
    plugins_dir = sup.worker_cfg().get("plugins_dir") or ""
    if not plugins_dir:
        return {"ok": False, "error": "注册表 worker.plugins_dir 未配置"}
    bdir = _backups_dir(sup.data_root, plugin_id)
    if not os.path.isdir(bdir):
        return {"ok": False, "error": "该插件没有任何备份"}
    cands = sorted((f for f in os.listdir(bdir) if f.endswith(".zip")),
                   reverse=True)
    if not cands:
        return {"ok": False, "error": "该插件没有任何备份"}
    chosen = os.path.join(bdir, cands[0])
    sup.stop_unit(plugin_id)
    plugin_dir = os.path.join(plugins_dir, plugin_id)
    tmp = plugin_dir + ".restore-tmp"
    try:
        with zipfile.ZipFile(chosen) as zf:
            ok, bad = _zip_entries_safe(zf)
            if not ok:
                return {"ok": False, "error": "备份含不安全路径：%s" % bad}
            os.makedirs(tmp, exist_ok=True)
            prefix = "plugins/%s/" % plugin_id
            for info in zf.infolist():
                rel = info.filename.replace("\\", "/")
                if rel.endswith("/") or not rel.startswith(prefix):
                    continue
                target = os.path.join(tmp, *rel[len(prefix):].split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
        if os.path.isdir(plugin_dir):
            shutil.rmtree(plugin_dir)
        shutil.move(tmp, plugin_dir)
    except Exception as exc:
        _rm(tmp)
        return {"ok": False, "error": "备份恢复失败：%s" % exc}
    finally:
        _rm(tmp)
    r = sup.start_unit(plugin_id)
    from supervisor import supervisor_core as sc
    registry = sc.load_registry(sup.data_root)
    unit = sc.find_unit(registry, plugin_id)
    if unit is not None:
        try:
            with open(os.path.join(plugin_dir, "manifest.json"),
                      "r", encoding="utf-8-sig") as f:
                unit["installed_version"] = str(json.load(f).get("version") or "")
            sc.save_registry(sup.data_root, registry)
            sup._registry = registry
        except Exception:
            pass
    return {"ok": True, "id": plugin_id, "backup": chosen,
            "restart": r.get("ok"), "port": (unit or {}).get("port")}


def _rm(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
