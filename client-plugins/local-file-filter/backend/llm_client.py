# -*- coding: utf-8 -*-
"""llm_client —— 大模型字段匹配的客户端垫片（存量适配 T-O9-8）。

推理在服务器工件端点 ``POST /api/file-filter/llm/match-columns``（jz_llm + Key 不出
服务器），本模块经 jz_relay.own() 调用并回传 {表头: 匹配字段 or ""}——与服务器
llm_client.match_columns 同形。服务器不可达/未配置时抛 LLMError，调用方按既有
降级口径处理（mode=llm 在会话未配置时已自动回退 hard）。
"""
import jz_relay

LLM_TIMEOUT = 60
PROMPT_VER = 0                # 版本记账在服务器端点内
RECHECK_PROMPT_VER = 0
RECHECK_BATCH_SIZE = 20
REQUESTS_AVAILABLE = True     # 客户端运行时自带 requests（/status 自检口径）


class LLMError(Exception):
    pass


def _own():
    return jz_relay.own("local-file-filter")


def default_prompt():
    return {}                  # 提示词归服务器（jz_llm.load_prompt 于端点内取用）


def match_columns(headers, keep_columns, session=None, timeout=LLM_TIMEOUT, samples=None):
    headers = [str(h) for h in (headers or [])]
    if not headers:
        raise LLMError("表格字段为空，无法匹配")
    if not [k for k in (keep_columns or []) if str(k).strip()]:
        raise LLMError("保留字段名单为空，无法匹配（请先在管理配置中设定保留字段）")
    try:
        r = _own().post("/api/file-filter/llm/match-columns", timeout=int(timeout) + 30, json={
            "headers": headers,
            "keep_columns": [str(k) for k in (keep_columns or [])],
            "samples": samples or None,
        })
    except Exception as exc:
        raise LLMError("服务器暂不可用，语义过滤降级：%s" % type(exc).__name__)
    d = r.json() or {}
    if r.status_code != 200:
        raise LLMError(str(d.get("error") or "服务器大模型匹配失败（%s）" % r.status_code))
    return d.get("matches") or {}


def recheck_mappings(*_a, **_k):
    raise LLMError("映射复核在服务器端执行（/mappings/recheck 已透传，本地不再内联调用）")
