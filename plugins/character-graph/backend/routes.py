"""人物关系立体星图 —— JZToolsHub 后端插件路由。

将原项目(character-graph)的 FastAPI 后端移植为 Flask 路由，
复用同目录下的 llm_client.py / document_reader.py（未修改）。
所有接口挂在 /api/character-graph 前缀下，与主应用其他工具隔离。
"""

import json
import os

from flask import jsonify, request

from . import document_reader, llm_client

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PLUGIN_DIR, "config.json")
PROMPT_FILE = os.path.join(PLUGIN_DIR, "prompt.json")
API_PREFIX = "/api/character-graph"

DEFAULT_CONFIG = {
    "ui": {"api_source": "web"},
    "llm": {"base_url": "", "api_key": "", "model": ""},
}

DEFAULT_PROMPT = {
    "system": llm_client.DEFAULT_SYSTEM_PROMPT,
    "user_template": llm_client.DEFAULT_USER_TEMPLATE,
}


def load_config() -> dict:
    """读取 config.json，缺失键用默认值补齐。"""
    data = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    ui = data.get("ui") or {}
    llm = data.get("llm") or {}
    api_source = ui.get("api_source")
    if api_source not in ("web", "config"):
        api_source = "web"
    return {
        "ui": {"api_source": api_source},
        "llm": {
            "base_url": llm.get("base_url", ""),
            "api_key": llm.get("api_key", ""),
            "model": llm.get("model", ""),
        },
    }


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_prompt() -> dict:
    data = {}
    if os.path.exists(PROMPT_FILE):
        try:
            with open(PROMPT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    return {
        "system": data.get("system") or DEFAULT_PROMPT["system"],
        "user_template": data.get("user_template") or DEFAULT_PROMPT["user_template"],
    }


def ensure_config_files() -> None:
    """首次运行生成可编辑的默认配置文件。"""
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
    if not os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_PROMPT, f, ensure_ascii=False, indent=2)


def register(app) -> None:
    """插件入口：由 JZToolsHub 主应用在启动时调用。"""
    ensure_config_files()

    @app.get(f"{API_PREFIX}/config")
    def cg_get_config():
        cfg = load_config()
        return jsonify({
            "api_source": cfg["ui"]["api_source"],
            "base_url": cfg["llm"]["base_url"],
            "api_key": cfg["llm"]["api_key"],
            "model": cfg["llm"]["model"],
        })

    @app.post(f"{API_PREFIX}/config")
    def cg_post_config():
        body = request.get_json(silent=True) or {}
        merged = load_config()
        merged["llm"]["base_url"] = body.get("base_url", "")
        merged["llm"]["api_key"] = body.get("api_key", "")
        merged["llm"]["model"] = body.get("model", "")
        save_config(merged)
        return jsonify({"ok": True, **merged["llm"]})

    @app.get(f"{API_PREFIX}/prompt")
    def cg_get_prompt():
        return jsonify(load_prompt())

    @app.post(f"{API_PREFIX}/analyze")
    def cg_analyze():
        if "file" not in request.files:
            return jsonify({"detail": "缺少文件"}), 400
        file = request.files["file"]
        if not file.filename:
            return jsonify({"detail": "缺少文件"}), 400

        raw = file.read()
        if not raw:
            return jsonify({"detail": "文件为空"}), 400

        try:
            text = document_reader.extract_text(file.filename, raw)
        except ValueError as e:
            return jsonify({"detail": str(e)}), 400
        except Exception as e:
            return jsonify({"detail": f"读取文档失败：{e}"}), 500

        if len(text.strip()) < 5:
            return jsonify({"detail": "文档中提取不到可用文本（PDF 可能是扫描件）"}), 400

        cfg = load_config()
        base_url = request.form.get("base_url") or cfg["llm"]["base_url"]
        api_key = request.form.get("api_key") or cfg["llm"]["api_key"]
        model = request.form.get("model") or cfg["llm"]["model"]

        if not api_key:
            return jsonify({
                "detail": "尚未配置 API Key，请先填写大模型信息（网页模式保存，或编辑 config.json）"
            }), 400

        if len(text) > 120000:
            text = text[:120000] + "\n...（文档过长，已截断）"

        try:
            graph = llm_client.extract_graph(
                text, base_url, api_key, model, prompt=load_prompt()
            )
        except llm_client.LLMError as e:
            return jsonify({"detail": str(e)}), 502

        return jsonify({"ok": True, "filename": file.filename, "graph": graph})
