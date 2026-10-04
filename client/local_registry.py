# -*- coding: utf-8 -*-
"""本地插件注册表（批次 3 T16）：扫描客户端本地插件目录，产出与 /api/tools 同构的条目。"""
import json
import os


def scan_local_plugins(local_plugins_dir, log=None):
    """扫描 local-plugins/，返回合规 manifest 列表（目录名必须 local- 前缀、runtime=local）。"""
    out = []
    if not os.path.isdir(local_plugins_dir):
        return out
    for name in sorted(os.listdir(local_plugins_dir)):
        mp = os.path.join(local_plugins_dir, name, "manifest.json")
        if not os.path.isfile(mp):
            continue
        try:
            with open(mp, "r", encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            if log:
                log("本地插件 %s manifest 不可解析，跳过" % name)
            continue
        if m.get("runtime") != "local" or not name.startswith("local-"):
            if log:
                log("本地插件目录 %s 命名/类别不合规，跳过（须 local- 前缀 + runtime=local）" % name)
            continue
        m = dict(m)
        m["_dir"] = name
        m["_dir_abs"] = os.path.join(local_plugins_dir, name)
        out.append(m)
    return out


def find(manifests, tool_id):
    for m in manifests:
        if m.get("id") == tool_id:
            return m
    return None


def tool_item(m, order):
    """manifest → /api/tools 条目（字段与主包 get_aggregated_tools 同构，方案 §6.4）。"""
    entry = m.get("entry") or {}
    return {
        "id": m.get("id") or m.get("_dir", ""),
        "name": m.get("name") or m.get("_dir", ""),
        "description": m.get("description", ""),
        "icon": m.get("icon", "🧩"),
        "icon_file": None,
        "accent": m.get("accent", "#4285F4"),
        "entry": entry.get("frontend") or "index.html",
        "features": m.get("features", []),
        "category": "本地插件",
        "category_id": "local",
        "order": order,
        "health": "ok",
        "health_degraded": [],
    }


def tool_items(manifests, start_order=1000):
    """本地条目统一排在远程条目之后（order 从 start_order 起）。"""
    return [tool_item(m, start_order + i) for i, m in enumerate(manifests)]


LOCAL_CATEGORY = {"id": "local", "name": "本地插件", "enabled": True}
