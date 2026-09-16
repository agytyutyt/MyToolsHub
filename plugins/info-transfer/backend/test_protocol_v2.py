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
    # word 生产链路载荷 = 提取后的纯文本字符串（extract_doc_lean），与 v1 解析语义一致
    # （仅 excel 做 JSON 二维数组还原）；word 列表形态非生产形状，不进 roundtrip 用例。
    ("word", "纯文本", "第一段内容" * 20 + "\n\n第三段", ".docx"),
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

print("== 5. T10 xz 压缩：试压选择 + flags + 还原 ==")
import base64 as _b64
# 试压数据设计：三段相同的伪随机文本，相邻段间隔 >32KB（zlib 窗口外）。
# 伪随机文本段内无自引用 → zlib 只能按字面编码第 2/3 段；xz 凭 16MB 字典
# 跨距引用第 1 段 → 必然严格小于 zlib（小规模连续重复文本 zlib 常反超，不适用）。
unit = "".join(random.choice(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    for _ in range(4096))
filler = "隔断填充。" * 6000   # ~54KB，自身可压，仅作窗口隔断
rep_text = unit + filler + unit + filler + unit
env_x = it.build_envelope_v2("text", "xz测试", rep_text, ".txt")
fr_x = it.parse_frame_jz2(env_x)
check("T10 xz 被选中（flags=2）", fr_x["comp"] == it.JZ2_COMP_XZ, f"comp={fr_x['comp']}")
check("T10 xz 严格小于 zlib",
      len(fr_x["payload"]) < len(__import__("zlib").compress(rep_text.encode("utf-8"), 9)),
      f"xz={len(fr_x['payload'])}")
check("T10 xz 信封还原", it.parse_envelope_v2(env_x)["data"] == rep_text)
high = bytes(random.getrandbits(8) for _ in range(800))
env_h = it.build_envelope_v2("file", "h.bin", _b64.b64encode(high).decode())
check("T10 高熵不启用压缩", it.parse_frame_jz2(env_h)["comp"] == it.JZ2_COMP_NONE)
paths_x, _c = it.encode_static_output("text", "xz测试", env_x, 15, TMP_ROOT, "xz1")
codes_x = []
for p in paths_x:
    codes_x.extend(it.decode_image_file(open(p, "rb").read()))
env_obj, _ = it._parse_scanned_codes(codes_x)
_fmt, _n, data, _e = it.parse_envelope(env_obj)
check("T10 xz 静态还原", data == rep_text)

print("== 6. T11 拆包重组（mode=rebuild） ==")
import io as _io
import zipfile as _zf
buf = _io.BytesIO()
with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as z:
    for i in range(20):
        z.writestr(f"word/document{i}.xml",
                   "<w:document>" + "重复内容体，用于拆包重组验证。" * 200 + "</w:document>")
docx_bytes = buf.getvalue()
env_r = it.build_envelope_v2("file", "r.docx", _b64.b64encode(docx_bytes).decode(),
                             "docx", rebuild=True)
fr_r = it.parse_frame_jz2(env_r)
check("T11 rebuild flags 置位", fr_r["mode"] == 1, f"mode={fr_r['mode']}")
check("T11 rebuild 体积显著更小", len(env_r) < len(docx_bytes) * 0.6,
      f"env={len(env_r)} raw={len(docx_bytes)}")
env_obj = it.parse_envelope_v2(env_r)
rebuilt = _b64.b64decode(env_obj["data"])
check("T11 重建 zip 合法且条目一致", env_obj.get("mode") == 1 and
      set(_zf.ZipFile(_io.BytesIO(rebuilt)).namelist()) ==
      set(_zf.ZipFile(_io.BytesIO(docx_bytes)).namelist()))
check("T11 重建产物已压缩（≈原件尺寸）", len(rebuilt) <= len(docx_bytes) * 1.2,
      f"rebuilt={len(rebuilt)} raw={len(docx_bytes)}")
env_d = it.build_envelope_v2("file", "r.docx", _b64.b64encode(docx_bytes).decode(), "docx")
check("T11 默认 exact 路径不置位", it.parse_frame_jz2(env_d)["mode"] == 0)
env_bad = it.build_envelope_v2("file", "bad.docx",
                               _b64.b64encode(b"not a zip container").decode(),
                               "docx", rebuild=True)
check("T11 非 zip 容器回退纯压缩", it.parse_frame_jz2(env_bad)["mode"] == 0)
paths_r, _c = it.encode_static_output("file", "r.docx", env_r, 15, TMP_ROOT, "rb1")
codes_r = []
for p in paths_r:
    codes_r.extend(it.decode_image_file(open(p, "rb").read()))
env_obj, _ = it._parse_scanned_codes(codes_r)
rebuilt2 = _b64.b64decode(env_obj["data"])
with _zf.ZipFile(_io.BytesIO(rebuilt2)) as z1:
    _z0 = _zf.ZipFile(_io.BytesIO(docx_bytes))
    content_ok = all(z1.read(nm) == _z0.read(nm) for nm in _z0.namelist())
check("T11 rebuild 静态 roundtrip 条目内容等价", content_ok)

print("== 7. T12 pdf/ppt 精简提取 + fuzz ==")
BENCH = os.path.join(REPO, ".workbuddy", "tmp", "it_bench")
SAMPLES_12 = {}
for s in ("sample.pdf", "sample.ppt"):
    p = os.path.join(BENCH, s)
    if os.path.exists(p):
        with open(p, "rb") as f:
            SAMPLES_12[s] = f.read()
for s, fb in SAMPLES_12.items():
    try:
        lfmt, lname, ldata, lext = it.extract_doc_lean(s, fb)
        check(f"T12 {s} 精简提取", lfmt == "text" and lext == s.split(".")[1]
              and isinstance(ldata, str) and len(ldata) > 50,
              f"fmt={lfmt} len={len(ldata) if isinstance(ldata, str) else '?'}")
        env_p = it.build_envelope(lfmt, lname, ldata, lext)
        env_obj = it.parse_envelope_v2(env_p)
        check(f"T12 {s} 信封还原一致", env_obj["data"] == ldata)
    except it.LeanUnsupported as e:
        print(f"  SKIP {s} 提取（{e}）")
    except Exception as e:
        check(f"T12 {s} 精简提取", False, f"{type(e).__name__}: {e}")
# fuzz：随机字节 / 截断 / 位翻转 1000 轮，仅允许 LeanUnsupported
rnd = random.Random(20260916)
pdf_fb = SAMPLES_12.get("sample.pdf", b"%PDF-1.4 dummy")
ppt_fb = SAMPLES_12.get("sample.ppt", b"\xd0\xcf\x11\xe0 dummy")
crash = 0
for i in range(1000):
    kind = i % 3
    src = pdf_fb if i % 2 == 0 else ppt_fb
    if kind == 0:
        blob = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 2000)))
    elif kind == 1:
        blob = src[:rnd.randint(0, len(src))]
    else:
        ba = bytearray(src) if src else bytearray(b"x")
        ba[rnd.randrange(len(ba))] ^= 1 << rnd.randrange(8)
        blob = bytes(ba)
    for fn in (it._extract_pdf_text, it._extract_ppt_text):
        try:
            fn(blob)
        except it.LeanUnsupported:
            pass
        except Exception:
            crash += 1
check("T12 fuzz 1000 轮零崩溃（仅 LeanUnsupported）", crash == 0, f"crash={crash}")

print("== 8. v1 兼容：v1 编码产物经新解码端零回归 ==")
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
