#!/usr/bin/env node
/* 插件商店页状态机台架（2026-10-13 需求①②）：node + 最小 DOM **直跑真实 store.js**。
 *
 * 用法：node tools/e2e/store-page-state-tests.js    （退出码 0=全过）
 *       python tools\e2e\store-page-state-tests.py  （包装：无 node 时跳过）
 *
 * 覆盖（断言的是真文件里的渲染/交互逻辑，不是复刻）：
 *   A 依赖页签：暂存组件（pending）→ 按钮「待重启」；已生效 → 「已安装」；卸载按钮带进度条结构
 *     （.st-dl-fill/.st-dl-label —— 需求①）
 *   B 卸载点击：POST 携带 progress_id（前端 token）+ 400ms 轮询进度端点，标签随 pct 走（需求①）
 *   C 安装点击（hybrid 插件 restart_required）：装完按钮定格「待重启」而非「安装」（需求②）
 *   D 卸载撤销待重启态 → 回「安装」
 *   E 依赖安装（dep-slot restart_required）+ 用户选「稍后」→ **依赖卡片**定格「待重启」（需求③）
 *   F 联合安装里一并装上的依赖（dep_results[].pending）同样定格，且未要求重启的插件不被误定格
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.dirname(path.dirname(__dirname));
const STORE_JS = path.join(ROOT, "plugins", "store", "frontend", "store.js");

const FAILS = [];
const ALL = [];
function check(name, ok, extra) {
  ALL.push(name);
  if (!ok) FAILS.push(name);
  console.log((ok ? "ok   " : "FAIL ") + name + (extra ? "   " + extra : ""));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- 最小 DOM ----------
function El(tag) {
  const self = this;
  this.tagName = String(tag || "div").toUpperCase();
  this.children = [];
  this.attrs = {};
  this.style = {};
  this.dataset = {};
  this.textContent = "";
  this.value = "";
  this.disabled = false;
  this.title = "";
  this._html = "";
  this._classes = new Set();
  this._listeners = {};
  this._qcache = {};
  this.classList = {
    add() { for (const c of arguments) self._classes.add(c); },
    remove() { for (const c of arguments) self._classes.delete(c); },
    contains: (c) => self._classes.has(c),
    toggle(c, on) {
      const want = on === undefined ? !self._classes.has(c) : !!on;
      if (want) self._classes.add(c); else self._classes.delete(c);
    },
  };
}
Object.defineProperty(El.prototype, "innerHTML", {
  get() { return this._html; },
  set(v) { this._html = String(v == null ? "" : v); this.children = []; },   // 与真 DOM 同效：重设清空子节点
});
El.prototype.setAttribute = function (k, v) {
  this.attrs[k] = String(v);
  if (k === "data-id") this.dataset.id = String(v);
  if (k === "data-ver") this.dataset.ver = String(v);
  if (k === "data-act") this.dataset.act = String(v);
};
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.appendChild = function (c) { this.children.push(c); return c; };
El.prototype.addEventListener = function (t, fn) {
  (this._listeners[t] = this._listeners[t] || []).push(fn);
};
El.prototype.querySelector = function (sel) {
  if (this._qcache[sel]) return this._qcache[sel];
  if (sel.charAt(0) === "." || sel.charAt(0) === "#") {          // 进度条填充/标签等子元素桩
    const stub = new El("span");
    this._qcache[sel] = stub;
    return stub;
  }
  return null;
};
El.prototype.closest = function () { return null; };

function makeDom() {
  const docEl = new El("html");
  docEl._classes.add("jz-shell");                                // 壳内（桌面端）路径
  const tabs = [];
  for (const cat of ["plugin", "dep"]) {
    const t = new El("button");
    t.setAttribute("data-cat", cat);
    if (cat === "plugin") t._classes.add("active");
    tabs.push(t);
  }
  const byId = {
    "st-q": Object.assign(new El("input"), { value: "" }),
    "st-grid": new El("main"),
  };
  const body = new El("body");
  const doc = {
    documentElement: docEl,
    body,
    createElement: (t) => new El(t),
    addEventListener: (t, fn) => { (doc._listeners[t] = doc._listeners[t] || []).push(fn); },
    _listeners: {},
    querySelector(sel) {
      if (sel === ".st-tab.active") return tabs.find((x) => x._classes.has("active")) || null;
      if (sel === "#st-q") return byId["st-q"];
      if (sel === "#st-grid") return byId["st-grid"];
      if (sel.charAt(0) === "#") return byId[sel.slice(1)] || null;
      return null;
    },
    querySelectorAll(sel) { return sel === ".st-tab" ? tabs : []; },
    getElementById(id) { return byId[id] || null; },
    _byId: byId,
  };
  const origAppend = El.prototype.appendChild;
  body.appendChild = function (c) {                              // toast 之类按 id 注册（msg 会回查 #st-toast）
    if (c && c.attrs && c.attrs.id) byId[c.attrs.id] = c;
    if (c && c.id) byId[c.id] = c;
    return origAppend.call(this, c);
  };
  return { doc, tabs, byId };
}

// ---------- 世界（fetch 路由 + 状态） ----------
function makeWorld() {
  const st = {
    installs: {},                       // 账号登记（空 {} = 账号态可用；不装任何东西）
    plugins: [],                        // /local-api/plugins → plugins
    frontends: [],
    components: [],
    catalog: { packages: [] },
    preflight: { installable: true, deps: [] },
    installResp: { ok: true },
    uninstallResp: { ok: true },
    progress: { status: "unknown" },
    restartResp: { ok: true },
    requests: [],
    intervals: [],
    pendingPost: null,                  // {prefix, _res}：让 POST 悬停，好观测轮询/中间态
  };
  const J = (obj) => Promise.resolve({
    json: () => Promise.resolve(obj),
    text: () => Promise.resolve(JSON.stringify(obj)),
  });
  st.fetch = function (url, opts) {
    const method = (opts && opts.method) || "GET";
    let body = {};
    try { body = opts && opts.body ? JSON.parse(opts.body) : {}; } catch (e) { /* 原样 */ }
    st.requests.push({ url: String(url), method, body });
    if (st.pendingPost && method === "POST"
        && String(url).indexOf(st.pendingPost.prefix) === 0) {
      const hold = st.pendingPost;
      return new Promise((res) => {
        hold._res = () => res(J(st[hold.respKey || "uninstallResp"]));
      });
    }
    if (url.indexOf("/plugin/store/api/catalog") === 0) return J(st.catalog);
    if (url.indexOf("/plugin/store/api/installs") === 0 && method === "GET") {
      return J({ ok: true, installs: st.installs });
    }
    if (url.indexOf("/plugin/store/api/installs") === 0) return J({ ok: true, installs: st.installs });
    if (url.indexOf("/local-api/plugins") === 0 && method === "GET") {
      return J({ plugins: st.plugins, server_frontends: st.frontends, components: st.components });
    }
    if (url.indexOf("/local-api/store/preflight") === 0) return J(st.preflight);
    if (url.indexOf("/local-api/store/install/progress") === 0) return J({ status: "unknown" });
    if (url.indexOf("/local-api/store/install") === 0) return J(st.installResp);
    if (url.indexOf("/local-api/store/uninstall-dep/progress") === 0) return J(st.progress);
    if (url.indexOf("/local-api/store/uninstall-dep") === 0) return J(st.uninstallResp);
    if (url.indexOf("/local-api/plugins/uninstall") === 0) return J({ ok: true, data_purged: false });
    if (url.indexOf("/local-api/restart") === 0) return J(st.restartResp);
    return J({ ok: false, error: "mock 未实现：" + url });
  };
  return st;
}

function boot(world) {
  const { doc, tabs, byId } = makeDom();
  const win = {};
  const sandbox = {
    window: win,
    document: doc,
    fetch: world.fetch,
    Promise, Date, Math, JSON, String, Number, Object, Array, RegExp, Error,
    encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN,
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: (fn, ms) => { world.intervals.push({ fn, ms }); return world.intervals.length; },
    clearInterval: (id) => { if (id) world.intervals[id - 1] = null; },
    console,
  };
  sandbox.self = sandbox;
  sandbox.top = sandbox;
  win.self = sandbox;
  win.top = sandbox;
  win.JZDialog = { open: (o) => Promise.resolve(!o || o.title !== "需要重启") };   // 重启询问默认「稍后」
  win.window = win;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(STORE_JS, "utf8"), sandbox, { filename: "store.js" });
  return { sandbox, doc, tabs, byId, grid: byId["st-grid"] };
}

function cardOf(grid, id) {
  return grid.children.find((c) => c.getAttribute("data-id") === id) || null;
}
function clickAct(page, act, id, ver) {
  const btn = new El("button");
  btn.setAttribute("data-act", act);
  btn.setAttribute("data-id", id);
  if (ver) btn.setAttribute("data-ver", ver);
  page.doc._listeners.click[0]({ target: { closest: (sel) => (sel === "button[data-act]" ? btn : null) } });
  return btn;
}
function lastReq(world, prefix, method) {
  for (let i = world.requests.length - 1; i >= 0; i--) {
    const r = world.requests[i];
    if (r.url.indexOf(prefix) === 0 && (!method || r.method === method)) return r;
  }
  return null;
}

(async function main() {
  const world = makeWorld();
  world.catalog = { packages: [
    { id: "depx", runtime: "dep-slot", name: "依赖X", version: "1.0.0", size: 100, requires: {} },
    { id: "ready", runtime: "dep-slot", name: "依赖就绪", version: "1.0.0", size: 100, requires: {} },
    { id: "depy", runtime: "dep-slot", name: "依赖Y", version: "1.0.0", size: 100, requires: {} },
    { id: "depjoint", runtime: "dep-slot", name: "依赖联合", version: "1.0.0", size: 100,
      requires: {} },
    { id: "local-svc", runtime: "local", name: "本地插件", version: "2.0.0", size: 100, requires: {} },
    { id: "svc-a", runtime: "server", name: "在线插件", version: "3.0.0", size: 100,
      frontend_file: "svc-a-fe.zip", frontend_size: 10, requires: {} },
  ] };
  world.components = [
    { id: "depx", version: "1.0.0", pending: true },        // 暂存（DLL 待重启）
    { id: "ready", version: "1.0.0", pending: false },      // 已生效
    { id: "depjoint", version: "1.0.0", pending: false },   // 已生效（会被联合安装改成待重启）
  ];
  const page = boot(world);
  await sleep(40);                                          // 初次加载：installs → 本地态 → 目录 → render

  // ---- A：依赖页签按钮三态（需求②的持久态一半 + 需求①的进度条结构） ----
  page.tabs[1]._listeners.click[0]();                        // 切「依赖」页签
  const depx = cardOf(page.grid, "depx");
  const ready = cardOf(page.grid, "ready");
  check("A1 暂存组件 → 按钮「待重启」（不再翻回「安装」）",
    depx && depx.innerHTML.indexOf("待重启") >= 0 && depx.innerHTML.indexOf(">安装<") < 0
      && depx.innerHTML.indexOf("st-btn-pending") >= 0,
    depx ? depx.innerHTML.slice(0, 140) : "无卡片");
  check("A2 已生效组件 → 按钮「已安装」", !!ready && ready.innerHTML.indexOf("已安装") >= 0,
    ready ? ready.innerHTML.slice(0, 140) : "无卡片");
  check("A3 卸载按钮含进度条结构（.st-dl-fill/.st-dl-label + data-label）——需求①",
    !!ready && ready.innerHTML.indexOf("uninstall-dep") >= 0
      && ready.innerHTML.indexOf("st-dl-fill") >= 0 && ready.innerHTML.indexOf("st-dl-label") >= 0
      && ready.innerHTML.indexOf('data-label="卸载"') >= 0,
    ready ? ready.innerHTML.slice(-220) : "无卡片");

  // ---- B：卸载点击 → progress_id + 轮询进度端点（需求①） ----
  world.progress = { status: "running", phase: "卸载组件文件", pct: 42 };
  world.pendingPost = { prefix: "/local-api/store/uninstall-dep" };   // 悬停 POST：先观测轮询
  const unBtn = clickAct(page, "uninstall-dep", "ready");
  await sleep(30);
  const unReq = lastReq(world, "/local-api/store/uninstall-dep", "POST");
  check("B1 卸载请求携带 progress_id（前端生成 token）",
    !!unReq && typeof unReq.body.progress_id === "string"
      && unReq.body.progress_id.indexOf("un-") === 0, unReq ? JSON.stringify(unReq.body) : "无请求");
  const poll = world.intervals.filter(Boolean).find((x) => x.ms === 400);
  if (poll) poll.fn();                                       // 手动推一次 400ms 轮询
  await sleep(20);
  const progReq = lastReq(world, "/local-api/store/uninstall-dep/progress");
  check("B2 轮询卸载进度端点（同 token）",
    !!progReq && !!unReq && progReq.url.indexOf(encodeURIComponent(unReq.body.progress_id)) >= 0,
    progReq ? progReq.url : "无轮询");
  const unLabel = unBtn.querySelector(".st-dl-label");
  check("B3 按钮标签随服务端 pct 走（卸载中 42%）",
    unBtn.disabled === true && unLabel.textContent === "卸载中 42%",
    unLabel.textContent + " / disabled=" + unBtn.disabled);
  world.components = [{ id: "depx", version: "1.0.0", pending: true }];   // 卸载完成：ready 组件消失
  world.uninstallResp = { ok: true, files_removed: 1 };
  world.pendingPost._res();
  world.pendingPost = null;
  await sleep(50);
  check("B4 卸载完成 → 卡片回「安装」（待重启态撤销）",
    (cardOf(page.grid, "ready") || { innerHTML: "" }).innerHTML.indexOf(">安装<") >= 0,
    (cardOf(page.grid, "ready") || { innerHTML: "无卡片" }).innerHTML.slice(0, 120));

  // ---- C：安装 hybrid 插件 → 装完定格「待重启」（需求②） ----
  page.tabs[0]._listeners.click[0]();                        // 切回「插件」页签
  world.installResp = { ok: true, id: "local-svc", restart_required: true,
                        hint: "类 C 插件需重启能力层生效" };
  const instBtn = clickAct(page, "install", "local-svc");
  await sleep(60);
  const preReq = lastReq(world, "/local-api/store/preflight", "POST");
  const insReq = lastReq(world, "/local-api/store/install", "POST");
  check("C1 预检 → 安装请求按序发出", !!preReq && !!insReq,
    JSON.stringify({ preflight: !!preReq, install: !!insReq }));
  check("C2 装完按钮定格「待重启」（restart_required；用户选「稍后」也一样）",
    (cardOf(page.grid, "local-svc") || { innerHTML: "" }).innerHTML.indexOf("st-btn-pending") >= 0
      && (cardOf(page.grid, "local-svc") || { innerHTML: "" }).innerHTML.indexOf("待重启") >= 0,
    (cardOf(page.grid, "local-svc") || { innerHTML: "无卡片" }).innerHTML.slice(0, 160));
  check("C3 安装按钮过程态为 pending 而非 done（视觉区分待重启）",
    instBtn._classes.has("pending") || instBtn._classes.has("prog"),
    [...instBtn._classes].join(","));

  // ---- D：卸载 → 撤销待重启态 ----
  world.pendingPost = { prefix: "/local-api/plugins/uninstall", respKey: "restartResp" };
  clickAct(page, "uninstall", "local-svc");                  // 账号级卸载（两段询问自动确认）
  await sleep(30);
  const unInst = lastReq(world, "/local-api/plugins/uninstall", "POST");
  check("D1 插件卸载请求已发出", !!unInst && unInst.body.id === "local-svc",
    unInst ? JSON.stringify(unInst.body) : "无请求");
  world.installs = {};                                       // 服务端登记已移除（与真实卸载一致）
  world.pendingPost._res();
  world.pendingPost = null;
  await sleep(50);                                           // 页面 loadInstalls → loadLocalState → render
  check("D2 卸载后按钮回「安装」（待重启态不残留）",
    (cardOf(page.grid, "local-svc") || { innerHTML: "" }).innerHTML.indexOf(">安装<") >= 0,
    (cardOf(page.grid, "local-svc") || { innerHTML: "无卡片" }).innerHTML.slice(0, 140));

  // ---- E：依赖安装 → 用户选「稍后」→ 依赖卡片定格「待重启」（2026-10-14 需求③） ----
  // 需求原文：依赖安装完成后，若用户未选择重启，依赖的「安装」按钮应显示为「待重启」。
  page.tabs[1]._listeners.click[0]();                        // 切「依赖」页签
  world.installResp = { ok: true, id: "depy", kind: "component", restart_required: true,
                        hint: "组件含 DLL，下次启动能力层时生效" };
  const depInstBtn = clickAct(page, "install", "depy");
  await sleep(60);
  const depInsReq = lastReq(world, "/local-api/store/install", "POST");
  check("E1 依赖卡片点「安装」→ 走 /local-api/store/install",
    !!depInsReq && depInsReq.body.id === "depy",
    depInsReq ? JSON.stringify(depInsReq.body) : "无请求");
  check("E2 依赖装完定格「待重启」（用户选「稍后」也不翻回「安装」）",
    (cardOf(page.grid, "depy") || { innerHTML: "" }).innerHTML.indexOf("st-btn-pending") >= 0
      && (cardOf(page.grid, "depy") || { innerHTML: "" }).innerHTML.indexOf("待重启") >= 0
      && (cardOf(page.grid, "depy") || { innerHTML: "" }).innerHTML.indexOf(">安装<") < 0,
    (cardOf(page.grid, "depy") || { innerHTML: "无卡片" }).innerHTML.slice(0, 160));
  check("E3 该依赖按钮过程态为 pending（不是 done「已完成」）",
    depInstBtn._classes.has("pending"), [...depInstBtn._classes].join(","));
  // 持久态一半：能力层把暂存的该组件报成 pending（页面重载后无会话定格，靠这条定格）
  world.components = world.components.concat([{ id: "depy", version: "1.0.0", pending: true }]);
  page.tabs[0]._listeners.click[0]();                        // 切「插件」
  page.tabs[1]._listeners.click[0]();                        // 切回「依赖」→ render()
  check("E4 刷新/重渲染后仍是「待重启」（components.pending 是持久判据）",
    (cardOf(page.grid, "depy") || { innerHTML: "" }).innerHTML.indexOf("待重启") >= 0,
    (cardOf(page.grid, "depy") || { innerHTML: "无卡片" }).innerHTML.slice(0, 140));

  // ---- F：联合安装一并装上的依赖同样定格（dep_results[].pending；2026-10-14 需求③） ----
  // 模拟响应：插件自身不需重启（restart_required=false），但这次一并装上的依赖组件是
  // DLL 类 → 能力层报 dep_results[].pending=true（真实来源：include_deps 联合安装）。
  page.tabs[0]._listeners.click[0]();                        // 切「插件」页签
  world.installResp = { ok: true, id: "local-svc", restart_required: false,
                        dep_results: [{ id: "depjoint", ok: true, pending: true, native: true }] };
  clickAct(page, "install", "local-svc");
  await sleep(60);
  check("F1 未要求重启的插件自身不被误定格（只是依赖待生效）",
    (cardOf(page.grid, "local-svc") || { innerHTML: "" }).innerHTML.indexOf("待重启") < 0,
    (cardOf(page.grid, "local-svc") || { innerHTML: "无卡片" }).innerHTML.slice(0, 160));
  page.tabs[1]._listeners.click[0]();                        // 切「依赖」
  check("F2 联合安装里的依赖卡片同样定格「待重启」",
    (cardOf(page.grid, "depjoint") || { innerHTML: "" }).innerHTML.indexOf("st-btn-pending") >= 0
      && (cardOf(page.grid, "depjoint") || { innerHTML: "" }).innerHTML.indexOf("待重启") >= 0
      && (cardOf(page.grid, "depjoint") || { innerHTML: "" }).innerHTML.indexOf(">安装<") < 0,
    (cardOf(page.grid, "depjoint") || { innerHTML: "无卡片" }).innerHTML.slice(0, 160));

  console.log("\n== 商店页状态机台架：%d 项，失败 %d 项 ==", ALL.length, FAILS.length);
  FAILS.forEach((n) => console.log("  失败：" + n));
  process.exit(FAILS.length ? 1 : 0);
})().catch((err) => {
  console.error("台架异常：", err);
  process.exit(2);
});
