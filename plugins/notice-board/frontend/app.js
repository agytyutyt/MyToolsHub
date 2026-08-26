/* 公告板 —— 前端逻辑
 * - 全员可查看（grant_all 默认赋权）；发布/编辑仅对管理员角色/超管开放；
 * - 编辑：管理员对可见范围内的公告可改标题/内容/可见范围（卡片「编辑」按钮），
 *   复用发布对话框，提交 PUT；删除仍限创建者或超管；
 * - 发布对话框左右结构：左侧标题与内容，右侧可见范围；
 * - 可见范围为三段式：左=单位→部门→用户树（可多选勾选），
 *   中=「选择」按钮（把勾选项加入右侧），右=已选对象框（可逐项移除）；
 * - 公告仅对已选对象可见：命中任一目标即可见（后端过滤）。
 */

const $ = (id) => document.getElementById(id);
const API = "/api/notice-board";

/* ==================== 工具 ==================== */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function nl2br(s) {
  // 先转义再替换换行，保证内容纯文本安全渲染
  return esc(s).replace(/\n/g, "<br>");
}

let toastTimer = null;
function toast(text, isErr) {
  const el = $("toast");
  el.textContent = text;
  el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

async function api(url, options) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

const TYPE_ICON = { unit: "🏢", department: "🏛️", user: "👤" };
const TYPE_NAME = { unit: "单位", department: "部门", user: "用户" };

/* ==================== 全局状态 ==================== */
let ORG_TREE = [];                 // 单位→部门→用户 树
const NAME_MAP = {};               // id -> {name, type}（卡片徽章解析展示名）
const checkedSet = new Set();      // 树中已勾选待加入的 key："type:id"
let targets = [];                  // 已选可见范围 [{type, id, uid?}]
let expandedUnits = {};            // 单位节点展开状态
let expandedDepts = {};            // 部门节点展开状态
let deletingId = null;             // 待删除公告 id
let editingId = null;              // 待编辑公告 id（非空时对话框为编辑模式）

const keyOf = (t) => `${t.type}:${t.id}`;

/* ==================== 配置：是否可发布 ==================== */
async function loadConfig() {
  try {
    const cfg = await api(API + "/config");
    $("publishBtn").classList.toggle("hidden", !cfg.can_publish);
  } catch (_) { /* 匿名时框架已拦截，静默 */ }
}

/* ==================== 组织树（含用户） ==================== */
async function loadOrgTree() {
  try {
    const data = await api("/api/admin/org-tree");
    ORG_TREE = data.tree || [];
    for (const k of Object.keys(NAME_MAP)) delete NAME_MAP[k];
    for (const unit of ORG_TREE) {
      NAME_MAP[unit.id] = { name: unit.name, type: "unit" };
      for (const dept of unit.children || []) {
        NAME_MAP[dept.id] = { name: dept.name, type: "department" };
        for (const u of dept.children || []) {
          NAME_MAP[u.id] = { name: u.name, type: "user" };
        }
      }
    }
    renderTree();
    renderSelList();   // 组织树就绪后重绘已选框，让编辑模式预填的展示名正确解析
  } catch (_) {
    $("scopeTree").innerHTML = '<div class="tree-empty">组织架构加载失败</div>';
  }
}

function toggleExpand(store, id) {
  store[id] = !store[id];
}

function buildNodeRow(node, depth, onToggleKids, kidsEl) {
  const row = document.createElement("div");
  row.className = "tree-row";
  row.dataset.level = node.type;
  row.dataset.id = node.id;

  const arrow = document.createElement("button");
  arrow.className = "tree-arrow" + (onToggleKids ? "" : " ghost");
  arrow.textContent = "▶";
  if (onToggleKids) {
    arrow.addEventListener("click", (e) => {
      e.stopPropagation();
      onToggleKids(arrow, kidsEl);
    });
  }

  const check = document.createElement("span");
  check.className = "tree-check";
  check.textContent = "✓";

  const icon = document.createElement("span");
  icon.className = "tree-icon";
  icon.textContent = TYPE_ICON[node.type] || "•";

  const name = document.createElement("span");
  name.textContent = node.name;

  row.append(arrow, check, icon, name);
  row.addEventListener("click", () => toggleChecked(node));
  return row;
}

// 勾选 / 取消勾选（仅标记，点「选择」后才加入右侧）
function toggleChecked(node) {
  const key = keyOf(node);
  if (checkedSet.has(key)) checkedSet.delete(key);
  else checkedSet.add(key);
  document.querySelectorAll("#scopeTree .tree-row").forEach((row) => {
    row.classList.toggle("checked",
      checkedSet.has(`${row.dataset.level}:${row.dataset.id}`));
  });
}

function buildUnitNode(unit) {
  const li = document.createElement("li");
  li.className = "tree-unit";

  const kids = document.createElement("ul");
  kids.className = "tree-kids" + (expandedUnits[unit.id] ? "" : " collapsed");
  (unit.children || []).forEach((dept) => kids.appendChild(buildDeptNode(dept)));

  const hasKids = (unit.children || []).length > 0;
  li.append(buildNodeRow(unit, 0,
    hasKids ? ((arrow, el) => {
      toggleExpand(expandedUnits, unit.id);
      arrow.classList.toggle("open", !!expandedUnits[unit.id]);
      el.classList.toggle("collapsed", !expandedUnits[unit.id]);
    }) : null,
    kids));
  li.appendChild(kids);
  return li;
}

function buildDeptNode(dept) {
  const li = document.createElement("li");
  li.className = "tree-dept";

  const kids = document.createElement("ul");
  kids.className = "tree-kids" + (expandedDepts[dept.id] ? "" : " collapsed");
  (dept.children || []).forEach((u) => {
    const userLi = document.createElement("li");
    userLi.className = "tree-user";
    userLi.appendChild(buildNodeRow(u, 2, null, null));
    kids.appendChild(userLi);
  });

  const hasKids = (dept.children || []).length > 0;
  li.append(buildNodeRow(dept, 1,
    hasKids ? ((arrow, el) => {
      toggleExpand(expandedDepts, dept.id);
      arrow.classList.toggle("open", !!expandedDepts[dept.id]);
      el.classList.toggle("collapsed", !expandedDepts[dept.id]);
    }) : null,
    kids));
  li.appendChild(kids);
  return li;
}

function renderTree() {
  const box = $("scopeTree");
  box.innerHTML = "";
  if (!ORG_TREE.length) {
    box.innerHTML = '<div class="tree-empty">暂无单位/部门数据</div>';
    return;
  }
  const root = document.createElement("div");
  for (const unit of ORG_TREE) root.appendChild(buildUnitNode(unit));
  box.appendChild(root);
  refreshCheckedMarks();
}

function refreshCheckedMarks() {
  document.querySelectorAll("#scopeTree .tree-row").forEach((row) => {
    row.classList.toggle("checked",
      checkedSet.has(`${row.dataset.level}:${row.dataset.id}`));
  });
}

// 全选：直接把全部单位加入右侧已选框（选定单位即覆盖其下属部门与用户，
// 无需逐个列出）。已存在的单位自动去重跳过。
function selectAllUnits() {
  const total = ORG_TREE.length;
  if (!total) {
    toast("暂无单位数据", true);
    return;
  }
  let added = 0;
  for (const unit of ORG_TREE) {
    if (addTarget({ type: "unit", id: unit.id })) added++;
  }
  renderSelList();
  if (added === total) toast(`已加入全部 ${total} 个单位（含其下部门与用户）`);
  else if (added > 0) toast(`已补充加入 ${added} 个单位`);
  else toast("全部单位均已在可见范围中", true);
}

/* ==================== 已选框 ==================== */
function addTarget(t) {
  if (targets.some((x) => keyOf(x) === keyOf(t))) return false; // 去重
  targets.push({ ...t });   // 整体浅拷贝：保留部门的 uid（所属单位）
  return true;
}

// 把树中勾选项加入右侧已选框；部门自动带上所属单位 uid（供双重比对）
function addCheckedToTargets() {
  if (!checkedSet.size) {
    toast("请先在左侧勾选单位、部门或用户", true);
    return;
  }
  let added = 0;
  let skipped = 0;
  for (const unit of ORG_TREE) {
    for (const dept of unit.children || []) {
      for (const u of dept.children || []) {
        const key = `user:${u.id}`;
        if (checkedSet.has(key)) {
          if (addTarget({ type: "user", id: u.id })) added++; else skipped++;
          checkedSet.delete(key);
        }
      }
      const dk = `department:${dept.id}`;
      if (checkedSet.has(dk)) {
        if (addTarget({ type: "department", id: dept.id, uid: unit.id })) added++;
        else skipped++;
        checkedSet.delete(dk);
      }
    }
    const uk = `unit:${unit.id}`;
    if (checkedSet.has(uk)) {
      if (addTarget({ type: "unit", id: unit.id })) added++; else skipped++;
      checkedSet.delete(uk);
    }
  }
  refreshCheckedMarks();
  renderSelList();
  if (added && skipped) toast(`已加入 ${added} 项，${skipped} 项重复未加`);
  else if (added) toast(`已加入 ${added} 项`);
  else if (skipped) toast("所选项目已在可见范围中", true);
}

function removeTarget(key) {
  targets = targets.filter((t) => keyOf(t) !== key);
  renderSelList();
}

// 清空右侧全部已选对象
function clearTargets() {
  if (!targets.length) return;
  const n = targets.length;
  targets = [];
  renderSelList();
  toast(`已清空 ${n} 个可见范围对象`);
}

function targetLabel(t) {
  const info = NAME_MAP[t.id];
  return info ? info.name : t.id;
}

function renderSelList() {
  const list = $("selList");
  list.innerHTML = "";
  $("selEmpty").classList.toggle("hidden", targets.length > 0);
  for (const t of targets) {
    const row = document.createElement("div");
    row.className = "sel-row";
    row.dataset.key = keyOf(t);

    const icon = document.createElement("span");
    icon.className = "sel-icon";
    icon.textContent = TYPE_ICON[t.type];

    const name = document.createElement("span");
    name.className = "sel-name";
    name.textContent = targetLabel(t);   // F-3：textContent 渲染

    const tag = document.createElement("span");
    tag.className = "sel-tag";
    tag.textContent = TYPE_NAME[t.type] || t.type;

    const rm = document.createElement("button");
    rm.className = "sel-remove";
    rm.title = "移除";
    rm.textContent = "×";
    rm.addEventListener("click", () => removeTarget(keyOf(t)));

    row.append(icon, name, tag, rm);
    list.appendChild(row);
  }
}

/* ==================== 公告列表 ==================== */
async function loadList() {
  const board = $("board");
  try {
    const data = await api(API + "/announcements");
    board.innerHTML = "";
    const items = data.items || [];
    $("empty").classList.toggle("hidden", items.length > 0);
    for (const item of items) board.appendChild(buildCard(item));
  } catch (e) {
    board.innerHTML = "";
    $("empty").classList.remove("hidden");
    toast("加载公告失败：" + e.message, true);
  }
}

function buildCard(item) {
  const card = document.createElement("article");
  card.className = "ann-card";
  card.title = "点击查看全文";
  card.addEventListener("click", () => openDetail(item));

  const top = document.createElement("div");
  top.className = "ann-top";
  const title = document.createElement("h3");
  title.className = "ann-title";
  title.textContent = item.title;   // F-3：textContent 渲染用户输入
  top.appendChild(title);

  const content = document.createElement("div");
  content.className = "ann-content";
  content.innerHTML = nl2br(item.content);

  const meta = document.createElement("div");
  meta.className = "ann-meta";
  meta.append(
    Object.assign(document.createElement("span"),
      { textContent: "👤 " + (item.created_by_name || item.created_by || "未知") }),
    Object.assign(document.createElement("span"), { textContent: "·" }),
    Object.assign(document.createElement("time"), { textContent: item.created_at || "" }),
    Object.assign(document.createElement("span"), { className: "spacer" }),
  );
  if (item.editable) {
    const edit = document.createElement("button");
    edit.className = "ann-edit";
    edit.textContent = "编辑";
    edit.title = "修改标题/内容/可见范围";
    edit.addEventListener("click", (e) => {
      e.stopPropagation();   // 阻止冒泡：点编辑不应触发卡片「查看全文」
      openEditDialog(item);
    });
    meta.appendChild(edit);
  }
  if (item.manageable) {
    const del = document.createElement("button");
    del.className = "ann-del";
    del.textContent = "删除";
    // 阻止冒泡：点删除不应触发卡片「查看全文」
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      openDeleteConfirm(item);
    });
    meta.appendChild(del);
  }

  card.append(top, content, meta);
  return card;
}

/* ==================== 详情悬浮窗 ==================== */
function openDetail(item) {
  $("detailTitle").textContent = item.title || "";
  $("detailAuthor").textContent =
    "👤 " + (item.created_by_name || item.created_by || "未知");
  $("detailTime").textContent = item.created_at || "";
  const box = $("detailContent");
  box.innerHTML = nl2br(item.content);   // esc 后按换行转 <br>
  box.scrollTop = 0;
  $("detailDialog").classList.remove("hidden");
}

function closeDetail() {
  $("detailDialog").classList.add("hidden");
}

/* ==================== 发布 / 编辑 ==================== */
function openPublishDialog() {
  editingId = null;
  $("dialogTitle").textContent = "发布公告";
  $("confirmBtn").textContent = "发布";
  $("annTitle").value = "";
  $("annContent").value = "";
  targets = [];
  checkedSet.clear();
  renderSelList();
  refreshCheckedMarks();
  $("dialog").classList.remove("hidden");
  setTimeout(() => $("annTitle").focus(), 50);
}

function openEditDialog(item) {
  editingId = item.id;
  $("dialogTitle").textContent = "编辑公告";
  $("confirmBtn").textContent = "保存";
  $("annTitle").value = item.title || "";
  $("annContent").value = item.content || "";
  targets = (item.targets || []).map((t) => ({ ...t }));
  checkedSet.clear();
  renderSelList();   // 展示名依赖 NAME_MAP，loadOrgTree 异步完成后会重绘一次
  refreshCheckedMarks();
  $("dialog").classList.remove("hidden");
  setTimeout(() => $("annTitle").focus(), 50);
}

async function confirmPublish() {
  const title = $("annTitle").value.trim();
  const content = $("annContent").value.trim();
  if (!title) return toast("请输入公告标题", true);
  if (!content) return toast("请输入公告内容", true);
  if (!targets.length) return toast("请在右侧选择可见范围", true);

  const btn = $("confirmBtn");
  btn.disabled = true;
  try {
    const payload = { title, content, targets };
    if (editingId) {
      await api(`${API}/announcements/${encodeURIComponent(editingId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast("公告已保存");
    } else {
      await api(API + "/announcements", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast("公告已发布");
    }
    closePublishDialog();
    loadList();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

function closePublishDialog() {
  $("dialog").classList.add("hidden");
}

/* ==================== 删除 ==================== */
function openDeleteConfirm(item) {
  deletingId = item.id;
  $("confirmText").textContent =
    `确定删除公告「${item.title}」？删除后不可恢复。`;
  $("confirm").classList.remove("hidden");
}

async function doDelete() {
  if (!deletingId) return;
  try {
    await api(`${API}/announcements/${encodeURIComponent(deletingId)}`, { method: "DELETE" });
    toast("公告已删除");
    loadList();
  } catch (e) {
    toast(e.message, true);
  } finally {
    deletingId = null;
    $("confirm").classList.add("hidden");
  }
}

/* ==================== 事件绑定与初始化 ==================== */
$("publishBtn").addEventListener("click", openPublishDialog);
$("cancelBtn").addEventListener("click", closePublishDialog);
$("confirmBtn").addEventListener("click", confirmPublish);
$("addBtn").addEventListener("click", addCheckedToTargets);
$("selectAllBtn").addEventListener("click", selectAllUnits);
$("clearTargetsBtn").addEventListener("click", clearTargets);
$("dialog").addEventListener("click", (e) => {
  if (e.target === $("dialog")) closePublishDialog();
});
$("confirmCancelBtn").addEventListener("click", () => {
  deletingId = null;
  $("confirm").classList.add("hidden");
});
$("confirmOkBtn").addEventListener("click", doDelete);
$("confirm").addEventListener("click", (e) => {
  if (e.target === $("confirm")) {
    deletingId = null;
    $("confirm").classList.add("hidden");
  }
});
$("detailCloseBtn").addEventListener("click", closeDetail);
$("detailDialog").addEventListener("click", (e) => {
  if (e.target === $("detailDialog")) closeDetail();
});

loadConfig();
loadOrgTree();
loadList();
