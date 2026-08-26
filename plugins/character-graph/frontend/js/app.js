import * as THREE from "./lib/three/three.module.js";
import { OrbitControls } from "./lib/three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "./lib/three/addons/renderers/CSS2DRenderer.js";

/* ------------------------------------------------------------------ */
/* 工具函数                                                             */
/* ------------------------------------------------------------------ */

const API_BASE = "/api/character-graph";

const $ = (id) => document.getElementById(id);

function log(msg, type = "") {
  const status = $("status");
  const line = document.createElement("div");
  line.className = "line " + type;
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  status.appendChild(line);
  status.scrollTop = status.scrollHeight;
}

/* ------------------------------------------------------------------ */
/* 三维场景                                                            */
/* ------------------------------------------------------------------ */

const container = $("viewport");
const canvas = $("graphCanvas");
const overlay = $("overlay");
const tooltip = $("tooltip");

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(
  55,
  container.clientWidth / container.clientHeight,
  0.1,
  1000
);
camera.position.set(0, 4, 20);

const renderer = new THREE.WebGLRenderer({
  canvas,
  antialias: true,
  alpha: true,
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(container.clientWidth, container.clientHeight);

const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(container.clientWidth, container.clientHeight);
labelRenderer.domElement.style.position = "absolute";
labelRenderer.domElement.style.top = "0";
labelRenderer.domElement.style.pointerEvents = "none";
container.appendChild(labelRenderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.6;
controls.minDistance = 4;
controls.maxDistance = 60;

/* 背景星空 */
function createStars() {
  const count = 1800;
  const positions = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const r = 90 + Math.random() * 160;
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
    positions[i * 3 + 2] = r * Math.cos(phi);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const mat = new THREE.PointsMaterial({
    color: 0x9fb4ff,
    size: 0.25,
    transparent: true,
    opacity: 0.8,
    sizeAttenuation: true,
  });
  return new THREE.Points(geo, mat);
}
const stars = createStars();
scene.add(stars);

/* 节点辉光贴图（径向渐变） */
function glowTexture(color, inner) {
  const size = 128;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const ctx = c.getContext("2d");
  const g = ctx.createRadialGradient(
    size / 2, size / 2, 0,
    size / 2, size / 2, size / 2
  );
  g.addColorStop(0, inner);
  g.addColorStop(0.25, color.replace("1)", "0.55)"));
  g.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  return new THREE.CanvasTexture(c);
}

/* 关系类型配色板 */
const TYPE_COLORS = [
  "#ff6b9d", "#ffb86b", "#ffd93d", "#6bcbff", "#7bffb0",
  "#c77bff", "#ff7b7b", "#ff9df5", "#8dffd9", "#d4a8ff",
  "#ffe27a", "#7de3ff",
];
function typeColor(type, i) {
  return i != null ? TYPE_COLORS[i % TYPE_COLORS.length] : TYPE_COLORS[0];
}

const groups = {
  nodes: new THREE.Group(),
  edges: new THREE.Group(),
  labels: new THREE.Group(),
};
scene.add(groups.nodes);
scene.add(groups.edges);
scene.add(groups.labels);

let nodeObjects = []; // {obj, data}
let relationshipTypes = [];

function clearGraph() {
  for (const name of ["nodes", "edges", "labels"]) {
    while (groups[name].children.length) {
      const child = groups[name].children.pop();
      child.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) {
          (Array.isArray(o.material) ? o.material : [o.material])
            .forEach((m) => m.dispose());
        }
      });
    }
  }
  nodeObjects = [];
  relationshipTypes = [];
  $("legend").innerHTML = "";
}

/* ------------------------------------------------------------------ */
/* 构图：Fibonacci 球面分布（立体星点图）                                */
/* ------------------------------------------------------------------ */

function fibSpherePositions(n, radius) {
  const pts = [];
  const phi = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / Math.max(n - 1, 1)) * 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = phi * i;
    pts.push(
      new THREE.Vector3(
        Math.cos(theta) * r * radius,
        y * radius,
        Math.sin(theta) * r * radius
      )
    );
  }
  return pts;
}

function buildGraph(graph) {
  clearGraph();

  const characters = graph.characters || [];
  const relationships = graph.relationships || [];

  /* 节点度（连接数） */
  const degree = {};
  characters.forEach((c) => (degree[c.name] = 0));
  relationships.forEach((r) => {
    degree[r.source] = (degree[r.source] || 0) + 1;
    degree[r.target] = (degree[r.target] || 0) + 1;
  });

  /* 按度排序，中心人物排在前面，得到更稳定的球面分布 */
  const sorted = [...characters].sort(
    (a, b) => (degree[b.name] || 0) - (degree[a.name] || 0)
  );
  const n = sorted.length;
  const radius = n <= 1 ? 4 : 6 + Math.log2(n + 1) * 1.2;
  const positions = fibSpherePositions(n, radius);
  const posByIndex = {};

  const maxDeg = Math.max(1, ...Object.values(degree));

  sorted.forEach((ch, i) => {
    const deg = degree[ch.name] || 0;
    const t = deg / maxDeg; // 0~1 重要度
    const pos = positions[i];

    const hex = new THREE.Color("#5b8cff").lerp(
      new THREE.Color("#ffd93d"),
      0.15 + t * 0.85
    );
    const inner = `rgba(${(hex.r * 255) | 0},${(hex.g * 255) | 0},${(hex.b * 255) | 0},1)`;

    /* 节点小球 */
    const size = 0.35 + t * 0.55;
    const sphere = new THREE.Mesh(
      new THREE.SphereGeometry(size, 24, 24),
      new THREE.MeshBasicMaterial({ color: hex })
    );
    sphere.position.copy(pos);
    groups.nodes.add(sphere);

    /* 辉光精灵 */
    const sprite = new THREE.Sprite(
      new THREE.SpriteMaterial({
        map: glowTexture(inner, inner),
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      })
    );
    sprite.scale.setScalar(size * 9);
    sprite.position.copy(pos);
    groups.nodes.add(sprite);

    /* 文字标签 */
    const labelDiv = document.createElement("div");
    labelDiv.className = "node-label";
    labelDiv.textContent = ch.name;
    const label = new CSS2DObject(labelDiv);
    label.position.copy(pos).add(new THREE.Vector3(0, size + 0.45, 0));
    groups.labels.add(label);

    nodeObjects.push({
      obj: sprite,
      data: { ...ch, degree: deg },
    });
    posByIndex[ch.name] = pos;
  });

  /* 连线 */
  const typeSet = [];
  relationships.forEach((r) => {
    if (!typeSet.includes(r.type)) typeSet.push(r.type);
  });
  relationshipTypes = typeSet;

  relationships.forEach((r) => {
    const a = posByIndex[r.source];
    const b = posByIndex[r.target];
    if (!a || !b) return;
    const color = new THREE.Color(typeColor(r.type, typeSet.indexOf(r.type)));
    const strength = Math.max(0.15, Math.min(1, (r.strength || 5) / 10));

    /* 弧线让图形更立体 */
    const mid = a.clone().add(b).multiplyScalar(0.5);
    mid.y += 0.6 + strength * 1.4;
    const curve = new THREE.QuadraticBezierCurve3(a.clone(), mid, b.clone());
    const curvePts = curve.getPoints(24);

    const geo = new THREE.BufferGeometry().setFromPoints(curvePts);
    const mat = new THREE.LineBasicMaterial({
      color,
      transparent: true,
      opacity: 0.35 + strength * 0.6,
    });
    const line = new THREE.Line(geo, mat);
    groups.edges.add(line);

    /* 发光弱线（叠加） */
    const glowMat = new THREE.LineBasicMaterial({
      color,
      transparent: true,
      opacity: 0.15 * strength,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const glowLine = new THREE.Line(geo.clone(), glowMat);
    groups.edges.add(glowLine);
  });

  /* 图例 */
  const legendEl = $("legend");
  if (relationshipTypes.length === 0) {
    legendEl.innerHTML = '<div class="none">暂无关系数据</div>';
  } else {
    relationshipTypes.forEach((t, i) => {
      const row = document.createElement("div");
      row.className = "row";
      const count = relationships.filter((r) => r.type === t).length;
      row.innerHTML = `
        <span class="swatch" style="background:${typeColor(t, i)}"></span>
        <span>${escapeHtml(t)}</span>
        <span class="count">${count} 条</span>`;
      legendEl.appendChild(row);
    });
  }

  /* 相机取景 */
  fitCamera(positions);
  controls.autoRotate = true;
  log(`成功生成立体图：${n} 个人物，${relationships.length} 条关系`, "ok");
}

function fitCamera(positions) {
  const box = new THREE.Box3();
  positions.forEach((p) => box.expandByPoint(p));
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3()).length() || 10;
  controls.target.copy(center);
  camera.position.copy(center).add(new THREE.Vector3(0, size * 0.4, size * 1.6));
  controls.update();
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

/* ------------------------------------------------------------------ */
/* 交互：悬停高亮 + 点击详情                                            */
/* ------------------------------------------------------------------ */

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let hovered = null;

function pick(event) {
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const sprites = nodeObjects.map((n) => n.obj);
  const hits = raycaster.intersectObjects(sprites, false);
  return hits.length ? hits[0] : null;
}

canvas.addEventListener("pointermove", (e) => {
  const hit = pick(e);
  if (hovered) {
    hovered.obj.scale.setScalar(hovered.base);
    hovered = null;
  }
  if (hit) {
    const node = nodeObjects.find((n) => n.obj === hit.object);
    if (node) {
      hovered = node;
      hovered.base = node.obj.scale.x;
      node.obj.scale.setScalar(hovered.base * 1.35);
      showTooltip(e, node.data);
      canvas.style.cursor = "pointer";
      return;
    }
  }
  hideTooltip();
  canvas.style.cursor = "grab";
});

canvas.addEventListener("click", (e) => {
  const hit = pick(e);
  if (hit) {
    const node = nodeObjects.find((n) => n.obj === hit.object);
    if (node) openDetailModal(node.data);
  }
});

/* 详情弹窗 */
const detailModal = $("detailModal");
const modalTitle = detailModal.querySelector(".modal-title");
const modalBody = detailModal.querySelector(".modal-body");

function relationStrength(r) {
  if (typeof r.strength === "number") return r.strength;
  if (typeof r.weight === "number") return r.weight;
  return 0;
}

function openDetailModal(data) {
  const graph = currentGraph || { relationships: [] };
  const nodes = graph.characters || [];
  const nameById = {};
  nodes.forEach((c, i) => (nameById[c.name] = c.name));

  const links = [];
  const relColorByType = {};
  let relIdx = 0;
  (relationshipTypes || []).forEach((t, i) => (relColorByType[t] = typeColor(t, i)));

  graph.relationships.forEach((r) => {
    const strength = relationStrength(r);
    if (r.source === data.name && nameById[r.target]) {
      links.push({
        dot: relColorByType[r.type] || "var(--muted)",
        text: `${r.type} → ${r.target}`,
        strength,
      });
    }
    if (r.target === data.name && nameById[r.source]) {
      links.push({
        dot: relColorByType[r.type] || "var(--muted)",
        text: `${r.source} → ${r.type}`,
        strength,
      });
    }
    relIdx++;
  });

  const tags = (data.tags || [])
    .map((t) => `<span class="tag">${escapeHtml(t)}</span>`)
    .join("");

  const relHtml = links.length
    ? `<div class="rel-title">关联关系</div><ul class="rel-list">${links
        .map(
          (l) =>
            `<li><span class="rel-dot" style="background:${l.dot}"></span><span class="rel-text">${escapeHtml(
              l.text
            )}</span><span class="rel-strength">亲密度 ${(l.strength * 100).toFixed(0)}%</span></li>`
        )
        .join("")}</ul>`
    : `<div class="rel-title">关联关系</div><p class="desc">暂无关联关系</p>`;

  modalTitle.textContent = data.name;
  modalBody.innerHTML = `
    <div class="desc">${escapeHtml(data.description || "暂无描述")}</div>
    ${tags ? `<div>${tags}</div>` : ""}
    ${relHtml}`;

  detailModal.hidden = false;
  hideTooltip();
}

function closeDetailModal() {
  detailModal.hidden = true;
}

detailModal.querySelector(".modal-close").addEventListener("click", closeDetailModal);
detailModal.querySelector(".modal-ok").addEventListener("click", closeDetailModal);
detailModal.addEventListener("click", (e) => {
  if (e.target === detailModal) closeDetailModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !detailModal.hidden) closeDetailModal();
});

function showTooltip(event, data) {
  if (!detailModal.hidden) return;
  const graph = currentGraph || { relationships: [] };
  const links = [];
  graph.relationships.forEach((r) => {
    if (r.source === data.name) links.push(`${r.type} → ${r.target}`);
    if (r.target === data.name) links.push(`${r.source} → ${r.type}`);
  });
  const tags = (data.tags || []).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("");
  tooltip.innerHTML = `
    <div class="name">${escapeHtml(data.name)}</div>
    <div class="desc">${escapeHtml(data.description || "暂无描述")}</div>
    <div>${tags}</div>
    <div class="links">${links.length ? "关系：" + links.map(escapeHtml).join("、") : "暂无关系"}</div>`;
  tooltip.hidden = false;
  const vw = container.clientWidth;
  const vh = container.clientHeight;
  const tw = 240;
  const x = Math.min(event.clientX + 16, vw - tw - 12);
  const y = Math.min(event.clientY + 12, vh - 120);
  tooltip.style.left = x + "px";
  tooltip.style.top = y + "px";
}
function hideTooltip() {
  tooltip.hidden = true;
}

/* ------------------------------------------------------------------ */
/* 动画循环                                                            */
/* ------------------------------------------------------------------ */

const clock = new THREE.Clock();
function animate() {
  requestAnimationFrame(animate);
  const t = clock.getElapsedTime();

  /* 星空缓慢旋转 */
  stars.rotation.y += 0.0004;
  stars.rotation.x += 0.0001;

  controls.update();
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
}
animate();

/* ------------------------------------------------------------------ */
/* 页面交互：配置、上传、分析                                            */
/* ------------------------------------------------------------------ */

let currentGraph = { relationships: [] };

function refreshAnalyzeState() {
  const hasFile = !!fileInput.files.length;
  $("analyzeBtn").disabled = !hasFile;
  $("fileName").textContent = hasFile ? fileInput.files[0].name : "";
}

/* 配置 */
const baseUrl = $("baseUrl");
const apiKey = $("apiKey");
const model = $("model");
const apiFormSection = $("apiFormSection");
const apiNotice = $("apiNotice");

async function loadConfig() {
  try {
    const r = await fetch(API_BASE + "/config");
    const cfg = await r.json();

    /* api_source: web = 页面填写并持久化；config = 隐藏表单，使用配置文件 */
    if (cfg.api_source === "config") {
      apiFormSection.style.display = "none";
      apiNotice.hidden = false;
      apiNotice.textContent =
        "当前为「配置文件模式」：API 由 config.json 中的 llm 段统一管理，" +
        "页面无需填写大模型信息。如需改为在网页填写，请编辑 config.json 将 api_source 设为 web。";
      log("检测到配置文件模式，大模型配置由 config.json 提供", "ok");
      return;
    }

    /* web 模式：自动读取上次保存的 API 配置（Key 不回传明文，仅掩码提示） */
    apiFormSection.style.display = "";
    apiNotice.hidden = true;
    baseUrl.value = cfg.base_url || "";
    apiKey.value = "";
    apiKey.placeholder = cfg.api_key_set
      ? `已设置（${cfg.api_key_masked || "****"}），留空则不修改`
      : "sk-...";
    model.value = cfg.model || "";
    log("已读取上次保存的大模型配置", "ok");
  } catch (e) {
    log("读取配置失败", "err");
  }
}
loadConfig();

$("saveConfig").addEventListener("click", async () => {
  const tip = $("configTip");
  tip.textContent = "保存中…";
  try {
    const r = await fetch(API_BASE + "/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: baseUrl.value.trim(),
        api_key: apiKey.value.trim(), // 留空 = 沿用已保存的 Key
        model: model.value.trim(),
      }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "保存失败");
    tip.textContent = "✓ 配置已保存";
    apiKey.value = "";
    apiKey.placeholder = "已设置，留空则不修改";
  } catch (e) {
    tip.textContent = "✗ " + e.message;
  }
});

/* 文件选择 / 拖拽 */
const fileInput = $("fileInput");
const dropZone = $("dropZone");

dropZone.addEventListener("click", (e) => {
  e.stopPropagation();
  fileInput.click();
});
fileInput.addEventListener("change", refreshAnalyzeState);

["dragenter", "dragover"].forEach((ev) =>
  dropZone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((ev) =>
  dropZone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
  })
);
dropZone.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) {
    const dt = new DataTransfer();
    dt.items.add(f);
    fileInput.files = dt.files;
    refreshAnalyzeState();
  }
});

/* 分析 */
$("analyzeBtn").addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  const formData = new FormData();
  formData.append("file", file);
  formData.append("base_url", baseUrl.value.trim());
  formData.append("api_key", apiKey.value.trim());
  formData.append("model", model.value.trim());

  const btn = $("analyzeBtn");
  btn.disabled = true;
  btn.textContent = "提交中…";
  log(`开始分析《${file.name}》，正在提交任务…`);

  try {
    const r = await fetch(API_BASE + "/analyze", { method: "POST", body: formData });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "提交失败");

    /* 异步任务：轮询 result 接口直到完成 */
    const taskId = data.task_id;
    log(`任务已提交（${taskId}），正在调用大模型…`);
    btn.textContent = "分析中…";

    let graph = null;
    for (let i = 0; i < 600; i++) {
      await new Promise((res) => setTimeout(res, 2000));
      const resp = await fetch(`${API_BASE}/result/${encodeURIComponent(taskId)}`);
      const task = await resp.json();
      if (!resp.ok) throw new Error(task.detail || "查询任务失败");

      if (task.status === "done") {
        graph = task.graph;
        break;
      }
      if (task.status === "error") {
        throw new Error(task.detail || "分析失败");
      }
      /* 仍在运行中，继续等待 */
    }

    if (!graph) throw new Error("分析超时，请稍后重试");
    currentGraph = graph;
    buildGraph(graph);
  } catch (e) {
    log("分析失败：" + e.message, "err");
  } finally {
    btn.textContent = "开始分析";
    refreshAnalyzeState();
  }
});

/* 窗口自适应 */
window.addEventListener("resize", () => {
  const w = container.clientWidth;
  const h = container.clientHeight;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
  labelRenderer.setSize(w, h);
});

/* 加载完成，移除遮罩 */
overlay.style.display = "none";

/* 标签样式注入 */
const style = document.createElement("style");
style.textContent = `
.node-label {
  color: #e8ecff;
  font-size: 13px;
  font-weight: 600;
  text-shadow: 0 0 6px rgba(0,0,0,.9), 0 0 12px rgba(91,140,255,.6);
  background: rgba(10,13,31,.55);
  border: 1px solid rgba(91,140,255,.35);
  border-radius: 6px;
  padding: 2px 8px;
  white-space: nowrap;
  pointer-events: none;
  user-select: none;
}
`;
document.head.appendChild(style);
