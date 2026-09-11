# -*- coding: utf-8 -*-
"""轨迹分析引擎 —— 对外唯一入口（可插拔、零框架依赖）。

一句话职责
----------
把**过滤后的二维表格**变成**结构化的轨迹结论与速写报告**，中间不碰 Flask、不碰数据库、
不碰文件系统。整个 engine/ 包只依赖 Python 标准库（解耦约束 D-1 / D-2）。

对外接口
--------

.. code-block:: python

    from engine import analyze_rows
    result = analyze_rows(rows, params, algo="v2", meta={"kept": [...]})

``rows``
    二维表，首行为表头；单元格可为 str / int / float / datetime。通常是
    「过滤器」插件 ``POST /api/file-filter/apply`` 返回的 ``rows``。

``params``
    插件配置（``config.json``）或已规范化的参数 dict；``None`` 表示全用默认值。
    规范化与默认值见 :mod:`engine.params`。

``algo``
    算法版本；缺省取 ``params["algo"]``，再缺省 ``"v2"``。未注册的版本抛
    ``AnalysisError``（中文可读信息，符合 SEC-5）。

返回结构（JSON 可序列化）::

    {
      "ok": True, "algo": "v2",
      "schema":   {"matched": {...}, "missing_required": [], ...},   # 列映射结果
      "quality":  {...},        # 数据质量与生效阈值
      "stays":    [...],        # 停留点
      "trips":    [...],        # 出行段
      "clusters": [...],        # 地点簇（摘要，前端折叠面板用）
      "report":   {"entries": [...], "text_by_user": {...},
                   "user_summaries": {...}, "summary": {...}},
      "warnings": [...],        # 面向用户的中文提示（数据 / 配置）
      "filter":   {...}         # meta 原样回填（过滤摘要等）
    }

失败时抛 :class:`engine.contract.AnalysisError`，``str(exc)`` 可直接展示给用户。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import adapter, algorithms, params as params_mod, registry  # noqa: F401
from .contract import AnalysisError, Dataset  # noqa: F401

__version__ = "1.0.0"

__all__ = ["analyze_rows", "normalize_params", "available_algorithms",
           "resolve_columns", "AnalysisError", "Dataset", "registry"]


def analyze_rows(rows: Sequence[Sequence[Any]], params: Any = None,
                 algo: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行一次完整分析。参数与返回见模块文档。"""
    p = params_mod.normalize(params)
    version = str(algo or p.get("algo") or "v2").strip()
    if not registry.has(version):
        raise AnalysisError("未知的算法版本「%s」，当前可用版本：%s"
                            % (version, "、".join(registry.versions()) or "（无）"))

    dataset = adapter.build_dataset(rows, p)
    run = registry.get(version)
    result = run(dataset, p)

    # ---- 统一装配元信息：算法模块不需要关心这些 ----
    result["ok"] = True
    result["algo"] = version
    result["schema"] = dataset.schema
    result["filter"] = meta or {}
    warnings: List[str] = []
    for src in (dataset.warnings, result.get("warnings"), p.get("report", {}).get("_warnings")):
        for w in (src or []):
            if w and w not in warnings:
                warnings.append(w)
    for w in params_mod.validate(p):
        if w not in warnings:
            warnings.append(w)
    result["warnings"] = warnings
    return result


def normalize_params(config: Any = None) -> Dict[str, Any]:
    """把插件配置规范化为引擎参数（供路由层做配置校验 / 自检展示）。"""
    return params_mod.normalize(config)


def available_algorithms() -> List[str]:
    """已注册的算法版本列表。"""
    return registry.versions()


def resolve_columns(headers: Sequence[Any], params: Any = None):
    """列映射自检（与正式分析**共用同一实现**，保证"看到的"即"用到的"）。

    返回 ``(matched_header, missing_required, missing_optional)``。
    """
    p = params_mod.normalize(params)
    _idx, missing_req, missing_opt, matched = adapter.resolve_columns(headers, p["schema"])
    return matched, missing_req, missing_opt
