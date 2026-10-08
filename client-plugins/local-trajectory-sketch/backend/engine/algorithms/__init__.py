# 同源副本登记（C 类客户端工件，2026-10-09）：本文件与 plugins/trajectory-sketch/backend/engine/algorithms/__init__.py 同源、
# 独立演进（设计文档 §3.15/§3.16；规范 §16.15）。改动请只在本副本内进行。
# -*- coding: utf-8 -*-
"""内置算法集合。

**导入即注册**：每个子包在被 import 时会调用 ``registry.register(版本号, run)``。
新增算法只需两步（见设计文档 §5.3）：

  ① 新建 ``algorithms/v3/``（内部按 cluster / staypoint / trip / report 拆分）；
  ② 在本文件加一行 ``from . import v3  # noqa: F401``。

路由与前端无需任何改动。
"""
from __future__ import annotations

from . import v2  # noqa: F401  导入即注册 "v2"
from . import v3  # noqa: F401  导入即注册 "v3"

__all__ = ["v2", "v3"]
