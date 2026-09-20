"""JZToolsHub 框架 API 门面 —— 主体对插件承诺的稳定接口层（plugin_api = 1）。

为什么存在（见 `docs/design/主体与插件解耦-设计文档.md` §5.1 FC-3、§5.4）：
    插件的会话工具过去从 **admin 插件的内部模块** `jztools_admin.routes` 导入，
    形成「插件 → 另一个插件的内部文件」的隐式契约（耦合点 K-3）；主体自身也反向
    依赖该模块（K-2）。本模块把这条依赖**倒置**为主体模块：admin 在 `register(app)`
    时注册实现（provider），主体与插件一律从本模块取用——插件之间因此互不依赖。

降级语义（与既有 try/except 兜底逐条等价）：
    未注册 provider（admin 未加载 / 加载失败）时：
      - `get_session_user()` → None（等价"未登录"，调用方按匿名处理）
      - `set_operation()`     → 空操作（不落任何标签）
      - `get_org_tree()`      → None（调用方按"组织架构不可用"降级）
    调用方**不再需要写 try/except**，直接调用即可。

组织架构树（设计文档 §5.4 第一处修补）：
    notice-board 等插件过去由前端直调 admin 的 `/api/admin/org-tree`；现在由主体
    暴露 `/api/org/tree`（见 app.py），数据经本模块的 provider 取自 admin——
    "插件调另一个插件"变成"插件调主体提供的框架 API"。

纪律（与 `jztools_data.py` 相同，务必遵守）：
    仅标准库、导入无副作用、**不得 import 任何插件**、避免循环依赖。
    本模块被 app.py 与各插件后端共同 import。
"""

import logging

log = logging.getLogger("jztools.api")

# provider 表：由核心插件 admin 在 register(app) 时注册（模块级单例）
_PROVIDERS = {
    "session_user": None,   # () -> dict | None
    "set_operation": None,  # (str) -> None
    "org_tree": None,       # () -> list | None
}


def register_providers(get_session_user=None, set_operation=None, get_org_tree=None):
    """注册框架 API 的实现（只由核心插件 admin 调用）。

    允许部分注册（缺省项保持既有实现不变）；重复注册直接覆盖，便于自重启/重载场景。
    """
    if get_session_user is not None:
        _PROVIDERS["session_user"] = get_session_user
    if set_operation is not None:
        _PROVIDERS["set_operation"] = set_operation
    if get_org_tree is not None:
        _PROVIDERS["org_tree"] = get_org_tree
    log.debug("框架 API provider 已注册：%s", providers_registered())


def providers_registered():
    """返回各 provider 是否已注册（诊断用，后台/日志可展示）。"""
    return {name: fn is not None for name, fn in _PROVIDERS.items()}


def get_session_user():
    """当前登录用户的展示信息（含 permissions / super_admin），未登录或 admin 未加载返回 None。"""
    fn = _PROVIDERS["session_user"]
    if fn is None:
        return None
    try:
        return fn()
    except Exception:  # 会话读取失败按未登录处理，不阻断请求
        return None


def set_operation(op):
    """标记当前请求的「具体操作」标签（供访问日志使用）；无 provider 时空操作。"""
    fn = _PROVIDERS["set_operation"]
    if fn is None:
        return
    try:
        fn(op)
    except Exception:
        pass


def get_org_tree():
    """单位→部门→用户 树（只读，仅标识与姓名）；admin 未加载时返回 None。"""
    fn = _PROVIDERS["org_tree"]
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def is_logged_in():
    """当前请求是否已登录（get_session_user() 的布尔便捷形式）。"""
    return get_session_user() is not None
