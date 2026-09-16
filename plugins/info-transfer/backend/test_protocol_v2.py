# -*- coding: utf-8 -*-
"""协议 v2（JZ2 帧）专项测试：T07 CRC 完整性 / T08 二进制直载 / T09 k-of-m 容错。

运行：python test_protocol_v2.py
依赖：qrcode / zfec / opencv-python / numpy / zxing-cpp（venv 齐备）。
"""
import json
import os
import random
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, REPO)

TMP_ROOT = tempfile.mkdtemp(prefix="it_v2_")
os.environ["JZTOOLS_DATA_ROOT"] = TMP_ROOT  # 隔离真实数据根（routes 导入前设置）

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "it_routes_v2", os.path.join(HERE, "routes.py"))
it = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(it)

import qrcode
from qrcode import constants as qrc

random.seed(20260916)
FAILS = []
PASSES = [0]


def check(name, ok, detail=""):
    if ok:
        PASSES[0] += 1
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {detail}")


print("== 1. JZ2 帧 roundtrip（各 fmt，含中文/高熵/空数据） ==")
CASES = [
    ("text", "会议纪要", "本项目旨在解决内网环境下跨网闸的数据交换问题。" * 40, ".txt"),
    ("text", "tiny", "hi", None),
    ("markdown", "说明文档", "# 标题\n\n正文段落。" * 30, ".md"),
    ("word", "纯文本", ["第一段内容" * 20, "", "第三段"], ".docx"),
    ("excel", "花名册", [["序号", "姓名"], [1, "张伟"], [2, "李娜"]], ".xlsx"),
    ("file", "photo.jpg", None, None),   # 高熵字节单独构造
    ("file", "empty.bin", "", None),
]
high_entropy = bytes(random.getrandbits(8) for _ in range(1500))
for fmt, name, data, ext in CASES:
    if data is None and fmt == "file":
        import base64 as _b64
        data = _b64.b64encode(high_entropy).decode()
    env = it.build_envelope_v2(fmt, name, data, ext)
    check(f"magic {name}", env[:3] == b"JZ2")
    back = it.parse_envelope_v2(env)
    # ext 口径与 v1 parse_envelope 一致（去点 + 小写）
    expect_ext = ext.lstrip(".").lower() if ext else None
    same = (back["fmt"] == fmt and back["name"] == name
            and back["data"] == data and (back.get("ext") or None) == expect_ext)
    check(f"roundtrip {name}", same,
          f"fmt={back['fmt']} name={back['name']} ext={back.get('ext')}")

print("== 2. CRC 完整性：位翻转注入 ≥1000 次全拒 ==")
env = it.build_envelope_v2("text", "注入测试", "完整性校验载荷。" * 100)
rejected = 0
accepted = 0
N = 1200
for _ in range(N):
    mutated = bytearray(env)
    pos = random.randrange(len(mutated))
    mutated[pos] ^= (1 << random.randrange(8))
    try:
        it.parse_frame_jz2(bytes(mutated))
        accepted += 1
    except ValueError:
        rejected += 1
check(f"位翻转 {N} 次全拒", rejected == N, f"accepted={accepted}")


def _try_reject(b):
    try:
        it.parse_frame_jz2(b)
        return False
    except ValueError:
        return True


trunc_all = all(_try_reject(env[:cut]) for cut in range(1, len(env)))
check("截断注入全拒", trunc_all)

print("== 3. v2 静态码端到端（单张/多页 + 解码重组） ==")
import cv2  # noqa: E402


def _png_to_file(png):
    p = os.path.join(TMP_ROOT, f"t_{random.randrange(1 << 30):08x}.png")
    with open(p, "wb") as f:
        f.write(png)
    return p


small = "小数据静态码 roundtrip。"
env_s = it.build_envelope_v2("text", "单张", small, ".txt")
paths, count = it.encode_static_output("text", "单张", env_s, 15, TMP_ROOT, "v2s")
check("v2 单张页数", count == 1, f"count={count}")
codes = it.decode_image_file(open(paths[0], "rb").read())
env_obj, page_info = it._parse_scanned_codes(codes)
fmt, name, data, ext = it.parse_envelope(env_obj)
check("v2 单张还原", data == small and name == "单张" and fmt == "text")

big = "多页静态码测试载荷。" * 2000   # 高度可压缩 → 单帧属正常，多页用高熵原件验证
env_b = it.build_envelope_v2("text", "多页", big, ".txt")
paths, count = it.encode_static_output("text", "多页", env_b, 15, TMP_ROOT, "v2m")
check("v2 可压缩小数据单帧", count == 1, f"count={count}")
codes = []
for p in paths:
    codes.extend(it.decode_image_file(open(p, "rb").read()))
env_obj, page_info = it._parse_scanned_codes(codes)
fmt, name, data, ext = it.parse_envelope(env_obj)
check("v2 单帧还原", data == big, f"len={len(data)} vs {len(big)}")

# 不可压缩的原件数据 → 强制多页
import base64 as _b64
big_raw = bytes(random.getrandbits(8) for _ in range(6000))
big_env = it.build_envelope_v2("file", "blob.bin", _b64.b64encode(big_raw).decode())
paths, count = it.encode_static_output("file", "blob.bin", big_env, 15, TMP_ROOT, "v2b")
check("v2 高熵多页拆分", count > 1, f"count={count}")
codes = []
for p in paths:
    codes.extend(it.decode_image_file(open(p, "rb").read()))
env_obj, page_info = it._parse_scanned_codes(codes)
fmt, name, data, ext = it.parse_envelope(env_obj)
check("v2 高熵多页还原", _b64.b64decode(data) == big_raw)

print("== 4. v2 视频流端到端 + T09 k-of-m 容错（丢帧注入） ==")
payload = ("视频流 k-of-m 容错测试。" * 500).encode("utf-8")
mp4 = os.path.join(TMP_ROOT, "v2video.mp4")
nframes, k, m = it.encode_to_video(big_env, 15, mp4, frame_repeat=1)
print(f"  编码：nframes={nframes} k={k} m={m}")
codes_v = it.decode_video_frames(mp4)
raw = it.reassemble_any(codes_v)
env_obj = it._envelope_from_raw(raw)
fmt, name, data, ext = it.parse_envelope(env_obj)
check("v2 视频流还原", _b64.b64decode(data) == big_raw and name == "blob.bin")

# T09：v2 share 帧丢帧注入——复用上面的高熵大载荷（k=14, m=24），丢 30% 后重组
stream = (len(big_env)).to_bytes(4, "big") + big_env
chunks = it._chunk_data(stream, 15, qrc.ERROR_CORRECT_L,
                        maxchunk=it._max_chunk_size_v2(15, qrc.ERROR_CORRECT_L, 2))
shares, k2, m2 = it._fec_encode(chunks)
frames = it._qrformat_encode_v2(shares, k2, m2)
drop = max(1, int(len(frames) * 0.3))
survivors = frames[:-drop]           # 丢尾部 30%（模拟视频截断/漏扫）
print(f"  丢帧口径：nframes={len(frames)} k={k2} m={m2} drop={drop} got={len(frames) - drop}")
try:
    raw2 = it.reassemble_qrtransfer_v2(survivors)
    ok = raw2 == big_env
except ValueError as e:
    ok = False
    print("  丢帧重组异常:", e)
check(f"v2 丢 {drop}/{len(frames)} 帧（~30%，k={k2}）仍还原", ok)

# 丢到低于 k → 必须报错
try:
    it.reassemble_qrtransfer_v2(frames[:k2 - 1])
    check("丢到 <k 报错", False)
except ValueError:
    check("丢到 <k 报错", True)

print("== 5. v1 兼容：v1 编码产物经新解码端零回归 ==")
it.PROTOCOL_V2 = False
env_v1 = it.build_envelope("text", "v1兼容", "旧协议文本载荷。" * 50)
check("v1 build_envelope 仍为 JSON", json.loads(env_v1.decode("utf-8"))["jzt"] == 1)
mp41 = os.path.join(TMP_ROOT, "v1video.mp4")
it.encode_to_video(env_v1, 15, mp41, frame_repeat=1)
codes1 = it.decode_video_frames(mp41)
raw1 = it.reassemble_any(codes1)
env_obj1 = it._envelope_from_raw(raw1)
fmt, name, data, ext = it.parse_envelope(env_obj1)
check("v1 视频流经新解码端还原", data == "旧协议文本载荷。" * 50)
# v1 静态
paths1, c1 = it.encode_static_output("text", "v1兼容", env_v1, 15, TMP_ROOT, "v1s")
codes1s = []
for p in paths1:
    codes1s.extend(it.decode_image_file(open(p, "rb").read()))
env1s, _ = it._parse_scanned_codes(codes1s)
fmt, name, data, ext = it.parse_envelope(env1s)
check("v1 静态经新解码端还原", data == "旧协议文本载荷。" * 50)
it.PROTOCOL_V2 = True

print(f"\n结果：PASS={PASSES[0]} FAIL={len(FAILS)}")
if FAILS:
    print("失败项：", FAILS)
    sys.exit(1)
