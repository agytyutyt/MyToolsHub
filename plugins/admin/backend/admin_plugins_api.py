"""管理后台 — 插件管理面 API（admin 插件同包模块）

本模块由 routes.py 拆出（P3-1 D1）。设计要点（照 batch_io 范式）：与主插件 routes.py
解耦、不反向 import，避免循环依赖——由 routes.py 在 register(app) 的**原注册位置**
调用 register(app, deps) 注入 admin 内部依赖；端点路径/方法/装饰器及其先后/响应体
逐字未动。

接口（均需登录且仅超级管理员）：插件盘点/上传/应用/备份/回滚/启停/卸载/对齐/
索引比对/批量应用、依赖清单、包扫描与安装、服务重启。
"""

import os
import re
import sys

import jztools_data
from flask import jsonify, request

_deps = {}


def register(app, deps):
    """挂载插件管理面路由（deps 由 routes.py 注入，见模块头）。"""
    global _deps
    _deps = deps

    @app.get("/api/admin/plugins")
    @_deps["login_required"]
    def admin_api_plugins():
        """插件盘点：代码版本 / 登记版本 / 待重启 / 备份数 / 数据占用 / 启停。"""
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        _deps["set_operation"]("查询插件列表")
        rows = _deps["plugin_admin"].list_plugins(_deps["project_dir"], jztools_data.get_data_root())
        cfg = _deps["load_admin_config"]()
        # 依赖概况（设计文档 §5.3 展示面）：可运行 / 降级 / 不可运行 三档汇总，
        # 前端据此显示"逐依赖徽标（绿 / 红 ×）"与顶部横幅。
        summary = {"ok": 0, "degraded": 0, "blocked": 0, "unknown": 0}
        for r in rows:
            d = r.get("deps") or {}
            st = d.get("status") or "unknown"
            summary[st if st in summary else "unknown"] += 1
        return jsonify({
            "ok": True,
            "plugins": rows,
            "deps_summary": summary,
            "app_version": _deps["plugin_admin"].app_version(_deps["project_dir"]) or "",
            "data_root": jztools_data.get_data_root(),
            "index_path": cfg.get("plugin_index_path") or "",
            "frozen": bool(getattr(sys, "frozen", False)),
            "restart_pending": [r["id"] for r in rows if r["restart_pending"]],
        })

    @app.get("/api/admin/deps")
    @_deps["login_required"]
    def admin_api_deps():
        """本机「已安装依赖」清单（框架包 / 外部组件 / 插件自带，含版本）。

        设计文档 §5.3 D-7：管理员要能看到"本机装了什么、什么版本"，
        才能判断插件为什么不可运行（缺什么、要不要装组件）。
        """
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可查看依赖清单"}), 403
        _deps["set_operation"]("查询已安装依赖清单")
        return jsonify(_deps["plugin_admin"].installed_deps(_deps["project_dir"]))

    @app.post("/api/admin/plugins/upload")
    @_deps["login_required"]
    def admin_api_plugins_upload():
        """上传插件包并只读校验；返回应用计划（尚未写程序目录）。"""
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        _deps["set_operation"]("上传插件包")
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "请选择插件包（.zip）"}), 400
        if not f.filename.lower().endswith(".zip"):
            return jsonify({"error": "只接受 .zip 插件包（由 tools\\build-plugin-package.ps1 生成）"}), 400
        if request.content_length and request.content_length > _deps["plugin_admin"].MAX_ZIP_BYTES:
            return jsonify({"error": "包体积超过上限 %.0f MB" % (_deps["plugin_admin"].MAX_ZIP_BYTES / 1048576)}), 413
        force = str(request.form.get("force") or "").lower() in ("1", "true", "on")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(f.filename))
        name = "%s-%s" % (_deps["plugin_admin"].now_stamp(), safe)
        dest = os.path.join(_deps["staging_uploads_dir"](), name)
        try:
            f.save(dest)
        except Exception as e:
            return jsonify({"error": "保存上传文件失败：%s" % e}), 500
        try:
            inspected = _deps["plugin_admin"].inspect_package(dest, _deps["project_dir"], jztools_data.get_data_root(), force=force)
        except Exception as e:
            return jsonify({"error": "校验时出错：%s" % e}), 500
        if not inspected["ok"]:
            # 同时给 error 摘要：前端通用 api() 抛错只取 data.error，
            # 没有它的话管理员只会看到"HTTP 400"而看不到真正的拒绝原因。
            return jsonify({"ok": False, "file": name,
                            "error": "；".join(inspected["errors"]),
                            "errors": inspected["errors"],
                            "warnings": inspected["warnings"]}), 400
        meta = inspected["meta"]
        plan = inspected["plan"]
        return jsonify({
            "ok": True, "file": name, "id": meta.get("id"), "version": meta.get("version"),
            "name": _deps["plugin_display_name"](str(meta.get("id") or ""), meta.get("tools_entry")),
            "from_version": inspected["from_version"],
            "requires_restart": inspected["restart_needed"],
            "restart_reason": inspected["restart_reason"],
            "min_app_version": meta.get("min_app_version") or "",
            "warnings": inspected["warnings"],
            "plan": {
                "new": len(plan.get("new") or []),
                "changed": len(plan.get("changed") or []),
                "unchanged": plan.get("unchanged") or 0,
                "deleted": len(plan.get("deleted") or []),
                "unknown": len(plan.get("unknown") or []),
                "base_source": plan.get("base_source") or "",
                "deleted_files": (plan.get("deleted") or [])[:50],
                "unknown_files": (plan.get("unknown") or [])[:50],
            },
        })

    @app.post("/api/admin/plugins/apply")
    @_deps["login_required"]
    def admin_api_plugins_apply():
        """应用已上传的插件包：服务端重新校验 → 备份 → 替换 → 登记（可回滚）。"""
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        path = _deps["resolve_upload"](data.get("file"))
        if not path:
            return jsonify({"error": "上传的包不存在或已过期，请重新上传"}), 404
        _deps["set_operation"]("应用插件包")
        base, root = _deps["project_dir"], jztools_data.get_data_root()
        inspected = _deps["plugin_admin"].inspect_package(path, base, root, force=bool(data.get("force")))
        if not inspected["ok"]:
            # 同上：带上 error 摘要，避免前端只显示"HTTP 400"
            return jsonify({"ok": False, "error": "；".join(inspected["errors"]),
                            "errors": inspected["errors"]}), 400
        report = _deps["plugin_admin"].apply_package(
            base, root, inspected,
            purge_unknown=bool(data.get("purge_unknown")),
            update_entry=bool(data.get("update_entry")),
        )
        if report.get("ok"):
            try:
                os.remove(path)          # 应用成功即清理上传包（PU-5）
            except OSError:
                pass
        report["restart_pending"] = [r["id"] for r in _deps["plugin_admin"].list_plugins(base, root)
                                     if r["restart_pending"]]
        report["name"] = _deps["plugin_display_name"](str(inspected["meta"].get("id") or ""),
                                               inspected["meta"].get("tools_entry"))
        # 应用成功且含后端改动 → 自动停服重启（data.auto_restart=false 可关闭）
        _deps["maybe_auto_restart"](report, auto=data.get("auto_restart", True))
        return jsonify(report), (200 if report.get("ok") else 500)

    @app.get("/api/admin/plugins/backups")
    @_deps["login_required"]
    def admin_api_plugins_backups():
        """某插件的备份清单（供回滚选择）。"""
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        pid = (request.args.get("id") or "").strip()
        if not _deps["plugin_admin"].PLUGIN_ID_RE.match(pid):
            return jsonify({"error": "插件 id 非法"}), 400
        _deps["set_operation"]("查询插件备份")
        items = _deps["plugin_admin"].list_backups(jztools_data.get_data_root(), pid)
        return jsonify({"ok": True, "backups": [
            {"name": it["name"], "size": it["size"], "mtime": it["mtime"]} for it in items]})

    @app.post("/api/admin/plugins/rollback")
    @_deps["login_required"]
    def admin_api_plugins_rollback():
        """回滚插件到某个备份（缺省=最近一份）。"""
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        pid = str(data.get("id") or "").strip()
        if not _deps["plugin_admin"].PLUGIN_ID_RE.match(pid):
            return jsonify({"error": "插件 id 非法"}), 400
        _deps["set_operation"]("回滚插件")
        # 只允许回滚到该插件备份目录内的文件（拒绝任意路径）
        backup = None
        name = os.path.basename(str(data.get("backup") or ""))
        if name:
            candidate = os.path.join(_deps["plugin_admin"].backups_dir(jztools_data.get_data_root(), pid), name)
            if os.path.isfile(candidate):
                backup = candidate
            else:
                return jsonify({"error": "指定的备份不存在"}), 404
        report = _deps["plugin_admin"].rollback_plugin(_deps["project_dir"], jztools_data.get_data_root(), pid, backup)
        report["restart_pending"] = [r["id"] for r in _deps["plugin_admin"].list_plugins(_deps["project_dir"], jztools_data.get_data_root())
                                     if r["restart_pending"]]
        report["name"] = _deps["plugin_display_name"](pid)
        # 回滚是整目录替换，一律需要重启；同样走自动停服重启
        _deps["maybe_auto_restart"](report, needed=True, auto=data.get("auto_restart", True))
        return jsonify(report), (200 if report.get("ok") else 500)

    @app.post("/api/admin/plugins/enable")
    @_deps["login_required"]
    def admin_api_plugins_enable():
        """启用 / 停用插件（写数据根 tools.json，不动代码与数据）。"""
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        pid = str(data.get("id") or "").strip()
        if not _deps["plugin_admin"].PLUGIN_ID_RE.match(pid):
            return jsonify({"error": "插件 id 非法"}), 400
        enabled = bool(data.get("enabled"))
        _deps["set_operation"]("启用插件" if enabled else "停用插件")
        ok = _deps["plugin_admin"].set_plugin_enabled(jztools_data.get_data_root(), pid, enabled)
        if not ok:
            return jsonify({"error": "tools.json 中没有该插件的注册条目"}), 404
        return jsonify({"ok": True, "id": pid, "enabled": enabled})

    @app.post("/api/admin/plugins/uninstall")
    @_deps["login_required"]
    def admin_api_plugins_uninstall():
        """卸载插件（规范 §11 顺序：删/停注册条目 → 备份数据 → 删目录）。

        - 核心插件（manifest.core = true，如 admin）拒绝卸载（设计文档 §3.5）；
        - 用户数据默认保留；purge_entry 删除注册条目，backup_data 先只读打包一份数据。
        """
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        pid = str(data.get("id") or "").strip()
        if not _deps["plugin_admin"].PLUGIN_ID_RE.match(pid):
            return jsonify({"error": "插件 id 非法"}), 400
        _deps["set_operation"]("卸载插件")
        res = _deps["plugin_admin"].uninstall_plugin(
            _deps["project_dir"], jztools_data.get_data_root(), pid,
            purge_entry=bool(data.get("purge_entry")),
            backup_data=bool(data.get("backup_data")),
        )
        if not res.get("ok"):
            return jsonify({"error": res.get("error") or "卸载失败", "steps": res.get("steps") or []}), 400
        app.logger.warning("插件 %s 已被管理员卸载：%s", pid, "；".join(res.get("steps") or []))
        return jsonify(res)

    @app.post("/api/admin/plugins/align")
    @_deps["login_required"]
    def admin_api_plugins_align():
        """把状态登记的版本对齐到程序目录的实际版本（登记漂移的一键修复）。

        只改数据根 .app_state.json 的登记版本；代码与用户数据不动。
        防回退比较的是登记版本，漂移会让下次主包升级误判 → 该操作消除这个隐患。
        """
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        pid = str(data.get("id") or "").strip()
        if pid and pid != "*":                      # 单插件（保留能力）
            if not _deps["plugin_admin"].PLUGIN_ID_RE.match(pid):
                return jsonify({"error": "插件 id 非法"}), 400
            _deps["set_operation"]("对齐插件登记")
            res = _deps["plugin_admin"].align_state_version(_deps["project_dir"], jztools_data.get_data_root(), pid)
            if not res.get("ok"):
                return jsonify({"error": res.get("error") or "对齐失败"}), 400
            if res.get("changed"):
                app.logger.warning("插件 %s 的登记版本已对齐：%s → %s", pid,
                                   res.get("from_version") or "（无登记）", res.get("to_version"))
            return jsonify(res)
        # 一键对齐（列表外按钮）：不带 id 或 id="*"
        _deps["set_operation"]("一键对齐插件登记")
        res = _deps["plugin_admin"].align_state_versions(_deps["project_dir"], jztools_data.get_data_root())
        app.logger.warning("一键对齐插件登记：%d 个已对齐（共 %d 个插件，失败 %d）",
                           len(res.get("aligned") or []), res.get("total"), len(res.get("failed") or []))
        return jsonify(res)

    @app.get("/api/admin/plugins/index")
    @_deps["login_required"]
    def admin_api_plugins_index():
        """读取共享盘索引并比对已装版本（阶段三）。path 缺省用上次记住的路径。

        D1 裁定 6 方案 (b)：本处读改写段中间夹着共享盘读取（`check_updates` 走网络盘），
        整段包锁会把全部 admin.json 写排在它之后，故拆成**两次进锁**：
          ① 锁内只取「上次记住的路径」这一份快照；
          ② 共享盘读取在锁外（锁内绝不含盘/网络 IO，读失败的错误语义原样保留）；
          ③ 再进锁**重新 load**（不得复用①的对象，否则②期间其他字段的并发改动会被
             旧对象整体覆盖）、以快照为依据只改 `plugin_index_path` 一个字段后 save。
        同一字段的并发（两人同时换索引路径）按「后写者为准」——与其余读改写处同语义。
        """
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        with _deps["admin_cfg_lock"]:                  # 第一段锁：只取快照，不做盘/网络 IO
            remembered = _deps["load_admin_config"]().get("plugin_index_path") or ""
        path = (request.args.get("path") or "").strip() or remembered
        if not path:
            return jsonify({"error": "请填写索引文件路径（共享盘上的 index.json）"}), 400
        _deps["set_operation"]("检查插件更新")
        res = _deps["plugin_admin"].check_updates(_deps["project_dir"], jztools_data.get_data_root(), path)
        if not res.get("ok"):
            return jsonify(res), 400
        # 记住路径，下次免填（同一台机器上通常固定一个共享目录）
        if path != remembered:
            with _deps["admin_cfg_lock"]:              # 第二段锁：重新 load 后只改这一个字段
                cfg = _deps["load_admin_config"]()
                cfg["plugin_index_path"] = path
                _deps["save_admin_config"](cfg)
        return jsonify(res)

    @app.post("/api/admin/plugins/batch-apply")
    @_deps["login_required"]
    def admin_api_plugins_batch_apply():
        """按索引批量应用插件包（顺序应用；后端改动则最后统一重启一次）。"""
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        cfg = _deps["load_admin_config"]()
        index_path = (data.get("index_path") or "").strip() or (cfg.get("plugin_index_path") or "")
        ids = [str(x) for x in (data.get("ids") or []) if str(x)]
        if not index_path or not ids:
            return jsonify({"error": "请先检查更新并勾选要升级的插件"}), 400
        _deps["set_operation"]("批量升级插件")
        res = _deps["plugin_admin"].batch_apply(_deps["project_dir"], jztools_data.get_data_root(), index_path, ids)
        res["restart_pending"] = [r["id"] for r in _deps["plugin_admin"].list_plugins(_deps["project_dir"], jztools_data.get_data_root())
                                  if r["restart_pending"]]
        # 批量应用：全部成功后统一重启一次
        _deps["maybe_auto_restart"](res, auto=data.get("auto_restart", True))
        return jsonify(res)

    @app.post("/api/admin/packages/scan")
    @_deps["login_required"]
    def admin_api_packages_scan():
        """扫描介质目录里的插件包与依赖组件包（只读，不安装）。

        目录缺省用 default_scan_dirs()（程序目录 / 上级 / 桌面 / 下载）；
        管理员也可指定路径（离线部署时介质常解压到 U 盘或某个临时目录）。
        """
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        raw = (data.get("dir") or "").strip()
        dirs = None
        if raw:
            if not os.path.isdir(raw):
                return jsonify({"error": "目录不存在或不可读：%s" % raw}), 400
            dirs = [raw]
        _deps["set_operation"]("扫描介质目录")
        try:
            res = _deps["plugin_admin"].scan_packages(_deps["project_dir"], jztools_data.get_data_root(),
                                             dirs=dirs, force=bool(data.get("force")))
        except Exception as e:
            return jsonify({"error": "扫描出错：%s" % e}), 500
        res["default_dirs"] = _deps["plugin_admin"].default_scan_dirs(_deps["project_dir"])
        res["suggest"] = [it for it in res["items"] if it["status"] == "install"]
        return jsonify(res)

    @app.post("/api/admin/packages/install")
    @_deps["login_required"]
    def admin_api_packages_install():
        """安装扫描到的包（插件包 + 依赖组件包）。

        安全：只接受**服务端重新扫描得到**的包路径，不接受前端传来的任意路径
        （前端把扫描结果里的 path 回传，服务端再扫一次做白名单比对）。
        """
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        wanted = [str(x) for x in (data.get("paths") or []) if str(x)]
        if not wanted:
            return jsonify({"error": "请先扫描并勾选要安装的包"}), 400
        raw = (data.get("dir") or "").strip()
        dirs = [raw] if raw and os.path.isdir(raw) else None
        _deps["set_operation"]("安装扫描到的包")
        scan = _deps["plugin_admin"].scan_packages(_deps["project_dir"], jztools_data.get_data_root(), dirs=dirs)
        allowed = {it["path"]: it for it in scan["items"]}
        picked = [p for p in wanted if p in allowed]
        rejected = [p for p in wanted if p not in allowed]
        if not picked:
            return jsonify({"error": "勾选的包不在扫描结果里（请重新扫描后再装）",
                            "rejected": rejected}), 400
        try:
            res = _deps["plugin_admin"].install_scanned(_deps["project_dir"], jztools_data.get_data_root(),
                                               picked, force=bool(data.get("force")))
        except Exception as e:
            return jsonify({"error": "安装出错：%s" % e}), 500
        # 依赖组件装完**无需重启**（主体下次请求会自动补注入路径）；插件含后端改动才需要
        needs_restart = bool(res.get("restart_pending")) or any(
            r.get("requires_restart") for r in res.get("results", []) if r.get("ok"))
        res["rejected"] = rejected
        res["needs_restart"] = needs_restart
        if needs_restart:
            _deps["maybe_auto_restart"](res, auto=data.get("auto_restart", True))
        return jsonify(res)

    @app.post("/api/admin/plugins/restart")
    @_deps["login_required"]
    def admin_api_plugins_restart():
        """重启服务让新插件代码生效（仅打包运行；源码模式提示手工重启）。"""
        if not _deps["plugin_mgr_ok"]():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        _deps["set_operation"]("重启服务（插件生效）")
        rows = _deps["plugin_admin"].list_plugins(_deps["project_dir"], jztools_data.get_data_root())
        pending = [r["id"] for r in rows if r["restart_pending"]]
        if not getattr(sys, "frozen", False):
            return jsonify({"ok": False, "pending": pending,
                            "message": "源码开发模式不自动重启：Flask 调试重载通常会自行重启；"
                                       "若未生效请手工重启 python app.py"})
        ok, err = _deps["spawn_self_restart"]()
        if not ok:
            return jsonify({"ok": False, "pending": pending, "error": err}), 500
        return jsonify({"ok": True, "pending": pending,
                        "message": "服务正在重启，约 5~10 秒后本页会自动重新加载"})
