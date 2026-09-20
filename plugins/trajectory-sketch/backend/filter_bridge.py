# -*- coding: utf-8 -*-
"""过滤能力门面：转发到**本插件自带**的实现，插件之间零依赖。

背景（设计文档《主体与插件解耦》§5.4 第二处修补 / D-9）
------------------------------------------------------
本插件过去通过 HTTP（进程内 ``test_client`` 或回环 ``requests``）调用「过滤器」插件的
``POST /api/file-filter/apply``，还要读 ``config/tools.json`` 判断对方是否启用。那是
"未声明、无版本校验、不进依赖判定"的隐式跨插件耦合，对方被下线时本插件的字段自检与
轨迹分析会直接不可用。

现在过滤能力在本插件内**自带一份**（``filter_local.py``，与 file-filter 的
``core.py`` 同源副本、各自独立演进），本模块只做转发与兼容：

* 保留原有公开名字（``apply_filter`` / ``FilterError`` / ``filter_status`` /
  ``filter_llm_configured`` / ``REQUESTS_AVAILABLE``），routes.py 无需大改；
* 不再有 HTTP 调用、不再读 ``tools.json``、不再需要 ``requests``；
* **行为变化**：``mode="llm"``（大模型辅助匹配）依赖过滤器插件自己的大模型配置，
  已随解耦移除——请求 llm 时回退为硬过滤，并在响应里给出 ``warning``（见 filter_local）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import filter_local
from .filter_local import FilterError                     # 兼容既有 `filter_bridge.FilterError`

# 不再需要 HTTP 回环（保留名字：/status 输出与既有调用方仍会读它）
REQUESTS_AVAILABLE = False
# 过滤能力已内置；保留常量避免外部引用报错（不再指向另一个插件）
FILTER_PLUGIN_ID = "trajectory-sketch"


# --------------------------------------------------------------------------
# 可用性自检（本地实现恒可用）
# --------------------------------------------------------------------------

def filter_enabled() -> bool:
    """过滤能力是否可用——本地实现恒为 True（不再依赖其他插件）。"""
    return True


def filter_route_available(app=None) -> bool:
    """兼容保留：不再有"对方路由"这一说，恒为 True。"""
    return True


def filter_status(app=None) -> Dict[str, Any]:
    """过滤能力状态摘要（给 ``/status`` 与前端用）。"""
    return filter_local.status()


def filter_llm_configured(app=None, cookie: str = "", host_url: str = "") -> bool:
    """大模型辅助匹配是否可用——**恒为 False**（该能力依赖过滤器插件的大模型配置，已随解耦移除）。

    前端据此禁用「大模型辅助」选项（既有行为：llm_configured 为假时自动回退硬过滤）。
    """
    return False


# --------------------------------------------------------------------------
# 主调用
# --------------------------------------------------------------------------

def apply_filter(app=None, rows: Sequence[Sequence[Any]] = (), mode: str = "hard",
                 columns: Optional[List[str]] = None,
                 post_rules: Optional[List[Dict[str, Any]]] = None,
                 cookie: str = "", host_url: str = "") -> Dict[str, Any]:
    """执行过滤（本插件自带实现）。

    保留 ``app`` / ``cookie`` / ``host_url`` 形参只为兼容既有调用方（内部不再使用）。
    失败抛 :class:`FilterError`（``status`` 为建议的 HTTP 状态码）。
    """
    return filter_local.apply_filter(rows, mode=mode, columns=columns, post_rules=post_rules)
