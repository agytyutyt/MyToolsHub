"""JZToolsHub 框架入口（宿主外壳）。

做三件事（实现按职责拆在 ``jz_host/`` 包，本文件只做装配与路由——2026-10-10
P2-4 / 报告 §4.4）：
  · 应用装配：Flask 实例与内核配置、jz_host 五模块接线、核心路由（首页 / 工具壳页 /
    插件资源 / 工具与站点接口 / 可见性与排序）；
  · 启动流程：数据根初始化 → 访问日志 → 插件历史大模型配置清理 → 插件后端注册 →
    E 架构反代桥 → 生产服务器（waitress）/ 托盘；
  · 专用 CLI：--check-deps / --wait-restart / --worker（冻结运行由安装器与自动重启使用）。

插件面向的稳定接口是 jz_api / jz_deps / jz_llm / jztools_data，不是本文件的内部结构。
"""
import os
import sys
import threading

from flask import Flask, jsonify, request, send_from_directory

import jz_api
import jztools_data
from jz_host import access_log, dep_bootstrap, llm_routes, plugin_loader, tray

# ★ 以下名字经 from-import 进入本模块命名空间（**勿改为 plugin_loader.xxx 模块限定调用**）：
#   台架（admin-config-lock 等）在进程内对 app 模块的同名属性打桩（如 load_registry、
#   REGISTRY_LOCK）来放大竞态，模块限定调用会绕过打桩。
from jz_host.dep_bootstrap import dep_components_status, migrate_plugin_legacy_llm
from jz_host.plugin_loader import (
    REGISTRY_LOCK,
    _refresh_plugin_states,
    get_aggregated_tools,
    get_plugin_dir,
    load_registry,
    load_state_snapshot,
    register_plugin_backends,
    save_registry,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # PyInstaller 打包运行：部署根目录 = exe 所在目录。
    # config / static / plugins / logs 均位于 exe 同层，前端源码可直接修改、
    # 配置与插件运行数据可读写（源码开发时保持 __file__ 所在目录不变）。
    BASE_DIR = os.path.dirname(sys.executable)
# 数据根目录：用户可配置，默认 <用户目录>\.jztoolshub（见 jztools_data.py）。
# 用户数据（tools.json / admin.json / 插件数据 / 日志）随数据根目录存放，
# 程序整体替换升级时数据不丢失。CONFIG_PATH / LOG_DIR / LOG_FILE 在
# init_data_root() 中按数据根目录重算。
DATA_ROOT = None
CONFIG_PATH = os.path.join(BASE_DIR, "config", "tools.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "access.log")

# 装配：把程序目录 / 路径注入插件注册表与访问日志模块（init_data_root 时再按数据根更新）
plugin_loader.set_paths(BASE_DIR, CONFIG_PATH)
access_log.set_log_paths(LOG_DIR, LOG_FILE)

# 依赖组件启动件（应用暂存组件 → 注入 pylibs 路径 → 注册重扫回调）——
# 原模块级副作用块，实现与注释见 jz_host/dep_bootstrap.py。
dep_bootstrap.boot_components(BASE_DIR)


def init_data_root():
    """启动时初始化数据根目录：先迁移旧版程序目录中的用户数据，再重算关键路径。"""
    global DATA_ROOT, CONFIG_PATH, LOG_DIR, LOG_FILE
    paths = dep_bootstrap.resolve_paths()
    DATA_ROOT = paths["data_root"]
    CONFIG_PATH = paths["config_path"]
    LOG_DIR = paths["log_dir"]
    LOG_FILE = paths["log_file"]
    plugin_loader.set_paths(BASE_DIR, CONFIG_PATH)
    access_log.set_log_paths(LOG_DIR, LOG_FILE)


# 打包运行（PyInstaller）时 Flask 内置 /static 默认指向 _internal/static，
# 前端源码保留在 exe 同层，故显式指定 static_folder 指向部署根目录下的 static。
if getattr(sys, "frozen", False):
    app = Flask(
        __name__,
        static_folder=os.path.join(BASE_DIR, "static"),
        static_url_path="/static",
    )
else:
    app = Flask(__name__)
# 会话密钥兜底：admin 插件 register() 时会被 config/admin.json 中的持久化密钥覆盖
app.config["SECRET_KEY"] = __import__("secrets").token_hex(32)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# T27 框架级兜底：单请求体硬上限 640MB（Werkzeug 在解析请求体前拦截，超限 413，
# 不进入视图函数、不占用插件内存）。取值依据：各插件自身上传限额中最宽松的是
# admin 插件包 300MB（plugin_admin.MAX_ZIP_BYTES）与商店上架「插件包 + 前端包」
# 两文件相加（store_admin.check_frontend_zip 同为 300MB）≈600MB，叠加 multipart
# 开销后取 640MB——高于任一插件的合法请求上限（不误伤既有插件），同时给
# 未设自身限额的上传路径（如 trajectory-convert 的 f.read()）一个无界读硬边界。
app.config["MAX_CONTENT_LENGTH"] = 640 * 1024 * 1024

# 请求/响应外壳（访问日志 / gzip / 安全头 / 静态缓存）与 LLM 蓝图装配
access_log.install(app)
llm_routes.install(app)


@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")


@app.route("/tool/<tool_id>")
def tool_page(tool_id):
    return send_from_directory(os.path.join(BASE_DIR, "static"), "tool.html")


@app.route("/plugin/<plugin_id>/<path:filename>")
def plugin_assets(plugin_id, filename):
    """以 iframe 方式对外提供插件前端资源（plugins/<id>/frontend/）。"""
    plugin_dir = get_plugin_dir(plugin_id)
    if not plugin_dir:
        return jsonify({"error": "plugin not found"}), 404
    return send_from_directory(os.path.join(plugin_dir, "frontend"), filename)


@app.route("/api/tools")
def api_tools():
    _refresh_plugin_states()          # 装依赖组件后无需重启：降级标记实时纠正
    registry = load_registry()
    categories = [c for c in registry.get("categories", []) if c.get("enabled", True)]
    return jsonify({
        "site": registry.get("site", {}),
        "categories": categories,
        "tools": _filter_visible_tools(get_aggregated_tools()),
        # 插件健康状况（解耦后新增）：加载失败原因 + 版本/依赖门控三态结论。
        # 首页据此对"不可运行"的插件不显示卡片、对"降级"的插件显示角标
        # （见 docs/design/主体与插件解耦-设计文档.md §5.3 展示面）。
        **load_state_snapshot(),
    })


@app.route("/api/site")
def api_site():
    """轻量站点信息（site 段：title / subtitle 等）。

    工具壳页等只要站点名的前端走这里，不再为一行标题拉全量 /api/tools
    （后者含 manifest 扫描 + 门控状态合并，见 2026-09-29 代码质量分析报告 §4.4）。
    """
    registry = load_registry()
    return jsonify({"site": registry.get("site", {})})


@app.route("/api/tools/<tool_id>")
def api_tool(tool_id):
    tools = _filter_visible_tools(get_aggregated_tools())
    for tool in tools:
        if tool["id"] == tool_id:
            return jsonify(tool)
    return jsonify({"error": "tool not found"}), 404


@app.route("/api/org/tree")
def api_org_tree():
    """框架级组织架构树（单位→部门→用户，只读）。

    主体对插件的稳定契约（FC-4）：数据经 jz_api 的 provider 取自 admin 插件，
    插件（如公告板）调用本接口即"调主体"，不再直连另一个插件的接口——
    插件之间因此互不依赖（见 docs/design/主体与插件解耦-设计文档.md §5.4）。
    未登录由 admin 的全局拦截兜底（401）；admin 未加载时返回 503。
    """
    tree = jz_api.get_org_tree()
    if tree is None:
        return jsonify({"error": "组织架构数据不可用（核心插件未加载）"}), 503
    jz_api.set_operation("查询组织架构树")
    return jsonify({"ok": True, "tree": tree})


def _current_user_allowlist():
    """当前登录用户可见工具 ID 集合；None 表示不限制。

    权限点的唯一读取入口：来自框架 API 门面 `jz_api.get_session_user()` 的
    permissions 字段（逐人授权 + grant_all 全站开放工具；超级管理员
    授予全部）。admin 插件未加载或读取失败时返回 None（不限制），
    与既有降级策略一致。所有需要按权限过滤工具清单的接口都应经过
    本函数，避免各处自行读取造成口径不一。
    """
    info = jz_api.get_session_user()
    if info is None or info.get("super_admin"):
        return None
    return set(info.get("permissions") or [])


def _filter_visible_tools(tools):
    """按当前登录用户的权限点过滤可见工具；匿名或超级管理员不限制。

    权限信息经 jz_api 取自 admin 插件（框架 API 门面），未加载时不做过滤。
    """
    allowed = _current_user_allowlist()
    if allowed is None:
        return tools
    return [t for t in tools if t["id"] in allowed]


def _validate_reorder(registry, categories_order, tools_order):
    """校验排序载荷（类别 / 工具 ID 均须存在）；有问题返回错误响应，否则 None。

    调用方已持 REGISTRY_LOCK（与 _apply_reorder 同在一次读改写事务内）。
    """
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
    return None


def _apply_reorder(registry, categories_order, tools_order):
    """把拖拽排序写进注册表（类别顺序 + 各分类下工具的顺序 / 归属）。

    调用方已持 REGISTRY_LOCK；调用前须先过 _validate_reorder。
    """
    tool_map = {t["id"]: t for t in registry.get("tools", [])}
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

    with REGISTRY_LOCK:                       # D2 全程锁
        registry = load_registry()
        err = _validate_reorder(registry, categories_order, tools_order)
        if err is not None:
            return err
        _apply_reorder(registry, categories_order, tools_order)
        save_registry(registry)
    return jsonify({"ok": True})


@app.get("/api/tools/visibility")
def api_tools_visibility():
    """返回分类与工具及其启用状态，供「隐藏工具」浮窗使用（含已隐藏项）。

    与首页 /api/tools 走同一套权限口径（_current_user_allowlist）：
    未被授权的工具不出现在列表中——用户对其全流程不可见；
    分类仅返回仍含有可见工具者。匿名请求已被强制登录拦截，
    超级管理员不限制。权限设置弹窗所需的全量工具清单走
    /api/admin/permission-points（admin 插件）。
    """
    registry = load_registry()
    allowed = _current_user_allowlist()
    tools = registry.get("tools", [])
    if allowed is not None:
        tools = [t for t in tools if t["id"] in allowed]
    visible_cat_ids = {t.get("category") for t in tools if t.get("category")}
    return jsonify({
        "categories": [
            {"id": c["id"], "name": c["name"], "enabled": c.get("enabled", True)}
            for c in registry.get("categories", [])
            if c["id"] in visible_cat_ids
        ],
        "tools": [
            {"id": t["id"], "name": t.get("name") or t["id"], "enabled": t.get("enabled", True)}
            for t in tools
        ],
    })


def _set_tool_enabled(registry, tid, enabled, allowed):
    """[锁内] 切换单个工具的 enabled；返回 Flask 响应。调用方已持 REGISTRY_LOCK。"""
    for t in registry.get("tools", []):
        if t["id"] == tid:
            if allowed is not None and tid not in allowed:
                return jsonify({"error": "无该工具的操作权限"}), 403
            t["enabled"] = enabled
            save_registry(registry)
            return jsonify({"ok": True})
    return jsonify({"error": "tool not found"}), 404


def _set_category_enabled(registry, tid, enabled, allowed):
    """[锁内] 切换单个分类的 enabled；返回 Flask 响应。调用方已持 REGISTRY_LOCK。"""
    for c in registry.get("categories", []):
        if c["id"] == tid:
            if allowed is not None and not any(
                t.get("category") == tid and t["id"] in allowed
                for t in registry.get("tools", [])
            ):
                return jsonify({"error": "无该分类的操作权限"}), 403
            c["enabled"] = enabled
            save_registry(registry)
            return jsonify({"ok": True})
    return jsonify({"error": "category not found"}), 404


@app.post("/api/tools/visibility")
def api_tools_visibility_save():
    """切换分类 / 工具是否启用：{type: 'tool'|'category', id, enabled}。

    与读取接口同口径：仅允许操作当前用户可见的工具 / 分类
    （无权限时返回 403，防止绕过前端直接改写未授权插件的可见性）。
    """
    data = request.get_json(silent=True) or {}
    typ = data.get("type")
    tid = data.get("id")
    if typ not in ("tool", "category") or not isinstance(tid, str) or not tid:
        return jsonify({"error": "invalid payload"}), 400
    enabled = bool(data.get("enabled"))
    # 权限点先取（要读 admin 数据）——避免在 REGISTRY_LOCK 里嵌套另一把锁
    allowed = _current_user_allowlist()
    with REGISTRY_LOCK:                   # D2 全程锁
        registry = load_registry()
        if typ == "tool":
            return _set_tool_enabled(registry, tid, enabled, allowed)
        return _set_category_enabled(registry, tid, enabled, allowed)


def _run_restart_helper(wait_pid):
    """冻结模式自重启助手（`exe --wait-restart <旧PID>`，由 routes._spawn_self_restart 派出）。

    等待旧服务进程退出后，按原工作目录重新拉起 exe。旧方案（分离 cmd 轮询
    tasklist）在无控制台的分离进程里 tasklist 恒输出为空，助手永远走不到 start
    行，真机自重启从不生效；改用内核句柄等待，不依赖任何控制台工具、不落临时文件。

    返回进程退出码：0=已拉起新实例；1=放弃重启；2=参数非法。
    """
    import ctypes
    import subprocess
    import time

    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(wait_pid))
    if handle:
        try:
            # 30 秒上限：旧进程若因异常迟迟不退出，放弃重启以免出现双实例
            if ctypes.windll.kernel32.WaitForSingleObject(handle, 30000) != WAIT_OBJECT_0:
                return 1
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    # 旧进程已退出：短暂宽限让监听端口/插件文件句柄回归内核，再原位拉起
    time.sleep(1.5)
    base = os.path.dirname(sys.executable)
    flags = 0
    for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    try:
        subprocess.Popen([sys.executable], cwd=base, creationflags=flags, close_fds=True)
    except OSError:
        return 1
    return 0


def _handle_dep_check(argv):
    """`--check-deps`：依赖组件自检（安装器在目标机上用它做"装完能不能真的 import"的验证，T25 第 1 步）。

    ★ 冻结 exe 是 GUI 子系统（console=False），print 无处可去 —— 结果写文件，
      由安装器读取（见 tools/dep-component/install-dep-component.ps1）。
      用法：JZToolsHub.exe --check-deps [<输出json路径>]
    命中返回进程退出码；未命中返回 None。
    """
    if "--check-deps" not in argv:
        return None
    import json as _json
    st = dep_components_status(BASE_DIR)
    try:
        i = argv.index("--check-deps")
        out = argv[i + 1] if len(argv) > i + 1 else os.path.join(
            BASE_DIR, "runtime", "pylibs", ".check-deps.json")
    except (ValueError, IndexError):
        out = os.path.join(BASE_DIR, "runtime", "pylibs", ".check-deps.json")
    ok = all(v == "ok" for v in st["imports"].values()) and bool(st["installed"])
    st["ok"] = ok
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            _json.dump(st, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return 0 if ok else 1


def _handle_wait_restart(argv):
    """`--wait-restart <旧PID>`：自重启助手入口（必须早于任何初始化处理）。

    助手实例自身不对外服务，处理完即退出；参数非法也直接退出（返回 2），
    绝不落回正常启动（防止与旧实例形成双实例）。未命中返回 None。
    """
    if not (getattr(sys, "frozen", False) and "--wait-restart" in argv):
        return None
    try:
        _wait_pid = int(argv[argv.index("--wait-restart") + 1])
    except (IndexError, ValueError):
        return 2
    return _run_restart_helper(_wait_pid)


def _handle_worker(argv):
    """`--worker <插件id>`：E 架构（需求⑩）worker 入口。

    supervisor 拉起，端口 / 数据根 / 主应用地址经环境变量注入——见 jz_worker.py。
    命中返回进程退出码；未命中返回 None。
    """
    if "--worker" not in argv:
        return None
    try:
        _worker_id = argv[argv.index("--worker") + 1]
    except (IndexError, ValueError):
        return 2
    from jz_worker import run_worker
    return run_worker(_worker_id) or 0


def _resolve_server_addr():
    """监听地址：JZTOOLS_HOST（默认 0.0.0.0）/ JZTOOLS_PORT（默认 5000，解析失败回落）。"""
    host = os.environ.get("JZTOOLS_HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("JZTOOLS_PORT", "5000"))
    except ValueError:
        port = 5000
    return host, port


def _serve(app, host, port):
    """启动 HTTP 服务（冻结运行：waitress + 托盘；源码开发：Flask 内置服务器）。"""
    if getattr(sys, "frozen", False):
        # 打包运行（可上线）：waitress 多线程生产级 WSGI 服务器，禁用 debug/reloader
        try:
            from waitress import serve
        except ImportError:
            serve = None
    else:
        serve = None
    if getattr(sys, "frozen", False):
        # 打包运行：无控制台窗口，以系统托盘图标常驻后台（pystray 自带消息循环线程）
        threading.Thread(
            target=tray.run_tray_icon, args=(host, port),
            daemon=True, name="tray-icon",
        ).start()
        # 服务就绪后的系统级通知（托盘气泡；自动重启拉起的新实例同样触发）
        threading.Thread(
            target=tray.notify_service_started, args=(host, port),
            daemon=True, name="startup-notify",
        ).start()
    try:
        if getattr(sys, "frozen", False) and serve is not None:
            serve(app, host=host, port=port, threads=8)
        elif getattr(sys, "frozen", False):
            app.run(host=host, port=port, debug=False)
        else:
            # 源码开发模式：Flask 内置服务器（自动重载 + 调试）
            app.run(host=host, port=port, debug=True)
    finally:
        access_log.shutdown_access_logging()


if __name__ == "__main__":
    # 专用 CLI 先行（冻结运行由安装器与自动重启使用）：命中即退出——助手类参数
    # 尤其不能落回正常启动（会与旧实例形成双实例，见 _handle_wait_restart）。
    for _cli in (_handle_dep_check, _handle_wait_restart, _handle_worker):
        _code = _cli(sys.argv)
        if _code is not None:
            sys.exit(_code)

    init_data_root()  # 迁移旧版程序目录数据 + 解析数据根目录（决定日志/配置位置）
    access_log.setup_access_logging(app)
    # 插件历史大模型配置清理（C1 归属上移，2026-10-09 决策 7）：主体在插件加载前对插件
    # 数据根做一次性清理——先收编历史明文 Key（加密落盘）、再清除插件侧副本、最后写退场
    # 标记（llm_migrated.flag，存在即跳过扫描）。不依赖任何插件注册；删除窗口见函数注释。
    _legacy_llm = migrate_plugin_legacy_llm()
    if _legacy_llm:
        if _legacy_llm["adopted_from"]:
            app.logger.info("插件 %s 的历史大模型配置已收编进统一配置（加密存储）",
                            _legacy_llm["adopted_from"])
        if _legacy_llm["cleared"]:
            app.logger.info("已清除 %d 个插件 config.json 中的历史 llm 段：%s",
                            len(_legacy_llm["cleared"]), "、".join(_legacy_llm["cleared"]))
        if _legacy_llm["dropped_duplicates"]:
            app.logger.info("其余插件的历史 llm 段（重复副本）已丢弃：%s",
                            "、".join(_legacy_llm["dropped_duplicates"]))
        for bad in _legacy_llm["failed"]:
            app.logger.warning("清除插件历史大模型配置失败：%s", bad)
    # 会话密钥 / 登录鉴权 / 后台接口均由 admin 插件在 register() 中初始化
    register_plugin_backends(app)
    # E 架构反代桥（T-O2-12b）：worker 化插件的 /api/<id>/* 转发 + 统一 errorhandler
    # ——必须在 register_plugin_backends 之后注册（before_request 排在 admin 鉴权门
    # 之后执行，鉴权管线先行；见 supervisor/mainapp_bridge.py 模块头）。
    try:
        from supervisor.mainapp_bridge import install_worker_proxy, worker_units_snapshot
        install_worker_proxy(app, BASE_DIR)
        app.logger.info("E 架构反代桥已安装（worker 化插件 %d 个）",
                        len(worker_units_snapshot(jztools_data.get_data_root())))
    except Exception as _e:
        app.logger.warning("E 架构反代桥安装失败（按未启用处理）：%s", _e)
    # 插件后端已按磁盘上的代码加载完毕 → 清掉管理后台留下的"待重启"标记
    # （管理后台在页内应用插件包后标记 restart_pending；见 docs/design/插件独立升级方案-设计文档.md §9）
    try:
        _cleared = jztools_data.clear_plugin_restart_flags()
        if _cleared:
            app.logger.info("已清除 %d 个插件的待重启标记（插件后端已按当前代码加载）", _cleared)
    except Exception as _e:
        app.logger.warning("清除插件待重启标记失败：%s", _e)
    _host, _port = _resolve_server_addr()
    _serve(app, _host, _port)
