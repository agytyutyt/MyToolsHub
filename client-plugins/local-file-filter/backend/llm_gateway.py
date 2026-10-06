# -*- coding: utf-8 -*-
"""llm_gateway —— jz_llm 的客户端垫片（存量适配 T-O9-8，C 类客户端工件专用）。

服务器侧统一大模型（jz_llm）的能力经 jz_relay.own() 触达：本模块只回答
「能不能用」，推理与建议匹配全部发生在服务器工件端点（/llm/match-columns），
Key 不落本机（红线 5 / L-10）。缓存 60s，离线时按"不可用"降级。
"""
import time

_CACHE = {"ts": 0.0, "ok": False, "reason": "尚未探测"}


def _own():
    import jz_relay
    return jz_relay.own("local-file-filter")


class GatewaySession:
    """jz_llm.resolve() 返回形态的最小兼容面（configured/provider/reason/source）。"""

    def __init__(self, ok, reason):
        self.provider = {}
        self.reason = reason
        self.source = "server"
        self._ok = ok

    def configured(self):
        return self._ok

    def source_label(self):
        return "服务器端统一大模型"


def invalidate():
    """服务器语义段连接失败时调用：立即使可用性缓存失效（后续请求在入口即降级）。"""
    _CACHE.update(ts=0.0, ok=False, reason="服务器暂不可用")


def resolve(plugin_id=""):
    if time.time() - _CACHE["ts"] >= 60:
        try:
            r = _own().get("/api/file-filter/status", timeout=15)
            d = r.json() or {}
            ok = bool(d.get("llm_configured"))
            _CACHE.update(ts=time.time(), ok=ok,
                          reason="" if ok else str(d.get("reason") or "服务器端大模型未配置"))
        except Exception as exc:
            _CACHE.update(ts=time.time(), ok=False,
                          reason="服务器暂不可用，本地部分可继续（%s）" % type(exc).__name__)
    return GatewaySession(_CACHE["ok"], _CACHE["reason"])


def test_connection(plugin_id=""):
    _CACHE["ts"] = 0.0
    s = resolve(plugin_id)
    return s.configured(), (s.reason or "服务器端统一大模型可用")


def load_prompt(plugin_id="", default=None):
    return default if isinstance(default, dict) else {}


def save_prompt(*_a, **_k):
    pass
