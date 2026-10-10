# -*- coding: utf-8 -*-
"""仓库根测试共享夹（P2-3 / `docs/eval/20260929代码质量分析报告.md` §4.3 收敛）。

四类样板收敛于此，测试文件用 ``from conftest import ...`` 引用：

1. ``load_plugin`` / ``load_plugin_module`` / ``load_plugin_by_id``——复刻框架把
   ``plugins/<id>/backend`` 动态加载为包 ``jztools_<id>`` 的口径（与 app.py 的
   ``register_plugin_backends`` 同构）；
2. ``temp_data_root``——临时数据根 + ``JZTOOLS_DATA_ROOT`` 环境变量，返回
   ``(root, restore)``，``restore()`` 恢复环境变量原值并删除临时目录；
3. ``USER`` / ``OTHER``——固定会话身份（管理员+超管 / 普通用户）；
4. ``PLUGIN_DIRS``——插件 id → 仓库内插件目录的映射（按 ``plugins/`` 目录实况自动发现）。

本文件同时是 pytest 的 rootdir conftest；**导入无副作用**（不建目录、不改环境变量、
不加载插件），``pytest --collect-only`` 与 ``unittest`` 装载均为纯操作。

运行口径不变（在仓库根执行）：
    python -m unittest test_filter_preview -v
    python -m pytest test_filter_preview.py -q
"""
import importlib
import importlib.util
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))


def _discover_plugin_dirs():
    base = os.path.join(ROOT, "plugins")
    return {name: os.path.join(base, name)
            for name in sorted(os.listdir(base))
            if os.path.isfile(os.path.join(base, name, "manifest.json"))}


PLUGIN_DIRS = _discover_plugin_dirs()

USER = {"username": "tester", "role_id": "role-admin", "super_admin": True}
OTHER = {"username": "someone-else", "role_id": "role-user", "super_admin": False}


def load_plugin_module(pkg_name, plugin_dir, submodule, clean=True):
    """按框架口径把 ``plugin_dir/backend`` 加载为包 ``pkg_name``，返回其 ``submodule``。

    ``clean=True``（默认）先清掉同名旧模块与子模块：多个测试文件都会以同一包名
    （``jztools_<id>``）加载各自数据根下的插件实例，其模块级常量（如 CONFIG_FILE）
    指向各自的临时数据根；不清会拿到旧实例、读到已删除目录下的路径（曾实际踩到）。
    """
    if clean:
        for name in [n for n in list(sys.modules)
                     if n == pkg_name or n.startswith(pkg_name + ".")]:
            sys.modules.pop(name, None)
    backend = os.path.join(plugin_dir, "backend")
    spec = importlib.util.spec_from_file_location(
        pkg_name, os.path.join(backend, "__init__.py"), submodule_search_locations=[backend])
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(pkg_name + "." + submodule)


def load_plugin(pkg_name, plugin_dir, clean=True):
    """``load_plugin_module(..., "routes")`` 的快捷方式。"""
    return load_plugin_module(pkg_name, plugin_dir, "routes", clean=clean)


def load_plugin_by_id(pid, submodule="routes", clean=True):
    """按插件 id 加载（包名自动转 ``jztools_<id 下划线>``，目录取 ``PLUGIN_DIRS``）。"""
    return load_plugin_module("jztools_" + pid.replace("-", "_"),
                              PLUGIN_DIRS[pid], submodule, clean=clean)


def temp_data_root(prefix, subdir=None):
    """建临时数据根并令 ``JZTOOLS_DATA_ROOT`` 指向它；返回 ``(root, restore)``。

    ``root``＝新建的临时目录（``subdir`` 非空时为临时目录下该子目录，并预建）。
    ``restore()`` 恢复环境变量原值（原先未设则删除）并递归删除整个临时目录。

    典型用法：``setUpClass`` 里 ``cls.data_root, cls._restore = temp_data_root("jz-x-", subdir="data")``，
    ``tearDownClass`` 里 ``cls._restore()``。
    """
    tmp = tempfile.mkdtemp(prefix=prefix)
    root = tmp
    if subdir:
        root = os.path.join(tmp, subdir)
        os.makedirs(root, exist_ok=True)
    prev = os.environ.get("JZTOOLS_DATA_ROOT")
    os.environ["JZTOOLS_DATA_ROOT"] = root

    def restore():
        if prev is None:
            os.environ.pop("JZTOOLS_DATA_ROOT", None)
        else:
            os.environ["JZTOOLS_DATA_ROOT"] = prev
        shutil.rmtree(tmp, ignore_errors=True)

    return root, restore
