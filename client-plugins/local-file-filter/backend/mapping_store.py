# -*- coding: utf-8 -*-
"""mapping_store —— 自学习映射库的客户端垫片（存量适配 T-O9-8）。

过滤流的三档查表（confirmed → exact → suggested）经 jz_relay.own() 到服务器工件端点
``POST /api/file-filter/mappings/lookup``（键归一化与歧义消解全在服务器）；映射库的
**写**（建议落库/确认/否决/投票/复核）全部留在服务器——单一写方纪律不变，本模块不落盘。

配对契约（勿单独使用）：
- ``record_suggestions`` / ``bump_stats`` 为 no-op——服务器
  ``/llm/match-columns`` 端点已代为落库计数；
- ``is_rejected`` 恒 False——该端点返回前已按 valid_targets 与否决表预过滤，
  本地拿到的建议即"干净可用"。
"""
import jz_relay

MAX_KEY_LEN = 200
STATUS_SUGGESTED = "suggested"
STATUS_CONFIRMED = "confirmed"
STATUS_REMAP = "remap"


def norm_key(s):
    return str(s or "").strip()


def compact_key(s):
    return norm_key(s)


def _own():
    return jz_relay.own("local-file-filter")


class MappingStore:
    """服务器映射库的本地视图（只读查表 + 建议回传由服务器端点代写）。"""

    def __init__(self, path=None):
        self._path = None                      # 兼容签名；本地不落盘

    def lookup(self, header, valid_targets, include_suggested=True, tiers="all"):
        try:
            r = _own().post("/api/file-filter/mappings/lookup", timeout=30, json={
                "headers": [header],
                "valid_keep": sorted(valid_targets or []),
                "include_suggested": bool(include_suggested),
                "tiers": tiers if tiers in ("all", "confirmed", "suggested") else "all",
            })
        except Exception:
            return None                        # 服务器不可达 → 未命中（本地精确/正则档照常）
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("mappings", {}).get(str(header))

    def lookup_remap(self, header):
        try:
            r = _own().post("/api/file-filter/mappings/lookup", timeout=30, json={
                "headers": [header], "remap": True})
        except Exception:
            return None
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("mappings", {}).get(str(header))

    def is_rejected(self, header, target):
        return False                           # 见模块头"配对契约"

    def record_suggestions(self, *args, **kwargs):
        pass                                   # 服务器 /llm/match-columns 已落库

    def bump_stats(self, **counters):
        pass                                   # 统计记账在服务器侧
