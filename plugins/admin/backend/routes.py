"""管理后台 — JZToolsHub 后端插件路由

「一切皆插件」：登录鉴权、单位/部门/人员/权限管理、工具访问控制等后台能力
全部封装为本插件，由 app.register_plugin_backends() 动态加载并调用 register(app)。

账号存储层级：单位(units) → 部门(departments) → 用户(users)。
用户字段：登录名 username、密码 password、姓名 name、身份证 idcard、角色 role、
权限点 permissions（config/tools.json 插件 ID 列表）、大模型配置 llm{base_url, api_key, model}。

安全：
- 敏感字段（密码、身份证、大模型 API Key）以 Fernet 对称加密存于 config/admin.json；
- 密钥保存在 config/.admin_key（已 gitignore，与密文分离）；
- 已登录的非超级管理员按权限点拦截无权限的工具访问。

数据与密钥：
- config/admin.json ：单位/部门/用户 / 角色 / 会话密钥（已 gitignore）；
- config/.admin_key  ：Fernet 加密密钥（已 gitignore，与密文分离）。
"""

import json
import os
import re
import secrets
import sys
import time
import uuid
from datetime import timedelta
from functools import wraps

from cryptography.fernet import Fernet
from flask import g, jsonify, redirect, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import jztools_data
# 会话超时默认值（config/admin.json 的 session 节可覆盖）
SESSION_IDLE_MINUTES = 30    # 空闲超时：连续这么久没有任何请求，自动登出
SESSION_ABSOLUTE_HOURS = 12  # 绝对有效期：登录满这么久必须重新登录

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(PLUGIN_DIR, "frontend")
PROJECT_DIR = os.path.dirname(os.path.dirname(PLUGIN_DIR))
# 用户数据统一存放于数据根目录（默认 <用户目录>\.jztoolshub，见 jztools_data.py）。
# 升级时整体替换程序文件夹，数据保存在数据根目录中不丢失。
CONFIG_PATH = jztools_data.get_data_root_file("config", "tools.json")
ADMIN_CONFIG_PATH = jztools_data.get_data_root_file("config", "admin.json")
ADMIN_KEY_PATH = jztools_data.get_data_root_file("config", ".admin_key")

# 允许的权限模块（管理后台的四个子模块）
ADMIN_MODULES = ("unit", "department", "user", "permission")
ADMIN_MODULE_NAMES = {
    "unit": "单位管理",
    "department": "部门管理",
    "user": "人员管理",
    "permission": "权限管理",
}

# 管理后台自身的插件 ID（前端资源不受工具权限点拦截，页面由模块权限管控）
ADMIN_PLUGIN_ID = "admin"


# ===================== 配置读写 =====================

def load_registry():
    """读取 tools.json（工具注册清单），作为权限点校验与拦截的权威来源。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_admin_config():
    """确保 config/admin.json 存在，不存在则创建含默认管理员账号的初始文件。

    默认账号 admin / admin123，首次启动自动生成，可登录后在「人员管理」中修改。
    层级：单位(管理单位) → 部门(管理部门) → 用户(admin)。
    """
    if os.path.isfile(ADMIN_CONFIG_PATH):
        return
    os.makedirs(os.path.dirname(ADMIN_CONFIG_PATH), exist_ok=True)
    default = {
        "secret_key": secrets.token_hex(32),
        "session": {"idle_minutes": SESSION_IDLE_MINUTES, "absolute_hours": SESSION_ABSOLUTE_HOURS},
        "units": [
            {
                "id": "unit-management",
                "name": "管理单位",
                "description": "系统管理单位",
                "departments": [
                    {
                        "id": "dept-management",
                        "name": "管理部门",
                        "description": "系统管理后台的运维部门",
                        "users": [
                            {
                                "username": "admin",
                                "password": encrypt_field(generate_password_hash("admin123")),
                                "name": "系统管理员",
                                "idcard": encrypt_field(""),
                                "role": "role-admin",
                                "permissions": sorted(_registered_tool_ids()),
                                "llm": {"base_url": "", "api_key": encrypt_field(""), "model": ""},
                            }
                        ],
                    }
                ],
            }
        ],
        "permissions": [
            {
                "id": "role-admin",
                "name": "管理员",
                "description": "拥有管理后台全部模块的权限",
                "modules": ["unit", "department", "user", "permission"],
            },
            {
                "id": "role-case-handler",
                "name": "办案员",
                "description": "日常办案工具使用者（不含管理后台）",
                "modules": [],
            },
        ],
    }
    with open(ADMIN_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(default, f, ensure_ascii=False, indent=2)


def load_admin_config():
    """读取 config/admin.json（鉴权数据 / 单位 / 部门 / 人员 / 权限）。"""
    ensure_admin_config()
    with open(ADMIN_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_admin_config(cfg):
    """原子写入 config/admin.json。"""
    tmp = ADMIN_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ADMIN_CONFIG_PATH)


def _session_settings(cfg):
    """读取会话超时配置，返回 (idle_minutes, absolute_hours)。"""
    s = cfg.get("session") or {}
    try:
        idle = int(s.get("idle_minutes") or SESSION_IDLE_MINUTES)
    except (TypeError, ValueError):
        idle = SESSION_IDLE_MINUTES
    try:
        absolute = int(s.get("absolute_hours") or SESSION_ABSOLUTE_HOURS)
    except (TypeError, ValueError):
        absolute = SESSION_ABSOLUTE_HOURS
    return max(1, idle), max(1, absolute)


# ===================== 层级遍历 =====================

def iter_users(cfg):
    """扁平遍历所有用户，产出 (unit, dept, user) 元组。"""
    for unit in cfg.get("units", []):
        for dept in unit.get("departments", []):
            for user in dept.get("users", []):
                yield unit, dept, user


def find_user(cfg, username):
    """按登录名查找用户，返回 (unit, dept, user) 或 None。"""
    for unit, dept, user in iter_users(cfg):
        if user.get("username") == username:
            return unit, dept, user
    return None


def find_dept(cfg, dept_id):
    """按部门 ID 查找部门，返回 (unit, dept) 或 None。"""
    for unit in cfg.get("units", []):
        for dept in unit.get("departments", []):
            if dept.get("id") == dept_id:
                return unit, dept
    return None


def find_unit(cfg, unit_id):
    """按单位 ID 查找单位，返回 unit 或 None。"""
    for unit in cfg.get("units", []):
        if unit.get("id") == unit_id:
            return unit
    return None


def count_users(cfg):
    """统计全部用户数。"""
    return sum(1 for _ in iter_users(cfg))


def count_departments(cfg):
    """统计全部部门数。"""
    return sum(len(unit.get("departments", [])) for unit in cfg.get("units", []))


def append_user(cfg, unit_id, dept_id, user):
    """把用户追加到 (unit, dept) 下；单位或部门不存在返回 False。"""
    unit = find_unit(cfg, unit_id)
    if unit is None:
        return False
    for dept in unit.get("departments", []):
        if dept.get("id") == dept_id:
            dept.setdefault("users", []).append(user)
            return True
    return False


# ===================== 加密 =====================

def load_or_create_admin_key():
    """加载（不存在则生成）加密密钥。

    密钥保存在 config/.admin_key（已 gitignore），与密文分离存放，
    保证 config/admin.json 中敏感字段为不可读密文。
    """
    if os.path.isfile(ADMIN_KEY_PATH):
        with open(ADMIN_KEY_PATH, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(ADMIN_KEY_PATH), exist_ok=True)
    with open(ADMIN_KEY_PATH, "wb") as f:
        f.write(key)
    return key


_fernet = None


def get_fernet():
    """懒加载 Fernet 实例（基于 config/.admin_key）。"""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(load_or_create_admin_key())
    return _fernet


def encrypt_field(value):
    """加密敏感字段；空值原样返回空串。"""
    if value is None or value == "":
        return ""
    return get_fernet().encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt_field(token):
    """解密敏感字段；无法解密（如历史明文数据）时原样返回，兼容迁移。"""
    if not token:
        return ""
    try:
        return get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        return token


def migrate_admin_encryption(cfg):
    """把历史明文敏感字段迁移为密文（启动时调用一次）。

    解密失败视为明文：密码 / 身份证 / 大模型 API Key 若仍为明文则加密后落盘，
    保证 config/admin.json 中不再出现可读的敏感数据。
    """
    changed = False
    for _unit, _dept, user in iter_users(cfg):
        for key in ("password", "idcard"):
            val = user.get(key)
            if val and decrypt_field(val) == val:
                user[key] = encrypt_field(val)
                changed = True
        llm = user.get("llm")
        if isinstance(llm, dict):
            api_key = llm.get("api_key")
            if api_key and decrypt_field(api_key) == api_key:
                llm["api_key"] = encrypt_field(api_key)
                changed = True
        else:
            user["llm"] = {"base_url": "", "api_key": encrypt_field(""), "model": ""}
            changed = True
    return changed


def sync_admin_permissions(cfg):
    """把 tools.json 中的全部工具 ID 并集写入 admin 账号的权限点，返回是否变更。

    保证管理员账号拥有全部工具权限，且新增插件后自动补齐。
    """
    changed = False
    all_ids = sorted(_registered_tool_ids())
    for _unit, _dept, user in iter_users(cfg):
        if user.get("username") == "admin":
            existing = set(user.get("permissions", []))
            full = existing | set(all_ids)
            if full != existing:
                user["permissions"] = sorted(full)
                changed = True
    return changed


def migrate_admin_roles(cfg):
    """确保内置管理员角色（role-admin）拥有全部管理模块（含新模块 unit）。

    幂等：每次启动执行，保证升级后管理员角色仍是超级管理员（可访问全部工具）。
    """
    changed = False
    for role in cfg.get("permissions", []):
        if role.get("id") == "role-admin":
            modules = set(role.get("modules", []))
            if modules != set(ADMIN_MODULES):
                role["modules"] = sorted(modules | set(ADMIN_MODULES))
                changed = True
    return changed


def migrate_admin_case_handler_role(cfg):
    """确保内置「办案员」角色（role-case-handler）存在，供人员管理选择。

    幂等：每次启动执行，缺失才补齐。办案员无管理模块（modules=[]），
    可被管理员分配工具权限点（不含管理后台），不属于超级管理员。
    """
    changed = False
    roles = cfg.setdefault("permissions", [])
    if not any(r.get("id") == "role-case-handler" for r in roles):
        roles.append({
            "id": "role-case-handler",
            "name": "办案员",
            "description": "日常办案工具使用者（不含管理后台）",
            "modules": [],
        })
        changed = True
    return changed


def _role_super_admin(cfg, role_id):
    """判断角色是否为超级管理员（拥有全部管理模块）。"""
    role = next((r for r in cfg.get("permissions", []) if r.get("id") == role_id), None)
    modules = (role or {}).get("modules", [])
    return set(modules) == set(ADMIN_MODULES) and bool(modules)


def migrate_admin_hierarchy(cfg):
    """把旧平铺结构（顶层 departments / users）迁移为 单位→部门→用户 层级。

    返回是否发生变更；仅当 config 中尚无 units 字段时执行。
    """
    if cfg.get("units") is not None:
        return False
    depts = cfg.pop("departments", []) or []
    users = cfg.pop("users", []) or []
    if not depts:
        depts = [{"id": "dept-management", "name": "管理部门", "description": ""}]
    default_unit = {
        "id": "unit-management",
        "name": "管理单位",
        "description": "由历史数据迁移生成的默认单位",
        "departments": [],
    }
    for dept in depts:
        dept.pop("users", None)
        dept["users"] = [u for u in users if u.get("department") == dept["id"]]
        for u in dept["users"]:
            u.pop("department", None)
        default_unit["departments"].append(dept)
    # 未匹配到部门的用户归入第一个部门
    assigned = {u.get("username") for d in default_unit["departments"] for u in d.get("users", [])}
    leftover = [u for u in users if u.get("username") not in assigned]
    if leftover and default_unit["departments"]:
        for u in leftover:
            u.pop("department", None)
        default_unit["departments"][0]["users"].extend(leftover)
    cfg["units"] = [default_unit]
    # 旧版全模块角色（department/user/permission）补齐新模块 unit，保持超级管理员语义
    old_full = {"department", "user", "permission"}
    for role in cfg.get("permissions", []):
        if set(role.get("modules", [])) == old_full:
            role["modules"] = sorted(old_full | {"unit"})
    return True


# ===================== 会话与鉴权 =====================

def set_operation(op):
    """标记当前请求的「具体操作」标签（写入请求上下文 g）。

    访问日志（app.py _log_response）在 after_request 时读取该标签，
    使日志能记录诸如「新增单位 / 发布公告 / 修改密码」等具体操作；
    未标记的请求回退到基于路径/方法的兜底描述。供各插件与本插件
    路由处理器调用，用法：set_operation("新增单位")。
    """
    try:
        g._current_operation = op
    except Exception:
        pass


def get_session_user():
    """返回当前登录用户的展示信息，未登录返回 None。

    含权限点（可用工具 ID 列表）：拥有全部管理模块的角色视为超级管理员，
    默认授予全部工具；其余账号取自身 permissions 字段。
    """
    username = session.get("user")
    if not username:
        return None
    cfg = load_admin_config()
    found = find_user(cfg, username)
    if found is None:
        session.clear()
        return None
    unit, dept, user = found
    role = next((r for r in cfg.get("permissions", []) if r["id"] == user.get("role")), None)
    modules = (role or {}).get("modules", [])
    super_admin = _role_super_admin(cfg, user.get("role"))
    if super_admin:
        permissions = sorted(_registered_tool_ids())
    else:
        # 逐人授权权限点 + 全站默认开放工具（tools.json 中 grant_all: true）
        perms = list(user.get("permissions", []))
        perms.extend(tid for tid in _grant_all_tool_ids() if tid not in perms)
        permissions = sorted(perms)
    return {
        "username": user["username"],
        "name": user.get("name") or user["username"],
        "role": (role or {}).get("name", ""),
        "role_id": user.get("role", ""),
        "unit": unit.get("name", ""),
        "unit_id": unit.get("id", ""),
        "department": dept.get("name", ""),
        "department_id": dept.get("id", ""),
        "modules": modules,
        "super_admin": super_admin,
        "permissions": permissions,
    }


def _registered_tool_ids():
    """返回 tools.json 中注册的全部工具 ID（含禁用），用于权限点校验与接口拦截。"""
    try:
        return {t["id"] for t in load_registry().get("tools", [])}
    except Exception:
        return set()


def _grant_all_tool_ids():
    """返回声明了 grant_all: true 的工具 ID：对全体登录用户默认开放（无需逐人授权）。

    在 tools.json 对应工具条目上配置 "grant_all": true 即生效，
    典型场景：公告板这类全站基础设施型业务插件。
    """
    try:
        return {t["id"] for t in load_registry().get("tools", []) if t.get("grant_all")}
    except Exception:
        return set()


def _normalize_permission_points(value):
    """规范化权限点（tools.json 中的插件 ID 列表），返回 (列表, 错误或 None)。"""
    if value is None:
        return [], None
    if not isinstance(value, list):
        return None, "权限点格式不正确"
    ids = [str(p).strip() for p in value if str(p).strip()]
    known = _registered_tool_ids()
    invalid = [p for p in ids if p not in known]
    if invalid:
        return None, f"权限点必须是 tools.json 中的插件 ID：{', '.join(invalid[:5])}"
    return ids, None


def login_required(f):
    """页面 / 接口登录保护：未登录则 JSON 返回 401，页面重定向到 /login。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if get_session_user() is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录或登录已过期"}), 401
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def permission_required(module):
    """模块权限保护：仅允许拥有指定模块权限的角色访问。"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            info = get_session_user()
            if info is None:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "未登录或登录已过期"}), 401
                return redirect(url_for("admin_login", next=request.path))
            if module not in info["modules"]:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "无此模块的操作权限"}), 403
                return redirect(url_for("admin_index"))
            return f(*args, **kwargs)
        return wrapper
    return decorator


def make_session_guard(idle_minutes, absolute_hours):
    """生成会话守卫：空闲超时 / 绝对超时则强制登出，否则滑动续期。

    - 空闲超时：连续 idle_minutes 分钟没有任何请求（含轮询），自动登出；
    - 绝对有效期：登录满 absolute_hours 小时必须重新登录（防止轮询永续）。
    - 请求本身算"活动"，活跃用户的空闲计时会被滑动归零。
    """
    def guard():
        if not session.get("user"):
            return None
        now = time.time()
        expired = (
            now - session.get("last_active", 0) > idle_minutes * 60
            or now - session.get("login_at", now) > absolute_hours * 3600
        )
        if expired:
            session.clear()
            return None
        session["last_active"] = now   # 滑动续期：活跃用户的空闲计时归零
        return None
    return guard


# R2 强制登录白名单：只放行登录闭环自身必需的路径与无业务数据的静态资源
PUBLIC_PATHS = ("/login", "/api/login", "/api/logout", "/api/session", "/favicon.ico")
PUBLIC_PREFIXES = ("/static/", "/plugin/admin/css/", "/plugin/admin/js/")


def _enforce_login():
    """R2 强制登录：白名单之外的路径一律要求已登录。

    - API 请求返回 401 JSON（前端 AdminCommon.api 会自动跳登录页）；
    - 页面请求 302 到 /login 并携带 next 参数，登录后原路返回。
    """
    path = request.path
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return None
    if get_session_user() is None:
        if path.startswith("/api/"):
            return jsonify({"error": "未登录或登录已过期"}), 401
        return redirect(url_for("admin_login", next=request.full_path))
    return None


def _enforce_tool_access():
    """对已登录的非超级管理员账号拦截无权限的工具访问。

    - /tool/<id>、/plugin/<id>/...：直接校验工具 ID（admin 插件自身资源除外）；
    - /api/<插件id>/...：插件后端接口按首段插件 ID 校验（不影响 /api/tools 等平台接口）；
    - 未登录（匿名）访问不拦截，保持原有体验。
    """
    path = request.path
    if not (path.startswith("/tool/") or path.startswith("/plugin/") or path.startswith("/api/")):
        return None
    info = get_session_user()
    if info is None or info.get("super_admin"):
        return None
    allowed = set(info.get("permissions") or [])
    tool_id = None
    if path.startswith("/tool/"):
        tool_id = path[len("/tool/"):].strip("/")
    elif path.startswith("/plugin/"):
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            tool_id = parts[1]
    elif path.startswith("/api/"):
        first = path[len("/api/"):].split("/")[0]
        if first in _registered_tool_ids():
            tool_id = first
    # 管理后台自身资源（登录页 / 后台页面资源）不按工具权限点拦截
    if tool_id == ADMIN_PLUGIN_ID:
        return None
    if tool_id and tool_id not in allowed:
        if path.startswith("/api/"):
            return jsonify({"error": "无该工具的使用权限"}), 403
        return ("<div style='font-family:sans-serif;text-align:center;padding:80px 20px;'>"
                "<h2>403</h2><p>无该工具的使用权限</p>"
                "<p><a href='/' style='color:#4285F4;'>返回工具箱</a></p></div>"), 403
    return None


def _protect_admin_ops():
    """保护首页布局写操作：编辑位置 / 隐藏工具需登录。"""
    path = request.path
    if path == "/api/tools/reorder":
        if get_session_user() is None:
            return jsonify({"error": "未登录或登录已过期"}), 401
    if path == "/api/tools/visibility" and request.method == "POST":
        if get_session_user() is None:
            return jsonify({"error": "未登录或登录已过期"}), 401
    return None


# ===================== 批量导入导出（同包子模块） =====================

def _load_batch_io():
    """载入同包 batch_io 子模块（单位/部门/人员 批量导入导出）。

    优先按包内相对导入加载（app.py 以 jztools_admin 包名动态加载本文件）；
    极端情况下（模块被单独按路径执行）回退为按文件路径加载。
    """
    try:
        from . import batch_io  # noqa: WPS433 - 包内相对导入为常规路径
        return batch_io
    except Exception:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "batch_io.py")
        spec = importlib.util.spec_from_file_location("jztools_admin_batch_io", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _register_batch_io(app):
    """挂载批量导入导出路由，注入本模块的内部依赖（避免循环导入）。"""
    batch_io = _load_batch_io()
    batch_io.register(app, {
        "load_admin_config": load_admin_config,
        "save_admin_config": save_admin_config,
        "load_registry": load_registry,
        "find_unit": find_unit,
        "find_dept": find_dept,
        "find_user": find_user,
        "iter_users": iter_users,
        "encrypt_field": encrypt_field,
        "decrypt_field": decrypt_field,
        "role_super_admin": _role_super_admin,
        "registered_tool_ids": _registered_tool_ids,
        "get_session_user": get_session_user,
        "set_operation": set_operation,
    })


def _load_plugin_admin():
    """载入同包 plugin_admin 子模块（插件包校验 / 应用 / 回滚 / 索引，纯逻辑）。

    优先按包内相对导入加载；极端情况下（模块被单独按路径执行）回退为按文件路径加载。
    """
    try:
        from . import plugin_admin  # noqa: WPS433 - 包内相对导入为常规路径
        return plugin_admin
    except Exception:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin_admin.py")
        spec = importlib.util.spec_from_file_location("jztools_admin_plugin_admin", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _spawn_self_restart():
    """冻结模式自重启：spawn 独立 cmd，等本进程退出后按原工作目录重新拉起 exe。

    见 docs/插件独立升级方案-设计文档.md §9.3。返回 (ok, error)。
    要点：① 先回 HTTP 响应再退出（调用方在本函数内用 threading.Timer 延迟 os._exit）；
         ② 助手轮询本进程 PID 而不是固定延时（避免端口未释放导致启动即失败）；
         ③ `cd /d <程序目录>` 必须带（与托盘/快捷方式同款工作目录约定）。
    """
    import subprocess
    import tempfile
    import threading

    exe = sys.executable
    base = PROJECT_DIR
    pid = os.getpid()
    helper = os.path.join(tempfile.gettempdir(), "jz-restart-%d.cmd" % pid)
    try:
        # 助手内容全是 ASCII，而路径可能含中文 → 用系统默认 ANSI（GBK）编码写盘
        with open(helper, "w", encoding="gbk", errors="replace") as f:
            f.write("@echo off\r\n")
            f.write(":wait\r\n")
            f.write('tasklist /FI "PID eq %d" | find "%d" >nul && '
                    '(timeout /t 1 /nobreak >nul & goto wait)\r\n' % (pid, pid))
            f.write('cd /d "%s"\r\n' % base)
            f.write('start "" /min "%s"\r\n' % exe)
            f.write('del "%%~f0"\r\n')
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            flags |= getattr(subprocess, name, 0)
        subprocess.Popen(["cmd", "/c", helper], creationflags=flags, close_fds=True, cwd=base)
    except Exception as e:  # pragma: no cover - 平台/权限异常
        return False, "启动重启助手失败：%s" % e

    threading.Timer(0.6, lambda: os._exit(0)).start()
    return True, ""


def _maybe_auto_restart(report, needed=None, auto=True):
    """应用 / 回滚成功后如需重启：打包模式自动「停服 → 重新拉起」，源码模式只给提示。

    就地给 report 补两个字段供前端决定行为：
      restarting       —— 本进程即将退出并自动拉起（前端应等待并自动刷新）
      restart_message  —— 给管理员看的中文说明
    needed 缺省取 report["restart_needed"]；回滚没有该字段，由调用方显式传 True。

    注意：打包模式下本函数会安排 0.6 秒后 os._exit(0)，调用方必须在本函数之后
    立刻 return 响应（不能再做耗时操作），否则响应发不出去。
    """
    if needed is None:
        needed = bool(report.get("restart_needed"))
    report["restart_needed"] = needed
    if not needed:
        return report
    if not report.get("ok"):
        report["restarting"] = False
        report["restart_message"] = "本次操作未完全成功，已跳过自动重启；请处理后手工重启"
        return report
    if not auto:
        report["restarting"] = False
        report["restart_message"] = "已按设置跳过自动重启，可点上方「立即重启服务」手工重启"
        return report
    if not getattr(sys, "frozen", False):
        report["restarting"] = False
        report["restart_message"] = ("源码开发模式不自动重启：Flask 调试重载通常会自行重启；"
                                     "若未生效请手工重启 python app.py")
        return report
    ok, err = _spawn_self_restart()
    report["restarting"] = bool(ok)
    report["restart_message"] = ("服务正在重启，约 5~10 秒后本页会自动重新加载" if ok
                                 else "自动重启失败：%s（可点上方「立即重启服务」重试）" % err)
    return report


# ===================== 路由注册 =====================

def register(app):
    """挂载管理后台：会话密钥、鉴权、后台页面与接口。由 register_plugin_backends 调用。"""

    # 会话密钥：首次启动生成并持久化于 config/admin.json，重启后会话保持有效
    ensure_admin_config()
    cfg = load_admin_config()
    if not cfg.get("secret_key") or cfg.get("secret_key") == "replace-on-first-startup":
        cfg["secret_key"] = secrets.token_hex(32)
        save_admin_config(cfg)
    # 会话配置：旧文件缺失 session 节时自动补齐（读取处也有默认值兜底）
    if not isinstance(cfg.get("session"), dict):
        cfg["session"] = {"idle_minutes": SESSION_IDLE_MINUTES, "absolute_hours": SESSION_ABSOLUTE_HOURS}
        save_admin_config(cfg)
    if migrate_admin_hierarchy(cfg):
        save_admin_config(cfg)
    if migrate_admin_roles(cfg):
        save_admin_config(cfg)
    if migrate_admin_case_handler_role(cfg):
        save_admin_config(cfg)
    if migrate_admin_encryption(cfg):
        save_admin_config(cfg)
    if sync_admin_permissions(cfg):
        save_admin_config(cfg)
    app.config["SECRET_KEY"] = cfg["secret_key"]
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # 会话超时：读取 session 节配置（缺省 30 分钟空闲 / 12 小时绝对）
    idle_minutes, absolute_hours = _session_settings(cfg)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=absolute_hours)

    # 全局拦截（注册顺序即执行顺序，必须先超时守卫、再登录拦截）：
    #   1. make_session_guard    空闲/绝对超时登出 + 滑动续期（R1）
    #   2. _enforce_login        白名单外一律要求登录（R2）
    #   3. _protect_admin_ops    首页布局写操作需登录（保留，语义已被 2 覆盖）
    #   4. _enforce_tool_access  非超管按权限点拦工具（保持不变）
    app.before_request(make_session_guard(idle_minutes, absolute_hours))
    app.before_request(_enforce_login)
    app.before_request(_protect_admin_ops)
    app.before_request(_enforce_tool_access)

    # ---------------- 登录 / 会话 ----------------

    @app.get("/login")
    def admin_login():
        """登录页。"""
        return send_from_directory(FRONTEND_DIR, "login.html")

    @app.post("/api/login")
    def admin_api_login():
        """登录校验：成功写入 session。"""
        set_operation("登录系统")
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            return jsonify({"error": "请输入用户名和密码"}), 400

        cfg = load_admin_config()
        found = find_user(cfg, username)
        if found is None or not check_password_hash(decrypt_field(found[2].get("password")), password):
            return jsonify({"error": "用户名或密码错误"}), 401

        now = time.time()
        session.clear()               # 先清空再写入，防会话固定攻击
        session.permanent = True
        session["user"] = found[2]["username"]
        session["login_at"] = now     # 绝对有效期基准
        session["last_active"] = now  # 空闲超时基准
        return jsonify({"ok": True, "user": get_session_user()})

    @app.post("/api/logout")
    def admin_api_logout():
        """退出登录：清空 session。"""
        set_operation("退出登录")
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/session")
    def admin_api_session():
        """返回当前登录状态，供前端渲染登录 / 用户菜单。"""
        set_operation("查询登录状态")
        return jsonify({"user": get_session_user()})

    @app.post("/api/account/password")
    @login_required
    def admin_account_password():
        """自助修改密码：校验旧密码 → 新密码 ≥6 位且不得与旧密码相同。

        修改后保持当前会话有效（session 只存 username，不存密码指纹），不强制重登。
        """
        set_operation("修改密码")
        data = request.get_json(silent=True) or {}
        old_pwd = data.get("old_password") or ""
        new_pwd = data.get("new_password") or ""
        if len(new_pwd) < 6:
            return jsonify({"error": "新密码长度不能少于 6 位"}), 400
        cfg = load_admin_config()
        info = get_session_user()
        found = find_user(cfg, info["username"])
        if found is None:
            return jsonify({"error": "账号不存在"}), 404
        _unit, _dept, user = found
        if not check_password_hash(decrypt_field(user.get("password")), old_pwd):
            return jsonify({"error": "原密码错误"}), 400
        if old_pwd == new_pwd:
            return jsonify({"error": "新密码不能与原密码相同"}), 400
        user["password"] = encrypt_field(generate_password_hash(new_pwd))
        save_admin_config(cfg)
        return jsonify({"ok": True})

    # ---------------- 管理后台页面 ----------------

    @app.get("/admin")
    @login_required
    def admin_index():
        return send_from_directory(FRONTEND_DIR, "admin.html")

    @app.get("/admin/<module>")
    @login_required
    def admin_module_page(module):
        if module not in ADMIN_MODULES:
            return jsonify({"error": "unknown admin module"}), 404
        if module == "permission":
            # 权限管理暂时屏蔽：已并入「人员管理」模块
            return jsonify({"error": "权限管理已并入「人员管理」模块，暂不单独开放"}), 404
        return send_from_directory(FRONTEND_DIR, f"admin-{module}.html")

    # ---------------- 组织架构树（只读，供业务插件选择单位/部门） ----------------

    @app.get("/api/admin/org-tree")
    @login_required
    def admin_org_tree():
        """返回 单位→部门→用户 树（只读，仅标识与姓名），仅需登录。

        供公告板等插件选择可见范围；不含密码/身份证等敏感字段。
        """
        set_operation("查询组织架构树")
        cfg = load_admin_config()
        tree = []
        for u in cfg.get("units", []):
            tree.append({
                "id": u.get("id", ""),
                "name": u.get("name", ""),
                "type": "unit",
                "children": [
                    {
                        "id": d.get("id", ""),
                        "name": d.get("name", ""),
                        "type": "department",
                        "children": [
                            {"id": x.get("username", ""),
                             "name": x.get("name") or x.get("username", ""),
                             "type": "user"}
                            for x in d.get("users", [])
                        ],
                    }
                    for d in u.get("departments", [])
                ],
            })
        return jsonify({"ok": True, "tree": tree})

    # ---------------- 后台总览 ----------------

    @app.get("/api/admin/summary")
    @login_required
    def admin_api_summary():
        set_operation("查询后台总览")
        cfg = load_admin_config()
        info = get_session_user()
        counts = {
            "unit": len(cfg.get("units", [])),
            "department": count_departments(cfg),
            "user": count_users(cfg),
            "permission": len(cfg.get("permissions", [])),
        }
        # 权限管理已并入「人员管理」，后台首页不再展示独立模块卡片（暂时屏蔽）
        visible_modules = [m for m in ADMIN_MODULES if m != "permission"]
        modules = [
            {
                "id": m,
                "name": ADMIN_MODULE_NAMES[m],
                "count": counts[m],
                "allowed": m in info["modules"],
            }
            for m in visible_modules
        ]
        # 系统设置：数据目录管理等敏感配置，仅超级管理员可见、可入
        modules.append({
            "id": "settings",
            "name": "系统设置",
            "count": 0,
            "allowed": bool(info.get("super_admin")),
        })
        # 插件管理：插件包升级 / 回滚 / 批量更新（阶段二/三），同样仅超级管理员
        modules.append({
            "id": "plugins",
            "name": "插件管理",
            "count": 0,
            "allowed": bool(info.get("super_admin")),
        })
        return jsonify({
            "modules": modules,
            "user": info,
        })

    # ---------------- 单位管理 ----------------

    @app.get("/api/admin/units")
    @permission_required("unit")
    def admin_api_units():
        set_operation("查询单位列表")
        cfg = load_admin_config()
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
    @permission_required("unit")
    def admin_api_units_create():
        set_operation("新增单位")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "单位名称不能为空"}), 400
        cfg = load_admin_config()
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
        save_admin_config(cfg)
        return jsonify({"ok": True, "unit": unit})

    @app.put("/api/admin/units/<unit_id>")
    @permission_required("unit")
    def admin_api_units_update(unit_id):
        set_operation("修改单位")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "单位名称不能为空"}), 400
        cfg = load_admin_config()
        unit = find_unit(cfg, unit_id)
        if unit is None:
            return jsonify({"error": "单位不存在"}), 404
        if any(u["id"] != unit_id and u["name"] == name for u in cfg.get("units", [])):
            return jsonify({"error": "单位名称已存在"}), 400
        unit["name"] = name
        unit["description"] = (data.get("description") or "").strip()
        save_admin_config(cfg)
        return jsonify({"ok": True})

    @app.delete("/api/admin/units/<unit_id>")
    @permission_required("unit")
    def admin_api_units_delete(unit_id):
        set_operation("删除单位")
        cfg = load_admin_config()
        unit = find_unit(cfg, unit_id)
        if unit is None:
            return jsonify({"error": "单位不存在"}), 404
        if unit.get("departments"):
            return jsonify({"error": "该单位下仍有部门，请先调整或删除部门"}), 400
        cfg["units"] = [u for u in cfg.get("units", []) if u["id"] != unit_id]
        save_admin_config(cfg)
        return jsonify({"ok": True})

    # ---------------- 部门管理 ----------------

    @app.get("/api/admin/departments")
    @permission_required("department")
    def admin_api_departments():
        set_operation("查询部门列表")
        cfg = load_admin_config()
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
    @permission_required("department")
    def admin_api_departments_create():
        set_operation("新增部门")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        unit_id = (data.get("unit_id") or "").strip()
        if not name:
            return jsonify({"error": "部门名称不能为空"}), 400
        if not unit_id:
            return jsonify({"error": "请选择所属单位"}), 400
        cfg = load_admin_config()
        unit = find_unit(cfg, unit_id)
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
        save_admin_config(cfg)
        return jsonify({"ok": True, "department": dept})

    @app.put("/api/admin/departments/<dept_id>")
    @permission_required("department")
    def admin_api_departments_update(dept_id):
        set_operation("修改部门")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "部门名称不能为空"}), 400
        cfg = load_admin_config()
        found = find_dept(cfg, dept_id)
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
            target = find_unit(cfg, new_unit_id)
            if target is None:
                return jsonify({"error": "目标单位不存在"}), 404
            if any(d["id"] != dept_id and d["name"] == name for d in target.get("departments", [])):
                return jsonify({"error": "目标单位下已存在同名部门"}), 400
            old_unit["departments"] = [d for d in old_unit.get("departments", []) if d["id"] != dept_id]
            target.setdefault("departments", []).append(dept)
        save_admin_config(cfg)
        return jsonify({"ok": True})

    @app.delete("/api/admin/departments/<dept_id>")
    @permission_required("department")
    def admin_api_departments_delete(dept_id):
        set_operation("删除部门")
        cfg = load_admin_config()
        found = find_dept(cfg, dept_id)
        if found is None:
            return jsonify({"error": "部门不存在"}), 404
        unit, dept = found
        if dept.get("users"):
            return jsonify({"error": "该部门下仍有人员，请先调整其所属部门"}), 400
        unit["departments"] = [d for d in unit.get("departments", []) if d["id"] != dept_id]
        save_admin_config(cfg)
        return jsonify({"ok": True})

    # ---------------- 人员管理 ----------------

    @app.get("/api/admin/users")
    @permission_required("user")
    def admin_api_users():
        set_operation("查询人员列表")
        cfg = load_admin_config()
        role_map = {r["id"]: r["name"] for r in cfg.get("permissions", [])}
        users = []
        for unit, dept, user in iter_users(cfg):
            users.append({
                "username": user["username"],
                "name": user.get("name") or user["username"],
                "unit_id": unit["id"],
                "unit_name": unit["name"],
                "department_id": dept["id"],
                "department_name": dept["name"],
                "role": user.get("role", ""),
                "role_name": role_map.get(user.get("role", ""), ""),
                "super_admin": _role_super_admin(cfg, user.get("role")),
                "idcard": decrypt_field(user.get("idcard")),
                "permissions": user.get("permissions", []),
                "llm": {
                    "base_url": (user.get("llm") or {}).get("base_url", ""),
                    "api_key": decrypt_field((user.get("llm") or {}).get("api_key")),
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
    @permission_required("user")
    def admin_api_users_create():
        set_operation("新增人员")
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
        cfg = load_admin_config()
        if find_user(cfg, username) is not None:
            return jsonify({"error": "该登录名已存在"}), 400
        llm = data.get("llm") or {}
        if not isinstance(llm, dict):
            llm = {}
        role_id = (data.get("role") or "").strip()
        if "permissions" in data:
            permissions, perr = _normalize_permission_points(data.get("permissions"))
            if perr:
                return jsonify({"error": perr}), 400
        else:
            # 未显式指定权限点时：管理员（超管）不依赖权限点；普通角色（办案员等）
            # 默认授予全部工具（不含管理后台），管理员可在「权限」弹窗中再调整。
            if _role_super_admin(cfg, role_id):
                permissions = []
            else:
                permissions = sorted(_registered_tool_ids() - {"admin"})
        user = {
            "username": username,
            "password": encrypt_field(generate_password_hash(password)),
            "name": name,
            "idcard": encrypt_field(data.get("idcard") or ""),
            "role": role_id,
            "permissions": permissions,
            "llm": {
                "base_url": (llm.get("base_url") or "").strip(),
                "api_key": encrypt_field(llm.get("api_key") or ""),
                "model": (llm.get("model") or "").strip(),
            },
        }
        if not append_user(cfg, unit_id, dept_id, user):
            return jsonify({"error": "所属单位或部门不存在"}), 404
        save_admin_config(cfg)
        return jsonify({"ok": True})

    @app.put("/api/admin/users/<username>")
    @permission_required("user")
    def admin_api_users_update(username):
        set_operation("修改人员")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "姓名不能为空"}), 400
        cfg = load_admin_config()
        found = find_user(cfg, username)
        if found is None:
            return jsonify({"error": "人员不存在"}), 404
        unit, dept, user = found
        user["name"] = name
        user["role"] = (data.get("role") or "").strip()
        password = data.get("password") or ""
        if password:
            if len(password) < 6:
                return jsonify({"error": "密码长度不能少于 6 位"}), 400
            user["password"] = encrypt_field(generate_password_hash(password))
        # 身份证：传空字符串则清空，不传则保留原值
        if "idcard" in data:
            user["idcard"] = encrypt_field(data.get("idcard") or "")
        # 权限点（tools.json 中的插件 ID）
        if "permissions" in data:
            permissions, perr = _normalize_permission_points(data.get("permissions"))
            if perr:
                return jsonify({"error": perr}), 400
            user["permissions"] = permissions
        # 大模型配置
        llm = data.get("llm")
        if isinstance(llm, dict):
            u_llm = user.setdefault("llm", {})
            if "base_url" in llm:
                u_llm["base_url"] = (llm.get("base_url") or "").strip()
            if "api_key" in llm:
                u_llm["api_key"] = encrypt_field(llm.get("api_key") or "")
            if "model" in llm:
                u_llm["model"] = (llm.get("model") or "").strip()
        # 调整所属单位 / 部门（移动用户）
        new_unit_id = (data.get("unit_id") or "").strip()
        new_dept_id = (data.get("department_id") or "").strip()
        if new_unit_id and new_dept_id and (new_unit_id != unit["id"] or new_dept_id != dept["id"]):
            target = find_dept(cfg, new_dept_id)
            if target is None or target[0]["id"] != new_unit_id:
                return jsonify({"error": "目标单位或部门不存在"}), 404
            dept["users"] = [u for u in dept.get("users", []) if u.get("username") != username]
            target[1].setdefault("users", []).append(user)
        save_admin_config(cfg)
        return jsonify({"ok": True})

    @app.delete("/api/admin/users/<username>")
    @permission_required("user")
    def admin_api_users_delete(username):
        set_operation("删除人员")
        cfg = load_admin_config()
        if username == get_session_user()["username"]:
            return jsonify({"error": "不能删除当前登录账号"}), 400
        found = find_user(cfg, username)
        if found is None:
            return jsonify({"error": "人员不存在"}), 404
        _unit, dept, _user = found
        dept["users"] = [u for u in dept.get("users", []) if u.get("username") != username]
        save_admin_config(cfg)
        return jsonify({"ok": True})

    # ---------------- 权限（角色）管理（已并入人员管理，gate 用 user 模块） ----------------

    @app.get("/api/admin/permission-points")
    @permission_required("user")
    def admin_api_permission_points():
        """返回全部已注册工具（含停用），供人员「权限设置」弹窗勾选权限点。

        与首页「隐藏工具」浮窗的 /api/tools/visibility 解耦：后者按当前
        登录用户的权限点过滤（未授权插件全流程不可见）；本接口面向拥有
        人员管理模块权限的管理者，始终返回全量工具清单。
        """
        set_operation("查询权限点清单")
        registry = load_registry()
        tools = [
            {"id": t["id"], "name": t.get("name") or t["id"], "enabled": t.get("enabled", True)}
            for t in registry.get("tools", [])
        ]
        return jsonify({"tools": tools})

    @app.get("/api/admin/permissions")
    @permission_required("user")
    def admin_api_permissions():
        set_operation("查询角色列表")
        cfg = load_admin_config()
        role_usage = {}
        for _unit, _dept, user in iter_users(cfg):
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
    @permission_required("user")
    def admin_api_permissions_create():
        set_operation("新增角色")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "角色名称不能为空"}), 400
        modules = data.get("modules") or []
        if not isinstance(modules, list) or any(m not in ADMIN_MODULES for m in modules):
            return jsonify({"error": "权限模块不合法"}), 400
        cfg = load_admin_config()
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
        save_admin_config(cfg)
        return jsonify({"ok": True, "permission": role})

    @app.put("/api/admin/permissions/<role_id>")
    @permission_required("user")
    def admin_api_permissions_update(role_id):
        set_operation("修改角色")
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "角色名称不能为空"}), 400
        modules = data.get("modules") or []
        if not isinstance(modules, list) or any(m not in ADMIN_MODULES for m in modules):
            return jsonify({"error": "权限模块不合法"}), 400
        cfg = load_admin_config()
        role = next((r for r in cfg.get("permissions", []) if r["id"] == role_id), None)
        if role is None:
            return jsonify({"error": "角色不存在"}), 404
        if any(r["id"] != role_id and r["name"] == name for r in cfg.get("permissions", [])):
            return jsonify({"error": "角色名称已存在"}), 400
        role["name"] = name
        role["description"] = (data.get("description") or "").strip()
        role["modules"] = modules
        save_admin_config(cfg)
        return jsonify({"ok": True})

    @app.delete("/api/admin/permissions/<role_id>")
    @permission_required("user")
    def admin_api_permissions_delete(role_id):
        set_operation("删除角色")
        cfg = load_admin_config()
        if any(u.get("role") == role_id for _u, _d, u in iter_users(cfg)):
            return jsonify({"error": "该角色已分配给人员，请先调整其角色"}), 400
        roles = cfg.get("permissions", [])
        if not any(r["id"] == role_id for r in roles):
            return jsonify({"error": "角色不存在"}), 404
        cfg["permissions"] = [r for r in roles if r["id"] != role_id]
        save_admin_config(cfg)
        return jsonify({"ok": True})

    # ---------------- 系统数据目录管理（仅超级管理员） ----------------

    @app.get("/admin/settings")
    @login_required
    def admin_settings_page():
        """数据目录设置页（仅超级管理员）。"""
        info = get_session_user()
        if not info or not info.get("super_admin"):
            return redirect(url_for("admin_index"))
        return send_from_directory(FRONTEND_DIR, "admin-settings.html")

    @app.get("/api/admin/data-settings")
    @login_required
    def admin_api_data_settings():
        """返回当前数据根目录与占用摘要，供数据目录设置页展示。"""
        info = get_session_user()
        if not info or not info.get("super_admin"):
            return jsonify({"error": "仅超级管理员可查看数据目录设置"}), 403
        set_operation("查询数据目录设置")
        try:
            summary = jztools_data.data_usage_summary()
        except Exception:
            summary = {"root": jztools_data.get_data_root(), "subdirs": [], "total_bytes": 0}
        return jsonify({
            "ok": True,
            "data_root": summary["root"],
            "default_data_root": jztools_data.default_data_root(),
            "subdirs": summary["subdirs"],
            "total_bytes": summary["total_bytes"],
        })

    @app.post("/api/admin/data-settings")
    @login_required
    def admin_api_data_settings_save():
        """修改数据根目录：把旧目录数据整体迁移到新目录后持久化指针。

        请求体：{ "data_root": "绝对路径", "migrate": true }
        迁移采用「目标不存在则移动、已存在则跳过」策略，不会覆盖新目录已有数据。
        """
        info = get_session_user()
        if not info or not info.get("super_admin"):
            return jsonify({"error": "仅超级管理员可修改数据目录"}), 403
        set_operation("修改数据目录")
        data = request.get_json(silent=True) or {}
        new_root = (data.get("data_root") or "").strip()
        migrate = bool(data.get("migrate", True))
        if not new_root:
            return jsonify({"error": "请填写数据保存目录"}), 400
        new_root = os.path.abspath(os.path.expanduser(new_root))
        if not os.path.isabs(new_root):
            return jsonify({"error": "数据保存目录必须是绝对路径"}), 400
        try:
            root, moved, err = jztools_data.set_data_root(new_root, migrate=migrate)
        except Exception as e:
            return jsonify({"error": f"设置数据目录失败：{e}"}), 500
        if err:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True, "data_root": root, "migrated": moved})

    # ---------------- 插件管理（插件包：上传 / 应用 / 回滚 / 批量升级，仅超级管理员） ----------------
    # 设计：docs/插件独立升级方案-设计文档.md §9（阶段二：应用内升级）与 §10（阶段三：共享盘索引）
    # 安全（PU-1~PU-6）：全部仅限超级管理员；上传包先落到数据根 .staging\uploads\（不落程序目录），
    #   校验通过才允许应用；apply 时服务端**重新校验一遍**（不信任前端传来的任何状态）；
    #   逐条规则与目标机离线安装器 tools/plugin-upgrade/install-plugin.ps1 一致。

    plugin_admin = _load_plugin_admin()

    def _plugin_mgr_ok():
        info = get_session_user()
        return bool(info and info.get("super_admin"))

    def _plugin_display_name(pid, extra_entry=None):
        """插件中文展示名：数据根 tools.json（权威，规范 M-2）→ 包内/注册条目 → manifest.name → id。

        后台页面与所有接口统一走这里，避免出现"表格是中文、计划预览是英文 id"的不一致。
        """
        if not pid:
            return ""
        try:
            reg = load_registry()
        except Exception:
            reg = {}
        names = plugin_admin.plugin_names(reg if isinstance(reg, dict) else {})
        try:
            manifest = plugin_admin.read_json(
                os.path.join(PROJECT_DIR, "plugins", str(pid), "manifest.json")) or {}
        except Exception:
            manifest = {}
        return plugin_admin.display_name(names, str(pid), manifest, extra=extra_entry or {})

    def _staging_uploads_dir():
        path = os.path.join(jztools_data.get_data_root(), ".staging", "uploads")
        os.makedirs(path, exist_ok=True)
        return path

    def _resolve_upload(name):
        """把前端回传的文件名解析为暂存目录内的绝对路径（拒绝任何路径穿越）。"""
        raw = str(name or "")
        base = os.path.basename(raw)
        if not base or base != raw or not base.lower().endswith(".zip"):
            return None
        path = os.path.join(_staging_uploads_dir(), base)
        return path if os.path.isfile(path) else None

    @app.get("/admin/plugins")
    @login_required
    def admin_plugins_page():
        """插件管理页（仅超级管理员）。"""
        if not _plugin_mgr_ok():
            return redirect(url_for("admin_index"))
        return send_from_directory(FRONTEND_DIR, "admin-plugins.html")

    @app.get("/api/admin/plugins")
    @login_required
    def admin_api_plugins():
        """插件盘点：代码版本 / 登记版本 / 待重启 / 备份数 / 数据占用 / 启停。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        set_operation("查询插件列表")
        rows = plugin_admin.list_plugins(PROJECT_DIR, jztools_data.get_data_root())
        cfg = load_admin_config()
        return jsonify({
            "ok": True,
            "plugins": rows,
            "app_version": plugin_admin.app_version(PROJECT_DIR) or "",
            "data_root": jztools_data.get_data_root(),
            "index_path": cfg.get("plugin_index_path") or "",
            "frozen": bool(getattr(sys, "frozen", False)),
            "restart_pending": [r["id"] for r in rows if r["restart_pending"]],
        })

    @app.post("/api/admin/plugins/upload")
    @login_required
    def admin_api_plugins_upload():
        """上传插件包并只读校验；返回应用计划（尚未写程序目录）。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        set_operation("上传插件包")
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "请选择插件包（.zip）"}), 400
        if not f.filename.lower().endswith(".zip"):
            return jsonify({"error": "只接受 .zip 插件包（由 tools\\build-plugin-package.ps1 生成）"}), 400
        if request.content_length and request.content_length > plugin_admin.MAX_ZIP_BYTES:
            return jsonify({"error": "包体积超过上限 %.0f MB" % (plugin_admin.MAX_ZIP_BYTES / 1048576)}), 413
        force = str(request.form.get("force") or "").lower() in ("1", "true", "on")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(f.filename))
        name = "%s-%s" % (plugin_admin.now_stamp(), safe)
        dest = os.path.join(_staging_uploads_dir(), name)
        try:
            f.save(dest)
        except Exception as e:
            return jsonify({"error": "保存上传文件失败：%s" % e}), 500
        try:
            inspected = plugin_admin.inspect_package(dest, PROJECT_DIR, jztools_data.get_data_root(), force=force)
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
            "name": _plugin_display_name(str(meta.get("id") or ""), meta.get("tools_entry")),
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
    @login_required
    def admin_api_plugins_apply():
        """应用已上传的插件包：服务端重新校验 → 备份 → 替换 → 登记（可回滚）。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        path = _resolve_upload(data.get("file"))
        if not path:
            return jsonify({"error": "上传的包不存在或已过期，请重新上传"}), 404
        set_operation("应用插件包")
        base, root = PROJECT_DIR, jztools_data.get_data_root()
        inspected = plugin_admin.inspect_package(path, base, root, force=bool(data.get("force")))
        if not inspected["ok"]:
            # 同上：带上 error 摘要，避免前端只显示"HTTP 400"
            return jsonify({"ok": False, "error": "；".join(inspected["errors"]),
                            "errors": inspected["errors"]}), 400
        report = plugin_admin.apply_package(
            base, root, inspected,
            purge_unknown=bool(data.get("purge_unknown")),
            update_entry=bool(data.get("update_entry")),
        )
        if report.get("ok"):
            try:
                os.remove(path)          # 应用成功即清理上传包（PU-5）
            except OSError:
                pass
        report["restart_pending"] = [r["id"] for r in plugin_admin.list_plugins(base, root)
                                     if r["restart_pending"]]
        report["name"] = _plugin_display_name(str(inspected["meta"].get("id") or ""),
                                               inspected["meta"].get("tools_entry"))
        # 应用成功且含后端改动 → 自动停服重启（data.auto_restart=false 可关闭）
        _maybe_auto_restart(report, auto=data.get("auto_restart", True))
        return jsonify(report), (200 if report.get("ok") else 500)

    @app.get("/api/admin/plugins/backups")
    @login_required
    def admin_api_plugins_backups():
        """某插件的备份清单（供回滚选择）。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        pid = (request.args.get("id") or "").strip()
        if not plugin_admin.PLUGIN_ID_RE.match(pid):
            return jsonify({"error": "插件 id 非法"}), 400
        set_operation("查询插件备份")
        items = plugin_admin.list_backups(jztools_data.get_data_root(), pid)
        return jsonify({"ok": True, "backups": [
            {"name": it["name"], "size": it["size"], "mtime": it["mtime"]} for it in items]})

    @app.post("/api/admin/plugins/rollback")
    @login_required
    def admin_api_plugins_rollback():
        """回滚插件到某个备份（缺省=最近一份）。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        pid = str(data.get("id") or "").strip()
        if not plugin_admin.PLUGIN_ID_RE.match(pid):
            return jsonify({"error": "插件 id 非法"}), 400
        set_operation("回滚插件")
        # 只允许回滚到该插件备份目录内的文件（拒绝任意路径）
        backup = None
        name = os.path.basename(str(data.get("backup") or ""))
        if name:
            candidate = os.path.join(plugin_admin.backups_dir(jztools_data.get_data_root(), pid), name)
            if os.path.isfile(candidate):
                backup = candidate
            else:
                return jsonify({"error": "指定的备份不存在"}), 404
        report = plugin_admin.rollback_plugin(PROJECT_DIR, jztools_data.get_data_root(), pid, backup)
        report["restart_pending"] = [r["id"] for r in plugin_admin.list_plugins(PROJECT_DIR, jztools_data.get_data_root())
                                     if r["restart_pending"]]
        report["name"] = _plugin_display_name(pid)
        # 回滚是整目录替换，一律需要重启；同样走自动停服重启
        _maybe_auto_restart(report, needed=True, auto=data.get("auto_restart", True))
        return jsonify(report), (200 if report.get("ok") else 500)

    @app.post("/api/admin/plugins/enable")
    @login_required
    def admin_api_plugins_enable():
        """启用 / 停用插件（写数据根 tools.json，不动代码与数据）。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        pid = str(data.get("id") or "").strip()
        if not plugin_admin.PLUGIN_ID_RE.match(pid):
            return jsonify({"error": "插件 id 非法"}), 400
        enabled = bool(data.get("enabled"))
        set_operation("启用插件" if enabled else "停用插件")
        ok = plugin_admin.set_plugin_enabled(jztools_data.get_data_root(), pid, enabled)
        if not ok:
            return jsonify({"error": "tools.json 中没有该插件的注册条目"}), 404
        return jsonify({"ok": True, "id": pid, "enabled": enabled})

    @app.get("/api/admin/plugins/index")
    @login_required
    def admin_api_plugins_index():
        """读取共享盘索引并比对已装版本（阶段三）。path 缺省用上次记住的路径。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        cfg = load_admin_config()
        path = (request.args.get("path") or "").strip() or (cfg.get("plugin_index_path") or "")
        if not path:
            return jsonify({"error": "请填写索引文件路径（共享盘上的 index.json）"}), 400
        set_operation("检查插件更新")
        res = plugin_admin.check_updates(PROJECT_DIR, jztools_data.get_data_root(), path)
        if not res.get("ok"):
            return jsonify(res), 400
        # 记住路径，下次免填（同一台机器上通常固定一个共享目录）
        if cfg.get("plugin_index_path") != path:
            cfg["plugin_index_path"] = path
            save_admin_config(cfg)
        return jsonify(res)

    @app.post("/api/admin/plugins/batch-apply")
    @login_required
    def admin_api_plugins_batch_apply():
        """按索引批量应用插件包（顺序应用；后端改动则最后统一重启一次）。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        data = request.get_json(silent=True) or {}
        cfg = load_admin_config()
        index_path = (data.get("index_path") or "").strip() or (cfg.get("plugin_index_path") or "")
        ids = [str(x) for x in (data.get("ids") or []) if str(x)]
        if not index_path or not ids:
            return jsonify({"error": "请先检查更新并勾选要升级的插件"}), 400
        set_operation("批量升级插件")
        res = plugin_admin.batch_apply(PROJECT_DIR, jztools_data.get_data_root(), index_path, ids)
        res["restart_pending"] = [r["id"] for r in plugin_admin.list_plugins(PROJECT_DIR, jztools_data.get_data_root())
                                  if r["restart_pending"]]
        # 批量应用：全部成功后统一重启一次
        _maybe_auto_restart(res, auto=data.get("auto_restart", True))
        return jsonify(res)

    @app.post("/api/admin/plugins/restart")
    @login_required
    def admin_api_plugins_restart():
        """重启服务让新插件代码生效（仅打包运行；源码模式提示手工重启）。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可管理插件"}), 403
        set_operation("重启服务（插件生效）")
        rows = plugin_admin.list_plugins(PROJECT_DIR, jztools_data.get_data_root())
        pending = [r["id"] for r in rows if r["restart_pending"]]
        if not getattr(sys, "frozen", False):
            return jsonify({"ok": False, "pending": pending,
                            "message": "源码开发模式不自动重启：Flask 调试重载通常会自行重启；"
                                       "若未生效请手工重启 python app.py"})
        ok, err = _spawn_self_restart()
        if not ok:
            return jsonify({"ok": False, "pending": pending, "error": err}), 500
        return jsonify({"ok": True, "pending": pending,
                        "message": "服务正在重启，约 5~10 秒后本页会自动重新加载"})

    # ---------------- 批量导入导出（单位 / 部门 / 人员） ----------------

    # 挂载同包 batch_io 子模块提供的 模板下载 / 导出 / 导入 接口；
    # 加载失败仅告警，不影响登录鉴权等核心能力。
    try:
        _register_batch_io(app)
        app.logger.info("admin 批量导入导出接口已挂载（单位 / 部门 / 人员）")
    except Exception as e:  # pragma: no cover - 依赖缺失等异常
        app.logger.warning(f"admin 批量导入导出接口挂载失败：{e}")

