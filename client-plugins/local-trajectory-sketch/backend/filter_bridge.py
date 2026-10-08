# -*- coding: utf-8 -*-
"""过滤能力门面：转发到**本插件自带**的实现，插件之间零依赖。

背景（设计文档《主体与插件解耦》§5.4 第二处修补 / D-9）
------------------------------------------------------
本插件过去通过 HTTP（进程内 ``test_client`` 或回环 ``requests``）调用「过滤器」插件的
``POST /api/file-filter/apply``，还要读 ``config/tools.json`` 判断对方是否启用。那是
"未声明、无版本校验、不进依赖判定"的隐式跨插件耦合，对方被下线时本插件的字段自检与
轨迹分析会直接不可用。

现在过滤能力在本插件内**自带一份**（``filter_local.py``，与 file-filter 的
``core.py`` 同源副本、各自独立演进），本模块只做转发：
``apply_filter`` / ``FilterError`` / ``filter_status`` 仍是对外名字，routes.py 无需大改。

大模型辅助（mode=llm）为什么是"恢复"而不是"移除"（2026-09-24）
------------------------------------------------------------
解耦当时一并移除了大模型辅助匹配，理由写的是"它依赖「过滤器」插件自己的大模型配置"。
**该前提已随统一大模型模块（主体模块 ``jz_llm``）落地而失效**：接入信息由管理员在
「大模型设置」维护，``jz_llm.resolve()`` 的 ``plugin_id`` 只用于日志定位、不参与解析；
``jz_llm`` 又在 ``FRAMEWORK_MODULES`` 白名单内（见 ``tools/build-plugin-package.ps1``
的 C-4/C-11），插件 import 它是正规出口。因此本插件恢复 mode=llm，走框架模块取用，
**不引入任何跨插件依赖、也不再需要 requests**（HTTP 由 jz_llm 发起）。

线程纪律（重要）
----------------
``llm_client.available()`` 与 ``jz_llm.resolve()`` 要读会话，**只能在请求线程内调用**。
后台分析线程必须用请求线程取好的 ``session`` 快照（见 ``routes._run_analysis``）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import filter_local, llm_client
from .filter_local import FilterError                     # 兼容既有 `filter_bridge.FilterError`

# 过滤能力已内置；保留常量避免外部引用报错（不再指向另一个插件）
FILTER_PLUGIN_ID = "trajectory-sketch"


# --------------------------------------------------------------------------
# 可用性自检
# --------------------------------------------------------------------------

def filter_enabled() -> bool:
    """过滤能力是否可用——本地实现恒为 True（不再依赖其他插件）。"""
    return True


def filter_route_available(app=None) -> bool:
    """兼容保留：不再有"对方路由"这一说，恒为 True。"""
    return True


def filter_status(app=None) -> Dict[str, Any]:
    """过滤能力状态摘要（给 ``/status`` 与前端用）。

    ``llm`` 表示大模型辅助是否已配置（``jz_llm`` 解析结果，**请求线程内调用**）。
    """
    st: Dict[str, Any] = dict(filter_local.status())
    st["llm"] = llm_client.available()
    return st


def filter_llm_configured(app=None, cookie: str = "", host_url: str = "") -> bool:
    """大模型辅助是否可用：已配置接入信息即为真（**请求线程内调用**）。

    形参 ``app`` / ``cookie`` / ``host_url`` 只为兼容既有调用方，内部不再使用
    （接入信息由框架模块 ``jz_llm`` 统一解析，与其它插件无关）。
    """
    return llm_client.available()


def llm_session():
    """取接入配置快照（**只能在请求线程内调用**），取到后传进后台任务。

    用法见 ``jz_llm`` 模块头的线程纪律：后台线程读不到会话，必须由请求线程传入。
    """
    return llm_client.session()


# --------------------------------------------------------------------------
# 主调用
# --------------------------------------------------------------------------

def apply_filter(app=None, rows: Sequence[Sequence[Any]] = (), mode: str = "hard",
                 columns: Optional[List[str]] = None,
                 post_rules: Optional[List[Dict[str, Any]]] = None,
                 cookie: str = "", host_url: str = "",
                 session: Any = None) -> Dict[str, Any]:
    """执行过滤（本插件自带实现；``mode="llm"`` 时经框架模块 ``jz_llm`` 做语义匹配）。

    保留 ``app`` / ``cookie`` / ``host_url`` 形参只为兼容既有调用方（内部不再使用）。
    ``session`` 仅在 ``mode="llm"`` 时需要，且必须由请求线程取得后传入。
    失败抛 :class:`FilterError`（``status`` 为建议的 HTTP 状态码）。
    """
    return filter_local.apply_filter(rows, mode=mode, columns=columns,
                                     post_rules=post_rules, session=session)
