"""统一大模型：框架级设置接口（Blueprint）。

从 app.py 拆出（2026-10-10 P2-4 / 报告 §4.4「LLM 路由」块，报告点名的 LLM Blueprint）。
主体对插件的稳定契约（FC-5，见 jz_llm.py）。三条接口都在**框架层**（不是某个插件），
因为设置入口在首页右下角 ⋯，属于外壳而非任何插件；账号数据经 jz_api 的 provider
向 admin 插件取用（依赖倒置，插件之间零依赖）。
登录拦截由 admin 的 _enforce_login 兜底（/api/llm/* 不在白名单）；
权限拦截不适用——该前缀不是已注册工具 ID，_enforce_tool_access 会放行。
"""
import functools

from flask import Blueprint, g, jsonify, request

import jz_api
import jz_llm

llm_bp = Blueprint("llm", __name__, url_prefix="/api/llm")


def require_login(fn):
    """框架级路由的登录门：未登录/会话不可读独立返回 401（不依赖 admin 兜底）。

    登录用户放进取 flask.g.current_user，视图函数直接取用（避免重复调用 provider）。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        info = jz_api.get_session_user()
        if info is None:
            return jsonify({"error": "未登录或登录已过期"}), 401
        g.current_user = info
        return fn(*args, **kwargs)
    return wrapper


@llm_bp.get("/settings")
@require_login
def api_llm_settings():
    """返回当前生效的大模型设置：模式、格式清单、本人配置（脱敏）与生效来源。

    悬浮卡片据此决定是否显示入口（user_mode 为假时整条入口不出现）、
    回显表单、并提示"当前实际生效的是哪一份配置"。
    """
    info = g.current_user
    user_mode = jz_llm.user_mode()
    username = info.get("username")
    own = jz_llm.normalize_provider(jz_api.get_user_llm(username) or {})
    session = jz_llm.resolve()
    jz_api.set_operation("查询大模型设置")
    return jsonify({
        "ok": True,
        "mode": jz_llm.mode(),
        "user_mode": user_mode,
        "can_self_config": user_mode,
        "fallback": jz_llm.load_global()["fallback"],
        "formats": jz_llm.formats_public(),
        "mine": {
            "format": own["format"],
            "url": own["url"],
            "api_key": jz_llm.mask_key(own["api_key"]),
            "api_key_set": bool(own["api_key"]),
            "model": own["model"],
            "configured": jz_llm.provider_usable(own),
            "problem": jz_llm.provider_problem(own),
        },
        "effective": session.public(),
    })


@llm_bp.post("/settings")
@require_login
def api_llm_settings_save():
    """保存**本人**的大模型接入配置（仅"用户各自设置"模式开放）。

    请求体：{format, url, api_key, model}；api_key 留空或回传掩码表示不修改已保存的 Key。
    管理员统一配置模式下直接 403——避免用户以为自己的设置生效了（实际被忽略）。
    """
    info = g.current_user
    if not jz_llm.user_mode():
        return jsonify({"error": "当前由管理员统一配置大模型，个人设置未开放"}), 403
    data = request.get_json(silent=True) or {}
    username = info.get("username")
    current = jz_llm.normalize_provider(jz_api.get_user_llm(username) or {})
    provider = jz_llm.normalize_provider(data)
    if jz_llm.is_mask(provider["api_key"]):
        provider["api_key"] = current["api_key"]  # 掩码/留空 = 保留原 Key
    if not jz_api.save_user_llm(username, provider):
        return jsonify({"error": "保存失败：账号数据不可写（核心插件未加载）"}), 503
    jz_api.set_operation("保存个人大模型设置")
    return jsonify({
        "ok": True,
        "configured": jz_llm.provider_usable(provider),
        "problem": jz_llm.provider_problem(provider),
    })


@llm_bp.post("/test")
@require_login
def api_llm_test():
    """连通性测试：优先测请求体里的这套（表单里刚填、还没保存的），否则测当前生效的。

    请求体：{format, url, api_key, model}（可缺省）；api_key 为掩码/留空时用已保存的。
    """
    info = g.current_user
    data = request.get_json(silent=True) or {}
    username = info.get("username")
    own = jz_llm.normalize_provider(jz_api.get_user_llm(username) or {})

    draft = None
    if any(str(data.get(k) or "").strip() for k in ("url", "model", "format")):
        draft = jz_llm.normalize_provider(data)
        if jz_llm.is_mask(draft["api_key"]):
            # 掩码/留空 = 沿用已保存的 Key：用户模式取本人配置，管理员模式取全局配置
            draft["api_key"] = own["api_key"] if jz_llm.user_mode() \
                else jz_llm.load_global()["provider"]["api_key"]

    jz_api.set_operation("测试大模型连通性")
    ok, detail = jz_llm.test_connection(provider=draft)
    return jsonify({"ok": ok, "detail": detail})


def install(app):
    """把蓝图注册进主应用（app.py 装配期调用）。"""
    app.register_blueprint(llm_bp)
