"""宿主启动件：依赖组件注入 + 数据根初始化 + 插件历史大模型配置清理。

从 app.py 拆出（2026-10-10 P2-4 / 报告 §4.4「依赖注入」块）。本模块不在 import 期
推导程序目录——base_dir 一律由 app.py 传入（见 jz_host/__init__.py 的装配约定）。
"""
import json
import logging
import os
import re
from datetime import datetime

import jz_deps
import jz_llm
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


def _write_llm_migration_flag(flag_path):
    """写「插件历史大模型配置已清理」退场标记（C1 步骤②）。

    标记存在即代表该数据根已完整通过一次清理（扫描完成且无失败），后续启动跳过扫描。
    写失败不致命：这里吞掉异常、不改变调用方行为，下次启动重扫（清理本身幂等）即可。
    内容只有时间戳，供 flag 覆盖率统计/排障（删除窗口判定见 migrate_plugin_legacy_llm）。
    """
    try:
        os.makedirs(os.path.dirname(flag_path), exist_ok=True)
        with open(flag_path, "w", encoding="utf-8") as f:
            f.write("plugin legacy llm migration completed at %s\n"
                    % datetime.now().isoformat(timespec="seconds"))
    except OSError:
        pass


def migrate_plugin_legacy_llm():
    """把插件历史 config.json 里的大模型配置**收编进统一配置，并清除插件侧副本**。

    归属：本函数原在 admin 插件 register() 中常驻执行（每次启动改写**其他插件**的数据
    文件，属未声明、未退场的插件间数据耦合——2026-09-29 代码质量分析报告 C1）。
    2026-10-09 决策 7（方案 A）上移为主体启动流程的一次性清理（主体管理插件数据根是
    sanctioned 方向），并加退场标记：完整通过一次后写 ``<数据根>/config/llm_migrated.flag``，
    之后启动先查标记、存在即跳过扫描（C1 步骤①/②）。与插件注册无关——
    调用点在 __main__ 的 init_data_root() 之后、register_plugin_backends() 之前。

    背景：统一大模型落地前，case-report / character-graph / file-filter 各自在
    ``<数据根>/plugins/<id>/config.json`` 存了一份 ``llm{base_url,api_key,model}``，
    Key 是**明文**。统一后这份副本已无读取方（兼容桥本轮一并移除），留着既是明文泄漏面，
    也会让"Key 到底存在哪"说不清。

    做法（**先收编、再清除**，避免"清掉副本 = 大模型功能直接失效"）：
      ① 统一配置还没有可用接入信息时，把插件那份搬进 ``<数据根>/config/llm.json``
         （经 jz_llm.save_global 加密落盘；历史语义就是 OpenAI 兼容）；
      ② 无论是否收编，都把插件 config.json 里的 ``llm`` 段删除（原子写）；
      ③ 完整通过（扫描完成且 failed 为空，含没有历史 llm 段的空跑）后写退场标记。
    幂等：有退场标记即跳过；没有 llm 段的文件不动，重复执行无副作用。
    返回可读的处理摘要（供调用方写日志）；无处理动作或已有标记时返回 None。

    ★ 删除窗口（C1 步骤③，后续任务）：这是**一次性迁移**，不该永驻。判定依据：
    凡可能产出历史 llm 段的存量数据根，都已启动过带本函数的版本——
    即所有目标数据根都存在 ``<数据根>/config/llm_migrated.flag``（flag 覆盖率 100%）；
    且新安装、新插件版本均不再向插件 config.json 写 llm 段。满足后本函数、
    _write_llm_migration_flag 与 __main__ 中的调用点可整段删除（标记文件可留可删，无害）。
    """
    import jztools_data          # 调用期导入，见模块头注
    root = jztools_data.get_data_root()
    flag_path = os.path.join(root, "config", "llm_migrated.flag")
    if os.path.isfile(flag_path):
        return None                      # 退场标记：清理已完成，跳过扫描
    plugins_dir = os.path.join(root, "plugins")
    if not os.path.isdir(plugins_dir):
        return None

    candidates = []          # [(pid, llm_dict)]
    targets = []             # [(path, data)]  含 llm 段、待清除
    for pid in sorted(os.listdir(plugins_dir)):
        if not re.match(r"^[A-Za-z0-9_-]+$", pid):
            continue
        path = os.path.join(plugins_dir, pid, "config.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            log.debug("旧版 LLM 迁移：跳过不可读的插件配置 %s", path, exc_info=True)
            continue
        llm = (data or {}).get("llm") if isinstance(data, dict) else None
        if not isinstance(llm, dict):
            continue
        targets.append((path, data))
        if (llm.get("api_key") or "").strip():
            candidates.append((pid, llm))

    if not targets:
        _write_llm_migration_flag(flag_path)   # 存量数据根无历史 llm 段：同样视为已完整过一遍
        return None

    adopted = ""
    cfg = jz_llm.load_global()
    if candidates and not jz_llm.provider_usable(cfg["provider"]):
        pid, llm = candidates[0]
        url = str(llm.get("url") or llm.get("base_url") or "").strip()
        if url and not url.endswith("/chat/completions"):
            url = url.rstrip("/") + "/chat/completions"     # 历史允许省写，补全为完整接口地址
        provider = jz_llm.normalize_provider({
            "format": "openai", "url": url,
            "api_key": llm.get("api_key"), "model": llm.get("model"),
        })
        if jz_llm.provider_usable(provider):
            jz_llm.save_global({"mode": cfg["mode"], "fallback": cfg["fallback"],
                                "provider": provider})
            adopted = pid

    cleared = []
    failed = []
    for path, data in targets:
        data.pop("llm", None)
        try:
            jztools_data.atomic_write_json(path, data)
            cleared.append(os.path.basename(os.path.dirname(path)))
        except OSError as e:
            failed.append("%s（%s）" % (path, e.strerror or e))

    if not failed:
        _write_llm_migration_flag(flag_path)   # 完整通过 → 退场标记（写失败下次重扫，幂等）

    return {"adopted_from": adopted, "cleared": cleared, "failed": failed,
            "dropped_duplicates": [p for p, _ in candidates if p != adopted]}
