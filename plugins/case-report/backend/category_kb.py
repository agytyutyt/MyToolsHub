"""战果录入 —— 用户类别知识库（本地持久化）。

用户在前端对「缴获物品类别」做出的修正会记录到 data/item_categories.json：
    {"map": {"电话卡": "电话卡", "平板": "电脑", ...}}

后续解析时优先复用这些「既有类别」；未命中的物品再交由大模型单独归类
（未配置大模型时回落本地规则）。修改持久化后即时生效，无需重启。
"""

import json
import os
import threading

import jztools_data
DATA_DIR = jztools_data.get_data_root_dir("plugins", "case-report", "data")
KB_FILE = jztools_data.get_data_root_file("plugins", "case-report", "item_categories.json")

_LOCK = threading.RLock()
_cache = None  # {"map": {物品名: 类别}}


def _load():
    global _cache
    if _cache is None:
        data = {}
        if os.path.exists(KB_FILE):
            try:
                with open(KB_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
        _cache = {"map": dict(data.get("map") or {})}
    return _cache


def _save():
    with _LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = KB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"map": _cache["map"]}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, KB_FILE)


def get_override(name):
    name = (name or "").strip()
    if not name:
        return None
    return _load()["map"].get(name) or None


def all_overrides():
    """返回 {物品名: 类别} 的副本。"""
    return dict(_load()["map"])


def set_override(name, category):
    """记录「物品名 → 类别」的学习结果；写入磁盘。"""
    name = (name or "").strip()
    category = (category or "").strip()
    if not name or not category:
        return False
    with _LOCK:
        kb = _load()
        kb["map"][name] = category
        _save()
        return True


def remove_override(name):
    name = (name or "").strip()
    with _LOCK:
        kb = _load()
        if name in kb["map"]:
            del kb["map"][name]
            _save()
            return True
        return False


def reset_cache():
    """测试辅助：清空内存缓存。"""
    global _cache
    _cache = None