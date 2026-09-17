# -*- coding: utf-8 -*-
"""二维码流「按需生成 mp4」专项测试。

覆盖：
  1. video 模式默认只落帧 PNG（不产 mp4），帧接口可用、下载视频返回 409 引导；
  2. POST /encode/<id>/video 按需生成 mp4（幂等、状态回显 video_status/progress）；
  3. 生成后的 mp4 走 POST /decode（type=video）能完整还原原始数据；
  4. 直出视频（video_file=1）与按需生成两条路径的帧数口径一致；
  5. 帧序列缺失时的失败路径（不静默产出坏视频）。

运行：python test_video_ondemand.py
依赖：qrcode / zfec / opencv-python / numpy / zxing-cpp（venv 齐备）。
"""
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, REPO)

TMP_ROOT = tempfile.mkdtemp(prefix="it_vod_")
os.environ["JZTOOLS_DATA_ROOT"] = TMP_ROOT  # 隔离真实数据根（routes 导入前设置）

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "it_routes_vod", os.path.join(HERE, "routes.py"))
it = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(it)

from flask import Flask  # noqa: E402

FAILS = []
PASSES = [0]
API = "/api/info-transfer"


def check(name, ok, detail=""):
    if ok:
        PASSES[0] += 1
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}  {detail}")


def make_app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    it.register(app)
    return app


def wait_task(client, task_id, timeout=180):
    """轮询任务到终态，返回最终 payload。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = client.get(f"{API}/task/{task_id}").get_json()
        if d.get("status") in ("done", "error", "canceled"):
            return d
        time.sleep(0.2)
    raise AssertionError("任务超时")


def wait_video(client, task_id, timeout=180):
    """轮询按需视频生成完成（主状态已 done，只看 video_status）。"""
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        d = client.get(f"{API}/task/{task_id}").get_json()
        last = d
        if d.get("video_status") in ("done", "error", "canceled"):
            return d
        time.sleep(0.2)
    raise AssertionError(f"视频生成超时：{last}")


def main():
    app = make_app()
    client = app.test_client()
    text = "按需视频口径验证。".join(str(i) for i in range(120))  # 约 1.5KB

    print("\n[1] video 模式默认只落帧 PNG")
    r = client.post(f"{API}/encode", data={
        "mode": "video", "qr_version": "15", "text": text})
    assert r.status_code == 200, r.data
    task_id = r.get_json()["task_id"]
    st = wait_task(client, task_id)
    check("封装成功", st.get("status") == "done", str(st))
    check("video_status=none（未生成视频）", st.get("video_status") == "none", str(st))
    check("video_size=0", st.get("video_size", -1) == 0, str(st.get("video_size")))
    frames = st.get("frame_count", 0)
    check("frame_count>0（帧序列已落盘）", frames > 0, str(frames))
    check("回显 frame_repeat（重建视频用）",
          st.get("frame_repeat") == it.CAMERA_FRAME_REPEAT, str(st.get("frame_repeat")))
    r = client.get(f"{API}/frame/{task_id}/1")
    check("帧接口可取首帧", r.status_code == 200 and r.data[:8] == b"\x89PNG\r\n\x1a\n")
    r = client.get(f"{API}/frame/{task_id}/{frames + 1}")
    check("帧序号越界被拒", r.status_code == 404)
    r = client.get(f"{API}/download/{task_id}/x.mp4")
    check("未生成时下载视频返回 409 引导", r.status_code == 409,
          f"{r.status_code} {r.get_json()}")

    print("\n[2] 按需生成 mp4")
    r = client.post(f"{API}/encode/{task_id}/video")
    check("生成请求受理", r.status_code == 200
          and r.get_json().get("video_status") in ("building", "done"), r.data)
    st = wait_video(client, task_id)
    check("video_status=done", st.get("video_status") == "done", str(st))
    check("video_size>0", st.get("video_size", 0) > 0, str(st.get("video_size")))
    r = client.post(f"{API}/encode/{task_id}/video")
    check("重复触发幂等（不重新排队）",
          r.status_code == 200 and r.get_json().get("video_status") == "done", r.data)
    r = client.get(f"{API}/download/{task_id}/x.mp4")
    mp4 = r.data
    check("下载 mp4 成功", r.status_code == 200 and mp4[:4] == b"\x00\x00\x00\x20",
          f"{r.status_code} {len(mp4)}B")
    check("mp4 体积为帧序列的 ≥5 倍（体积差实证）",
          len(mp4) > sum(os.path.getsize(os.path.join(it._frames_dir(task_id), f))
                         for f in os.listdir(it._frames_dir(task_id))) * 5,
          f"mp4={len(mp4)}B")

    print("\n[3] 重建视频可解码还原（走 /decode type=video 全链路）")
    r = client.post(f"{API}/decode", data={
        "type": "video",
        "file": (io.BytesIO(mp4), "stream.mp4"),
    }, content_type="multipart/form-data")
    check("解析任务受理", r.status_code == 200, r.data)
    did = r.get_json().get("task_id")
    t0 = time.time()
    d = {}
    while time.time() - t0 < 180:
        d = client.get(f"{API}/decode/{did}").get_json()
        if d.get("status") in ("done", "error"):
            break
        time.sleep(0.2)
    check("视频解析完成", d.get("status") == "done", str(d))
    check("还原文本与原文逐字节一致",
          d.get("payload_json") and json.loads(d["payload_json"]).get("data") == text,
          str(d)[:200])

    print("\n[4] 直出视频（video_file=1）与按需生成口径一致")
    r = client.post(f"{API}/encode", data={
        "mode": "video", "qr_version": "15", "text": text, "video_file": "1"})
    task2 = r.get_json()["task_id"]
    st2 = wait_task(client, task2)
    check("video_status=done（当场产出）", st2.get("video_status") == "done", str(st2))
    check("两条路径帧数一致", st2.get("frame_count") == frames,
          f"{st2.get('frame_count')} vs {frames}")
    check("两条路径码数口径一致", st2.get("nframes") == st.get("nframes"))

    print("\n[5] 失败路径：帧序列缺失不静默产坏视频")
    empty = "f" * 12
    it.set_task(empty, status="done", output_type="video", frame_count=3,
                frame_repeat=5, created_at=time.time(), created_by="")
    r = client.post(f"{API}/encode/{empty}/video")
    check("无帧目录时拒绝生成", r.status_code == 409, f"{r.status_code} {r.data}")
    r = client.post(f"{API}/encode/nonexistent000/video")
    check("未知任务 404", r.status_code == 404, str(r.status_code))

    print(f"\n结果：PASS {PASSES[0]} / FAIL {len(FAILS)}")
    if FAILS:
        for f in FAILS:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
