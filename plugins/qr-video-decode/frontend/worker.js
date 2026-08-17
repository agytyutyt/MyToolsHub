/* QR 视频流解码 —— Web Worker
 * 在独立线程中用 jsQR 解码单帧 QR，避免阻塞主线程（主线程负责取帧与进度 UI）。
 */
importScripts("./jsqr.min.js");

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

function parseFrame(qrData) {
  if (typeof qrData !== "string" || qrData.length < 12) return null;
  return {
    index: decodeIndex(qrData.slice(0, 4)),
    n: decodeIndex(qrData.slice(4, 8)),
    fec: decodeFecInfo(qrData.slice(8, 12)),
    payload: qrData.slice(12),
  };
}

self.onmessage = (e) => {
  const { id, data, width, height } = e.data;
  const img = new Uint8ClampedArray(data);
  let parsed = null;
  try {
    const res = jsQR(img, width, height);
    if (res) parsed = parseFrame(res.data);
  } catch (err) {
    // 单帧解码失败不致命，返回 null 由主线程继续
  }
  self.postMessage({ id, parsed }, [data]);
};