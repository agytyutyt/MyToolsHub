# -*- coding: utf-8 -*-
"""插件商店 —— 后端路由（读面：catalog 投影 / 包分发 / 下载统计）。

数据契约（设计文档 §3.4 / §4.1，本文件是消费方实现）：
    数据根\\store\\catalog.json   admin 写 → 本插件只读（防御性校验，坏行跳过不炸页面）
    数据根\\store\\packages\\      admin 写 → 本插件只读 + 分发（流式回包）
    数据根\\store\\stats.jsonl    本插件唯一可写（追加式下载事件）

纪律：
- 对 catalog.json / packages\\ 无任何写代码（C6 验收：grep 无写点）；
- 客户端 flat 投影 = 每 id「最新已上架版本」，字段与共享盘 index.json 同名同义
  + runtime/requires 扩展（客户端契约同构，接缝①）；
- 下载按版本号寻包，仅 published 状态可下载；统计口径 = 仅下载事件（拍板 Q2）。
"""

import json
import os
import re
import threading
from datetime import datetime

from flask import jsonify, request, send_file

import jz_api
import jztools_data

_get_session_user = jz_api.get_session_user

# ---- 数据契约路径（数据根\\store\\，框架级共享区；_DATA_SUBDIRS 已含 store，换根随迁） ----
STORE_DIR = jztools_data.get_data_root_dir("store")
CATALOG_PATH = os.path.join(STORE_DIR, "catalog.json")
PACKAGES_DIR = os.path.join(STORE_DIR, "packages")
STATS_PATH = os.path.join(STORE_DIR, "stats.jsonl")

API_PREFIX = "/plugin/store/api"

CATALOG_SCHEMA = 2
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
STATS_LOCK = threading.Lock()

VALID_RUNTIME = ("local", "server", "dep-slot")


# ===================== catalog 读取（防御性） =====================

def load_catalog():
    """读 catalog.json；结构不符 / 条目损坏一律跳过（坏行不炸页面，读侧防御）。"""
    try:
        with open(CATALOG_PATH, "r", encoding="utf-8-sig") as f:
            obj = json.load(f)
    except Exception:
        return {"schema": CATALOG_SCHEMA, "generated_at": "", "packages": []}
    if not isinstance(obj, dict) or obj.get("schema") != CATALOG_SCHEMA:
        return {"schema": CATALOG_SCHEMA, "generated_at": "", "packages": []}
    pkgs = []
    for raw in obj.get("packages") or []:
        if not isinstance(raw, dict) or not ID_RE.match(str(raw.get("id") or "")):
            continue
        versions = [v for v in (raw.get("versions") or [])
                    if isinstance(v, dict) and VERSION_RE.match(str(v.get("version") or ""))]
        entry = dict(raw)
        entry["versions"] = versions
        pkgs.append(entry)
    return {"schema": CATALOG_SCHEMA, "generated_at": str(obj.get("generated_at") or ""),
            "packages": pkgs}


def _ver_key(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v.get("version") or ""))) or (0,)


def latest_published(entry):
    """条目的「最新已上架版本」（可见性规则 §3.7：无则条目整体隐藏）。"""
    pub = [v for v in entry.get("versions") or [] if v.get("status") == "published"]
    return max(pub, key=_ver_key) if pub else None


def find_version(entry, version):
    for v in entry.get("versions") or []:
        if str(v.get("version")) == str(version) and v.get("status") == "published":
            return v
    return None


def flat_item(entry, ver):
    """catalog 条目 → 客户端 flat 条目（与共享盘 index.json 同名同义 + 扩展字段）。"""
    item = {
        "id": entry.get("id"),
        "name": entry.get("name") or entry.get("id"),
        "description": entry.get("description", ""),
        "icon": entry.get("icon", "🧩"),
        "runtime": entry.get("runtime", "local"),
        "deps_mode": entry.get("deps_mode", "shared"),
        "requires": entry.get("requires") or {},
        "version": ver.get("version"),
        "file": ver.get("file", ""),
        "sha256": ver.get("sha256", ""),
        "size": ver.get("size", 0),
        "api_version": ver.get("api_version", 1),
        "min_app_version": ver.get("min_app_version", ""),
        "requires_restart": bool(ver.get("requires_restart")),
        "changelog": ver.get("changelog", ""),
        "published_at": ver.get("published_at", ""),
    }
    # server 条目双包模型（需求⑧）：客户端「安装」取前端包工件
    for key in ("frontend_file", "frontend_sha256", "frontend_size"):
        if ver.get(key) is not None:
            item[key] = ver.get(key)
    # dep-slot 条目：provides 供客户端预检把缺依赖映射到可装依赖包（O1 会合点）
    if entry.get("provides"):
        item["provides"] = list(entry["provides"])
    return item


# ===================== 统计（唯一写点） =====================

def append_stat(event):
    """追加一条下载事件 {ts,user,id,version,via}；坏写不抛出（统计失败不影响下载）。"""
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        with STATS_LOCK:
            with open(STATS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _stats_rows():
    rows = []
    try:
        with open(STATS_PATH, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except Exception:
        pass
    return rows


# ===================== 路由 =====================

def register(app) -> None:
    os.makedirs(PACKAGES_DIR, exist_ok=True)

    def _viewer():
        try:
            return _get_session_user()
        except Exception:
            return None

    @app.get(f"{API_PREFIX}/catalog")
    def st_catalog():
        """flat 投影（每 id 最新 published 版）；?all=1 附版本历史（详情页用）。"""
        user = _viewer()
        if user is None:
            return jsonify({"error": "未登录或登录已过期"}), 401
        catalog = load_catalog()
        packages = []
        for entry in catalog["packages"]:
            ver = latest_published(entry)
            if ver is None:
                continue
            item = flat_item(entry, ver)
            if str(request.args.get("all") or "") in ("1", "true"):
                item["versions"] = [
                    {"version": v.get("version"), "changelog": v.get("changelog", ""),
                     "published_at": v.get("published_at", ""),
                     "status": v.get("status")} if v.get("status") == "published" else
                    {"version": v.get("version"), "status": v.get("status")}
                    for v in entry.get("versions") or []
                ]
            packages.append(item)
        return jsonify({"ok": True, "schema": CATALOG_SCHEMA,
                        "generated_at": catalog["generated_at"], "packages": packages})

    @app.get(f"{API_PREFIX}/download/<pid>/<version>")
    def st_download(pid, version):
        """流式回包文件；仅 published；写下载事件（拍板：统计仅记下载）。

        server 条目为双包（需求⑧）：缺省回后端包（file），?artifact=frontend 回前端包
        （客户端「安装」server 条目取前端工件）。
        """
        user = _viewer()
        if user is None:
            return jsonify({"error": "未登录或登录已过期"}), 401
        if not ID_RE.match(pid) or not VERSION_RE.match(version):
            return jsonify({"error": "id 或版本号非法"}), 400
        entry = next((e for e in load_catalog()["packages"] if e.get("id") == pid), None)
        ver = find_version(entry, version) if entry else None
        if ver is None:
            return jsonify({"error": "条目不存在或当前版本未上架"}), 404
        artifact = request.args.get("artifact") or "backend"
        fname = ""
        if artifact == "frontend":
            fname = os.path.basename(str(ver.get("frontend_file") or ""))
            if not fname:
                return jsonify({"error": "该版本没有前端包工件"}), 404
        else:
            fname = os.path.basename(str(ver.get("file") or ""))
        path = os.path.join(PACKAGES_DIR, fname) if fname else ""
        if not fname or not os.path.isfile(path):
            return jsonify({"error": "包文件缺失（请联系管理员重新上架）"}), 404
        via = request.args.get("via") or "browser"
        append_stat({"ts": datetime.now().isoformat(timespec="seconds"),
                     "user": user.get("username", ""),
                     "id": pid, "version": version,
                     "via": via if via in ("client", "browser") else "browser"})
        return send_file(path, as_attachment=True, download_name=fname)

    @app.get(f"{API_PREFIX}/stats")
    def st_stats():
        """本插件下载事件聚合（商店管理页统计页签也读这里，admin 侧只读）。"""
        user = _viewer()
        if user is None:
            return jsonify({"error": "未登录或登录已过期"}), 401
        return jsonify({"ok": True, "rows": _stats_rows()})
