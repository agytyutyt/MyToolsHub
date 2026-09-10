"""文件过滤器 —— OpenAI 兼容大模型客户端与字段语义匹配。

可对接任何暴露 /chat/completions 接口的模型服务（OpenAI / DeepSeek / 通义千问 /
Kimi / 智谱 / 本地 Ollama 等），与 case-report / character-graph 插件同一模式。

核心入口 match_columns()：给定表格全部表头与希望保留的字段名单，
由大模型判断每个表头与名单中哪个字段语义关联（如「时间」↔「开始时间」），
返回 {表头: 匹配到的保留字段 or ""}。
"""

import json
import re

import requests

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"

LLM_TIMEOUT = 120


class LLMError(Exception):
    """大模型调用相关异常，错误信息直接展示给前端用户。"""


def parse_model_output(text):
    """从模型回复中解析 JSON 对象，容忍 markdown 代码围栏。"""
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


def build_chat_url(base_url):
    """根据 base_url 拼出 chat/completions 完整地址。"""
    base = (base_url or DEFAULT_BASE_URL).strip()
    if base.endswith("/chat/completions"):
        return base
    return base.rstrip("/") + "/chat/completions"


def chat(base_url, api_key, model, system, user, temperature=0.1, timeout=LLM_TIMEOUT):
    """发送一次 chat/completions 请求，返回文本回复。"""
    url = build_chat_url(base_url)
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
            detail = json.dumps(resp.json(), ensure_ascii=False)[:300]
        except Exception:
            detail = (resp.text or "")[:300]
        raise LLMError(f"大模型接口返回错误（HTTP {resp.status_code}）：{detail}")

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("大模型返回结构异常，请检查模型名称是否正确")
    return content


def test_connection(base_url, api_key, model, timeout=15):
    """连通性测试，返回 (ok, detail)。"""
    url = build_chat_url(base_url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        return False, f"无法连接大模型服务，请检查网络与接口地址（{type(e).__name__}）"
    if resp.status_code >= 400:
        try:
            detail = json.dumps(resp.json(), ensure_ascii=False)[:300]
        except Exception:
            detail = (resp.text or "")[:300]
        hint = "（请检查 API Key）" if resp.status_code in (401, 403) else ""
        return False, f"接口返回错误（HTTP {resp.status_code}）{hint}：{detail}"
    try:
        data = resp.json()
        choices = data.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
    except (KeyError, TypeError, ValueError, IndexError):
        return False, "返回结构异常，请检查 base_url 是否为 OpenAI 兼容接口"
    if not message:
        return False, "返回结构异常：未包含 choices[].message"
    snippet = str(message.get("content") or message.get("reasoning_content") or "").strip()
    detail = "连通正常，模型可正常响应"
    if snippet:
        detail = f"连通正常，模型响应：{snippet[:40]}"
    return True, detail


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


def match_columns(headers, keep_columns, base_url, api_key, model, timeout=LLM_TIMEOUT):
    """调用大模型做字段语义匹配。

    headers: 表格表头列表；keep_columns: 需保留字段名单。
    返回 {表头: 匹配的保留字段原文 or ""}；
    模型返回了名单之外的值时按未匹配（""）处理，防幻觉映射。
    """
    if not headers:
        raise LLMError("表格字段为空，无法匹配")
    keep = [str(k).strip() for k in (keep_columns or []) if str(k).strip()]
    if not keep:
        raise LLMError("保留字段名单为空，无法匹配（请先在管理配置中设定保留字段）")
    user = (
        f"表格现有字段：{json.dumps(headers, ensure_ascii=False)}\n"
        f"需保留字段名单：{json.dumps(keep, ensure_ascii=False)}\n"
        "请按系统要求输出 JSON。"
    )
    data = parse_model_output(chat(base_url, api_key, model, MATCH_SYSTEM_PROMPT, user,
                                   temperature=0.1, timeout=timeout))
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
