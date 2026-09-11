# -*- coding: utf-8 -*-
"""算法注册表（解耦约束 D-4）。

轨迹分析算法会持续演进（上游 SQLRewrite 已有 v1/v2，未来还有 v3）。本模块让
"新增 / 替换算法"变成一个**注册动作**，而不是"改路由、改前端、改配置读取"：

    # engine/algorithms/v3/__init__.py
    from ... import registry
    def run(dataset, params):
        ...
    registry.register("v3", run)

    # engine/algorithms/__init__.py
    from . import v2, v3          # 导入即注册

路由层只按配置的 ``analysis.algo`` 取值，不感知任何具体算法实现。

算法签名（固定，不得更改）
--------------------------
``run(dataset: Dataset, params: dict) -> dict``
  返回值为 JSON 可序列化 dict，至少包含 ``quality / stays / trips / report`` 四键；
  形状见 ``engine/__init__.py`` 的模块文档。
"""
from __future__ import annotations

from typing import Callable, Dict, List

_REGISTRY: Dict[str, Callable] = {}


def register(version: str, run: Callable) -> None:
    """注册 / 覆盖一个算法版本。version 建议与目录名一致（如 "v2"）。"""
    key = str(version).strip()
    if not key:
        raise ValueError("算法版本名不能为空")
    if not callable(run):
        raise TypeError("run 必须是可调用对象")
    _REGISTRY[key] = run


def get(version: str) -> Callable:
    """取算法实现；未注册时抛 KeyError（由 engine 转成面向用户的中文错误）。"""
    return _REGISTRY[str(version).strip()]


def has(version: str) -> bool:
    return str(version).strip() in _REGISTRY


def versions() -> List[str]:
    """已注册的算法版本（按名称排序，便于前端下拉展示）。"""
    return sorted(_REGISTRY.keys())


def clear() -> None:
    """仅测试用：清空注册表。"""
    _REGISTRY.clear()
