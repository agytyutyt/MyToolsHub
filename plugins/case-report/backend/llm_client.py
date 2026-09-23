"""战果录入 —— 大模型调用（统一大模型模块的薄适配层）。

统一大模型（框架模块 ``jz_llm``）落地后，本文件**不再自带 HTTP 调用与 API Key**：
接入配置（格式 / 地址 / Key / 模型）由 jz_llm 按「管理员统一配置」或「用户各自设置」
解析，本插件只负责**拟定并保存提示词**，把提示词与参数推给 jz_llm，再把结构化结果
回推业务逻辑（见 docs/design/统一大模型模块-设计文档.md）。

保留本模块的理由：① 插件内既有调用点语义不变，改动面最小；② 五要素提示词与
"输出必须是键值对 JSON"这类**领域约束**属于本插件，不该塞进框架。
"""

import jz_llm

PLUGIN_ID = "case-report"

# 兼容既有调用点：插件各处按 llm_client.LLMError 捕获大模型异常
LLMError = jz_llm.LLMError

# 依赖标记：统一大模型模块持有 requests 的可用性（插件不再直接 import requests），
# 这里转发一份供 /status 自检与 jz_deps 刷新沿用（标记名不变，前端零改动）。
REQUESTS_AVAILABLE = jz_llm.REQUESTS_AVAILABLE


# 系统提示词：只输出五要素键值对 JSON（含缴获物品逐项明细、涉案价值）
DEFAULT_SYSTEM_PROMPT = """你是一名公安战果录入助手。根据收网情况简报，只输出一个 JSON，不要任何多余文字或代码块：

{
  "案件名": "如：8·16 系列盗窃案",
  "时间": "行动时间，如：2026年8月20日",
  "主办大队": "只能是：一大队、二大队、三大队（从原文识别），不确定则为空字符串",
  "抓获人数": "纯数字人数，如：5",
  "涉案价值": "涉案金额/案值，原文有则填（如：80万元），没有则为空字符串",
  "缴获物品": "涉案物品简列，顿号分隔，如：冰毒500克、手机6部",
  "缴获物品明细": [
    {"名称": "冰毒", "类别": "毒品", "数量": 500, "单位": "克"},
    {"名称": "手机", "类别": "手机", "数量": 6, "单位": "部"}
  ]
}

关键要求：
1. 只依原文，不编造；缺失字段填空字符串；"抓获人数" 用阿拉伯数字。
2. "主办大队" 只能从 一大队/二大队/三大队 中取一个，识别不出填空字符串。
3. "缴获物品" 逐项列出数量与单位，便于阅读。
4. 【重要】"缴获物品明细" 必须为数组，且每一项必须包含 名称、类别、数量、单位 四个字段。
   "缴获物品明细" 中的每一项必须与 "缴获物品" 中列出的物品一一对应，不可遗漏。
   数量为数字（如 500、6），无具体数量可填 null；类别一到三个字。
   如果原文有缴获物品，则 "缴获物品明细" 不能为空，必须逐项列出。"""


# 用户消息模板：{text} 会被替换为实际报告文本
DEFAULT_USER_TEMPLATE = """以下是收网情况简报：

{text}

请按系统要求输出 JSON。"""


def default_prompt():
    """本插件的内置缺省提示词（供 jz_llm.load_prompt 做回退）。"""
    return {"system": DEFAULT_SYSTEM_PROMPT, "user_template": DEFAULT_USER_TEMPLATE}


def extract_fields(text, prompt=None, session=None, timeout=120):
    """调用统一大模型抽取五要素，返回 {"案件名":..., "时间":..., "主办大队":...,
    "抓获人数":..., "涉案价值":..., "缴获物品":..., "缴获物品明细":[...]}。

    prompt 为可选的提示词配置：{"system": ..., "user_template": ...}；未提供时用内置默认。
    session 由调用方在**请求线程**内 jz_llm.resolve(PLUGIN_ID) 取得后传入——
    后台线程里读不到会话，不传会在"用户各自设置"模式下被当成未配置。
    """
    prompt = prompt or {}
    system = prompt.get("system") or DEFAULT_SYSTEM_PROMPT
    template = prompt.get("user_template") or DEFAULT_USER_TEMPLATE
    user = jz_llm.render(template, {"text": text or ""})
    data = jz_llm.chat_json(system, user, session=session,
                            plugin_id=PLUGIN_ID, timeout=timeout)
    if not isinstance(data, dict):
        raise LLMError("大模型输出的不是 JSON 键值对对象")
    return data
