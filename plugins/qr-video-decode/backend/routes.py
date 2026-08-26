"""QR 视频流解码 —— JZToolsHub 后端插件路由。

解码流程（对应 QR-transfer 协议）：
1. 前端在上传视频后用 jsQR 逐帧扫码，得到每个 QR 帧的内容；
2. 每帧内容为 base64 文本：前 4B=帧序号、4-8B=总帧数、8-12B=纠错参数(k,m)、
   12B 之后=该帧数据分块(base64)；
3. 前端把收集到的 {帧序号 -> 数据分块 base64} 送到本后端；
4. 本后端用 zfec 做前向纠错解码，重组出完整分片，再按长度前缀恢复原始文件。

协议细节与 https://github.com/Zhen-Ni/QR-transfer 的 qrtransfer.py 保持一致。

并发设计（适配大视频）：
- POST /reassemble 仅接收数据并立即返回 task_id；
- 实际 zfec 重组放到后台线程执行，按「纠错块」粒度更新进度；
- 前端通过 GET /reassemble/<task_id> 轮询进度（含预计剩余时间所需字段）。
"""

import base64
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import jsonify, request

try:
    from jztools_admin.routes import get_session_user as _get_session_user
except Exception:  # admin 插件缺失时兜底（理论上不会发生）
    _get_session_user = None

API_PREFIX = "/api/qr-video-decode"

SIZE_PREFIX = 4       # 帧头序号/总帧数 占 4 个 base64 字符（=3 字节）
SIZE_DATASIZE = 4     # 文件长度前缀占 4 字节
BYTE_ORDER = "big"

try:
    import zfec
    ZFEC_AVAILABLE = True
except Exception:  # zfec 未安装时给出明确提示
    zfec = None
    ZFEC_AVAILABLE = False

# 后台重组线程池与任务状态（仿 character-graph 并发设计）
REASSEMBLE_WORKERS = 2
_executor = ThreadPoolExecutor(max_workers=REASSEMBLE_WORKERS)
TASKS = {}
TASKS_LOCK = threading.Lock()
TASK_TTL_SECONDS = 30 * 60


def check_zfec_or_raise():
    if not ZFEC_AVAILABLE:
        raise RuntimeError(
            "后端缺少 zfec 依赖，无法做前向纠错重组。\n"
            "请在服务器执行：pip install zfec"
        )


def decode_index(b64: str) -> int:
    """将 4 字符 base64（=3 字节无符号整数）还原为帧序号/总帧数。"""
    return int.from_bytes(base64.b64decode(b64.encode("ascii")), BYTE_ORDER)


def decode_fecinfo(b64: str):
    n = decode_index(b64)
    return (n >> 8) & 255, n & 255


def set_task(task_id: str, **kwargs) -> None:
    with TASKS_LOCK:
        TASKS[task_id] = {**TASKS.get(task_id, {}), **kwargs}


def get_task(task_id: str):
    with TASKS_LOCK:
        return TASKS.get(task_id)


def cleanup_tasks() -> None:
    now = time.time()
    with TASKS_LOCK:
        expired = [
            tid for tid, t in TASKS.items()
            if t.get("created_at", 0) < now - TASK_TTL_SECONDS
        ]
        for tid in expired:
            TASKS.pop(tid, None)


def _viewer():
    """当前登录用户上下文；未登录返回 None。"""
    if _get_session_user is None:
        return None
    try:
        return _get_session_user()
    except Exception:
        return None


def _task_owned_by(task, user):
    """任务归属校验：非创建者（且非超管）视为任务不存在（404，纵深防御）。

    与 trajectory-convert 插件的同名机制保持一致。
    """
    owner = task.get("created_by")
    if not owner:
        return True  # 兼容升级前创建的旧任务
    if user is None:
        return False
    if user.get("super_admin"):
        return True
    return owner == user.get("username")


def validate_payload(payload):
    """校验前端提交的字段，返回 (nframes, k, m, chunks) 或抛 ValueError。"""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    nframes = payload.get("nframes")
    k = payload.get("k")
    m = payload.get("m")
    chunks = payload.get("chunks")
    if not isinstance(nframes, int) or nframes <= 0 or nframes > 100000:
        raise ValueError("nframes 非法")
    if not isinstance(k, int) or not isinstance(m, int) or k <= 0 or m > 256 or k > m:
        raise ValueError("zfec 参数 k/m 非法")
    if not isinstance(chunks, dict) or not chunks:
        raise ValueError("缺少分块数据")
    for idx, val in chunks.items():
        try:
            int(idx)
        except (ValueError, TypeError):
            raise ValueError("分块序号非法")
        if not isinstance(val, str) or not val:
            raise ValueError("分块数据格式非法")
    return nframes, k, m, chunks


def reassemble(nframes: int, k: int, m: int, chunks: dict,
               task_id: str = None) -> bytes:
    """按 QR-transfer 协议重组，返回原始文件字节。

    若给出 task_id，则按纠错块粒度更新任务进度。
    """
    check_zfec_or_raise()

    data_list = [None] * nframes
    for idx, val in chunks.items():
        i = int(idx)
        if 0 <= i < nframes:
            data_list[i] = base64.b64decode(val.encode("ascii"))

    nblocks = nframes // m
    if task_id:
        set_task(task_id, total=nblocks, done=0, progress=0.0)

    decoder = zfec.Decoder(k, m)
    recovered_blocks = []
    for i in range(nblocks):
        packet = [data_list[i + j * nblocks] for j in range(m)]
        blocks, blocknums = [], []
        for j, d in enumerate(packet):
            if d is not None:
                blocks.append(d)
                blocknums.append(j)
        if len(blocks) < k:
            raise RuntimeError(
                f"数据不足：第 {i + 1}/{nblocks} 组有效分块 {len(blocks)} < k={k}，"
                "无法纠错恢复（建议拍摄时正对屏幕、保持稳定，或启用更高 FEC）"
            )
        recovered_blocks.append(decoder.decode(blocks[:k], blocknums[:k]))
        if task_id and (i % 8 == 0 or i == nblocks - 1):
            set_task(task_id, done=i + 1,
                     progress=round((i + 1) / nblocks, 4))

    recovered = []
    for j in range(k):
        for i in range(nblocks):
            recovered.append(recovered_blocks[i][j])
    joined = b"".join(recovered)

    data_size = int.from_bytes(joined[:SIZE_DATASIZE], BYTE_ORDER)
    return joined[SIZE_DATASIZE:SIZE_DATASIZE + data_size]


def _run_reassemble(task_id: str, payload: dict) -> None:
    """后台线程：zfec 重组，进度写入任务状态。"""
    try:
        set_task(task_id, status="running", created_at=time.time())
        nframes, k, m, chunks = validate_payload(payload)
        raw = reassemble(nframes, k, m, chunks, task_id=task_id)
        set_task(task_id, status="done", progress=1.0,
                 size=len(raw),
                 data_b64=base64.b64encode(raw).decode("ascii"))
    except ValueError as e:
        set_task(task_id, status="error", detail=str(e))
    except RuntimeError as e:
        set_task(task_id, status="error", detail=str(e))
    except Exception as e:
        # SEC-5：非预期异常不向前端透出内部细节（堆栈/路径等）
        set_task(task_id, status="error",
                 detail=f"重组失败（{type(e).__name__}）")


def register(app) -> None:
    """插件入口：由 JZToolsHub 主应用在启动时调用。"""

    @app.post(f"{API_PREFIX}/reassemble")
    def qrvd_reassemble():
        """接收前端解码出的分块数据，异步 zfec 重组，立即返回 task_id。"""
        payload = request.get_json(silent=True)
        try:
            validate_payload(payload)
        except ValueError as e:
            return jsonify({"ok": False, "detail": str(e)}), 400

        task_id = uuid.uuid4().hex[:12]
        cleanup_tasks()
        set_task(task_id, status="pending", progress=0.0, created_at=time.time(),
                 created_by=(_viewer() or {}).get("username", ""))
        _executor.submit(_run_reassemble, task_id, payload)
        return jsonify({"ok": True, "task_id": task_id})

    @app.get(f"{API_PREFIX}/reassemble/<task_id>")
    def qrvd_reassemble_status(task_id):
        """轮询重组任务状态：pending / running / done / error（仅创建者与超管可见）。"""
        task = get_task(task_id)
        if not task or not _task_owned_by(task, _viewer()):
            return jsonify({"ok": False, "detail": "任务不存在或已过期"}), 404
        payload = {
            "ok": True,
            "status": task.get("status"),
            "progress": task.get("progress", 0.0),
            "done": task.get("done", 0),
            "total": task.get("total", 0),
        }
        if task.get("status") == "done":
            payload["size"] = task.get("size", 0)
            payload["data_b64"] = task.get("data_b64", "")
        if task.get("status") == "error":
            payload["detail"] = task.get("detail", "纠错重组失败")
        return jsonify(payload)

    @app.get(f"{API_PREFIX}/status")
    def qrvd_status():
        return jsonify({"ok": True, "zfec": ZFEC_AVAILABLE})