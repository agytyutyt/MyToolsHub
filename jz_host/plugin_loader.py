"""宿主插件注册表与加载：tools.json 注册表、manifest 扫描、依赖门控、后端动态注册。

从 app.py 拆出（2026-10-10 P2-4 / 报告 §4.4「注册表 + 插件加载与门控」块）。
程序目录与 tools.json 路径由 app.py 装配期经 set_paths 注入；本模块不自行推导。

对外（供 app.py / 台架）：REGISTRY_LOCK、load_registry / save_registry /
load_manifests / load_tool_meta / get_plugin_dir / get_aggregated_tools /
_refresh_plugin_states / register_plugin_backends / load_state_snapshot。
"""
import json
import logging
import os
import re
import threading
import time

import jz_deps
import jz_llm
import jz_plugin_host

# 注：jztools_data 不在模块顶部导入——测试沙箱会按相位整副本替换该模块（sys.path 优先），
# 而本模块长驻 sys.modules 不随相位重载；顶部导入会把绑定冻结成第一个相位的副本。
# 所以凡用到它的函数一律在**调用期**导入（与 jz_deps.apply_pending_components 同一手法）。

log = logging.getLogger("jztools.app")

# 插件 ID 只允许出现在目录名中，防止目录穿越
PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

REGISTRY_LOCK = threading.RLock()   # D2：config/tools.json 读改写全程锁（RLock 便于同线程复入）

# 程序目录 / tools.json 路径（占位；app.py 装配期与 init_data_root() 时注入）
_BASE_DIR = None
_CONFIG_PATH = None
_PLUGINS_DIR = None

# load_tool_meta 的短 TTL 缓存（仅服务日志解析，避免每请求读文件）
_TOOL_META_TTL = 2.0  # 秒
_tool_meta_cache = None
_tool_meta_cache_ts = 0.0

# 插件主动声明的首页卡片内容钩子 {插件id: home_card 可调用对象}
# 由 register_plugin_backends() 在启动时收集；每次请求（如首页 /api/tools）时
# 于请求上下文内调用，使插件能按当前登录用户权限动态声明卡片内容。
# 优先级高于 config/tools.json：声明了则用之，未声明则回退读取 tools.json。
_plugin_home_card_hooks = {}

# 插件后端加载失败记录 {插件id: "异常类型: 摘要"}。
# 单个插件加载失败只影响它自己，绝不允许拖垮整站启动（规范 B-4 的同一精神）：
# 典型场景是两个插件各写了 def status()，Flask 的 endpoint 重名会在注册阶段抛
# AssertionError。任何异常都在 register_plugin_backends() 内被隔离并记录于此，
# 供启动日志与故障排查使用；每次 register_plugin_backends() 执行时清空。
_plugin_load_errors = {}

# 单个插件 manifest.json 损坏的记录 {插件id: "异常类型: 摘要"}（load_manifests 填充）。
# 解耦后插件目录由外部介质写入，坏文件概率上升：只跳过该插件，不拖垮整站。
_manifest_errors = {}
# tools.json 读取/解析失败的记录（空串表示正常）。
_registry_error = ""

# 插件运行状态 {插件id: {"status": "ok|degraded|blocked", "missing": […], "reason": "…"}}。
# 由 register_plugin_backends() 在加载前求值（jz_deps.evaluate）：版本门控 + 三类依赖，
# 三态结论——blocked 不加载、degraded 加载但标注；供 /api/tools 与后台展示
# （见 docs/design/主体与插件解耦-设计文档.md §5.2 / §5.3）。
_plugin_states = {}


def set_paths(base_dir, config_path):
    """注入程序目录与 tools.json 路径（app.py 装配期与 init_data_root() 调用）。"""
    global _BASE_DIR, _CONFIG_PATH, _PLUGINS_DIR
    _BASE_DIR = base_dir
    _CONFIG_PATH = config_path
    _PLUGINS_DIR = os.path.join(base_dir, "plugins")


def load_state_snapshot():
    """加载/门控状态快照（/api/tools 组装响应时取用，标题同既有响应字段）。"""
    return {
        "plugin_errors": dict(_plugin_load_errors),
        "plugin_states": dict(_plugin_states),
        "registry_error": _registry_error,
        "manifest_errors": dict(_manifest_errors),
    }


def load_registry():
    """加载后台配置(config/tools.json)，这是工具的注册清单。

    健壮性（解耦后插件目录由外部介质写入，坏文件概率上升）：读取/解析失败时
    返回空注册表并告警，绝不让单个坏文件把整站打挂（见设计文档 §2 健壮性缺口）。

    D2：本函数与 `save_registry` 单步都是安全的（读一次全量、写用唯一临时名 +
    replace 原子落盘）；跨步的 `load → 改 → save` 丢失更新由调用方用模块级
    `REGISTRY_LOCK` 整段包住（见 `/api/tools/reorder`、`/api/tools/visibility`）。
    """
    try:
        # utf-8-sig（D4 口径统一）：与 jz_deps / jz_llm 一致，容忍人工编辑留下的 BOM。
        with open(_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {"site": {}, "categories": [], "tools": []}
    except Exception as exc:
        global _registry_error
        _registry_error = f"{type(exc).__name__}: {exc}"
        log.warning("tools.json 不可读/不可解析，已按空注册表继续：%s", _registry_error)
        return {"site": {}, "categories": [], "tools": []}


def save_registry(registry):
    """原子写入 config/tools.json（统一走 ``jztools_data.atomic_write_json``：唯一临时名 + rename）。

    固定名 `tools.json.tmp` 在同进程并发写（请求线程 + 后台线程）或上次写入被强杀留残file时，
    会出现两个写者往同一个临时文件落笔、后者 `os.replace` 把前者的半成品提升为正式文件。
    唯一临时名（`tempfile.mkstemp` 同目录）消除该冲突面；失败清理临时文件不留残骸。
    """
    import jztools_data          # 调用期导入，见模块头注
    jztools_data.atomic_write_json(_CONFIG_PATH, registry)


def load_manifests():
    """扫描 plugins/ 下所有插件目录，读取各自 manifest.json。

    健壮性：单个插件 manifest 缺失/损坏只跳过该插件并记入 `_manifest_errors`，
    其余插件与整站启动不受影响（设计文档 §2 健壮性缺口 / AC-6）。
    """
    manifests = {}
    _manifest_errors.clear()
    if not os.path.isdir(_PLUGINS_DIR):
        return manifests
    for name in os.listdir(_PLUGINS_DIR):
        manifest_path = os.path.join(_PLUGINS_DIR, name, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            # utf-8-sig：manifest.json 是人工可编辑文件，记事本"UTF-8"另存会带 BOM，
            # 普通 utf-8 读到 BOM 直接 JSONDecodeError（本函数按「跳过该插件」降级，难排查）。
            with open(manifest_path, "r", encoding="utf-8-sig") as f:
                obj = json.load(f)
            if not isinstance(obj, dict):
                raise ValueError("manifest 不是 JSON 对象")
            # 加载期一致性校验（D7）：manifest.id 必须等于目录名。聚合接口按 manifest.id
            # 取展示元信息、可见性判定按 registry/目录名 id，两者不一致会静默错位。
            mid = str(obj.get("id") or "").strip()
            if mid != name:
                raise ValueError("manifest.id=%r 与目录名 %r 不一致" % (obj.get("id"), name))
            manifests[name] = obj
        except Exception as exc:
            _manifest_errors[name] = f"{type(exc).__name__}: {exc}"
            log.warning("插件 %s 的 manifest.json 不可解析，已跳过：%s", name, exc)
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
    return os.path.join(_PLUGINS_DIR, plugin_id)


def _resolve_home_card(plugin_id):
    """按当前请求上下文解析插件主动声明的首页卡片内容。

    钩子 home_card() 在每次请求（如首页 /api/tools）时实时求值，使插件
    能依据当前登录用户的权限返回动态卡片内容；未提供钩子、返回值非 dict
    或求值异常时返回空 dict，由调用方回退读取 tools.json / manifest。
    """
    hook = _plugin_home_card_hooks.get(plugin_id)
    if hook is None:
        return {}
    try:
        result = hook()  # 在请求上下文内调用，插件可用 flask.request / get_session_user()
        return result if isinstance(result, dict) else {}
    except Exception:
        log.debug("插件 %s 的首页卡片钩子求值失败，回退 tools.json/manifest", plugin_id, exc_info=True)
        return {}


def _gate_state_of(verdict):
    """门控结论（jz_deps.evaluate）→ _plugin_states 记录。

    启动加载与请求期刷新两处组装点共用同一映射，避免字段集在两边悄悄漂移。
    """
    return {
        "status": verdict["status"],
        "missing": verdict["missing"],
        "degraded": verdict.get("degraded", []),
        "reason": verdict.get("reason", ""),
    }


def _refresh_plugin_states():
    """请求期重算**已加载**插件的依赖状态（只重算当前非 ok 的那些）。

    为什么需要：`_plugin_states` 在启动时算一次；服务运行中装了「依赖组件包」后，
    插件页面横幅会立刻变好（jz_deps.refresh_flags 实时刷新），但首页卡片/工具列表
    若继续用启动时的结论，就会一边显示"功能降级"一边功能可用——自相矛盾。
    blocked 的插件仍以启动结论为准（它们**没被加载**，装依赖后必须重启才能加载路由）。
    """
    manifests = None
    for pid, st in list(_plugin_states.items()):
        if not st or st.get("status") == "ok" or st.get("status") == "blocked":
            continue
        # 注：blocked 的插件在启动时就**没有加载路由**（上面已跳过），装依赖后必须重启
        # 才能让路由生效，故不在这里重算。
        if manifests is None:
            manifests = load_manifests()
        manifest = manifests.get(pid) or {}
        plugin_dir = get_plugin_dir(pid)
        if not plugin_dir or not manifest:
            continue
        _vj = jz_deps.read_version_json(_BASE_DIR)
        verdict = jz_deps.evaluate(pid, plugin_dir, manifest, app_dir=_BASE_DIR,
                                   app_version=_vj.get("app"), plugin_api=_vj.get("plugin_api"))
        if verdict["status"] != st.get("status"):
            log.info("插件 %s 依赖状态刷新：%s → %s", pid, st.get("status"), verdict["status"])
            _plugin_states[pid] = _gate_state_of(verdict)


def get_aggregated_tools():
    """聚合后台配置与插件清单，生成前端可用的工具列表。

    首页卡片内容采用两级读取：
    1. 插件主动声明（方式一）：插件后端提供可选钩子 home_card()，启动时登记、
       首页请求时于请求上下文内实时求值（可按当前登录用户权限返回动态内容），
       结果作为卡片展示字段的最高优先级来源；
    2. tools.json 兜底（方式二）：插件未提供钩子时，name / description 取
       config/tools.json（权威），icon / accent / features 取 manifest.json。

    健康状态（解耦后新增）：门控结论为 blocked 的插件**不出现在卡片里**
    （避免"能打开但一用就报错"）；degraded 的照常展示并带 health 字段供前端标角标。
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
        if item.get("hidden"):  # 隐藏（如管理后台插件）不展示为工具卡片
            continue
        if item.get("category") in hidden_cats:
            continue
        manifest = manifests.get(item["id"])
        if manifest is None:
            continue
        state = _plugin_states.get(item["id"]) or {}
        if state.get("status") == "blocked":
            continue  # 不可运行：不出现卡片（缺失清单在后台可见）
        m = meta.get(item["id"], {})
        # 方式一：插件主动声明的卡片内容优先（未声明则为空 dict，走 tools.json 兜底）
        card = _resolve_home_card(item["id"])
        icon = card.get("icon") or manifest.get("icon", "🧩")
        tools.append({
            "id": manifest.get("id", item["id"]),
            "name": card.get("name") or m.get("name") or manifest.get("name", item["id"]),
            "description": card.get("description")
                if card.get("description") is not None
                else (m.get("description") or manifest.get("description", "")),
            "icon": icon,
            # emoji 图标的 SVG 回退只在**前端一处**维护：static/js/jz-icon.js
            # （旧方案在此再维护一份 emoji→文件名硬编码表并把 icon_file 塞进 /api/tools，
            #  实测前端从不消费，等于两处映射表要同步维护——已删，见 20260914 评估报告 D-3）。
            "accent": card.get("accent") or manifest.get("accent", "#4285F4"),
            "entry": manifest.get("entry", "index.html"),
            "features": card.get("features")
                if card.get("features") is not None
                else manifest.get("features", []),
            "category": category_map.get(item.get("category"), "未分类"),
            "category_id": item.get("category", ""),
            "order": item.get("order", 0),
            # 健康状态：ok / degraded（可选依赖缺失，前端可标"功能降级"角标）
            "health": state.get("status", "ok"),
            "health_degraded": state.get("degraded", []),
        })
    tools.sort(key=lambda t: t["order"])
    return tools


def _load_backend_module(plugin_id):
    """载入插件后端模块（GD8 档2：实现已抽取至 jz_plugin_host，插件目录参数化）。"""
    return jz_plugin_host.load_backend_module(_PLUGINS_DIR, plugin_id)


def _load_plugin_routes(app, plugin_id):
    """加载单个插件后端；任何异常都被隔离，不影响其他插件与整站启动。

    注意两个已知边界（不要在排查时误判为"万无一失"）：
    1. register(app) 中途抛错时，它之前已注册的路由会残留在 Flask 上（无法
       回滚）。隔离保证的是「整站能起来 + 错误可诊断」，不是「失败插件不留
       半个后端」——根治仍靠路由函数命名纪律（带插件前缀）。
    2. _load_backend_module 用 importlib.import_module 取 `.routes`，已缓存
       的模块不会刷新；同进程内重复调用会拿到旧代码。插件后端改动必须重启
       进程才生效。
    """
    try:
        routes = _load_backend_module(plugin_id)
        if routes is None:
            return False
        register = getattr(routes, "register", None)
        if not callable(register):
            _plugin_load_errors[plugin_id] = "未导出可调用的 register(app)"
            app.logger.error(
                "插件 %s 的后端未导出 register(app)，已跳过", plugin_id
            )
            return False
        register(app)
    except Exception as exc:
        _plugin_load_errors[plugin_id] = f"{type(exc).__name__}: {exc}"
        app.logger.exception(
            "插件 %s 的后端加载失败，已隔离：其他插件与整站启动不受影响",
            plugin_id,
        )
        return False

    app.logger.info(f"已注册后端插件：{plugin_id}")
    # 进程内插件 id 集（E 架构统一 errorhandler 判定用，见 supervisor/mainapp_bridge）
    app.config.setdefault("_PLUGIN_IDS", set()).add(plugin_id)
    # 方式一：登记插件首页卡片内容声明钩子（每次请求实时求值，失败不阻断加载）
    hook = getattr(routes, "home_card", None)
    if callable(hook):
        _plugin_home_card_hooks[plugin_id] = hook
        app.logger.info(
            f"插件 {plugin_id} 提供 home_card() 首页卡片内容声明钩子"
        )
    return True


def _worker_units_snapshot():
    """supervisor 注册表快照（E 架构 worker 化单元）；未启用/不可读时返回空 dict。"""
    import jztools_data          # 调用期导入，见模块头注
    try:
        from supervisor.mainapp_bridge import worker_units_snapshot
        return worker_units_snapshot(jztools_data.get_data_root())
    except Exception:
        log.debug("supervisor 注册表快照不可用，按未 worker 化处理", exc_info=True)
        return {}


def _register_tool_plugin(app, item, manifests, version_json, worker_units):
    """按门控结论注册单个（非 admin）插件。

    版本与依赖门控（设计文档 §5.2 / §5.3）：加载前逐插件求值，三态结论——
      ok       正常加载
      degraded 仅可选依赖缺失 → 加载，列表/卡片标注"功能降级"
      blocked  必需依赖缺失或版本门控不满足 → **不加载**并记录原因
    与既有"加载失败隔离"同一条纪律：任何单插件问题都不拖垮整站。

    E 架构（需求⑩）：supervisor 注册表里有单元的插件 = worker 化 → 跳过进程内
    加载，/api/<id>/* 由反代桥转发到 worker（supervisor/mainapp_bridge）。迁移期
    两种形态并存：未 worker 化的插件走既有进程内链，一条一条切。
    """
    plugin_id = item["id"]
    if plugin_id == "admin" or not item.get("enabled", True):
        return
    if plugin_id in worker_units:
        _plugin_states[plugin_id] = {"status": "ok", "mode": "worker",
                                     "missing": [], "degraded": [],
                                     "reason": "supervisor 托管（E 架构）"}
        app.logger.info("插件 %s 已 worker 化（supervisor 托管），跳过进程内加载", plugin_id)
        return
    manifest = manifests.get(plugin_id) or {}
    plugin_dir = get_plugin_dir(plugin_id)
    if not plugin_dir or not manifest:
        # manifest 损坏/缺失：load_manifests 已记录，这里只标注状态
        if plugin_id in _manifest_errors:
            _plugin_states[plugin_id] = {"status": "blocked",
                                         "missing": ["manifest.json"],
                                         "reason": "manifest.json 不可解析"}
        return
    verdict = jz_deps.evaluate(plugin_id, plugin_dir, manifest, app_dir=_BASE_DIR,
                               app_version=version_json.get("app"),
                               plugin_api=version_json.get("plugin_api"))
    _plugin_states[plugin_id] = _gate_state_of(verdict)
    if verdict["status"] == "blocked":
        why = verdict.get("reason") or ("缺少必需依赖：" + "、".join(verdict["missing"]))
        app.logger.warning("插件 %s 不可运行，已跳过加载（%s）", plugin_id, why)
        return
    if verdict["status"] == "degraded":
        app.logger.warning("插件 %s 功能降级（可选依赖缺失：%s）",
                           plugin_id, "、".join(verdict.get("degraded", [])))
    _load_plugin_routes(app, plugin_id)


def register_plugin_backends(app):
    """动态注册插件后端。

    每个插件在 plugins/<插件id>/backend/ 下提供 __init__.py 与 routes.py，
    后端代码随插件整体打包、随 enabled 配置启停 —— 一切皆插件。

    插件若提供可选钩子 home_card()，则启动时登记该钩子；首页 /api/tools
    请求时于请求上下文内实时调用（可依据当前登录用户权限动态声明卡片内容）。
    未提供钩子的插件，其卡片内容回退读取 config/tools.json（见
    get_aggregated_tools 的合并逻辑）。

    admin（管理后台）为核心基础设施插件：不随 enabled 启停，始终加载，
    保证登录鉴权 / 后台接口 / 工具访问控制一直可用。

    加载失败隔离：每个插件的后端加载（模块导入 + register(app)）整体包在
    try/except 内。任一插件因 endpoint 冲突、依赖缺失、语法错误等原因失败时，
    只记录到 _plugin_load_errors 并打日志告警，其余插件与整站启动不受影响。

    版本与依赖门控（解耦后新增，设计文档 §5.2 / §5.3）：加载前逐插件求值
    「版本门控 + 三类依赖」三态结论——blocked 不加载并记录缺失清单，
    degraded 照常加载但标注；结论存于 _plugin_states，供 /api/tools 与后台展示。
    """
    global _plugin_home_card_hooks
    _plugin_home_card_hooks = {}  # 每次启动重新收集，避免跨重启残留旧钩子
    _plugin_load_errors.clear()
    _plugin_states.clear()

    # 统一大模型模块的依赖标记（REQUESTS_AVAILABLE）也按请求刷新：
    # 服务运行中装「依赖组件包 requests」后，无需重启即可用上大模型功能
    # （与各插件 register() 里的 jz_deps.install_refresher 同一机制）。
    jz_deps.install_refresher(app, jz_llm)

    registry = load_registry()
    tools = list(registry.get("tools", []))

    # 管理后台：始终加载（即使 tools.json 中未注册或 enabled=false）
    if not _load_plugin_routes(app, "admin"):
        app.logger.warning("核心插件 admin 未加载，登录鉴权 / 工具访问控制将不可用")

    manifests = load_manifests()
    version_json = jz_deps.read_version_json(_BASE_DIR)
    worker_units = _worker_units_snapshot()
    for item in tools:
        _register_tool_plugin(app, item, manifests, version_json, worker_units)
