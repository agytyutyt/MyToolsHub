"""JZToolsHub 数据根目录管理 —— 集中处理「数据存在哪」的问题。

设计目标：系统数据（管理配置 / 插件运行数据 / 日志）默认保存到计算机
用户目录下的 .jztoolshub/，管理员可随时修改数据根目录；更换数据目录后
自动把旧目录中的全部数据迁移到新目录。这样项目升级（整体替换程序文件夹）
时，只要数据存放在用户目录（或管理员指定的目录），用户数据就不会丢失。

数据根目录布局（data_root 默认 ~/.jztoolshub）：
    data_root/
      config/          -> tools.json（工具注册/站点配置，随数据迁移）
                          admin.json（账号/单位/部门/人员/权限）
                          .admin_key（加密密钥，与密文同目录迁移）
      logs/            -> access.log（访问日志）
      plugins/<id>/    -> 各插件运行数据（data/、config.json、prompt.json、.task_cache/）

数据根目录指针：
  - 主指针：~/.jztoolshub.json（位于用户目录，整体替换程序文件夹后仍可找到）；
  - 备份指针：<程序目录>/config/data_root.json（便于排查，与程序同目录）。
  两个指针都会被读取（主指针优先）与写入，保证无论从哪个入口启动都能解析到
  当前数据根目录。指针文件格式：{"data_root": "绝对路径"}。

本模块被 app.py 与各插件后端共同 import，务必保持「仅标准库、无第三方依赖、
导入无副作用」，并避免循环依赖。
"""

import json
import logging
import os
import shutil
import sys

log = logging.getLogger("jztools.data")

# 默认数据根目录：<用户主目录>\.jztoolshub
DEFAULT_ROOT_NAME = ".jztoolshub"
# 主指针文件：用户目录下 .jztoolshub.json
POINTER_USER_FILE = os.path.join(os.path.expanduser("~"), ".jztoolshub.json")
# 备份指针文件：<程序目录>/config/data_root.json
BACKUP_POINTER_REL = os.path.join("config", "data_root.json")

# 需要随数据根目录迁移的「程序目录」相对子路径 -> 数据根目录相对子路径
# （启动时把旧版本留在程序目录里的用户数据搬进数据根目录，只搬不存在的项）
_LEGACY_MAP = [
    # (源：程序目录下相对路径, 目标：数据根目录下相对路径, 是否目录)
    (os.path.join("config", "admin.json"), os.path.join("config", "admin.json"), False),
    (os.path.join("config", ".admin_key"), os.path.join("config", ".admin_key"), False),
    (os.path.join("logs"), os.path.join("logs"), True),
    (os.path.join("plugins", "shared-docs", "backend", "data"), os.path.join("plugins", "shared-docs", "data"), True),
    (os.path.join("plugins", "notice-board", "backend", "data"), os.path.join("plugins", "notice-board", "data"), True),
    (os.path.join("plugins", "case-report", "backend", "data"), os.path.join("plugins", "case-report", "data"), True),
    (os.path.join("plugins", "case-report", "backend", "config.json"), os.path.join("plugins", "case-report", "config.json"), False),
    (os.path.join("plugins", "case-report", "backend", "prompt.json"), os.path.join("plugins", "case-report", "prompt.json"), False),
    (os.path.join("plugins", "character-graph", "backend", "config.json"), os.path.join("plugins", "character-graph", "config.json"), False),
    (os.path.join("plugins", "character-graph", "backend", "prompt.json"), os.path.join("plugins", "character-graph", "prompt.json"), False),
    (os.path.join("plugins", "trajectory-convert", "backend", "config.json"), os.path.join("plugins", "trajectory-convert", "config.json"), False),
    (os.path.join("plugins", "trajectory-convert", "backend", ".task_cache"), os.path.join("plugins", "trajectory-convert", ".task_cache"), True),
]

# 程序目录下随插件一起分发的 data 目录清单（打包时不携带，但源码开发时存在，
# 用于「首次启动把开发期旧数据也一并搬进数据根目录」，与 _LEGACY_MAP 一致）。
_DATA_SUBDIRS = ("config", "logs", "plugins")

# 随版本升级同步的「程序目录模板 -> 数据根目录运行配置」清单。
# 模式：
#   overwrite   ：直接覆盖运行配置（旧文件先备份为 .bak-<版本>）。用于 prompt.json 这类
#                 「随版本迭代演进」的模板（大模型提示词升级后应自动生效）。
#   merge-tools ：把 tools.json 模板中的新分类/新工具合并进数据根目录现有配置，
#                 保留用户已做的启停 / 排序 / 自定义（不覆盖用户改动）。
#   ensure-keys ：仅把模板中「用户配置缺失的键」补全（深合并），
#                 保留用户已有值（如 LLM api_key / base_url / model）。
_TEMPLATE_SYNC = [
    # (程序目录相对路径, 数据根目录相对路径, 模式)
    (("plugins", "case-report", "backend", "prompt.json"),
     ("plugins", "case-report", "prompt.json"), "overwrite"),
    (("plugins", "character-graph", "backend", "prompt.json"),
     ("plugins", "character-graph", "prompt.json"), "overwrite"),
    (("config", "tools.json"), ("config", "tools.json"), "merge-tools"),
    (("plugins", "case-report", "backend", "config.json"),
     ("plugins", "case-report", "config.json"), "ensure-keys"),
    (("plugins", "character-graph", "backend", "config.json"),
     ("plugins", "character-graph", "config.json"), "ensure-keys"),
]

# 应用版本状态文件：数据根目录 config/.app_state.json（记录上次启动的应用版本，
# 用于「仅在版本升级时同步一次模板」，避免每次启动都打扰用户已保存的配置）。
_APP_STATE_FILE = ("config", ".app_state.json")


def get_base_dir():
    """返回程序根目录：源码运行时为项目目录，PyInstaller 打包运行为 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def default_data_root():
    """默认数据根目录：<用户主目录>\\.jztoolshub。"""
    return os.path.join(os.path.expanduser("~"), DEFAULT_ROOT_NAME)


def _read_pointer_file(path):
    """读取单个指针文件，返回 data_root 绝对路径；不存在/非法返回 None。"""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            val = (json.load(f) or {}).get("data_root")
        if not val:
            return None
        return os.path.abspath(os.path.expanduser(val))
    except Exception as e:
        log.warning("读取数据根目录指针失败（%s）：%s", path, e)
        return None


def _write_pointer_file(path, root):
    """原子写入单个指针文件。"""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"data_root": root}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("写入数据根目录指针失败（%s）：%s", path, e)


def get_data_root():
    """返回当前数据根目录绝对路径（存在性懒处理，首次解析即写入指针）。

    解析优先级：主指针（用户目录）> 备份指针（程序目录 config/）> 默认用户目录。
    """
    root = _read_pointer_file(POINTER_USER_FILE)
    if root is None:
        root = _read_pointer_file(os.path.join(get_base_dir(), BACKUP_POINTER_REL))
    if root is None:
        root = default_data_root()
        # 首次运行：把默认根目录持久化为指针，后续启动即可稳定解析
        _write_pointer_file(POINTER_USER_FILE, root)
        _write_pointer_file(os.path.join(get_base_dir(), BACKUP_POINTER_REL), root)
    return root


def get_data_root_dir(*parts):
    """返回数据根目录下相对子路径（并确保目录存在）；parts 为空时仅确保根目录。"""
    root = get_data_root()
    path = os.path.join(root, *parts) if parts else root
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        log.warning("创建数据目录失败（%s）：%s", path, e)
    return path


def get_data_root_file(*parts):
    """返回数据根目录下相对文件路径（不创建文件，仅确保父目录存在）。"""
    path = os.path.join(get_data_root(), *parts)
    parent = os.path.dirname(path)
    try:
        os.makedirs(parent, exist_ok=True)
    except Exception as e:
        log.warning("创建数据父目录失败（%s）：%s", parent, e)
    return path


def _safe_move(src, dst):
    """把 src 移动到 dst（父目录自动创建）；src 不存在则忽略，失败仅告警不抛异常。"""
    if src == dst:
        return False
    if not os.path.exists(src):
        return False
    if os.path.exists(dst):
        return False  # 目标已存在则不覆盖（幂等）
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            shutil.move(src, dst)
        else:
            shutil.move(src, dst)
        return True
    except Exception as e:
        log.warning("迁移数据失败：%s -> %s（%s）", src, dst, e)
        return False


def migrate_legacy_app_data():
    """启动时把旧版本留在程序目录里的用户数据搬进数据根目录（幂等）。

    适用于旧版升级：旧版把 data/、admin.json、logs/ 等放在程序目录，
    新版启动后把这些数据迁到数据根目录，从而支持后续「整体替换程序文件夹」。
    仅当数据根目录中目标不存在时才迁移，重复启动不会重复移动。
    """
    base = get_base_dir()
    root = get_data_root()
    if os.path.normpath(base) == os.path.normpath(root):
        return  # 数据根目录恰为程序目录（异常配置）则跳过，避免原地迁移
    moved = 0
    for src_rel, dst_rel, _isdir in _LEGACY_MAP:
        src = os.path.join(base, src_rel)
        dst = os.path.join(root, dst_rel)
        if _safe_move(src, dst):
            moved += 1
    # 确保数据根目录结构就绪
    get_data_root_dir("config")
    get_data_root_dir("logs")
    get_data_root_dir("plugins")
    # 数据根目录缺 tools.json 时，从程序目录的模板复制一份（模板保留在程序目录）
    _ensure_tools_json(base, root)
    if moved:
        log.info("已从程序目录迁移 %d 项用户数据到数据根目录：%s", moved, root)
    return moved


def _ensure_tools_json(base, root):
    """数据根目录 config/tools.json 缺失时，从程序目录模板复制。"""
    src = os.path.join(base, "config", "tools.json")
    dst = os.path.join(root, "config", "tools.json")
    if os.path.isfile(dst):
        return
    if not os.path.isfile(src):
        log.warning("未找到 tools.json 模板：%s", src)
        return
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        log.info("已初始化数据根目录 tools.json：%s", dst)
    except Exception as e:
        log.warning("初始化 tools.json 失败：%s", e)


def migrate_data_root(old_root, new_root):
    """把数据从旧根目录整体迁移到新根目录（更换数据目录时调用）。

    对 config / logs / plugins 三个子目录逐项迁移：目标不存在则移动，
    目标已存在则跳过（不覆盖新目录已有数据）。返回迁移的条目数。
    """
    old_root = os.path.abspath(os.path.expanduser(old_root))
    new_root = os.path.abspath(os.path.expanduser(new_root))
    if os.path.normpath(old_root) == os.path.normpath(new_root):
        return 0
    if not os.path.isdir(old_root):
        return 0
    moved = 0
    for sub in _DATA_SUBDIRS:
        src = os.path.join(old_root, sub)
        dst = os.path.join(new_root, sub)
        if _safe_move(src, dst):
            moved += 1
    get_data_root_dir("config")
    get_data_root_dir("logs")
    get_data_root_dir("plugins")
    _ensure_tools_json(get_base_dir(), new_root)
    return moved


def set_data_root(new_root, migrate=True):
    """管理员设置新的数据根目录；默认把旧目录数据迁移过去。

    返回 (新根目录绝对路径, 迁移条目数, 错误或 None)。
    """
    new_root = os.path.abspath(os.path.expanduser(new_root or ""))
    if not new_root:
        return None, 0, "数据目录不能为空"
    if not os.path.isabs(new_root):
        return None, 0, "数据目录不合法"
    old_root = get_data_root()
    moved = 0
    if migrate and os.path.normpath(old_root) != os.path.normpath(new_root):
        moved = migrate_data_root(old_root, new_root)
    # 持久化新指针（主指针 + 备份指针）
    _write_pointer_file(POINTER_USER_FILE, new_root)
    _write_pointer_file(os.path.join(get_base_dir(), BACKUP_POINTER_REL), new_root)
    # 确保新目录结构就绪
    get_data_root_dir("config")
    get_data_root_dir("logs")
    get_data_root_dir("plugins")
    _ensure_tools_json(get_base_dir(), new_root)
    return new_root, moved, None


# ===================== 配置模板同步（版本升级时自动生效） =====================


def _read_json_file(path, default=None):
    """安全读取 JSON 文件，失败返回 default。"""
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_file(path, obj):
    """写入 JSON 文件（原子写）。"""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("写入 JSON 文件失败（%s）：%s", path, e)


def _file_hash16(path):
    """返回文件 MD5 前 16 位十六进制字符串（用于比较模板版本）。"""
    import hashlib
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except Exception:
        return ""


def _read_app_state():
    """读取数据根目录 config/.app_state.json。"""
    path = get_data_root_file(*_APP_STATE_FILE)
    return _read_json_file(path) or {}


def _write_app_state(state):
    """写入数据根目录 config/.app_state.json。"""
    path = get_data_root_file(*_APP_STATE_FILE)
    _write_json_file(path, state)


def _read_version(base=None):
    """读取程序目录 version.json 中的 app 版本号，不存在返回 None。"""
    base = base or get_base_dir()
    vf = os.path.join(base, "version.json")
    obj = _read_json_file(vf)
    return (obj or {}).get("app") if obj else None


def _merge_tools(user, template):
    """把模板 tools.json 合并进用户现有配置（保留用户自定义）。

    - site    ：保留用户已有字段，仅补充模板新增字段
    - categories：按 id 合并，已有分类保留用户版本，新分类追加
    - tools   ：按 id 合并，已有工具保留用户版本（含 enabled/order/hidden），新工具追加
    """
    out = {}
    # site
    us = user.get("site") or {}
    ts = template.get("site") or {}
    ms = dict(ts)
    for k, v in us.items():
        ms[k] = v
    out["site"] = ms
    # categories
    uc = list(user.get("categories") or [])
    uc_ids = {c.get("id") for c in uc if isinstance(c, dict)}
    for c in template.get("categories") or []:
        if isinstance(c, dict) and c.get("id") not in uc_ids:
            uc.append(c)
            uc_ids.add(c.get("id"))
    out["categories"] = uc
    # tools
    ut = list(user.get("tools") or [])
    ut_map = {t.get("id"): t for t in ut if isinstance(t, dict)}
    for t in template.get("tools") or []:
        if isinstance(t, dict) and t.get("id") not in ut_map:
            ut.append(t)
    out["tools"] = ut
    return out


def _ensure_deep_keys(user, template):
    """递归确保 template 中的键在 user 中存在（仅补缺失，不覆盖已有）。"""
    if isinstance(template, dict) and isinstance(user, dict):
        for k, v in template.items():
            if k not in user:
                user[k] = v
            elif isinstance(v, dict) and isinstance(user[k], dict):
                _ensure_deep_keys(user[k], v)
    return user


def _sync_templates(base, root, version):
    """核心：按 _TEMPLATE_SYNC 清单同步配置模板。返回同步的文件数。"""
    synced = 0
    for src_rel, dst_rel, mode in _TEMPLATE_SYNC:
        src = os.path.join(base, *src_rel)
        dst = os.path.join(root, *dst_rel)
        if not os.path.isfile(src):
            log.warning("模板同步：程序目录缺少 %s，跳过", os.path.join(*src_rel))
            continue
        if not os.path.isfile(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copyfile(src, dst)
                log.info("模板同步：已初始化 %s", os.path.join(*dst_rel))
                synced += 1
            except OSError as e:
                log.warning("模板同步：初始化 %s 失败：%s", os.path.join(*dst_rel), e)
            continue
        # 目标已存在，按模式处理
        try:
            if mode == "overwrite":
                bak = dst + ".bak-old"
                if not os.path.isfile(bak):
                    shutil.copyfile(dst, bak)
                shutil.copyfile(src, dst)
                log.info("模板同步：已覆盖 %s（旧版备份 .bak-old）", os.path.join(*dst_rel))
                synced += 1
            elif mode == "merge-tools":
                tu = _read_json_file(dst) or {}
                tt = _read_json_file(src) or {}
                merged = _merge_tools(tu, tt)
                _write_json_file(dst, merged)
                log.info("模板同步：已合并 %s（保留用户启停/排序）", os.path.join(*dst_rel))
                synced += 1
            elif mode == "ensure-keys":
                uu = _read_json_file(dst) or {}
                tt = _read_json_file(src) or {}
                _ensure_deep_keys(uu, tt)
                _write_json_file(dst, uu)
                log.info("模板同步：已补全 %s（保留用户配置）", os.path.join(*dst_rel))
                synced += 1
        except Exception as e:
            log.warning("模板同步：%s 处理失败（%s）", os.path.join(*dst_rel), e)
    return synced


def sync_templates():
    """版本升级时同步一次：程序目录模板 -> 数据根目录运行配置。

    在 app 启动时（init_data_root 后）调用。当
    ``version.json`` 中的 app 版本号与数据根目录
    ``config/.app_state.json`` 中记录的 last_app 不一致时，
    执行 _TEMPLATE_SYNC 清单中所有条目，并更新 last_app。
    版本一致时跳过（幂等）。
    """
    base = get_base_dir()
    root = get_data_root()
    version = _read_version(base)
    if not version:
        return 0
    state = _read_app_state()
    if version == state.get("last_app"):
        return 0
    synced = _sync_templates(base, root, version)
    state["last_app"] = version
    _write_app_state(state)
    if synced:
        log.info("模板同步完成：%d 个文件已同步（版本 %s）", synced, version)
    return synced


def data_usage_summary():
    """返回数据根目录当前占用摘要（供管理界面展示）：各子目录条目数与总大小。"""
    root = get_data_root()
    summary = {"root": root, "subdirs": [], "total_bytes": 0}
    total = 0
    for sub in _DATA_SUBDIRS:
        d = os.path.join(root, sub)
        size = 0
        count = 0
        if os.path.isdir(d):
            for dirpath, _dirnames, filenames in os.walk(d):
                for fn in filenames:
                    try:
                        size += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
                    count += 1
        total += size
        summary["subdirs"].append({"name": sub, "count": count, "bytes": size})
    summary["total_bytes"] = total
    return summary
