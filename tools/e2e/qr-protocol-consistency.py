# -*- coding: utf-8 -*-
"""二维码传输协议 · 跨实现一致性台架（报告 C3 / Q6b）。

驱动四处实现（trajectory-convert、info-transfer、map-marker 生成脚本、qr-video-decode）
对同一份 `testdata/protocol/` 向量做断言，外加 Node 侧 `worker.js` 的 v2 交叉验证。
台架在**宿主侧**：只 import 各实现，插件之间零依赖不变（C-11 不涉及，台架不出包）。

    python tools/e2e/qr-protocol-consistency.py

红=协议漂了（某端与向量/他端不一致）。**缺陷①（m=256 申报溢出）**已于 2026-10-09 按修法①
修复（三端参数推导把 m 收到 ≤255，帧头布局与解码端未动），C6–C7 自此为**回归断言**：
复现输入必须 m ≤ 255 且往返逐字节一致，参数推导扫描必须恒不产出 m=256
（`defect_m256.cases` 仍保留修复前史实，那是低层基元 `encode_fecinfo` 的直接产出，不参与断言）。
**缺陷②（map-marker 多组分组不同构）**已于 2026-10-09 修复，C3–C5 转为回归断言：
map-marker 多组输出必须可被 qr-video-decode 重组、往返逐字节一致（旧产物需重生成）。

契约真源：docs/design/二维码传输协议-契约文档.md
向量再生成：python testdata/protocol/generate.py
"""
import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
VEC = os.path.join(REPO, "testdata", "protocol")
sys.path.insert(0, REPO)

# 与控制台编码和解：本台架打印 ✅/❌，GBK 控制台（Windows 缺省）会在 print 处抛
# UnicodeEncodeError 直接崩掉整个台架（§B-20 同类隐患统一处置）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


TMP_ROOT = tempfile.mkdtemp(prefix="qrc-proto-")
os.environ["JZTOOLS_DATA_ROOT"] = TMP_ROOT       # 隔离真实数据根（必须在 routes 导入前）

FAILS = []
PASSES = [0]
SKIPS = []


def check(name, ok, detail=""):
    if ok:
        PASSES[0] += 1
        print("  ✅ %s" % name)
    else:
        FAILS.append(name)
        print("  ❌ %s %s" % (name, detail))


def skip(name, why):
    SKIPS.append(name)
    print("  ⏭ %s（%s）" % (name, why))


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha(b):
    return hashlib.sha256(b).hexdigest()


JS_HELPER = r"""
const fs = require("fs"), path = require("path"), vm = require("vm");
const FRONTEND = process.argv[2];
const src = fs.readFileSync(path.join(FRONTEND, "worker.js"), "utf8");
const sandbox = {console, atob, btoa, TextDecoder, TextEncoder, Uint8Array, Uint8ClampedArray,
  Int8Array, ArrayBuffer, DataView, Buffer, Blob, Response, DecompressionStream, Promise,
  setTimeout, clearTimeout};
sandbox.self = sandbox;
sandbox.importScripts = () => {};
sandbox.postMessage = () => {};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {filename: "worker.js"});
const h = sandbox.__qrvdTestHooks;
const out = {hooks: !!(h && h.parseJz2Frame && h.crc32)};
const framesFile = process.argv[3], sharesFile = process.argv[4];
const env = fs.readFileSync(framesFile);
try {
  const f = h.parseJz2Frame(env);
  out.fields = {ver: f.ver, flags: f.flags, comp: f.comp, mode: f.mode,
                is_fec: f.isFec ? 1 : 0, seg_i: f.segI, seg_n: f.segN,
                meta_len: f.meta.length, body_len: f.payload.length};
  out.meta_hex = Buffer.from(f.meta).toString("hex");
  out.payload_sha = require("crypto").createHash("sha256").update(f.payload).digest("hex");
} catch (e) { out.parse_error = String(e.message || e); }
// crc 覆盖范围：头前 17 字节 + meta + payload
const crc = h.crc32(env, 0, 17);
out.crc_over_head = crc >>> 0;
// 破坏 crc 必须被拒
const bad = Buffer.from(env); bad[20] ^= 0xFF;
try { h.parseJz2Frame(bad); out.rejects_corrupt = false; }
catch (e) { out.rejects_corrupt = true; out.reject_kind = String(e.message || e).slice(0, 40); }
// share 帧逐个解析（与 Python 端同一批字节）
const raw = fs.readFileSync(sharesFile);
const shares = []; let p = 0;
while (p + 4 <= raw.length) {
  const n = raw.readUInt32BE(p); p += 4; shares.push(raw.slice(p, p + n)); p += n;
}
try {
  out.share_count = shares.length;
  const f0 = h.parseJz2Frame(shares[0]);
  out.first_share = {seg_i: f0.segI, seg_n: f0.segN, is_fec: f0.isFec ? 1 : 0,
                     comp: f0.comp, mode: f0.mode, meta_len: f0.meta.length,
                     body_len: f0.payload.length};
} catch (e) { out.share_error = String(e.message || e); }
console.log(JSON.stringify(out));
"""


def main():
    if not os.path.isdir(VEC):
        print("缺少向量目录 %s——先跑 python testdata/protocol/generate.py" % VEC)
        return 1

    it = load("qr_it_routes", "plugins/info-transfer/backend/routes.py")
    traj = load("qr_traj_routes", "plugins/trajectory-convert/backend/routes.py")
    mm = load("qr_mm_gen", "plugins/map-marker/frontend/generate_qr_stream.py")
    qrvd = load("qr_qrvd_routes", "plugins/qr-video-decode/backend/routes.py")

    manifest = json.load(open(os.path.join(VEC, "manifest.json"), encoding="utf-8"))
    headers = json.load(open(os.path.join(VEC, "v1_headers.json"), encoding="utf-8"))
    rt = json.load(open(os.path.join(VEC, "v1_roundtrip.json"), encoding="utf-8"))
    v2 = json.load(open(os.path.join(VEC, "v2_frames.json"), encoding="utf-8"))
    input_bytes = open(os.path.join(VEC, "input.txt"), "rb").read()

    print("== A 段：向量自洽与来源 ==")
    check("A1 输入与 manifest 哈希一致", sha(input_bytes) == manifest["input_sha256"])
    check("A2 向量来源为契约文档所指实现清单",
          all(os.path.isfile(os.path.join(REPO, p)) for p in manifest["implementations"]))

    print("== B 段：v1 帧头基元（三端必须逐字符相同） ==")
    diverge = []
    for c in headers["index"]:
        vals = {traj.encode_index(c["n"]).decode("ascii"), it.encode_index(c["n"]).decode("ascii"),
                mm.encode_index(c["n"])}
        if len(vals) != 1 or c["expected"] not in vals:
            diverge.append(("index", c["n"], vals))
    check("B1 encode_index 三端一致且等于向量（%d 例）" % len(headers["index"]), not diverge,
          str(diverge[:2]))
    diverge = []
    for c in headers["fecinfo"]:
        mmv = mm.encode_fecinfo(c["k"], c["m"])
        mmv = mmv.decode("ascii") if isinstance(mmv, bytes) else mmv
        vals = {traj.encode_fecinfo(c["k"], c["m"]).decode("ascii"),
                it.encode_fecinfo(c["k"], c["m"]).decode("ascii"), mmv}
        if len(vals) != 1 or c["expected"] not in vals:
            diverge.append((c["k"], c["m"], vals))
    check("B2 encode_fecinfo 三端一致且等于向量（m ≤ 255，%d 例）" % len(headers["fecinfo"]),
          not diverge, str(diverge[:2]))
    diverge = []
    for c in headers["data"]:
        raw = base64.b64decode(c["b64src"])
        vals = {traj.encode_data(raw).decode("ascii"), it.encode_data(raw).decode("ascii")}
        if len(vals) != 1 or c["expected"] not in vals:
            diverge.append((len(raw), vals))
    check("B3 encode_data 两端一致且等于向量（%d 例）" % len(headers["data"]), not diverge,
          str(diverge[:2]))
    check("B4 共享常量数值一致（SIZE_PREFIX / SIZE_DATASIZE / BYTE_ORDER）",
          traj.SIZE_PREFIX == it.SIZE_PREFIX == qrvd.SIZE_PREFIX
          and traj.SIZE_DATASIZE == it.SIZE_DATASIZE == qrvd.SIZE_DATASIZE
          and traj.BYTE_ORDER == it.BYTE_ORDER == qrvd.BYTE_ORDER,
          "traj=%s it=%s qrvd=%s" % (traj.SIZE_PREFIX, it.SIZE_PREFIX, qrvd.SIZE_PREFIX))
    if not hasattr(qrvd, "SIZE_INDEX"):
        skip("B5 qrvd 的 SIZE_INDEX 常量", "qr-video-decode 未定义该常量（契约文档 §7-1 已登记）")

    print("== C 段：v1 往返（编码端产帧 → qr-video-decode 解回同一输入） ==")

    def frames_to_chunks(frames):
        chunks, nframes, k, m = {}, None, None, None
        for content in frames:
            pre = content[:12]
            idx = int.from_bytes(base64.b64decode(pre[0:4].encode("ascii")), "big")
            nframes = int.from_bytes(base64.b64decode(pre[4:8].encode("ascii")), "big")
            km = int.from_bytes(base64.b64decode(pre[8:12].encode("ascii")), "big")
            chunks[str(idx)] = content[12:]
            k, m = km >> 8, km & 0xFF
        return nframes, k, m, chunks

    def re_encode(mod, payload):
        prefixed = len(payload).to_bytes(mod.SIZE_DATASIZE, mod.BYTE_ORDER) + payload
        import qrcode
        from qrcode import constants as qrc
        err = getattr(qrc, "ERROR_CORRECT_" + manifest["ec"])
        ver = manifest["qr_version"]
        chunks = mod._chunk_data(prefixed, ver, err)
        shares, k, m = mod._fec_encode(chunks)
        return mod._qrformat_encode(shares, k, m)

    for name in ("trajectory-convert", "info-transfer"):
        mod = traj if name == "trajectory-convert" else it
        case = rt["cases"][name]
        frames = re_encode(mod, input_bytes)
        check("C1a %s 重跑产帧与向量逐帧相同（%d 帧）" % (name, case["nframes"]),
              frames == case["frames"])
        nf, k, m, ch = frames_to_chunks(frames)
        got = qrvd.reassemble(nf, k, m, ch)
        check("C1b %s → qr-video-decode 解回原输入" % name, sha(got) == case["reassembled_sha256"])

    case = rt["cases"]["map-marker"]
    mm_cs = int((mm.qr_data_capacity(manifest["qr_version"], manifest["ec"]) - 12) * 3 / 4) - 4

    def mm_encode(payload):
        """复刻 generate_qr_stream.encode_to_video 的容量/k-m 推导（不碰 cv2/视频）。"""
        padded = mm.prepare_data(payload, mm_cs)
        tdb = len(padded) // mm_cs

        def find_k(total):
            best_k = 1
            for cand in range(1, min(total, mm.MAX_FEC_K) + 1):
                if total % cand == 0:
                    nb = total // cand
                    if 50 <= nb <= 500:
                        return cand
                    if best_k == 1 or abs(nb - 200) < abs(total // best_k - 200):
                        best_k = cand
            return best_k

        k = find_k(tdb)
        nb = tdb // k
        m = k + max(1, k // 2)
        if m <= k:
            m = k + 1
        tot = nb * m
        blocks = mm.fec_encode(padded, mm_cs, k, m)
        frames = [mm.make_frame_payload(i, tot, k, m, base64.b64encode(b).decode("ascii"))
                  for i, b in enumerate(blocks)]
        return frames, k, m, nb, tot

    sm_frames, sm_k, sm_m, sm_nb, sm_tot = mm_encode(base64.b64decode(case["input_b64"]))
    check("C2a map-marker 单组用例重跑与向量逐帧相同（nblocks=1）",
          sm_frames == case["frames"] and sm_nb == 1,
          "nblocks=%d k=%d m=%d" % (sm_nb, sm_k, sm_m))
    nf, k, m, ch = frames_to_chunks(sm_frames)
    got = qrvd.reassemble(nf, k, m, ch)
    check("C2b map-marker 单组（nblocks=1）可被 qr-video-decode 正确解回",
          sha(got) == case["reassembled_sha256"])

    kd = rt["known_defect_multigroup"]
    md_frames, md_k, md_m, md_nb, md_tot = mm_encode(input_bytes)
    check("C3 多组用例登记为已修复（向量 status=fixed，契约文档 §6.2）",
          kd.get("status") == "fixed" and kd.get("fixed_at") == "2026-10-09",
          "向量 status=%s；修法已实施应标 fixed，请复核契约文档 §6.2" % kd.get("status"))
    check("C4 多组输出本次重跑与向量逐帧相同（跨步分组形态未漂移）",
          md_nb > 1 and sha("\n".join(md_frames).encode("utf-8")) == kd["frames_sha256"],
          "nblocks=%d；实现或向量有改动，请重生成向量并复核契约文档" % md_nb)
    got2 = qrvd.reassemble(md_tot, md_k, md_m, frames_to_chunks(md_frames)[3])
    check("C5 map-marker 多组输出可被 qr-video-decode 重组、往返逐字节一致（缺陷②修复形态）",
          md_nb > 1 and got2 == input_bytes and sha(got2) == kd["reassembled_sha256"]
          and kd["output_equals_input"] and kd["output_length_matches"],
          "nblocks=%d 往返不一致；契约文档 §6.2 的修复被破坏" % md_nb)


    d = headers["defect_m256"]
    g = d["derivation_guard"]["repro"]
    payload = b"\xA5" * g["data_bytes"]
    repro_frames = re_encode(traj, payload)
    nf, rk, rm, ch = frames_to_chunks(repro_frames)
    got3 = qrvd.reassemble(nf, rk, rm, ch)
    check("C6 m=256 申报溢出已修复：复现输入（traj v%s/%s、恰 %d 块=%d B）帧头回读 m ≤ 255 且往返逐字节一致（契约文档 §6.1 修法①）"
          % (g["qr_version"], g["ec"], g["data_blocks"], g["data_bytes"]),
          d.get("status") == "fixed" and d.get("fixed_at") == "2026-10-09"
          and g["before"]["m"] == 256 and g["before"]["decodes"] is False
          and rm == g["after"]["m"] and rk == g["after"]["k"] and rm <= 255
          and len(repro_frames) == g["after"]["nframes"]
          and got3 == payload and sha(got3) == g["reassembled_sha256"]
          and g["roundtrip_equals_input"] is True,
          "修复前 m=256；本次 k=%d m=%d（向量 k=%d m=%d），往返%s；请复核契约文档 §6.1 并重生成向量"
          % (rk, rm, g["after"]["k"], g["after"]["m"], "一致" if got3 == payload else "不一致"))

    sweep = d["derivation_guard"]["plan_sweep"]
    viol = []
    for name, mod in (("trajectory-convert", traj), ("info-transfer", it)):
        ms = [mod._fec_plan(s)[3] for s in range(1, sweep[name]["sizes"][1] + 1)]
        if max(ms) != sweep[name]["max_m"] or sum(1 for x in ms if x >= 256) != 0:
            viol.append((name, max(ms), sweep[name]["max_m"]))
    cap = d["derivation_guard"]["map_marker_k_cap"]
    check("C7 三端参数推导恒不产出 m=256（traj/it 1..%d 扫描 + map-marker k≤%d、实产 m=%d/%d）"
          % (sweep["info-transfer"]["sizes"][1], cap["MAX_FEC_K"], sm_m, md_m),
          not viol and mm.MAX_FEC_K == cap["MAX_FEC_K"]
          and cap["max_m_at_cap"] <= 255 and sm_m <= 255 and md_m <= 255,
          "越界处=%s（契约文档 §6.1 的 m ≤ 255 约束被破坏）" % viol[:2])

    print("== D 段：v2（JZ2）帧头与 CRC 覆盖 ==")
    env_file = open(os.path.join(VEC, "v2_envelope.bin"), "rb").read()
    rebuilt = it.build_envelope_v2("text", "protocol-vector.txt", input_bytes.decode("utf-8"))
    check("D1 信封帧重建与向量逐字节相同", rebuilt == env_file)
    f = it.parse_frame_jz2(env_file)
    fields = {"comp": f["comp"], "mode": int(bool(f["mode"])), "is_fec": int(bool(f["is_fec"])),
              "seg_i": f["seg_i"], "seg_n": f["seg_n"], "meta_len": len(f["meta"]),
              "body_len": len(f["payload"])}
    check("D2 解析字段等于向量（magic/ver/flags/seg/meta_len/body_len）",
          fields == v2["envelope"]["fields"], "%s ≠ %s" % (fields, v2["envelope"]["fields"]))
    check("D3 magic 与版本字段就位", env_file[:3] == b"JZ2" and env_file[3] == 2
          and len(env_file[:21]) == it.JZ2_HEADER_LEN == 21)
    crc_in = int.from_bytes(env_file[17:21], "big")
    crc_calc = zlib.crc32(env_file[:17] + env_file[21:]) & 0xFFFFFFFF
    check("D4 CRC 覆盖范围 = 头前 17B + meta + payload（不含 crc 字段本身）",
          crc_in == crc_calc, "头内=%d 重算=%d" % (crc_in, crc_calc))
    bad = bytearray(env_file); bad[20] ^= 0xFF
    try:
        it.parse_frame_jz2(bytes(bad))
        check("D5 crc 被破坏时 Python 端拒收", False, "未抛错")
    except Exception:
        check("D5 crc 被破坏时 Python 端拒收", True)
    dec = it.parse_envelope_v2(env_file)
    check("D6 信封解码内容等于原输入", sha(dec["data"].encode("utf-8"))
          == v2["envelope"]["decoded_text_sha256"] and dec["fmt"] == v2["envelope"]["decoded_fmt"])

    raw = open(os.path.join(VEC, "v2_shares.bin"), "rb").read()
    shares, p = [], 0
    while p + 4 <= len(raw):
        n = int.from_bytes(raw[p:p + 4], "big"); p += 4
        shares.append(raw[p:p + n]); p += n
    check("D7 share 帧数量与向量一致", len(shares) == v2["shares"]["count"],
          "%d ≠ %d" % (len(shares), v2["shares"]["count"]))
    fs0 = it.parse_frame_jz2(shares[0])
    check("D8 share 帧 flags 声明 FEC 且字段可解析",
          bool(fs0["is_fec"]) and len(fs0["meta"]) == 2,
          "is_fec=%s meta_len=%d" % (fs0["is_fec"], len(fs0["meta"])))
    back = it.reassemble_qrtransfer_v2(list(shares))
    check("D9 v2 share 帧重组回原输入（已剥 4B 长度前缀）", sha(back) == v2["roundtrip_v2"]
          and sha(back) == sha(input_bytes))
    check("D10 share 帧 meta 内 k/m 与向量申报一致",
          (shares[0][21], shares[0][22]) == (v2["shares"]["k"], v2["shares"]["m"]),
          "meta=%s 向量=(k=%s,m=%s)" % (shares[0][21:23].hex(), v2["shares"]["k"], v2["shares"]["m"]))

    print("== E 段：worker.js（Node）交叉验证 ==")
    node = shutil.which("node")
    if not node:
        skip("E1 worker.js 解析 v2 信封帧", "本机无 node，跳过跨语言核对（不伪装成通过）")
    else:
        js_path = os.path.join(TMP_ROOT, "qrc_js_helper.js")
        with open(js_path, "w", encoding="utf-8") as fp:
            fp.write(JS_HELPER)
        r = subprocess.run([node, js_path,
                            os.path.join(REPO, "plugins", "qr-video-decode", "frontend"),
                            os.path.join(VEC, "v2_envelope.bin"), os.path.join(VEC, "v2_shares.bin")],
                           capture_output=True, text=True, timeout=120, encoding="utf-8")
        if r.returncode != 0 or not r.stdout.strip():
            check("E0 worker.js 沙箱可加载", False, (r.stderr or "")[-300:])
        else:
            j = json.loads(r.stdout.strip().splitlines()[-1])
            check("E0 worker.js 沙箱加载 + 测试钩子可用", j.get("hooks") is True)
            shared = ("comp", "mode", "is_fec", "seg_i", "seg_n", "meta_len", "body_len")
            js_subset = {k: j.get("fields", {}).get(k) for k in shared}
            py_subset = {k: v2["envelope"]["fields"].get(k) for k in shared}
            check("E1 JS 解析字段等于 Python（flags/comp/mode/is_fec/seg/meta_len/body_len）",
                  js_subset == py_subset,
                  "%s ≠ %s" % (js_subset, py_subset))
            meta_len = v2["envelope"]["fields"]["meta_len"]
            body_len = v2["envelope"]["fields"]["body_len"]
            py_meta = env_file[21:21 + meta_len]
            py_payload = env_file[21 + meta_len:21 + meta_len + body_len]
            check("E2 JS 与 Python 解出同一 meta/payload",
                  j.get("meta_hex") == py_meta.hex()
                  and j.get("payload_sha") == sha(py_payload),
                  "meta_ok=%s payload_ok=%s" % (j.get("meta_hex") == py_meta.hex(),
                                                j.get("payload_sha") == sha(py_payload)))
            py_crc = zlib.crc32(env_file[:17]) & 0xFFFFFFFF
            check("E3 CRC32 两端同值（覆盖头前 17 字节）",
                  j.get("crc_over_head") == py_crc,
                  "JS=%s Py=%s" % (j.get("crc_over_head"), py_crc))
            check("E4 crc 被破坏时 JS 端同样拒收", j.get("rejects_corrupt") is True,
                  str(j.get("reject_kind")))
            check("E5 JS 解析 share 帧数量与首帧字段一致",
                  j.get("share_count") == v2["shares"]["count"]
                  and j.get("first_share", {}).get("is_fec") == 1,
                  "JS=%s 向量=%d" % (j.get("first_share"), v2["shares"]["count"]))

    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    total = PASSES[0] + len(FAILS)
    print("\n== 二维码协议一致性台架：%d/%d 通过%s ==" % (
        PASSES[0], total, ("，跳过 %d" % len(SKIPS)) if SKIPS else ""))
    if FAILS:
        print("失败项：" + "、".join(FAILS))
        print("定位顺序：比对四端与 testdata/protocol/ 向量 → 判谁偏离 "
              "docs/design/二维码传输协议-契约文档.md → 同步改并重生成向量。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
