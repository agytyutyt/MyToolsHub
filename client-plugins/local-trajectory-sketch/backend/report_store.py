# -*- coding: utf-8 -*-
"""上传暂存与任务表（含 TTL 清理与归属校验）。

目录：数据根目录 ``plugins/trajectory-sketch/.task_cache/``（TTL 30 分钟自动清理）

两张内存表（均加锁）：

- ``STAGES``：上传暂存 ``{staged_id: {...}}``——「上传→自检」与「生成报告」是两步，
  原始文件必须暂存下来，用户在自检后切换过滤开关时不必重新上传；
- ``TASKS``：分析任务 ``{task_id: {...}}``——异步任务三件套（规范 8.5）的状态载体。

归属（规范 9.2 铁律一）：两表创建时都从**会话**取 ``created_by``（不接受请求体），
轮询与下载按「创建者或超管」校验——非创建者且非超管一律按"不存在"处理（404，
不暴露资源存在性）。
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any, Dict, Optional

try:
    import jztools_data
except Exception:  # pragma: no cover
    jztools_data = None

PLUGIN_ID = "trajectory-sketch"
CACHE_TTL_SECONDS = 30 * 60
MAX_UPLOAD_BYTES = 20 * 1024 * 1024            # 20MB（SEC-3）

STAGES: Dict[str, Dict[str, Any]] = {}
TASKS: Dict[str, Dict[str, Any]] = {}
LOCK = threading.RLock()


def cache_dir() -> str:
    if jztools_data is not None:
        return jztools_data.get_data_root_dir("plugins", PLUGIN_ID, ".task_cache")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".task_cache")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _touch(path: str) -> None:
    try:
        os.utime(path, None)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 上传暂存
# --------------------------------------------------------------------------

def save_upload(blob: bytes, ext: str, original_name: str, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """落盘原始上传件（文件名用服务端生成的 ID，不沿用原始文件名——SEC-3）。"""
    os.makedirs(cache_dir(), exist_ok=True)
    sid = new_id()
    path = os.path.join(cache_dir(), "%s_upload.%s" % (sid, ext))
    with open(path, "wb") as f:
        f.write(blob)
    stage = {
        "staged_id": sid,
        "path": path,
        "ext": ext,
        "original_name": original_name,
        "size": len(blob),
        "created_at": time.time(),
        "created_by": (user or {}).get("username") or "",
        "headers": [],
        "row_count": 0,
    }
    with LOCK:
        STAGES[sid] = stage
    return stage


def get_stage(staged_id: str) -> Optional[Dict[str, Any]]:
    with LOCK:
        return STAGES.get(str(staged_id or "").strip())


def update_stage(staged_id: str, **kwargs) -> None:
    with LOCK:
        stage = STAGES.get(staged_id)
        if stage is not None:
            stage.update(kwargs)


def drop_stage(staged_id: str) -> None:
    with LOCK:
        stage = STAGES.pop(staged_id, None)
    if stage:
        _remove_file(stage.get("path"))


# --------------------------------------------------------------------------
# 任务表
# --------------------------------------------------------------------------

def create_task(staged_id: str, user: Optional[Dict[str, Any]],
                mode: str = "hard") -> str:
    tid = new_id()
    with LOCK:
        TASKS[tid] = {
            "task_id": tid,
            "staged_id": staged_id,
            "mode": mode,
            "status": "pending",
            "created_at": time.time(),
            "created_by": (user or {}).get("username") or "",
        }
    return tid


def set_task(task_id: str, **kwargs) -> None:
    with LOCK:
        task = TASKS.get(task_id)
        if task is None:
            TASKS[task_id] = dict(kwargs)
        else:
            task.update(kwargs)


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with LOCK:
        return TASKS.get(str(task_id or "").strip())


def owned_by(record: Optional[Dict[str, Any]], user: Optional[Dict[str, Any]]) -> bool:
    """归属校验：创建者本人或超管可见；两者都不是 → 视为不存在（404）。"""
    if not record:
        return False
    owner = record.get("created_by") or ""
    if user is None:
        return False
    if user.get("super_admin"):
        return True
    if not owner:
        return False
    return owner == user.get("username")


# --------------------------------------------------------------------------
# 清理（内存表 + 落盘文件双清理）
# --------------------------------------------------------------------------

def cleanup() -> None:
    """按 TTL 清理两张内存表与过期文件（每次提交任务时调用，无独立线程）。"""
    now = time.time()
    with LOCK:
        for table in (STAGES, TASKS):
            for key in [k for k, v in table.items()
                        if float(v.get("created_at", 0)) < now - CACHE_TTL_SECONDS]:
                table.pop(key, None)
    _clean_files()


def _clean_files() -> None:
    directory = cache_dir()
    if not os.path.isdir(directory):
        return
    now = time.time()
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) > CACHE_TTL_SECONDS:
                os.remove(path)
        except Exception:
            pass


def _remove_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def keep_alive(path: str) -> None:
    """把产物文件的 mtime 推到当前时刻，避免长轮询期间被清理。"""
    _touch(path)
