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
    def account_change_password():
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
        return jsonify({
            "modules": [
                {
                    "id": m,
                    "name": ADMIN_MODULE_NAMES[m],
                    "count": counts[m],
                    "allowed": m in info["modules"],
                }
                for m in visible_modules
            ],
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

