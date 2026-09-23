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

# 会话工具经主体模块 jz_api 取用（依赖倒置：插件不再 import admin 插件的内部模块，
# 插件之间零依赖；admin 未加载时自动降级为「未登录 / 空操作」）。
# 见 docs/design/主体与插件解耦-设计文档.md §5.1 FC-3。
import jz_api
import jz_deps
import jz_llm

_get_session_user = jz_api.get_session_user

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import jsonify, request

from . import document_reader, llm_client


import jztools_data
CONFIG_FILE = jztools_data.get_data_root_file("plugins", "character-graph", "config.json")
PROMPT_FILE = jztools_data.get_data_root_file("plugins", "character-graph", "prompt.json")
API_PREFIX = "/api/character-graph"
PLUGIN_ID = "character-graph"

# 后台分析线程池：限制并发 LLM 任务数，防止大模型调用耗尽资源
ANALYZE_WORKERS = 2
_analyze_executor = ThreadPoolExecutor(max_workers=ANALYZE_WORKERS)

# 任务状态存储：task_id -> {status, filename, ...}
# status: pending -> running -> done / error
TASKS = {}
TASKS_LOCK = threading.Lock()
TASK_TTL_SECONDS = 30 * 60  # 任务结果保留 30 分钟

# 本插件已无自有配置项：提示词由 prompt.json 管理（经 jz_llm），大模型接入信息归
# 统一大模型模块。历史 config.json 里的 llm 段与 ui.api_source（"网页填 Key"开关）
# 由 admin 插件的启动迁移收编/清除（见 migrate_plugin_legacy_llm）。
DEFAULT_CONFIG = {}

DEFAULT_PROMPT = {
    "system": llm_client.DEFAULT_SYSTEM_PROMPT,
    "user_template": llm_client.DEFAULT_USER_TEMPLATE,
}


def load_config() -> dict:
    """读取 config.json（整份原样返回；本插件当前无自有配置项，保留给将来用）。"""
    data = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_prompt() -> dict:
    """读取提示词（本插件只"拟定并保存 prompt"，调用由统一大模型模块完成）。

    存放位置与历史版本一致（<数据根>/plugins/character-graph/prompt.json）。
    """
    return jz_llm.load_prompt(PLUGIN_ID, DEFAULT_PROMPT)


def ensure_config_files() -> None:
    """首次运行生成可编辑的默认配置文件。"""
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
    if not os.path.exists(PROMPT_FILE):
        jz_llm.save_prompt(PLUGIN_ID, DEFAULT_PROMPT)


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
                  session, prompt: dict) -> None:
    """后台线程执行：文档解析 + 大模型关系抽取。

    session 由请求线程 jz_llm.resolve() 取得后传入——后台线程读不到会话，
    在这里现解析会在"用户各自设置"模式下被误判为未配置（见 jz_llm 模块头部的线程纪律）。
    """
    try:
        set_task(task_id, status="running")

        if not session.configured():
            raise ValueError(session.reason or "尚未配置大模型，无法分析文档")

        text = document_reader.extract_text(filename, raw)
        if len(text.strip()) < 5:
            raise ValueError("文档中提取不到可用文本（PDF 可能是扫描件）")

        if len(text) > 120000:
            text = text[:120000] + "\n...（文档过长，已截断）"

        graph = llm_client.extract_graph(text, prompt=prompt, session=session)
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


def _submit_analysis(filename: str, raw: bytes, session, prompt: dict) -> str:
    """提交后台分析任务，返回 task_id。"""
    user = _viewer()
    task_id = create_task(filename, time.time(),
                          created_by=(user or {}).get("username", ""))
    cleanup_tasks()  # 顺带清理过期任务
    _analyze_executor.submit(_run_analysis, task_id, filename, raw, session, prompt)
    return task_id


def register(app) -> None:
    """插件入口：由 JZToolsHub 主应用在启动时调用。"""
    # 依赖可用性按请求实时刷新：服务运行中装「依赖组件包」后无需重启即可生效
    # （标记在模块导入时算好，不刷新会让插件页面一直显示"未安装"——jz_deps.refresh_flags）
    jz_deps.install_refresher(app, globals(), document_reader, llm_client)
    ensure_config_files()

    @app.get(f"{API_PREFIX}/config")
    def cg_get_config():
        """读取大模型可用状态。

        统一大模型落地后本插件**不再保存也不回传** API 地址与 Key（SEC-2/F-4）：
        接入信息由 jz_llm 按模式解析，这里只回"能不能用 + 用的是哪一份"。
        """
        session = jz_llm.resolve(PLUGIN_ID)
        return jsonify({
            "ok": True,
            "llm_configured": session.configured(),
            "llm_source": session.source,
            "llm_source_label": session.source_label(),
            "llm_reason": session.reason,
        })

    @app.post(f"{API_PREFIX}/config")
    def cg_post_config():
        """兼容占位：统一大模型落地后本插件已无可写配置项。

        不覆盖 config.json 里的历史 llm 段——那段是升级前的旧 Key，
        jz_llm 在统一配置为空时仍会读它（兼容桥），删掉会让老部署突然不可用。
        """
        session = jz_llm.resolve(PLUGIN_ID)
        return jsonify({"ok": True, "llm_configured": session.configured()})

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

        # 前置校验：先看统一大模型是否可用，避免提交后白等（失败原因直接给用户）
        session = jz_llm.resolve(PLUGIN_ID)
        if not session.configured():
            return jsonify({"detail": session.reason or "尚未配置大模型，无法分析文档"}), 400

        task_id = _submit_analysis(safe_name, raw, session, load_prompt())
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