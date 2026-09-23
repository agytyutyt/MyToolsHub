"""人物星图 —— 大模型调用（统一大模型模块的薄适配层）。

统一大模型（框架模块 ``jz_llm``）落地后，本文件**不再自带 HTTP 调用与 API Key**：
接入配置（格式 / 地址 / Key / 模型）由 jz_llm 按「管理员统一配置」或「用户各自设置」
解析，本插件只负责**拟定并保存提示词**，把提示词与参数推给 jz_llm，再把结果回推业务
逻辑（见 docs/design/统一大模型模块-设计文档.md）。

保留本模块的理由：① 插件内既有调用点语义不变；② 人物/关系提示词与"关系必须引用已识别
人物"这类**领域约束**属于本插件，不该塞进框架。
"""

import jz_llm

PLUGIN_ID = "character-graph"

# 兼容既有调用点：插件各处按 llm_client.LLMError 捕获大模型异常
LLMError = jz_llm.LLMError

# 依赖标记：统一大模型模块持有 requests 的可用性（插件不再直接 import requests），
# 这里转发一份供 /status 自检与 jz_deps 刷新沿用（标记名不变，前端零改动）。
REQUESTS_AVAILABLE = jz_llm.REQUESTS_AVAILABLE


# 系统提示词：约束大模型只输出人物与关系的 JSON
DEFAULT_SYSTEM_PROMPT = """你是一名专业的办案辅助分析助手。用户会给你一份口供笔录（或案卷材料），请从中提取人物以及人物之间的关系。

要求：
1. 只提取材料中真实出现的人物（包括有名有姓的，以及有明确指代的重要角色）。
2. 为每个人物给出简短描述和身份/角色标签。
3. 提取人物之间的关系，关系必须基于笔录内容，类型应覆盖普通关系（亲属、朋友、敌对、同事、上下级、借贷、纠纷、爱慕、恩怨等）以及案事件关系（同案、共犯、上下线、受害人与嫌疑人、知情人、目击者、窝藏、销赃等）。
4. 涉及案事件的关联时，type 使用案事件关系类型；如能明确，可在 description 中简要说明所涉案事件。
5. 关系强度 strength 取 1-10 的整数，数值越大代表关系越紧密/重要。
6. 你只能输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown 代码块。

输出 JSON 格式如下：
{
  "characters": [
    {"name": "人物名", "description": "一句话描述（可注明案事件角色）", "tags": ["标签1", "标签2"]}
  ],
  "relationships": [
    {"source": "人物A", "target": "人物B", "type": "同案", "strength": 9},
    {"source": "人物A", "target": "人物C", "type": "受害人与嫌疑人", "strength": 8}
  ]
}
"""

# 用户消息模板：{document} 会被替换为实际文档文本
DEFAULT_USER_TEMPLATE = """以下是口供笔录/案卷文档内容（可能被截断，忽略无关内容）：

{document}

请按照要求的 JSON 格式输出人物及其关系。"""


def default_prompt() -> dict:
    """本插件的内置缺省提示词（供 jz_llm.load_prompt 做回退）。"""
    return {"system": DEFAULT_SYSTEM_PROMPT, "user_template": DEFAULT_USER_TEMPLATE}


def extract_graph(text: str, prompt=None, session=None) -> dict:
    """调用统一大模型抽取人物关系，返回 {"characters": [...], "relationships": [...]}。

    prompt 为可选的 prompt 配置字典：{"system": ..., "user_template": ...}；
    未提供时使用内置默认。user_template 中的 {document} 会被替换为文档文本。
    session 由调用方在**请求线程**内 jz_llm.resolve(PLUGIN_ID) 取得后传入。
    返回前会过滤掉引用了未出现人物的关系，保证数据自洽。
    """
    prompt = prompt or {}
    system = prompt.get("system") or DEFAULT_SYSTEM_PROMPT
    template = prompt.get("user_template") or DEFAULT_USER_TEMPLATE
    user = jz_llm.render(template, {"document": text})
    data = jz_llm.chat_json(system, user, session=session,
                            plugin_id=PLUGIN_ID, timeout=300)

    characters = data.get("characters") or []
    relationships = data.get("relationships") or []
    if not characters:
        raise LLMError("大模型未提取到任何人物，请确认文档包含人物内容")

    # 只保留 source / target 均为已识别人物的关系，且不允许自指
    names = {c.get("name", "").strip() for c in characters}
    valid_rels = []
    for r in relationships:
        src = (r.get("source") or "").strip()
        tgt = (r.get("target") or "").strip()
        if src in names and tgt in names and src != tgt:
            valid_rels.append(r)
    return {"characters": characters, "relationships": valid_rels}
