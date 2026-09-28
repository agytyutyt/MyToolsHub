"""文件过滤器 —— JZToolsHub 后端插件路由。

功能：对上传表格（xlsx/xls/csv，≤20MB）做字段过滤（脱敏）与文本后处理（合规检查）：
- 预处理：**删除背景图片**（识别文档里嵌入的工作表背景图片并摘除，见 bg_image.py）；
- 上传预览：识别文档里的**列名**并给出硬过滤预判（哪些列会被删）与后处理预判
  （哪些列名会被改、预计替换多少处），用户按列开关、决定是否启用后处理，再提交过滤；
- 硬过滤：按固定字段名单保留列（规整后精确匹配），其余删除；
- 大模型过滤：提取全部表头交大模型判断与保留名单的语义关联（「时间」↔「开始时间」），
  匹配保留、其余删除；名单内同名字段直接保留（用户点名要的列不交给模型判断）；
- 自学习映射缓存（P0）：大模型判定经用户把关后落盘复用——已确认映射直接查表过滤不再
  调大模型，未命中子集才进大模型；建议级映射在预览/结果里可见可改
  （见 docs/design/过滤器自学习字段映射-设计文档.md，存储在 mapping_store.py）；
- 后处理：按管理员配置的文本/正则规则对表头与单元格做替换（如「开始时间」-「开始」→「时间」），
  用户可在预览里**整体关闭**（默认开启）。

接口前缀：/api/file-filter（B-2）。
程序化调用（供其他插件）：POST /api/file-filter/apply —— JSON in / JSON out，同步，不落盘。

并发设计：大模型过滤走后台线程池 + task_id 轮询（8.5 异步任务模式）；
硬过滤同步完成（毫秒级），也统一走任务表返回 task_id，前端只实现一种轮询。
上传预览（/preview）**同步**返回：只读表 + 逐列预演，毫秒~秒级，故不必占用轮询通道
（与轨迹速写 /upload 同口径）；它把上传件暂存下来并返回 staged_id，/filter 可复用它，
用户在预览里反复调整字段时不必重复上传文件。
"""

# 会话工具经主体模块 jz_api 取用（依赖倒置：插件不再 import admin 插件的内部模块，
# 插件之间零依赖；admin 未加载时自动降级为「未登录 / 空操作」）。
# 见 docs/design/主体与插件解耦-设计文档.md §5.1 FC-3。
import jz_api
import jz_deps
import jz_llm

_get_session_user = jz_api.get_session_user
_set_operation = jz_api.set_operation

import csv
import io
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import jsonify, request, send_file

from . import bg_image, core, llm_client, mapping_store

# requests 的可用性由统一大模型模块（jz_llm）持有并转发，供 /status 自检与
# jz_deps 刷新沿用；本插件不再直接 import requests（凭据与 HTTP 调用都归框架）。
REQUESTS_AVAILABLE = llm_client.REQUESTS_AVAILABLE

import jztools_data

CONFIG_FILE = jztools_data.get_data_root_file("plugins", "file-filter", "config.json")
TASK_DIR = jztools_data.get_data_root_dir("plugins", "file-filter", ".task_cache")
# 自学习字段映射存储（两级信任：confirmed > suggested，见 mapping_store.py 模块头）
MAPPINGS_FILE = jztools_data.get_data_root_file("plugins", "file-filter", "mappings.json")
MAPPINGS = mapping_store.MappingStore(MAPPINGS_FILE)
API_PREFIX = "/api/file-filter"
PLUGIN_ID = "file-filter"

FILTER_WORKERS = 2
_executor = ThreadPoolExecutor(max_workers=FILTER_WORKERS)
TASKS = {}
# 匹配修正（P3b）同时只允许一个任务：非阻塞 acquire，占用中再提交返回 409
_recheck_lock = threading.Lock()
# 上传暂存表 {staged_id: {...}}：/preview 落盘的上传件，供 /filter 复用（用户调整字段后
# 不必重新上传）。与 TASKS 同锁同 TTL、同归属校验（非创建者且非超管一律 404）。
STAGES = {}
TASKS_LOCK = threading.Lock()
TASK_TTL_SECONDS = 30 * 60

TASK_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB（SEC-3）

DEFAULT_CONFIG = {
    "keep_columns": [],
    "post_rules": [],
}

# 自学习映射的自动建议条目上限（管理员可配）：到顶只拦自动新增建议（确认/改指不受限），
# 调低不删除已有条目——不采用主动清理策略（2026-09-26 拍板）
MAPPING_LIMIT_DEFAULT = 10000
MAPPING_LIMIT_MIN = 100
MAPPING_LIMIT_MAX = 100000

# 管理权限口径与 knowledge-base / notice-board 一致：超管或管理员角色
MANAGE_ROLE_IDS = {"role-admin"}
MANAGE_ROLE_NAMES = {"管理员"}


# ===================== 会话与权限 =====================

def _viewer():
    """当前登录用户上下文；未登录返回 None。"""
    if _get_session_user is None:
        return None
    try:
        return _get_session_user()
    except Exception:
        return None


def _can_manage(user):
    """管理权限：超级管理员或管理员角色。"""
    if not user:
        return False
    if user.get("super_admin"):
        return True
    return (user.get("role_id") in MANAGE_ROLE_IDS
            or user.get("role") in MANAGE_ROLE_NAMES)


# ===================== 配置读写 =====================

def load_config():
    data = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            data = {}
    keep = data.get("keep_columns")
    rules = data.get("post_rules")
    try:
        limit = int(data.get("mapping_limit"))
    except Exception:
        limit = MAPPING_LIMIT_DEFAULT
    limit = max(MAPPING_LIMIT_MIN, min(MAPPING_LIMIT_MAX, limit))
    return {
        "keep_columns": [str(k).strip() for k in keep if str(k).strip()] if isinstance(keep, list) else [],
        "post_rules": _sanitize_rules(rules),
        "mapping_limit": limit,
    }


def _sanitize_rules(rules):
    """规整后处理规则列表（防脏数据）。"""
    out = []
    if isinstance(rules, list):
        for r in rules:
            if not isinstance(r, dict):
                continue
            pattern = str(r.get("pattern") or "").strip()
            if not pattern:
                continue
            out.append({
                "pattern": pattern[:200],
                "replacement": "" if r.get("replacement") is None else str(r.get("replacement"))[:200],
                "is_regex": bool(r.get("is_regex")),
                "enabled": r.get("enabled") is not False,
            })
    return out


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ===================== 任务表 =====================

def set_task(task_id, **kwargs):
    with TASKS_LOCK:
        TASKS[task_id] = {**TASKS.get(task_id, {}), **kwargs}


def get_task(task_id):
    with TASKS_LOCK:
        return TASKS.get(task_id)


def cleanup_tasks():
    now = time.time()
    with TASKS_LOCK:
        for table in (TASKS, STAGES):
            expired = [tid for tid, t in table.items()
                       if t.get("created_at", 0) < now - TASK_TTL_SECONDS]
            for tid in expired:
                table.pop(tid, None)


def create_task(user):
    tid = uuid.uuid4().hex[:12]
    set_task(tid, status="pending", created_at=time.time(),
             created_by=(user or {}).get("username") or "")
    return tid


def save_stage(blob, ext, original_name, user, sanitize):
    """落盘上传暂存件并登记（文件名用服务端 ID，不沿用原始文件名 —— SEC-3）。"""
    os.makedirs(TASK_DIR, exist_ok=True)
    sid = uuid.uuid4().hex[:12]
    path = os.path.join(TASK_DIR, f"{sid}_upload.{ext}")
    with open(path, "wb") as fp:
        fp.write(blob)
    stage = {"staged_id": sid, "path": path, "ext": ext, "size": len(blob),
             "original_name": original_name, "sanitize": sanitize,
             "created_at": time.time(),
             "created_by": (user or {}).get("username") or ""}
    with TASKS_LOCK:
        STAGES[sid] = stage
    return stage


def get_stage(staged_id):
    with TASKS_LOCK:
        return STAGES.get(staged_id)


def drop_stage(staged_id):
    """丢弃暂存（预览失败时立即回收，不必等 TTL）。"""
    with TASKS_LOCK:
        stage = STAGES.pop(staged_id, None)
    if not stage:
        return
    try:
        os.remove(stage["path"])
    except Exception:
        pass


def _task_owned_by(task, user):
    """任务归属校验：非创建者且非超管视为不存在（404，纵深防御）。"""
    owner = task.get("created_by")
    if not owner:
        return bool(user and user.get("super_admin"))
    if user is None:
        return False
    if user.get("super_admin"):
        return True
    return owner == user.get("username")


def _clean_task_files():
    os.makedirs(TASK_DIR, exist_ok=True)
    now = time.time()
    for name in os.listdir(TASK_DIR):
        path = os.path.join(TASK_DIR, name)
        try:
            if now - os.path.getmtime(path) > TASK_TTL_SECONDS:
                os.remove(path)
        except Exception:
            pass


# ===================== 过滤执行 =====================

def run_filter(headers, rows, mode, keep_columns, post_rules, session, exclude=None,
               use_suggested=False, store=None, username="", suggest_limit=None):
    """执行过滤 + 后处理，返回结果字典（自学习方案 P0 起改为 dict 返回）。

    mode: hard / llm；llm 模式失败抛 llm_client.LLMError。
    session: 统一大模型的接入配置快照，由**请求线程** jz_llm.resolve() 取得后传入
        （后台线程读不到会话，见 jz_llm 模块头部的线程纪律）。
    exclude: 用户在上传预览里**手动关闭**的列名——两种模式下一律删除（用户决定优先于
        模式自身的判定：大模型认为该保留、但用户点了删除，就删除）。
    store: 字段映射存储（mapping_store.MappingStore）；None = 不读写缓存，行为同旧版。
    use_suggested: 是否采用 suggested 级映射（大模型建议、未经确认）。交互路径传 True
        （建议在预览/结果里可见可改，把关发生在用户侧）；程序化调用（/apply）缺省
        False——没有把关环节，只吃已确认映射。
    username: 映射建议的 created_by（审计字段）。
    suggest_limit: 自动建议条目上限（管理员可配）；None 用 mapping_store 内置缺省。

    返回 {headers2, rows2, kept, removed, removed_detail, replace_count, llm_used,
    mappings_used}：
    - kept: [{"column","matched","source"}]，source ∈ exact/confirmed/suggested/llm；
    - removed: 列名列表（/result 与 /apply 的既有契约，保持字符串数组）；
    - removed_detail: [{"column","source"}]，source 另有 confirmed_drop/suggested_drop/
      llm_drop/excluded/empty/hard_unmatched；
    - mappings_used: 本次实际采用的缓存/大模型映射（key/sample/target/status/source），
      供结果页映射复核区展示。
    """
    excluded = {core.clean_text(x).casefold() for x in (exclude or []) if core.clean_text(x)}
    keep_fold = {}
    for k in (keep_columns or []):
        kc = core.clean_text(k)
        if kc:
            keep_fold.setdefault(kc.casefold(), kc)
    valid_targets = set(keep_fold)
    llm_used = False
    mappings_used = []

    if mode == "llm":
        # 逐列先走零成本档：exclude（用户否决）→ 已确认映射（confirmed 是用户把关过的
        # 最强依据，压过名单内同名自匹配——否则 UI 把预填列传回 columns 后，确认映射的
        # 来源与命中统计会被 exact 遮蔽）→ 名单内同名（exact）→ 建议映射；
        # 全部未命中的表头子集才交给大模型（prompt 只含 miss，省钱省时）。
        decide = [None] * len(headers)  # i -> (keep, matched, source)
        tier_hits = {"hit_exact": 0, "hit_confirmed": 0, "hit_suggested": 0}
        for i, h in enumerate(headers):
            hf = core.clean_text(h).casefold()
            if not hf:
                decide[i] = (False, "", "empty")
                continue
            if hf in excluded:
                decide[i] = (False, "", "excluded")
                continue
            hit = store.lookup(h, valid_targets, include_suggested=False, tiers="confirmed") \
                if store is not None else None
            if hit is not None:
                if hit["target"]:
                    decide[i] = (True, hit["target"], "confirmed")
                else:
                    decide[i] = (False, "", "confirmed_drop")
                tier_hits["hit_confirmed"] += 1
                mappings_used.append({"key": hit["key"], "sample": hit["sample"],
                                      "target": hit["target"], "status": "confirmed",
                                      "source": "confirmed"})
                continue
            hit_remap = store.lookup_remap(h) if store is not None else None
            if hit_remap is not None:
                # 待重映射（P4）：用户曾翻转保留但目标未定 → 临时保留，交管理员重映射
                decide[i] = (True, "", "remap_keep")
                continue
            if hf in keep_fold:
                # 名单内同名字段直接保留：那是用户点名要的列，不把判断权交给模型
                # （模型偶发返回空值时会让用户"明明开着却被删"，与硬过滤口径也不一致）
                decide[i] = (True, keep_fold[hf], "exact")
                tier_hits["hit_exact"] += 1
                continue
            hit = store.lookup(h, valid_targets, include_suggested=use_suggested,
                               tiers="suggested") if store is not None else None
            if hit is not None:
                if hit["target"]:
                    decide[i] = (True, hit["target"], "suggested")
                else:
                    decide[i] = (False, "", "suggested_drop")
                tier_hits["hit_suggested"] += 1
                mappings_used.append({"key": hit["key"], "sample": hit["sample"],
                                      "target": hit["target"], "status": "suggested",
                                      "source": "suggested"})
            # store 为 None 或缓存未命中 → 留给 LLM（decide 保持 None）
        if store is not None:
            store.bump_stats(**tier_hits)
        miss_names = list(dict.fromkeys(
            headers[i] for i in range(len(headers)) if decide[i] is None))
        if miss_names:
            # 样本值注入（P4）：每个未命中表头取前 3 个非空单元格内容，辅助语义判断
            samples = {}
            if rows:
                col_of = {}
                for i, h in enumerate(headers):
                    col_of.setdefault(h, i)
                for name in miss_names:
                    col = col_of.get(name)
                    if col is None:
                        continue
                    vals = []
                    for r in rows:
                        s = core.cell_to_str(r[col] if col < len(r) else None).strip()
                        if s:
                            vals.append(s[:20])
                        if len(vals) >= 3:
                            break
                    if vals:
                        samples[name] = vals
            mappings = llm_client.match_columns(miss_names, keep_columns, session=session,
                                                samples=samples or None)
            llm_used = True
            model = (getattr(session, "provider", {}) or {}).get("model") \
                if session is not None else ""
            if store is not None:
                store.record_suggestions(miss_names, mappings, valid_targets, model=model,
                                         prompt_ver=llm_client.PROMPT_VER, user=username,
                                         limit=suggest_limit)
                store.bump_stats(llm_calls=1, misses=len(miss_names))
            for i, h in enumerate(headers):
                if decide[i] is not None:
                    continue
                m = str(mappings.get(h) or "").strip()
                if m and (core.clean_text(m).casefold() not in valid_targets
                          or (store is not None and store.is_rejected(h, m))):
                    m = ""  # 名单外按未匹配（match_columns 已兜底，这里再兜一道）；被否决的对不回锅
                if m:
                    decide[i] = (True, m, "llm")
                    mappings_used.append({"key": core.clean_text(h).casefold(),
                                          "sample": core.clean_text(h), "target": m,
                                          "status": "suggested", "source": "llm"})
                else:
                    decide[i] = (False, "", "llm_drop")
                    # 「判定删除」同样进复核区：负映射也要把关（首次运行就能确认/改指）
                    mappings_used.append({"key": core.clean_text(h).casefold(),
                                          "sample": core.clean_text(h), "target": "",
                                          "status": "suggested", "source": "llm"})
        elif store is not None:
            store.bump_stats(llm_saved=1)  # 全部命中缓存，这次大模型调用省下了
        kept, removed_detail = [], []
        for i, h in enumerate(headers):
            keep, matched, source = decide[i]
            if keep:
                kept.append({"column": h, "matched": matched, "source": source})
            else:
                removed_detail.append({"column": h, "source": source})
        headers2 = [headers[i] for i in range(len(headers)) if decide[i][0]]
        rows2 = [[(r[i] if i < len(r) else None)
                  for i in range(len(headers)) if decide[i][0]] for r in rows]
    else:
        keep_eff = [k for k in (keep_columns or [])
                    if core.clean_text(k) and core.clean_text(k).casefold() not in excluded]
        headers2, rows2, kept_pairs, removed = core.filter_columns(headers, rows, keep_eff)
        kept = [{"column": c, "matched": m, "source": "exact"} for c, m in kept_pairs]
        removed_detail = [{"column": c,
                           "source": "excluded" if core.clean_text(c).casefold() in excluded
                           else "hard_unmatched"} for c in removed]

    headers2, rows2, count = core.post_process(headers2, rows2, post_rules)
    return {"headers2": headers2, "rows2": rows2, "kept": kept,
            "removed": [d["column"] for d in removed_detail],
            "removed_detail": removed_detail,
            "replace_count": count, "llm_used": llm_used,
            "mappings_used": mappings_used}


def build_preview(headers, rows, keep_columns, post_rules, store=None):
    """上传预览：识别到的列名 + 硬过滤预判 + 后处理预判 + 缓存映射预填。

    返回 ``{"columns": [...], "summary": {...}}``，每列：

    - ``name`` 列名（规整后；空表头为空串）；``index`` 原列序号（0 起）；
    - ``keep`` / ``matched``：**硬过滤（字段名精确匹配）预判**——该列是否会被保留、
      匹配到名单里的哪个字段。大模型过滤的语义匹配发生在提交之后，故这里是预判；
    - ``match``：**映射缓存预填**（自学习 P1）——该列命中已确认/建议级映射时为
      ``{"target", "status"}``（``target=""`` 为确认/建议删除的负映射），未命中或
      名单内同名（``keep=True``，不走缓存）为 None。``keep/matched`` 仍按硬过滤口径
      不变（向后兼容）；前端在大模型模式下据 ``match`` 预填胶囊并标注来源徽标；
    - ``post_name`` / ``replace_count``：按当前后处理规则预演的表头新名与该列替换处数
      （仅字符串单元格计数，与 :func:`core.post_process` 同口径）；
    - ``locked``：空表头列，无法被"按名字保留"，前端不给切换；
    - ``dup``：同名重复列（同名必然同判，前端点击时一起切换，避免"看着删了其实还在"）。
    """
    keep_fold = {}
    for k in (keep_columns or []):
        kc = core.clean_text(k)
        if kc:
            keep_fold.setdefault(kc.casefold(), kc)
    valid_targets = set(keep_fold)
    dup_counter = {}
    for h in headers:
        hf = core.clean_text(h).casefold()
        dup_counter[hf] = dup_counter.get(hf, 0) + 1
    post = core.post_process_columns(headers, rows, post_rules)

    columns, n_keep, n_drop, n_replace = [], 0, 0, 0
    for i, h in enumerate(headers):
        name = core.clean_text(h)
        matched = keep_fold.get(name.casefold(), "")
        keep = bool(matched)
        match = None
        if store is not None and name:
            # 缓存预填：待重映射 → 已确认 → 建议（与 run_filter 管线顺序一致——
            # remap 临时保留压过名单内同名；预填结果只进前端徽标/预填，不改 keep/matched 口径）
            hit = store.lookup_remap(name) \
                or store.lookup(name, valid_targets, include_suggested=True, tiers="confirmed") \
                or store.lookup(name, valid_targets, include_suggested=True, tiers="suggested")
            if hit is not None:
                match = {"target": hit.get("target", ""), "status": hit["status"]}
        new_header, count = post[i]
        if keep:
            n_keep += 1
            n_replace += count          # 只有保留的列会进后处理，口径与运行结果一致
        else:
            n_drop += 1
        columns.append({
            "index": i,
            "name": name,
            "keep": keep,
            "matched": matched,
            "match": match,
            "post_name": new_header,
            "replace_count": count,
            "locked": not name,
            "dup": dup_counter.get(name.casefold(), 0) > 1,
        })
    return {"columns": columns,
            "summary": {"total": len(headers), "keep": n_keep, "drop": n_drop,
                        "replace": n_replace}}


def _resolve_params(mode, columns_raw):
    """规整模式与保留名单：columns 参数缺省（None/空）时用管理员配置。

    注意「传了空数组」与「没传」在这里都走配置回退（/apply 的既有契约：columns 缺省用
    管理员配置）；/filter 另有一道显式校验——用户把字段全关时直接报错，见 _form_list。
    """
    cfg = load_config()
    if columns_raw:
        keep = columns_raw
    else:
        keep = cfg["keep_columns"]
    if mode not in ("hard", "llm"):
        mode = "hard"
    return cfg, mode, keep


def _form_list(raw):
    """解析表单/请求体里的 JSON 数组字段，返回 (列表, 是否显式提供)。

    区分「没传」（用管理员配置）与「传了空数组」（用户把字段全关了）：混为一谈会让
    "我明明全删了"静默变成"怎么全保留了"。
    """
    if raw is None or raw == "":
        return None, False
    try:
        parsed = json.loads(raw)
    except Exception:
        return None, False
    if not isinstance(parsed, list):
        return None, False
    return [str(v) for v in parsed], True


def _flag(raw, default):
    """解析布尔型表单字段：缺省取 default；"0"/"false"/"no"/"off" 为假。"""
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _mapping_valid_targets():
    """当前保留名单的规整集合：映射的确认校验与失配（orphan）判定都以它为准。"""
    cfg = load_config()
    return {core.clean_text(k).casefold() for k in cfg["keep_columns"] if core.clean_text(k)}


def _mapping_limit():
    """自动建议条目上限（管理员可配，load_config 已夹紧 100~100000）。"""
    return load_config()["mapping_limit"]


def _run_recheck_task(task_id, pairs, keep, valid_targets, session, username):
    """后台执行匹配修正（P3b）：分批重判 → 写回建议级 → 产出 diff（§8.2）。

    占用 _recheck_lock 直到结束（POST 侧 acquire 后交给本任务释放）。
    session 由请求线程 resolve 后传入（线程纪律同过滤任务）。
    """
    try:
        set_task(task_id, status="running")
        model = (getattr(session, "provider", {}) or {}).get("model") or ""
        batch = llm_client.RECHECK_BATCH_SIZE
        judged = {}
        for i in range(0, len(pairs), batch):
            chunk = pairs[i:i + batch]
            res = llm_client.recheck_mappings(chunk, keep, session=session)
            judged.update(res)
            set_task(task_id, processed=min(i + batch, len(pairs)))
        outcomes = MAPPINGS.apply_recheck(
            [{"key": p["key"], "new_target": judged.get(p["key"], p["old_target"])}
             for p in pairs],
            valid_targets, model=model, prompt_ver=llm_client.RECHECK_PROMPT_VER,
            user=username)
        diffs = []
        for p in pairs:
            entry = MAPPINGS.get_entry(p["key"])
            new_target = entry["target"] if entry else p["old_target"]
            if mapping_store.norm_key(new_target) != mapping_store.norm_key(p["old_target"]):
                diffs.append({"key": p["key"], "sample": p["sample"],
                              "old_target": p["old_target"], "new_target": new_target})
        set_task(task_id, status="done", diffs=diffs, processed=len(pairs),
                 outcomes=outcomes)
    except llm_client.LLMError as e:
        set_task(task_id, status="error", detail=f"匹配修正失败：{e}")
    except Exception as e:  # SEC-5：不透出堆栈与路径；异常消息截断供排障（如 KeyError 键名）
        set_task(task_id, status="error",
                 detail=f"匹配修正失败（{type(e).__name__}: {e}）"[:300])
    finally:
        _recheck_lock.release()


def _run_filter_task(task_id, in_path, in_ext, out_path, out_ext, mode, keep, post_rules,
                     session, exclude=None):
    """后台线程执行：读表 → 过滤 → 后处理 → 写出 → 任务置 done。

    session 由请求线程 jz_llm.resolve() 取得后传入（后台线程读不到会话）。
    交互路径吃 suggested 级映射（把关发生在预览/结果页的用户动作里）；
    大模型建议的 created_by 取任务创建者（审计字段）。
    """
    try:
        set_task(task_id, status="running")
        username = (get_task(task_id) or {}).get("created_by") or ""
        headers, rows = core.read_table(in_path, f"input.{in_ext}")
        result = run_filter(headers, rows, mode, keep, post_rules, session, exclude,
                            use_suggested=True, store=MAPPINGS, username=username,
                            suggest_limit=_mapping_limit())
        core.write_table(out_path, out_ext, result["headers2"], result["rows2"])
        set_task(task_id, status="done",
                 kept=result["kept"],
                 removed=result["removed"],
                 removed_detail=result["removed_detail"],
                 mappings_used=result["mappings_used"],
                 replace_count=result["replace_count"],
                 llm_used=result["llm_used"],
                 rows=len(result["rows2"]),
                 filename=os.path.basename(out_path),
                 download=f"{API_PREFIX}/download/{task_id}",
                 output_ext=out_ext)
    except llm_client.LLMError as e:
        set_task(task_id, status="error", detail=f"大模型过滤失败：{e}")
    except core.TableError as e:
        set_task(task_id, status="error", detail=str(e))
    except Exception as e:  # SEC-5：不透出堆栈与路径
        set_task(task_id, status="error", detail=f"过滤失败（{type(e).__name__}）")


# ===================== 路由注册 =====================

def register(app):
    # 依赖可用性按请求实时刷新：服务运行中装「依赖组件包」后无需重启即可生效
    # （标记在模块导入时算好，不刷新会让插件页面一直显示"未安装"——jz_deps.refresh_flags）
    jz_deps.install_refresher(app, globals(), core)
    # ★ 路由函数命名纪律：一律带插件前缀 ff_*。
    # Flask 以 view_func.__name__ 作为 endpoint，若本插件定义 def status() 而其他插件
    # 也定义同名函数，会在启动阶段抛 AssertionError 导致注册失败（历史缺陷：本插件
    # 曾是全项目唯一不带前缀的路由集合）。详见 docs/archive/P0问题修复方案.md FIX-4。
    @app.get(f"{API_PREFIX}/status")
    def ff_status():
        cfg = load_config()
        return jsonify({
            "ok": True,
            "dependencies": {
                "openpyxl": core.OPENPYXL_AVAILABLE,
                "xlrd": core.XLRD_AVAILABLE,
                "requests": REQUESTS_AVAILABLE,
            },
            "llm_configured": jz_llm.resolve(PLUGIN_ID).configured(),
        })

    @app.get(f"{API_PREFIX}/config")
    def ff_config_get():
        cfg = load_config()
        cfg = json.loads(json.dumps(cfg))  # 深拷贝
        # 统一大模型落地后本插件**不再保存也不回传** API 地址与 Key（SEC-2/F-4）：
        # 接入信息由 jz_llm 按模式解析，这里只回"能不能用 + 用的是哪一份"。
        session = jz_llm.resolve(PLUGIN_ID)
        cfg["llm_configured"] = session.configured()
        cfg["llm_source"] = session.source
        cfg["llm_source_label"] = session.source_label()
        cfg["llm_reason"] = session.reason
        cfg["can_manage"] = _can_manage(_viewer())
        return jsonify(cfg)

    @app.post(f"{API_PREFIX}/config")
    def ff_config_post():
        user = _viewer()
        if not _can_manage(user):
            return jsonify({"error": "仅管理员可修改过滤配置"}), 403
        data = request.get_json(silent=True) or {}
        cfg = load_config()
        # 统一大模型落地后本插件不再接受接入信息，也不读取 config.json 里的历史 llm 段：
        # 该段由启动迁移（migrate_plugin_legacy_llm）收编进统一配置并清除；
        # jz_llm.resolve() 的 plugin_id 不参与解析（兼容桥已于 2026-09-23 移除），本插件不再读取它。
        if "keep_columns" in data:
            keep = data.get("keep_columns")
            cfg["keep_columns"] = [str(k).strip()[:100] for k in keep if str(k).strip()] \
                if isinstance(keep, list) else []
        if "post_rules" in data:
            cfg["post_rules"] = _sanitize_rules(data.get("post_rules"))
        if "mapping_limit" in data:
            try:
                limit = int(data.get("mapping_limit"))
            except Exception:
                return jsonify({"error": f"mapping_limit 需为 {MAPPING_LIMIT_MIN}~"
                                         f"{MAPPING_LIMIT_MAX} 的整数"}), 400
            cfg["mapping_limit"] = max(MAPPING_LIMIT_MIN, min(MAPPING_LIMIT_MAX, limit))
        save_config(cfg)
        _set_operation("保存过滤器配置")
        return jsonify({"ok": True})

    @app.post(f"{API_PREFIX}/config/test")
    def ff_config_test():
        if not _can_manage(_viewer()):
            return jsonify({"error": "仅管理员可测试大模型配置"}), 403
        # 配置入口统一到「管理后台 → 大模型设置」或首页右下角「⋯ → 大模型设置」，
        # 本接口不再接受表单传入的地址/Key（避免插件侧出现第二处凭据输入）。
        ok, detail = jz_llm.test_connection(plugin_id=PLUGIN_ID)
        return jsonify({"ok": ok, "detail": detail})

    # 保留字段名单的表位表头：导入时首格命中则跳过（防把「字段名称」当字段导入）
    _IMPORT_HEADER_HINTS = {"字段", "字段名", "字段名称", "字段列表", "保留字段", "列名"}

    @app.post(f"{API_PREFIX}/config/import-columns")
    def ff_config_import_columns():
        """批量导入保留字段名单（管理员）：解析上传表格的**第一列**，返回字段名清单。

        只解析**不入库**——前端把返回的字段合并进名单芯片，管理员核对后点
        「保存配置」才落盘（与手动添加同一保存口径）。支持 xlsx / xls / csv
        （复用 core.read_table 的解析与容错：不信任 dimension 声明、补齐行宽）；
        空单元格跳过、大小写不敏感去重、首格为表位表头（「字段名称」之类）时跳过。
        """
        user = _viewer()
        if not _can_manage(user):
            return jsonify({"error": "仅管理员可导入保留字段名单"}), 403
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "未选择文件"}), 400
        orig = f.filename or ""
        ext = orig.lower().rsplit(".", 1)[-1] if "." in orig else ""
        if ext not in core.ALLOWED_EXTS:
            return jsonify({"error": "仅支持 xlsx / xls / csv 文件"}), 400
        blob = f.read()
        if len(blob) > MAX_UPLOAD_BYTES:
            return jsonify({"error": "文件超过 20MB 上限"}), 413
        if not blob:
            return jsonify({"error": "文件为空"}), 400
        # SEC-3：落盘文件名用随机 ID，不沿用原始文件名；解析后立即删除
        tmp = os.path.join(TASK_DIR, f"import_{uuid.uuid4().hex[:12]}.{ext}")
        os.makedirs(TASK_DIR, exist_ok=True)
        try:
            with open(tmp, "wb") as fp:
                fp.write(blob)
            headers, rows = core.read_table(tmp, f"import.{ext}")
        except core.TableError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        cells = [headers[0]] if headers else []
        cells.extend(core.cell_to_str(r[0] if r else "") for r in rows)
        seen, columns, skipped = set(), [], 0
        for idx, raw in enumerate(cells):
            name = core.clean_text(raw)
            if not name:
                continue
            if idx == 0 and name.casefold() in _IMPORT_HEADER_HINTS:
                skipped += 1
                continue
            kf = name.casefold()
            if kf in seen:
                skipped += 1
                continue
            seen.add(kf)
            columns.append(name[:100])  # 与 POST /config 的单字段截断同口径
        _set_operation("导入保留字段名单")
        return jsonify({"ok": True, "columns": columns, "count": len(columns),
                        "skipped": skipped, "filename": orig})

    # ===================== 自学习字段映射（P0） =====================
    # 权限口径（设计文档 §7-1，2026-09-26 拍板）：识别/复核环节的操作员（登录用户）
    # 确认/否决字段映射；学习记忆的修改维护（查看全量/删除/清空/导出）归管理员，
    # 与 keep_columns 的管理口径（_can_manage）一致。

    @app.get(f"{API_PREFIX}/mappings")
    def ff_mappings_list():
        if not _can_manage(_viewer()):
            return jsonify({"error": "仅管理员可查看字段映射"}), 403
        status = (request.args.get("status") or "").strip() or None
        q = (request.args.get("q") or "").strip() or None
        all_rows = MAPPINGS.list_entries(_mapping_valid_targets())
        governance = {
            "auto_promoted": sum(1 for e in all_rows if e["confirmed_by"] == "加权转正"),
            "revising": sum(1 for e in all_rows
                            if e.get("dissent_by") and e["status"] == "suggested"),
            "flipped": sum(1 for e in all_rows if e.get("flips", 0) > 0),
        }
        return jsonify({
            "ok": True,
            "entries": MAPPINGS.list_entries(_mapping_valid_targets(), status=status, q=q),
            "stats": MAPPINGS.stats(),
            "governance": governance,
            "keep_columns": load_config()["keep_columns"],
            "limit": _mapping_limit(),
            "count": MAPPINGS.count(),
        })

    @app.post(f"{API_PREFIX}/mappings/confirm")
    def ff_mappings_confirm():
        user = _viewer()
        if user is None:
            return jsonify({"error": "未登录"}), 401
        data = request.get_json(silent=True) or {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        res = MAPPINGS.confirm(items, user=user.get("username") or "",
                               valid_targets=_mapping_valid_targets(),
                               force=bool(data.get("force")))
        if res["conflicts"]:
            return jsonify({"error": "部分字段已有不同的已确认映射，请确认是否覆盖",
                            "conflicts": res["conflicts"]}), 409
        _set_operation("确认字段映射")
        return jsonify({"ok": True, "confirmed": res["confirmed"]})

    @app.post(f"{API_PREFIX}/mappings/reject")
    def ff_mappings_reject():
        user = _viewer()
        if user is None:
            return jsonify({"error": "未登录"}), 401
        data = request.get_json(silent=True) or {}
        key = str(data.get("key") or "")
        target = str(data.get("target") or "")
        if not mapping_store.norm_key(key) or not mapping_store.norm_key(target):
            return jsonify({"error": "key 与 target 均不能为空"}), 400
        MAPPINGS.reject(key, target, user=user.get("username") or "")
        _set_operation("否决字段映射")
        return jsonify({"ok": True})

    @app.post(f"{API_PREFIX}/mappings/delete")
    def ff_mappings_delete():
        # 用 POST + 请求体传 key 而不是 DELETE /mappings/<key>：表头可能含 "/"，进路径不安全
        if not _can_manage(_viewer()):
            return jsonify({"error": "仅管理员可删除字段映射"}), 403
        data = request.get_json(silent=True) or {}
        if not MAPPINGS.delete(str(data.get("key") or "")):
            return jsonify({"error": "映射不存在"}), 404
        _set_operation("删除字段映射")
        return jsonify({"ok": True})

    @app.post(f"{API_PREFIX}/mappings/clear")
    def ff_mappings_clear():
        if not _can_manage(_viewer()):
            return jsonify({"error": "仅管理员可清空字段映射"}), 403
        MAPPINGS.clear()
        _set_operation("清空字段映射")
        return jsonify({"ok": True})

    @app.get(f"{API_PREFIX}/mappings/export")
    def ff_mappings_export():
        if not _can_manage(_viewer()):
            return jsonify({"error": "仅管理员可导出字段映射"}), 403
        status = (request.args.get("status") or "confirmed").strip().lower() or "confirmed"
        if status not in ("confirmed", "suggested", "all"):
            return jsonify({"error": "status 仅支持 confirmed / suggested / all"}), 400
        rows = MAPPINGS.export_records(_mapping_valid_targets(),
                                       status=None if status == "all" else status)
        stamp = datetime.now().strftime("%Y%m%d")
        if (request.args.get("format") or "csv").strip().lower() == "jsonl":
            body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            return send_file(io.BytesIO(body.encode("utf-8")), as_attachment=True,
                             download_name=f"mappings_golden_{stamp}.jsonl",
                             mimetype="application/x-ndjson")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["表头样本", "映射目标", "状态"])
        for r in rows:
            w.writerow([r["header"], r["target"], r["status"]])
        return send_file(io.BytesIO(buf.getvalue().encode("utf-8-sig")), as_attachment=True,
                         download_name=f"mappings_golden_{stamp}.csv", mimetype="text/csv")

    @app.post(f"{API_PREFIX}/mappings/remap")
    def ff_mappings_remap():
        """删除→保留翻转（P4）：条目转「待重映射」——目标待定，管线临时保留，
        进失配分组交管理员经匹配修正重映射后最终确定。"""
        user = _viewer()
        if user is None:
            return jsonify({"error": "未登录"}), 401
        data = request.get_json(silent=True) or {}
        key = str(data.get("key") or "")
        if not mapping_store.norm_key(key):
            return jsonify({"error": "key 不能为空"}), 400
        if not MAPPINGS.flip_to_remap(key, user=user.get("username") or ""):
            return jsonify({"error": "映射不存在或已确认，无法转入待重映射"}), 409
        _set_operation("标记映射待重映射")
        return jsonify({"ok": True})

    @app.post(f"{API_PREFIX}/mappings/vote")
    def ff_mappings_vote():
        """加权计票（P4）：被动采纳项逐条 +1 票（建议级，达判据自动转正）；
        带 `dissent: true` 的项为非管理员对已确认映射的异议 → 降级重议（决策 11）。
        管理员的改指/否决不走此路径（前端按 can_manage 分流，立即生效）。"""
        user = _viewer()
        if user is None:
            return jsonify({"error": "未登录"}), 401
        data = request.get_json(silent=True) or {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        passive, dissents = [], []
        for item in items:
            if not isinstance(item, dict):
                continue
            (dissents if item.get("dissent") else passive).append(item)
        res = MAPPINGS.record_passive_votes(passive, user=user.get("username") or "")
        dissented = 0
        for item in dissents:
            if MAPPINGS.dissent(item.get("key"), item.get("dir"),
                                user=user.get("username") or ""):
                dissented += 1
        _set_operation("映射加权计票")
        return jsonify({"ok": True, "voted": res["voted"],
                        "promoted": res["promoted"], "outcomes": res["outcomes"],
                        "dissented": dissented})

    @app.post(f"{API_PREFIX}/mappings/recheck")
    def ff_mappings_recheck():
        """匹配修正（P3b/P4，管理员）：把条目分批交 LLM 重判，产出 diff 供采纳。

        范围（P4 校准，回归"失配驱动"初衷）：缺省 `mismatch` = 失配条目（名单变更
        orphan suggested + 待重映射 remap）；`all_suggested` 可选扩展为全部建议级。
        **已确认条目一律不动**（决策 ⑥）。新结果由后台写回（apply_recheck，remap
        提出目标后转回建议级），diff 经 GET /mappings/recheck/<task_id> 轮询，
        采纳走既有 confirm 接口升级。
        """
        if not _can_manage(_viewer()):
            return jsonify({"error": "仅管理员可执行匹配修正"}), 403
        # session 必须在请求线程内解析（后台线程读不到会话），见 jz_llm 线程纪律
        session = jz_llm.resolve(PLUGIN_ID)
        if not session.configured():
            return jsonify({"error": session.reason or "大模型未配置，无法执行匹配修正"}), 400
        data = request.get_json(silent=True) or {}
        scope = data.get("scope") or "mismatch"
        if scope not in ("mismatch", "all_suggested"):
            return jsonify({"error": "scope 仅支持 mismatch / all_suggested"}), 400
        if not _recheck_lock.acquire(False):
            return jsonify({"error": "已有匹配修正任务进行中，请稍候"}), 409
        try:
            valid_targets = _mapping_valid_targets()
            if scope == "mismatch":
                # 失配 = 名单变更导致的 orphan（建议级 + 已确认，决策 10 扩展）+ 待重映射 remap
                entries = MAPPINGS.list_entries(valid_targets, status="orphan")
                entries += MAPPINGS.list_entries(valid_targets, status="remap")
            else:
                entries = MAPPINGS.list_entries(valid_targets, status="suggested")
                entries += MAPPINGS.list_entries(valid_targets, status="remap")
            if not entries:
                _recheck_lock.release()  # 提前返回必须放锁（否则锁泄漏，后续提交全部 409）
                return jsonify({"error": "没有可复核的条目（已确认条目不在范围；"
                                         "可改用 scope=all_suggested 复核全部建议级）"}), 400
            keep = load_config()["keep_columns"]
            user = _viewer()
            task_id = create_task(user)
            set_task(task_id, type="recheck", status="pending",
                     total=len(entries), processed=0)
            pairs = [{"key": e["key"], "sample": e["sample"], "old_target": e["target"]}
                     for e in entries]
            _executor.submit(_run_recheck_task, task_id, pairs, keep, valid_targets,
                             session, (user or {}).get("username") or "")
            _set_operation("启动映射匹配修正")
            return jsonify({"ok": True, "task_id": task_id, "total": len(entries)})
        except Exception:
            _recheck_lock.release()  # 提交失败路径；成功提交后由 _run_recheck_task 释放
            raise

    @app.get(f"{API_PREFIX}/mappings/recheck/<task_id>")
    def ff_mappings_recheck_result(task_id):
        if not _can_manage(_viewer()):
            return jsonify({"error": "仅管理员可查看匹配修正结果"}), 403
        if not TASK_ID_RE.match(task_id or ""):
            return jsonify({"error": "非法任务 ID"}), 400
        task = get_task(task_id)
        if not task or task.get("type") != "recheck" or not _task_owned_by(task, _viewer()):
            return jsonify({"error": "任务不存在或已过期"}), 404
        payload = {"task_id": task_id, "status": task.get("status"),
                   "processed": task.get("processed") or 0,
                   "total": task.get("total") or 0}
        if task.get("status") == "done":
            payload["diffs"] = task.get("diffs") or []
            payload["outcomes"] = task.get("outcomes") or {}
        elif task.get("status") == "error":
            payload["detail"] = task.get("detail") or "匹配修正失败"
        return jsonify(payload)

    @app.post(f"{API_PREFIX}/preview")
    def ff_preview():
        """上传表格 → 识别列名 → 返回硬过滤与后处理预判（**同步**）。

        只读表 + 逐列预演，毫秒~秒级，故不进任务队列（前端不需要第二种轮询）。
        返回的 ``staged_id`` 可交给 /filter 复用：用户在预览里反复调整字段时
        **不必重复上传文件**（上传件已落盘暂存，TTL 30 分钟）。
        """
        user = _viewer()
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "未选择文件"}), 400
        orig = f.filename or ""
        ext = orig.lower().rsplit(".", 1)[-1] if "." in orig else ""
        if ext not in core.ALLOWED_EXTS:
            return jsonify({"error": "仅支持 xlsx / xls / csv 文件"}), 400
        blob = f.read()
        if len(blob) > MAX_UPLOAD_BYTES:
            return jsonify({"error": "文件超过 20MB 上限"}), 413
        if not blob:
            return jsonify({"error": "文件为空"}), 400

        cfg = load_config()
        cleanup_tasks()
        _clean_task_files()
        # 预处理：删除背景图片（未发现背景图片时原字节返回，不做任何改写）。
        # 必须在落盘暂存之前做：预览与随后的过滤读到的必须是同一份"干净"字节。
        blob, sanitize = bg_image.strip_background_images(blob, ext)
        stage = save_stage(blob, ext, orig, user, sanitize)
        try:
            headers, rows = core.read_table(stage["path"], f"upload.{ext}")
        except core.TableError as e:
            drop_stage(stage["staged_id"])   # 预览失败即回收，不占 TTL
            return jsonify({"error": str(e)}), 400

        preview = build_preview(headers, rows, cfg["keep_columns"], cfg["post_rules"],
                                store=MAPPINGS)
        _set_operation("预览表格字段与过滤预判")
        return jsonify({
            "ok": True,
            "staged_id": stage["staged_id"],
            "filename": orig,
            "size": len(blob),
            "ext": ext,
            "rows": len(rows),
            "columns": preview["columns"],
            "summary": preview["summary"],
            "post_rules": cfg["post_rules"],
            "keep_columns": cfg["keep_columns"],
            "sanitize": sanitize,
            "llm_configured": jz_llm.resolve(PLUGIN_ID).configured(),
        })

    @app.post(f"{API_PREFIX}/filter")
    def ff_filter():
        user = _viewer()
        f = request.files.get("file")
        staged_id = (request.form.get("staged_id") or "").strip()
        stage = None
        if f is not None and f.filename:
            orig = f.filename or ""
            ext = orig.lower().rsplit(".", 1)[-1] if "." in orig else ""
            if ext not in core.ALLOWED_EXTS:
                return jsonify({"error": "仅支持 xlsx / xls / csv 文件"}), 400
            blob = f.read()
            if len(blob) > MAX_UPLOAD_BYTES:
                return jsonify({"error": "文件超过 20MB 上限"}), 413
            if not blob:
                return jsonify({"error": "文件为空"}), 400
        else:
            # 复用 /preview 暂存的上传件（用户在预览里调整字段后不必重新上传）
            if not TASK_ID_RE.match(staged_id or ""):
                return jsonify({"error": "未选择文件"}), 400
            stage = get_stage(staged_id)
            if not stage or not _task_owned_by(stage, user):
                return jsonify({"error": "上传内容不存在或已过期，请重新选择文件",
                                "code": "staged_expired"}), 404
            orig, ext = stage["original_name"], stage["ext"]

        # v1.5：不再提供过滤模式选择——默认双重过滤（精确 → 学习记忆/大模型）；
        # 大模型不可用时自动降级为仅精确匹配，降级原因随任务结果提示（degraded）。
        mode = (request.form.get("mode") or "llm").strip()
        columns_raw, columns_given = _form_list(request.form.get("columns"))
        exclude_raw, _ = _form_list(request.form.get("exclude"))
        if columns_given and not columns_raw:
            # 显式传空数组 = 用户把字段全关了：报错而不是静默回退管理员名单
            # （静默回退会让"我明明全删了"变成"怎么全保留了"）
            return jsonify({"error": "未选择任何保留字段：请在上方「识别与调整」中至少保留一列"}), 400
        cfg, mode, keep = _resolve_params(mode, columns_raw)
        if not keep:
            return jsonify({"error": "保留字段名单为空：请勾选保留字段或联系管理员配置"}), 400
        # 必须在请求线程内解析（后台线程读不到会话），见 jz_llm 模块头部的线程纪律
        session = jz_llm.resolve(PLUGIN_ID)
        degraded = ""
        if mode == "llm" and not session.configured():
            mode = "hard"
            degraded = session.reason or "大模型未配置"

        task_id = create_task(user)
        cleanup_tasks()
        _clean_task_files()
        if stage is None:
            # ① 预处理：删除背景图片（未发现背景图片时原字节返回，不做任何改写）
            blob, sanitize = bg_image.strip_background_images(blob, ext)
            # SEC-3：落盘文件名用 task_id 重命名，不沿用用户原始文件名
            in_path = os.path.join(TASK_DIR, f"{task_id}_input.{ext}")
            with open(in_path, "wb") as fp:
                fp.write(blob)
        else:
            in_path = stage["path"]
            sanitize = stage.get("sanitize") or {}
        out_ext = "csv" if ext == "csv" else "xlsx"  # .xls 只读，输出转 .xlsx
        out_path = os.path.join(TASK_DIR, f"{task_id}_output.{out_ext}")
        # 后处理默认开启（用户的显式关闭才不跑规则）
        post_enabled = _flag(request.form.get("post_process"), True)
        post_rules = cfg["post_rules"] if post_enabled else []
        set_task(task_id, output_ext=out_ext, original_name=orig, sanitize=sanitize,
                 post_enabled=post_enabled, degraded=degraded)
        _executor.submit(_run_filter_task, task_id, in_path, ext, out_path, out_ext,
                         mode, keep, post_rules, session, exclude_raw)
        _set_operation("提交表格过滤任务")
        return jsonify({"task_id": task_id, "mode": mode, "output_ext": out_ext,
                        "sanitize": sanitize, "post_enabled": post_enabled})

    @app.get(f"{API_PREFIX}/result/<task_id>")
    def ff_result(task_id):
        if not TASK_ID_RE.match(task_id or ""):
            return jsonify({"error": "非法任务 ID"}), 400
        task = get_task(task_id)
        if not task or not _task_owned_by(task, _viewer()):
            return jsonify({"error": "任务不存在或已过期"}), 404
        payload = {
            "task_id": task_id,
            "status": task.get("status"),
        }
        if task.get("status") == "done":
            payload.update({
                "kept": task.get("kept") or [],
                "removed": task.get("removed") or [],
                "removed_detail": task.get("removed_detail") or [],
                "mappings_used": task.get("mappings_used") or [],
                "replace_count": task.get("replace_count") or 0,
                "rows": task.get("rows") or 0,
                "llm_used": bool(task.get("llm_used")),
                "post_enabled": task.get("post_enabled") is not False,
                "sanitize": task.get("sanitize") or {},
                "download": task.get("download"),
                "filename": task.get("original_name") or "",
                "output_ext": task.get("output_ext") or "",
            })
        elif task.get("status") == "error":
            payload["detail"] = task.get("detail") or "过滤失败"
        if task.get("degraded"):
            payload["degraded"] = True
            payload["degraded_reason"] = task.get("degraded") or ""
        return jsonify(payload)

    @app.get(f"{API_PREFIX}/download/<task_id>")
    def ff_download(task_id):
        if not TASK_ID_RE.match(task_id or ""):
            return jsonify({"error": "非法任务 ID"}), 400
        task = get_task(task_id)
        if not task or not _task_owned_by(task, _viewer()):
            return jsonify({"error": "任务不存在或已过期"}), 404
        if task.get("status") != "done":
            return jsonify({"error": "任务尚未完成"}), 409
        out_ext = task.get("output_ext") or "xlsx"
        path = os.path.join(TASK_DIR, f"{task_id}_output.{out_ext}")
        if not os.path.isfile(path):
            return jsonify({"error": "结果文件已过期，请重新过滤"}), 404
        orig = task.get("original_name") or ""
        base = os.path.splitext(os.path.basename(orig))[0] if orig else "filtered"
        download_name = f"{base}_已过滤.{out_ext}"
        _set_operation("下载过滤后文件")
        return send_file(path, as_attachment=True, download_name=download_name)

    @app.post(f"{API_PREFIX}/apply")
    def ff_apply():
        """程序化调用接口（供其他插件调用）：JSON in / JSON out，同步，不落盘。

        请求体：{
          "rows": [[表头...], [数据行]...],   # 必填，首行为表头
          "mode": "hard" | "llm",            # 缺省 hard
          "columns": ["保留字段", ...],       # 缺省用管理员配置
          "exclude": ["强制删除的字段", ...],  # 可选：覆盖模式自身的判定
          "post_rules": [{"pattern","replacement","is_regex","enabled"}],  # 缺省用管理员配置
          "post_process": false,             # 可选：显式关闭后处理（缺省 true）
          "use_suggested": true              # 可选：采用未确认的建议级映射（缺省 false——
        }                                    #   程序化调用没有把关环节，只吃已确认映射）
        响应：{"rows": [...], "kept": [{"column","matched","source"}], "removed": [...],
               "mappings_used": [...], "replace_count": N, "mode": "..."}
        """
        user = _viewer()
        if user is None:
            return jsonify({"error": "未登录"}), 401
        data = request.get_json(silent=True) or {}
        raw_rows = data.get("rows")
        if not isinstance(raw_rows, list) or not raw_rows or not isinstance(raw_rows[0], list):
            return jsonify({"error": "rows 必须为非空的二维数组（首行为表头）"}), 400
        if len(raw_rows) > core.MAX_ROWS + 1:
            return jsonify({"error": f"行数超过上限（≤{core.MAX_ROWS}）"}), 400
        cfg, mode, keep = _resolve_params(
            (data.get("mode") or "hard").strip(),
            data.get("columns") if isinstance(data.get("columns"), list) else None)
        if not keep:
            return jsonify({"error": "保留字段名单为空"}), 400
        post_rules = data.get("post_rules") if isinstance(data.get("post_rules"), list) \
            else cfg["post_rules"]
        if data.get("post_process") is False:
            post_rules = []
        exclude = data.get("exclude") if isinstance(data.get("exclude"), list) else None
        headers = [core.clean_text(h) for h in raw_rows[0]]
        rows = []
        for r in raw_rows[1:]:
            if not isinstance(r, list):
                continue
            row = list(r[:len(headers)])
            if len(row) < len(headers):
                row.extend([None] * (len(headers) - len(row)))
            if all(core.clean_text(v) == "" for v in row):
                continue
            rows.append(row)
        use_suggested = bool(data.get("use_suggested"))
        try:
            result = run_filter(headers, rows, mode, keep, post_rules,
                                jz_llm.resolve(PLUGIN_ID), exclude,
                                use_suggested=use_suggested, store=MAPPINGS,
                                username=(user or {}).get("username") or "",
                                suggest_limit=_mapping_limit())
        except llm_client.LLMError as e:
            return jsonify({"error": f"大模型过滤失败：{e}"}), 502
        except core.TableError as e:
            return jsonify({"error": str(e)}), 400
        _set_operation("程序化表格过滤")
        return jsonify({
            "rows": [result["headers2"]] + result["rows2"],
            "kept": result["kept"],
            "removed": result["removed"],
            "mappings_used": result["mappings_used"],
            "replace_count": result["replace_count"],
            "mode": mode,
        })
