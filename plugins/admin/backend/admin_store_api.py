"""管理后台 — 商店管理面 API（admin 插件同包模块）

本模块由 routes.py 拆出（P3-1 D1）。设计要点（照 batch_io 范式）：与主插件 routes.py
解耦、不反向 import，避免循环依赖——由 routes.py 在 register(app) 的**原注册位置**
调用 register(app, deps) 注入 admin 内部依赖；端点路径/方法/装饰器及其先后/响应体
逐字未动。

接口：商店目录/上架/审核发布（server 条目发布即部署）/重新部署/服务器端停用启用/
卸载（撤下后端包）/下架/元数据编辑/下载统计。管理页 /admin/store 留在 routes.py。
"""

import os

import jz_deps
import jztools_data
from flask import jsonify, request

_deps = {}


def register(app, deps):
    """挂载商店管理面路由（deps 由 routes.py 注入，见模块头）。"""
    global _deps
    _deps = deps
    store_admin = deps["load_store_admin"]()

    @app.get("/plugin/admin/store/list")
    @_deps["login_required"]
    def admin_store_list():
        """全量目录（含 pending/delisted，管理视角）+ 服务器侧部署状态。"""
        _deps["set_operation"]("查询商店目录")
        root = _deps["store_root"]()
        catalog = store_admin.load_catalog(root)
        rows = []
        for entry in catalog.get("packages") or []:
            row = dict(entry)
            if entry.get("runtime") == "server":
                row["installed_version"] = _deps["plugin_admin"].installed_version(_deps["project_dir"], entry.get("id"))
                state_entry = _deps["plugin_admin"].read_state(root).get("plugins", {}).get(entry.get("id")) or {}
                row["restart_pending"] = bool(state_entry.get("restart_pending"))
                row["backups"] = len(_deps["plugin_admin"].list_backups(root, entry.get("id")))
                row["srv_enabled"] = store_admin.srv_enabled(root, entry.get("id"))
            elif entry.get("runtime") == "dep-slot":
                row["srv_component_version"] = store_admin.srv_component_version(
                    _deps["project_dir"], entry.get("id"))
            rows.append(row)
        installed = jz_deps.load_installed_deps(_deps["project_dir"])
        installed_names = sorted({str(k).casefold() for k in (installed.get("modules") or {})}
                                 | {str(k).casefold() for k in (installed.get("packages") or {})})
        return jsonify({"ok": True, "packages": rows, "installed_names": installed_names,
                        "can_review": _deps["can_review_store"](_deps["get_session_user"]()),
                        "super_admin": bool((_deps["get_session_user"]() or {}).get("super_admin")),
                        "index_server_path": store_admin.index_server_path(root),
                        "data_root": root})

    @app.post("/plugin/admin/store/upload")
    @_deps["login_required"]
    def admin_store_upload():
        """上架第 1 步：包上传 + 机器校验 + 元数据落库（pending）。"""
        _deps["set_operation"]("商店上架")
        user = _deps["get_session_user"]() or {}
        runtime = request.form.get("runtime") or ""
        form = {k: (request.form.get(k) or "") for k in
                ("name", "description", "changelog", "icon", "deps_mode", "provides",
                 "requires_json")}
        pkg = request.files.get("file")
        frontend = request.files.get("frontend_file")
        if pkg is None or not pkg.filename:
            return jsonify({"ok": False, "errors": ["未选择包文件"]}), 400
        root = _deps["store_root"]()
        staging = os.path.join(root, ".staging", "store")
        os.makedirs(staging, exist_ok=True)
        stamp = _deps["plugin_admin"].now_stamp()
        pkg_path = os.path.join(staging, "%s-%s" % (stamp, os.path.basename(pkg.filename)))
        pkg.save(pkg_path)
        frontend_path = ""
        if frontend is not None and frontend.filename:
            frontend_path = os.path.join(staging, "%s-fe-%s" % (stamp, os.path.basename(frontend.filename)))
            frontend.save(frontend_path)
        try:
            report = store_admin.submit(root, runtime, pkg_path, form,
                                        str(user.get("username") or ""),
                                        frontend_zip_path=frontend_path or None)
        finally:
            for p in (pkg_path, frontend_path):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        if not report.get("ok"):
            return jsonify(report), 400
        return jsonify(report)

    @app.post("/plugin/admin/store/review")
    @_deps["login_required"]
    def admin_store_review():
        """审核：approve（发布；server 条目含发布即部署）/ reject / republish。

        权限（2026-10-08 修订）：仅管理员 / 超级管理员；提交上架不受此限。
        """
        _deps["set_operation"]("商店审核")
        if not _deps["can_review_store"](_deps["get_session_user"]()):
            return jsonify({"ok": False, "error": "仅管理员或超级管理员可审核（提交上架不受限）"}), 403
        data = request.get_json(silent=True) or {}
        pid = str(data.get("id") or "")
        version = str(data.get("version") or "")
        action = str(data.get("action") or "")
        note = str(data.get("note") or "")
        by = str((_deps["get_session_user"]() or {}).get("username") or "")
        root = _deps["store_root"]()
        if action == "approve":
            catalog = store_admin.load_catalog(root)
            entry = store_admin.find_entry(catalog, pid)
            record = store_admin.find_version_record(entry, version) if entry else None
            if record is None:
                return jsonify({"ok": False, "error": "版本不存在：%s v%s" % (pid, version)}), 404
            # 依赖满足门槛（2026-10-07）：声明的每个依赖，主包未自带且依赖类目未上架时
            # 审核不通过（可提交、不可发布）——依赖由插件制作者或其他用户上架补齐
            installed = jz_deps.load_installed_deps(_deps["project_dir"])
            avail = {str(k).casefold() for k in (installed.get("modules") or {})}
            avail |= {str(k).casefold() for k in (installed.get("packages") or {})}
            unsat = store_admin.unsatisfied_deps(catalog, entry, version, avail)
            if unsat:
                return jsonify({"ok": False,
                                "error": "依赖不满足，审核不通过：%s——请先在「依赖」类目上架对应组件包"
                                         % "、".join(unsat)}), 409
            if entry.get("runtime") == "server":
                # 需求⑧：审核通过即发布，发布即自动部署后端包（当前形态=进程内 inspect/apply；
                # E 架构落地后此处换 supervisor 编排）。上传/待审核阶段绝不部署。
                dep = store_admin.deploy_backend_package(_deps["project_dir"], root, pid, version)
                if not dep.get("ok"):
                    store_admin.mark_deploy_result(root, pid, version, False,
                                                   dep.get("error", ""), by)
                    return jsonify({"ok": False,
                                    "error": "后端包部署失败，发布已回退待审核：%s" % dep.get("error"),
                                    "deploy": dep}), 500
                res = store_admin.review(root, pid, version, "approve", by, note)
                if res.get("ok"):
                    store_admin.mark_deploy_result(root, pid, version, True,
                                                   "v%s 自动部署成功" % version, by)
                    res["dep_deploy"] = _deploy_required_components(root, catalog, entry)
                return jsonify(res)
        res = store_admin.review(root, pid, version, action, by, note)
        if not res.get("ok"):
            return jsonify(res), 400
        if action == "approve":
            # 依赖条目（2026-10-08）：已上架 server 插件需要它 → 发布即部署到服务器
            catalog2 = store_admin.load_catalog(root)
            entry2 = store_admin.find_entry(catalog2, pid)
            if entry2 is not None and entry2.get("runtime") == "dep-slot":
                need = store_admin.deps_for_published_server(catalog2)
                provided = {pid.lower()} | {str(x).lower() for x in (entry2.get("provides") or [])}
                if need & provided:
                    r = store_admin.deploy_dep_component(_deps["project_dir"], root, pid, version)
                    res["dep_deploy"] = ["组件已部署到服务器" if r.get("ok")
                                         else "组件部署失败（%s）" % r.get("error")]
        return jsonify(res)

    def _deploy_required_components(root, catalog, entry):
        """server 插件发布后：把 requires 里「已上架依赖条目」的组件补装到**服务器**（尽力而为）。

        2026-10-08 真机报告：商店依赖的安装只落桌面客户端，服务器侧插件（shared-docs 等）
        在服务器上仍需组件——口径与 server 插件一致：上架（发布）即部署到服务器。
        """
        notes = []
        for x in ((entry.get("requires") or {}).get("python_packages") or []):
            name = str(x.get("name") or "").strip().lower()
            if not name:
                continue
            dep_entry = next((p for p in catalog.get("packages") or []
                              if p.get("runtime") == "dep-slot"
                              and (str(p.get("id") or "").lower() == name
                                   or name in [str(y).lower() for y in (p.get("provides") or [])])), None)
            if dep_entry is None:
                continue
            ver = next((v.get("version") for v in (dep_entry.get("versions") or [])
                        if v.get("status") == "published"), "")
            if not ver:
                continue
            r = store_admin.deploy_dep_component(_deps["project_dir"], root, dep_entry.get("id"), ver)
            notes.append("组件 %s %s" % (dep_entry.get("id"),
                                        "已部署 v%s" % ver if r.get("ok")
                                        else "部署失败（%s）" % r.get("error")))
        return notes

    @app.post("/plugin/admin/store/redeploy")
    @_deps["login_required"]
    def admin_store_redeploy():
        """部署运行面板「重新部署」：把指定版本的 server 后端包重新应用到服务器（当前形态）。

        权限（2026-10-08 修订）：仅管理员 / 超级管理员（发布即部署属服务器敏感操作）。
        """
        _deps["set_operation"]("商店重新部署")
        if not _deps["can_review_store"](_deps["get_session_user"]()):
            return jsonify({"ok": False, "error": "仅管理员或超级管理员可重新部署"}), 403
        data = request.get_json(silent=True) or {}
        pid = str(data.get("id") or "")
        version = str(data.get("version") or "")
        root = _deps["store_root"]()
        entry = store_admin.find_entry(store_admin.load_catalog(root), pid)
        if entry is not None and entry.get("runtime") == "dep-slot":
            # 依赖条目（2026-10-08）：部署到**服务器**（runtime/pylibs；同 install_component_package 链）
            res = store_admin.deploy_dep_component(_deps["project_dir"], root, pid, version)
        else:
            res = store_admin.deploy_backend_package(_deps["project_dir"], root, pid, version)
        return jsonify(res) if res.get("ok") else (jsonify(res), 500)

    @app.post("/plugin/admin/store/srv-state")
    @_deps["login_required"]
    def admin_store_srv_state():
        """停用/启用已上架插件的服务器端运行（**仅超级管理员**）。

        口径（2026-10-08 需求方拍板）：服务器端后端包**只在商店管理的 停用/卸载 时被撤下**——
        「下架」只动客户端可见性、桌面端卸载只删前端包副本，两者都不碰服务器部署。
        停用 = 注册条目 enabled:false（清单/主页即时隐藏；路由随下次服务重启卸载）；后端包保留。
        """
        _deps["set_operation"]("商店停用/启用插件")
        info = _deps["get_session_user"]() or {}
        if not info.get("super_admin"):
            return jsonify({"ok": False, "error": "仅超级管理员可停用/启用服务器端插件"}), 403
        data = request.get_json(silent=True) or {}
        pid = str(data.get("id") or "")
        enabled = bool(data.get("enabled"))
        root = _deps["store_root"]()
        entry = store_admin.find_entry(store_admin.load_catalog(root), pid)
        if entry is None or entry.get("runtime") != "server":
            return jsonify({"ok": False, "error": "仅「服务器」类目已上架条目可停用/启用"}), 400
        if not _deps["plugin_admin"].set_plugin_enabled(root, pid, enabled):
            return jsonify({"ok": False,
                            "error": "注册表（tools.json）中没有该插件条目——先「重新部署」"}), 400
        store_admin.record_event(root, pid, str(data.get("version") or ""),
                                 "srv_enable" if enabled else "srv_disable",
                                 str(info.get("username") or ""), str(data.get("note") or ""))
        return jsonify({"ok": True, "enabled": enabled,
                        "restart_hint": "路由随下次服务重启卸载" if not enabled else "路由随下次服务重启加载"})

    @app.post("/plugin/admin/store/withdraw")
    @_deps["login_required"]
    def admin_store_withdraw():
        """卸载（撤下后端包，**仅超级管理员**）：把已部署后端包从服务器撤下。

        自动备份可「重新部署」原样恢复；条目/版本状态不动（仅记 withdraw 台账）；
        核心插件（admin/store）由 plugin_admin 拒绝卸载。
        """
        _deps["set_operation"]("商店卸载（撤下后端包）")
        info = _deps["get_session_user"]() or {}
        if not info.get("super_admin"):
            return jsonify({"ok": False, "error": "仅超级管理员可卸载（撤下后端包）"}), 403
        data = request.get_json(silent=True) or {}
        pid = str(data.get("id") or "")
        version = str(data.get("version") or "")
        root = _deps["store_root"]()
        entry = store_admin.find_entry(store_admin.load_catalog(root), pid)
        if entry is None or entry.get("runtime") != "server":
            return jsonify({"ok": False, "error": "仅「服务器」类目已上架条目可卸载（撤下后端包）"}), 400
        res = store_admin.withdraw_backend_package(_deps["project_dir"], root, pid)
        if not res.get("ok"):
            return jsonify(res), 500
        store_admin.record_event(root, pid, version, "withdraw",
                                 str(info.get("username") or ""), str(data.get("note") or ""))
        return jsonify({"ok": True, "steps": res.get("steps", []),
                        "unit_stopped": res.get("unit_stopped"),
                        "restart_hint": "路由随下次服务重启卸载；可用「重新部署」原样恢复"})

    @app.post("/plugin/admin/store/delist")
    @_deps["login_required"]
    def admin_store_delist():
        """下架（version 留空 = 最新已上架版；下架最新版 = 投影自动回落旧版）。

        权限（2026-10-08 修订）：仅管理员 / 超级管理员。
        """
        _deps["set_operation"]("商店下架")
        if not _deps["can_review_store"](_deps["get_session_user"]()):
            return jsonify({"ok": False, "error": "仅管理员或超级管理员可下架"}), 403
        data = request.get_json(silent=True) or {}
        res = store_admin.delist(_deps["store_root"](), str(data.get("id") or ""),
                                 str(data.get("version") or ""),
                                 str((_deps["get_session_user"]() or {}).get("username") or ""),
                                 str(data.get("note") or ""))
        return jsonify(res) if res.get("ok") else (jsonify(res), 400)

    @app.post("/plugin/admin/store/meta")
    @_deps["login_required"]
    def admin_store_meta():
        """元数据编辑（name/description/icon），记 history 台账。"""
        _deps["set_operation"]("商店元数据编辑")
        data = request.get_json(silent=True) or {}
        res = store_admin.meta_edit(_deps["store_root"](), str(data.get("id") or ""), data,
                                    str((_deps["get_session_user"]() or {}).get("username") or ""))
        return jsonify(res) if res.get("ok") else (jsonify(res), 400)

    @app.get("/plugin/admin/store/stats")
    @_deps["login_required"]
    def admin_store_stats():
        """下载统计聚合（读 stats.jsonl——store 插件写、admin 只读，单一写方纪律）。"""
        return jsonify({"ok": True, **store_admin.stats_summary(_deps["store_root"]())})
