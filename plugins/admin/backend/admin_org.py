"""管理后台 — 组织 / 部门 / 人员 / 角色 管理路由（admin 插件同包模块）

本模块由 routes.py 拆出（P3-1 D1）。设计要点（照 batch_io 范式）：与主插件 routes.py
解耦、不反向 import，避免循环依赖——由 routes.py 在 register(app) 的**原注册位置**
调用 register(app, deps) 注入 admin 内部依赖；端点路径/方法/装饰器及其先后/响应体
逐字未动。

接口（均要求对应模块权限）：units / departments / users 增删改查、
permission-points 清单、permissions（角色）增删改查。
"""

import re
import uuid

import jz_llm
from flask import jsonify, request
from werkzeug.security import generate_password_hash

_deps = {}


def register(app, deps):
    """挂载组织 / 账号管理路由（deps 由 routes.py 注入，见模块头）。"""
    global _deps
    _deps = deps

    @app.get("/api/admin/units")
    @_deps["permission_required"]("unit")
    def admin_api_units():
        _deps["set_operation"]("查询单位列表")
        cfg = _deps["load_admin_config"]()
        return jsonify({"units": [
            {
                "id": u["id"],
                "name": u["name"],
                "description": u.get("description", ""),
                "departments_count": len(u.get("departments", [])),
                "users_count": sum(len(d.get("users", [])) for d in u.get("departments", [])),
            }
            for u in cfg.get("units", [])
        ]})

    @app.post("/api/admin/units")
    @_deps["permission_required"]("unit")
    def admin_api_units_create():
        _deps["set_operation"]("新增单位")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "单位名称不能为空"}), 400
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            units = cfg.setdefault("units", [])
            if any(u["name"] == name for u in units):
                return jsonify({"error": "单位名称已存在"}), 400
            unit = {
                "id": "unit-" + uuid.uuid4().hex[:8],
                "name": name,
                "description": (data.get("description") or "").strip(),
                "departments": [],
            }
            units.append(unit)
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True, "unit": unit})

    @app.put("/api/admin/units/<unit_id>")
    @_deps["permission_required"]("unit")
    def admin_api_units_update(unit_id):
        _deps["set_operation"]("修改单位")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "单位名称不能为空"}), 400
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            unit = _deps["find_unit"](cfg, unit_id)
            if unit is None:
                return jsonify({"error": "单位不存在"}), 404
            if any(u["id"] != unit_id and u["name"] == name for u in cfg.get("units", [])):
                return jsonify({"error": "单位名称已存在"}), 400
            unit["name"] = name
            unit["description"] = (data.get("description") or "").strip()
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True})

    @app.delete("/api/admin/units/<unit_id>")
    @_deps["permission_required"]("unit")
    def admin_api_units_delete(unit_id):
        _deps["set_operation"]("删除单位")
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            unit = _deps["find_unit"](cfg, unit_id)
            if unit is None:
                return jsonify({"error": "单位不存在"}), 404
            if unit.get("departments"):
                return jsonify({"error": "该单位下仍有部门，请先调整或删除部门"}), 400
            cfg["units"] = [u for u in cfg.get("units", []) if u["id"] != unit_id]
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True})

    # ---------------- 部门管理 ----------------

    @app.get("/api/admin/departments")
    @_deps["permission_required"]("department")
    def admin_api_departments():
        _deps["set_operation"]("查询部门列表")
        cfg = _deps["load_admin_config"]()
        departments = []
        for unit in cfg.get("units", []):
            for dept in unit.get("departments", []):
                departments.append({
                    "id": dept["id"],
                    "name": dept["name"],
                    "description": dept.get("description", ""),
                    "unit_id": unit["id"],
                    "unit_name": unit["name"],
                    "users_count": len(dept.get("users", [])),
                })
        return jsonify({
            "departments": departments,
            "units": [{"id": u["id"], "name": u["name"]} for u in cfg.get("units", [])],
        })

    @app.post("/api/admin/departments")
    @_deps["permission_required"]("department")
    def admin_api_departments_create():
        _deps["set_operation"]("新增部门")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        unit_id = (data.get("unit_id") or "").strip()
        if not name:
            return jsonify({"error": "部门名称不能为空"}), 400
        if not unit_id:
            return jsonify({"error": "请选择所属单位"}), 400
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            unit = _deps["find_unit"](cfg, unit_id)
            if unit is None:
                return jsonify({"error": "所属单位不存在"}), 404
            if any(d["name"] == name for d in unit.get("departments", [])):
                return jsonify({"error": "该单位下已存在同名部门"}), 400
            dept = {
                "id": "dept-" + uuid.uuid4().hex[:8],
                "name": name,
                "description": (data.get("description") or "").strip(),
                "users": [],
            }
            unit.setdefault("departments", []).append(dept)
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True, "department": dept})

    @app.put("/api/admin/departments/<dept_id>")
    @_deps["permission_required"]("department")
    def admin_api_departments_update(dept_id):
        _deps["set_operation"]("修改部门")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "部门名称不能为空"}), 400
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            found = _deps["find_dept"](cfg, dept_id)
            if found is None:
                return jsonify({"error": "部门不存在"}), 404
            old_unit, dept = found
            if any(d["id"] != dept_id and d["name"] == name for d in old_unit.get("departments", [])):
                return jsonify({"error": "该单位下已存在同名部门"}), 400
            dept["name"] = name
            dept["description"] = (data.get("description") or "").strip()
            # 移动部门到其他单位
            new_unit_id = (data.get("unit_id") or "").strip()
            if new_unit_id and new_unit_id != old_unit["id"]:
                target = _deps["find_unit"](cfg, new_unit_id)
                if target is None:
                    return jsonify({"error": "目标单位不存在"}), 404
                if any(d["id"] != dept_id and d["name"] == name for d in target.get("departments", [])):
                    return jsonify({"error": "目标单位下已存在同名部门"}), 400
                old_unit["departments"] = [d for d in old_unit.get("departments", []) if d["id"] != dept_id]
                target.setdefault("departments", []).append(dept)
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True})

    @app.delete("/api/admin/departments/<dept_id>")
    @_deps["permission_required"]("department")
    def admin_api_departments_delete(dept_id):
        _deps["set_operation"]("删除部门")
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            found = _deps["find_dept"](cfg, dept_id)
            if found is None:
                return jsonify({"error": "部门不存在"}), 404
            unit, dept = found
            if dept.get("users"):
                return jsonify({"error": "该部门下仍有人员，请先调整其所属部门"}), 400
            unit["departments"] = [d for d in unit.get("departments", []) if d["id"] != dept_id]
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True})

    # ---------------- 人员管理 ----------------

    @app.get("/api/admin/users")
    @_deps["permission_required"]("user")
    def admin_api_users():
        _deps["set_operation"]("查询人员列表")
        cfg = _deps["load_admin_config"]()
        role_map = {r["id"]: r["name"] for r in cfg.get("permissions", [])}
        users = []
        for unit, dept, user in _deps["iter_users"](cfg):
            users.append({
                "username": user["username"],
                "name": user.get("name") or user["username"],
                "unit_id": unit["id"],
                "unit_name": unit["name"],
                "department_id": dept["id"],
                "department_name": dept["name"],
                "role": user.get("role", ""),
                "role_name": role_map.get(user.get("role", ""), ""),
                "super_admin": _deps["role_super_admin"](cfg, user.get("role")),
                "idcard": _deps["decrypt_field"](user.get("idcard")),
                "permissions": user.get("permissions", []),
                "llm": {
                    "format": (user.get("llm") or {}).get("format", "openai"),
                    "base_url": (user.get("llm") or {}).get("base_url", ""),
                    # API Key **不回明文**，只回"是否已配置"与固定掩码——与
                    # /api/llm/settings、插件 /config 同一口径（F-4 / SEC-2：
                    # 明文凭据不进浏览器；代填/清除走本弹窗或批量导入）。
                    "api_key": jz_llm.mask_key((user.get("llm") or {}).get("api_key")),
                    "api_key_set": bool((user.get("llm") or {}).get("api_key")),
                    "model": (user.get("llm") or {}).get("model", ""),
                },
            })
        return jsonify({
            "users": users,
            "units": [{"id": u["id"], "name": u["name"]} for u in cfg.get("units", [])],
            "departments": [
                {"id": d["id"], "name": d["name"], "unit_id": unit["id"]}
                for unit in cfg.get("units", []) for d in unit.get("departments", [])
            ],
            "permissions": cfg.get("permissions", []),
        })

    @app.post("/api/admin/users")
    @_deps["permission_required"]("user")
    def admin_api_users_create():
        _deps["set_operation"]("新增人员")
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        name = (data.get("name") or "").strip()
        password = data.get("password") or ""
        unit_id = (data.get("unit_id") or "").strip()
        dept_id = (data.get("department_id") or "").strip()
        if not username:
            return jsonify({"error": "登录名不能为空"}), 400
        if not re.match(r"^[A-Za-z0-9_.-]+$", username):
            return jsonify({"error": "登录名只能包含字母、数字、_、.、-"}), 400
        if not name:
            return jsonify({"error": "姓名不能为空"}), 400
        if len(password) < 6:
            return jsonify({"error": "密码长度不能少于 6 位"}), 400
        if not unit_id or not dept_id:
            return jsonify({"error": "请选择所属单位与部门"}), 400
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            if _deps["find_user"](cfg, username) is not None:
                return jsonify({"error": "该登录名已存在"}), 400
            llm = data.get("llm") or {}
            if not isinstance(llm, dict):
                llm = {}
            role_id = (data.get("role") or "").strip()
            if "permissions" in data:
                permissions, perr = _deps["normalize_permission_points"](data.get("permissions"))
                if perr:
                    return jsonify({"error": perr}), 400
            else:
                # 未显式指定权限点时：管理员（超管）不依赖权限点；普通角色（办案员等）
                # 默认授予全部工具（不含管理后台），管理员可在「权限」弹窗中再调整。
                if _deps["role_super_admin"](cfg, role_id):
                    permissions = []
                else:
                    permissions = sorted(_deps["registered_tool_ids"]() - {"admin"})
            user = {
                "username": username,
                "password": _deps["encrypt_field"](generate_password_hash(password)),
                "name": name,
                "idcard": _deps["encrypt_field"](data.get("idcard") or ""),
                "role": role_id,
                "permissions": permissions,
                "llm": {
                    "format": (str(llm.get("format") or "").strip().lower()
                               if str(llm.get("format") or "").strip().lower() in jz_llm.FORMAT_META
                               else "openai"),
                    "base_url": (llm.get("base_url") or "").strip(),
                    "api_key": _deps["encrypt_field"](llm.get("api_key") or ""),
                    "model": (llm.get("model") or "").strip(),
                },
            }
            if not _deps["append_user"](cfg, unit_id, dept_id, user):
                return jsonify({"error": "所属单位或部门不存在"}), 404
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True})

    @app.put("/api/admin/users/<username>")
    @_deps["permission_required"]("user")
    def admin_api_users_update(username):
        _deps["set_operation"]("修改人员")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "姓名不能为空"}), 400
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            found = _deps["find_user"](cfg, username)
            if found is None:
                return jsonify({"error": "人员不存在"}), 404
            unit, dept, user = found
            user["name"] = name
            user["role"] = (data.get("role") or "").strip()
            password = data.get("password") or ""
            if password:
                if len(password) < 6:
                    return jsonify({"error": "密码长度不能少于 6 位"}), 400
                user["password"] = _deps["encrypt_field"](generate_password_hash(password))
            # 身份证：传空字符串则清空，不传则保留原值
            if "idcard" in data:
                user["idcard"] = _deps["encrypt_field"](data.get("idcard") or "")
            # 权限点（tools.json 中的插件 ID）
            if "permissions" in data:
                permissions, perr = _deps["normalize_permission_points"](data.get("permissions"))
                if perr:
                    return jsonify({"error": perr}), 400
                user["permissions"] = permissions
            # 大模型配置（用户"各自设置"模式下由本人自助维护，此处供管理员代填）
            llm = data.get("llm")
            if isinstance(llm, dict):
                u_llm = user.setdefault("llm", {})
                if "format" in llm:
                    fmt = (llm.get("format") or "").strip().lower()
                    u_llm["format"] = fmt if fmt in jz_llm.FORMAT_META else "openai"
                if "base_url" in llm:
                    u_llm["base_url"] = (llm.get("base_url") or "").strip()
                if "api_key" in llm:
                    # 掩码/留空 = 不修改（与 /api/llm/settings、插件 /config、批量导入同一口径）。
                    # 不这样处理会把字面量 "••••••••" 存成 Key，用户此后调用一律 401 且原因难查。
                    if not jz_llm.is_mask(llm.get("api_key")):
                        u_llm["api_key"] = _deps["encrypt_field"](str(llm.get("api_key")).strip())
                if "model" in llm:
                    u_llm["model"] = (llm.get("model") or "").strip()
            # 调整所属单位 / 部门（移动用户）
            new_unit_id = (data.get("unit_id") or "").strip()
            new_dept_id = (data.get("department_id") or "").strip()
            if new_unit_id and new_dept_id and (new_unit_id != unit["id"] or new_dept_id != dept["id"]):
                target = _deps["find_dept"](cfg, new_dept_id)
                if target is None or target[0]["id"] != new_unit_id:
                    return jsonify({"error": "目标单位或部门不存在"}), 404
                dept["users"] = [u for u in dept.get("users", []) if u.get("username") != username]
                target[1].setdefault("users", []).append(user)
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True})

    @app.delete("/api/admin/users/<username>")
    @_deps["permission_required"]("user")
    def admin_api_users_delete(username):
        _deps["set_operation"]("删除人员")
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            if username == _deps["get_session_user"]()["username"]:
                return jsonify({"error": "不能删除当前登录账号"}), 400
            found = _deps["find_user"](cfg, username)
            if found is None:
                return jsonify({"error": "人员不存在"}), 404
            _unit, dept, _user = found
            dept["users"] = [u for u in dept.get("users", []) if u.get("username") != username]
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True})

    # ---------------- 权限（角色）管理（已并入人员管理，gate 用 user 模块） ----------------

    @app.get("/api/admin/permission-points")
    @_deps["permission_required"]("user")
    def admin_api_permission_points():
        """返回全部已注册工具（含停用），供人员「权限设置」弹窗勾选权限点。

        与首页「隐藏工具」浮窗的 /api/tools/visibility 解耦：后者按当前
        登录用户的权限点过滤（未授权插件全流程不可见）；本接口面向拥有
        人员管理模块权限的管理者，始终返回全量工具清单。
        """
        _deps["set_operation"]("查询权限点清单")
        registry = _deps["load_registry"]()
        tools = [
            {"id": t["id"], "name": t.get("name") or t["id"], "enabled": t.get("enabled", True)}
            for t in registry.get("tools", [])
        ]
        return jsonify({"tools": tools})

    @app.get("/api/admin/permissions")
    @_deps["permission_required"]("user")
    def admin_api_permissions():
        _deps["set_operation"]("查询角色列表")
        cfg = _deps["load_admin_config"]()
        role_usage = {}
        for _unit, _dept, user in _deps["iter_users"](cfg):
            role_usage[user.get("role")] = role_usage.get(user.get("role"), 0) + 1
        permissions = [{
            "id": r["id"],
            "name": r["name"],
            "description": r.get("description", ""),
            "modules": r.get("modules", []),
            "users": role_usage.get(r["id"], 0),
        } for r in cfg.get("permissions", [])]
        return jsonify({"permissions": permissions})

    @app.post("/api/admin/permissions")
    @_deps["permission_required"]("user")
    def admin_api_permissions_create():
        _deps["set_operation"]("新增角色")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "角色名称不能为空"}), 400
        modules = data.get("modules") or []
        if not isinstance(modules, list) or any(m not in _deps["admin_modules"] for m in modules):
            return jsonify({"error": "权限模块不合法"}), 400
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            roles = cfg.setdefault("permissions", [])
            if any(r["name"] == name for r in roles):
                return jsonify({"error": "角色名称已存在"}), 400
            role = {
                "id": "role-" + uuid.uuid4().hex[:8],
                "name": name,
                "description": (data.get("description") or "").strip(),
                "modules": modules,
            }
            roles.append(role)
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True, "permission": role})

    @app.put("/api/admin/permissions/<role_id>")
    @_deps["permission_required"]("user")
    def admin_api_permissions_update(role_id):
        _deps["set_operation"]("修改角色")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "角色名称不能为空"}), 400
        modules = data.get("modules") or []
        if not isinstance(modules, list) or any(m not in _deps["admin_modules"] for m in modules):
            return jsonify({"error": "权限模块不合法"}), 400
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            role = next((r for r in cfg.get("permissions", []) if r["id"] == role_id), None)
            if role is None:
                return jsonify({"error": "角色不存在"}), 404
            if any(r["id"] != role_id and r["name"] == name for r in cfg.get("permissions", [])):
                return jsonify({"error": "角色名称已存在"}), 400
            role["name"] = name
            role["description"] = (data.get("description") or "").strip()
            role["modules"] = modules
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True})

    @app.delete("/api/admin/permissions/<role_id>")
    @_deps["permission_required"]("user")
    def admin_api_permissions_delete(role_id):
        _deps["set_operation"]("删除角色")
        with _deps["admin_cfg_lock"]:                  # D1 全程锁
            cfg = _deps["load_admin_config"]()
            if any(u.get("role") == role_id for _u, _d, u in _deps["iter_users"](cfg)):
                return jsonify({"error": "该角色已分配给人员，请先调整其角色"}), 400
            roles = cfg.get("permissions", [])
            if not any(r["id"] == role_id for r in roles):
                return jsonify({"error": "角色不存在"}), 404
            cfg["permissions"] = [r for r in roles if r["id"] != role_id]
            _deps["save_admin_config"](cfg)
        return jsonify({"ok": True})
