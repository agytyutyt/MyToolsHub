"""人物关系立体星图 —— JZToolsHub 后端插件路由。

将原项目(character-graph)的 FastAPI 后端移植为 Flask 路由，
复用同目录下的 llm_client.py / document_reader.py（未修改）。
所有接口挂在 /api/character-graph 前缀下，与主应用其他工具隔离。

并发设计：
- /analyze 仅做文件接收与参数校验，立即返回 task_id（毫秒级），
  长耗时的文档解析 + 大模型调用放到后台线程池执行，
  不再占用 HTTP worker —— 避免多个并发分析拖垮整个站点。
- 前端通过 GET /result/<task_id> 轮询任务状态。
"""

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import jsonify, request

from . import document_reader, llm_client

try:
    from jztools_admin.routes import get_session_user as _get_session_user
except Exception:  # admin 插件缺失时兜底（理论上不会发生）
    _get_session_user = None

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PLUGIN_DIR, "config.json")
PROMPT_FILE = os.path.join(PLUGIN_DIR, "prompt.json")
API_PREFIX = "/api/character-graph"

# 后台分析线程池：限制并发 LLM 任务数，防止大模型调用耗尽资源
ANALYZE_WORKERS = 2
_analyze_executor = ThreadPoolExecutor(max_workers=ANALYZE_WORKERS)

# 任务状态存储：task_id -> {status, filename, ...}
# status: pending -> running -> done / error
TASKS = {}
TASKS_LOCK = threading.Lock()
TASK_TTL_SECONDS = 30 * 60  # 任务结果保留 30 分钟

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


def set_task(task_id: str, **kwargs) -> None:
    with TASKS_LOCK:
        TASKS[task_id] = {**TASKS.get(task_id, {}), **kwargs}


def get_task(task_id: str):
    with TASKS_LOCK:
        return TASKS.get(task_id)


def cleanup_tasks() -> None:
    """清理过期任务，避免内存无限增长。"""
    now = time.time()
    with TASKS_LOCK:
        expired = [
            tid for tid, t in TASKS.items()
            if t.get("created_at", 0) < now - TASK_TTL_SECONDS
        ]
        for tid in expired:
            TASKS.pop(tid, None)


def create_task(filename: str, created_at: float, created_by: str = "") -> str:
    tid = uuid.uuid4().hex[:12]
    set_task(tid, status="pending", filename=filename,
             created_at=created_at, created_by=created_by)
    return tid


def _run_analysis(task_id: str, filename: str, raw: bytes,
                  base_url: str, api_key: str, model: str, prompt: dict) -> None:
    """后台线程执行：文档解析 + 大模型关系抽取。"""
    try:
        set_task(task_id, status="running")

        text = document_reader.extract_text(filename, raw)
        if len(text.strip()) < 5:
            raise ValueError("文档中提取不到可用文本（PDF 可能是扫描件）")

        if len(text) > 120000:
            text = text[:120000] + "\n...（文档过长，已截断）"

        graph = llm_client.extract_graph(
            text, base_url, api_key, model, prompt=prompt
        )
        set_task(task_id, status="done", filename=filename, graph=graph)
    except llm_client.LLMError as e:
        # 大模型错误信息为面向用户的业务文案，保留原文
        set_task(task_id, status="error", filename=filename, detail=str(e))
    except (ValueError, RuntimeError) as e:
        set_task(task_id, status="error", filename=filename, detail=str(e))
    except Exception as e:
        # SEC-5：非预期异常不向前端透出内部细节（堆栈/路径等）
        set_task(task_id, status="error", filename=filename,
                 detail=f"分析失败（{type(e).__name__}）")


def _task_owned_by(task, user):
    """任务归属校验：非创建者（且非超管）视为任务不存在（404，纵深防御）。

    与 trajectory-convert 插件的同名机制保持一致。
    """
    owner = task.get("created_by")
    if not owner:
        return True  # 兼容升级前创建的旧任务
    if user is None:
        return False
    if user.get("super_admin"):
        return True
    return owner == user.get("username")


def _viewer():
    """当前登录用户上下文；未登录返回 None。"""
    if _get_session_user is None:
        return None
    try:
        return _get_session_user()
    except Exception:
        return None


def _submit_analysis(filename: str, raw: bytes,
                     base_url: str, api_key: str, model: str, prompt: dict) -> str:
    """提交后台分析任务，返回 task_id。"""
    user = _viewer()
    task_id = create_task(filename, time.time(),
                          created_by=(user or {}).get("username", ""))
    cleanup_tasks()  # 顺带清理过期任务
    _analyze_executor.submit(
        _run_analysis, task_id, filename, raw, base_url, api_key, model, prompt
    )
    return task_id


def register(app) -> None:
    """插件入口：由 JZToolsHub 主应用在启动时调用。"""
    ensure_config_files()

    @app.get(f"{API_PREFIX}/config")
    def cg_get_config():
        """读取展示配置。API Key 不回传明文，仅返回掩码（SEC-2/F-4）。"""
        cfg = load_config()
        key = cfg["llm"]["api_key"]
        masked = (key[:3] + "****" + key[-4:]) if len(key) > 8 else ("已设置" if key else "")
        return jsonify({
            "api_source": cfg["ui"]["api_source"],
            "base_url": cfg["llm"]["base_url"],
            "api_key_set": bool(key),
            "api_key_masked": masked,
            "model": cfg["llm"]["model"],
        })

    @app.post(f"{API_PREFIX}/config")
    def cg_post_config():
        """保存展示配置；api_key 留空表示沿用原值，避免明文回显后空写覆盖。"""
        body = request.get_json(silent=True) or {}
        merged = load_config()
        merged["llm"]["base_url"] = (body.get("base_url") or "").strip()
        new_key = (body.get("api_key") or "").strip()
        if new_key:
            merged["llm"]["api_key"] = new_key
        merged["llm"]["model"] = (body.get("model") or "").strip()
        save_config(merged)
        return jsonify({
            "ok": True,
            "llm_configured": bool(merged["llm"]["api_key"]),
        })

    @app.get(f"{API_PREFIX}/prompt")
    def cg_get_prompt():
        return jsonify(load_prompt())

    @app.get(f"{API_PREFIX}/status")
    def cg_status():
        """依赖自检：缺依赖时优雅降级（B-4）。"""
        deps = {
            "python-docx": document_reader.DOCX_AVAILABLE,
            "pypdf": document_reader.PDF_AVAILABLE,
            "requests": llm_client.REQUESTS_AVAILABLE,
        }
        return jsonify({"ok": all(deps.values()), "dependencies": deps})

    @app.post(f"{API_PREFIX}/analyze")
    def cg_analyze():
        """接收文件，提交后台任务，立即返回 task_id（不占用 HTTP worker）。"""
        if "file" not in request.files:
            return jsonify({"detail": "缺少文件"}), 400
        file = request.files["file"]
        if not file.filename:
            return jsonify({"detail": "缺少文件"}), 400
        # 仅取基础文件名，剥离任何路径成分（SEC-1 纵深防御）
        safe_name = os.path.basename(file.filename.replace("\\", "/")) or "未命名"

        raw = file.read()
        if not raw:
            return jsonify({"detail": "文件为空"}), 400

        # 前置校验：先检查是否已配置 API Key，避免提交后白等
        cfg = load_config()
        base_url = request.form.get("base_url") or cfg["llm"]["base_url"]
        api_key = request.form.get("api_key") or cfg["llm"]["api_key"]
        model = request.form.get("model") or cfg["llm"]["model"]

        if not api_key:
            return jsonify({
                "detail": "尚未配置 API Key，请先填写大模型信息（网页模式保存，或编辑 config.json）"
            }), 400

        task_id = _submit_analysis(
            safe_name, raw, base_url, api_key, model, load_prompt()
        )
        return jsonify({"ok": True, "task_id": task_id})

    @app.get(f"{API_PREFIX}/result/<task_id>")
    def cg_result(task_id):
        """轮询任务状态：pending / running / done / error（仅创建者与超管可见）。"""
        task = get_task(task_id)
        if not task or not _task_owned_by(task, _viewer()):
            return jsonify({"detail": "任务不存在或已过期"}), 404
        payload = {
            "status": task.get("status"),
            "filename": task.get("filename", ""),
        }
        if task.get("status") == "done":
            payload["graph"] = task.get("graph")
        if task.get("status") == "error":
            payload["detail"] = task.get("detail", "分析失败")
        return jsonify(payload)