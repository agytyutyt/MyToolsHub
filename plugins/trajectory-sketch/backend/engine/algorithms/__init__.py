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

__all__ = ["v2"]
