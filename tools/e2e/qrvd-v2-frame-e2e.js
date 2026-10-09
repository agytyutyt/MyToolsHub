#!/usr/bin/env node
/* QR 视频流解码 —— v2（JZ2 二进制帧）消费链路验证脚本（审计 D-7）
 *
 * 用法：
 *   node tools/e2e/qrvd-v2-frame-e2e.js                 # 核心 + 扩展（依赖齐全时）
 *   node tools/e2e/qrvd-v2-frame-e2e.js --core-only     # 只跑核心（纯 Node，无外部依赖）
 *   node tools/e2e/qrvd-v2-frame-e2e.js --extended-required
 *        扩展段依赖缺失时判失败而非跳过（CI/验收用）
 *
 * 覆盖：
 *   A 核心（纯 Node）：
 *     A1 按帧头规格造 JZ2 share 帧序列 → worker.js 解析逻辑 → 载荷逐字节一致
 *     A2 CRC32：测试内逐位实现（独立代码路径）与 worker 表实现交叉校验
 *     A3 损坏帧（位翻转 / 截断 / 版本不符 / 长度越界）一律拒收
 *     A4 信封帧（kind=env）不作为视频流帧接受
 *     A5 v1 文本帧样例仍按原语义解析（回归，含 onmessage 全链路）
 *     A6 v2 信封帧经 worker kind="unpack" 解析（comp=none/zlib 逐字节一致；
 *        comp=xz 与 mode=rebuild 返回明确限制说明——负向断言）
 *   B 扩展（需要 python + qrcode/zfec/cv2/numpy，缺省自动跳过）：
 *     B1 调用 info-transfer 真实编码函数产 v2 帧 → 渲染 PNG → 真 jsQR 读 binaryData →
 *        worker 解析 → qr-video-decode 真实后端 zfec 重组 → 信封解析 → 与原文件逐字节一致
 *     B2 同链条的 v1 全链路回归（生成/真 jsQR 解析/真实后端重组均与原字节一致）
 *     B3 真实帧上强制 zlib / xz 两种载荷：zlib 逐字节还原，xz 返回限制说明
 *
 * 不做（本脚本范围外）：浏览器 UI 交互、真实视频/摄像头链路、APP 端。
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");
const zlib = require("zlib");
const { spawnSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..", "..");
const FRONTEND = path.join(ROOT, "plugins", "qr-video-decode", "frontend");
const ARGS = new Set(process.argv.slice(2));
const CORE_ONLY = ARGS.has("--core-only");
const EXT_REQUIRED = ARGS.has("--extended-required");

let pass = 0;
const fails = [];
function ok(cond, name, extra) {
  if (cond) {
    pass++;
    console.log("  [PASS] " + name);
  } else {
    fails.push(name);
    console.log("  [FAIL] " + name + (extra !== undefined ? "  :: " + extra : ""));
  }
}
function eqBytes(a, b) {
  const ua = Buffer.from(a), ub = Buffer.from(b);
  return ua.length === ub.length && Buffer.compare(ua, ub) === 0;
}
function hexHead(u8, n) {
  return Buffer.from(u8).subarray(0, n || 16).toString("hex");
}
function sha256(buf) {
  return require("crypto").createHash("sha256").update(buf).digest("hex");
}

/* ===================== A 段：worker.js 沙箱 ===================== */

const workerSrc = fs.readFileSync(path.join(FRONTEND, "worker.js"), "utf8");
const realJsQR = require(path.join(FRONTEND, "jsqr.min.js"));

function makeSandbox(jsqrImpl) {
  const posted = [];
  const sandbox = {
    console,
    atob, btoa, TextDecoder, TextEncoder,
    Uint8Array, Uint8ClampedArray, Int8Array, ArrayBuffer, DataView, Buffer,
    Blob, Response, DecompressionStream, Promise, setTimeout, clearTimeout,
  };
  sandbox.self = sandbox;
  sandbox.importScripts = () => { sandbox.jsQR = jsqrImpl || realJsQR; };
  sandbox.postMessage = (msg) => { posted.push(msg); };
  vm.createContext(sandbox);
  vm.runInContext(workerSrc, sandbox, { filename: "worker.js" });
  return { sandbox, posted };
}

const { sandbox, posted } = makeSandbox();
const hooks = sandbox.__qrvdTestHooks;
ok(!!(hooks && hooks.parseQrResult && hooks.crc32 && hooks.unpackJz2),
   "A0 worker.js 沙箱加载 + 测试钩子可用");

/* 测试内独立 CRC32 实现（逐位，不用查表——与 worker 的表实现交叉验证） */
function crc32Bitwise(u8) {
  let c = 0xffffffff;
  for (let i = 0; i < u8.length; i++) {
    c ^= u8[i];
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
  }
  return (c ^ 0xffffffff) >>> 0;
}

/* 按帧头规格（routes.py JZ2_* / _jz2_frame）独立造帧 */
function buildJz2Frame(opts) {
  const { kind = "fec", segI = 0, segN = 1, meta = Buffer.alloc(0),
          payload = Buffer.alloc(0), comp = 0, mode = 0,
          ver = 2, magic = "JZ2" } = opts || {};
  const flags = (comp & 3) | (mode ? 0x04 : 0) | (kind === "fec" ? 0x08 : 0);
  const head = Buffer.alloc(17);
  head.write(magic, 0, "ascii");
  head[3] = ver;
  head[4] = flags;
  head.writeUIntBE(segI, 5, 3);
  head.writeUIntBE(segN, 8, 3);
  head.writeUInt16BE(meta.length, 11);
  head.writeUInt32BE(payload.length, 13);
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32Bitwise(Buffer.concat([head, meta, payload])), 0);
  return Buffer.concat([head, crcBuf, meta, payload]);
}

function envMeta(fmtCode, name, ext, origLen) {
  const nameB = Buffer.from(name || "未命名", "utf8");
  const extB = Buffer.from(ext || "", "utf8");
  return Buffer.concat([
    Buffer.from([fmtCode]),
    Buffer.from([(origLen >>> 24) & 255, (origLen >>> 16) & 255, (origLen >>> 8) & 255, origLen & 255]),
    Buffer.from([(nameB.length >> 8) & 255, nameB.length & 255]),
    nameB,
    Buffer.from([extB.length]),
    extB,
  ]);
}

function rndBytes(seed, n) {
  let s = seed >>> 0;
  const out = Buffer.alloc(n);
  for (let i = 0; i < n; i++) {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    out[i] = s >>> 24;
  }
  return out;
}

async function unpackViaWorker(data, id) {
  const before = posted.length;
  sandbox.onmessage({ data: { id, kind: "unpack", data } });
  const t0 = Date.now();
  while (Date.now() - t0 < 8000) {
    const hit = posted.slice(before).find((m) => m.id === id && m.kind === "unpack");
    if (hit) return hit.result;
    await new Promise((r) => setTimeout(r, 5));
  }
  throw new Error("worker unpack 超时（id=" + id + "）");
}

async function runCore() {
  console.log("\n== A 段：纯 Node 核心验证 ==");

  /* ---- A2 CRC 交叉校验 ---- */
  {
    let same = true;
    for (let n = 0; n <= 300; n += 7) {
      const b = rndBytes(0x9e3779b9 + n, n);
      if (hooks.crc32(b, 0, n) !== crc32Bitwise(b)) { same = false; break; }
    }
    const empty = hooks.crc32(Buffer.alloc(0), 0, 0) === 0;
    ok(same && empty, "A2 worker 表驱动 CRC32 ≡ 测试逐位 CRC32（含空串=0）");
  }

  /* ---- A1 JZ2 share 帧序列：解析 → 载荷逐字节一致 ---- */
  const K = 3, M = 5;
  const shares = [rndBytes(1, 700), rndBytes(2, 512), rndBytes(3, 513),
                  rndBytes(4, 700), rndBytes(5, 1)];
  const frames = shares.map((p, i) =>
    buildJz2Frame({ kind: "fec", segI: i, segN: shares.length, meta: Buffer.from([K, M]), payload: p }));
  {
    const parsedAll = frames.map((f) => hooks.parseQrResult({ binaryData: new Uint8Array(f) }));
    ok(parsedAll.every((p) => p && p.proto === 2 && p.n === shares.length &&
        p.fec[0] === K && p.fec[1] === M), "A1.1 帧序列解析出 proto=2 / n / k,m");
    let byteExact = true, idxExact = true;
    parsedAll.forEach((p, i) => {
      if (!p || p.index !== i) idxExact = false;
      if (!p || !eqBytes(Buffer.from(p.payload, "base64"), shares[i])) byteExact = false;
    });
    ok(idxExact, "A1.2 帧序号（seg_i）逐帧还原");
    ok(byteExact, "A1.3 share 载荷逐字节一致（含 0x00/0xFF 高熵段）");
    // 后端 data_list 落位语义：按 seg_i 归位后逐帧与原 share 一致
    const dataList = new Array(shares.length).fill(null);
    parsedAll.forEach((p) => { dataList[p.index] = Buffer.from(p.payload, "base64"); });
    ok(dataList.every((b, i) => b && eqBytes(b, shares[i])),
       "A1.4 模拟后端 data_list[seg_i] 落位后与原 share 全等");
    // jsQR 结果同时含有损文本时，必须优先 binaryData（二进制帧文本必然有损）
    const lossy = { data: "JZ2\uFFFD\uFFFD mangled", binaryData: new Uint8Array(frames[0]) };
    const pref = hooks.parseQrResult(lossy);
    ok(pref && eqBytes(Buffer.from(pref.payload, "base64"), shares[0]),
       "A1.5 帧结果含 data（有损文本）时仍优先取 binaryData");
  }

  /* ---- A3 损坏帧拒收 ---- */
  {
    const good = frames[0];
    const flip = Buffer.from(good); flip[25] ^= 0x01;              // payload 位翻转
    const flipHead = Buffer.from(good); flipHead[6] ^= 0x01;       // 帧头位翻转（seg_i）
    const trunc = good.subarray(0, good.length - 3);               // 截断
    const badVer = buildJz2Frame({ kind: "fec", segI: 0, segN: 1, meta: Buffer.from([K, M]),
                                   payload: shares[1], ver: 3 });
    const overflow = Buffer.from(good);                            // body_len 越界
    overflow.writeUInt32BE(0x00ffffff, 13);
    const cases = [
      ["payload 位翻转", flip], ["帧头位翻转", flipHead], ["载荷截断", trunc],
      ["协议版本不符", badVer], ["body_len 越界", overflow],
    ];
    let allRejected = true, detail = "";
    for (const [name, buf] of cases) {
      let threw = false;
      try { hooks.parseQrResult({ binaryData: new Uint8Array(buf) }); } catch (e) { threw = true; }
      if (!threw) { allRejected = false; detail += name + "; "; }
    }
    ok(allRejected, "A3 损坏/截断/版本不符帧全部拒收（CRC+长度+版本）", detail);
    // 全链路：worker.onmessage 对损坏帧返回 parsed=null（不抛）
    const { sandbox: s2, posted: p2 } = makeSandbox(() =>
      ({ data: "junk", binaryData: new Uint8Array(flip) }));
    s2.onmessage({ data: { id: 77, data: new ArrayBuffer(4), width: 2, height: 2 } });
    const msg = p2.find((m) => m.id === 77);
    ok(msg && msg.parsed === null, "A3.2 onmessage 全链路：损坏帧 → parsed=null");
    const { sandbox: s3, posted: p3 } = makeSandbox(() =>
      ({ data: "", binaryData: new Uint8Array(frames[1]) }));
    s3.onmessage({ data: { id: 78, data: new ArrayBuffer(4), width: 2, height: 2 } });
    const msg3 = p3.find((m) => m.id === 78);
    ok(msg3 && msg3.parsed && msg3.parsed.proto === 2 && msg3.parsed.index === 1,
       "A3.3 onmessage 全链路：合法 JZ2 帧 → parsed 正常");
  }

  /* ---- A4 信封帧不作为视频流帧 ---- */
  {
    const env = buildJz2Frame({ kind: "env", segI: 0, segN: 1,
      meta: envMeta(4, "x.bin", "bin", 3), payload: Buffer.from([1, 2, 3]) });
    const p = hooks.parseQrResult({ binaryData: new Uint8Array(env) });
    ok(p === null, "A4 信封帧（非 FEC）不作为视频流分块");
  }

  /* ---- A5 v1 文本帧回归 ---- */
  {
    function v1Index(n) { const b = Buffer.alloc(3); b.writeUIntBE(n, 0, 3); return b.toString("base64"); }
    const v1Payload = Buffer.from("v1 回归载荷 abc123").toString("base64");
    const text = v1Index(2) + v1Index(9) + v1Index((4 << 8) + 6) + v1Payload;
    const p = hooks.parseQrResult({ data: text });
    const parsed = p && p.proto === 1 && p.index === 2 && p.n === 9 &&
      p.fec[0] === 4 && p.fec[1] === 6 && p.payload === v1Payload;
    ok(parsed, "A5.1 v1 文本帧按原语义解析（序号/总帧/纠错参数/载荷）");
    ok(hooks.parseQrResult({ data: "AAAA" }) === null, "A5.2 v1 短帧（<12 字符）返回 null");
    const { sandbox: s4, posted: p4 } = makeSandbox(() => ({ data: text, binaryData: null }));
    s4.onmessage({ data: { id: 79, data: new ArrayBuffer(4), width: 2, height: 2 } });
    const m4 = p4.find((m) => m.id === 79);
    ok(m4 && m4.parsed && m4.parsed.proto === 1 && m4.parsed.payload === v1Payload,
       "A5.3 onmessage 全链路：v1 文本帧零回归");
  }

  /* ---- A6 v2 信封帧（unpack 消息） ---- */
  {
    const rawFile = rndBytes(42, 1024);
    // comp=none / fmt=file
    const fNone = buildJz2Frame({ kind: "env", meta: envMeta(4, "e2e-file.bin", "bin", rawFile.length),
                                  payload: rawFile, comp: 0 });
    const rNone = await unpackViaWorker(new Uint8Array(fNone), 101);
    ok(rNone && rNone.ok && eqBytes(rNone.bytes, rawFile) && rNone.downloadName === "e2e-file.bin",
       "A6.1 comp=none 信封帧：载荷逐字节一致 + 下载名保留原名",
       rNone && (rNone.message || rNone.downloadName));
    // comp=zlib（zlib 格式流，与 python zlib.compress / 后端 _compress_best 同格式）
    const zpayload = zlib.deflateSync(rawFile, { level: 9 });
    const fZ = buildJz2Frame({ kind: "env", meta: envMeta(4, "e2e-file.bin", "bin", rawFile.length),
                               payload: zpayload, comp: 1 });
    const rZ = await unpackViaWorker(new Uint8Array(fZ), 102);
    ok(rZ && rZ.ok && eqBytes(rZ.bytes, rawFile), "A6.2 comp=zlib 信封帧：解压后逐字节一致",
       rZ && rZ.message);
    // comp=xz：现有能力外 → 明确的限制说明（负向断言）
    const fX = buildJz2Frame({ kind: "env", meta: envMeta(4, "e2e-file.bin", "bin", rawFile.length),
                               payload: Buffer.from([0xfd, 0x37, 0x7a, 0x58, 0x5a, 0x00]),
                               comp: 2 });
    const rX = await unpackViaWorker(new Uint8Array(fX), 103);
    ok(rX && !rX.ok && rX.reason === "xz" && /LZMA2|信息解析/.test(rX.message || ""),
       "A6.3 comp=xz：返回 reason=xz 与替代路径说明（不误报成功）");
    // mode=rebuild：限制说明
    const fR = buildJz2Frame({ kind: "env", meta: envMeta(4, "a.docx", "docx", rawFile.length),
                               payload: rawFile, comp: 0, mode: 1 });
    const rR = await unpackViaWorker(new Uint8Array(fR), 104);
    ok(rR && !rR.ok && rR.reason === "rebuild", "A6.4 mode=rebuild：返回 reason=rebuild 限制说明");
    // 文本类：fmt=text，下载名补 .txt
    const txt = Buffer.from("v2 文本载荷\n第二行", "utf8");
    const fT = buildJz2Frame({ kind: "env", meta: envMeta(0, "note", "txt", txt.length),
                               payload: txt, comp: 0 });
    const rT = await unpackViaWorker(new Uint8Array(fT), 105);
    ok(rT && rT.ok && rT.text === "v2 文本载荷\n第二行" && rT.downloadName === "note.txt",
       "A6.5 fmt=text：文本还原 + 下载名 note.txt");
    // orig_len 不符 → 拒收
    const fL = buildJz2Frame({ kind: "env", meta: envMeta(4, "e2e-file.bin", "bin", rawFile.length + 1),
                               payload: rawFile, comp: 0 });
    const rL = await unpackViaWorker(new Uint8Array(fL), 106);
    ok(rL && !rL.ok && rL.reason === "corrupt" && /长度不符/.test(rL.message || ""),
       "A6.6 orig_len 二次校验失败 → 拒收");
    // 信封帧 CRC 损坏 → 拒收
    const bad = Buffer.from(fNone); bad[bad.length - 1] ^= 0x80;
    const rB = await unpackViaWorker(new Uint8Array(bad), 107);
    ok(rB && !rB.ok && rB.reason === "corrupt" && /CRC32/.test(rB.message || ""),
       "A6.7 信封帧 CRC 损坏 → 拒收");
    // 未知 fmt 码 → 拒收
    const fF = buildJz2Frame({ kind: "env", meta: envMeta(9, "x", "", 3), payload: Buffer.from([1, 2, 3]) });
    const rF = await unpackViaWorker(new Uint8Array(fF), 108);
    ok(rF && !rF.ok && rF.reason === "corrupt", "A6.8 未知 fmt 码 → 拒收");
    // 传 ArrayBuffer（app.js 以 raw.buffer 传递）亦可
    const ab = new Uint8Array(fZ).slice().buffer;
    const rAB = await unpackViaWorker(ab, 109);
    ok(rAB && rAB.ok && eqBytes(rAB.bytes, rawFile), "A6.9 unpack 入参 ArrayBuffer 形态可用");
    // 文件名来自不可信载荷：路径穿越/控制字符必须被清洗（只取末级名）
    const evil = Buffer.from("x", "utf8");
    const fE = buildJz2Frame({ kind: "env",
      meta: envMeta(0, "../../evil\\name\u0001bad", "txt", evil.length), payload: evil });
    const rE = await unpackViaWorker(new Uint8Array(fE), 110);
    ok(rE && rE.ok && rE.downloadName === "name_bad.txt" &&
       !/[\x00-\x1f\\/:*?"<>|]/.test(rE.downloadName),
       "A6.10 下载名清洗：路径穿越/控制字符被剔除", rE && rE.downloadName);
  }
}

/* ===================== B 段：真实编码器 + 真 jsQR + 真实后端 ===================== */

const PY_HELPER = String.raw`
# -*- coding: utf-8 -*-
"""qrvd-v2-frame-e2e.js 的扩展段助手：调用真实插件后端函数生成/校验，本身不做断言。"""
import base64, importlib.util, json, os, sys, zlib

mode, ROOT, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, ROOT)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


import numpy as np
import cv2

IT = _load("it_routes_e2e", "plugins/info-transfer/backend/routes.py")
QRD = _load("qrd_routes_e2e", "plugins/qr-video-decode/backend/routes.py")

VERSION = 20
ERR = IT.qrcode_constants.ERROR_CORRECT_L


def render_rgba(code_bytes, is_v2, tag):
    """渲染 + 生成端自检（口径同 encode_to_video：逐字节校验，失败换 mask）。"""
    png = IT._qr_png_bytes(code_bytes, VERSION, ERR)
    verify = IT._zxing_can_decode if is_v2 else IT._cv2_can_decode
    mask_used = "auto"
    if not verify(png, expected=code_bytes):
        for mask in range(8):
            png = IT._qr_png_bytes(code_bytes, VERSION, ERR, mask_pattern=mask)
            if verify(png, expected=code_bytes):
                mask_used = mask
                break
        else:
            raise RuntimeError("生成端自检失败：" + tag)
    gray = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    rgba = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGBA)
    fp = os.path.join(OUT, "frames", tag + ".raw")
    with open(fp, "wb") as f:
        f.write(rgba.tobytes())
    return fp, int(rgba.shape[1]), int(rgba.shape[0]), mask_used


def prepare():
    os.makedirs(os.path.join(OUT, "frames"), exist_ok=True)
    plan = {"version": VERSION}

    # ---- v2：真实 build_envelope_v2 + 真实 FEC/帧编码（与 encode_to_video 同路径） ----
    # 载荷 = ASCII 头 + 0..255 全字节段 + 6KB 随机字节：三个压缩分支都被真实编码器
    # 走过一遍（随机段不可压缩 → comp=none），且随机字节对 jsQR binaryData 最严格。
    rng = __import__("random").Random(7)
    raw2 = b"JZ2 e2e payload |" + bytes(range(256)) + rng.randbytes(6000)
    data_b64 = base64.b64encode(raw2).decode("ascii")
    env = IT.build_envelope_v2("file", "e2e-payload.bin", data_b64, "bin", rebuild=False)
    f = IT.parse_frame_jz2(env)
    payload = len(env).to_bytes(4, "big") + env
    chunks = IT._chunk_data(payload, VERSION, ERR,
                            maxchunk=IT._max_chunk_size_v2(VERSION, ERR, 2))
    shares, k, m = IT._fec_encode(chunks)
    codes = IT._qrformat_encode_v2(shares, k, m)          # list[bytes]
    v2_frames = []
    for i, code in enumerate(codes):
        fp, w, h, mask_used = render_rgba(code, True, "v2_%02d" % i)
        v2_frames.append({"seg_i": i, "seg_n": len(codes), "k": k, "m": m,
                          "code_b64": base64.b64encode(code).decode("ascii"),
                          "rgba": fp, "w": w, "h": h, "mask": mask_used})
    plan["v2"] = {"comp": f["comp"], "k": k, "m": m, "total": len(codes),
                  "env_len": len(env), "frames": v2_frames}
    with open(os.path.join(OUT, "expected_v2.bin"), "wb") as fo:
        fo.write(raw2)

    # ---- v1：PROTOCOL_V2=False 路径（旧产物），真实编码 ----
    IT.PROTOCOL_V2 = False
    try:
        env1 = IT.build_envelope("text", "e2e-v1", "v1 回归载荷 " + "abc123" * 50, None, False)
    finally:
        IT.PROTOCOL_V2 = True
    payload1 = len(env1).to_bytes(4, "big") + env1
    chunks1 = IT._chunk_data(payload1, VERSION, ERR)
    shares1, k1, m1 = IT._fec_encode(chunks1)
    codes1 = IT._qrformat_encode(shares1, k1, m1)         # list[str]
    v1_frames = []
    for i, code_txt in enumerate(codes1):
        code_b = code_txt.encode("ascii")
        fp, w, h, mask_used = render_rgba(code_b, False, "v1_%02d" % i)
        v1_frames.append({"index": i, "n": len(codes1), "k": k1, "m": m1,
                          "code_text": code_txt, "rgba": fp, "w": w, "h": h,
                          "mask": mask_used})
    plan["v1"] = {"k": k1, "m": m1, "total": len(codes1), "frames": v1_frames}
    with open(os.path.join(OUT, "expected_v1.bin"), "wb") as fo:
        fo.write(env1)

    # ---- B3：强制 zlib / xz 载荷的信封帧（真实 _jz2_frame / _env_meta_v2 组装） ----
    raw3 = ("强制用例：" + "数据段 " * 200).encode("utf-8")
    meta3 = IT._env_meta_v2("file", "force-zlib.bin", "bin", len(raw3))
    zframe = IT._jz2_frame("env", 0, 1, meta3, zlib.compress(raw3, 9),
                           comp=IT.JZ2_COMP_ZLIB, mode=0)
    xframe = IT._jz2_frame("env", 0, 1, meta3, IT._xz_compress(raw3),
                           comp=IT.JZ2_COMP_XZ, mode=0)
    with open(os.path.join(OUT, "force_zlib.jz2"), "wb") as fo:
        fo.write(zframe)
    with open(os.path.join(OUT, "force_xz.jz2"), "wb") as fo:
        fo.write(xframe)
    with open(os.path.join(OUT, "force_expected.bin"), "wb") as fo:
        fo.write(raw3)

    # ---- 真实 build_envelope_v2 的 xz 产物（文本载荷必选 xz）：证明产物合法且
    #      浏览器端属能力边界（后端 parse_envelope_v2 可完整还原） ----
    raw_txt = ("信息传输 v2 xz 真实产物验证。" * 900).encode("utf-8")
    env_xz = IT.build_envelope_v2("text", "xz-note", raw_txt.decode("utf-8"), None, False)
    comp_xz = IT.parse_frame_jz2(env_xz)["comp"]
    with open(os.path.join(OUT, "real_xz.jz2"), "wb") as fo:
        fo.write(env_xz)
    with open(os.path.join(OUT, "real_xz_expected.bin"), "wb") as fo:
        fo.write(raw_txt)
    plan["real_xz"] = {"comp": comp_xz, "text_len": len(raw_txt)}

    with open(os.path.join(OUT, "plan.json"), "w", encoding="utf-8") as fo:
        json.dump(plan, fo, ensure_ascii=False)
    print(json.dumps({"ok": True, "output": OUT}))


def verify():
    with open(os.path.join(OUT, "plan.json"), encoding="utf-8") as fi:
        plan = json.load(fi)
    with open(os.path.join(OUT, "decoded_v2_codes.json"), encoding="utf-8") as fi:
        codes_v2 = [base64.b64decode(s) for s in json.load(fi)]
    with open(os.path.join(OUT, "decoded_v1.json"), encoding="utf-8") as fi:
        v1 = json.load(fi)

    res = {}
    # v2：真实后端重组（JZ2 share 帧）→ 真实信封解析
    env_frame = IT.reassemble_qrtransfer_v2(codes_v2)
    with open(os.path.join(OUT, "recovered_v2_env.jz2"), "wb") as fo:
        fo.write(env_frame)
    env_d = IT.parse_envelope_v2(env_frame)
    got2 = base64.b64decode(env_d["data"])
    with open(os.path.join(OUT, "expected_v2.bin"), "rb") as fi:
        exp2 = fi.read()
    res["v2"] = {"match": got2 == exp2, "bytes": len(got2),
                 "sha256": __import__("hashlib").sha256(got2).hexdigest(),
                 "fmt": env_d.get("fmt"), "name": env_d.get("name")}
    # v1：真实 qr-video-decode 后端重组（旧路径）
    got1 = QRD.reassemble(v1["n"], v1["k"], v1["m"], v1["chunks"])
    with open(os.path.join(OUT, "expected_v1.bin"), "rb") as fi:
        exp1 = fi.read()
    res["v1"] = {"match": got1 == exp1, "bytes": len(got1)}
    # 真实 xz 产物：后端（Python 全能力）可完整还原——证明浏览器端限制是消费端能力边界
    with open(os.path.join(OUT, "real_xz.jz2"), "rb") as fi:
        env_xz = fi.read()
    with open(os.path.join(OUT, "real_xz_expected.bin"), "rb") as fi:
        exp_xz = fi.read()
    env_xzd = IT.parse_envelope_v2(env_xz)
    got_xz = env_xzd["data"].encode("utf-8")
    res["real_xz"] = {"match": got_xz == exp_xz, "bytes": len(got_xz),
                      "comp": plan["real_xz"]["comp"]}
    res["ok"] = res["v2"]["match"] and res["v1"]["match"] and res["real_xz"]["match"]
    print(json.dumps(res, ensure_ascii=False))


if mode == "prepare":
    prepare()
elif mode == "verify":
    verify()
else:
    raise SystemExit("unknown mode: " + mode)
`;

function findPython() {
  for (const cmd of [["python", []], ["py", ["-3"]]]) {
    const r = spawnSync(cmd[0], cmd[1].concat(["-c", "import qrcode,zfec,cv2,numpy;print('ok')"]),
                        { encoding: "utf8", timeout: 120000 });
    if (r.status === 0 && /ok/.test(r.stdout || "")) return cmd;
  }
  return null;
}

async function runExtended() {
  console.log("\n== B 段：真实编码器 + 真 jsQR + 真实后端（扩展） ==");
  const py = findPython();
  if (!py) {
    const msg = "B 段跳过：未找到可用的 python + qrcode/zfec/cv2/numpy 环境";
    if (EXT_REQUIRED) { fails.push(msg); console.log("  [FAIL] " + msg); }
    else console.log("  [SKIP] " + msg);
    return;
  }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "qrvd-v2-e2e-"));
  const helper = path.join(tmp, "helper.py");
  fs.writeFileSync(helper, PY_HELPER, "utf8");
  const pyArgs = (mode) => py[1].concat([helper, mode, ROOT, tmp]);
  const run = (mode) => {
    const r = spawnSync(py[0], pyArgs(mode), { encoding: "utf8", timeout: 600000,
                                               maxBuffer: 64 * 1024 * 1024 });
    if (r.status !== 0) {
      throw new Error("python " + mode + " 失败（status=" + r.status + "）：" +
                      (r.stderr || "").slice(-1500));
    }
    const m = (r.stdout || "").match(/\{[\s\S]*\}\s*$/);
    if (!m) throw new Error("python " + mode + " 无 JSON 输出：" + (r.stdout || "").slice(-500));
    return JSON.parse(m[0]);
  };

  console.log("  临时产物目录：" + tmp);
  const prep = run("prepare");
  ok(prep && prep.ok, "B0 真实编码器生成 v2/v1 帧序列 + 渲染 PNG（生成端自检通过）");
  const plan = JSON.parse(fs.readFileSync(path.join(tmp, "plan.json"), "utf8"));

  // ---- B1/B2：真 jsQR 解码真实渲染帧 → worker 解析 ----
  const decodedV2Codes = [];
  const chunksV1 = {};
  let v1N = 0, v1K = 0, v1M = 0;
  let v2FrameErr = 0, v1FrameErr = 0;
  for (const fr of plan.v2.frames) {
    const buf = fs.readFileSync(fr.rgba);
    const res = realJsQR(new Uint8ClampedArray(buf), fr.w, fr.h);
    if (!res || !res.binaryData) { v2FrameErr++; continue; }
    const orig = Buffer.from(fr.code_b64, "base64");
    if (!eqBytes(res.binaryData, orig)) { v2FrameErr++; continue; }
    decodedV2Codes.push(res.binaryData);
    const parsed = hooks.parseQrResult({ data: res.data, binaryData: new Uint8Array(res.binaryData) });
    if (!parsed || parsed.proto !== 2 || parsed.index !== fr.seg_i ||
        parsed.n !== fr.seg_n || parsed.fec[0] !== fr.k || parsed.fec[1] !== fr.m) {
      v2FrameErr++; continue;
    }
  }
  ok(v2FrameErr === 0 && decodedV2Codes.length === plan.v2.frames.length,
     "B1.1 真 jsQR 对 " + plan.v2.frames.length + " 个 v2 二进制帧全部解码且 binaryData 与编码端逐字节一致",
     "失败 " + v2FrameErr + " 帧");
  ok(typeof plan.v2.comp === "number",
     "B1.2 真实编码器选用压缩算法 comp=" + plan.v2.comp +
     "（0=none 1=zlib 2=xz）；k=" + plan.v2.k + " m=" + plan.v2.m);
  fs.writeFileSync(path.join(tmp, "decoded_v2_codes.json"),
                   JSON.stringify(decodedV2Codes.map((c) => Buffer.from(c).toString("base64"))));

  for (const fr of plan.v1.frames) {
    const buf = fs.readFileSync(fr.rgba);
    const res = realJsQR(new Uint8ClampedArray(buf), fr.w, fr.h);
    if (!res || res.data !== fr.code_text) { v1FrameErr++; continue; }
    const parsed = hooks.parseQrResult({ data: res.data, binaryData: res.binaryData });
    if (!parsed || parsed.proto !== 1 || parsed.index !== fr.index ||
        parsed.n !== fr.n || parsed.fec[0] !== fr.k || parsed.fec[1] !== fr.m) {
      v1FrameErr++; continue;
    }
    chunksV1[parsed.index] = parsed.payload;
    v1N = parsed.n; v1K = parsed.fec[0]; v1M = parsed.fec[1];
  }
  ok(v1FrameErr === 0 && Object.keys(chunksV1).length === plan.v1.frames.length,
     "B2.1 真 jsQR 对 " + plan.v1.frames.length + " 个 v1 文本帧解码零回归（文本逐字符一致）",
     "失败 " + v1FrameErr + " 帧");
  fs.writeFileSync(path.join(tmp, "decoded_v1.json"),
                   JSON.stringify({ n: v1N, k: v1K, m: v1M, chunks: chunksV1 }));

  // ---- B1.3/B2.2：真实后端重组 + 信封解析 ----
  const ver = run("verify");
  ok(ver.ok && ver.v2.match,
     "B1.3 真实后端 zfec 重组 + parse_envelope_v2 → 与原文件逐字节一致（" +
     ver.v2.bytes + " B, sha256=" + String(ver.v2.sha256).slice(0, 16) + "…）");
  ok(ver.v1.match, "B2.2 真实 qr-video-decode 后端重组（v1 路径）→ 与原字节一致（" +
     ver.v1.bytes + " B）");
  ok(ver.real_xz && ver.real_xz.match && ver.real_xz.comp === 2,
     "B1.5 真实 build_envelope_v2 的 xz 产物（comp=2）经后端 parse_envelope_v2 " +
     "完整还原（证明产物合法，浏览器端限制属消费端能力边界）");

  // ---- B3：真实帧的 worker 解包（zlib 正向 / xz 限制说明） ----
  const zframe = new Uint8Array(fs.readFileSync(path.join(tmp, "force_zlib.jz2")));
  const xframe = new Uint8Array(fs.readFileSync(path.join(tmp, "force_xz.jz2")));
  const expected3 = fs.readFileSync(path.join(tmp, "force_expected.bin"));
  const rz = await unpackViaWorker(new Uint8Array(zframe).slice().buffer, 201);
  ok(rz && rz.ok && eqBytes(rz.bytes, expected3) && rz.comp === 1,
     "B3.1 真实 _jz2_frame(comp=zlib) → worker 解包逐字节一致（sha256=" +
     sha256(expected3).slice(0, 16) + "…）", rz && rz.message);
  const rx = await unpackViaWorker(new Uint8Array(xframe).slice().buffer, 202);
  ok(rx && !rx.ok && rx.reason === "xz",
     "B3.2 真实 _xz_compress 帧 → worker 明确返回 xz 限制说明（现有能力边界，已文档化）");

  // ---- B1.4：真实重组产物（JZ2 信封帧）经 worker 解包 ----
  const recFrame = new Uint8Array(fs.readFileSync(path.join(tmp, "recovered_v2_env.jz2")));
  const rRec = await unpackViaWorker(new Uint8Array(recFrame).slice().buffer, 203);
  if (plan.v2.comp === 2) {
    ok(rRec && !rRec.ok && rRec.reason === "xz",
       "B1.4 真实 v2 产物 comp=xz → worker 返回限制说明（替代路径：信息传输「信息解析」/APP）");
  } else {
    const exp2 = fs.readFileSync(path.join(tmp, "expected_v2.bin"));
    ok(rRec && rRec.ok && eqBytes(rRec.bytes, exp2),
       "B1.4 真实 v2 产物（comp=" + plan.v2.comp + "）经 worker 解包 → 与原文件逐字节一致");
  }
  // ---- real_xz：真实 xz 产物在 worker 端必须显式报限制（不得误判为损坏/成功） ----
  const realXz = new Uint8Array(fs.readFileSync(path.join(tmp, "real_xz.jz2")));
  const rXzReal = await unpackViaWorker(new Uint8Array(realXz).slice().buffer, 204);
  ok(rXzReal && !rXzReal.ok && rXzReal.reason === "xz",
     "B1.6 真实 build_envelope_v2(xz) → worker 返回 reason=xz 限制说明（后端/APP 可解）");
}

(async () => {
  console.log("QR 视频流解码 v2（JZ2）消费链路验证 —— worker.js/app.js");
  await runCore();
  if (!CORE_ONLY) {
    try {
      await runExtended();
    } catch (e) {
      if (EXT_REQUIRED) { fails.push("B 段执行异常"); console.log("  [FAIL] B 段执行异常：" + e.message); }
      else console.log("  [SKIP] B 段执行异常（按跳过处理）：" + e.message);
    }
  }
  console.log("\n== 汇总 ==");
  console.log("  通过 " + pass + " 项，失败 " + fails.length + " 项");
  if (fails.length) {
    for (const f of fails) console.log("   - " + f);
    process.exit(1);
  }
  console.log("  结论：全部通过");
})().catch((e) => {
  console.error("脚本异常：" + (e && e.stack ? e.stack : e));
  process.exit(2);
});
