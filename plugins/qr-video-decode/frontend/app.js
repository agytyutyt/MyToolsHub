/* QR 视频流解码插件 —— 前端逻辑
 * 流程：选择视频 → 逐帧抽到 canvas → Web Worker 中 jsQR 解码 → 按协议解析
 *       帧头（序号/总帧数/纠错参数）→ 收集分块 → 后端异步 zfec 重组（轮询进度）。
 *
 * 性能设计（针对大视频）：
 * - 解码放在 Web Worker，主线程只负责取帧与进度渲染，不卡 UI；
 * - 取帧与解码流水线重叠（预取下一帧的同时解码上一帧）；
 * - 实测帧率推断总帧数，据此计算进度百分比与「预计剩余时间」。
 */

const $ = (id) => document.getElementById(id);

/* 部署时请同步 +1，用于绕过浏览器对 .js 资源的 24h 长缓存 */
const APP_VER = "v=2";

window.addEventListener("error", (e) => {
  try { setStatus("页面错误：" + (e.message || "未知"), true); } catch (_) {}
});
window.addEventListener("unhandledrejection", (e) => {
  const msg = (e.reason && e.reason.message) || String(e.reason) || "未知";
  try { setStatus("解码流程出错：" + msg, true); } catch (_) {}
});

const urlState = {
  video: null,
  chunks: new Map(),
  nframes: 0,         // 按实测帧率估算的总帧数
  k: 0,
  m: 0,
  framesScanned: 0,
  declaredN: 0,
  recoveredText: null,
};

let worker = null;
const pending = new Map(); // id -> {resolve, requesterLabel}

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

function setStatus(text, isError) {
  $("status").textContent = text;
  $("status").className = "status" + (isError ? " error" : "");
}

function fmtDuration(sec) {
  if (!isFinite(sec) || sec < 0) return "--";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h > 0) return `${h} 小时 ${m} 分 ${s} 秒`;
  if (m > 0) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

/* ---------- Worker 通信 ---------- */

function ensureWorker() {
  if (worker) return;
  try {
    worker = new Worker("./worker.js?" + APP_VER);
  } catch (e) {
    setStatus("无法启动解码线程：" + e.message, true);
    return;
  }
  worker.onmessage = (e) => {
    const { id, parsed } = e.data;
    const item = pending.get(id);
    if (item) {
      pending.delete(id);
      item.resolve(parsed);
    }
  };
  worker.onerror = (e) => {
    const msg = "解码进程出错：" + (e.message || "未知原因");
    // 通知所有等待者
    for (const p of pending.values()) p.resolve(null);
    pending.clear();
    setStatus(msg, true);
  };
}

/* 派发一帧到 Worker 解码，返回 Promise<parsed|null>。
 * frame 为 Uint8ClampedArray 的 ImageData.data，转交时转移所有权（transferable）。 */
function dispatchDecode(dataArr, width, height) {
  ensureWorker();
  return new Promise((resolve) => {
    const id = (Math.random() * 1e9) | 0;
    pending.set(id, { resolve });
    worker.postMessage(
      { id, data: dataArr.buffer, width, height },
      [dataArr.buffer]
    );
  });
}

/* 将一帧解码结果写入 chunks 集合 */
function collectFrameResult(parsed) {
  if (!parsed) return;
  if (urlState.declaredN === 0) {
    urlState.declaredN = parsed.n;
    urlState.k = parsed.fec[0];
    urlState.m = parsed.fec[1];
  } else if (parsed.n !== urlState.declaredN ||
             parsed.fec[0] !== urlState.k ||
             parsed.fec[1] !== urlState.m) {
    return;
  }
  urlState.chunks.set(parsed.index, parsed.payload);
}

/* 将当前 video 帧绘制到 canvas，取回 RGBA 数据 */
function captureFrame(video, canvas, ctx) {
  const w = video.videoWidth, h = video.videoHeight;
  if (!w || !h) return null;
  if (canvas.width !== w) { canvas.width = w; canvas.height = h; }
  ctx.drawImage(video, 0, 0, w, h);
  return ctx.getImageData(0, 0, w, h);
}

/* ---------- 扫描主流程 ---------- */

async function openVideo(file) {
  const video = $("videoSrc");
  video.src = URL.createObjectURL(file);
  await new Promise((resolve, reject) => {
    video.onloadedmetadata = resolve;
    video.onerror = () => reject(new Error("视频解析失败"));
  });
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function waitForFrame(video, ms) {
  /* 等待视频画面可读取（videoWidth > 0）。 */
  const t0 = performance.now();
  while (!video.videoWidth && performance.now() - t0 < ms) {
    await sleep(20);
  }
}

/* 跳到指定时间并等待 seek 完成：seeked 事件或短超时兜底。
 * 超时设短：video display:none 时 seeked 事件可能不触发，靠下层单调推进逻辑去重/重试。 */
function videoSeekAndWait(video, target, ms = 40) {
  return new Promise((resolve) => {
    const failSafe = setTimeout(resolve, ms);
    const done = () => {
      clearTimeout(failSafe);
      video.removeEventListener("seeked", done);
      resolve();
    };
    video.addEventListener("seeked", done);
    video.currentTime = target;
  });
}

/* 确定性单调逐帧扫描：以上一帧「实际吸附到的时间」为基准推进，
 * 不依赖估算的目标时刻，避免浮点/吸附误差导致漏帧。
 * seek 未前进时小幅推进重试；video 即使 display:none 也逐帧不漏。 */
async function scanLoop(video, canvas, ctx, onFrameDecoded, declaredN) {
  video.muted = true;
  const dt = video.duration / Math.max(declaredN, 1);
  let capTime = null;      // 最近一次实际捕获的 currentTime
  let guard = 0;
  const MAX_GUARD = declaredN + 400;

  while (guard < MAX_GUARD) {
    let t = capTime === null ? 0 : capTime + dt;
    let ft = -1;
    for (let tried = 0; tried < 50; tried++) {
      if (t >= video.duration - 0.002) { ft = video.currentTime; break; }
      await videoSeekAndWait(video, t);
      ft = video.currentTime;
      if (capTime === null || ft > capTime + 1e-9) break; // 成功前进
      t += 0.003;                                         // 未前进：小幅推进重试
    }
    if (!(capTime === null || ft > capTime + 1e-9)) break; // 连续重试无进展
    capTime = ft;
    const frame = captureFrame(video, canvas, ctx);
    if (frame) {
      guard++;
      urlState.framesScanned++;
      const parsed = await dispatchDecode(frame.data, frame.width, frame.height);
      onFrameDecoded(parsed);
    }
    if (capTime >= video.duration - 0.003) break;
  }
}

async function runScan(file) {
  setStatus("正在载入视频…");
  try { await openVideo(file); }
  catch (e) { setStatus(e.message, true); return; }

  const video = videoSrc;
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });

  urlState.chunks.clear();
  urlState.framesScanned = 0;
  urlState.declaredN = 0;
  urlState.nframes = 0;
  urlState.m = 0;
  urlState.k = 0;

  await waitForFrame(video, 4000);
  if (!video.videoWidth) { setStatus("视频画面无法读取", true); return; }

  // 先取首帧解码，从帧头读取声明总帧数与纠错参数(k,m)
  await videoSeekAndWait(video, 0);
  const first = captureFrame(video, canvas, ctx);
  if (!first) { setStatus("视频画面无法读取", true); return; }
  urlState.framesScanned = 1;
  const parsed0 = await dispatchDecode(first.data, first.width, first.height);
  collectFrameResult(parsed0);

  let declaredN = urlState.declaredN;
  if (!declaredN) {
    // 首帧识别失败时，向前试探数帧再判定
    for (let i = 1; i <= 12 && !declaredN; i++) {
      await videoSeekAndWait(video, Math.min(video.duration - 0.002, i / 30));
      const f = captureFrame(video, canvas, ctx);
      if (!f) continue;
      urlState.framesScanned++;
      const p = await dispatchDecode(f.data, f.width, f.height);
      collectFrameResult(p);
      declaredN = urlState.declaredN;
    }
  }
  if (!declaredN) {
    setStatus("未能从视频中识别到 QR 码，请确认这是 QR-transfer 生成的视频流。", true);
    return;
  }

  const totalDur = video.duration || 1;
  const startWall = performance.now();

  setStatus("正在解码视频帧…");
  showProgress(0, "正在解码", null);

  await scanLoop(video, canvas, ctx, (parsed) => {
    collectFrameResult(parsed);
    urlState.nframes = declaredN;

    // 进度与 ETA：直接以声明总帧数为基准
    const pct = Math.min(100, Math.round((urlState.framesScanned / declaredN) * 100));
    const elapsed = (performance.now() - startWall) / 1000;
    const perFrame = elapsed / Math.max(urlState.framesScanned, 1);
    const remainingFrames = Math.max(declaredN - urlState.framesScanned, 0);
    showProgress(pct, "正在解码", remainingFrames * perFrame);
  }, declaredN);

  if (urlState.chunks.size === 0) {
    setStatus("未从视频中识别到任何 QR 帧。请确认这是 QR-transfer 生成的视频流。", true);
    return;
  }

  setStatus("帧扫描完成，正在提交后端进行 zfec 纠错重组…");
  await submitToBackend();
}

/* ---------- 进度渲染 ---------- */

function showProgress(pct, stage, etaSec) {
  $("progressWrap").hidden = false;
  $("progressFill").style.width = pct + "%";
  const meta = urlState.declaredN
    ? `（声明总帧 ${urlState.declaredN} · 已收分块 ${urlState.chunks.size}）`
    : `（已收分块 ${urlState.chunks.size}）`;
  const eta = etaSec != null && etaSec >= 0 ? `  ·  预计剩余 ${fmtDuration(etaSec)}` : "";
  let text;
  if (stage === "正在解码") {
    const counter = urlState.nframes > 0
      ? `${urlState.framesScanned}/${urlState.nframes} 帧`
      : `${urlState.framesScanned} 帧（统计帧率中…）`;
    text = `正在解码：${pct}%  ·  已扫 ${counter} ${meta}${eta}`;
    $("status").textContent = text;
    $("status").className = "status";
  } else {
    text = `${stage}：${pct}%${meta}${eta}`;
  }
  $("progressText").textContent = text;
}

function showReassembleProgress(pct, stage, etaSec, detail) {
  $("progressWrap").hidden = false;
  $("progressFill").style.width = pct + "%";
  $("progressText").textContent =
    `${stage}：${pct}%` +
    (detail ? `  ·  ${detail}` : "") +
    (etaSec != null && etaSec >= 0
      ? `  ·  预计剩余 ${fmtDuration(etaSec)}`
      : "");
}

/* ---------- 后端异步纠错 ---------- */

async function submitToBackend() {
  const chunks = {};
  for (const [k, v] of urlState.chunks) chunks[k] = v;
  try {
    const resp = await fetch("/api/qr-video-decode/reassemble", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        nframes: urlState.declaredN,
        k: urlState.k,
        m: urlState.m,
        chunks,
      }),
    });
    const data = await resp.json();
    if (!data.ok) { setStatus(data.detail || "提交失败", true); return; }

    const taskId = data.task_id;
    if (!taskId) {
      // 旧版同步响应兼容
      return renderRecovered(data);
    }

    const startWall = performance.now();
    let prevPct = 0;
    for (;;) {
      await new Promise((r) => setTimeout(r, 400));
      const sresp = await fetch(`/api/qr-video-decode/reassemble/${taskId}`);
      const s = await sresp.json();
      if (!s.ok) { setStatus(s.detail || "纠错任务失败", true); return; }

      if (s.status === "running" || s.status === "pending") {
        const pct = Math.round((s.progress || 0) * 100);
        const elapsed = (performance.now() - startWall) / 1000;
        // 用进度增量估算 ETA
        const doneFrac = s.progress || 0;
        let etaSec = null;
        if (elapsed > 1.5 && doneFrac > 0.02) {
          etaSec = elapsed / doneFrac - elapsed;
        } else if (prevPct !== pct) {
          etaSec = null;
        }
        showReassembleProgress(pct, "正在纠错重组", etaSec,
          `已完成 ${s.done || 0}/${s.total || "-"} 个纠错块`);
        prevPct = pct;
      } else if (s.status === "done") {
        return renderRecovered(s);
      } else if (s.status === "error") {
        setStatus(s.detail || "纠错重组失败", true);
        return;
      }
      if (performance.now() - startWall > 600000) { // 10 分钟超时
        setStatus("纠错重组超时，请重试", true);
        return;
      }
    }
  } catch (e) {
    setStatus("网络错误：" + e.message, true);
  }
}

function renderRecovered(result) {
  const bin = atob(result.data_b64);
  const raw = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) raw[i] = bin.charCodeAt(i);
  const text = new TextDecoder().decode(raw);
  const isJson = /^[\s]*[[{]$/.test(text.trim());
  let summary = `重组成功 ✔  恢复 ${result.size} 字节（原始文件）`;
  if (isJson) {
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) summary += `，JSON 数组含 ${parsed.length} 个元素`;
      else summary += "，JSON 对象";
    } catch (e) { /* 忽略 */ }
  }

  setStatus(summary);
  $("resultSize").textContent = result.size + " 字节";
  $("result").hidden = false;
  $("previewTitle").textContent = isJson ? "恢复内容预览（JSON）" : "恢复内容预览（文本）";
  $("preview").textContent = text.slice(0, 2000);
  $("downloadBtn").disabled = false;
  urlState.recoveredText = text;

  // 弹出浮窗展示完整数据
  showDataModal(text, result.size, `${result.size} 字节 · 重组成功`);
}

function downloadRecovered() {
  const text = urlState.recoveredText || "";
  const blob = new Blob([text], { type: "application/octet-stream" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "recovered-data.json";
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ---------- 解码结果浮窗 ---------- */

function showDataModal(text, size, stat) {
  let shown = text;
  try {
    shown = JSON.stringify(JSON.parse(text), null, 2); // 尝试美化 JSON
  } catch (e) { /* 保留原文 */ }
  $("modalStats").textContent = stat || `${size} 字节`;
  $("modalData").textContent = shown;
  $("dataModal").hidden = false;
}

function closeDataModal() {
  $("dataModal").hidden = true;
}

function copyRecovered() {
  const text = urlState.recoveredText || "";
  const done = () => {
    const btn = $("modalCopy");
    const old = btn.textContent;
    btn.textContent = "已复制";
    setTimeout(() => { btn.textContent = old; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { /* 忽略 */ }
  document.body.removeChild(ta);
}

function init() {
  const drop = $("dropZone");
  const input = $("fileInput");
  $("downloadBtn").addEventListener("click", downloadRecovered);

  // 浮窗交互
  $("modalClose").addEventListener("click", closeDataModal);
  $("modalCloseBtn").addEventListener("click", closeDataModal);
  $("modalCopy").addEventListener("click", copyRecovered);
  $("modalDownload").addEventListener("click", downloadRecovered);
  $("dataModal").addEventListener("click", (e) => {
    if (e.target === $("dataModal")) closeDataModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("dataModal").hidden) closeDataModal();
  });

  drop.addEventListener("click", () => input.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("drag");
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
  });
  input.addEventListener("change", () => {
    if (input.files.length) handleFile(input.files[0]);
  });
  $("scanBtn").addEventListener("click", () => {
    if (urlState.video) runScan(urlState.video);
  });
  $("reChoose").addEventListener("click", () => {
    closeDataModal();
    $("result").hidden = true;
    setStatus("");
    $("videoSrc").removeAttribute("src");
    urlState.video = null;
    urlState.recoveredText = null;
    $("fileName").textContent = "";
    $("scanBtn").disabled = true;
    $("progressWrap").hidden = true;
  });
}

function handleFile(file) {
  const video = $("videoSrc");
  video.src = URL.createObjectURL(file);
  video.onloadedmetadata = () => {
    urlState.video = file;
    $("fileName").textContent =
      `${file.name}  ·  ${(file.size / 1048576).toFixed(1)} MB  ·  ` +
      `${video.duration.toFixed(1)}s  @${video.videoWidth}×${video.videoHeight}`;
    $("scanBtn").disabled = false;
    $("result").hidden = true;
    $("progressWrap").hidden = true;
    closeDataModal();
    setStatus("");
  };
  video.onerror = () => setStatus("无法读取该视频文件", true);
}

document.addEventListener("DOMContentLoaded", init);
window.__qrDebug = () => ({
  framesScanned: urlState.framesScanned,
  declaredN: urlState.declaredN,
  nframes: urlState.nframes,
  k: urlState.k,
  m: urlState.m,
  nChunks: urlState.chunks.size,
  sampleIndices: Array.from(urlState.chunks.keys()).slice(0, 40),
});