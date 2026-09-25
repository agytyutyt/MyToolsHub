"""JZToolsHub 统一大模型模块 —— 主体对插件承诺的稳定接口层（FC-5）。

为什么存在
    「战果录入 / 人物星图 / 过滤器」三个插件各自复制了一份 llm_client.py：HTTP 调用、
    JSON 解析、连通测试几乎逐行相同，API Key 也各存各的——插件之间禁止互相 import
    （规范 B-7 / 设计文档 D-9），于是只能复制，三处口径、三处漏改。本模块把这部分能力
    **提升为主体模块**：插件只"拟定并保存提示词"，把提示词与相关参数推给本模块，
    由本模块完成与模型服务的交互，再把结果回推给插件。

两级配置 · 两种模式（管理员在「管理后台 → 大模型设置」中二选一）
    模式 admin（管理员统一配置）：所有调用都用管理员维护的全局配置；用户看不到自助入口。
    模式 user （用户各自设置）  ：优先用用户自己的配置（首页右下角 ⋯ →「大模型设置」）；
                                 用户没配、且 fallback=true 时回退到全局配置。

配置落盘位置
    全局：<数据根>/config/llm.json —— 本模块所有；api_key 用 config/.admin_key 加密。
    用户：<数据根>/config/admin.json 中每个账号的 llm 字段 —— 账号数据归 admin 插件管，
          本模块经 jz_api 的 provider 取用，**不另立一份**，避免同一用户两处配置不一致。

调用格式（format）
    openai / anthropic / ollama / custom 四种。**地址一律填完整接口地址**
    （OpenAI 兼容要填到 .../chat/completions），本模块不做任何路径拼接——
    "base_url 还是完整地址"两种口径混用是历史踩坑点（少写 /v1、多写一次
    chat/completions 都会 404，且报错信息与真实原因无关）。

线程纪律（务必遵守）
    resolve() 要读会话（当前登录用户），只能在**请求线程**内调用。后台线程（插件的
    线程池任务）里读不到会话，会静默降级成"未登录"，在"用户各自设置"模式下表现为
    "未配置大模型"这种莫名其妙的失败。正确用法：
        请求线程里 session = jz_llm.resolve("my-plugin")，把 session 传进后台任务，
        后台任务里 jz_llm.chat(..., session=session)。

纪律（与 jz_api.py / jztools_data.py 相同，务必遵守）
    仅标准库（外加可选依赖 requests）、导入无副作用、**不得 import 任何插件**。
    本模块被 app.py 与各插件后端共同 import；插件侧用法见 `插件设计规范.md` §9.4。
"""

import json
import logging
import os
import re
import threading

import jz_api
import jztools_data

log = logging.getLogger("jztools.llm")

# requests 是**可选**依赖（由「依赖组件包」按需安装）：必须保护性导入——
# 裸 import 会让缺它时整个插件后端 ModuleNotFoundError，插件直接加载失败，
# 与"可选依赖缺失 → 仍加载并标注功能降级"的设计相悖（真机实测踩到）。
# 装了组件后无需重启即可生效：app.py 注册了 jz_deps.install_refresher(app, jz_llm)。
try:
    import requests
    REQUESTS_AVAILABLE = True
except Exception:  # pragma: no cover - 干净机未装依赖组件时
    requests = None
    REQUESTS_AVAILABLE = False


# ===================== 调用格式 =====================

DEFAULT_FORMAT = "openai"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TIMEOUT = 120
DEFAULT_ANTHROPIC_MAX_TOKENS = 4096

# 每种格式的展示文案与地址样例：后台/悬浮卡片直接取用，避免两处前端各写一份。
FORMAT_META = {
    "openai": {
        "label": "OpenAI 兼容（chat/completions）",
        "url_hint": "https://api.deepseek.com/chat/completions",
        "key_required": True,
        "desc": "适用于 OpenAI、DeepSeek、通义千问（兼容模式）、Kimi、智谱、内网 vLLM 等一切"
                "暴露 chat/completions 的服务。地址要填到 chat/completions。",
    },
    "anthropic": {
        "label": "Anthropic Messages（/v1/messages）",
        "url_hint": "https://api.anthropic.com/v1/messages",
        "key_required": True,
        "desc": "Claude 官方 Messages 接口：system 单独成字段、认证头为 x-api-key。"
                "地址要填到 /v1/messages。",
    },
    "ollama": {
        "label": "Ollama 本地（/api/chat）",
        "url_hint": "http://127.0.0.1:11434/api/chat",
        "key_required": False,
        "desc": "本机或内网 Ollama 服务，默认无需 API Key。地址要填到 /api/chat。",
    },
    "custom": {
        "label": "自定义（请求头 / 请求体模板）",
        "url_hint": "https://内网网关/v1/chat",
        "key_required": False,
        "desc": "接口形态与上述都不同时使用：自行填写请求头与请求体模板，"
                "并指定从响应的哪个路径取正文。模板占位符："
                "{{model}} {{system}} {{user}} {{temperature}} {{max_tokens}} {{api_key}} "
                "{{messages_json}}。",
    },
}

FORMATS = tuple(FORMAT_META.keys())

# 模板占位符：{{name}}（双花括号，避免与 JSON 自身的 {} 冲突）
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


# ===================== 异常 =====================

class LLMError(Exception):
    """大模型调用相关异常，错误信息面向最终用户，可直接展示。"""
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


def mask_key(value):
    """API Key 掩码（回传前端用）：只表明"配没配"，不泄漏任何字符。"""
    return "••••••••" if (value or "").strip() else ""


def is_mask(value):
    """判断前端回传的是否为掩码（掩码/留空都表示"不修改已保存的 Key"）。"""
    v = (value or "").strip()
    return (not v) or v == "••••••••"


# ===================== 配置：全局（<数据根>/config/llm.json） =====================

# 全局配置文件的密钥复用 admin 的 config/.admin_key：
# 一把钥匙管本机所有落盘密文（admin.json 的密码/身份证/用户 Key 与 llm.json 的全局 Key），
# 少一把钥匙就少一处备份遗漏。本模块与 admin 共用同一把，只在文件缺失时按同一格式
# 生成、不参与它的轮换。
_KEY_FILE_PARTS = ("config", ".admin_key")

_fernet_lock = threading.Lock()
_fernet = None


def _key_file():
    return jztools_data.get_data_root_file(*_KEY_FILE_PARTS)


def _get_fernet():
    """懒加载 Fernet 实例（密钥文件 config/.admin_key，与 admin 插件共用）。"""
    global _fernet
    if _fernet is not None:
        return _fernet
    with _fernet_lock:
        if _fernet is not None:
            return _fernet
        from cryptography.fernet import Fernet  # 仅此处需要，避免无谓的导入开销
        path = _key_file()
        if os.path.isfile(path):
            with open(path, "rb") as f:
                key = f.read().strip()
        else:
            key = Fernet.generate_key()
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(key)
            os.replace(tmp, path)
        _fernet = Fernet(key)
    return _fernet


def encrypt_secret(value):
    """加密敏感字段；空值原样返回空串（未设置就是空串，不产生密文）。"""
    if value is None or value == "":
        return ""
    return _get_fernet().encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt_secret(token):
    """解密敏感字段；无法解密（历史明文 / 手工编辑）时原样返回，兼容迁移。"""
    if not token:
        return ""
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        return token


def config_path():
    """全局配置文件路径（每次现取，运行期切换数据根后立即生效）。"""
    return jztools_data.get_data_root_file("config", "llm.json")


def default_global():
    """全局配置默认值（缺键补齐的模板）。"""
    return {
        "mode": "admin",       # admin = 管理员统一配置；user = 用户各自设置
        "fallback": True,      # user 模式下，用户未配置时是否回退到全局配置
        "provider": {
            "format": DEFAULT_FORMAT,
            "url": "",
            "api_key": "",
            "model": "",
            "temperature": None,
            "max_tokens": None,
            "timeout": DEFAULT_TIMEOUT,
            "headers": {},
            "body": "",
            "text_path": "",
            "error_path": "",
        },
    }


def normalize_provider(raw):
    """把任意来源（配置文件 / 请求体 / 用户账号）的接入配置规整成统一结构。

    只做"取值 + 边界钳制"，不做合法性拒绝——半填的配置也要能原样回显给用户继续编辑。
    """
    raw = raw if isinstance(raw, dict) else {}
    fmt = str(raw.get("format") or DEFAULT_FORMAT).strip().lower()
    if fmt not in FORMAT_META:
        fmt = DEFAULT_FORMAT

    def _num(key, lo, hi, cast, positive=False):
        """取数值并钳到合法区间。

        positive=True 时 0/负数视为"未设置"（返回 None）而不是钳到下限：
        max_tokens 填 0 或 -5 若钳成 1，会让模型只输出一个 token 而用户看不出原因，
        按"未设置、用服务默认"处理更安全。超出上限则钳到上限（temperature 填 99
        的用户本意就是"最大随机性"）。
        """
        val = raw.get(key)
        if val is None or val == "":
            return None
        try:
            num = cast(val)
        except (TypeError, ValueError):
            return None
        if positive and num <= 0:
            return None
        return max(lo, min(hi, num))

    headers = raw.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    headers = {str(k)[:100]: str(v)[:2000] for k, v in headers.items() if str(k).strip()}

    return {
        "format": fmt,
        "url": str(raw.get("url") or "").strip()[:500],
        "api_key": str(raw.get("api_key") or "").strip()[:500],
        "model": str(raw.get("model") or "").strip()[:200],
        "temperature": _num("temperature", 0.0, 2.0, float),
        "max_tokens": _num("max_tokens", 1, 1_000_000, int, positive=True),
        "timeout": _num("timeout", 1, 3600, int, positive=True),
        "headers": headers,
        "body": str(raw.get("body") or "")[:20000],
        "text_path": str(raw.get("text_path") or "").strip()[:200],
        "error_path": str(raw.get("error_path") or "").strip()[:200],
    }


def normalize_global(cfg):
    """规整整份全局配置（含模式与回退开关）。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    mode = str(cfg.get("mode") or "admin").strip().lower()
    if mode not in ("admin", "user"):
        mode = "admin"
    return {
        "mode": mode,
        "fallback": cfg.get("fallback") is not False,
        "provider": normalize_provider(cfg.get("provider")),
    }


def load_global():
    """读取全局配置（api_key 为**明文**，仅供本机调用与后台表单使用）。

    文件不存在/损坏时返回默认值（不落盘），由保存动作负责创建。
    """
    path = config_path()
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log.warning("读取全局大模型配置失败（%s）：%s", path, e)
            data = {}
    cfg = normalize_global(data)
    cfg["provider"]["api_key"] = decrypt_secret(cfg["provider"]["api_key"])
    return cfg


def save_global(cfg):
    """原子写入全局配置（api_key 落盘前加密）。"""
    cfg = normalize_global(cfg)
    stored = dict(cfg)
    stored["provider"] = dict(cfg["provider"])
    stored["provider"]["api_key"] = encrypt_secret(stored["provider"]["api_key"])
    path = config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stored, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return cfg


def public_global():
    """全局配置的对外形态（脱敏）：api_key 只回掩码，附 configured 结论。"""
    cfg = load_global()
    # 先在**明文**上判定可用性再掩码：掩码后 api_key 恒为真值，判定会失真
    usable = provider_usable(cfg["provider"])
    provider = dict(cfg["provider"])
    provider["api_key"] = mask_key(provider["api_key"])
    provider["configured"] = usable
    return {"mode": cfg["mode"], "fallback": cfg["fallback"], "provider": provider}


def mode():
    """当前模式：admin（管理员统一配置）/ user（用户各自设置）。"""
    return load_global()["mode"]


def user_mode():
    """是否允许用户自助配置（决定首页 ⋯ 菜单里是否出现「大模型设置」）。"""
    return mode() == "user"


# ===================== 可用性判定 =====================

def provider_usable(provider):
    """接入配置是否"可发起调用"：地址必填；需要 Key 的格式还必须有 Key。

    地址是硬门槛——没有地址无从发起请求。Key 按格式判定：
    OpenAI / Anthropic 少了它必然 401，判定为"未配置完"能更早给出可读提示；
    Ollama / 自定义（内网网关常无认证）不强制。
    """
    p = provider or {}
    if not (p.get("url") or "").strip():
        return False
    meta = FORMAT_META.get(p.get("format") or DEFAULT_FORMAT) or {}
    if meta.get("key_required") and not (p.get("api_key") or "").strip():
        return False
    return True


def provider_problem(provider):
    """返回"还差什么"的可读描述；已可用时返回空串。"""
    p = provider or {}
    if not (p.get("url") or "").strip():
        return "尚未填写 API 地址（需填完整接口地址）"
    meta = FORMAT_META.get(p.get("format") or DEFAULT_FORMAT) or {}
    if meta.get("key_required") and not (p.get("api_key") or "").strip():
        return "尚未填写 API Key"
    return ""


# ===================== 会话（一次调用要用的接入配置快照） =====================

class LLMSession:
    """一次调用要用的接入配置快照。

    **可跨线程传递**（后台任务里拿不到会话，必须靠它）：
        session = jz_llm.resolve("my-plugin")     # 请求线程内
        executor.submit(work, session)            # 交给后台线程
        jz_llm.chat(..., session=session)         # 后台线程内使用
    """

    __slots__ = ("provider", "source", "reason", "username")

    def __init__(self, provider=None, source="", reason="", username=""):
        self.provider = provider or {}
        self.source = source          # user | global | ""（未配置）
        self.reason = reason          # 未配置时的可读原因
        self.username = username      # 取到用户配置时的用户名（审计/排障用）

    def configured(self):
        return bool(self.source) and provider_usable(self.provider)

    def source_label(self):
        return {
            "user": "用户自己配置",
            "global": "管理员统一配置",
        }.get(self.source, "未配置")

    def public(self):
        """脱敏后的快照（前端展示用），不含 api_key 明文。"""
        p = dict(self.provider)
        p["api_key"] = mask_key(p.get("api_key"))
        return {
            "source": self.source,
            "source_label": self.source_label(),
            "configured": self.configured(),
            "reason": self.reason,
            "provider": p,
        }

    def __repr__(self):  # pragma: no cover - 仅排障
        return f"<LLMSession source={self.source!r} url={self.provider.get('url')!r}>"


def _user_provider(username):
    """取某个账号自己的接入配置（经 jz_api 的 provider 向 admin 插件要，明文）。"""
    if not username:
        return {}
    raw = jz_api.get_user_llm(username)
    if not isinstance(raw, dict):
        return {}
    return normalize_provider(raw)


def resolve(plugin_id=None):
    """解析当前生效的接入配置，返回 LLMSession（**只能在请求线程内调用**）。

    顺序：用户自己的配置（仅 user 模式）→ 全局配置（admin 模式，或 user 模式且允许回退）
          → 未配置（source 为空，reason 说明差什么）。

    plugin_id 仅用于日志与排障定位，不参与解析（历史兼容桥已于 2026-09-23 移除：
    插件侧不再保存任何接入信息，见 docs/design/数据迁移-设计文档.md 与
    admin 插件的 migrate_plugin_legacy_llm —— 它会把历史 Key 收编进统一配置再清除副本）。
    """
    cfg = load_global()
    user = jz_api.get_session_user()

    if cfg["mode"] == "user" and user:
        provider = _user_provider(user.get("username"))
        if provider_usable(provider):
            return LLMSession(provider, "user", username=user.get("username"))

    if cfg["mode"] == "admin" or cfg["fallback"]:
        if provider_usable(cfg["provider"]):
            return LLMSession(cfg["provider"], "global")

    if cfg["mode"] == "user":
        problem = provider_problem(_user_provider((user or {}).get("username") or ""))
        reason = (problem or "尚未配置大模型") + "：请点击主界面右下角「⋯」→「大模型设置」填写"
    else:
        reason = "尚未配置大模型：请联系管理员在「管理后台 → 大模型设置」中填写"
    return LLMSession({}, "", reason=reason)


# ===================== 请求构造与响应取值 =====================

def _render_placeholders(text, values):
    """替换 {{name}} 占位符。

    字符串值按 JSON 字面量**转义后**替换（转义引号/反斜杠/换行），
    因此模板里把占位符写在引号内即可，提示词里的换行与引号不会破坏 JSON。
    """
    def repl(match):
        name = match.group(1)
        if name not in values:
            return match.group(0)
        val = values[name]
        if isinstance(val, str):
            return json.dumps(val, ensure_ascii=False)[1:-1]
        return json.dumps(val, ensure_ascii=False)

    return _PLACEHOLDER_RE.sub(repl, text)


def _messages(system, user, messages):
    """统一成 OpenAI 风格的 messages 数组（anthropic/自定义模板各自再取用）。"""
    if messages:
        out = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "user")
            out.append({"role": role, "content": str(m.get("content") or "")})
        if out:
            return out
    out = []
    if system:
        out.append({"role": "system", "content": system})
    out.append({"role": "user", "content": user or ""})
    return out


def _build_request(provider, system, user, messages, timeout):
    """按格式构造请求，返回 (method, url, headers, body, text_paths, error_path)。"""
    fmt = provider.get("format") or DEFAULT_FORMAT
    url = (provider.get("url") or "").strip()
    key = (provider.get("api_key") or "").strip()
    model = (provider.get("model") or "").strip()
    temperature = provider.get("temperature")
    if temperature is None:
        temperature = DEFAULT_TEMPERATURE
    max_tokens = provider.get("max_tokens")
    msgs = _messages(system, user, messages)

    if fmt == "openai":
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        body = {"model": model, "messages": msgs, "temperature": temperature, "stream": False}
        if max_tokens:
            body["max_tokens"] = max_tokens
        return "POST", url, headers, body, [("choices", 0, "message", "content")], ""

    if fmt == "anthropic":
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if key:
            headers["x-api-key"] = key
        # Anthropic 的 system 是顶层字段，不能放进 messages
        body = {
            "model": model,
            "messages": [m for m in msgs if m.get("role") != "system"],
            "max_tokens": max_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS,
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        return "POST", url, headers, body, [("content", 0, "text")], ""

    if fmt == "ollama":
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        body = {
            "model": model,
            "messages": msgs,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if max_tokens:
            body["options"]["num_predict"] = max_tokens
        # /api/chat 取 message.content；/api/generate 取 response —— 两种都认
        return "POST", url, headers, body, [("message", "content"), ("response",)], ""

    # custom：请求头 / 请求体全部来自用户模板
    values = {
        "model": model,
        "system": system or "",
        "user": user or "",
        "api_key": key,
        "temperature": temperature,
        "max_tokens": max_tokens if max_tokens else 0,
        "messages_json": json.dumps(msgs, ensure_ascii=False),
    }
    headers = {"Content-Type": "application/json"}
    for name, tpl in (provider.get("headers") or {}).items():
        headers[name] = _render_placeholders(str(tpl), values)
    body_text = provider.get("body") or ""
    if not body_text.strip():
        raise LLMError("自定义格式缺少请求体模板（后台「大模型设置」→ 请求体模板）")
    rendered = _render_placeholders(body_text, values)
    try:
        body = json.loads(rendered)
    except json.JSONDecodeError as e:
        raise LLMError(f"自定义请求体模板不是合法 JSON（{e.msg}，位置 {e.pos}）；"
                       "字符串占位符请写在引号内，如 \"model\": \"{{model}}\"") from e
    text_path = _split_path(provider.get("text_path"))
    return "POST", url, headers, body, [tuple(text_path)] if text_path else [], \
        (provider.get("error_path") or "").strip()


def _split_path(path):
    """把 "choices.0.message.content" / "choices[0].message.content" 拆成路径段。"""
    if not path:
        return []
    text = str(path).replace("[", ".").replace("]", "")
    return [seg for seg in (p.strip() for p in text.split(".")) if seg]


def _extract(data, path):
    """按路径取值；取不到返回 None。

    路径段可以是字符串键，也可以是列表下标（既支持 "choices.0.message.content"，
    也支持内部直接给出的 ("choices", 0, "message", "content") 元组）。
    """
    cur = data
    for seg in path:
        if isinstance(cur, dict):
            cur = cur.get(seg if isinstance(seg, str) else str(seg))
        elif isinstance(cur, (list, tuple)):
            if isinstance(seg, int):
                idx = seg
            elif str(seg).isdigit():
                idx = int(seg)
            else:
                return None
            if idx < 0 or idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            return None
    return cur


def _extract_text(data, paths):
    """按候选路径依次取正文；都不中则返回 None。"""
    for path in paths:
        val = _extract(data, list(path))
        if val is None:
            continue
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)
        return str(val)
    return None


def _http_error_detail(resp, error_path=""):
    """HTTP >= 400 时的可读错误详情（截断，避免把整页 HTML 甩给用户）。"""
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "")[:300]
    if error_path:
        val = _extract(data, _split_path(error_path))
        if val is not None:
            return str(val)[:300]
    try:
        return json.dumps(data, ensure_ascii=False)[:300]
    except Exception:
        return str(data)[:300]


def _post(provider, system, user, messages, timeout):
    """发起一次请求，返回 (响应 JSON 或原文, 取到的正文文本)。"""
    if not REQUESTS_AVAILABLE:
        raise LLMError("缺少 requests：大模型功能不可用。请安装依赖组件包 "
                       "JZToolsHub-依赖-requests-v*.zip（解压后双击「安装依赖组件.bat」）后刷新页面（免重启）。")
    if not (provider.get("url") or "").strip():
        raise LLMError("尚未配置大模型 API 地址")

    method, url, headers, body, text_paths, error_path = _build_request(
        provider, system, user, messages, timeout)
    try:
        resp = requests.request(method, url, headers=headers, json=body, timeout=timeout)
    except Exception as e:  # requests.RequestException 及其子类；requests 缺失也已在上方拦掉
        raise LLMError(f"无法连接大模型服务，请检查网络与接口地址（{type(e).__name__}）") from e

    if resp.status_code >= 400:
        detail = _http_error_detail(resp, error_path)
        hint = "（请检查 API Key）" if resp.status_code in (401, 403) else ""
        raise LLMError(f"大模型接口返回错误（HTTP {resp.status_code}）{hint}：{detail}")

    try:
        data = resp.json()
    except Exception:
        # 非 JSON 响应：自定义格式允许直接把原文当正文（未配置取值路径时）
        text = (resp.text or "").strip()
        if text:
            return text, text
        raise LLMError("大模型返回内容为空或不是 JSON")

    text = _extract_text(data, text_paths)
    if text is None:
        where = "、".join(".".join(str(s) for s in p) for p in text_paths) or "（未配置取值路径）"
        raise LLMError(f"大模型返回结构异常：按 {where} 未取到正文，"
                       "请检查模型名称与调用格式是否正确")
    return data, text


# ===================== 对外调用接口 =====================

def chat(system=None, user=None, messages=None, *, session=None, plugin_id=None,
         model=None, temperature=None, max_tokens=None, timeout=None, json_mode=False):
    """把提示词与参数推给统一大模型模块，返回结构化结果。

    参数
        system / user : 系统提示词与用户消息（最常用）
        messages      : 需要多轮时直接给 messages 数组，替代 system/user
        session       : 由 resolve() 在请求线程内取得；后台线程**必须**显式传入
        plugin_id     : 本插件 id（日志与排障定位用）
        model / temperature / max_tokens / timeout : 单次调用覆盖配置里的默认值
        json_mode     : 为 True 时把正文按 JSON 解析，放进结果的 "json" 字段

    返回
        {"text": 正文, "json": 解析结果或 None, "usage": 用量, "format": 格式,
         "model": 实际模型, "source": 配置来源}
    """
    if session is None:
        session = resolve(plugin_id)
    if not session.configured():
        raise LLMError(session.reason or "尚未配置大模型")

    provider = dict(session.provider)
    if model:
        provider["model"] = model
    if temperature is not None:
        provider["temperature"] = temperature
    if max_tokens is not None:
        provider["max_tokens"] = max_tokens
    if timeout is None:
        timeout = provider.get("timeout") or DEFAULT_TIMEOUT

    data, text = _post(provider, system, user, messages, timeout)
    result = {
        "text": text,
        "json": parse_model_output(text) if json_mode else None,
        "usage": (data or {}).get("usage") if isinstance(data, dict) else None,
        "format": provider.get("format") or DEFAULT_FORMAT,
        "model": provider.get("model") or "",
        "source": session.source,
    }
    log.info("大模型调用完成：插件=%s 来源=%s 格式=%s 模型=%s 正文 %d 字",
             plugin_id or "-", session.source, result["format"], result["model"], len(text or ""))
    return result


def chat_text(system=None, user=None, messages=None, **kwargs):
    """调用大模型并只要正文文本（调用方自行解析，如表格字段匹配）。"""
    return chat(system, user, messages, **kwargs)["text"]


def chat_json(system=None, user=None, messages=None, **kwargs):
    """调用大模型并把正文解析成 JSON 对象。"""
    kwargs["json_mode"] = True
    return chat(system, user, messages, **kwargs)["json"]


def test_connection(session=None, provider=None, plugin_id=None, timeout=15):
    """连通性测试：发一条最小请求，验证地址 / Key / 模型是否可用。

    provider 非空时用它（后台/悬浮卡片里"测当前表单里填的这套"）；
    否则用 session（或现解析的）配置。返回 (ok, detail)，detail 面向用户可读。
    判定标准：HTTP 200 且按取值路径拿得到正文即视为连通——推理模型在 max_tokens
    较小时会把额度花在 reasoning 上，正文可能为空，故正文为空也判连通。
    """
    try:
        if provider is None:
            if session is None:
                session = resolve(plugin_id)
            if not session.configured():
                return False, session.reason or "尚未配置大模型"
            provider = session.provider
        provider = normalize_provider(provider)
        problem = provider_problem(provider)
        if problem:
            return False, problem
        if not REQUESTS_AVAILABLE:
            return False, ("缺少 requests：大模型功能不可用。请安装依赖组件包 "
                           "JZToolsHub-依赖-requests-v*.zip（解压后双击「安装依赖组件.bat」）后刷新页面（免重启）。")
        method, url, headers, body, text_paths, error_path = _build_request(
            provider, "", "ping", None, timeout)
        # 探测请求尽量小：限制输出长度，避免真的花掉一次完整推理
        if isinstance(body, dict):
            if provider.get("format") == "anthropic":
                body["max_tokens"] = 16
            elif provider.get("format") == "ollama":
                body.setdefault("options", {})["num_predict"] = 16
            else:
                body["max_tokens"] = 16
        try:
            resp = requests.request(method, url, headers=headers, json=body, timeout=timeout)
        except Exception as e:
            return False, f"无法连接大模型服务，请检查网络与接口地址（{type(e).__name__}）"
        if resp.status_code >= 400:
            detail = _http_error_detail(resp, error_path)
            hint = "（请检查 API Key）" if resp.status_code in (401, 403) else ""
            return False, f"接口返回错误（HTTP {resp.status_code}）{hint}：{detail}"
        try:
            data = resp.json()
        except Exception:
            if (resp.text or "").strip():
                return True, "连通正常（接口返回的不是 JSON，但已收到响应）"
            return False, "返回内容为空，请检查接口地址是否为完整的调用地址"
        text = _extract_text(data, text_paths)
        if text is None:
            where = "、".join(".".join(str(s) for s in p) for p in text_paths) or "（未配置取值路径）"
            return False, f"返回结构异常：按 {where} 未取到正文，请检查调用格式与模型名称"
        snippet = (text or "").strip()
        if snippet:
            return True, f"连通正常，模型响应：{snippet[:40]}"
        return True, "连通正常，模型可正常响应"
    except LLMError as e:
        return False, str(e)
    except Exception as e:  # 兜底：测试接口永远返回结论，不把异常抛给前端
        return False, f"测试失败（{type(e).__name__}）：{e}"


# ===================== 提示词：插件只"拟定并保存 prompt" =====================

def prompt_path(plugin_id):
    """插件的提示词文件路径：<数据根>/plugins/<id>/prompt.json。

    与既有两个插件的存放位置完全一致（不产生数据迁移）。
    """
    return jztools_data.get_data_root_file("plugins", str(plugin_id), "prompt.json")


def load_prompt(plugin_id, defaults=None):
    """读取插件提示词；文件缺失/损坏时回退到 defaults（插件内置的缺省提示词）。

    返回 dict，键由插件自定（惯例：system / user_template）。
    """
    merged = dict(defaults or {})
    path = prompt_path(plugin_id)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for key, val in saved.items():
                    if isinstance(val, str) and val.strip():
                        merged[str(key)] = val
        except Exception as e:
            log.warning("读取提示词失败（%s）：%s，已回退内置默认值", path, e)
    return merged


def save_prompt(plugin_id, prompt):
    """保存插件提示词（原子写）。只收字符串值，非字符串键值丢弃。"""
    clean = {str(k): str(v) for k, v in (prompt or {}).items() if isinstance(v, str)}
    path = prompt_path(plugin_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return clean


def render(template, values=None, **kwargs):
    """把模板里的 {name} 占位符替换为值（提示词用户消息的常规拼装方式）。

    只替换提供的键，未提供的占位符原样保留——避免把提示词里的其他花括号吃掉。
    """
    mapping = dict(values or {})
    mapping.update(kwargs)
    out = template or ""
    for key, val in mapping.items():
        out = out.replace("{" + str(key) + "}", "" if val is None else str(val))
    return out


# ===================== 给后台/悬浮卡片用的展示信息 =====================

def formats_public():
    """调用格式清单（标签 + 地址样例 + 是否必须 Key），供前端渲染下拉框。"""
    return [
        {
            "id": fid,
            "label": meta["label"],
            "url_hint": meta["url_hint"],
            "key_required": bool(meta["key_required"]),
            "desc": meta["desc"],
        }
        for fid, meta in FORMAT_META.items()
    ]
