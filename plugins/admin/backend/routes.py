"""管理后台 — JZToolsHub 后端插件路由

「一切皆插件」：登录鉴权、单位/部门/人员/权限管理、工具访问控制等后台能力
全部封装为本插件，由 app.register_plugin_backends() 动态加载并调用 register(app)。

账号存储层级：单位(units) → 部门(departments) → 用户(users)。
用户字段：登录名 username、密码 password、姓名 name、身份证 idcard、角色 role、
权限点 permissions（config/tools.json 插件 ID 列表）、大模型配置
llm{format, base_url, api_key, model}（用户"各自设置"模式下由本人自助维护）。

安全：
- 敏感字段（密码、身份证、大模型 API Key）以 Fernet 对称加密存于 config/admin.json；
- 密钥保存在 config/.admin_key（已 gitignore，与密文分离）；
- 已登录的非超级管理员按权限点拦截无权限的工具访问。

数据与密钥：
- config/admin.json ：单位/部门/用户 / 角色 / 会话密钥（已 gitignore）；
- config/.admin_key  ：Fernet 加密密钥（已 gitignore，与密文分离）。
"""

import io
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from functools import wraps

from cryptography.fernet import Fernet
from flask import (g, has_request_context, jsonify, redirect, request, send_file,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import jz_api
import jz_deps
import jz_llm
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
# admin.json 读写锁（D1）：单进程内跨线程串行化，见 load/save_admin_config
ADMIN_CFG_LOCK = threading.RLock()

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
                                "llm": {"format": "openai", "base_url": "",
                                        "api_key": encrypt_field(""), "model": ""},
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
    """读取 config/admin.json（鉴权数据 / 单位 / 部门 / 人员 / 权限）。

    D1：读与写共用模块级 `ADMIN_CFG_LOCK`（RLock 便于同线程内 load→save 复入），
    保证「写者不会撞同一个临时名、读者不会读到 replace 中途的文件」。
    """
    with ADMIN_CFG_LOCK:
        ensure_admin_config()
        with open(ADMIN_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)


def save_admin_config(cfg):
    """原子写入 config/admin.json（唯一临时名 + rename，见 `app.save_registry` 同口径）。

    D1 已闭合：全部 `load → 改 → save` 写路径都整段包在 `with ADMIN_CFG_LOCK:` 内
    （跨步丢失更新窗口消除）。唯一特例是 `/api/admin/plugins/index`——它的读改写段中间
    夹着共享盘读取，按裁定 6 拆成两次进锁（锁内取快照 → 锁外读盘 → 重新 load 后只改
    目标字段再 save），见该视图的 docstring。
    """
    with ADMIN_CFG_LOCK:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(ADMIN_CONFIG_PATH),
                                   prefix="admin.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, ADMIN_CONFIG_PATH)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    # P3-1：配置写入后，同请求内已缓存的会话用户可能已过期（如刚授权/改角色）→ 立即作废。
    if has_request_context():
        try:
            g.pop("_sess_user_cache", None)
        except Exception:
            pass


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
            user["llm"] = {"format": "openai", "base_url": "",
                           "api_key": encrypt_field(""), "model": ""}
            changed = True
    return changed


def migrate_admin_llm_format(cfg):
    """给历史账号的 llm 段补上调用格式（统一大模型模块新增字段）。

    统一大模型模块支持多种调用格式（openai / anthropic / ollama / custom），
    老数据只有 base_url/api_key/model —— 语义上就是 OpenAI 兼容，补 "openai" 即可，
    不改变既有行为（用户"各自设置"模式下打开设置卡片就能看到并改格式）。
    """
    changed = False
    for _unit, _dept, user in iter_users(cfg):
        llm = user.get("llm")
        if isinstance(llm, dict) and not (llm.get("format") or "").strip():
            llm["format"] = "openai"
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


def can_review_store(info):
    """商店审核/下架/部署权限（2026-10-08 需求方修订）：仅超级管理员或管理员角色。

    与历史「全流程不分角色」不同——**提交上架仍全员可用（不设门槛）**，
    审核 / 驳回 / 下架 / 重新部署收紧到本判据（发布即部署属服务器敏感操作）。
    """
    if not info:
        return False
    if info.get("super_admin"):
        return True
    return info.get("role_id") in ("role-admin",) or info.get("role") == "管理员"


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


def _req_user_cache():
    """请求级会话用户缓存槽（挂在 flask.g）。

    P3-1：一请求内 get_session_user() 会被多条 before_request 守卫 + 端点处理器 +
    jz_api provider 反复调用，每次都 `load_admin_config()`（整份 admin.json 解析）+
    用户检索。请求内会话身份不变，故按 `session["user"]` 为键缓存派生结果，命中即返回
    —— 单请求由 N 次解析降为 1 次。无请求上下文（如单测直调）时返回 None（不缓存）。
    """
    if not has_request_context():
        return None
    cache = getattr(g, "_sess_user_cache", None)
    if cache is None:
        cache = {}
        g._sess_user_cache = cache
    return cache


def get_session_user():
    """返回当前登录用户的展示信息，未登录返回 None。

    含权限点（可用工具 ID 列表）：拥有全部管理模块的角色视为超级管理员，
    默认授予全部工具；其余账号取自身 permissions 字段。
    请求级缓存见 `_req_user_cache`（P3-1）；配置一经 save 即作废该缓存（见 save_admin_config）。
    """
    username = session.get("user")
    if not username:
        return None
    cache = _req_user_cache()
    if cache is not None and cache.get("key") == username and "val" in cache:
        return cache["val"]
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
    val = {
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
    if cache is not None:                     # 请求内后续调用直接命中（只读消费；无调用方改写）
        cache["key"] = username
        cache["val"] = val
    return val


def can_manage_llm(info):
    """是否可维护统一大模型的**全局**配置：超级管理员或管理员角色。

    需求口径是"由管理员或超级管理员确定"（模式 / API 地址 / 格式 / Key），
    故这里比"仅超级管理员"的敏感操作（数据目录、插件管理）放宽一档，
    与各业务插件里 _can_manage() 的判定保持一致（同一对角色常量）。
    """
    if not info:
        return False
    if info.get("super_admin"):
        return True
    return info.get("role_id") in ("role-admin",) or info.get("role") == "管理员"


# ===================== 用户级大模型配置（统一大模型模块的账号侧接缝） =====================
# 主体模块 jz_llm 与框架路由 /api/llm/* 需要读写"用户自己的接入配置"，
# 而账号数据归本插件所有 —— 故经 jz_api 暴露下面两个 provider（依赖倒置），
# 既不让框架直接改 admin.json，也不另立一份用户配置造成两处不一致。
# 返回/接收的都是**明文** Key：加解密只在本插件内完成。

def get_user_llm(username):
    """取某账号自己的大模型接入配置（明文 Key）；账号不存在返回 None。"""
    cfg = load_admin_config()
    found = find_user(cfg, username)
    if found is None:
        return None
    llm = found[2].get("llm") or {}
    return {
        "format": llm.get("format") or "openai",
        "url": llm.get("base_url") or "",
        "api_key": decrypt_field(llm.get("api_key")),
        "model": llm.get("model") or "",
        # 自定义格式（custom）的四个模板字段——不透传会让"用户自助设置"下的
        # custom 配置在保存后被静默丢弃（界面报可用、调用必失败）。
        "headers": llm.get("headers") if isinstance(llm.get("headers"), dict) else {},
        "body": llm.get("body") or "",
        "text_path": llm.get("text_path") or "",
        "error_path": llm.get("error_path") or "",
    }


def save_user_llm(username, config):
    """保存某账号自己的大模型接入配置（明文 Key 落盘前加密）；账号不存在返回 False。"""
    with ADMIN_CFG_LOCK:                      # D1：读改写全程持锁
        cfg = load_admin_config()
        found = find_user(cfg, username)
        if found is None:
            return False
        user = found[2]
        llm = user.setdefault("llm", {})
        config = config or {}
        llm["format"] = (str(config.get("format") or "").strip() or "openai")
        llm["base_url"] = str(config.get("url") or "").strip()
        llm["api_key"] = encrypt_field(str(config.get("api_key") or ""))
        llm["model"] = str(config.get("model") or "").strip()
        headers = config.get("headers")
        llm["headers"] = headers if isinstance(headers, dict) else {}
        llm["body"] = str(config.get("body") or "")
        llm["text_path"] = str(config.get("text_path") or "").strip()
        llm["error_path"] = str(config.get("error_path") or "").strip()
        save_admin_config(cfg)
        return True


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


def build_org_tree():
    """构建 单位→部门→用户 树（只读，仅标识与姓名，不含敏感字段）。

    本函数是**框架 API 的 admin 侧实现**：既供本插件的 /api/admin/org-tree
    （历史路径，保留兼容）使用，也经 jz_api.register_providers 暴露给主体
    的 /api/org/tree——业务插件因此不必再直连 admin 的接口（插件间零依赖，
    见 docs/design/主体与插件解耦-设计文档.md §5.4）。
    """
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
    return tree


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


def _load_admin_org():
    """载入同包 admin_org 子模块（组织/部门/人员/角色管理路由；P3-1 D1 拆出）。

    优先按包内相对导入加载（app.py 以 jztools_admin 包名动态加载本文件）；
    极端情况下（模块被单独按路径执行）回退为按文件路径加载。
    """
    try:
        from . import admin_org  # noqa: WPS433 - 包内相对导入为常规路径
        return admin_org
    except Exception:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_org.py")
        spec = importlib.util.spec_from_file_location("jztools_admin_admin_org", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _register_admin_org(app):
    """挂载组织 / 账号管理路由，注入本模块的内部依赖（避免循环导入）。"""
    admin_org = _load_admin_org()
    admin_org.register(app, {
        "login_required": login_required,
        "permission_required": permission_required,
        "set_operation": set_operation,
        "get_session_user": get_session_user,
        "load_admin_config": load_admin_config,
        "save_admin_config": save_admin_config,
        "load_registry": load_registry,
        "iter_users": iter_users,
        "find_user": find_user,
        "find_dept": find_dept,
        "find_unit": find_unit,
        "encrypt_field": encrypt_field,
        "decrypt_field": decrypt_field,
        "role_super_admin": _role_super_admin,
        "registered_tool_ids": _registered_tool_ids,
        "normalize_permission_points": _normalize_permission_points,
        "append_user": append_user,
        "admin_cfg_lock": ADMIN_CFG_LOCK,
        "admin_modules": ADMIN_MODULES,
    })


def _load_admin_plugins_api():
    """载入同包 admin_plugins_api 子模块（插件管理面 API；P3-1 D1 拆出）。

    优先按包内相对导入加载；极端情况下（模块被单独按路径执行）回退为按文件路径加载。
    """
    try:
        from . import admin_plugins_api  # noqa: WPS433 - 包内相对导入为常规路径
        return admin_plugins_api
    except Exception:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_plugins_api.py")
        spec = importlib.util.spec_from_file_location("jztools_admin_admin_plugins_api", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _register_admin_plugins_api(app):
    """挂载插件管理面路由，注入本模块的内部依赖（避免循环导入）。"""
    admin_plugins_api = _load_admin_plugins_api()
    admin_plugins_api.register(app, {
        "login_required": login_required,
        "set_operation": set_operation,
        "get_session_user": get_session_user,
        "load_admin_config": load_admin_config,
        "save_admin_config": save_admin_config,
        "plugin_admin": _load_plugin_admin(),
        "project_dir": PROJECT_DIR,
        "admin_cfg_lock": ADMIN_CFG_LOCK,
        "plugin_mgr_ok": _plugin_mgr_ok,
        "plugin_display_name": _plugin_display_name,
        "staging_uploads_dir": _staging_uploads_dir,
        "resolve_upload": _resolve_upload,
        "maybe_auto_restart": _maybe_auto_restart,
        "spawn_self_restart": _spawn_self_restart,
    })


def _load_admin_store_api():
    """载入同包 admin_store_api 子模块（商店管理面 API；P3-1 D1 拆出）。

    优先按包内相对导入加载；极端情况下（模块被单独按路径执行）回退为按文件路径加载。
    """
    try:
        from . import admin_store_api  # noqa: WPS433 - 包内相对导入为常规路径
        return admin_store_api
    except Exception:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_store_api.py")
        spec = importlib.util.spec_from_file_location("jztools_admin_admin_store_api", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _register_admin_store_api(app):
    """挂载商店管理面路由，注入本模块的内部依赖（避免循环导入）。"""
    admin_store_api = _load_admin_store_api()
    admin_store_api.register(app, {
        "login_required": login_required,
        "set_operation": set_operation,
        "get_session_user": get_session_user,
        "can_review_store": can_review_store,
        "load_admin_config": load_admin_config,
        "save_admin_config": save_admin_config,
        "plugin_admin": _load_plugin_admin(),
        "project_dir": PROJECT_DIR,
        "admin_cfg_lock": ADMIN_CFG_LOCK,
        "store_root": _store_root,
        "load_store_admin": _load_store_admin,
    })


def _load_data_migrate():
    """载入同包 data_migrate 子模块（数据迁移：导出/导入，纯逻辑）。

    优先按包内相对导入加载；极端情况下（模块被单独按路径执行）回退为按文件路径加载。
    """
    try:
        from . import data_migrate  # noqa: WPS433 - 包内相对导入为常规路径
        return data_migrate
    except Exception:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_migrate.py")
        spec = importlib.util.spec_from_file_location("jztools_admin_data_migrate", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


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


def _plugin_mgr_ok():
    """商店/插件管理面超管门槛（P3-1 D1：自 register 内提升为模块级，供子模块 deps 注入）。"""
    info = get_session_user()
    return bool(info and info.get("super_admin"))


def _plugin_display_name(pid, extra_entry=None):
    """插件中文展示名：数据根 tools.json（权威，规范 M-2）→ 包内/注册条目 → manifest.name → id。

    后台页面与所有接口统一走这里，避免出现"表格是中文、计划预览是英文 id"的不一致。
    """
    if not pid:
        return ""
    plugin_admin = _load_plugin_admin()
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


def _store_root():
    """商店数据根（store 条目/包/台账都在 <数据根>/store/ 下；P3-1 D1 提升为模块级）。"""
    return jztools_data.get_data_root()


def _load_store_admin():
    """载入同包 store_admin 子模块（商店管理写面：上架/审核/发布/下架/统计 + 数据契约）。

    与 store 插件（读面）零跨插件 import——同包子模块属 admin 自有命名空间（设计文档 §3.3）。
    """
    try:
        from . import store_admin  # noqa: WPS433 - 包内相对导入为常规路径
        return store_admin
    except Exception:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store_admin.py")
        spec = importlib.util.spec_from_file_location("jztools_admin_store_admin", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _spawn_self_restart():
    """冻结模式自重启：派出分离的助手进程，等本进程退出后按原工作目录重新拉起 exe。

    见 docs/design/插件独立升级方案-设计文档.md §9.3。返回 (ok, error)。
    要点：① 先回 HTTP 响应再退出（调用方在本函数内用 threading.Timer 延迟 os._exit）；
         ② 助手 = exe 自身以 `--wait-restart <本进程PID>` 模式运行（见 app.py
         `_run_restart_helper`），用 OpenProcess 内核句柄等待旧进程退出，不依赖
         控制台工具。曾用「cmd 助手轮询 tasklist」方案，实测在 DETACHED_PROCESS
         下 tasklist 输出恒为空、助手永远走不到 start 行，真机自重启从不生效；
         ③ 助手以本进程为父派生，工作目录 = 程序目录（与托盘/快捷方式同款约定）。
    """
    import subprocess
    import threading

    exe = sys.executable
    base = PROJECT_DIR
    pid = os.getpid()
    try:
        flags = 0
        for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
            flags |= getattr(subprocess, name, 0)
        subprocess.Popen([exe, "--wait-restart", str(pid)],
                         creationflags=flags, close_fds=True, cwd=base)
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
    # 依赖可用性按请求实时刷新：服务运行中装「依赖组件包」后无需重启即可生效
    # （标记在模块导入时算好，不刷新会让插件页面一直显示"未安装"——jz_deps.refresh_flags）
    jz_deps.install_refresher(app, globals(), _load_batch_io)

    # 会话密钥：首次启动生成并持久化于 config/admin.json，重启后会话保持有效
    ensure_admin_config()
    with ADMIN_CFG_LOCK:                      # D1 全程锁：注册期单线程，与请求写路径同口径上锁
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
        if migrate_admin_llm_format(cfg):
            save_admin_config(cfg)
        if sync_admin_permissions(cfg):
            save_admin_config(cfg)
    app.config["SECRET_KEY"] = cfg["secret_key"]
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # 会话超时：读取 session 节配置（缺省 30 分钟空闲 / 12 小时绝对）
    idle_minutes, absolute_hours = _session_settings(cfg)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=absolute_hours)

    # 框架 API 注册（依赖倒置，见 docs/design/主体与插件解耦-设计文档.md §5.1 FC-3）：
    # 主体与业务插件一律从主体模块 jz_api 取用会话工具与组织架构树，
    # 不再 import 本插件的内部模块（jztools_admin.routes）——插件之间因此互不依赖。
    # 本插件卸载/未加载时，jz_api 自动降级为"未登录 / 空操作 / 组织架构不可用"。
    jz_api.register_providers(
        get_session_user=get_session_user,
        set_operation=set_operation,
        get_org_tree=build_org_tree,
        # 统一大模型模块的用户级配置接缝（见上方 get_user_llm/save_user_llm 的说明）
        get_user_llm=get_user_llm,
        save_user_llm=save_user_llm,
    )

    # 全局拦截（注册顺序即执行顺序，必须先超时守卫、再登录拦截）：
    #   1. make_session_guard    空闲/绝对超时登出 + 滑动续期（R1）
    #   2. _enforce_login        白名单外一律要求登录（R2）——已覆盖 /api/tools/reorder 与
    #                            /api/tools/visibility（二者不在 PUBLIC_PATHS/PREFIXES），
    #                            故原第 3 道 _protect_admin_ops 系冗余死代码，已随 P3-2 清理
    #   3. _enforce_tool_access  非超管按权限点拦工具（保持不变）
    app.before_request(make_session_guard(idle_minutes, absolute_hours))
    app.before_request(_enforce_login)
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

    @app.post("/api/admin/account/password")
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
        with ADMIN_CFG_LOCK:                  # D1 全程锁
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
        **新代码请调主体接口 `/api/org/tree`**（同一份实现经 jz_api 暴露，
        插件间因此零依赖）；本路径保留兼容旧版前端。
        """
        set_operation("查询组织架构树")
        return jsonify({"ok": True, "tree": build_org_tree()})

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
        # 大模型设置：统一大模型的模式与全局接入配置，管理员 / 超级管理员可见
        modules.append({
            "id": "llm",
            "name": "大模型设置",
            "count": 0,
            "allowed": can_manage_llm(info),
        })
        # 数据迁移：导出/导入全部数据（按插件分类打包、口令加密），仅超级管理员
        modules.append({
            "id": "migrate",
            "name": "数据迁移",
            "count": 0,
            "allowed": bool(info.get("super_admin")),
        })
        return jsonify({
            "modules": modules,
            "user": info,
        })

    # ---------------- 单位管理 ----------------

    # ---------------- 组织 / 部门 / 人员 / 角色管理（P3-1 D1 拆出 admin_org.py，原位注册） ----------------

    _register_admin_org(app)

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

    # ---------------- 统一大模型设置（管理员 / 超级管理员） ----------------
    # 配置真源在框架模块 jz_llm（<数据根>/config/llm.json），本插件只做**界面与鉴权**：
    # 模式（管理员统一配置 / 用户各自设置）、全局接入配置、连通测试。
    # 与 /admin/settings（数据目录）的区别：那一项仅超级管理员，本项管理员亦可（需求口径）。

    @app.get("/admin/llm")
    @login_required
    def admin_llm_page():
        """大模型设置页（管理员 / 超级管理员）。"""
        info = get_session_user()
        if not can_manage_llm(info):
            return redirect(url_for("admin_index"))
        return send_from_directory(FRONTEND_DIR, "admin-llm.html")

    @app.get("/api/admin/llm-settings")
    @login_required
    def admin_api_llm_settings():
        """读取统一大模型的全局设置（API Key 只回掩码）。"""
        info = get_session_user()
        if not can_manage_llm(info):
            return jsonify({"error": "仅管理员可查看大模型设置"}), 403
        set_operation("查询大模型设置")
        cfg = jz_llm.public_global()
        return jsonify({
            "ok": True,
            "mode": cfg["mode"],
            "fallback": cfg["fallback"],
            "provider": cfg["provider"],
            "formats": jz_llm.formats_public(),
            "requests_available": jz_llm.REQUESTS_AVAILABLE,
            "user_count": count_users(load_admin_config()),
        })

    @app.post("/api/admin/llm-settings")
    @login_required
    def admin_api_llm_settings_save():
        """保存统一大模型的全局设置（**按字段局部更新**）。

        请求体：{mode?, fallback?, provider?{format,url,api_key,model,…}}
        ——只改传进来的字段，未传的保持原值。这样界面上两张卡片可以各存各的：
        「调用模式」卡改模式即自动保存 {mode} / {fallback}，
        「全局接入配置」卡的保存按钮只提交 {provider}，不会顺带改动模式。
        （全量提交也照常工作，向后兼容。）
        api_key 留空或回传掩码表示不修改已保存的 Key。
        """
        info = get_session_user()
        if not can_manage_llm(info):
            return jsonify({"error": "仅管理员可修改大模型设置"}), 403
        data = request.get_json(silent=True) or {}
        current = jz_llm.load_global()

        mode = current["mode"]
        if "mode" in data:
            mode = str(data.get("mode") or "").strip().lower()
            if mode not in ("admin", "user"):
                return jsonify({"error": "模式取值非法"}), 400

        fallback = current["fallback"]
        if "fallback" in data:
            fallback = data.get("fallback") is not False

        provider = current["provider"]
        if isinstance(data.get("provider"), dict):
            provider = jz_llm.normalize_provider(data["provider"])
            if jz_llm.is_mask(provider["api_key"]):
                provider["api_key"] = current["provider"]["api_key"]  # 掩码/留空 = 保留原 Key

        jz_llm.save_global({"mode": mode, "fallback": fallback, "provider": provider})
        set_operation("保存大模型设置")
        return jsonify({
            "ok": True,
            "mode": mode,
            "fallback": fallback,
            "configured": jz_llm.provider_usable(provider),
            "problem": jz_llm.provider_problem(provider),
        })

    @app.post("/api/admin/llm-test")
    @login_required
    def admin_api_llm_test():
        """连通性测试：测请求体里这套（表单里刚填、还没保存的），缺省测已保存的全局配置。"""
        info = get_session_user()
        if not can_manage_llm(info):
            return jsonify({"error": "仅管理员可测试大模型配置"}), 403
        data = request.get_json(silent=True) or {}
        provider = None
        if any(str(data.get(k) or "").strip() for k in ("url", "model", "format")):
            provider = jz_llm.normalize_provider(data)
            if jz_llm.is_mask(provider["api_key"]):
                provider["api_key"] = jz_llm.load_global()["provider"]["api_key"]
        set_operation("测试大模型连通性")
        ok, detail = jz_llm.test_connection(provider=provider)
        return jsonify({"ok": ok, "detail": detail})

    # ---------------- 数据迁移（导出 / 导入，仅超级管理员） ----------------
    # 纯逻辑在 data_migrate.py（按插件分类打包 + 口令加密容器 + 密钥字段改封），
    # 本处只做鉴权、暂存与 HTTP 语义。设计见 docs/design/数据迁移-设计文档.md。
    # 安全：仅超管（可覆盖全部数据，与数据目录/插件管理同一档）；导出整包口令加密；
    # 导入逐条白名单校验 + 覆盖前备份；暂存的包本身已加密，落盘不泄漏。

    @app.get("/admin/migrate")
    @login_required
    def admin_migrate_page():
        """数据迁移页（仅超级管理员）。"""
        if not _plugin_mgr_ok():
            return redirect(url_for("admin_index"))
        return send_from_directory(FRONTEND_DIR, "admin-migrate.html")

    def _migrate_labels():
        """插件中文展示名（tools.json 权威）：迁移分段与预览里都要用。"""
        try:
            return {t.get("id"): (t.get("name") or t.get("id"))
                    for t in load_registry().get("tools", []) if t.get("id")}
        except Exception:
            return {}

    def _migrate_app_version():
        try:
            return str(jz_deps.read_version_json(PROJECT_DIR).get("app") or "")
        except Exception:
            return ""

    @app.get("/api/admin/migrate/plan")
    @login_required
    def admin_api_migrate_plan():
        """导出前的盘点：可导出的分段（框架三段 + 各插件数据段）与体积。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可导出数据"}), 403
        set_operation("盘点可导出数据")
        dm = _load_data_migrate()
        sections = dm.plan_export(jztools_data.get_data_root(), PROJECT_DIR, _migrate_labels())
        return jsonify({
            "ok": True,
            "sections": sections,
            "data_root": jztools_data.get_data_root(),
            "app_version": _migrate_app_version(),
            "totals": {"files": sum(s["files"] for s in sections),
                       "bytes": sum(s["bytes"] for s in sections)},
            "min_passphrase": 8,
        })

    @app.post("/api/admin/migrate/export")
    @login_required
    def admin_api_migrate_export():
        """导出数据迁移包（口令加密的 .jzdata，直接下载）。

        请求体：{passphrase, sections: ["accounts","llm","tools","plugin:<id>",…]}
        """
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可导出数据"}), 403
        data = request.get_json(silent=True) or {}
        sections = data.get("sections")
        if not isinstance(sections, list) or not sections:
            return jsonify({"error": "请至少选择一个要导出的数据段"}), 400
        set_operation("导出数据迁移包")
        dm = _load_data_migrate()
        try:
            blob, manifest = dm.build_package(
                jztools_data.get_data_root(), PROJECT_DIR, sections,
                str(data.get("passphrase") or ""), get_fernet(),
                app_version=_migrate_app_version(), plugin_labels=_migrate_labels())
        except dm.MigrateError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"导出失败：{e.strerror or e}"}), 500
        return send_file(io.BytesIO(blob), as_attachment=True,
                         download_name=dm.default_filename(),
                         mimetype="application/octet-stream")

    # 迁移包暂存表：token -> {envelope_key, zip_bytes, path, expires}
    # 口令派生的**信封密钥**只在内存里活 30 分钟（不落盘、不回传前端）：
    # 这样"解析预览 → 确认导入"两步之间不必让管理员重复输入口令；
    # 服务重启则该 token 失效，重新解析一次即可（包还在本地，无损失）。
    _MIGRATE_TTL = 30 * 60
    _MIGRATE_STAGE = {}
    _MIGRATE_LOCK = threading.Lock()

    def _migrate_purge_locked():
        """清掉过期暂存（含磁盘文件）。调用方需已持有 _MIGRATE_LOCK。"""
        now = time.time()
        for key in [k for k, v in _MIGRATE_STAGE.items() if v["expires"] < now]:
            entry = _MIGRATE_STAGE.pop(key, None)
            if entry and entry.get("path"):
                try:
                    os.remove(entry["path"])
                except OSError:
                    pass

    @app.post("/api/admin/migrate/inspect")
    @login_required
    def admin_api_migrate_inspect():
        """上传迁移包 + 口令 → 解密校验并返回预览（不写任何数据），返回 token 供导入用。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可导入数据"}), 403
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "请选择数据迁移包（.jzdata）"}), 400
        set_operation("解析数据迁移包")
        dm = _load_data_migrate()
        stage_dir = os.path.join(jztools_data.get_data_root(), ".staging", "migrate")
        os.makedirs(stage_dir, exist_ok=True)
        token = uuid.uuid4().hex
        path = os.path.join(stage_dir, token + dm.EXT)
        try:
            with open(path, "wb") as fp:
                shutil.copyfileobj(file.stream, fp, 1024 * 1024)
            size = os.path.getsize(path)
            if size > dm.MAX_PACKAGE_BYTES:
                raise dm.MigrateError("数据包超过上限（300 MB）")
            with open(path, "rb") as fp:
                blob = fp.read()
            info = dm.inspect(blob, str(request.form.get("passphrase") or ""),
                              jztools_data.get_data_root(), _migrate_labels())
        except dm.MigrateError as e:
            try:
                os.remove(path)
            except OSError:
                pass
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"读取上传文件失败：{e.strerror or e}"}), 500

        with _MIGRATE_LOCK:
            _migrate_purge_locked()
            _MIGRATE_STAGE[token] = {
                "envelope_key": info.pop("envelope_key"),
                "zip_bytes": info.pop("zip_bytes"),
                "path": path,
                "expires": time.time() + _MIGRATE_TTL,
            }
        return jsonify({"ok": True, "token": token, "size": size, **info})

    @app.post("/api/admin/migrate/import")
    @login_required
    def admin_api_migrate_import():
        """把解析过的迁移包按勾选分段写入数据根；覆盖前自动备份。"""
        if not _plugin_mgr_ok():
            return jsonify({"error": "仅超级管理员可导入数据"}), 403
        data = request.get_json(silent=True) or {}
        token = str(data.get("token") or "")
        sections = data.get("sections")
        if not isinstance(sections, list) or not sections:
            return jsonify({"error": "请至少选择一个要导入的数据段"}), 400
        with _MIGRATE_LOCK:
            _migrate_purge_locked()
            entry = _MIGRATE_STAGE.get(token)
        if not entry:
            return jsonify({"error": "数据包已过期（暂存 30 分钟），请重新选择文件并解析"}), 400
        set_operation("导入数据迁移包")
        dm = _load_data_migrate()
        backup_root = os.path.join(jztools_data.get_data_root(), "backups", "migrate")
        try:
            report = dm.apply_import(entry["zip_bytes"], entry["envelope_key"],
                                     jztools_data.get_data_root(), set(sections),
                                     get_fernet(), backup_root=backup_root)
        except dm.MigrateError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"写入数据失败：{e.strerror or e}"}), 500
        finally:
            # 一次性：无论成败都清掉暂存（包已加密，但没必要留着）
            with _MIGRATE_LOCK:
                _MIGRATE_STAGE.pop(token, None)
            try:
                os.remove(entry["path"])
            except OSError:
                pass
        try:
            report["purged_backups"] = dm.cleanup_backups(backup_root, keep=3)
        except Exception:
            report["purged_backups"] = 0
        return jsonify({"ok": True, **report})

    # ---------------- 插件管理（插件包：上传 / 应用 / 回滚 / 批量升级，仅超级管理员） ----------------
    # 设计：docs/design/插件独立升级方案-设计文档.md §9（阶段二：应用内升级）与 §10（阶段三：共享盘索引）
    # 安全（PU-1~PU-6）：全部仅限超级管理员；上传包先落到数据根 .staging\uploads\（不落程序目录），
    #   校验通过才允许应用；apply 时服务端**重新校验一遍**（不信任前端传来的任何状态）；
    #   逐条规则与目标机离线安装器 tools/plugin-upgrade/install-plugin.ps1 一致。

    @app.get("/admin/plugins")
    @login_required
    def admin_plugins_page():
        """插件管理页（仅超级管理员）。"""
        if not _plugin_mgr_ok():
            return redirect(url_for("admin_index"))
        return send_from_directory(FRONTEND_DIR, "admin-plugins.html")

    # ---------------- 插件管理面 API（P3-1 D1 拆出 admin_plugins_api.py，原位注册） ----------------

    _register_admin_plugins_api(app)

    # ---------------- 商店管理（O2 写面：上架/审核/发布/下架/统计） ----------------
    # 设计：docs/design/桌面端插件商店-设计文档.md §3.3 / §3.7。
    # 权限口径（需求⑦拍板）：不设角色门槛——全部登录用户同权（机器校验+内网信任模型兜底）。
    # 与 store 插件（读面）唯一交互面 = 数据根\store\ 数据契约；端点挂 /plugin/admin/store/*
    # （admin 自有命名空间）；管理页 /admin/store（浏览器与桌面端同页）。

    store_admin = _load_store_admin()

    @app.get("/admin/store")
    @login_required
    def admin_store_page():
        """商店管理页（登录即可，不分角色）。"""
        return send_from_directory(FRONTEND_DIR, "admin-store.html")

    # 商店数据根（P3-1 D1：提升为模块级，供本页与 admin_store_api 子模块共用）

    # ---------------- 商店管理面 API（P3-1 D1 拆出 admin_store_api.py，原位注册） ----------------

    _register_admin_store_api(app)

    # ---------------- 批量导入导出（单位 / 部门 / 人员） ----------------

    # 挂载同包 batch_io 子模块提供的 模板下载 / 导出 / 导入 接口；
    # 加载失败仅告警，不影响登录鉴权等核心能力。
    try:
        _register_batch_io(app)
        app.logger.info("admin 批量导入导出接口已挂载（单位 / 部门 / 人员）")
    except Exception as e:  # pragma: no cover - 依赖缺失等异常
        app.logger.warning(f"admin 批量导入导出接口挂载失败：{e}")

