"""文件过滤器 —— 字段映射自学习存储（自学习方案 P0）。

设计文档：docs/design/过滤器自学习字段映射-设计文档.md。纯存储层，不依赖 Flask；
供 routes.py 调用。其他插件按规范 B-7 禁止 import 本模块。

把「表头 → 保留名单字段」的映射按**两级信任**落盘复用：

- ``confirmed``（用户确认/改指）：直接查表过滤，不再问大模型；``target=""`` 为负映射
  （确认删除），同样免问；
- ``suggested``（大模型建议）：下次同样表头直接读建议、省一次 LLM；判定来源在前端
  可见可改，用户的明确动作经 :meth:`MappingStore.confirm` / :meth:`MappingStore.reject`
  落库后升级为 confirmed / 进拦截表；
- ``rejected``（被否决过的映射对）：拦截同一错误建议反复回锅——不入库、本次也按未匹配处理。

并发口径：进程内一把可重入锁串行化读改写；落盘沿用 tmp + ``os.replace`` 原子替换
（与 routes.save_config 同模式）。``hits`` 只在内存累加、随下次落盘顺带持久化
（纯统计字段，崩溃丢失可接受，避免每次命中都写盘）。
"""

import json
import os
import re
import threading
from datetime import datetime

from . import core

SCHEMA_VERSION = 1

STATUS_SUGGESTED = "suggested"
STATUS_CONFIRMED = "confirmed"
STATUS_REMAP = "remap"          # 待重映射：用户翻转保留但未定目标，交管理员复核

# 加权转正判据（P4）：同向票数 ≥ MIN_VOTES 且零翻转 → 建议级自动升级 confirmed。
# 注：设计初稿的「Wilson 下界 ≥0.95」在 5 票时数学上不可达（5/5 一致的下界仅 0.57，
# 需 73 票），故判据为同向一致票数，Wilson 比率保留为展示指标。
PROMOTE_MIN_VOTES = 5
_WILSON_Z = 1.96

MAX_KEY_LEN = 200
MAX_TARGET_LEN = 100
# 自动建议条目上限的内置缺省：管理员可在 config.json 的 mapping_limit 调整
# （routes 侧夹紧 100~100000）。不采用主动清理策略（2026-09-26 拍板）：到顶只拦
# **自动新增**建议——已有条目更新与用户 confirm 不受限，调低不删除已有条目。
DEFAULT_MAX_ENTRIES = 10000

VALID_STATS = ("llm_calls", "llm_saved", "hit_exact", "hit_confirmed",
               "hit_suggested", "misses")

_WS_RE = re.compile(r"\s+")


def norm_key(name):
    """映射主键：与硬过滤同一口径（clean_text + casefold）。"""
    return core.clean_text(name).casefold()


def compact_key(name):
    """压缩键（兜底档）：主键基础上去掉全部内部空白，对付「开 始 时间」类脏表头。"""
    return _WS_RE.sub("", core.clean_text(name)).casefold()


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _coerce_limit(value):
    """上限防御性规整：非法/缺省回内置缺省，至少 1（管理员范围已在 routes 侧夹紧）。"""
    if value is None:
        return DEFAULT_MAX_ENTRIES
    try:
        n = int(value)
    except Exception:
        return DEFAULT_MAX_ENTRIES
    return max(1, n)


def _wilson_lower(p, n, z=_WILSON_Z):
    """Wilson 成功比例置信下界（展示指标：票数的统计可信度）。"""
    if n <= 0:
        return 0.0
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (centre - margin) / denom)


class MappingStore:
    """字段映射存储：懒加载文件 → 进程内副本，读改写全程持锁，变更即原子落盘。"""

    def __init__(self, path):
        self._path = str(path or "")
        self._lock = threading.RLock()
        self._data = None          # 懒加载的进程内副本（见 _sanitize 的结构）
        self._compact_idx = None   # {压缩键: [主键...]}，仅 confirmed；变更后置 None 惰性重建

    # ===================== 落盘 =====================

    def _load_unlocked(self):
        if self._data is not None:
            return self._data
        raw = None
        if self._path and os.path.isfile(self._path):
            try:
                with open(self._path, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
            except Exception:
                raw = None  # 坏文件不致命：按空库重新开始（与 load_config 同口径）
        self._data = self._sanitize(raw)
        return self._data

    def reload(self):
        """丢弃进程内副本，下次访问重新读文件（测试隔离 / 多进程手工同步用）。"""
        with self._lock:
            self._data = None
            self._compact_idx = None
            return self._load_unlocked()

    def _save_unlocked(self):
        if not self._path:
            return
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._data["updated_at"] = _now()
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def _sanitize(self, raw):
        """把任意（可能损坏的）文件内容规整成合法结构；脏数据逐项丢弃而不是整体报废。"""
        data = {"version": SCHEMA_VERSION, "updated_at": "",
                "stats": {k: 0 for k in VALID_STATS}, "entries": {}, "rejected": {}}
        if not isinstance(raw, dict):
            return data
        data["updated_at"] = str(raw.get("updated_at") or "")[:32]
        stats = raw.get("stats")
        if isinstance(stats, dict):
            for k in VALID_STATS:
                try:
                    data["stats"][k] = max(0, int(stats.get(k) or 0))
                except Exception:
                    pass
        entries = raw.get("entries")
        if isinstance(entries, dict):
            for rk, rv in entries.items():
                key = norm_key(rk)
                if not key:
                    continue
                cleaned = self._clean_entry(key, rv)
                if cleaned is None:
                    continue
                # 重复主键取确认态优先（不应发生，防御脏文件）
                old = data["entries"].get(key)
                if old is None or (old["status"] != STATUS_CONFIRMED
                                   and cleaned["status"] == STATUS_CONFIRMED):
                    data["entries"][key] = cleaned
        rejected = raw.get("rejected")
        if isinstance(rejected, dict):
            for rk, pairs in rejected.items():
                key = norm_key(rk)
                if not key or not isinstance(pairs, dict):
                    continue
                bucket = data["rejected"].setdefault(key, {})
                for t, ts in pairs.items():
                    t = norm_key(t)
                    if t:
                        bucket[t] = str(ts or "")[:32]
        return data

    @staticmethod
    def _clean_entry(key, raw):
        if not isinstance(raw, dict):
            return None
        target = "" if raw.get("target") is None else str(raw.get("target")).strip()[:MAX_TARGET_LEN]
        status = raw.get("status")
        if status not in (STATUS_SUGGESTED, STATUS_CONFIRMED, STATUS_REMAP):
            status = STATUS_SUGGESTED
        try:
            hits = max(0, int(raw.get("hits") or 0))
        except Exception:
            hits = 0
        try:
            prompt_ver = max(0, int(raw.get("prompt_ver") or 0))
        except Exception:
            prompt_ver = 0

        def _int(field):
            try:
                return max(0, int(raw.get(field) or 0))
            except Exception:
                return 0

        vote_dir = raw.get("last_vote_dir")
        if vote_dir not in ("keep", "drop"):
            vote_dir = ""
        passive = raw.get("passive_users")
        passive_users = {}
        if isinstance(passive, dict):
            for k, v in list(passive.items())[:500]:
                ku = str(k)[:100]
                if ku:
                    passive_users[ku] = str(v or "")[:32]
        return {
            "key": key,
            "sample": str(raw.get("sample") or key)[:MAX_KEY_LEN],
            "target": target,
            "status": status,
            "source": "user" if raw.get("source") == "user" else "llm",
            "model": str(raw.get("model") or "")[:100],
            "prompt_ver": prompt_ver,
            "hits": hits,
            "keep_votes": _int("keep_votes"),
            "drop_votes": _int("drop_votes"),
            "flips": _int("flips"),
            "streak": _int("streak"),
            "last_vote_dir": vote_dir,
            "last_vote_at": str(raw.get("last_vote_at") or "")[:32],
            "passive_users": passive_users,
            "dissent_by": str(raw.get("dissent_by") or "")[:100],
            "dissent_at": str(raw.get("dissent_at") or "")[:32],
            "created_by": str(raw.get("created_by") or "")[:100],
            "created_at": str(raw.get("created_at") or "")[:32],
            "confirmed_by": str(raw.get("confirmed_by") or "")[:100],
            "confirmed_at": str(raw.get("confirmed_at") or "")[:32],
        }

    def _compact_index_unlocked(self):
        if self._compact_idx is None:
            idx = {}
            for k, e in self._data["entries"].items():
                if e["status"] == STATUS_CONFIRMED:
                    ck = compact_key(k)
                    if ck:
                        idx.setdefault(ck, []).append(k)
            self._compact_idx = idx
        return self._compact_idx

    # ===================== 查找（匹配管线 ③④⑤ 档） =====================

    @staticmethod
    def _entry_valid(entry, valid_targets):
        """target 在当前保留名单内才算有效（target="" 的负映射不依赖名单）。

        名单改名/删项后指向旧目标的映射在此失配（orphan），不参与匹配——
        名单变更不能让陈旧映射悄悄命中。
        """
        if not entry["target"]:
            return True
        return norm_key(entry["target"]) in (valid_targets or set())

    def lookup(self, header, valid_targets, include_suggested=True, tiers="all"):
        """查一个表头的可用映射；命中返回 {key,target,status,sample}，未命中返回 None。

        查找顺序：主键精确 → 压缩键兜底（仅 confirmed，且同压缩键下有效目标唯一才
        生效——多目标命中视为歧义，宁可不命中也不误合并）。``include_suggested=False``
        供没有把关环节的程序化调用（/apply）只吃已确认映射。

        ``tiers`` 限定档位：``"confirmed"`` 只查已确认（主键+压缩键），``"suggested"``
        只查建议（主键），``"all"`` 按主键→压缩键整体查找。匹配管线用它把 confirmed
        档插在「名单内同名」之前、suggested 档放在其后（confirmed 是用户把关过的
        最强依据，优先于自匹配），每次调用最多计一次 hits。
        """
        key = norm_key(header)
        if not key:
            return None
        if tiers not in ("all", "confirmed", "suggested"):
            tiers = "all"
        with self._lock:
            data = self._load_unlocked()
            entries = data["entries"]
            if tiers in ("all", "confirmed", "suggested"):
                entry = entries.get(key)
                if entry is not None and self._entry_valid(entry, valid_targets):
                    want_confirmed = entry["status"] == STATUS_CONFIRMED
                    ok = (want_confirmed and tiers in ("all", "confirmed")) \
                        or (not want_confirmed and include_suggested
                            and tiers in ("all", "suggested"))
                    if ok:
                        entry["hits"] += 1
                        return {"key": key, "target": entry["target"],
                                "status": entry["status"], "sample": entry["sample"]}
            if tiers == "suggested":
                return None  # 建议档不做压缩键兜底（设计如此：压缩键仅 confirmed）
            ck = compact_key(header)
            if not ck:
                return None
            cands = [entries[k] for k in self._compact_index_unlocked().get(ck, [])
                     if k != key and entries[k]["status"] == STATUS_CONFIRMED
                     and self._entry_valid(entries[k], valid_targets)]
            targets = {c["target"] for c in cands}
            if len(targets) != 1:
                return None  # 0 个=无映射；多于 1 个=歧义，不猜
            hit = sorted(cands, key=lambda c: c["key"])[0]
            hit["hits"] += 1
            return {"key": hit["key"], "target": hit["target"],
                    "status": hit["status"], "sample": hit["sample"]}

    def is_rejected(self, header, target):
        """该映射对是否被用户否决过（拦截 LLM 同一错误建议回锅）。"""
        key = norm_key(header)
        t = norm_key(target)
        if not key or not t:
            return False
        with self._lock:
            data = self._load_unlocked()
            return t in (data["rejected"].get(key) or {})

    # ===================== 加权统计（P4）：计票 / 转正 / 待重映射 =====================

    @staticmethod
    def _bump_vote_unlocked(entry, direction, now):
        """+1 票并维护连续同向计数（streak）与方向翻转计数。

        streak：相邻投票方向相同则累加，方向变化清零重计（早期一次异议只重置
        streak，后续共识可重新转正）；方向变化同时记一次 flips（冲突信号，仅展示）。
        """
        if entry.get("last_vote_dir") == direction:
            entry["streak"] = entry.get("streak", 0) + 1
        else:
            if entry.get("last_vote_dir"):
                entry["flips"] = entry.get("flips", 0) + 1
            entry["streak"] = 1
        entry["last_vote_dir"] = direction
        entry["last_vote_at"] = now
        field = "keep_votes" if direction == "keep" else "drop_votes"
        entry[field] = entry.get(field, 0) + 1

    @staticmethod
    def _maybe_promote_unlocked(entry, now):
        """加权定论评估（P4）：建议级/待重映射 + **连续同向票 streak ≥5** → 自动定论。

        - 建议级：投票方向与条目自身方向一致（keep 建议看 keep 票、负建议看 drop 票）
          → 升级 confirmed（target 不变）；
        - **反向多数同样定论**：keep 建议被连续 5 张 drop 票 → 转确认删除（target="")；
          负建议被连续 5 张 keep 票（集体说留但无目标）→ 转 **remap 待重映射**，
          交管理员经匹配修正定目标；
        - **remap：连续 5 张 drop 票 → 集体判定删除（confirmed 负映射）**（P4 收尾）；
          keep 票维持 remap（已是保留态）；
        - 方向变化清零重计——早期一次异议只重置 streak，后续共识可重新定论；
          持续分歧（来回翻转）streak 永不达标 → 停留建议级持续提案（翻转率 = 冲突信号）。
        定论条目 confirmed_by 记「加权转正」（remap 除外，交管理员定目标）。
        """
        if entry["status"] == STATUS_REMAP:
            if entry.get("last_vote_dir") == "drop" and entry.get("streak", 0) >= PROMOTE_MIN_VOTES:
                entry["status"] = STATUS_CONFIRMED
                entry["target"] = ""
                entry["confirmed_by"] = "加权转正"
                entry["confirmed_at"] = now
                return True
            return False
        if entry["status"] != STATUS_SUGGESTED:
            return False
        if entry.get("streak", 0) < PROMOTE_MIN_VOTES:
            return False
        own = "keep" if entry["target"] else "drop"
        last = entry.get("last_vote_dir")
        if last == own:
            entry["status"] = STATUS_CONFIRMED
            entry["confirmed_by"] = "加权转正"
            entry["confirmed_at"] = now
            return True
        if last == "drop" and own == "keep":
            # 集体判定删除：keep 建议转确认删除（负映射）
            entry["target"] = ""
            entry["status"] = STATUS_CONFIRMED
            entry["confirmed_by"] = "加权转正"
            entry["confirmed_at"] = now
            return True
        if last == "keep" and own == "drop":
            # 集体判定保留但无目标：负建议转待重映射，交管理员定目标
            entry["status"] = STATUS_REMAP
            return True
        return False
        return False

    def dissent(self, key, direction, user=""):
        """非管理员对已确认映射的异议（P4 决策 11）：降级重议。

        confirmed → suggested（**现任目标不变**），异议方向计一票（streak 重置），
        记 dissent_by/at；后续连续同向票决定最终走向（原映射正确 → 快速重新转正；
        持续翻转 → 停留建议级交管理员）。与管理员的立即生效改指/否决相区分。
        返回是否发生了降级（一致方向的"异议"为 no-op，返回 False）。
        """
        key = norm_key(key)
        if not key or direction not in ("keep", "drop"):
            return False
        with self._lock:
            data = self._load_unlocked()
            entry = data["entries"].get(key)
            if entry is None or entry["status"] != STATUS_CONFIRMED:
                return False
            own_direction = "keep" if entry["target"] else "drop"
            if direction == own_direction:
                return False  # 与现行确认一致，不构成异议
            now = _now()
            entry["status"] = STATUS_SUGGESTED
            entry["dissent_by"] = str(user or "")[:100]
            entry["dissent_at"] = now
            self._bump_vote_unlocked(entry, direction, now)
            self._compact_idx = None  # confirmed 集合变化
            self._save_unlocked()
            return True

    def record_passive_votes(self, items, user=""):
        """被动采纳计票（P4）：确认流整批采纳的未翻转项逐条 +1 票，不改状态；
        达判据的建议级/待重映射条目自动定论。

        - 建议级：同向 streak ≥5 → confirmed（负建议反向 keep 多数 → 转 remap）；
        - **remap：drop 票 streak ≥5 → 集体判定删除（confirmed 负映射）**（P4 收尾）；
        - **按用户去重**：同一用户对同一条目的被动采纳只计一次（防单人刷票），
          显式动作（确认/改指/否决/异议）不受限。返回 {voted, promoted, outcomes}。
        """
        voted, promoted, outcomes = 0, [], {"confirmed": 0, "remap": 0}
        with self._lock:
            data = self._load_unlocked()
            entries = data["entries"]
            for item in (items or []):
                if not isinstance(item, dict):
                    continue
                key = norm_key(item.get("key"))
                direction = item.get("dir")
                if not key or direction not in ("keep", "drop"):
                    continue
                entry = entries.get(key)
                if entry is None or entry["status"] not in (STATUS_SUGGESTED, STATUS_REMAP):
                    continue
                now = _now()
                voters = entry.setdefault("passive_users", {})
                if user and user in voters:
                    continue  # 同用户同条目只计一次
                if user:
                    voters[user] = now
                was_status = entry["status"]
                self._bump_vote_unlocked(entry, direction, now)
                voted += 1
                if self._maybe_promote_unlocked(entry, now):
                    promoted.append(key)
                    # 定论方向统计：remap 定论（集体判删）与 confirmed 定论分开计数
                    outcomes["remap" if was_status == STATUS_REMAP else "confirmed"] += 1
            if voted:
                self._compact_idx = None  # 转正会改变 confirmed 集合（压缩键索引仅 confirmed）
                self._save_unlocked()
        return {"voted": voted, "promoted": promoted, "outcomes": outcomes}

    def lookup_remap(self, header):
        """待重映射命中（P4）：该表头曾被评为保留但目标未定 → 临时保留。

        仅主键精确匹配（remap 稀少，不做压缩键）；命中返回与 lookup 同构的快照
        （target=""、status=remap），否则 None。
        """
        key = norm_key(header)
        if not key:
            return None
        with self._lock:
            data = self._load_unlocked()
            e = data["entries"].get(key)
            if e is not None and e["status"] == STATUS_REMAP:
                return {"key": key, "sample": e["sample"],
                        "target": "", "status": STATUS_REMAP}
            return None

    def flip_to_remap(self, key, user=""):
        """删除→保留翻转（P4）：条目转待重映射——目标清空、票 +1（keep），
        退出负建议判定路径（管线临时保留），交管理员经匹配修正重映射。"""
        key = norm_key(key)
        if not key:
            return False
        with self._lock:
            data = self._load_unlocked()
            entry = data["entries"].get(key)
            if entry is None or entry["status"] == STATUS_CONFIRMED:
                return False  # 已确认条目的目标变更走 confirm/force，不经 remap
            now = _now()
            entry["status"] = STATUS_REMAP
            entry["target"] = ""
            self._bump_vote_unlocked(entry, "keep", now)
            self._compact_idx = None
            self._save_unlocked()
            return True

    # ===================== 学习：回写建议 / 用户把关 =====================

    def record_suggestions(self, headers, mappings, valid_targets,
                           model="", prompt_ver=0, user="", limit=None):
        """把大模型对（未命中子集）表头的判定回写为 suggested；返回记录条数。

        口径：
        - 名单外的值 / 被 rejected 拦截的对：不记录（防幻觉 + 防回锅）；
        - 已有 confirmed 条目（含失配 orphan）：不覆盖——确认态只能被用户动作改变；
        - 已有 suggested 条目：target 以最新一次判定为准（模型的最新意见），hits 保留；
        - ``limit``：条目上限（管理员可配），只拦**自动新增**——已有条目的更新与用户
          confirm 不受限；调低不删除已有条目（不采用主动清理策略）。
        """
        cap = _coerce_limit(limit)
        recorded = 0
        with self._lock:
            data = self._load_unlocked()
            entries, rejected = data["entries"], data["rejected"]
            for h in (headers or []):
                key = norm_key(h)
                if not key:
                    continue
                val = str((mappings or {}).get(h) or "").strip()[:MAX_TARGET_LEN]
                if val:
                    vt = norm_key(val)
                    if vt not in (valid_targets or set()) or vt in (rejected.get(key) or {}):
                        continue
                entry = entries.get(key)
                if entry is not None and entry["status"] == STATUS_CONFIRMED:
                    continue
                if entry is None and len(entries) >= cap:
                    continue  # 到顶后不再自动扩张（confirmed 不受限，见 confirm）
                now = _now()
                if entry is None:
                    entries[key] = {
                        "key": key,
                        "sample": core.clean_text(h)[:MAX_KEY_LEN],
                        "target": val,
                        "status": STATUS_SUGGESTED,
                        "source": "llm",
                        "model": str(model or "")[:100],
                        "prompt_ver": prompt_ver,
                        "hits": 0,
                        "keep_votes": 0, "drop_votes": 0, "flips": 0, "streak": 0,
                        "last_vote_dir": "", "last_vote_at": "",
                        "created_by": str(user or "")[:100],
                        "created_at": now,
                        "confirmed_by": "", "confirmed_at": "",
                    }
                else:
                    entry["target"] = val
                    entry["sample"] = core.clean_text(h)[:MAX_KEY_LEN]
                    entry["model"] = str(model or "")[:100]
                    entry["prompt_ver"] = prompt_ver
                recorded += 1
            if recorded:
                self._compact_idx = None
                self._save_unlocked()
        return recorded

    def confirm(self, items, user="", valid_targets=None, force=False):
        """用户把关动作：确认/改指映射，条目升级为 confirmed；返回 {confirmed, conflicts}。

        - ``target=""`` 表示确认删除（负映射）；``target!=""`` 时必须仍在当前名单内；
        - 已有 confirmed 且目标不同 → 记入 conflicts 交前端弹窗处置，``force=True``
          才覆盖（替换全局映射）；被确认的对同时从 rejected 里摘除（用户改主意了）。
        """
        confirmed, conflicts = 0, []
        with self._lock:
            data = self._load_unlocked()
            entries, rejected = data["entries"], data["rejected"]
            for item in (items or []):
                if not isinstance(item, dict):
                    continue
                key = norm_key(item.get("key"))
                if not key:
                    continue
                target = "" if item.get("target") is None \
                    else str(item.get("target")).strip()[:MAX_TARGET_LEN]
                if target and norm_key(target) not in (valid_targets or set()):
                    continue  # 指向名单外目标会造成孤儿映射，防御性拒绝
                existing = entries.get(key)
                if (existing is not None and existing["status"] == STATUS_CONFIRMED
                        and norm_key(existing["target"]) != norm_key(target) and not force):
                    conflicts.append({"key": key, "target": target,
                                      "existing_target": existing["target"]})
                    continue
                now = _now()
                base = dict(existing) if existing else {}
                new_entry = {
                    "key": key,
                    "sample": str(item.get("sample") or base.get("sample") or key)[:MAX_KEY_LEN],
                    "target": target,
                    "status": STATUS_CONFIRMED,
                    "source": base.get("source") or "user",
                    "model": base.get("model") or "",
                    "prompt_ver": base.get("prompt_ver") or 0,
                    "hits": base.get("hits") or 0,
                    "keep_votes": base.get("keep_votes", 0),
                    "drop_votes": base.get("drop_votes", 0),
                    "flips": base.get("flips", 0),
                    "streak": base.get("streak", 0),
                    "last_vote_dir": base.get("last_vote_dir", ""),
                    "last_vote_at": base.get("last_vote_at", ""),
                    "passive_users": base.get("passive_users", {}),
                    "created_by": base.get("created_by") or str(user or "")[:100],
                    "created_at": base.get("created_at") or now,
                    "confirmed_by": str(user or "")[:100],
                    "confirmed_at": now,
                }
                # 显式确认/改指同时计票（审计口径）；方向按目标是否为空（负映射=drop 票）
                self._bump_vote_unlocked(new_entry, "drop" if not target else "keep", now)
                entries[key] = new_entry
                rej = rejected.get(key)
                if rej is not None:
                    rej.pop(norm_key(target), None)
                    if not rej:
                        rejected.pop(key, None)
                confirmed += 1
            if confirmed:
                self._compact_idx = None
                self._save_unlocked()
        return {"confirmed": confirmed, "conflicts": conflicts}

    def reject(self, key, target, user=""):
        """否决映射对：进 rejected 拦截表；同名同目标的既有条目删除（防止下次再命中）。"""
        key = norm_key(key)
        t = norm_key(target)
        if not key or not t:
            return False  # 否决「删除」没有意义：空目标不构成映射对
        with self._lock:
            data = self._load_unlocked()
            data["rejected"].setdefault(key, {})[t] = _now()
            entry = data["entries"].get(key)
            if entry is not None and norm_key(entry["target"]) == t:
                del data["entries"][key]
                self._compact_idx = None
            self._save_unlocked()
            return True

    # ===================== 管理与统计 =====================

    def delete(self, key):
        key = norm_key(key)
        if not key:
            return False
        with self._lock:
            data = self._load_unlocked()
            if key not in data["entries"]:
                return False
            del data["entries"][key]
            self._compact_idx = None
            self._save_unlocked()
            return True

    def clear(self):
        """清空全部映射与拦截对（管理员维护）；stats 是历史计数，保留。"""
        with self._lock:
            data = self._load_unlocked()
            data["entries"] = {}
            data["rejected"] = {}
            self._compact_idx = None
            self._save_unlocked()

    def stats(self):
        with self._lock:
            data = self._load_unlocked()
            return dict(data["stats"])

    def count(self):
        """当前条目总数（含失配 orphan；供管理面板展示「已用 N / 上限 M」）。"""
        with self._lock:
            data = self._load_unlocked()
            return len(data["entries"])

    def get_entry(self, key):
        """取单条映射的快照（拷贝）；不存在返回 None。"""
        key = norm_key(key)
        if not key:
            return None
        with self._lock:
            data = self._load_unlocked()
            e = data["entries"].get(key)
            return dict(e) if e else None

    def apply_recheck(self, pairs, valid_targets, model="", prompt_ver=0, user=""):
        """匹配修正写回（P3b/P4）：更新**建议级与待重映射**条目（sample 保留）。

        口径：名单外的值、被 rejected 拦截的对、confirmed 条目（确认态只能被用户动作
        改变）一律跳过；remap 条目提出有效目标后**转回 suggested**（进 diff 待采纳）；
        新旧目标相同计 unchanged 不落盘。pairs: [{"key", "new_target"}]。
        返回 {"updated", "unchanged", "skipped"}。
        """
        updated = unchanged = skipped = 0
        with self._lock:
            data = self._load_unlocked()
            entries, rejected = data["entries"], data["rejected"]
            for p in (pairs or []):
                key = norm_key(p.get("key"))
                if not key:
                    skipped += 1
                    continue
                entry = entries.get(key)
                if entry is None:
                    skipped += 1  # 运行中被删 → 跳过
                    continue
                if entry["status"] == STATUS_CONFIRMED:
                    # 已确认条目不动——**除非失配**（名单变更后 target 已不在名单，
                    # 决策 10 扩展）：LLM 建议的新目标有效则降级回建议级（进 diff
                    # 待管理员采纳），让失配的已确认映射也能经修正闭环恢复。
                    orphan = norm_key(entry["target"]) not in (valid_targets or set())
                    val = str(p.get("new_target") or "").strip()[:MAX_TARGET_LEN]
                    if not orphan or not val:
                        skipped += 1
                        continue
                    vt = norm_key(val)
                    if vt not in (valid_targets or set()) or vt in (rejected.get(key) or {}):
                        skipped += 1
                        continue
                    if vt == norm_key(entry["target"]):
                        skipped += 1  # 建议与原目标相同（名单未含该目标的场景外无意义）
                        continue
                    entry["target"] = val
                    entry["status"] = STATUS_SUGGESTED
                    entry["model"] = str(model or "")[:100]
                    entry["prompt_ver"] = prompt_ver
                    updated += 1
                    continue
                val = str(p.get("new_target") or "").strip()[:MAX_TARGET_LEN]
                if val:
                    vt = norm_key(val)
                    if vt not in (valid_targets or set()) or vt in (rejected.get(key) or {}):
                        skipped += 1
                        continue
                if entry["status"] == STATUS_REMAP:
                    if not val:
                        skipped += 1  # 重映射仍无目标 → 维持 remap 待复核
                        continue
                    entry["target"] = val
                    entry["status"] = STATUS_SUGGESTED  # 转回建议级，进 diff 待管理员采纳
                    entry["model"] = str(model or "")[:100]
                    entry["prompt_ver"] = prompt_ver
                    updated += 1
                    continue
                if norm_key(entry["target"]) == (norm_key(val) if val else ""):
                    unchanged += 1
                    continue
                entry["target"] = val
                entry["model"] = str(model or "")[:100]
                entry["prompt_ver"] = prompt_ver
                updated += 1
            if updated:
                self._compact_idx = None
                self._save_unlocked()
        return {"updated": updated, "unchanged": unchanged, "skipped": skipped}

    def bump_stats(self, **counters):
        """累加运行统计（未知键与零值忽略）；有变化才落盘。"""
        with self._lock:
            data = self._load_unlocked()
            changed = False
            for k, v in counters.items():
                if k not in VALID_STATS or not v:
                    continue
                try:
                    data["stats"][k] = data["stats"].get(k, 0) + int(v)
                    changed = True
                except Exception:
                    pass
            if changed:
                self._save_unlocked()

    def list_entries(self, valid_targets, status=None, q=None):
        """管理面板列表：status 支持 suggested/confirmed/orphan（伪状态）；q 模糊匹配。

        ``in_keep_list``：确认删除（target=""）条目的 key 恰在当前保留名单中——该记忆
        正压着名单里的同名字段（管线中 confirmed 档优先于 exact），管理面板据此挂
        「名单内」徽标（§8.1/决策 ⑦）。
        """
        rows = []
        with self._lock:
            data = self._load_unlocked()
            for key in sorted(data["entries"]):
                e = data["entries"][key]
                orphan = self._entry_valid(e, valid_targets) is False and bool(e["target"])
                if status == "orphan":
                    if not orphan:
                        continue
                elif status and e["status"] != status:
                    continue
                if q and q not in e["key"] and q not in (e["sample"] or "") \
                        and q not in (e["target"] or ""):
                    continue
                rows.append({**e, "orphan": orphan,
                             "in_keep_list": not e["target"] and key in (valid_targets or set())})
        return rows

    def export_records(self, valid_targets, status=STATUS_CONFIRMED):
        """导出黄金评测集记录（默认 confirmed；失配条目不能进评测集，一律剔除）。"""
        rows = []
        with self._lock:
            data = self._load_unlocked()
            for key in sorted(data["entries"]):
                e = data["entries"][key]
                if status not in (None, "all") and e["status"] != status:
                    continue
                if not self._entry_valid(e, valid_targets):
                    continue
                rows.append({"header": e["sample"] or e["key"],
                             "target": e["target"], "status": e["status"]})
        return rows
