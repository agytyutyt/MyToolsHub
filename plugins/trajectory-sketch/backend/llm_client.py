# -*- coding: utf-8 -*-
"""轨迹速写 —— 大模型调用（主体模块 ``jz_llm`` 的薄适配层）。

为什么存在（2026-09-24 恢复）
---------------------------
本插件一度把「大模型辅助匹配（mode=llm）」整块移除，理由写在 ``filter_bridge`` 的模块头：
"该能力依赖「过滤器」插件自己的大模型配置"。**那个前提已经过期**——统一大模型模块
（主体模块 ``jz_llm``）落地后，接入信息（格式 / 地址 / Key / 模型）由管理员在
「管理后台 → 大模型设置」或用户自己的「⋯ → 大模型设置」维护，
``jz_llm.resolve()`` 的 ``plugin_id`` 只用于日志定位、**不参与解析**
（见 ``jz_llm`` 模块头 / 设计文档《统一大模型模块》）。

所以本插件既不需要「过滤器」插件的配置，也不产生跨插件耦合：``jz_llm`` 是框架 API 面
（``FRAMEWORK_MODULES`` 白名单内置放行，见 ``tools/build-plugin-package.ps1`` 的 C-4/C-11），
插件 import 它是**正规出口**（规范 B-7 的例外）。

本文件只负责两件插件自己的事：
1. **拟定提示词**（提示词归插件，见《统一大模型模块》§提示词归谁）；
2. **防幻觉约束**——模型返回值必须落在保留字段名单内，否则按"未匹配"处理。

核心入口 ``match_columns()``：给定表格全部表头与保留字段名单，由大模型判断每个表头与名单中
哪个字段语义关联（如「采集起始时刻」↔「开始时间」），返回 ``{表头: 匹配到的字段 or ""}``。
字面命中名单的列由调用方直接保留、不问模型（见 ``filter_local.filter_columns_llm``）。
"""

import json

import jz_llm

PLUGIN_ID = "trajectory-sketch"
LLM_TIMEOUT = 120

# 兼容既有调用点：各处按 ``llm_client.LLMError`` 捕获大模型异常
LLMError = jz_llm.LLMError

# 依赖可用性标记：统一大模型模块持有 requests 的可用性（本插件不直接 import requests）。
# ``/status`` 自检据此如实上报——缺 requests 时明确指向「依赖组件包」而不是笼统说失败。
REQUESTS_AVAILABLE = jz_llm.REQUESTS_AVAILABLE


# 系统提示词：把表格表头映射到保留字段名单
MATCH_SYSTEM_PROMPT = """你是数据合规助手。给定「表格现有字段」和「需保留字段名单」，
判断每个表格字段与名单中哪个字段语义相关联（名称不必完全一致，如「采集起始时刻」与「开始时间」关联；
「手机号」与「经度」不关联）。只输出一个 JSON，不要任何多余文字或代码块：

{"mappings": {"表格字段": "对应的名单字段或空字符串", ...}}

要求：
1. mappings 的键必须与表格现有字段逐一对应，一个不多一个不少。
2. 值只能取「需保留字段名单」中的原文，或空字符串（表示无关联、建议删除）。
3. 判断标准：两个字段指向同一类业务信息（含义相同、包含、同义、近义）即关联；
   仅字面相似但含义无关的不要关联。轨迹表常见语义对：时间类（开始时间/上报时间/时间）、
   号码类（用户号码/手机号/MSISDN）、位置类（经度/纬度/地址/位置区/小区号）。"""


# 用户消息模板：{headers} / {keep} 会被替换为实际字段清单（JSON 文本）
MATCH_USER_TEMPLATE = """表格现有字段：{headers}
需保留字段名单：{keep}
请按系统要求输出 JSON。"""


def default_prompt() -> dict:
    """本插件的内置缺省提示词（供 ``jz_llm.load_prompt`` 做回退）。"""
    return {"system": MATCH_SYSTEM_PROMPT, "user_template": MATCH_USER_TEMPLATE}


def available() -> bool:
    """大模型辅助是否可用（是否已配置接入信息）。

    ``jz_llm.resolve()`` 要读会话，**只能在请求线程内调用**；后台线程请改用
    ``session.configured()``（见 ``jz_llm`` 模块头的线程纪律）。
    """
    try:
        return bool(jz_llm.resolve(PLUGIN_ID).configured())
    except Exception:
        return False


def session():
    """取当前生效的接入配置快照（**只能在请求线程内调用**，取到后传进后台任务）。"""
    return jz_llm.resolve(PLUGIN_ID)


def match_columns(headers, keep_columns, session=None, timeout=LLM_TIMEOUT):
    """调用统一大模型做字段语义匹配。

    headers: 表格表头列表；keep_columns: 需保留字段名单（本插件传的是**别名全集**，
        见 ``config_store.expand_keep_columns``）。
    session: 由调用方在**请求线程**内 ``jz_llm.resolve(PLUGIN_ID)`` 取得后传入
        （后台线程读不到会话，见 ``jz_llm`` 模块头的线程纪律）。
    返回 ``{表头: 匹配的保留字段原文 or ""}``；
    模型返回了名单之外的值时按未匹配（``""``）处理，防幻觉映射。
    """
    if not headers:
        raise LLMError("表格字段为空，无法匹配")
    keep = [str(k).strip() for k in (keep_columns or []) if str(k).strip()]
    if not keep:
        raise LLMError("保留字段名单为空，无法匹配（请先在「⚙️ 配置」中设定保留字段）")
    prompt = jz_llm.load_prompt(PLUGIN_ID, default_prompt())
    user = jz_llm.render(prompt.get("user_template") or MATCH_USER_TEMPLATE, {
        "headers": json.dumps([str(h) for h in headers], ensure_ascii=False),
        "keep": json.dumps(keep, ensure_ascii=False),
    })
    data = jz_llm.chat_json(prompt.get("system") or MATCH_SYSTEM_PROMPT, user,
                            session=session, plugin_id=PLUGIN_ID,
                            temperature=0.1, timeout=timeout)
    mappings = data.get("mappings") if isinstance(data, dict) else None
    if not isinstance(mappings, dict):
        raise LLMError("大模型输出缺少 mappings 对象")
    keep_set = {k.casefold(): k for k in keep}
    out = {}
    for h in headers:
        key = str(h)
        val = mappings.get(key, mappings.get(key.strip(), ""))
        val = str(val or "").strip()
        out[key] = keep_set.get(val.casefold(), "")
    return out
