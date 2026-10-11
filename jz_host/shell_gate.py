"""壳身份门：业务面仅桌面客户端可达，浏览器只放行管理面（2026-10-11 需求①）。

设计真源 → ``docs/plan/20261011浏览器退役与商店分发口径改造-TODO（面向执行模型）.md`` §2.1。
机制 = **壳标识头 + 接入密钥（共享秘密）+ 路径白名单默认拒绝**：

  · 桌面客户端在所有指向服务器的请求上带 ``X-JZ-Shell: <接入密钥>``；
  · 本门按路径分类放行：管理面白名单（登录闭环 + /admin + /api/admin + /plugin/admin +
    /api/site + /static/*，但 /static/index.html、/static/tool.html 两个业务页面本体除外）
    浏览器可达；其余（首页 / 工具壳页 / 插件页面 / 商店页 / 全部业务 API）一律要求有效壳标识；
  · 未列出即默认拒绝——新增业务路由自动落在门后，无需逐个登记。

信任模型（诚实边界，§2.1.7）：目标是关闭"浏览器常规访问业务面"的路径；密钥随客户端
配置文件分发（本机用户可读），不防有技术能力的内部人员主动提取——与本仓内网信任域
既有口径（catalog 不防篡改、共享盘同信任域）一致。可选加固：allowed_ips 白名单。

装配纪律（G1-2）：``install(app)`` 必须在 ``access_log.install(app)`` 之后、插件注册之前调用
——Flask before_request 按注册序执行，本门因此先于 admin 登录门（routes.register 期注册）
与 worker 反代桥（supervisor.mainapp_bridge）运行；本门只看请求头、不读会话，
存量会话不因本门失效。install() 自身不触碰数据根（装配早于 init_data_root），
token 文件在首个请求时惰性初始化。
"""
import hmac
import json
import logging
import os
import secrets
import threading

from flask import jsonify, request

import jztools_data

log = logging.getLogger("jztools.app")

HEADER = "X-JZ-Shell"
TOKEN_FILE = "shell-token.json"

# 浏览器完全放行（精确路径）：登录闭环自身 + 管理面 logo 标题依赖的站点信息 +
# favicon。/api/site 只含站点名等展示信息，无业务数据（app.py api_site）。
PUBLIC_EXACT = frozenset({
    "/login", "/api/login", "/api/logout", "/api/session",
    "/favicon.ico", "/api/site",
})
# 浏览器放行的前缀：管理后台页面与接口、admin 插件静态资源、框架静态资源
# （登录页 / 后台页依赖 /static/css/style.css 与 /static/js/*，见 login.html）。
_PUBLIC_ADMIN_PREFIX = "/admin"
PUBLIC_PREFIXES = ("/api/admin/", "/plugin/admin/", "/static/")
# 框架静态里两个"业务页面本体"防直取（其数据接口已全部落门后，直取只能拿到空壳，
# 这里一并关死，口径=彻底）。
DENY_EXACT = frozenset({"/static/index.html", "/static/tool.html"})

_ENV_SWITCH = "JZTOOLS_SHELL_ENFORCE"

_state_lock = threading.RLock()
_state = None          # token 文件解析结果 {"token":…, "enforce":…, "allowed_ips":…, "created_at":…}
_state_mtime = None    # 文件 mtime 缓存（管理面重置 / 手工改文件后无需重启）
_html_cache = None     # 提示页内容缓存
_static_dir = None     # install() 注入（ frozen 态=部署根 static；源码态同）


# ---------------- 接入密钥生命周期（单一写方 = 本模块） ----------------

def _token_path():
    return jztools_data.get_data_root_file("config", TOKEN_FILE)


def _write_state(state):
    path = _token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def _load_state():
    """读取（必要时生成）token 文件；mtime 缓存——外部改动/管理面重置即时生效。"""
    global _state, _state_mtime
    path = _token_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if _state is not None and mtime is not None and mtime == _state_mtime:
        return _state
    with _state_lock:
        mtime = None
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            pass
        if _state is not None and mtime is not None and mtime == _state_mtime:
            return _state
        state = None
        if mtime is not None:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                token = str(raw.get("token") or "")
                if token:
                    state = {
                        "token": token,
                        "enforce": raw.get("enforce", True) is not False,
                        "allowed_ips": [str(x) for x in (raw.get("allowed_ips") or []) if str(x).strip()],
                        "created_at": str(raw.get("created_at") or ""),
                    }
            except Exception:
                log.warning("壳门 token 文件损坏，重新生成：%s", path, exc_info=True)
        if state is None:
            # 首启 / 文件缺失或损坏：自动生成（与 admin.json secret_key 同款纪律）
            from datetime import datetime
            state = {
                "token": secrets.token_hex(32),
                "enforce": True,
                "allowed_ips": [],
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            _write_state(state)
            log.info("壳门接入密钥已生成：%s（管理后台「系统设置」可查看/重置）", path)
        _state = state
        try:
            _state_mtime = os.path.getmtime(path)
        except OSError:
            _state_mtime = None
        return _state


def token_info():
    """密钥信息（管理面展示/复制用；含明文 token——仅超管接口可达，见 admin 路由）。"""
    st = dict(_load_state())
    st["token_file"] = _token_path()
    st["env_override"] = _env_enforcement()
    return st


def reset_token():
    """重置密钥（保留 enforce/allowed_ips）：所有已分发旧密钥立即失效。"""
    global _state, _state_mtime
    with _state_lock:
        st = dict(_load_state())
        from datetime import datetime
        st["token"] = secrets.token_hex(32)
        st["created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_state(st)
        _state = st
        try:
            _state_mtime = os.path.getmtime(_token_path())
        except OSError:
            _state_mtime = None
    log.warning("壳门接入密钥已重置（旧密钥立即失效；桌面端需重新配置）")
    return token_info()


def verify(supplied):
    """常量时间比对请求头携带的密钥。"""
    if not supplied:
        return False
    return hmac.compare_digest(str(supplied), _load_state()["token"])


# ---------------- 紧急开关 / 路径分类 ----------------

def _env_enforcement():
    """环境变量优先：显式 0/false/off=关、1/true/on=开；未设=交由文件 enforce 字段。"""
    raw = os.environ.get(_ENV_SWITCH)
    if raw is None or not str(raw).strip():
        return None
    v = str(raw).strip().lower()
    if v in ("0", "false", "off", "no"):
        return False
    if v in ("1", "true", "on", "yes"):
        return True
    return None


def enforcement_enabled():
    env = _env_enforcement()
    if env is not None:
        return env
    return _load_state()["enforce"]


def _ip_allowed():
    allowed = _load_state()["allowed_ips"]
    if not allowed:
        return False
    addr = (request.remote_addr or "").strip()
    return addr in allowed


def is_public(path):
    """管理面白名单判定（浏览器可达；G1-4 静态例外先行）。"""
    if path in DENY_EXACT:
        return False
    if path in PUBLIC_EXACT:
        return True
    if path == _PUBLIC_ADMIN_PREFIX or path.startswith(_PUBLIC_ADMIN_PREFIX + "/"):
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


# ---------------- 请求门 ----------------

def _deny():
    """403 分派：API → JSON 契约；页面 → static/shell-required.html。

    响应统一带 ``X-JZ-Gate: shell_required`` 头——客户端能力层据此把"门拒绝"与
    普通权限 403 区分开（给接入密钥提示，不进会话失效分支）。"""
    gate_headers = {"X-JZ-Gate": "shell_required"}
    if request.path.startswith("/api/"):
        # 管理后台入口不对用户面暴露（2026-10-11 二次调整）——只给"用桌面客户端"的指引
        return jsonify({"error": "shell_required",
                        "hint": "业务功能仅限桌面客户端访问（接入密钥请联系管理员）"}), 403, gate_headers
    global _html_cache
    if _html_cache is None and _static_dir:
        try:
            with open(os.path.join(_static_dir, "shell-required.html"),
                      "rb") as f:
                _html_cache = f.read()
        except OSError:
            _html_cache = False
    body = _html_cache
    if not body:
        body = ("<!DOCTYPE html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
                "<title>JZ 工具箱</title><body style=\"font-family:sans-serif;"
                "text-align:center;padding:80px 20px;color:#333\">"
                "<h2>业务功能仅限桌面客户端访问</h2>"
                "<p>管理后台请访问 <a href=\"/admin\">/admin</a></p></body>").encode("utf-8")
    return body, 403, {"Content-Type": "text/html; charset=utf-8", **gate_headers}


def _enforce_shell():
    path = request.path
    if is_public(path):
        return None
    if not enforcement_enabled():
        return None
    if _ip_allowed():
        return None
    if verify(request.headers.get(HEADER, "")):
        return None
    log.warning("壳门拒绝（浏览器访问业务面）：%s %s（来源 %s）",
                request.method, path, request.remote_addr or "-")
    return _deny()


def install(app, static_dir=None):
    """注册请求门（app.py 装配期调用；顺序纪律见模块头）。"""
    global _static_dir, _html_cache
    _static_dir = static_dir
    _html_cache = None
    app.before_request(_enforce_shell)
