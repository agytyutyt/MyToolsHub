"""宿主请求/响应外壳：异步访问日志 + gzip 压缩 + 安全头 + 静态缓存策略。

从 app.py 拆出（2026-10-10 P2-4 / 报告 §4.4「日志三件套」块，含同段注册的全部
after_request 钩子）。日志目录由 app.py 在装配期经 set_log_paths 注入。
"""
import gzip
import io
import logging
import logging.handlers
import os
import queue
import shutil
import time

from flask import g, request

import jz_api
from jz_host import plugin_loader

log = logging.getLogger("jztools.app")

# 日志目录（占位；app.py 装配期与 init_data_root() 时注入实际数据根路径）
LOG_DIR = None
LOG_FILE = None


def set_log_paths(log_dir, log_file):
    """注入日志目录/文件路径（app.py init_data_root 与模块装配期调用）。"""
    global LOG_DIR, LOG_FILE
    LOG_DIR = log_dir
    LOG_FILE = log_file


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

# 访问日志记录器（模块级单例，setup_access_logging 时初始化）
access_logger = logging.getLogger("jztools.access")

# 后台日志监听器（模块级单例）
_log_queue = None
_log_listener = None


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
        registry = plugin_loader.load_registry()
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
    meta = plugin_loader.load_tool_meta()

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


# 核心路由 → 具体操作 兜底映射（插件可显式 set_operation 覆盖）
_OPERATION_MAP = {
    ("GET", "/"): "访问首页",
    ("GET", "/api/tools"): "获取工具列表",
    ("POST", "/api/tools/reorder"): "调整首页工具排序",
    ("GET", "/api/tools/visibility"): "查询工具可见性",
    ("POST", "/api/tools/visibility"): "切换工具/分类可见性",
}


def parse_operation():
    """解析「具体操作」标签：优先取插件显式标记（g._current_operation），
    其次核心路由映射，最后基于路径/方法兜底。"""
    try:
        op = getattr(g, "_current_operation", None)
        if op:
            return op
    except Exception:
        pass
    key = (request.method, request.path)
    if key in _OPERATION_MAP:
        return _OPERATION_MAP[key]
    path = request.path
    if path.startswith("/plugin/"):
        return "打开插件功能页"
    if path.startswith("/tool/"):
        return "打开工具"
    if path.startswith("/api/"):
        return f"接口调用: {request.method} {path}"
    if path.startswith("/static/"):
        return "加载静态资源"
    return "访问页面"


def get_current_user():
    """当前登录用户信息（经框架 API 门面 jz_api 取自 admin 插件）；匿名返回 None。

    供日志记录 username 使用；admin 插件未加载时返回 None，不阻断请求。
    静态资源请求不解析用户，减少不必要的文件 I/O。
    """
    if request.path.startswith("/static/"):
        return None
    return jz_api.get_session_user()


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


# T30 框架级安全响应头（批次 8）。取值说明：
# - X-Frame-Options 必须 SAMEORIGIN：插件页由主页面以**同源 iframe** 嵌入
#   （见 /plugin/<id>/ 路由），DENY 会把所有插件页一并挡掉；
# - CSP 保守处理：只禁 object/embed（object-src 'none'）与 <base> 劫持
#   （base-uri 'self'）。**不设** script-src/style-src——既有页面大量使用
#   内联 <script>/onclick 与动态 style，收紧会直接白屏（上 nonce/hash 需先
#   改造全部前端，另立批次）；
# - 用 setdefault 而非覆盖：允许具体响应（如 send_file 的缓存/下载语义）自行
#   设置同名头，但插件与页面不做特殊处理时一律有这三项基础防护。
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "object-src 'none'; base-uri 'self'",
}


def _security_headers(response):
    """给所有响应补齐基础安全头（含错误响应：Flask 对 413/404 同样过 after_request）。"""
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


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


def _log_response(response):
    """记录本次请求：用户名 / IP / 方法 / 功能 / 具体操作 / 路径 / 状态 / 耗时 / UA。

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

    user = get_current_user()
    username = user.get("username") if user else "-"

    access_logger.info(
        "ip=%s\tuser=%s\tmethod=%s\tfunc=%s\top=%s\tpath=%s\tstatus=%s\tcost_ms=%s\tua=%s",
        get_client_ip(),
        username,
        request.method,
        parse_feature(),
        parse_operation(),
        request.path,
        response.status_code,
        cost_ms,
        (request.headers.get("User-Agent", "") or "")[:120],
    )
    return response


def install(app):
    """注册请求/响应外壳钩子（app.py 装配期调用）。

    注册顺序敏感：after_request 按注册逆序执行——日志必须最后注册（最先执行），
    记录的是压缩前的原始耗时与状态（见 _log_response 注释），勿调整本函数内的顺序。
    """
    app.before_request(_log_start)
    app.after_request(_compress_response)
    app.after_request(_security_headers)
    app.after_request(_static_cache)
    app.after_request(_log_response)
