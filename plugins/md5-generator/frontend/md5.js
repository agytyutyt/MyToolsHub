(function (root) {
  "use strict";

  /* ------------------------------------------------------------------ */
  /* MD5 core (RFC 1321) —— 纯 JS，无依赖                              */
  /* ------------------------------------------------------------------ */
  const S = [
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
  ];
  const K = new Uint32Array(64);
  for (let i = 0; i < 64; i++) K[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296);

  function add32(a, b) {
    return (a + b) & 0xffffffff;
  }
  function rol32(n, c) {
    return (n << c) | (n >>> (32 - c));
  }
  function cmn(q, a, b, x, s, t) {
    return add32(rol32(add32(add32(a, q), add32(x, t)), s), b);
  }
  function ff(a, b, c, d, x, s, t) { return cmn((b & c) | (~b & d), a, b, x, s, t); }
  function gg(a, b, c, d, x, s, t) { return cmn((b & d) | (c & ~d), a, b, x, s, t); }
  function hh(a, b, c, d, x, s, t) { return cmn(b ^ c ^ d, a, b, x, s, t); }
  function ii(a, b, c, d, x, s, t) { return cmn(c ^ (b | ~d), a, b, x, s, t); }

  /* 附加 padding 并返回消息字节 */
  function padData(bytes) {
    const bitLen = bytes.length * 8;
    const paddedLen = (((bytes.length + 8) >> 6) + 1) << 6;
    const padded = new Uint8Array(paddedLen);
    padded.set(bytes);
    padded[bytes.length] = 0x80;
    let hi = Math.floor(bitLen / 0x100000000);
    let lo = bitLen;
    for (let i = 0; i < 4; i++) padded[paddedLen - 8 + i] = lo & 0xff, lo = Math.floor(lo / 256);
    for (let i = 0; i < 4; i++) padded[paddedLen - 4 + i] = hi & 0xff, hi = Math.floor(hi / 256);
    return padded;
  }

  /* 计算 MD5，输入 Uint8Array，返回 16 字节 Uint8Array */
  function md5Bytes(input) {
    const data = input instanceof Uint8Array ? input : new Uint8Array(input);
    const padded = padData(data);
    let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;

    for (let off = 0; off < padded.length; off += 64) {
      const M = new Uint32Array(16);
      for (let i = 0; i < 16; i++) {
        let v = padded[off + i * 4] | (padded[off + i * 4 + 1] << 8) |
                (padded[off + i * 4 + 2] << 16) | (padded[off + i * 4 + 3] << 24);
        M[i] = v;
      }
      let A = a0, B = b0, C = c0, D = d0;
      for (let i = 0; i < 64; i++) {
        let F, g;
        if (i < 16)      { F = ff(A, B, C, D, M[i], S[i], K[i]); g = i; }
        else if (i < 32) { F = gg(A, B, C, D, M[(5 * i + 1) % 16], S[i], K[i]); g = (5 * i + 1) % 16; }
        else if (i < 48) { F = hh(A, B, C, D, M[(3 * i + 5) % 16], S[i], K[i]); g = (3 * i + 5) % 16; }
        else             { F = ii(A, B, C, D, M[(7 * i) % 16], S[i], K[i]); g = (7 * i) % 16; }
        void g;
        const tmp = D;
        D = C; C = B;
        B = F;
        A = tmp;
      }
      a0 = add32(a0, A); b0 = add32(b0, B); c0 = add32(c0, C); d0 = add32(d0, D);
    }
    const out = new Uint8Array(16);
    const words = [a0, b0, c0, d0];
    for (let w = 0; w < 4; w++) {
      out[w * 4] = words[w] & 0xff;
      out[w * 4 + 1] = (words[w] >>> 8) & 0xff;
      out[w * 4 + 2] = (words[w] >>> 16) & 0xff;
      out[w * 4 + 3] = (words[w] >>> 24) & 0xff;
    }
    return out;
  }

  function bytesToHex(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += (bytes[i].toString(16)).padStart(2, "0");
    return s;
  }

  function utf8Bytes(text) {
    return new TextEncoder().encode(String(text));
  }

  function md5Hex(text) {
    return bytesToHex(md5Bytes(utf8Bytes(text)));
  }

  function md5HexOfBytes(bytes) {
    return bytesToHex(md5Bytes(bytes));
  }

  /* ------------------------------------------------------------------ */
  /* HMAC-MD5 (RFC 2104)                                                */
  /* ------------------------------------------------------------------ */
  function hmacBytes(keyBytes, messageBytes) {
    const blockSize = 64;
    let key = keyBytes;
    if (key.length > blockSize) key = md5Bytes(key);
    const k = new Uint8Array(blockSize);
    k.set(key);
    const ipad = new Uint8Array(blockSize);
    const opad = new Uint8Array(blockSize);
    for (let i = 0; i < blockSize; i++) {
      ipad[i] = k[i] ^ 0x36;
      opad[i] = k[i] ^ 0x5c;
    }
    const inner = new Uint8Array(ipad.length + messageBytes.length);
    inner.set(ipad);
    inner.set(messageBytes, ipad.length);
    const innerHash = md5Bytes(inner);
    const outer = new Uint8Array(opad.length + innerHash.length);
    outer.set(opad);
    outer.set(innerHash, opad.length);
    return md5Bytes(outer);
  }

  function hmacMd5Hex(text, key) {
    return bytesToHex(hmacBytes(utf8Bytes(key), utf8Bytes(text)));
  }

  function hmacMd5HexOfBytes(bytes, keyBytes) {
    return bytesToHex(hmacBytes(utf8Bytes(keyBytes), bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)));
  }

  const api = {
    md5Hex,
    md5HexOfBytes,
    hmacMd5Hex,
    hmacMd5HexOfBytes,
    raw: { md5Bytes, hmacBytes, bytesToHex },
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.MD5 = api;
})(typeof window !== "undefined" ? window : globalThis);