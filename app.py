import importlib
import importlib.util
import gzip
import io
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import sys
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "tools.json")
PLUGINS_DIR = os.path.join(BASE_DIR, "plugins")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "access.log")

# 插件 ID 只允许出现在目录名中，防止目录穿越
PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# 可压缩的内容类型（gzip 白名单）
COMPRESSIBLE_TYPES = (
    "text/plain", "text/html", "text/css", "text/javascript",
    "application/javascript", "application/json", "application/xml",
    "image/svg+xml", "application/x-javascript",
)
# 静态资源缓存策略（按扩展名）
CACHE_LONG_EXT = {".js", ".css", ".json", ".png", ".jpg", ".jpeg", ".gif",
                  ".svg", ".webp", ".woff", ".woff2", ".ttf", ".ico", ".map"}
CACHE_NOCACHE_EXT = {".html"}

# 访问日志记录器（模块级单例，register_access_logging 时初始化）
access_logger = logging.getLogger("jztools.access")

# load_tool_meta 的短 TTL 缓存（仅服务日志解析，避免每请求读文件）
_TOOL_META_TTL = 2.0  # 秒
_tool_meta_cache = None
_tool_meta_cache_ts = 0.0

app = Flask(__name__)


class WindowsSafeTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """兼容 Windows 的按天滚动日志 handler。

    Windows 下当服务进程长期占用日志文件时，TimedRotatingFileHandler 的
    os.rename 会抛 PermissionError（WinError 32），导致该时段所有日志静默丢失。
    本类在 rename 失败时退回「复制原文件 + 截断」策略（copytruncate）：
    先把当前内容复制为归档文件，再清空原文件继续写入 —— 永不因文件锁丢日志。
    """

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        current_time = int(time.time())
        dfn = self.rotation_filename(
            self.baseFilename + "." + time.strftime(self.suffix, time.localtime(current_time))
        )
        if os.path.exists(self.baseFilename):
            try:
                os.rename(self.baseFilename, dfn)  # 常规路径：直接改名
            except OSError:
                # Windows 文件占用：复制 + 截断
                try:
                    shutil.copyfile(self.baseFilename, dfn)
                    with open(self.baseFilename, "w", encoding="utf-8") as f:
                        f.truncate()
                except OSError:
                    pass  # 复制也失败则放弃本次归档，继续写原文件
        if not self.encoding:
            self.stream = open(self.baseFilename, "w")
        else:
            self.stream = open(self.baseFilename, "w", encoding=self.encoding)
        self.rolloverAt = self.computeRollover(current_time)
        return dfn


# ===================== 访问日志（异步） =====================

# 后台日志监听器（模块级单例）
_log_queue = None
_log_listener = None


def setup_access_logging(app):
    """初始化异步访问日志：按天滚动写入 logs/access.log，保留最近 30 天。

    记录通过内存队列 + 后台 QueueListener 异步落盘，HTTP 线程只入队不阻塞 I/O。
    记录字段：时间 / 客户端 IP / HTTP 方法 / 功能（工具或插件）/ 路径 / 状态码 / 耗时 / UA。
    """
    global access_logger, _log_queue, _log_listener
    if config_logging_enabled() is False:
        return  # 配置中显式关闭日志

    os.makedirs(LOG_DIR, exist_ok=True)
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False

    # 已挂 handler 则跳过，避免 debug 模式下重复注册
    if access_logger.handlers:
        return

    handler = WindowsSafeTimedRotatingFileHandler(
        LOG_FILE, when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s\t%(levelname)s\t%(message)s"
    ))

    # 内存队列 1（容量 ~2000 条），超出时丢弃旧日志，保证写盘永不阻塞请求线程
    _log_queue = queue.Queue(maxsize=2000)
    _log_listener = logging.handlers.QueueListener(_log_queue, handler)
    access_logger.addHandler(logging.handlers.QueueHandler(_log_queue))
    _log_listener.start()
    app.logger.info("异步访问日志已启动：%s", LOG_FILE)


def shutdown_access_logging():
    """优雅停止后台日志监听器（flush 剩余日志）。"""
    global _log_listener
    if _log_listener is not None:
        _log_listener.stop()
        _log_listener = None


def config_logging_enabled():
    """从 config/tools.json 读取日志开关（site.logging，默认开启）。"""
    try:
        registry = load_registry()
        return registry.get("site", {}).get("logging", True)
    except Exception:
        return True


def get_client_ip():
    """获取客户端真实 IP，兼容反向代理（X-Forwarded-For）与直连。"""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return request.remote_addr or "-"


def parse_feature():
    """根据请求路径解析「功能」标签，映射到具体工具名/接口名。

    工具展示名以 config/tools.json 为准（与首页一致），manifest 仅兜底。
    """
    path = request.path
    meta = load_tool_meta()

    if path == "/":
        return "首页"
    if path == "/api/tools":
        return "API: 工具列表"
    if path.startswith("/api/tools/"):
        tool_id = path[len("/api/tools/"):].strip("/")
        name = meta.get(tool_id, {}).get("name") or tool_id
        return f"API: 工具详情({name})"
    if path.startswith("/plugin/"):
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            tool_id = parts[1]
            name = meta.get(tool_id, {}).get("name") or tool_id
            sub = "/".join(parts[2:]) or "入口页面"
            return f"插件({name})/frontend/{sub}"
    if path.startswith("/tool/"):
        tool_id = path[len("/tool/"):].strip("/")
        name = meta.get(tool_id, {}).get("name") or tool_id
        return f"工具壳页({name})"
    if path.startswith("/api/"):
        return f"后端接口: {path}"
    return f"静态资源: {path}"


@app.before_request
def _log_start():
    request.environ["_req_start"] = time.perf_counter()


def _should_compress(response):
    """判断响应是否适合 gzip 压缩。"""
    if response.status_code < 200 or response.status_code == 204 or response.status_code == 304:
        return False
    if response.headers.get("Content-Encoding"):
        return False  # 已压缩过
    if response.direct_passthrough:
        return False  # 流式响应（如 send_file 大文件）不压缩，避免序列化错误
    if len(response.get_data()) < 256:
        return False  # 小响应不压缩
    ct = response.content_type or ""
    if not any(ct.startswith(t) for t in COMPRESSIBLE_TYPES):
        return False
    ae = (request.headers.get("Accept-Encoding", "") or "").lower()
    return "gzip" in ae


@app.after_request
def _compress_response(response):
    """gzip 压缩：仅当客户端声明支持 gzip 且响应为可压缩文本。"""
    if not _should_compress(response):
        return response

    data = response.get_data()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        gz.write(data)
    response.set_data(buf.getvalue())
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Vary"] = "Accept-Encoding"
    return response


@app.after_request
def _static_cache(response):
    """静态资源缓存策略：长缓存静态资产、HTML 协商缓存。

    Flask debug 模式下 send_file 会自动加 no-cache，这里强制覆盖为统一策略。
    """
    path = request.path
    if not (path.startswith("/static/") or path.startswith("/plugin/")):
        return response
    ext = os.path.splitext(path)[1].lower()
    if ext in CACHE_LONG_EXT:
        response.headers["Cache-Control"] = "public, max-age=86400"
    elif ext in CACHE_NOCACHE_EXT or not ext:
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.after_request
def _log_response(response):
    """记录本次请求：IP / 方法 / 功能 / 路径 / 状态 / 耗时 / UA。

    注意：after_request 按注册逆序执行，本函数最后注册，会先于 gzip/缓存中间件
    被调用，因此日志记录的是压缩前的原始耗时与状态，不影响正确性。
    """
    if config_logging_enabled() is False:
        return response
    # 日志队列尚未初始化（例如关闭日志时）则跳过
    if not access_logger.handlers:
        return response

    start = request.environ.pop("_req_start", None)
    cost_ms = round((time.perf_counter() - start) * 1000) if start else -1

    access_logger.info(
        "ip=%s\tmethod=%s\tfunc=%s\tpath=%s\tstatus=%s\tcost_ms=%s\tua=%s",
        get_client_ip(),
        request.method,
        parse_feature(),
        request.path,
        response.status_code,
        cost_ms,
        (request.headers.get("User-Agent", "") or "")[:120],
    )
    return response


def load_registry():
    """加载后台配置(config/tools.json)，这是工具的注册清单。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_registry(registry):
    """原子写入 config/tools.json，先写临时文件再 rename。"""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def load_manifests():
    """扫描 plugins/ 下所有插件目录，读取各自 manifest.json。"""
    manifests = {}
    if not os.path.isdir(PLUGINS_DIR):
        return manifests
    for name in os.listdir(PLUGINS_DIR):
        manifest_path = os.path.join(PLUGINS_DIR, name, "manifest.json")
        if os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifests[name] = json.load(f)
    return manifests


def load_tool_meta():
    """聚合「名称 / 描述」等展示元信息，生成 {插件id: {name, description}} 映射。

    配置驱动原则：config/tools.json 中注册的 name / description 为权威取值，
    插件目录的 manifest.json 仅作为缺省回退（便于插件目录跨项目迁移时自解释）。

    带短 TTL 缓存：本函数被请求日志解析逐请求调用，加缓存避免每次请求都读文件；
    缓存仅用于日志展示，不影响 /api/tools 等实时读取的接口。
    """
    global _tool_meta_cache, _tool_meta_cache_ts
    now = time.time()
    if _tool_meta_cache is not None and now - _tool_meta_cache_ts < _TOOL_META_TTL:
        return _tool_meta_cache

    meta = {}
    try:
        registry = load_registry()
        for item in registry.get("tools", []):
            # 配置文件优先；manifest 兜底
            meta[item["id"]] = {
                "name": item.get("name") or "",
                "description": item.get("description") or "",
            }
    except Exception:
        pass
    for pid, manifest in load_manifests().items():
        entry = meta.setdefault(pid, {"name": "", "description": ""})
        if not entry["name"]:
            entry["name"] = manifest.get("name", "") or pid
        if not entry["description"]:
            entry["description"] = manifest.get("description", "") or ""

    _tool_meta_cache = meta
    _tool_meta_cache_ts = now
    return meta


def get_plugin_dir(plugin_id):
    """校验插件 ID 并返回其绝对目录，非法 ID 返回 None。"""
    if not PLUGIN_ID_RE.match(plugin_id or ""):
        return None
    return os.path.join(PLUGINS_DIR, plugin_id)


def get_aggregated_tools():
    """聚合后台配置与插件清单，生成前端可用的工具列表。

    配置驱动注册原则：
    - config/tools.json 决定哪些工具被展示、展示顺序与分类，同时是
      「名称 / 描述」的权威来源（manifest.json 仅作为缺省回退）；
    - 每个工具目录的 manifest.json 提供图标、主题色、入口与能力标签。
    """
    registry = load_registry()
    manifests = load_manifests()
    hidden_cats = {c["id"] for c in registry.get("categories", []) if not c.get("enabled", True)}
    category_map = {c["id"]: c["name"] for c in registry.get("categories", [])}
    meta = load_tool_meta()

    tools = []
    for item in registry.get("tools", []):
        if not item.get("enabled", True):
            continue
        if item.get("category") in hidden_cats:
            continue
        manifest = manifests.get(item["id"])
        if manifest is None:
            continue
        m = meta.get(item["id"], {})
        tools.append({
            "id": manifest.get("id", item["id"]),
            "name": m.get("name") or manifest.get("name", item["id"]),
            "description": m.get("description") or manifest.get("description", ""),
            "icon": manifest.get("icon", "🧩"),
            "accent": manifest.get("accent", "#4285F4"),
            "entry": manifest.get("entry", "index.html"),
            "features": manifest.get("features", []),
            "category": category_map.get(item.get("category"), "未分类"),
            "category_id": item.get("category", ""),
            "order": item.get("order", 0),
        })
    tools.sort(key=lambda t: t["order"])
    return tools


def _load_backend_module(plugin_id):
    """载入插件后端模块 plugins/<id>/backend/routes.py。

    每个插件为独立包，模块名取插件 ID 的合法形式，避免同名冲突。
    约定插件后端提供 register(app) 函数挂载路由。
    """
    plugin_dir = get_plugin_dir(plugin_id)
    if not plugin_dir:
        return None
    backend_dir = os.path.join(plugin_dir, "backend")
    package_file = os.path.join(backend_dir, "__init__.py")
    if not os.path.isfile(package_file):
        return None
    module_name = "jztools_" + re.sub(r"\W", "_", plugin_id)
    spec = importlib.util.spec_from_file_location(
        module_name, package_file, submodule_search_locations=[backend_dir]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = package
    spec.loader.exec_module(package)
    return importlib.import_module(f"{module_name}.routes")


def register_plugin_backends(app):
    """动态注册已启用工具所声明的 Python 后端插件。

    每个插件在 plugins/<插件id>/backend/ 下提供 __init__.py 与 routes.py，
    后端代码随插件整体打包、随 enabled 配置启停 —— 一切皆插件。
    """
    registry = load_registry()
    for item in registry.get("tools", []):
        if not item.get("enabled", True):
            continue
        routes = _load_backend_module(item["id"])
        if routes is None:
            continue
        register = getattr(routes, "register", None)
        if callable(register):
            register(app)
            app.logger.info(f"已注册后端插件：{item['id']}")


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/tool/<tool_id>")
def tool_page(tool_id):
    return send_from_directory("static", "tool.html")


@app.route("/plugin/<plugin_id>/<path:filename>")
def plugin_assets(plugin_id, filename):
    """以 iframe 方式对外提供插件前端资源（plugins/<id>/frontend/）。"""
    plugin_dir = get_plugin_dir(plugin_id)
    if not plugin_dir:
        return jsonify({"error": "plugin not found"}), 404
    return send_from_directory(os.path.join(plugin_dir, "frontend"), filename)


@app.route("/api/tools")
def api_tools():
    registry = load_registry()
    categories = [c for c in registry.get("categories", []) if c.get("enabled", True)]
    return jsonify({
        "site": registry.get("site", {}),
        "categories": categories,
        "tools": get_aggregated_tools(),
    })


@app.route("/api/tools/<tool_id>")
def api_tool(tool_id):
    tools = get_aggregated_tools()
    for tool in tools:
        if tool["id"] == tool_id:
            return jsonify(tool)
    return jsonify({"error": "tool not found"}), 404


@app.post("/api/tools/reorder")
def api_tools_reorder():
    """接收前端拖拽排序后的布局，写回 config/tools.json。

    请求体：{ categories: [id, …], tools: { catId: [toolId, …], … } }
    - categories 为分类数组（按显示顺序）
    - tools 为每个分类下的工具 ID 数组
    """
    data = request.get_json(silent=True) or {}
    categories_order = data.get("categories")
    tools_order = data.get("tools")
    if not isinstance(categories_order, list) or not isinstance(tools_order, dict):
        return jsonify({"error": "invalid payload"}), 400

    registry = load_registry()
    cat_ids = {c["id"] for c in registry.get("categories", [])}
    tool_map = {t["id"]: t for t in registry.get("tools", [])}

    for cid in categories_order:
        if cid not in cat_ids:
            return jsonify({"error": f"unknown category: {cid}"}), 400
    for cid, tool_ids in tools_order.items():
        if cid not in cat_ids:
            return jsonify({"error": f"unknown category: {cid}"}), 400
        if not isinstance(tool_ids, list):
            return jsonify({"error": "tools must be a list"}), 400
        for tid in tool_ids:
            if tid not in tool_map:
                return jsonify({"error": f"unknown tool: {tid}"}), 400

    cat_map = {c["id"]: c for c in registry["categories"]}
    new_cats = [cat_map[cid] for cid in categories_order]
    for c in registry["categories"]:
        if c["id"] not in {x["id"] for x in new_cats}:
            new_cats.append(c)
    registry["categories"] = new_cats

    for cid, tool_ids in tools_order.items():
        for pos, tid in enumerate(tool_ids):
            tool_map[tid]["category"] = cid
            tool_map[tid]["order"] = pos + 1

    save_registry(registry)
    return jsonify({"ok": True})


@app.get("/api/tools/visibility")
def api_tools_visibility():
    """返回全部分类与工具及其启用状态，供「隐藏工具」浮窗使用（含已隐藏项）。"""
    registry = load_registry()
    return jsonify({
        "categories": [
            {"id": c["id"], "name": c["name"], "enabled": c.get("enabled", True)}
            for c in registry.get("categories", [])
        ],
        "tools": [
            {"id": t["id"], "name": t.get("name") or t["id"], "enabled": t.get("enabled", True)}
            for t in registry.get("tools", [])
        ],
    })


@app.post("/api/tools/visibility")
def api_tools_visibility_save():
    """切换分类 / 工具是否启用：{type: 'tool'|'category', id, enabled}。"""
    data = request.get_json(silent=True) or {}
    typ = data.get("type")
    tid = data.get("id")
    if typ not in ("tool", "category") or not isinstance(tid, str) or not tid:
        return jsonify({"error": "invalid payload"}), 400
    enabled = bool(data.get("enabled"))

    registry = load_registry()
    if typ == "tool":
        for t in registry.get("tools", []):
            if t["id"] == tid:
                t["enabled"] = enabled
                save_registry(registry)
                return jsonify({"ok": True})
        return jsonify({"error": "tool not found"}), 404
    else:
        for c in registry.get("categories", []):
            if c["id"] == tid:
                c["enabled"] = enabled
                save_registry(registry)
                return jsonify({"ok": True})
        return jsonify({"error": "category not found"}), 404


if __name__ == "__main__":
    setup_access_logging(app)
    register_plugin_backends(app)
    try:
        app.run(host="0.0.0.0", port=5000, debug=True)
    finally:
        shutdown_access_logging()
