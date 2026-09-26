"""文件过滤器 —— 大模型调用（统一大模型模块的薄适配层）。

统一大模型（框架模块 ``jz_llm``）落地后，本文件**不再自带 HTTP 调用与 API Key**：
接入配置（格式 / 地址 / Key / 模型）由 jz_llm 按「管理员统一配置」或「用户各自设置」
解析，本插件只负责**拟定并保存提示词**，把提示词与参数推给 jz_llm，再把结果回推业务
逻辑（见 docs/design/统一大模型模块-设计文档.md）。

核心入口 match_columns()：给定表格全部表头与希望保留的字段名单，由大模型判断每个表头
与名单中哪个字段语义关联（如「时间」↔「开始时间」），返回 {表头: 匹配到的保留字段 or ""}。

保留本模块的理由：① 插件内既有调用点语义不变；② "模型只能返回名单内的字段"这类
**防幻觉约束**属于本插件，不该塞进框架。
"""

import json

import jz_llm

PLUGIN_ID = "file-filter"
LLM_TIMEOUT = 120

# 内置提示词版本：随映射建议落库（model + prompt_ver），改提示词/换模型后可归因评测
PROMPT_VER = 1

# 兼容既有调用点：插件各处按 llm_client.LLMError 捕获大模型异常
LLMError = jz_llm.LLMError

# 依赖标记：统一大模型模块持有 requests 的可用性（插件不再直接 import requests），
# 这里转发一份供 /status 自检与 jz_deps 刷新沿用（标记名不变，前端零改动）。
REQUESTS_AVAILABLE = jz_llm.REQUESTS_AVAILABLE


# 系统提示词：把表格表头映射到保留字段名单
MATCH_SYSTEM_PROMPT = """你是数据合规助手。给定「表格现有字段」和「需保留字段名单」，
判断每个表格字段与名单中哪个字段语义相关联（名称不必完全一致，如「开始时间」与「时间」关联；
「身份证号」与「姓名」不关联）。只输出一个 JSON，不要任何多余文字或代码块：

{"mappings": {"表格字段": "对应的名单字段或空字符串", ...}}

要求：
1. mappings 的键必须与表格字段逐一对应，一个不多一个不少。
2. 值只能取「需保留字段名单」中的原文，或空字符串（表示无关联、建议删除）。
3. 判断标准：两个字段指向同一类业务信息（含义相同、包含、同义、近义）即关联；
   仅字面相似但含义无关的不要关联。"""


# 用户消息模板：{headers} / {keep} 会被替换为实际字段清单（JSON 文本）
MATCH_USER_TEMPLATE = """表格现有字段：{headers}
需保留字段名单：{keep}
请按系统要求输出 JSON。"""


def default_prompt() -> dict:
    """本插件的内置缺省提示词（供 jz_llm.load_prompt 做回退）。"""
    return {"system": MATCH_SYSTEM_PROMPT, "user_template": MATCH_USER_TEMPLATE}


def match_columns(headers, keep_columns, session=None, timeout=LLM_TIMEOUT):
    """调用统一大模型做字段语义匹配。

    headers: 表格表头列表；keep_columns: 需保留字段名单。
    session: 由调用方在**请求线程**内 jz_llm.resolve(PLUGIN_ID) 取得后传入
        （后台线程读不到会话，见 jz_llm 模块头部的线程纪律）。
    返回 {表头: 匹配的保留字段原文 or ""}；
    模型返回了名单之外的值时按未匹配（""）处理，防幻觉映射。
    """
    if not headers:
        raise LLMError("表格字段为空，无法匹配")
    keep = [str(k).strip() for k in (keep_columns or []) if str(k).strip()]
    if not keep:
        raise LLMError("保留字段名单为空，无法匹配（请先在管理配置中设定保留字段）")
    prompt = jz_llm.load_prompt(PLUGIN_ID, default_prompt())
    user = jz_llm.render(prompt.get("user_template") or MATCH_USER_TEMPLATE, {
        "headers": json.dumps(headers, ensure_ascii=False),
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
        val = mappings.get(h, mappings.get(str(h).strip(), ""))
        val = str(val or "").strip()
        out[h] = keep_set.get(val.casefold(), "")
    return out
