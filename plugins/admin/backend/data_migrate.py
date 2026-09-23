"""管理后台 · 数据迁移（导出 / 导入）——把已安装插件的数据**按插件分类**打包，口令加密。

为什么需要
    换机器、重装、交接时要把"数据"搬走：账号与权限、统一大模型配置、工具启停与排序、
    各插件的业务数据（战果台账、公告、知识库文件、共享文档……）。手动拷数据根目录
    既容易漏（漏了 `config/` 就丢了账号）也容易带出不该带的东西（日志、任务缓存、机器密钥）。

包结构（一个 `.jzdata` 文件 = 口令加密容器；解开后是下面的 zip）
    manifest.json            迁移包清单：格式版本 / 来源机器 / 导出时间 / 各段文件数与体积
    config/admin.json        账号与权限数据（单位/部门/人员/角色）
    config/llm.json          统一大模型全局配置
    config/tools.json        工具启停 / 排序 / 可见性
    plugins/<插件id>/...     各插件的运行数据（按插件分类，一个插件一段）

安全口径（本模块的核心约束，改动前先读完）
    1. **整包加密**：没有口令连包内的文件名都读不到。口令经 PBKDF2-HMAC-SHA256
       （随机盐 + 迭代次数写在包头，便于以后提高强度）派生出两把子密钥：
       一把加密整包（zip 字节），一把用于下面的**字段信封**。加密用 Fernet
       （AES-CBC + HMAC）做认证加密，篡改会在解密时被拒绝，不会解出半截数据。
    2. **字段信封**：账号数据里本来就有的密文（密码哈希、身份证、用户/全局 LLM Key）是用
       **本机密钥** `config/.admin_key` 加密的。若原样打包会带来两个问题：
       ① 换台机器导入后**解不开**（新机器密钥不同，用户密码与 LLM Key 全成乱码）；
       ② 口令一旦泄露，拿到包的人还需要本机密钥才能解出 Key——少一层保护。
       故导出时把这些字段**改封**为"口令信封"（`JZMIG1:` 前缀），导入时再改封为
       **目标机**密钥。`config/.admin_key` **永不进包**。
    3. 识别"哪些字段是密文"不靠字段清单，而是**能否用本机密钥解开**：这样将来任何新增的
       密钥字段（含插件自己用 `jz_llm.encrypt_secret` 加密的配置）都自动被覆盖，
       不需要维护一份容易漏的清单。
    4. **不进包**：`config/.admin_key`（机器密钥）、`config/.app_state.json`（本机状态：
       模板指纹与插件版本登记，带过去会压制目标机的模板同步）、`config/installed-deps.json`
       （构建期生成、可重建）、`logs/`、`.staging/`、`backups/`、各插件的 `.task_cache/`、
       `out/`、`__pycache__/` 与 `*.tmp` / `*.bak*` / `*.pyc` 等易变文件。

导入的落点与安全
    - 条目路径**白名单校验**（只允许 `config/<框架三者>` 与 `plugins/<已声明插件id>/...`，
      拒绝绝对路径、盘符、`..`、含冒号——与插件包同一套 zip 安全口径）；
    - 覆盖前先把被覆盖的文件备份到 `<数据根>/backups/migrate/<时间戳>/`，导错可手工回退；
      内容一致的文件直接跳过（幂等，不写不备份）；
    - 分段导入（账号 / 大模型 / 工具注册 / 各插件），只写勾选的段；
    - 本模块只做纯逻辑（路径与密钥实例由调用方显式传入），便于单元测试；HTTP 层在 routes.py。
"""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import socket
import zipfile
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

SCHEMA = 1
FORMAT = "jztools-data-migrate"
EXT = ".jzdata"

MAGIC = b"JZDATA1\x00"          # 容器头：便于识别"这不是普通 zip"并快速给出可读报错
SALT_BYTES = 16
KDF_ITERATIONS = 200_000        # PBKDF2 迭代次数（写在包头，便于以后提高）
ENVELOPE_PREFIX = "JZMIG1:"     # 字段信封前缀：包内出现它 = 该值由口令密钥封装

# 体积/条目上限（防误操作与 zip 炸弹；与插件包 PU-3 同口径）
MAX_PACKAGE_BYTES = 300 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 50000

# 打包时排除的目录名 / 文件名 / 后缀（易变、可重建、或属本机状态）
EXCLUDE_DIRS = {"__pycache__", ".task_cache", "out", ".git", ".venv", "venv", "node_modules"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".tmp", ".bak", ".bak-old", ".orig", ".log")

# 框架级分段：(段 id, 展示名, 数据根相对路径)
FRAMEWORK_SECTIONS = (
    ("accounts", "账号与权限数据", ("config", "admin.json")),
    ("llm", "统一大模型配置", ("config", "llm.json")),
    ("tools", "工具启停与排序", ("config", "tools.json")),
)
_FRAMEWORK_BY_ID = {s[0]: s for s in FRAMEWORK_SECTIONS}

PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class MigrateError(Exception):
    """数据迁移相关异常，错误信息面向最终用户，可直接展示。"""


# ===================== 口令 → 密钥 =====================

def _derive_keys(passphrase, salt, iterations):
    """口令派生两把 32 字节密钥：整包密钥 / 字段信封密钥（同口令、不同 info 域分离）。"""
    if not passphrase or not str(passphrase).strip():
        raise MigrateError("请填写口令")
    keys = []
    for info in (b"jztools-migrate-container", b"jztools-migrate-envelope"):
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
        keys.append(base64.urlsafe_b64encode(kdf.derive(str(passphrase).encode("utf-8"))))
    return keys[0], keys[1]


def _wrap_container(zip_bytes, salt, iterations, container_key):
    """把 zip 字节封装成容器字节（包头 + 密文）。"""
    token = Fernet(container_key).encrypt(zip_bytes)
    return MAGIC + iterations.to_bytes(4, "big") + salt + token


def encrypt_container(zip_bytes, passphrase, iterations=KDF_ITERATIONS):
    """把 zip 字节封装为口令加密容器（`.jzdata` 文件内容）。

    ⚠ **只用于新建包**：本函数每次生成新盐，而**字段信封密钥与盐绑定**（同一口令 +
    同一盐才派生同一把信封密钥）。拿它去"重封"一个已含 `JZMIG1:` 字段信封的包，
    会让那些字段在导入时解不开（报"口令与包不匹配"）。要换口令重打包，请用
    `build_package()` 重新导出（它会用新盐重新封装字段）。
    """
    salt = os.urandom(SALT_BYTES)
    container_key, _envelope_key = _derive_keys(passphrase, salt, iterations)
    return _wrap_container(zip_bytes, salt, iterations, container_key)


def read_container_head(blob):
    """读取容器头，返回 (迭代次数, 盐)；格式非法时抛可读异常。

    供"在既有包上做二次加工"的场景复用同一把字段信封密钥（见 encrypt_container 的告警）。
    """
    head = len(MAGIC) + 4 + SALT_BYTES
    if not blob or len(blob) <= head or not blob.startswith(MAGIC):
        raise MigrateError("不是有效的数据迁移包（缺少格式标识）")
    iterations = int.from_bytes(blob[len(MAGIC):len(MAGIC) + 4], "big")
    if not (10_000 <= iterations <= 5_000_000):
        raise MigrateError("数据迁移包头损坏（KDF 迭代次数异常）")
    return iterations, blob[len(MAGIC) + 4:head]


def decrypt_container(blob, passphrase):
    """解开容器，返回 (zip 字节, 字段信封密钥)；口令错误 / 包被篡改时抛可读异常。"""
    head = len(MAGIC) + 4 + SALT_BYTES
    if not blob or len(blob) <= head:
        raise MigrateError("不是有效的数据迁移包（文件太小或已损坏）")
    if not blob.startswith(MAGIC):
        raise MigrateError("不是有效的数据迁移包（缺少格式标识；请选择导出的 .jzdata 文件）")
    pos = len(MAGIC)
    iterations = int.from_bytes(blob[pos:pos + 4], "big")
    pos += 4
    salt = blob[pos:pos + SALT_BYTES]
    pos += SALT_BYTES
    if not (10_000 <= iterations <= 5_000_000):
        raise MigrateError("数据迁移包头损坏（KDF 迭代次数异常）")
    container_key, envelope_key = _derive_keys(passphrase, salt, iterations)
    try:
        zip_bytes = Fernet(container_key).decrypt(blob[pos:])
    except InvalidToken:
        raise MigrateError("口令不正确，或数据包已损坏 / 被篡改") from None
    return zip_bytes, envelope_key


# ===================== 字段信封（本机密钥 ⇄ 口令密钥） =====================

def _is_envelope(value):
    return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)


def _try_local_decrypt(fernet, value):
    """能用本机密钥解开的字符串 → 明文；否则 None（不是密文，原样保留）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return fernet.decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeEncodeError, ValueError, TypeError):
        return None


def _walk_strings(node, fn):
    """递归遍历 JSON，对每个**字符串值**调用 fn；返回 (新结构, 改动计数)。"""
    if isinstance(node, dict):
        out = {}
        count = 0
        for key, val in node.items():
            if isinstance(val, str):
                new_val = fn(val)
                out[key] = new_val
                count += 0 if new_val == val else 1
            else:
                out[key], sub = _walk_strings(val, fn)
                count += sub
        return out, count
    if isinstance(node, list):
        out = []
        count = 0
        for val in node:
            if isinstance(val, str):
                new_val = fn(val)
                out.append(new_val)
                count += 0 if new_val == val else 1
            else:
                new_val, sub = _walk_strings(val, fn)
                out.append(new_val)
                count += sub
        return out, count
    return node, 0


def _load_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def seal_json_bytes(raw, fernet, envelope_key):
    """导出用：把 JSON 里能用本机密钥解开的密文字段改封为口令信封。

    没有密文字段（或不是 JSON）时**原样返回原字节**——避免无谓地重排用户的文件格式
    （插件业务数据多为 JSON，逐字节保真比重新序列化更重要）。
    返回 (字节, 改封字段数)。
    """
    data = _load_json(raw)
    if data is None:
        return raw, 0

    def fn(value):
        plain = _try_local_decrypt(fernet, value)
        if plain is None:
            return value
        token = Fernet(envelope_key).encrypt(plain.encode("utf-8")).decode("ascii")
        return ENVELOPE_PREFIX + token

    sealed, count = _walk_strings(data, fn)
    if not count:
        return raw, 0
    return json.dumps(sealed, ensure_ascii=False, indent=2).encode("utf-8"), count


def open_json_bytes(raw, fernet, envelope_key):
    """导入用：把口令信封改封为**目标机**密钥；无信封时原样返回。"""
    data = _load_json(raw)
    if data is None:
        return raw, 0

    def fn(value):
        if not _is_envelope(value):
            return value
        token = value[len(ENVELOPE_PREFIX):]
        try:
            plain = Fernet(envelope_key).decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeEncodeError):
            raise MigrateError("数据包内的密钥字段无法解开（口令与包不匹配，或包被篡改）") from None
        return fernet.encrypt(plain.encode("utf-8")).decode("ascii") if plain else ""

    opened, count = _walk_strings(data, fn)
    if not count:
        return raw, 0
    return json.dumps(opened, ensure_ascii=False, indent=2).encode("utf-8"), count


# ===================== 采集：哪些文件进包 =====================

def _excluded(name, is_dir):
    if is_dir:
        return name in EXCLUDE_DIRS
    if name in EXCLUDE_FILES:
        return True
    low = name.lower()
    return any(low.endswith(suf) for suf in EXCLUDE_SUFFIXES)


def _iter_files(root, rel_prefix=()):
    """遍历目录下应进包的文件，产出 (数据根相对路径元组, 绝对路径, 字节数)。"""
    out = []
    if not os.path.isdir(root):
        return out
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not _excluded(d, True))
        for name in sorted(files):
            if _excluded(name, False):
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root)
            out.append((tuple(rel_prefix) + tuple(rel.split(os.sep)), full, os.path.getsize(full)))
    return out


def _plugin_version(base_dir, pid):
    try:
        with open(os.path.join(base_dir, "plugins", pid, "manifest.json"), encoding="utf-8") as f:
            return str((json.load(f) or {}).get("version") or "")
    except Exception:
        return ""


def _is_installed(base_dir, pid):
    return os.path.isfile(os.path.join(base_dir, "plugins", pid, "manifest.json"))


def _section_files(section_id, data_root):
    """按分段 id 取出 (数据根相对路径元组, 绝对路径, 字节数) 列表。"""
    if section_id in _FRAMEWORK_BY_ID:
        parts = _FRAMEWORK_BY_ID[section_id][2]
        path = os.path.join(data_root, *parts)
        return [(tuple(parts), path, os.path.getsize(path))] if os.path.isfile(path) else []
    if section_id.startswith("plugin:"):
        pid = section_id.split(":", 1)[1]
        if not PLUGIN_ID_RE.match(pid):
            return []
        return _iter_files(os.path.join(data_root, "plugins", pid), ("plugins", pid))
    return []


def plan_export(data_root, base_dir, plugin_labels=None):
    """列出可导出的分段（框架三段 + 各插件数据段），供导出界面勾选。

    「已安装插件」以**程序目录** `plugins/<id>/manifest.json` 为准；数据根里仍有数据的
    插件（如已卸载但数据留着）也会列出并标注"未安装"，避免用户以为数据"不见了"。
    """
    plugin_labels = plugin_labels or {}
    sections = []
    for sid, label, parts in FRAMEWORK_SECTIONS:
        path = os.path.join(data_root, *parts)
        if os.path.isfile(path):
            sections.append({"id": sid, "kind": "config", "label": label,
                             "files": 1, "bytes": os.path.getsize(path),
                             "installed": True, "version": ""})

    ids = set()
    data_plugins = os.path.join(data_root, "plugins")
    if os.path.isdir(data_plugins):
        ids |= {d for d in os.listdir(data_plugins)
                if PLUGIN_ID_RE.match(d) and os.path.isdir(os.path.join(data_plugins, d))}
    inst_plugins = os.path.join(base_dir, "plugins")
    if os.path.isdir(inst_plugins):
        ids |= {d for d in os.listdir(inst_plugins)
                if PLUGIN_ID_RE.match(d) and _is_installed(base_dir, d)}

    for pid in sorted(ids):
        files = _iter_files(os.path.join(data_plugins, pid))
        if not files:
            continue
        sections.append({
            "id": "plugin:" + pid, "kind": "plugin", "plugin_id": pid,
            "label": plugin_labels.get(pid) or pid,
            "files": len(files), "bytes": sum(f[2] for f in files),
            "installed": _is_installed(base_dir, pid),
            "version": _plugin_version(base_dir, pid),
        })
    return sections


# ===================== 导出 =====================

def build_package(data_root, base_dir, section_ids, passphrase, fernet,
                  app_version="", source_label="", plugin_labels=None, now=None):
    """按分段打包并加密，返回 (包字节, 清单 dict)。fernet 为**本机**密钥实例。"""
    if not section_ids:
        raise MigrateError("请至少选择一个要导出的数据段")
    if len(str(passphrase or "")) < 8:
        raise MigrateError("导出口令至少 8 位（这是打开数据包的唯一凭据，遗失后无法恢复）")

    now = now or datetime.now()
    salt = os.urandom(SALT_BYTES)
    container_key, envelope_key = _derive_keys(passphrase, salt, KDF_ITERATIONS)

    manifest = {
        "schema": SCHEMA,
        "format": FORMAT,
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "app_version": app_version or "",
        "source": source_label or socket.gethostname(),
        "sections": [],
        "totals": {"files": 0, "bytes": 0},
    }
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sid in section_ids:
            entries = _section_files(sid, data_root)
            if not entries:
                continue
            if sid in _FRAMEWORK_BY_ID:
                sec = {"id": sid, "kind": "config", "label": _FRAMEWORK_BY_ID[sid][1]}
            else:
                pid = sid.split(":", 1)[1]
                # 段名写进清单：让包**自描述**（目标机没装该插件、或 tools.json 里没有它时，
                # 预览仍能显示中文名，而不是一串英文 id）
                sec = {"id": sid, "kind": "plugin", "plugin_id": pid,
                       "label": (plugin_labels or {}).get(pid) or pid,
                       "version": _plugin_version(base_dir, pid)}
            sec["files"] = 0
            sec["bytes"] = 0
            sec["secrets"] = 0
            for parts, full, _size in entries:
                try:
                    with open(full, "rb") as f:
                        raw = f.read()
                except OSError as exc:
                    raise MigrateError(
                        f"读取文件失败：{os.path.basename(full)}（{exc.strerror}）") from None
                # 字段改封：账号/大模型配置里的密文换成口令信封（其余文件原字节进包）
                payload, sealed = seal_json_bytes(raw, fernet, envelope_key)
                total += len(payload)
                if total > MAX_TOTAL_BYTES:
                    raise MigrateError("导出数据超过上限（1 GB）：请减少导出段或先清理任务缓存")
                zf.writestr("/".join(parts), payload)
                sec["files"] += 1
                sec["bytes"] += len(payload)
                sec["secrets"] += sealed
            manifest["sections"].append(sec)
            manifest["totals"]["files"] += sec["files"]
            manifest["totals"]["bytes"] += sec["bytes"]
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    blob = _wrap_container(buf.getvalue(), salt, KDF_ITERATIONS, container_key)
    if len(blob) > MAX_PACKAGE_BYTES:
        raise MigrateError("数据包超过上限（300 MB）：请减少导出段或先清理任务缓存")
    return blob, manifest


# ===================== 读取 / 预览 =====================

def _open_zip(zip_bytes):
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise MigrateError("数据包内容损坏（不是有效的压缩包）") from None
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        zf.close()
        raise MigrateError(f"包内条目数超过上限 {MAX_ENTRIES}")
    total = 0
    for info in infos:
        total += int(info.file_size or 0)
        if total > MAX_TOTAL_BYTES:
            zf.close()
            raise MigrateError("解压后体积超过上限（1 GB）")
    return zf


def _normalize(name):
    return (name or "").replace("\\", "/")


def _owner_section(name, section_ids):
    """条目属于哪个分段；不属于任何已声明分段（含不安全路径）时返回 None。"""
    name = _normalize(name)
    if not name or name.endswith("/"):
        return None
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ":" in name:
        return None
    parts = name.split("/")
    if any(p in ("", "..") for p in parts):
        return None
    for sid, _label, rel in FRAMEWORK_SECTIONS:
        if tuple(parts) == tuple(rel):
            return sid if sid in section_ids else None
    if len(parts) >= 2 and parts[0] == "plugins":
        pid = parts[1]
        sid = "plugin:" + pid
        if PLUGIN_ID_RE.match(pid) and sid in section_ids:
            return sid
    return None


def _read_manifest(zf):
    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
        raise MigrateError("数据包缺少清单文件（manifest.json），不是本系统导出的迁移包") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise MigrateError("数据包格式不受支持（不是本系统导出的迁移包）")
    if int(manifest.get("schema") or 0) != SCHEMA:
        raise MigrateError("数据包格式版本不受支持（请用同版本或更新的程序导出）")
    return manifest


def inspect(blob, passphrase, data_root, plugin_labels=None):
    """解密 + 校验 + 汇总，返回预览信息（**不写任何文件**）。"""
    zip_bytes, envelope_key = decrypt_container(blob, passphrase)
    zf = _open_zip(zip_bytes)
    try:
        manifest = _read_manifest(zf)
        section_ids = {s.get("id") for s in manifest.get("sections") or []}
        # 逐条归类（一次遍历）：段内文件数 / 目标机已存在的文件数
        counts = {}
        for info in zf.infolist():
            sid = _owner_section(info.filename, section_ids)
            if sid is None:
                continue
            name = _normalize(info.filename)
            target = os.path.join(data_root, *name.split("/"))
            row = counts.setdefault(sid, {"files": 0, "existing": 0})
            row["files"] += 1
            if os.path.isfile(target):
                row["existing"] += 1
    finally:
        zf.close()

    sections = []
    for sec in manifest.get("sections") or []:
        sid = sec.get("id") or ""
        pid = sec.get("plugin_id") or ""
        row = counts.get(sid, {"files": 0, "existing": 0})
        sections.append({
            "id": sid,
            # 展示名优先用**本机** tools.json 的称呼（权威），退回包内清单里的名字
            "label": (plugin_labels or {}).get(pid) or sec.get("label") or pid,
            "kind": sec.get("kind") or ("plugin" if sid.startswith("plugin:") else "config"),
            "plugin_id": pid,
            "version": sec.get("version", ""),
            "files": int(sec.get("files") or row["files"]),
            "bytes": int(sec.get("bytes") or 0),
            "secrets": int(sec.get("secrets") or 0),
            "existing": row["existing"],       # 目标机已存在（将被覆盖）的文件数
            "installed": True,
        })
    return {
        "created_at": manifest.get("created_at", ""),
        "source": manifest.get("source", ""),
        "app_version": manifest.get("app_version", ""),
        "totals": manifest.get("totals") or {"files": 0, "bytes": 0},
        "sections": sections,
        "envelope_key": envelope_key,          # 仅本次请求内交给 apply，不回传前端
        "zip_bytes": zip_bytes,
    }


# ===================== 导入 =====================

def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def apply_import(zip_bytes, envelope_key, data_root, section_ids, fernet,
                 backup_root=None, now=None):
    """把选中的分段写入数据根；覆盖前先备份被覆盖的文件。返回结果报告。"""
    if not section_ids:
        raise MigrateError("请至少选择一个要导入的数据段")
    now = now or datetime.now()
    zf = _open_zip(zip_bytes)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(backup_root or os.path.join(data_root, "backups", "migrate"), stamp)

    written = skipped = backed_up = written_bytes = 0
    plugins_touched = set()
    accounts_written = False
    try:
        for info in zf.infolist():
            name = _normalize(info.filename)
            if not name or name.endswith("/") or name == "manifest.json":
                continue
            if _owner_section(name, section_ids) is None:
                skipped += 1                       # 未勾选 / 不在白名单：一律不写
                continue
            parts = name.split("/")
            with zf.open(info) as src:
                raw = src.read()
            payload, _sealed = open_json_bytes(raw, fernet, envelope_key)

            target = os.path.join(data_root, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.isfile(target):
                with open(target, "rb") as f:
                    old = f.read()
                if _sha256(old) == _sha256(payload):
                    skipped += 1                   # 内容一致：幂等，不写不备份
                    continue
                bpath = os.path.join(backup_dir, *parts)
                os.makedirs(os.path.dirname(bpath), exist_ok=True)
                with open(bpath, "wb") as f:
                    f.write(old)
                backed_up += 1
            tmp = target + ".tmp"
            with open(tmp, "wb") as f:
                f.write(payload)
            os.replace(tmp, target)
            written += 1
            written_bytes += len(payload)
            if len(parts) >= 2 and parts[0] == "plugins":
                plugins_touched.add(parts[1])
            if tuple(parts) == ("config", "admin.json"):
                accounts_written = True
    finally:
        zf.close()

    return {
        "written": written,
        "skipped": skipped,
        "backed_up": backed_up,
        "bytes": written_bytes,
        "plugins": sorted(plugins_touched),
        "accounts_written": accounts_written,
        "backup_dir": backup_dir if backed_up else "",
        "at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def cleanup_backups(backup_root, keep=3):
    """只保留最近 N 份导入备份（其余删除），避免备份无限增长。返回删除份数。"""
    if not backup_root or not os.path.isdir(backup_root):
        return 0
    try:
        names = sorted((d for d in os.listdir(backup_root)
                        if os.path.isdir(os.path.join(backup_root, d))), reverse=True)
    except OSError:
        return 0
    removed = 0
    for name in names[keep:]:
        shutil.rmtree(os.path.join(backup_root, name), ignore_errors=True)
        removed += 1
    return removed


def default_filename(now=None):
    """导出文件名：JZToolsHub-数据迁移-<日期-时间>.jzdata。"""
    now = now or datetime.now()
    return f"JZToolsHub-数据迁移-{now.strftime('%Y%m%d-%H%M')}{EXT}"
