/* QR 视频流解码 —— Web Worker
 * 在独立线程中用 jsQR 解码单帧 QR，避免阻塞主线程（主线程负责取帧与进度 UI）。
 *
 * 协议双代自适应（对齐「信息传输」/ QR-transfer）：
 * - v1 文本帧：QR 文本前 12 个 base64 字符为帧头（帧序号/总帧数/k,m），其后为
 *   分块数据（base64 文本）；
 * - v2 二进制帧（JZ2，「信息传输」PROTOCOL_V2=True 的当前产物）：QR 内容为
 *   二进制字节，21 字节定长帧头 + meta + payload：
 *     magic "JZ2"(3B) | ver(1B) | flags(1B) | seg_i(3B) | seg_n(3B) |
 *     meta_len(2B) | body_len(4B) | crc32(4B)
 *   flags bit0-1=压缩算法(0=none 1=zlib 2=xz) | bit2=mode(0=exact 1=rebuild) |
 *   bit3=帧类型(0=信封帧 1=FEC share 帧)。视频流逐帧为 share 帧，meta=k,m，
 *   payload 为 zfec share 原始字节；帧头 CRC32 覆盖除 CRC 字段自身以外的帧头
 *   + meta + payload，损坏/截断帧在帧层被拒（按缺帧交给 zfec 容错）。
 *   jsQR 的 res.binaryData 提供逐字节还原（res.data 走 UTF-8 文本，对二进制有损）。
 *
 * 两种协议统一输出 {proto, index, n, fec:[k,m], payload(base64)}（payload 一律
 * 转 base64 文本），下游「主线程收集 → 后端 zfec 重组」对两代协议零分叉。
 *
 * 另：重组产物若为 JZ2 信封帧（v2），由主线程经 kind="unpack" 消息交回本线程
 * 解析（帧头/meta/压缩标志 → 解压 → 原始载荷），压缩解压用浏览器原生
 * DecompressionStream（zlib/deflate）；xz(LZMA2) 与 rebuild 拆包重组不在现有
 * 能力内，返回明确的限制说明。
 *
 * 注：JZ2 帧头常量目前是多份人肉拷贝（info-transfer/backend/routes.py、本文件、
 * APP 端），协议契约文档化属另一任务（D-7 报告已记）。
 */
importScripts("./jsqr.min.js?v=1");

/* ===================== v1 文本帧 ===================== */

function decodeIndex(b64) {
  const bin = atob(b64);
  let n = 0;
  for (let i = 0; i < bin.length; i++) n = (n << 8) | (bin.charCodeAt(i) & 255);
  return n;
}

function decodeFecInfo(b64) {
  const n = decodeIndex(b64);
  return [(n >> 8) & 255, n & 255];
}

function parseV1Frame(qrData) {
  if (typeof qrData !== "string" || qrData.length < 12) return null;
  return {
    proto: 1,
    index: decodeIndex(qrData.slice(0, 4)),
    n: decodeIndex(qrData.slice(4, 8)),
    fec: decodeFecInfo(qrData.slice(8, 12)),
    payload: qrData.slice(12),
  };
}

/* ===================== v2 JZ2 二进制帧 ===================== */

const JZ2_MAGIC_0 = 0x4a, JZ2_MAGIC_1 = 0x5a, JZ2_MAGIC_2 = 0x32; // "JZ2"
const JZ2_VERSION = 2;
const JZ2_HEADER_LEN = 21;        // 3+1+1+3+3+2+4+4
const JZ2_COMP_NONE = 0;
const JZ2_COMP_ZLIB = 1;
const JZ2_COMP_XZ = 2;            // LZMA2(xz)：浏览器无原生解压能力（见 unpackJz2）
const JZ2_FLAG_REBUILD = 0x04;
const JZ2_FLAG_FEC = 0x08;
const JZ2_FMT_NAMES = ["text", "markdown", "word", "excel", "file"];

/* CRC32（IEEE 802.3 反射式，与 zlib.crc32 一致）。 */
const CRC32_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32Update(c, b, start, end) {
  for (let i = start; i < end; i++) c = CRC32_TABLE[(c ^ b[i]) & 0xff] ^ (c >>> 8);
  return c >>> 0;
}

function crc32(b, start, end) {
  return (crc32Update(0xffffffff, b, start, end) ^ 0xffffffff) >>> 0;
}

function toUint8(x) {
  if (x instanceof Uint8Array) return x;
  if (x instanceof Uint8ClampedArray) return new Uint8Array(x.buffer, x.byteOffset, x.byteLength);
  if (Array.isArray(x)) return Uint8Array.from(x);
  if (x instanceof ArrayBuffer) return new Uint8Array(x);
  if (x && x.buffer instanceof ArrayBuffer) {
    return new Uint8Array(x.buffer, x.byteOffset || 0, x.byteLength);
  }
  throw new Error("不支持的字节序列类型");
}

function readU16(b, i) { return (b[i] << 8) | b[i + 1]; }
function readU24(b, i) { return (b[i] << 16) | (b[i + 1] << 8) | b[i + 2]; }
function readU32(b, i) {
  return ((b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]) >>> 0;
}

/* 解析 JZ2 帧；magic/版本/长度/CRC32 任一不符抛 Error（与后端 parse_frame_jz2 同口径）。 */
function parseJz2Frame(frame) {
  const b = toUint8(frame);
  if (b.length < JZ2_HEADER_LEN) throw new Error("JZ2 帧长度不足（数据损坏或被截断）");
  if (b[0] !== JZ2_MAGIC_0 || b[1] !== JZ2_MAGIC_1 || b[2] !== JZ2_MAGIC_2) {
    throw new Error("JZ2 帧标识不符");
  }
  if (b[3] !== JZ2_VERSION) throw new Error("JZ2 协议版本不受支持（" + b[3] + "）");
  const flags = b[4];
  const segI = readU24(b, 5);
  const segN = readU24(b, 8);
  const metaLen = readU16(b, 11);
  const bodyLen = readU32(b, 13);
  const crc = readU32(b, 17);
  if (b.length < JZ2_HEADER_LEN + metaLen + bodyLen) {
    throw new Error("JZ2 帧载荷不完整（数据被截断）");
  }
  const c = crc32Update(0xffffffff, b, 0, JZ2_HEADER_LEN - 4);
  const calc = (crc32Update(c, b, JZ2_HEADER_LEN, JZ2_HEADER_LEN + metaLen + bodyLen)
                ^ 0xffffffff) >>> 0;
  if (calc !== crc) throw new Error("JZ2 帧 CRC32 校验失败（数据损坏）");
  return {
    ver: b[3],
    flags,
    comp: flags & 0x03,
    mode: (flags & JZ2_FLAG_REBUILD) ? 1 : 0,
    isFec: !!(flags & JZ2_FLAG_FEC),
    segI, segN,
    meta: b.slice(JZ2_HEADER_LEN, JZ2_HEADER_LEN + metaLen),
    payload: b.slice(JZ2_HEADER_LEN + metaLen, JZ2_HEADER_LEN + metaLen + bodyLen),
  };
}

function b64FromBytes(u8) {
  let s = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < u8.length; i += CHUNK) {
    s += String.fromCharCode.apply(null, u8.subarray(i, i + CHUNK));
  }
  return btoa(s);
}

/* share 帧 → 统一输出；非 share 帧（信封帧等）返回 null。 */
function parseJz2ShareFrame(bin) {
  const f = parseJz2Frame(bin);
  if (!f.isFec || f.meta.length !== 2) return null;
  return {
    proto: 2,
    index: f.segI,
    n: f.segN,
    fec: [f.meta[0], f.meta[1]],
    payload: b64FromBytes(f.payload),
  };
}

/* jsQR 结果 → 统一帧结构：JZ2 魔数帧走 binaryData，否则回落 v1 文本帧。 */
function parseQrResult(res) {
  if (!res) return null;
  const bin = res.binaryData;
  if (bin && bin.length >= 3 &&
      bin[0] === JZ2_MAGIC_0 && bin[1] === JZ2_MAGIC_1 && bin[2] === JZ2_MAGIC_2) {
    return parseJz2ShareFrame(bin);
  }
  if (typeof res.data === "string") return parseV1Frame(res.data);
  return null;
}

/* ===================== v2 信封解码（重组产物 → 原始载荷） ===================== */

function inflateZlib(u8) {
  if (typeof DecompressionStream === "undefined") {
    return Promise.reject(new Error(
      "当前环境不支持原生解压（DecompressionStream），无法解析 zlib 压缩的 v2 载荷；"
      + "请升级浏览器/WebView2 后重试"));
  }
  const stream = new Blob([u8]).stream().pipeThrough(new DecompressionStream("deflate"));
  return new Response(stream).arrayBuffer().then((buf) => new Uint8Array(buf));
}

function utf8Decode(u8) {
  return new TextDecoder("utf-8").decode(u8);
}

/* 下载名：按 fmt 语义给出忠实后缀（word 精简载荷是正文文本、excel 是二维数组
 * JSON，均不是原文件格式，避免还原成 .docx/.xlsx 这类名不副实的文件）。
 * 名称来自解码载荷（不可信输入）：只取末级路径名并剔除控制/保留字符，防路径穿越。 */
function safeBaseName(name) {
  const s = String(name || "未命名").split(/[\\/]/).pop()
    .replace(/[\x00-\x1f<>:"|?*]/g, "_").trim().slice(0, 180);
  return s.replace(/^\.+$/, "") || "未命名";
}

function jz2DownloadName(fmt, name, ext) {
  const base = safeBaseName(name);
  const hasExt = /\.[^.]+$/.test(base);
  if (fmt === "file") {
    if (hasExt) return base;
    return base + "." + ((ext || "").replace(/^\./, "") || "bin");
  }
  if (fmt === "text" || fmt === "markdown") {
    if (hasExt) return base;
    return base + "." + (ext || (fmt === "markdown" ? "md" : "txt"));
  }
  if (fmt === "word") return hasExt ? base.replace(/\.[^.]+$/, ".txt") : base + ".txt";
  return hasExt ? base.replace(/\.[^.]+$/, ".json") : base + ".json";
}

/* v2 信封帧（重组产物）→ {ok, fmt, name, ext, name, downloadName, size, text, bytes}。
 * 失败返回 {ok:false, reason, message}（reason: corrupt/xz/rebuild）。 */
async function unpackJz2(frameBytes) {
  let f;
  try {
    f = parseJz2Frame(frameBytes);
  } catch (e) {
    return { ok: false, reason: "corrupt", message: "v2（JZ2）信封解析失败：" + e.message };
  }
  if (f.isFec) {
    return { ok: false, reason: "corrupt", message: "v2 帧类型不符（FEC 分帧不能直接作为信封）" };
  }
  const meta = f.meta;
  if (meta.length < 8) {
    return { ok: false, reason: "corrupt", message: "v2 信封元数据不完整" };
  }
  const fmt = JZ2_FMT_NAMES[meta[0]];
  if (!fmt) {
    return { ok: false, reason: "corrupt", message: "v2 信封声明了未知的文档格式：" + meta[0] };
  }
  const origLen = readU32(meta, 1);
  const nameLen = readU16(meta, 5);
  let pos = 7;
  const name = utf8Decode(meta.subarray(pos, pos + nameLen)) || "未命名";
  pos += nameLen;
  const extLen = pos < meta.length ? meta[pos] : 0;
  pos += 1;
  const ext = extLen ? utf8Decode(meta.subarray(pos, pos + extLen)) : "";
  let payload = f.payload;
  if (f.comp === JZ2_COMP_ZLIB) {
    try {
      payload = await inflateZlib(payload);
    } catch (e) {
      return { ok: false, reason: "corrupt", message: "压缩数据解码失败（" + e.message + "）" };
    }
  } else if (f.comp === JZ2_COMP_XZ) {
    return {
      ok: false, reason: "xz",
      message: "该 v2 产物使用 LZMA2(xz) 压缩，本插件网页端暂不支持解压。"
        + "请用「信息传输 → 信息解析」处理同一视频（后端 Python 支持 xz）、"
        + "或使用手机 APP 扫码还原。",
    };
  } else if (f.comp !== JZ2_COMP_NONE) {
    return { ok: false, reason: "corrupt", message: "v2 帧声明了未知的压缩算法：" + f.comp };
  }
  if (origLen && payload.length !== origLen) {
    return { ok: false, reason: "corrupt", message: "解压后数据长度不符（数据损坏）" };
  }
  if (f.mode) {
    return {
      ok: false, reason: "rebuild",
      message: "该 v2 产物为「拆包重组」模式（还原端重建 ZIP，内容等价），网页端暂不支持；"
        + "请用「信息传输 → 信息解析」处理同一视频、或使用手机 APP 扫码还原。",
    };
  }
  const text = fmt === "file" ? null : utf8Decode(payload);
  return {
    ok: true, fmt, name, ext, mode: f.mode, comp: f.comp,
    downloadName: jz2DownloadName(fmt, name, ext),
    size: payload.length,
    text,
    bytes: payload,   // 主线程用于下载（file 为原始文件字节，其余为载荷字节）
  };
}

/* ===================== Worker 消息入口 ===================== */

self.onmessage = (e) => {
  const msg = e.data;
  if (msg && msg.kind === "unpack") {
    // 重组产物（v2 信封帧）解析：帧头/meta/解压 → 原始载荷
    const bytes = toUint8(msg.data);
    unpackJz2(bytes).then((result) => {
      const transfer = (result && result.bytes) ? [result.bytes.buffer] : [];
      self.postMessage({ id: msg.id, kind: "unpack", result }, transfer);
    }, (err) => {
      self.postMessage({ id: msg.id, kind: "unpack",
        result: { ok: false, reason: "corrupt", message: "v2 信封解析异常：" + err.message } });
    });
    return;
  }
  const { id, data, width, height } = msg;
  const img = new Uint8ClampedArray(data);
  let parsed = null;
  try {
    const res = jsQR(img, width, height);
    if (res) parsed = parseQrResult(res);
  } catch (err) {
    // 单帧解码失败不致命，返回 null 由主线程继续
  }
  self.postMessage({ id, parsed }, [data]);
};

/* 测试钩子（tools/e2e 的验证脚本经 vm 沙箱取用，浏览器运行时无副作用）。 */
self.__qrvdTestHooks = {
  crc32, parseJz2Frame, parseV1Frame, parseQrResult, parseJz2ShareFrame,
  toUint8, b64FromBytes, unpackJz2,
};
