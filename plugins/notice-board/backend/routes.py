"""公告板 —— JZToolsHub 后端插件路由。

功能：
- 管理员角色（role-admin）与超级管理员可发布、修改公告，发布时选择可见范围：
  以树状结构勾选若干 单位 / 部门 / 用户，公告仅对这些对象可见；
- 修改公告：管理员角色/超管可编辑其可见范围内的公告（标题/内容/可见范围），
  保留创建归属四字段与创建时间，更新 updated_at；删除仍限创建者或超管；
- 全体登录用户默认拥有本插件权限（tools.json 中 grant_all: true，
  由 admin 插件在会话信息中统一并入权限点），办案员仅可查看；
- 公告按可见性规则过滤后展示：命中任一目标即可见
  （单位=同单位；部门=同部门且同单位；用户=本人）；超级管理员可见全部。

数据持久化：backend/data/ 下一条公告一个 JSON 文件（一记录一文件），
落盘含 created_by / created_by_name / unit_id / department_id 四个归属字段，
一律取自服务端会话、禁止从请求体接收（规范 9.2 铁律一）。
targets 仅存服务端生成的标识（类型+ID），展示名由前端经组织树解析。

接口前缀：/api/notice-board
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime

from flask import jsonify, request

try:
    from jztools_admin.routes import get_session_user as _get_session_user
except Exception:  # admin 插件缺失时兜底（理论上不会发生）
    _get_session_user = None

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
API_PREFIX = "/api/notice-board"

# 可发布公告的角色：超级管理员 + 管理员角色（role-admin / 「管理员」）
PUBLISH_ROLE_IDS = {"role-admin"}
PUBLISH_ROLE_NAMES = {"管理员"}

# 全局文件写锁（公告读写共用）
_LOCK = threading.RLock()

ANNOUNCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_TITLE_LEN = 80
MAX_CONTENT_LEN = 5000
MAX_TARGETS = 50
TARGET_TYPES = ("unit", "department", "user")


# ===================== 会话与权限 =====================

def _viewer():
    """当前登录用户上下文；未登录返回 None。"""
    if _get_session_user is None:
        return None
    try:
        return _get_session_user()
    except Exception:
        return None


def _can_publish(user):
    """发布权限：超级管理员或管理员角色。"""
    if not user:
        return False
    if user.get("super_admin"):
        return True
    return (user.get("role_id") in PUBLISH_ROLE_IDS
            or user.get("role") in PUBLISH_ROLE_NAMES)


def _can_manage(user, ann):
    """删除等管理动作：仅创建者或超级管理员（规范 9.3 写约束）。"""
    if not user:
        return False
    if user.get("super_admin"):
        return True
    return (ann.get("created_by") or "") == user.get("username")


def _can_edit(user):
    """修改权限：超级管理员或管理员角色（与发布权限一致），可修改其可见范围内的公告。"""
    return _can_publish(user)


def _hit_target(user, t):
    """单个目标是否命中当前用户。部门级须同部门且同单位（规范 9.3 双重比对）。"""
    ttype = t.get("type")
    tid = t.get("id")
    if not ttype or not tid:
        return False
    if ttype == "unit":
        return user.get("unit_id") == tid
    if ttype == "department":
        return (user.get("department_id") == tid
                and (not t.get("uid") or user.get("unit_id") == t.get("uid")))
    if ttype == "user":
        return user.get("username") == tid
    return False


def _readable(user, ann):
    """可见性规则（规范 9.3）：命中任一目标即可见；超级管理员全部可见。"""
    if user is None:
        return False
    if user.get("super_admin"):
        return True
    targets = ann.get("targets") or []
    if not targets:
        return False
    return any(_hit_target(user, t) for t in targets)


# ===================== 存储（backend/data/，一公告一 JSON 文件） =====================

def _ann_path(aid):
    return os.path.join(DATA_DIR, f"{aid}.json")


def _is_announcement(rec):
    """按结构特征识别公告文件，过滤目录中可能混入的其他 JSON（规范 9.1）。"""
    return isinstance(rec, dict) and bool(rec.get("id")) and isinstance(rec.get("title"), str)


def load_announcement(aid):
    if not ANNOUNCE_ID_RE.match(aid or ""):
        return None
    path = _ann_path(aid)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        return rec if _is_announcement(rec) else None
    except Exception:
        return None


def save_announcement(ann):
    tmp = _ann_path(ann["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ann, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _ann_path(ann["id"]))


def list_announcements():
    out = []
    if not os.path.isdir(DATA_DIR):
        return out
    for name in os.listdir(DATA_DIR):
        if not name.endswith(".json"):
            continue
        rec = load_announcement(name[:-5])
        if rec:
            out.append(rec)
    out.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return out


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _validate_targets(body):
    """校验可见范围目标列表：类型合法、ID 非空、去重，返回 (targets, 错误或 None)。

    - unit:      {type, id}
    - department:{type, id, uid}（uid=所属单位，读取时做部门+单位双重比对）
    - user:      {type, id}（id=登录名）
    """
    raw = body.get("targets")
    if not isinstance(raw, list) or not raw:
        return None, "请至少选择一个可见范围（单位/部门/用户）"
    if len(raw) > MAX_TARGETS:
        return None, f"可见范围对象不能超过 {MAX_TARGETS} 个"
    seen = set()
    targets = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ttype = str(item.get("type") or "").strip()
        tid = str(item.get("id") or "").strip()
        if ttype not in TARGET_TYPES or not tid:
            return None, "可见范围对象格式不正确"
        key = f"{ttype}:{tid}"
        if key in seen:
            continue
        seen.add(key)
        entry = {"type": ttype, "id": tid}
        if ttype == "department":
            uid = str(item.get("uid") or "").strip()
            if not uid:
                return None, "部门目标缺少所属单位"
            entry["uid"] = uid
        targets.append(entry)
    if not targets:
        return None, "请至少选择一个可见范围（单位/部门/用户）"
    return targets, None


def _parse_ann_body():
    """解析并校验发布/修改请求体的 标题 / 内容 / 可见范围。

    返回 (title, content, targets, err)；err 非空时前三个值为空占位。
    """
    body = request.get_json(silent=True) or {}
    title = str(body.get("title") or "").strip()
    content = str(body.get("content") or "").strip()
    if not title:
        return "", "", None, "请输入公告标题"
    if not content:
        return "", "", None, "请输入公告内容"
    if len(title) > MAX_TITLE_LEN:
        return "", "", None, f"标题不能超过 {MAX_TITLE_LEN} 字"
    if len(content) > MAX_CONTENT_LEN:
        return "", "", None, f"内容不能超过 {MAX_CONTENT_LEN} 字"
    targets, err = _validate_targets(body)
    if err:
        return "", "", None, err
    return title, content, targets, None


def home_card(app=None):
    """首页卡片内容声明（读取方式一：插件主动声明，按当前登录用户动态生成）。

    主应用在首页 /api/tools 请求时于请求上下文内调用本钩子：插件根据
    当前用户的可见范围检索最新一条可读公告，并以「时间 + 公告标题」为首行、
    换行后展示公告内容的方式声明卡片 description；无可读公告或未登录时
    回退静态文案。

    前端 frontend/js/home-card.js 的客户端覆写已随本机制下线，卡片内容
    统一由本钩子经主程序下发。
    """
    card = {
        "name": "公告板",
        "icon": "📢",
        "accent": "#E8710A",
        "features": ["最新公告", "可见范围发布"],
    }
    user = _viewer()
    if user is None:
        card["description"] = "暂无最新公告。"
        return card
    items = [a for a in list_announcements() if _readable(user, a)]
    if not items:
        card["description"] = "暂无最新公告。"
        return card
    a = items[0]  # list_announcements 已按 created_at 倒序，首条即最新
    when = (a.get("created_at") or "").strip()[:16]  # 2026-08-26 09:30
    title = (a.get("title") or "").strip()
    content = (a.get("content") or "").strip()
    if len(title) > 40:
        title = title[:40] + "…"
    if len(content) > 60:
        content = content[:60] + "…"
    # 首行「时间 + 公告标题」，换行后展示公告内容（样式层 white-space: pre-line 渲染换行）
    first = f"{when} {title}".strip() if when else title
    card["description"] = f"{first}\n{content}" if content else first
    return card


def register(app) -> None:
    """插件入口：由 JZToolsHub 主应用在启动时调用。"""
    os.makedirs(DATA_DIR, exist_ok=True)

    @app.get(f"{API_PREFIX}/status")
    def nb_status():
        """依赖自检：纯标准库实现，恒可用（B-4）。"""
        return jsonify({"ok": True, "dependencies": {}})

    @app.get(f"{API_PREFIX}/config")
    def nb_config():
        """前端渲染开关：当前用户是否可发布公告。"""
        user = _viewer()
        return jsonify({"ok": True, "can_publish": _can_publish(user)})

    @app.get(f"{API_PREFIX}/announcements")
    def nb_list():
        """公告列表：先取会话（铁律二第一步），再按可见性过滤后返回。"""
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        items = []
        for ann in list_announcements():
            if not _readable(user, ann):
                continue  # 越权读不暴露存在性
            items.append({
                "id": ann.get("id"),
                "title": ann.get("title", ""),
                "content": ann.get("content", ""),
                "created_by": ann.get("created_by", ""),
                "created_by_name": ann.get("created_by_name", ""),
                "created_at": ann.get("created_at", ""),
                "updated_at": ann.get("updated_at") or "",
                "targets": ann.get("targets") or [],
                "manageable": _can_manage(user, ann),
                "editable": _can_edit(user),
            })
        return jsonify({"ok": True, "count": len(items), "items": items})

    @app.get(f"{API_PREFIX}/latest")
    def nb_latest():
        """最新一条可见公告（供站点首页公告条展示）：按可见性过滤后取第一条。"""
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        items = [a for a in list_announcements() if _readable(user, a)]
        if not items:
            return jsonify({"ok": True, "item": None})
        a = items[0]
        return jsonify({"ok": True, "item": {
            "id": a.get("id"),
            "title": a.get("title", ""),
            "content": a.get("content", ""),
            "created_by_name": a.get("created_by_name", ""),
            "created_at": a.get("created_at", ""),
        }})

    @app.post(f"{API_PREFIX}/announcements")
    def nb_publish():
        """发布公告：仅管理员角色/超管；归属四字段取自会话（铁律一）。"""
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_publish(user):
            return jsonify({"ok": False, "error": "仅管理员可发布公告"}), 403

        title, content, targets, err = _parse_ann_body()
        if err:
            return jsonify({"ok": False, "error": err}), 400

        ann = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "content": content,
            # 归属四字段：来源为会话而非请求体（规范 9.2 铁律一）
            "created_by": user.get("username", ""),
            "created_by_name": user.get("name", ""),
            "unit_id": user.get("unit_id", ""),
            "department_id": user.get("department_id", ""),
            "created_at": _now(),
            "updated_at": None,
            "targets": targets,
        }
        with _LOCK:
            save_announcement(ann)
        return jsonify({"ok": True, "id": ann["id"]})

    @app.put(f"{API_PREFIX}/announcements/<aid>")
    def nb_update(aid):
        """修改公告：仅管理员角色/超管（须可读该公告）。

        可改标题/内容/可见范围；保留创建归属四字段与 created_at，
        记录 updated_at。不存在或不可见一律 404（规范 8.4）。
        """
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            ann = load_announcement(aid)
            if not ann or not _readable(user, ann):
                return jsonify({"ok": False, "error": "公告不存在或已被删除"}), 404
            if not _can_edit(user):
                return jsonify({"ok": False, "error": "仅管理员可修改公告"}), 403

            title, content, targets, err = _parse_ann_body()
            if err:
                return jsonify({"ok": False, "error": err}), 400

            ann["title"] = title
            ann["content"] = content
            ann["targets"] = targets
            ann["updated_at"] = _now()
            save_announcement(ann)
        return jsonify({"ok": True, "id": ann["id"]})

    @app.delete(f"{API_PREFIX}/announcements/<aid>")
    def nb_delete(aid):
        """删除公告：存在但无权管理返回 403；不存在或不可见一律 404（规范 8.4）。"""
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            ann = load_announcement(aid)
            if not ann or not _readable(user, ann):
                return jsonify({"ok": False, "error": "公告不存在或已被删除"}), 404
            if not _can_manage(user, ann):
                return jsonify({"ok": False, "error": "仅发布者或超级管理员可删除"}), 403
            try:
                os.remove(_ann_path(aid))
            except OSError:
                return jsonify({"ok": False, "error": "删除失败，请稍后重试"}), 500
        return jsonify({"ok": True})
