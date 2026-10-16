"""宿主启动件：依赖组件注入 + 数据根初始化。

从 app.py 拆出（2026-10-10 P2-4 / 报告 §4.4「依赖注入」块）。本模块不在 import 期
推导程序目录——base_dir 一律由 app.py 传入（见 jz_host/__init__.py 的装配约定）。
"""
import logging
import os

import jz_deps
import jz_plugin_host

# 注：jztools_data 不在模块顶部导入——测试沙箱会按相位整副本替换该模块（sys.path 优先），
# 而本模块长驻 sys.modules 不随相位重载；顶部导入会把绑定冻结成第一个相位的副本。
# 所以凡用到它的函数一律在**调用期**导入（与 jz_deps.apply_pending_components 同一手法）。

log = logging.getLogger("jztools.app")


def setup_dep_components(base_dir):
    """依赖组件包注入（GD8 档2：实现已抽取至 jz_plugin_host，行为保持）。"""
    return jz_plugin_host.setup_dep_components(base_dir, log)


def dep_components_status(base_dir):
    """依赖组件摘要（GD8 档2：实现已抽取至 jz_plugin_host，行为保持）。"""
    return jz_plugin_host.dep_components_status(base_dir)


def boot_components(base_dir):
    """启动时依赖组件三连（原 app.py 模块级副作用块，行为保持）：

    ① 先应用"暂存中的依赖组件"（服务运行时装组件时文件被占用 → 暂存到 pylibs\\.pending\\，
       此刻进程还没加载任何组件 DLL，覆盖必然成功）。必须在 setup 之前。
    ② 注入 pylibs 路径（setup_dep_components）。
    ③ 把"重新注入依赖组件路径"注册给判定引擎：jz_deps.refresh_flags 在重算插件标记前
       会先调用它，于是"服务运行中装依赖组件 → 刷新页面即可用"（此前必须重启服务才生效）。
    """
    try:
        _applied_pending = jz_deps.apply_pending_components(base_dir)
        if _applied_pending:
            log.info("已应用暂存的依赖组件：%s", "、".join(_applied_pending))
    except Exception:
        log.exception("应用暂存依赖组件失败（不影响启动，下次再试）")

    setup_dep_components(base_dir)
    jz_deps.register_component_rescanner(lambda: setup_dep_components(base_dir))


def resolve_paths():
    """启动时解析数据根目录并计算关键路径（原 app.py init_data_root 主体）。

    ① 先迁移旧版程序目录中的用户数据；
    ② 解析数据根目录（jztools_data 双指针）；
    ③ 版本升级时把程序目录中的配置模板（prompt.json / tools.json 等）同步进数据根目录，
       保证大模型提示词等「随版本迭代」的配置在更新后立即生效（幂等：仅版本变化时执行一次）。

    返回 {"data_root", "config_path", "log_dir", "log_file"}；调用方（app.py）负责把
    它们落到自己的模块级路径变量并转发给 plugin_loader / access_log。
    """
    import jztools_data          # 调用期导入，见模块头注
    jztools_data.migrate_legacy_app_data()
    data_root = jztools_data.get_data_root()
    paths = {
        "data_root": data_root,
        "config_path": os.path.join(data_root, "config", "tools.json"),
        "log_dir": os.path.join(data_root, "logs"),
        "log_file": os.path.join(data_root, "logs", "access.log"),
    }
    jztools_data.sync_templates()
    return paths
