"""战果录入 —— OpenAI 兼容的大模型客户端。

可对接任何暴露 /chat/completions 接口的模型服务：
OpenAI、DeepSeek、通义千问（兼容模式）、Kimi/Moonshot、智谱、本地 Ollama 等。

调用入口 extract_fields()：给定收网情况报告文本，
返回五要素键值对 JSON（案件名 / 时间 / 主办大队 / 抓获人数 / 缴获物品）。
"""

import json
import re

import requests

# 缺省接入地址与模型（未指定时使用）
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


class LLMError(Exception):
    """大模型调用相关异常，错误信息直接展示给前端用户。"""
    pass


def parse_model_output(text):
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
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise LLMError("大模型返回的内容不是合法 JSON，请检查模型是否按格式输出")


def chat_json(base_url, api_key, model, system, user, temperature=0.1, timeout=120):
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


# 系统提示词：只输出五要素键值对 JSON（含缴获物品逐项类别）
DEFAULT_SYSTEM_PROMPT = """你是一名公安战果录入助手。根据收网情况简报，提取五要素，只输出一个 JSON，不要任何多余文字或代码块：

{
  "案件名": "如：8·16 系列盗窃案",
  "时间": "行动时间，如：2026年8月20日",
  "主办大队": "主办单位，如：刑侦一支队",
  "抓获人数": "纯数字人数，如：5",
  "缴获物品": "涉案物品简列，顿号分隔，如：冰毒500克、手机6部",
  "物品分类": [{"名称": "冰毒", "类别": "毒品"}, {"名称": "手机", "类别": "手机"}]
}

要求：
1. 只依原文，不编造；缺失字段填空字符串；"抓获人数" 用阿拉伯数字。
2. "缴获物品" 逐项列出数量与单位，便于按项拆分。
3. "物品分类" 与缴获物品一一对应，类别一到三个字（如：毒品/手机/电脑/现金/电话卡），写不下就保留名称一致。"""


# 用户消息模板：{text} 会被替换为实际报告文本
DEFAULT_USER_TEMPLATE = """以下是收网情况简报：

{text}

请按系统要求输出 JSON。"""


def extract_fields(text, base_url, api_key, model, prompt=None, timeout=120):
    """调用大模型抽取五要素，返回 {"案件名":..., "时间":..., "主办大队":...,
    "抓获人数":..., "缴获物品":...}。

    prompt 为可选的提示词配置：{"system": ..., "user_template": ...}。
    未提供时使用内置默认 prompt。user_template 中的 {text} 会被替换为报告文本。
    """
    prompt = prompt or {}
    system = prompt.get("system") or DEFAULT_SYSTEM_PROMPT
    template = prompt.get("user_template") or DEFAULT_USER_TEMPLATE
    user = template.replace("{text}", text or "")
    data = chat_json(base_url, api_key, model, system, user, timeout=timeout)
    if not isinstance(data, dict):
        raise LLMError("大模型输出的不是 JSON 键值对对象")
    return data