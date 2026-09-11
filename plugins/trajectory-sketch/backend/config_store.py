# -*- coding: utf-8 -*-
"""插件配置读写（数据根目录 ``plugins/trajectory-sketch/config.json``）。

本插件**不含任何密钥**：大模型配置由「过滤器」插件持有（本插件只是按需调用其
``/apply`` 接口，mode=llm 时由过滤器插件使用它自己的 LLM 配置）。因此 config.json
可以随目录分发，无需 gitignore，也不需要"敏感配置外置"的额外说明。

配置分四节（默认值来自 :mod:`engine.params`，保证"代码默认值"与"配置文件"同源）：

- ``filter``：保留字段名单（需求 4 的落点）、后处理规则、默认过滤模式；
- ``schema``：必需 / 可选规范列与列映射（别名）；
- ``analysis``：算法版本与全部阈值（透传给引擎）；
- ``report``：抬头、落款、摘要与分节开关。
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, List, Tuple

try:
    import jztools_data
except Exception:  # pragma: no cover - 供离线单测兜底
    jztools_data = None

from .engine import params as engine_params

# --------------------------------------------------------------------------
# 默认配置（与 engine/params.py 同源：引擎节直接取自那里，避免两处维护）
# --------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "filter": {
        "mode_default": "hard",              # hard | llm（页面开关初始值）
        "keep_columns": ["BEGINTIME", "USERNUM", "LAI", "CI", "ADDRESS",
                         "LONGITUDE", "LATITUDE"],
        "post_rules": [],
        "use_filter_plugin_rules": False,
    },
    "schema": copy.deepcopy(engine_params.DEFAULT_SCHEMA),
    "analysis": {
        "algo": engine_params.DEFAULT_ALGO,
        "clean": copy.deepcopy(engine_params.DEFAULT_CLEAN),
        "cluster": copy.deepcopy(engine_params.DEFAULT_CLUSTER),
        "staypoint": copy.deepcopy(engine_params.DEFAULT_STAYPOINT),
        "trip": copy.deepcopy(engine_params.DEFAULT_TRIP),
    },
    "report": copy.deepcopy(engine_params.DEFAULT_REPORT),
}

MAX_KEEP_COLUMNS = 60
MAX_RULE_LEN = 200


def config_path() -> str:
    """配置文件绝对路径（数据根目录下）。"""
    if jztools_data is not None:
        return jztools_data.get_data_root_file("plugins", "trajectory-sketch", "config.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _deep_merge(defaults: Dict[str, Any], given: Any) -> Dict[str, Any]:
    """只覆盖 defaults 已知键，未知键忽略；字典递归、列表整体替换。"""
    out: Dict[str, Any] = {}
    src = given if isinstance(given, dict) else {}
    for k, dv in defaults.items():
        if k in src and src[k] is not None:
            gv = src[k]
            if isinstance(dv, dict):
                out[k] = _deep_merge(dv, gv)
            elif isinstance(dv, list):
                out[k] = gv if isinstance(gv, list) else copy.deepcopy(dv)
            else:
                out[k] = gv
        else:
            out[k] = copy.deepcopy(dv)
    return out


def load_config() -> Dict[str, Any]:
    """读取配置并与默认值合并（缺键补默认值；文件损坏退回全默认）。"""
    raw: Dict[str, Any] = {}
    path = config_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                raw = data
        except Exception:
            raw = {}
    cfg = _deep_merge(DEFAULT_CONFIG, raw)
    cfg["filter"] = _sanitize_filter(cfg.get("filter"))
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    """原子写盘（tmp + os.replace），避免半截文件。"""
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    clean = _deep_merge(DEFAULT_CONFIG, cfg)
    clean["filter"] = _sanitize_filter(clean.get("filter"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# filter 节规整
# --------------------------------------------------------------------------

def _sanitize_filter(flt: Any) -> Dict[str, Any]:
    d = DEFAULT_CONFIG["filter"]
    src = flt if isinstance(flt, dict) else {}
    mode = str(src.get("mode_default") or d["mode_default"]).strip().lower()
    if mode not in ("hard", "llm"):
        mode = "hard"
    keep_raw = src.get("keep_columns")
    keep: List[str] = []
    if isinstance(keep_raw, list):
        for k in keep_raw:
            s = str(k).strip()
            if s and s not in keep:
                keep.append(s[:100])
    return {
        "mode_default": mode,
        "keep_columns": keep[:MAX_KEEP_COLUMNS],
        "post_rules": sanitize_rules(src.get("post_rules")),
        "use_filter_plugin_rules": bool(src.get("use_filter_plugin_rules")),
    }


def sanitize_rules(rules: Any) -> List[Dict[str, Any]]:
    """规整后处理规则（与过滤器插件同结构，防止脏数据进引擎前污染表格）。"""
    out: List[Dict[str, Any]] = []
    if isinstance(rules, list):
        for r in rules:
            if not isinstance(r, dict):
                continue
            pattern = str(r.get("pattern") or "").strip()
            if not pattern:
                continue
            out.append({
                "pattern": pattern[:MAX_RULE_LEN],
                "replacement": "" if r.get("replacement") is None
                else str(r.get("replacement"))[:MAX_RULE_LEN],
                "is_regex": bool(r.get("is_regex")),
                "enabled": r.get("enabled") is not False,
            })
    return out


# --------------------------------------------------------------------------
# 保留字段 → 过滤器插件可用的别名全集（设计文档 §4.3）
# --------------------------------------------------------------------------

def expand_keep_columns(cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """把配置的保留字段展开为**别名全集**，供 filter 插件做字面精确匹配。

    规则：配置项命中某规范列的别名表 → 取该规范列的**全部别名**；否则保留原文
    （允许管理员直接写自定义列名）。这样配置里既可直接写 ``BEGINTIME``，
    也可写中文简称「时间」「经度」，同一份配置可适配多种来源的轨迹表。

    返回 ``(columns, notes)``；``notes`` 为展开说明（前端可展示，便于管理员核对）。
    """
    cmap = cfg.get("schema", {}).get("column_map", {}) or {}
    fold: Dict[str, str] = {}
    for canon, aliases in cmap.items():
        for a in (aliases or []):
            key = str(a).strip().casefold()
            if key:
                fold.setdefault(key, canon)

    cols: List[str] = []
    notes: List[str] = []
    for item in cfg.get("filter", {}).get("keep_columns", []) or []:
        raw = str(item).strip()
        if not raw:
            continue
        canon = fold.get(raw.casefold())
        if canon:
            aliases = [str(a).strip() for a in (cmap.get(canon) or []) if str(a).strip()]
            if canon not in aliases:
                aliases.insert(0, canon)
            for a in aliases:
                if a not in cols:
                    cols.append(a)
            notes.append("%s → %s 的全部别名（%d 个）" % (raw, canon, len(aliases)))
        else:
            if raw not in cols:
                cols.append(raw)
            notes.append("%s → 原样保留（未命中列映射）" % raw)
    return cols, notes


def column_display_name(cfg: Dict[str, Any], canonical: str) -> str:
    """规范列的中文展示名（前端自检卡片用）。"""
    names = {"BEGINTIME": "上报时间", "USERNUM": "号码", "SPCODE": "运营商/局码",
             "LAI": "位置区(LAI)", "CI": "小区(CI)", "ADDRESS": "地址",
             "LONGITUDE": "经度", "LATITUDE": "纬度"}
    return names.get(canonical, canonical)


def engine_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """抽出引擎所需的参数（schema / analysis / report），供 ``engine.analyze_rows`` 使用。"""
    return {
        "schema": cfg.get("schema"),
        "analysis": cfg.get("analysis"),
        "report": cfg.get("report"),
    }


def public_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """回传给前端的配置（当前无密钥字段，直接返回；保留此函数以便未来扩展掩码）。"""
    return cfg
