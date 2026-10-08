# -*- coding: utf-8 -*-
"""llm_client —— 统一大模型的客户端网关（存量适配 2026-10-08，C 类客户端工件专用）。

服务器侧统一大模型（jz_llm）的能力经 jz_relay.own() 触达：本模块只回答「能不能用」
（探服务器 /status 的 llm_configured），语义匹配全部发生在服务器工件端点
（POST /api/trajectory-sketch/llm/match-columns），Key 不落本机（红线 5 / L-10）。
缓存 60s；离线时按「不可用」降级（分析仍可走硬过滤档，界面明示）。
"""
import time

PLUGIN_ID = "trajectory-sketch"
LLM_TIMEOUT = 120


class LLMError(Exception):
    """大模型不可用 / 调用失败（与服务器侧 jz_llm.LLMError 同名同义，调用点无需改）。"""


_CACHE = {"ts": 0.0, "ok": False, "reason": "尚未探测"}
REQUESTS_AVAILABLE = True     # 由 _probe() 按服务器 /status 如实刷新（本机不再直连大模型）


def _own():
    import jz_relay
    return jz_relay.own("local-trajectory-sketch")


class GatewaySession(object):
    """jz_llm.resolve() 返回形态的最小兼容面（configured/provider/reason/source）。"""

    def __init__(self, ok, reason):
        self.provider = {}
        self.reason = reason
        self.source = "server"
        self._ok = ok

    def configured(self):
        return self._ok


def _probe():
    if time.time() - _CACHE["ts"] < 60:
        return
    try:
        r = _own().get("/api/trajectory-sketch/status", timeout=15)
        d = r.json() or {}
        ok = bool(d.get("llm_configured"))
        deps = d.get("dependencies") or {}
        globals()["REQUESTS_AVAILABLE"] = bool(deps.get("requests", True))
        _CACHE.update(ts=time.time(), ok=ok,
                      reason="" if ok else "服务器端大模型未配置（管理后台 → 大模型设置）")
    except Exception as exc:
        _CACHE.update(ts=time.time(), ok=False,
                      reason="服务器暂不可用，本地部分可继续（%s）" % type(exc).__name__)


def invalidate():
    """服务器语义段连接失败时调用：立即使可用性缓存失效（后续请求在入口即降级）。"""
    _CACHE.update(ts=0.0, ok=False, reason="服务器暂不可用")


def available():
    _probe()
    return bool(_CACHE["ok"])


def session():
    """取当前生效的接入配置快照（网关视图；只能在请求线程内调用，与服务器侧同纪律）。"""
    _probe()
    return GatewaySession(_CACHE["ok"], _CACHE["reason"])


def resolve(plugin_id=""):
    return session()


def default_prompt():
    """提示词归服务器（Key 与提示词同侧）；本地不需要，保留空实现兼容调用点。"""
    return {}


def match_columns(headers, keep_columns, session=None, timeout=LLM_TIMEOUT):
    """经 own() 调服务器语义匹配端点：{表头: 保留字段原文 or ""}。

    防幻觉名单过滤在服务器完成（名单外值→空）；离线/未配置抛 LLMError，由调用方
    转成可展示提示（分析任务内自动回退硬过滤档）。
    """
    if not headers:
        raise LLMError("表格字段为空，无法匹配")
    keep = [str(k).strip() for k in (keep_columns or []) if str(k).strip()]
    if not keep:
        raise LLMError("保留字段名单为空，无法匹配（请先在「⚙️ 配置」中设定保留字段）")
    try:
        r = _own().post("/api/trajectory-sketch/llm/match-columns",
                        json={"headers": [str(h) for h in headers], "keep_columns": keep},
                        timeout=timeout)
    except Exception as exc:
        invalidate()
        raise LLMError("服务器不可达（大模型辅助需联网）：%s" % type(exc).__name__)
    if r.status_code == 409:
        d = r.json() or {}
        raise LLMError(str(d.get("error") or "服务器端大模型未配置"))
    if r.status_code != 200:
        raise LLMError("大模型匹配失败（HTTP %s）" % r.status_code)
    d = r.json() or {}
    m = d.get("matches")
    if not isinstance(m, dict):
        raise LLMError("服务器返回缺少 matches 对象")
    return {str(h): str(m.get(str(h)) or "") for h in headers}
