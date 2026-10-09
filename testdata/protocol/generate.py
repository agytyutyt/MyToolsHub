# -*- coding: utf-8 -*-
"""二维码传输协议测试向量生成器（Q6b / 报告 C3）。

产出 `testdata/protocol/` 下的向量文件，供 `tools/e2e/qr-protocol-consistency.py`
驱动四处实现做一致性断言。**向量由真实实现产出**（不手写），来源与重生成方式见
同目录 `README.md`。

    python testdata/protocol/generate.py

设计约束：
- 只调用各实现的**纯函数**（不生成视频、不起 Flask、不联网）——产出必须可重现；
- v1 帧由三个编码端各自产出（分块/FEC 比例是各端本地调参，见契约文档 §4.1），
  统一交给 qr-video-decode 的重组函数验证「能解回同一份输入」；
- v2 帧由 info-transfer 产出（它与 worker.js 是 v2 的两处实现），
  同时记录解析字段与一个「crc 被破坏」的负样本期望。
"""
import base64
import hashlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import tempfile
TMP_ROOT = tempfile.mkdtemp(prefix="proto-gen-")
os.environ["JZTOOLS_DATA_ROOT"] = TMP_ROOT          # 隔离真实数据根（routes 导入前设置）

QR_VERSION = 15                                       # 向量固定二维码版本（写入 manifest）
EC_NAME = "L"

# 固定输入：ASCII + CJK + 标点 + 换行，覆盖 UTF-8 多字节与 base64 载荷形态
INPUT_TEXT = (
    "QR-transfer 协议测试向量 / protocol conformance vector\n"
    "0123456789abcdefghijklmnopqrstuvwxyz\n"
    "轨迹点：2026-10-09 08:15 经度 121.47 纬度 31.23 小区 460001234567890\n"
    "特殊字符：{}[]\"'\\`~!@#$%^&*()_+-=<>?/.,;:|　（全角空格）\n"
) * 12


def load(name, rel_path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    it = load("proto_it_routes", "plugins/info-transfer/backend/routes.py")
    traj = load("proto_traj_routes", "plugins/trajectory-convert/backend/routes.py")
    mm = load("proto_map_marker", "plugins/map-marker/frontend/generate_qr_stream.py")
    qrvd = load("proto_qrvd_routes", "plugins/qr-video-decode/backend/routes.py")

    import qrcode
    from qrcode import constants as qrc
    err = getattr(qrc, "ERROR_CORRECT_" + EC_NAME)

    data = INPUT_TEXT.encode("utf-8")
    input_sha = hashlib.sha256(data).hexdigest()
    with open(os.path.join(HERE, "input.txt"), "wb") as f:
        f.write(INPUT_TEXT.encode("utf-8"))

    # ---------- 1) v1 帧头基元向量（三端必须逐字符相同） ----------
    index_cases = [0, 1, 7, 63, 255, 256, 4095, 4096, 65535, 0xFFFFFF]
    fec_cases = [(1, 2), (3, 5), (10, 16), (128, 130), (200, 255), (255, 255), (1, 255)]
    data_cases = [b"", b"\x00", b"abc", bytes(range(256)), data[:137]]
    headers = {
        "note": "v1 帧头三段基元的期望值。三处实现（trajectory-convert / info-transfer / "
                "map-marker 生成脚本）必须逐字符相同——这是帧头布局的机器可核对面。"
                "fecinfo 用例只覆盖 m ≤ 255：m=256 属已知缺陷面，另列 defect_m256。",
        "index": [{"n": n, "expected": it.encode_index(n).decode("ascii")} for n in index_cases],
        "fecinfo": [{"k": k, "m": m, "expected": it.encode_fecinfo(k, m).decode("ascii")}
                    for k, m in fec_cases],
        "data": [{"b64src": base64.b64encode(b).decode("ascii"),
                  "expected": it.encode_data(b).decode("ascii")} for b in data_cases],
    }
    for k, m in fec_cases:                       # map-marker 只有 encode_fecinfo，逐一同形验证
        assert mm.encode_fecinfo(k, m) == it.encode_fecinfo(k, m).decode("ascii"), (k, m)

    # ---------- 1b) 已知缺陷刻画（m=256 时 FEC 参数申报溢出） ----------
    # 帧头第 3 段用 (k << 8) | m 表示 k/m，m 只有 8 位可用；但 MAX_FEC_M=256 允许 m=256，
    # 此时进位吃掉 k（读出 k+1、m=0），解码端要么判「k/m 非法」要么除零。
    # 三处实现还各不相同（map-marker 把 m 当单字节截断）。本向量把现状**钉住**：
    # 任一端修好（或三端统一）都会让台架变红，从而强制更新契约文档 §6.1。
    def mm_fec(k, m):
        v = mm.encode_fecinfo(k, m)
        return v.decode("ascii") if isinstance(v, bytes) else v

    defect_cases = [(1, 256), (230, 256), (255, 256)]
    headers["defect_m256"] = {
        "note": "m=256 的 FEC 参数申报：三端输出互不相同，且按 (k<<8)|m 反解得到的 k/m 已被破坏。"
                "契约文档 §6.1 / 整改清单 C3 已登记，修法待拍板（限制 m ≤ 255 或改两段字节）。",
        "cases": [{"k": k, "m": m,
                   "info_transfer": it.encode_fecinfo(k, m).decode("ascii"),
                   "trajectory_convert": traj.encode_fecinfo(k, m).decode("ascii"),
                   "map_marker": mm_fec(k, m),
                   "roundtrip_decodes": int.from_bytes(
                       base64.b64decode(it.encode_fecinfo(k, m)), "big") & 0xFF == m}
                  for k, m in defect_cases],
    }


    # ---------- 2) v1 往返向量（三端各自编码 → qrvd 解回同一输入） ----------
    prefixed = len(data).to_bytes(it.SIZE_DATASIZE, it.BYTE_ORDER) + data

    def frames_from(mod, chunks=None):
        chunks = chunks if chunks is not None else mod._chunk_data(prefixed, QR_VERSION, err)
        shares, k, m = mod._fec_encode(chunks)
        codes = mod._qrformat_encode(shares, k, m)
        return codes, k, m

    traj_frames, traj_k, traj_m = frames_from(traj)
    it_frames, it_k, it_m = frames_from(it)

    def decode_with_qrvd(frames):
        chunks, nframes = {}, None
        k = m = None
        for content in frames:
            pre = content[:12]
            idx = int.from_bytes(base64.b64decode(pre[0:4].encode("ascii")), "big")
            nframes = int.from_bytes(base64.b64decode(pre[4:8].encode("ascii")), "big")
            km = int.from_bytes(base64.b64decode(pre[8:12].encode("ascii")), "big")
            chunks[str(idx)] = content[12:]
            k, m = km >> 8, km & 0xFF
        return qrvd.reassemble(nframes, k, m, chunks)

    roundtrip = {"note": "同一输入由三个编码端各自产帧（分块与 FEC 比例属各端本地调参，"
                         "帧数/字节可不同，见契约文档 §4.1），但都必须能被 qr-video-decode 解回同一输入。"
                         "map-marker 生成脚本只在「单组（nblocks=1）」时与解码端同构，"
                         "多组时其块→组映射为连续切分（解码端为跨步切分），故单列 known_defect。",
                 "input_sha256": input_sha, "input_bytes": len(data),
                 "qr_version": QR_VERSION, "ec": EC_NAME, "cases": {}}
    for name, frames, k, m in (("trajectory-convert", traj_frames, traj_k, traj_m),
                               ("info-transfer", it_frames, it_k, it_m)):
        got = decode_with_qrvd(frames)
        assert got == data, "%s 往返不一致（解出 %d 字节）" % (name, len(got))
        roundtrip["cases"][name] = {
            "nframes": len(frames), "k": k, "m": m,
            "frames": frames, "reassembled_sha256": hashlib.sha256(got).hexdigest(),
        }

    # map-marker：单组用例（与其自身协议实现相容的路径）
    mm_small_data = INPUT_TEXT[:60].encode("utf-8")
    mm_cap = mm.qr_data_capacity(QR_VERSION, EC_NAME)
    mm_cs = int((mm_cap - 12) * 3 / 4) - 4

    def mm_frames_for(payload):
        padded = mm.prepare_data(payload, mm_cs)
        tdb = len(padded) // mm_cs

        def find_k(total):                      # 与 generate_qr_stream.encode_to_video 同逻辑
            best_k = 1
            for candidate in range(1, total + 1):
                if total % candidate == 0:
                    nblocks = total // candidate
                    if 50 <= nblocks <= 500:
                        return candidate
                    if best_k == 1 or abs(nblocks - 200) < abs(total // best_k - 200):
                        best_k = candidate
            return best_k

        k = find_k(tdb)
        nblocks = tdb // k
        m = k + max(1, k // 2)
        if m <= k:
            m = k + 1
        total_frames = nblocks * m
        blocks = mm.fec_encode(padded, mm_cs, k, m)
        frames = [mm.make_frame_payload(i, total_frames, k, m,
                                        base64.b64encode(b).decode("ascii"))
                  for i, b in enumerate(blocks)]
        return frames, k, m, nblocks, total_frames

    sm_frames, sm_k, sm_m, sm_nb, sm_tot = mm_frames_for(mm_small_data)
    assert sm_nb == 1, "单组用例应落在 nblocks=1，实际 %d" % sm_nb
    sm_got = decode_with_qrvd(sm_frames)
    assert sm_got == mm_small_data, "map-marker 单组往返不一致"
    roundtrip["cases"]["map-marker"] = {
        "nframes": sm_tot, "k": sm_k, "m": sm_m, "input_bytes": len(mm_small_data),
        "input_b64": base64.b64encode(mm_small_data).decode("ascii"),
        "input_sha256": hashlib.sha256(mm_small_data).hexdigest(),
        "frames": sm_frames, "reassembled_sha256": hashlib.sha256(sm_got).hexdigest(),
        "scope": "nblocks=1（单组）——map-marker 与解码端唯一同构的形态",
    }

    # map-marker：多组用例 = 已知缺陷刻画（长度正确、内容错位）
    md_frames, md_k, md_m, md_nb, md_tot = mm_frames_for(data)
    assert md_nb > 1, "多组缺陷用例应落在 nblocks>1，实际 %d" % md_nb
    md_got = decode_with_qrvd(md_frames)
    roundtrip["known_defect_multigroup"] = {
        "note": "map-marker 的 generate_qr_stream.py 把数据块**连续**分进各组"
                "（group g = 块 g*k..g*k+k-1），而 trajectory-convert / info-transfer / "
                "qr-video-decode 一律按**跨步**分组（data_list[i::nblocks]）。"
                "nblocks>1 时解码端长度校验通过但内容错位——静默坏。契约文档 §6.2 已登记，修法待拍板。",
        "input_bytes": len(data), "input_sha256": input_sha,
        "input_ref": "input.txt（主输入）",
        "nframes": md_tot, "k": md_k, "m": md_m, "nblocks": md_nb,
        "frames_sha256": hashlib.sha256("\n".join(md_frames).encode("utf-8")).hexdigest(),
        "wrong_output_sha256": hashlib.sha256(md_got).hexdigest(),
        "output_length_matches": len(md_got) == len(data),
        "output_equals_input": md_got == data,
    }

    # ---------- 3) v2（JZ2）向量 ----------
    env = it.build_envelope_v2("text", "protocol-vector.txt", INPUT_TEXT)
    chunks = it._chunk_data(prefixed, QR_VERSION, err,
                            maxchunk=it._max_chunk_size_v2(QR_VERSION, err, 2))
    shares, v2_k, v2_m = it._fec_encode(chunks)
    share_bytes = [bytes(f) if not isinstance(f, bytes) else f
                   for f in it._qrformat_encode_v2(shares, v2_k, v2_m)]

    def parse_fields(frame):
        d = it.parse_frame_jz2(frame)
        return {"comp": d["comp"], "mode": int(bool(d["mode"])), "is_fec": int(bool(d["is_fec"])),
                "seg_i": d["seg_i"], "seg_n": d["seg_n"], "meta_len": len(d["meta"]),
                "body_len": len(d["payload"])}

    with open(os.path.join(HERE, "v2_envelope.bin"), "wb") as f:
        f.write(env)
    with open(os.path.join(HERE, "v2_shares.bin"), "wb") as f:
        f.write(b"".join(len(b).to_bytes(4, "big") + b for b in share_bytes))

    corrupted = bytearray(env)
    corrupted[20] ^= 0xFF                                   # 破坏 crc32 字段
    bad = bytes(corrupted)
    try:
        it.parse_frame_jz2(bad)
        reject = "未拒绝"
    except Exception as exc:
        reject = type(exc).__name__

    envelope_decoded = it.parse_envelope_v2(env)
    v2 = {"note": "v2 由 info-transfer 产出（v2 的两处实现是 info-transfer 与 qr-video-decode 的 worker.js）。"
                  "comp 由编码端试压择优，运行期可能随 zlib 实现变化，故不作为期望值；"
                  "crc 覆盖范围与字段布局是断言对象。",
          "envelope": {"sha256": hashlib.sha256(env).hexdigest(), "bytes": len(env),
                       "header_hex": env[:21].hex(), "fields": parse_fields(env),
                       "decoded_text_sha256": hashlib.sha256(
                           (envelope_decoded.get("data") or "").encode("utf-8")).hexdigest(),
                       "decoded_fmt": envelope_decoded.get("fmt"),
                       "decoded_name": envelope_decoded.get("name")},
          "shares": {"count": len(share_bytes), "k": v2_k, "m": v2_m,
                     "first_sha256": hashlib.sha256(share_bytes[0]).hexdigest(),
                     "first_fields": parse_fields(share_bytes[0])},
          "crc_negative": {"expect_reject": reject, "python": True},
          "roundtrip_v2": hashlib.sha256(
              it.reassemble_qrtransfer_v2(list(share_bytes))).hexdigest()}

    manifest = {"schema": 1,
                "generated_by": "testdata/protocol/generate.py",
                "protocol_doc": "docs/design/二维码传输协议-契约文档.md",
                "harness": "tools/e2e/qr-protocol-consistency.py",
                "implementations": ["plugins/trajectory-convert/backend/routes.py",
                                     "plugins/info-transfer/backend/routes.py",
                                     "plugins/map-marker/frontend/generate_qr_stream.py",
                                     "plugins/qr-video-decode/backend/routes.py",
                                     "plugins/qr-video-decode/frontend/worker.js"],
                "input_sha256": input_sha, "input_bytes": len(data),
                "qr_version": QR_VERSION, "ec": EC_NAME,
                "constants_snapshot": {
                    "shared_v1_header": {"SIZE_INDEX": 3, "SIZE_PREFIX": 4,
                                          "SIZE_DATASIZE": 4, "BYTE_ORDER": "big",
                                          "MAX_FEC_M": 256},
                    "encoder_local": {"trajectory-convert.FEC_RATIO": traj.FEC_RATIO,
                                       "info-transfer.FEC_RATIO": it.FEC_RATIO,
                                       "info-transfer.PAYLOAD_SAFETY": it.PAYLOAD_SAFETY}},
                "files": ["input.txt", "v1_headers.json", "v1_roundtrip.json",
                           "v2_envelope.bin", "v2_shares.bin", "v2_frames.json"]}
    for name, obj in (("v1_headers.json", headers), ("v1_roundtrip.json", roundtrip),
                       ("v2_frames.json", v2), ("manifest.json", manifest)):
        with open(os.path.join(HERE, name), "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")

    print("向量已生成：%s" % HERE)
    for n in manifest["files"]:
        p = os.path.join(HERE, n)
        print("  %-22s %8d B" % (n, os.path.getsize(p)))
    print("输入 %d 字节 sha256=%s…；v1 帧数 traj=%d it=%d；map-marker 单组=%d 多组=%d(缺陷刻画)"
          % (len(data), input_sha[:12], len(traj_frames), len(it_frames),
             sm_tot, md_tot))


if __name__ == "__main__":
    main()
