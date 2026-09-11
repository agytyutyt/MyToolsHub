# -*- coding: utf-8 -*-
"""与「过滤器」插件的唯一通道（规范 B-7：禁止 import 其他插件后端模块）。

为什么走接口而不是 import
------------------------
``plugins/file-filter/backend/core.py`` 里的 ``filter_columns`` / ``post_process``
看起来"直接 import 就能复用"，但《插件设计规范》B-7 与反模式 #9 明确禁止——
一旦 import，过滤器插件的内部实现就变成了本插件的隐式依赖，两边任一改动都会互相牵扯，
且过滤器插件被下线时本插件会直接 ImportError。因此本插件**只调它公开的程序化接口**
``POST /api/file-filter/apply``（file-filter 设计文档 §5.1 指定的复用方式）。

两种调用通道
------------

1. **进程内派发（首选）**：用当前 Flask 应用的 ``test_client()`` 发一次内部请求，
   并把当前请求的 ``Cookie`` 原样带上。好处是
   - 不依赖监听地址（打包后 waitress / 开发服务器 / 单元测试三种场景行为一致）；
   - 真实经过 admin 插件的登录鉴权与工具权限拦截，**不存在绕过访问控制的后门**；
   - 可被 Flask test client 回归测试完整覆盖。

2. **HTTP 回环（兜底）**：进程内派发不可用时，改用 ``requests`` 请求
   ``request.host_url + /api/file-filter/apply``，同样携带 Cookie，超时 120 秒（SEC-6）。

权限提示
--------
非超级管理员调用 ``/api/file-filter/apply`` 时，admin 插件会按**「过滤器」工具权限点**
拦截（这与用户直接打开过滤器页面是同一套口径）。因此使用本插件前，管理员需在
「人员管理 → 权限」中同时勾选「轨迹速写」与「过滤器」。本模块会把 403 转成
**可操作的中文提示**，而不是让用户看到裸 403。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urljoin

try:
    import requests
    REQUESTS_AVAILABLE = True
except Exception:  # pragma: no cover
    requests = None
    REQUESTS_AVAILABLE = False

try:
    import jztools_data
except Exception:  # pragma: no cover
    jztools_data = None

FILTER_PLUGIN_ID = "file-filter"
API_PREFIX = "/api/file-filter"
APPLY_PATH = API_PREFIX + "/apply"
CONFIG_PATH = API_PREFIX + "/config"
TIMEOUT_SECONDS = 120

_MAX_PAYLOAD_BYTES = 48 * 1024 * 1024      # 内部派发前的大小保险（20MB 上传远达不到）


class FilterError(Exception):
    """过滤器桥接错误。``message`` 为可直接展示的中文描述（SEC-5）。"""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------
# 可用性自检
# --------------------------------------------------------------------------

def filter_enabled() -> bool:
    """「过滤器」插件是否在运行配置中启用（enabled 不为 false）。"""
    path = None
    if jztools_data is not None:
        try:
            path = jztools_data.get_data_root_file("config", "tools.json")
        except Exception:
            path = None
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return False
    for item in (data.get("tools") or []):
        if isinstance(item, dict) and item.get("id") == FILTER_PLUGIN_ID:
            return item.get("enabled", True) is not False
    return False


def filter_route_available(app) -> bool:
    """过滤器插件的 /apply 路由是否已注册（后端起不来时用于给出准确原因）。"""
    if app is None:
        return False
    try:
        for rule in app.url_map.iter_rules():
            if str(rule.rule) == APPLY_PATH:
                return True
    except Exception:
        return False
    return False


def filter_status(app=None) -> Dict[str, Any]:
    """给 ``/status`` 与前端用的过滤器插件状态摘要。"""
    enabled = filter_enabled()
    routed = filter_route_available(app)
    return {
        "enabled": enabled,
        "route": routed,
        "available": bool(enabled and routed),
        "reason": ("" if (enabled and routed)
                   else ("过滤器插件已在配置中停用" if not enabled
                         else "过滤器插件后端未注册（请重启服务进程）")),
    }


def filter_llm_configured(app, cookie: str = "", host_url: str = "") -> bool:
    """过滤器插件是否已配置大模型（经其公开接口 ``GET /config`` 查询）。

    本插件**不读**过滤器插件的配置文件：那样会把"对方的存储格式"变成依赖。
    查不到一律按"未配置"处理（前端会引导管理员去配置，属保守且安全的默认）。
    """
    if app is None:
        return False
    try:
        client = _client_with_session(app, cookie)
        resp = client.get(CONFIG_PATH)
        if resp.status_code == 200:
            data = resp.get_json(silent=True) or {}
            return bool(data.get("llm_configured"))
    except Exception:
        pass
    if not REQUESTS_AVAILABLE or not host_url:
        return False
    try:
        resp = requests.get(urljoin(host_url, CONFIG_PATH),
                            headers={"Cookie": cookie} if cookie else {},
                            timeout=15)
        if resp.status_code == 200:
            return bool((resp.json() or {}).get("llm_configured"))
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# 主调用
# --------------------------------------------------------------------------

def apply_filter(app, rows: Sequence[Sequence[Any]], mode: str = "hard",
                 columns: Optional[List[str]] = None,
                 post_rules: Optional[List[Dict[str, Any]]] = None,
                 cookie: str = "", host_url: str = "") -> Dict[str, Any]:
    """调用过滤器插件的程序化接口，返回其响应 dict。

    入参 ``rows`` 为二维表（首行表头，单元格必须为 JSON 可序列化类型）。
    失败抛 :class:`FilterError`（``status`` 为建议的 HTTP 状态码）。
    """
    if not rows or not isinstance(rows[0], (list, tuple)):
        raise FilterError("没有可提交过滤的数据。", 400)
    payload: Dict[str, Any] = {
        "rows": [list(r) for r in rows],
        "mode": "llm" if str(mode).lower() == "llm" else "hard",
        "columns": [str(c) for c in (columns or [])],
    }
    if post_rules is not None:
        payload["post_rules"] = post_rules

    st = filter_status(app)
    if not st["available"]:
        raise FilterError("过滤器插件不可用（%s）。本插件的字段过滤依赖该插件，"
                          "请联系管理员在首页启用「过滤器」后重启服务。" % st["reason"], 503)

    try:
        _guard_size(payload)
    except FilterError:
        raise

    # ---- 通道一：进程内派发（首选）----
    if app is not None:
        try:
            return _dispatch_inprocess(app, payload, cookie)
        except FilterError:
            raise
        except Exception:
            pass                     # 落到通道二

    # ---- 通道二：HTTP 回环（兜底）----
    return _dispatch_http(payload, cookie, host_url)


def _guard_size(payload: Dict[str, Any]) -> None:
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return
    if size > _MAX_PAYLOAD_BYTES:
        raise FilterError("数据量过大（约 %.1f MB），无法提交字段过滤："
                          "请减少行数或改用时段的子集。" % (size / 1048576.0), 413)


def _client_with_session(app, cookie: str):
    """造一个带当前会话的测试客户端。

    ⚠ 实测坑（Werkzeug ≥ 2.3）：给 ``test_client().get/post`` 传
    ``headers={"Cookie": ...}`` 或 ``environ_overrides={"HTTP_COOKIE": ...}``
    **都会被忽略**（测试客户端用自带的 cookie jar 重写 HTTP_COOKIE），结果是内部请求
    变成匿名 → 过滤器插件返回 401。唯一可靠的做法是把 cookie 逐条塞进 jar，
    即 ``client.set_cookie(name, value, domain="localhost")``。
    """
    client = app.test_client()
    for part in (cookie or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        try:
            client.set_cookie(name, value, domain="localhost")
        except TypeError:                      # 兼容旧版 Werkzeug 的签名
            client.set_cookie(name, value)
    return client


def _dispatch_inprocess(app, payload: Dict[str, Any], cookie: str) -> Dict[str, Any]:
    """用 Flask test_client 走一次内部请求（真实经过鉴权与权限拦截）。"""
    headers = {"Cookie": cookie} if cookie else {}
    client = _client_with_session(app, cookie)
    resp = client.post(APPLY_PATH, json=payload, headers=headers)
    return _decode(resp.status_code, resp.get_data(as_text=True))


def _dispatch_http(payload: Dict[str, Any], cookie: str, host_url: str) -> Dict[str, Any]:
    if not REQUESTS_AVAILABLE:
        raise FilterError("内部调用过滤器插件失败，且后端缺少 requests 依赖，无法回退 HTTP 调用。", 503)
    base = host_url or "http://127.0.0.1:5000/"
    url = urljoin(base, APPLY_PATH)
    try:
        resp = requests.post(url, json=payload,
                             headers={"Cookie": cookie} if cookie else {},
                             timeout=TIMEOUT_SECONDS)
    except Exception as exc:                                   # SEC-5：不透出堆栈
        raise FilterError("调用过滤器插件失败（%s）。" % type(exc).__name__, 503)
    return _decode(resp.status_code, resp.text)


def _decode(status: int, text: str) -> Dict[str, Any]:
    """把过滤器插件的响应转成本插件语义，并给出可操作的中文提示。"""
    data: Dict[str, Any] = {}
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    detail = str(data.get("error") or "").strip()

    if status == 200 and isinstance(data.get("rows"), list):
        return data
    if status == 401:
        raise FilterError("登录状态已失效，请重新登录后再操作。", 401)
    if status == 403:
        raise FilterError("当前账号没有「过滤器」工具的使用权限。"
                          "请管理员在「人员管理 → 权限」中同时勾选「过滤器」与「轨迹速写」。", 403)
    if status == 404:
        raise FilterError("过滤器插件未启用或已下线，请联系管理员。", 503)
    if detail:
        raise FilterError("字段过滤失败：%s" % detail, 400 if status < 500 else 502)
    raise FilterError("字段过滤失败（HTTP %s）。" % status, 502 if status >= 500 else status)
