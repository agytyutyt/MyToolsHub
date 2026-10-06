# -*- coding: utf-8 -*-
"""插件宿主内核（GD8 档2 抽取，2026-10-04）：主包与桌面客户端共用的插件机制。

从 app.py 行为保持抽取（批次 4 T19/T20 前置）：
- setup_dep_components / dep_components_status ← app.py 原 _setup_dep_components / dep_components_status
  （BASE_DIR 参数化；DLL 句柄与已注入集合为本模块级状态，语义与原实现一致）；
- load_backend_module ← app.py 原 _load_backend_module（插件目录参数化）。

主包侧以薄包装保持原函数名与调用点不变（app.py），行为零变化由
「主包全量自检 + smoke 双绿」背书（桌面端 TODO 红线 4）。
"""
import importlib
import json
import logging
import os
import re
import sys

log = logging.getLogger("jztools.plugin_host")

# 依赖组件包（runtime/pylibs）的 DLL 目录句柄：必须持有引用，否则句柄被 GC 后目录失效
_DEP_DLL_HANDLES = []
# 已注入过 DLL 搜索路径的目录（幂等：可重扫，供"服务运行中装组件"后即时生效）
_DEP_DLL_DIRS = set()


def setup_dep_components(base_dir, logger=None):
    """把「依赖组件包」注入 sys.path 与 DLL 搜索路径（设计文档 §3.4 / T25 第 1 步）。

    cv2(opencv-python) 与 numpy 不再随主包（压缩后约 60 MB），改由独立组件包按需安装到
    ``<base_dir>/runtime/pylibs/``。本函数在**模块加载时**执行（早于插件后端导入），
    并在**每次依赖刷新时重入**（`jz_deps.register_component_rescanner`）：
      · 把该目录插入 sys.path 首位 → 插件的 ``import cv2`` 能找到；
      · 每个子目录加入 Windows DLL 搜索路径 → cv2 的 .pyd 能找到同目录 DLL、
        numpy 能找到 ``numpy.libs`` 里的 OpenBLAS（二者必须同装同卸）。
    未安装组件时静默返回（插件侧自行降级并给出"安装依赖组件包"的提示）。

    幂等：sys.path 无条件注入（目录可能"服务运行中才装出来"，不存在的路径条目无害），
    DLL 目录按已注入集合去重——**装完组件刷新页面即生效，不必重启服务**。
    """
    logger = logger or log
    base = os.path.join(base_dir, "runtime", "pylibs")
    if base not in sys.path:
        sys.path.insert(0, base)
    if not os.path.isdir(base):
        return []
    newly = []
    # pylibs 根目录本身也要进 DLL 搜索路径：依赖组件自带的 VC++ 运行时（msvcp140*.dll 等）
    # 就放在根下，不进这一条的话 C 扩展在干净机器上会 "DLL load failed"。
    if base not in _DEP_DLL_DIRS and hasattr(os, "add_dll_directory"):
        try:
            _DEP_DLL_HANDLES.append(os.add_dll_directory(base))
            _DEP_DLL_DIRS.add(base)
            newly.append("(根)")
        except OSError:
            pass
    found = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    for name in names:
        d = os.path.join(base, name)
        if not os.path.isdir(d):
            continue
        found.append(name)
        if d in _DEP_DLL_DIRS or not hasattr(os, "add_dll_directory"):
            continue
        try:
            _DEP_DLL_HANDLES.append(os.add_dll_directory(d))
            _DEP_DLL_DIRS.add(d)
            newly.append(name)
        except OSError:
            pass
    if newly:
        # ★ 目录可能是"服务运行中才出现"的：必须清掉导入器缓存，否则新装的包在同一进程里
        #   import 不到——FileFinder 对"当时不存在"的路径会缓存空目录且不重扫（实测踩到）。
        try:
            sys.path_importer_cache.pop(base, None)
            importlib.invalidate_caches()
        except Exception:                      # 缓存清理失败不影响主流程
            pass
        logger.info("依赖组件已注入：%s（%s）", "、".join(found), base)
    return found


def dep_components_status(base_dir):
    """依赖组件的安装与可用性摘要（供 --check-deps 自检与后台展示）。

    探针**取自已装组件自己声明的 modules**（`runtime/pylibs/manifest.json`），不是写死
    numpy/cv2——写死会让"装 openpyxl/zxingcpp 等非 cv2/numpy 组件"自检必然失败（假失败），
    而新组件也永远不被验证。无清单（旧版组件包）时回退到历史探针。
    """
    base = os.path.join(base_dir, "runtime", "pylibs")
    installed = sorted(n for n in (os.listdir(base) if os.path.isdir(base) else [])
                       if os.path.isdir(os.path.join(base, n)) and not n.startswith("."))
    mods, requires, comps = [], [], []
    try:
        with open(os.path.join(base, "manifest.json"), "r", encoding="utf-8-sig") as f:
            man = json.load(f)
        for c in (man.get("components") or []):
            comps.append(c.get("id") or c.get("name") or "")
            for m in (c.get("modules") or {}):
                if m not in mods:
                    mods.append(m)
            for r in (c.get("requires_components") or []):
                if r not in requires:
                    requires.append(r)
    except Exception:
        pass
    if not mods:
        mods = ["numpy", "cv2"]          # 回退：旧版组件包没有 modules 字段
    out = {"path": base, "installed": installed, "components": [c for c in comps if c],
           "requires": requires, "imports": {}}
    for mod in mods:
        try:
            __import__(mod)
            out["imports"][mod] = "ok"
        except Exception as exc:
            out["imports"][mod] = "%s: %s" % (type(exc).__name__, exc)
    # 组件间依赖：声明了 requires_components 但没装的，单独点明——这类失败不是"组件损坏"，
    # 而是"还差另一个组件"（如 cv2 需要 numpy），提示必须能直接照做。
    out["missing_requires"] = [r for r in requires if r not in installed]
    return out


def load_backend_module(plugins_dir, plugin_id):
    """载入插件后端模块 <plugins_dir>/<id>/backend/routes.py（原 app.py._load_backend_module）。

    每个插件为独立包，模块名取插件 ID 的合法形式，避免同名冲突。
    约定插件后端提供 register(app) 函数挂载路由。
    """
    plugin_dir = os.path.join(plugins_dir, plugin_id)
    if not plugin_id or not re.fullmatch(r"[A-Za-z0-9_-]+", plugin_id) or not os.path.isdir(plugin_dir):
        return None
    backend_dir = os.path.join(plugin_dir, "backend")
    package_file = os.path.join(backend_dir, "__init__.py")
    if not os.path.isfile(package_file):
        return None
    module_name = "jztools_" + re.sub(r"\W", "_", plugin_id)
    # 热升级关键：清掉同名父/子模块缓存，否则 import_module 命中旧 .routes——
    # 升级后跑的仍是旧代码（批次 9 T-O9-8 台架发现的真缺陷）。
    for stale in [n for n in list(sys.modules)
                  if n == module_name or n.startswith(module_name + ".")]:
        sys.modules.pop(stale, None)
    spec = importlib.util.spec_from_file_location(
        module_name, package_file, submodule_search_locations=[backend_dir]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = package
    spec.loader.exec_module(package)
    return importlib.import_module(f"{module_name}.routes")
