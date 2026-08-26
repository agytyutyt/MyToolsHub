"""OpenAI 兼容的大模型客户端。

可对接任何暴露 /chat/completions 接口的模型服务：
OpenAI、DeepSeek、通义千问（兼容模式）、Kimi/Moonshot、智谱、
本地 Ollama 等。

调用入口为 extract_graph()：给定文档文本，返回 {characters, relationships}。
"""

import json
import re

try:
    import requests
    REQUESTS_AVAILABLE = True
except Exception:
    requests = None
    REQUESTS_AVAILABLE = False

# 默认接入地址与模型（未指定时使用）
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class LLMError(Exception):
    """大模型调用相关异常，错误信息直接展示给前端用户。"""
    pass


def parse_model_output(text: str) -> dict:
    """从模型回复中解析 JSON 对象，容忍 markdown 代码围栏。

    依次尝试：去掉 ``` 围栏后整体解析 → 正则抽取最外层大括号。
    """
    if not text:
        raise LLMError("大模型返回为空")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 兜底：从文本中抽取最外层 JSON 对象
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise LLMError("大模型返回的内容不是合法 JSON，请检查模型是否按格式输出")


def chat_json(base_url: str, api_key: str, model: str, system: str, user: str,
              temperature: float = 0.2, timeout: int = 300) -> dict:
    """发送一次 chat/completions 请求并解析 JSON 回复。

    base_url 允许以 /chat/completions 结尾（直接使用），否则自动拼接。
    """
    base = (base_url or DEFAULT_BASE_URL).strip()
    if base.endswith("/chat/completions"):
        url = base
    else:
        url = base.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "stream": False,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise LLMError(f"无法连接大模型服务，请检查网络与接口地址（{type(e).__name__}）") from e

    if resp.status_code >= 400:
        try:
            detail = resp.json()
            detail = json.dumps(detail, ensure_ascii=False)[:300]
        except Exception:
            detail = (resp.text or "")[:300]
        raise LLMError(f"大模型接口返回错误（HTTP {resp.status_code}）：{detail}")

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("大模型返回结构异常，请检查模型名称是否正确")
    return parse_model_output(content)


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


def extract_graph(text: str, base_url: str, api_key: str, model: str,
                  prompt=None) -> dict:
    """调用大模型抽取人物关系，返回 {"characters": [...], "relationships": [...]}。

    prompt 为可选的 prompt 配置字典：{"system": ..., "user_template": ...}。
    未提供时使用内置默认 prompt。user_template 中的 {document} 会被替换为文档文本。
    返回前会过滤掉引用了未出现人物的关系，保证数据自洽。
    """
    prompt = prompt or {}
    system = prompt.get("system") or DEFAULT_SYSTEM_PROMPT
    template = prompt.get("user_template") or DEFAULT_USER_TEMPLATE
    user = template.replace("{document}", text)
    data = chat_json(base_url, api_key, model, system, user)

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
