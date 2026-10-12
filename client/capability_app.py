# -*- coding: utf-8 -*-
"""能力层装配（批次 1 T08/T09）：路由分区四路裁决 + 会话中继骨架 + 本地直出。

路由分区（方案 §6.1 表；业务逻辑禁入本层）：
  ① 本地直出：/ 、/tool/<id>（客户端自带 static 副本）、/plugin/local-*/ 、/local-admin（批次 4）
  ①′ server 前端包直出：/plugin/<id>/<file> 且命中已装前端包文件清单（批次 8 T-O2-4，
      需求⑧：未装/停用自动回退中继直出）
  ② 清单合并：/api/tools*（批次 3 T16；批次 8 起隐藏已停用条目）
  ③ 本地插件 API：/api/local/<id>/*（批次 4 T20 执行器，本批次占位）
  ④ 中继远程：其余 /api/* 与类 A /plugin/<id>/*
  ⑤ /local-api/*：客户端自有本地接口（配置/健康/商店安装），永不中继

中继实现沿用 T03 spike 已验证口径：请求体 spool 临时文件按确切 Content-Length
转发（直传 werkzeug 流会被 requests 判为未知长度转 chunked 而挂起）；响应流式回传；
REST 无 WS（方案 §2.1 已核实）。
"""
import json
import os
import re
import secrets
import shutil
import sys as _sys
import tempfile
import threading
import uuid
import time
from urllib.parse import quote as _urlquote

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import jz_deps
import jz_plugin_host
import local_registry
import pkg_install
import plugin_source
from client_config import hidden_tools
from jz_relay import JzRelay
from plugin_executor import PluginExecutor
from session_mirror import SessionMirror

_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}
_DROP_RESP = {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}

# 本地端口壳自证（2026-10-11 需求① G2-6）：壳（WebView2）启动经 /shell-boot 种持久
# Cookie，之后本端口的业务路由（含 /local-api/*）一律要求该 Cookie——浏览器直开
# 本地端口（托盘旧「用浏览器打开」那类路径）拿不到壳身份代理。Cookie 值=进程内
# 随机 run token（JZCLIENT_SHELL_TOKEN 环境变量仅供台架/验收覆盖）。
SHELL_COOKIE = "jz_shell"
_LOCAL_EXEMPT_EXACT = frozenset({"/favicon.ico", "/shell-boot"})

_GATE_BLOCK_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>需要接入密钥</title><style>
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f1f3f4;margin:0;
display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.12);
padding:32px 40px;max-width:520px;width:92%;text-align:center}
h1{font-size:19px;margin:0 0 8px;color:#b06000}p{color:#5f6368;line-height:1.7;font-size:14px}
a{display:inline-block;margin-top:14px;padding:9px 28px;background:#1a73e8;color:#fff;
border-radius:6px;text-decoration:none;font-size:14px}
</style></head><body><div class="card">
<h1>服务器要求接入密钥</h1>
<p>服务器已启用壳身份门：业务功能仅限桌面客户端访问。<br>
当前客户端的接入密钥未配置或不正确，请向管理员索取后填写。</p>
<a href="/connection" target="_top">打开连接设置</a>
</div></body></html>"""

# 未安装提示页（D7）：壳页对未登记插件不出裸 404，给可执行指引 + 返回主界面
_NOT_INSTALLED_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>插件未安装</title><style>
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f1f3f4;margin:0;
display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.12);
padding:32px 40px;max-width:520px;width:92%;text-align:center}
h1{font-size:19px;margin:0 0 8px;color:#b06000}p{color:#5f6368;line-height:1.7;font-size:14px}
a{display:inline-block;margin-top:14px;padding:9px 28px;background:#1a73e8;color:#fff;
border-radius:6px;text-decoration:none;font-size:14px}
</style></head><body><div class="card">
<h1>插件未安装</h1><p>「__TOOL_ID__」__REASON__。<br>请打开插件商店安装后再使用。</p>
<a href="/" target="_top">返回主界面</a>
</div></body></html>"""


class RemoteHealth:
    """远程健康状态（T09 探针口径：GET /login 匿名可达即认为服务在）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.ok = None          # None=未知（未探测），True/False
        self.last_error = ""

    def mark(self, ok, error=""):
        with self._lock:
            self.ok = ok
            if error:
                self.last_error = error

    def snapshot(self):
        with self._lock:
            return {"remote_ok": self.ok, "last_error": self.last_error}


def create_capability_app(build_cfg, client_cfg, address, static_dir,
                          local_plugins_dir, fallback_file, on_address_change=None,
                          data_dir=None, executor=None, log=None, shell_token=None):
    """装配能力层 Flask 应用。address=None 时所有页面请求走本地兜底页。

    shell_token：本地端口壳自证的 run token（client_main 生成并同步传给壳窗口造
    boot URL；缺省进程内随机）。"""
    _log = log or (lambda m: None)
    app = Flask(
        __name__,
        static_folder=static_dir,
        static_url_path="/static",
    )
    app.config["JSON_AS_ASCII"] = False
    health = RemoteHealth()
    upstream = requests.Session()
    state = {"address": address}
    # 壳自证 run token + 服务器壳门状态（G2-4：403 shell_required 的提示面）
    shell_run_token = shell_token or secrets.token_urlsafe(24)
    gate = {"blocked": False, "ts": 0.0, "message": ""}

    # ---- 会话镜像 / 本地插件注册表 / jz_relay 门面（批次 3 T14/T16/T17） ----
    from client_config import data_dir as _cfg_data_dir, whitelist_path, resolve_token, load_client_config
    _data_dir = data_dir or _cfg_data_dir(build_cfg)
    mirror = SessionMirror(os.path.join(_data_dir, "session.bin"), _log)
    local_manifests = local_registry.scan_local_plugins(local_plugins_dir, _log)
    try:
        with open(whitelist_path(), "r", encoding="utf-8") as f:
            whitelist = json.load(f)
    except Exception:
        whitelist = {"allow": []}
        _log("relay_whitelist.json 加载失败，jz_relay 全拒绝")
    relay = JzRelay(upstream, lambda: state["address"], mirror, whitelist, _log,
                    token_getter=lambda: resolve_token(build_cfg, load_client_config(build_cfg)))
    _sys.modules["jz_relay"] = relay          # 插件后端 import jz_relay 即用

    # ---- 本地插件身份注入（批次 9 T-O9-4 / F1，Q4 拍板）----
    # 登录/续期后经上游 /api/session 缓存真实身份：hybrid 经 jz_api provider 生效，
    # worker 经派发链 X-JZ-User 头生效（jz_api 垫片兜底读头）；离线/未登录降级匿名。
    _identity = {"user": None, "ts": 0.0}

    def refresh_identity():
        if state["address"] and time.time() - _identity["ts"] >= 300:
            try:
                r = upstream.get(state["address"] + "/api/session",
                                 headers=client_headers(), timeout=(3, 8))
                user = (r.json() or {}).get("user") if r.status_code == 200 else None
                _identity["user"] = user if isinstance(user, dict) else None
            except Exception:
                _identity["user"] = None
            _identity["ts"] = time.time()
        return _identity["user"]

    def invalidate_identity():
        _identity["user"] = None
        _identity["ts"] = 0.0

    _mirror_update, _mirror_clear = mirror.update_from_set_cookie, mirror.clear

    def _mirror_update_hook(set_cookie):
        _mirror_update(set_cookie)            # 登录/滑动续期/删除型 Cookie → 身份可能变化
        invalidate_identity()

    def _mirror_clear_hook():
        _mirror_clear()                       # 登出/401 → 身份立即作废
        invalidate_identity()
        _account_clear()                      # 账号上下文同步作废（D19：根切 _anonymous + 停 worker）

    mirror.update_from_set_cookie = _mirror_update_hook
    mirror.clear = _mirror_clear_hook

    # ---- 账号上下文与账号级安装（2026-10-11 账号级安装批次 G2-1/G2-2/G3-1，D1–D22）----
    # 登录/续期时缓存 username 并拉取账号安装登记（GET /plugin/store/api/installs，§2.1）；
    # 登出/401（镜像清空信号）清空。离线快照 account-state.json（账号+installs+ts）：
    # 启动时无服务器可达按快照显示（G3-5），联网后刷新覆盖。插件可见数据根随账号切换
    # （JZTOOLS_DATA_ROOT 每次现读 → 进程内即时生效；worker 由执行器显式注入，G3-2）。
    _account = {"username": None, "installs": None, "installs_ok": False,
                "ts": 0.0, "root": "", "signed_out": False}
    _account_state_path = os.path.join(_data_dir, "account-state.json")
    _users_root = os.path.join(_data_dir, "users")
    _on_root_change = {"fn": None}     # 数据根切换后重挂 hybrid 子应用（模块级缓存数据根的旧子应用退役）
    _builtin_ids = {"store", "admin"}  # 主体自带插件不参与安装登记（常驻入口，不参与装卸）
    _visible_cache = {"ids": None, "ts": 0.0}     # 服务器 /api/tools 可见清单（D22 停用优先判定源）
    _catalog_cache = {"flat": None, "ts": 0.0, "ok": False}   # 商店 flat 投影（云卡元数据源）

    def _account_dir(username):
        # D20：目录名 = quote(username, safe='')（可逆、防穿越、中文/空格安全）
        name = _urlquote(str(username or ""), safe="")
        if not name or name in (".", "..") or any(c in name for c in '\\/:*?"<>|'):
            return ""
        return name

    def _apply_account_root(username, reload_hybrids=True):
        """账号确定/变化 → 数据根切 users/<账号>（未登录 _anonymous，D19）；切号停常驻 worker。"""
        name = _account_dir(username)
        sub = os.path.abspath(os.path.join(_users_root, name if name else "_anonymous"))
        if not sub.startswith(os.path.abspath(_users_root) + os.sep):
            _log("账号数据根路径校验失败（拒绝 %r）" % username)
            return
        if os.environ.get("JZTOOLS_DATA_ROOT") != sub:
            os.environ["JZTOOLS_DATA_ROOT"] = sub
            if executor is not None:
                executor.stop_residents()     # G3-2：切号停常驻 worker（防持旧根写串数据）
            _log("插件数据根：%s" % sub)
            if reload_hybrids and _on_root_change["fn"]:
                try:
                    _on_root_change["fn"]()   # 重挂 hybrid（旧子应用可能模块级缓存了旧数据根）
                except Exception as exc:
                    _log("账号切换重挂本地插件失败：%r" % exc)
        _account["root"] = sub

    def _load_account_state():
        try:
            with open(_account_state_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict) and obj.get("username"):
                return obj
        except Exception:
            pass
        return None

    def _save_account_state():
        try:
            os.makedirs(_users_root, exist_ok=True)
            tmp = _account_state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"username": _account["username"] or "",
                           "installs": _account["installs"] or {},
                           "ts": time.time()}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _account_state_path)
        except Exception as exc:
            _log("account-state.json 写入失败：%r" % exc)

    def _account_clear():
        """登出/401（D19）：停 worker + 数据根切 _anonymous；快照保留内容但不展示。

        signed_out 只置位不清位（登录才复位）——mirror.clear 在一次登出里会被触发两次
        （删除型 Cookie 的镜像处理 + 401 统一信号），第二次 username 已空，若按
        had_account 重算会把第一次置的位抹掉（台架 C4 抓出）。"""
        had_account = bool(_account["username"])
        _account.update({"username": None, "installs": None, "installs_ok": False,
                         "ts": 0.0})
        if had_account:
            _account["signed_out"] = True
        _apply_account_root(None)

    def _account_reg():
        """当前账号登记 dict；账号模式未启用（未登录/旧服务器 404 降级）→ None（G2-7）。"""
        if not _account["installs_ok"] or not _account["username"]:
            return None
        return _account["installs"] if isinstance(_account["installs"], dict) else {}

    def _gate_resource(pid):
        """D7 会话可用性门：账号模式下未登记/账号停用的本机资源一律不可达。

        返回 (gated, allowed, reason)：gated=False = 账号模式未启用，调用方按旧逻辑放行。
        A 类附加条件（id 在服务器可见清单）由安装/预检面叠加（§2.6/D22）；页面门在清单
        取不到（离线）时不硬拦——内容面随后走中继兜底/登录页，不产生越权内容。
        """
        reg = _account_reg()
        if reg is None:
            return False, True, ""
        if pid in _builtin_ids:
            return False, True, ""       # 主体自带插件常驻可达（商店/后台入口）
        rec = reg.get(pid)
        if rec is None:
            return True, False, "未安装"
        if rec.get("enabled") is False:
            return True, False, "已被停用"
        return True, True, ""

    def _not_installed_page(pid, reason):
        """壳页可读提示（D7：不出裸 404），带返回主界面链接。

        用占位符替换而非 % 格式化——模板 CSS 含字面 `%`，%s 会撞车（台架抓出）。"""
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(pid or ""))
        html = (_NOT_INSTALLED_HTML
                .replace("__TOOL_ID__", safe)
                .replace("__REASON__", ("在当前账号%s" % reason) if reason else "未安装"))
        return Response(html, status=404, content_type="text/html; charset=utf-8")

    def _fetch_installs():
        """GET /plugin/store/api/installs（用户会话）。返回 (installs, status)：
        True=成功；False=旧服务器无该 API（404，降级旧行为 G2-7）；None=暂时不可达（保持现状）。"""
        if not state["address"]:
            return None, None
        try:
            r = upstream.get(state["address"] + "/plugin/store/api/installs",
                             headers=client_headers(), timeout=(5, 15))
        except requests.RequestException:
            return None, None
        if r.status_code == 200:
            try:
                inst = (r.json() or {}).get("installs")
                return (inst if isinstance(inst, dict) else {}), True
            except Exception:
                return None, None
        if r.status_code == 404:
            return None, False
        return None, None

    def _installs_write(action, ids, version="", source="store"):
        """写服务端账号登记（D18：先登记后落盘）。成功回包同步进内存（登记变化即时生效）。"""
        if not state["address"]:
            return False
        try:
            r = upstream.post(state["address"] + "/plugin/store/api/installs",
                              headers=client_headers(), timeout=(5, 15),
                              json={"action": action, "ids": list(ids),
                                    "version": version, "source": source})
        except requests.RequestException:
            return False
        if r.status_code != 200:
            return False
        try:
            inst = (r.json() or {}).get("installs")
            if isinstance(inst, dict) and _account["username"]:
                _account["installs"] = inst
                _account["installs_ok"] = True
                _save_account_state()
        except Exception:
            pass
        return True

    def _server_visible_ids():
        """服务器 /api/tools 可见清单 id 集合（60s 缓存；D22 停用优先的判定源）。
        取不到（离线/异常）返回 None —— 判定面按「不硬拦」处理。"""
        now = time.time()
        if _visible_cache["ids"] is not None and now - _visible_cache["ts"] < 60:
            return _visible_cache["ids"]
        ids = None
        if state["address"]:
            try:
                r = upstream.get(state["address"] + "/api/tools", headers=client_headers(),
                                 timeout=(5, 15))
                if r.status_code == 200:
                    d = r.json()
                    tools = d.get("tools") if isinstance(d, dict) else d
                    if isinstance(tools, list):
                        ids = {str(t.get("id") or "") for t in tools
                               if isinstance(t, dict) and t.get("id")}
            except Exception:
                ids = None
        _visible_cache.update({"ids": ids, "ts": now})
        return ids

    def _catalog_flat():
        """商店 flat 投影（60s 缓存；云卡元数据源 §2.2 第 4 点）。取不到返回 {}（降级）。"""
        now = time.time()
        if _catalog_cache["flat"] is not None and now - _catalog_cache["ts"] < 60:
            return _catalog_cache["flat"]
        flat, ok = {}, False
        if state["address"]:
            try:
                r = upstream.get(state["address"] + "/plugin/store/api/catalog",
                                 headers=client_headers(), timeout=(5, 15))
                if r.status_code == 200:
                    for p in (r.json() or {}).get("packages") or []:
                        if isinstance(p, dict) and p.get("id"):
                            flat[str(p["id"])] = p
                    ok = True
            except Exception:
                flat, ok = {}, False
        _catalog_cache.update({"flat": flat, "ts": now, "ok": ok})
        return flat

    def _migrate_legacy_data(username):
        """遗留**数据**归属（D2/G3-3）：<数据目录>/plugins/ → 首个登录账号
        users/<账号>/plugins/（move 一次；标记 users/.legacy-migrated 幂等；失败下轮重试）。
        其余账号空白起步（升级说明给出手工复制指引）。"""
        marker = os.path.join(_users_root, ".legacy-migrated")
        if os.path.isfile(marker):
            return
        name = _account_dir(username)
        if not name:
            return
        legacy = os.path.join(_data_dir, "plugins")
        try:
            os.makedirs(_users_root, exist_ok=True)
            moved = 0
            if os.path.isdir(legacy):
                dest_root = os.path.join(_users_root, name, "plugins")
                os.makedirs(dest_root, exist_ok=True)
                for entry in os.listdir(legacy):
                    if os.path.exists(os.path.join(dest_root, entry)):
                        continue          # 目标已存在不覆盖（幂等）
                    shutil.move(os.path.join(legacy, entry), os.path.join(dest_root, entry))
                    moved += 1
                try:
                    os.rmdir(legacy)      # 移空才删；有残留（目标占位）留下轮
                except OSError:
                    pass
            with open(marker, "w", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), username))
            _log("遗留插件数据已归属账号 %s（迁移 %d 项）" % (username, moved))
        except Exception as exc:
            _log("遗留数据迁移失败（下轮登录重试）：%r" % exc)

    def _seed_legacy_installs(username):
        """登记补种（D11）：升级遗留机器的**首个登录账号**一次性——集合 = 本机
        local-plugins/* + server-frontends/*（store/admin 不参与），尊重 hidden_tools
        （列表内不补种）；机器级 local_state 停用映射为该账号 enabled=False（§7.7/D4）。
        消费后清 hidden_tools 键；installs API 不可达（旧服务器）→ 不消费不清理留待下轮。
        dep-slot 组件不进登记（D6：依赖是机器能力非账号插件）。"""
        marker = os.path.join(_users_root, ".legacy-seeded")
        if os.path.isfile(marker):
            return
        name = _account_dir(username)
        if not name or not state["address"]:
            return
        ids = {str(m.get("id")) for m in local_manifests if m.get("id")}
        ids |= {str(fid) for fid in frontends}
        ids -= _builtin_ids
        ids -= set(hidden_tools(build_cfg))     # 尊重 hidden：列表内的不补种
        if not _installs_write("install", sorted(ids), source="migrated"):
            return                              # API 不可达：不消费不清理（下轮再试）
        try:
            os.makedirs(_users_root, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), username))
        except OSError:
            return
        # 机器级停用 → 账号级 enabled=False（一次性语义迁移，D4/§7.7）
        states_cfg = (load_client_config(build_cfg).get("local_state") or {})
        off = [p for p, v in states_cfg.items() if v is False and p in ids]
        if off:
            _installs_write("disable", off)
        # 消费后清除 hidden_tools 键（登记制取代本机隐藏，§2.4）
        from client_config import save_client_config
        cfg = dict(load_client_config(build_cfg))
        if cfg.get("hidden_tools"):
            cfg.pop("hidden_tools", None)
            save_client_config(build_cfg, cfg)
        _log("已为首个登录账号 %s 补种安装登记 %d 项（迁移）" % (username, len(ids)))

    def refresh_account(force=False):
        """登录/续期后同步账号上下文（60s TTL；force=登记写后即时校准）。"""
        user = refresh_identity()
        username = str((user or {}).get("username") or "")
        if not username:
            return
        changed = _account["username"] != username
        if changed:
            _account["username"] = username
            _account["signed_out"] = False      # 重登恢复显示（D19）
            _apply_account_root(username)
            _migrate_legacy_data(username)     # D2：首个登录账号继承遗留数据（幂等）
            _seed_legacy_installs(username)    # D11：登记补种（幂等）
        now = time.time()
        if force or changed or _account["installs"] is None or now - _account["ts"] >= 60:
            inst, ok = _fetch_installs()
            if ok is True:
                _account["installs"] = inst
                _account["installs_ok"] = True
                _account["ts"] = now
                _save_account_state()
            elif ok is False:
                if _account["installs_ok"]:
                    _log("服务器 installs API 不可用（404）：降级为机器级行为（G2-7）")
                _account["installs"] = None
                _account["installs_ok"] = False
                _account["ts"] = now
            # None=暂时不可达：保持现状（离线快照显示）

    # 启动：离线快照恢复（G3-5）——账号 + 登记按快照显示，数据根指向快照账号
    _snap = _load_account_state()
    if _snap and _snap.get("username") and isinstance(_snap.get("installs"), dict):
        _account.update({"username": str(_snap["username"]),
                         "installs": dict(_snap["installs"]),
                         "installs_ok": True, "ts": 0.0})
        _log("账号离线快照恢复：%s（按快照显示，联网后刷新）" % _snap["username"])
    _apply_account_root(_account["username"], reload_hybrids=False)

    def _cloud_item(pid, rec, meta, cat_entry, order, catalog_ok=True):
        """灰蒙版云卡条目（§2.2 第 3/4 点）：登记有、本机副本缺。元数据优先级 =
        /api/tools（A 类）> 商店 flat > 登记记录降级（缺图标用默认）。"""
        src = cat_entry or {}
        item = {
            "id": pid,
            "name": (meta or {}).get("name") or src.get("name") or pid,
            "description": (meta or {}).get("description") or src.get("description", ""),
            "icon": (meta or {}).get("icon") or src.get("icon") or "🧩",
            "accent": (meta or {}).get("accent") or src.get("accent") or "#4285F4",
            "entry": "index.html", "features": [],
            "category": (meta or {}).get("category") or src.get("category") or "本地插件",
            "category_id": (meta or {}).get("category_id") or "local",
            "order": order, "health": "ok", "health_degraded": [],
            "version": src.get("version") or rec.get("version") or "",
            "cloud": True, "machine_present": False, "account_installed": True,
        }
        if cat_entry is None and catalog_ok:
            item["delisted"] = True        # D21①：catalog 无 published 投影 → 标注已下架
        return item

    def _local_item_with_health(m, order):
        """本地插件网格条目 + 三态健康度（2026-10-12）。

        真机反馈：卸载全部依赖后本地插件（过滤器/轨迹速写等）**照常降级运行却无任何提示**——
        main.js 有 「功能降级」chip 的渲染（health/health_degraded），但本地条目此前把 health
        硬编码为 ok、没人给数据；三态结论只落在 /local-api/plugins 的 state 里。
        """
        item = local_registry.tool_item(m, order)
        st = local_states.get(m.get("id")) or {}
        status = st.get("status")
        if status in ("degraded", "blocked"):
            names = list(st.get("degraded") or []) or list(st.get("missing") or [])
            if not names and st.get("reason"):
                names = [str(st.get("reason"))]
            item["health"] = "degraded"
            item["health_degraded"] = names
        return item

    def _account_grid_payload(data, base, reg):
        """账号登记为显示全集（需求①）+ 双源匹配 + 云卡（§2.2 四象限；D22 组合口径）。"""
        by_id = {str(t.get("id") or ""): t for t in base if isinstance(t, dict)}
        local_by_id = {m.get("id"): m for m in local_manifests}
        machine_disabled = {p for p, v in
                            (load_client_config(build_cfg).get("local_state") or {}).items()
                            if v is False}    # 机器级停用=次级开关（本地管理页），保留过滤
        flat, flat_ok = _catalog_flat(), _catalog_cache.get("ok", False)
        tools, cloud = [], []
        order = max([t.get("order", 0) for t in base] or [0]) + 1000
        for pid in sorted(reg.keys()):
            rec = reg.get(pid) or {}
            if rec.get("enabled") is False:
                continue                          # 停用随账号（D4）：本账号不显示
            meta = by_id.get(pid)
            m = local_by_id.get(pid)
            if meta is not None:                  # A 类：id 在服务器可见清单（D22 求交）
                if m is not None or pid in frontends:
                    tools.append(meta)            # 正常卡（A=在线直出前端副本/API 中继）
                else:
                    cloud.append(_cloud_item(pid, rec, meta, flat.get(pid), order, flat_ok))
                    order += 1
                continue
            if m is not None:                     # B/C：本机副本在 → 正常卡（下架不影响已装者 D21①）
                if pid in machine_disabled:
                    continue
                tools.append(_local_item_with_health(m, order))
                order += 1
                continue
            if str(pid).startswith("local-"):     # B/C 副本缺 → 云卡（缺元数据时降级）
                cloud.append(_cloud_item(pid, rec, None, flat.get(pid), order, flat_ok))
                order += 1
            # 其余：A 类 meta 缺（服务器端停用/无权限，D22 规则 1/2）→ 不显示
        if not isinstance(data, dict):
            data = {}
        cats = data.get("categories")
        if isinstance(cats, list) and all(c.get("id") != "local" for c in cats):
            cats.append(dict(local_registry.LOCAL_CATEGORY))
        data["tools"] = tools + cloud
        data["account_mode"] = True
        return data

    try:
        import jz_api as _jz_api
        _jz_api.register_providers(get_session_user=refresh_identity)
    except Exception as exc:
        _log("jz_api 身份 provider 注册失败（本地插件按匿名降级）：%r" % exc)
    app.config["SESSION_MIRROR"] = mirror
    app.config["JZ_RELAY"] = relay
    app.config["LOCAL_MANIFESTS"] = local_manifests

    # ---- 类 C 子应用派发 + 类 B 执行器状态（批次 4 T19/T20；pylibs 注入复用主包机制） ----
    import jz_deps
    _program_dir = build_cfg.get("_program_dir") or os.getcwd()
    # 暂存中的依赖组件先应用（与主包 app.py 同口径：setup 之前、DLL 未映射时），再注入路径
    try:
        _applied = jz_deps.apply_pending_components(_program_dir)
        if _applied:
            _log("已应用暂存依赖组件：%s" % ", ".join(_applied))
    except Exception as _exc:
        _log("应用暂存依赖组件失败（保留暂存，下次启动再试）：%s" % _exc)
    jz_plugin_host.setup_dep_components(_program_dir, _log)
    # 装依赖组件后免重启：jz_deps.refresh_flags 重算前先补注入路径（与主包同款注册）
    jz_deps.register_component_rescanner(
        lambda: jz_plugin_host.setup_dep_components(_program_dir, _log))
    # server 前端包目录（需求⑧：<数据目录>\server-frontends\，与 local-plugins\ 同级）
    frontends_dir = os.path.join(_data_dir, "server-frontends")
    frontends = {f["id"]: f for f in pkg_install.scan_frontends(frontends_dir)}
    local_states = {}
    hybrid_apps = {}      # id → Flask 子应用（进程内；装卸/升级=字典操作，天然全热插拔）

    _verdicts_at = {"ts": 0.0}

    def _eval_local_verdict(m, pdir):
        """单插件三态结论（依赖/版本门）——纯读取，无副作用（不挂载、不改配置）。"""
        return jz_deps.evaluate(
            m.get("id"), pdir, m,
            app_dir=build_cfg.get("_program_dir"),
            app_version=(build_cfg.get("product") or {}).get("version"),
            plugin_api=build_cfg.get("plugin_api", "1.0"))

    def _refresh_local_verdicts(force=False, ttl=2.0):
        """只复评三态结论（不重挂 hybrid 子应用）——主界面/商店每次加载即读。

        2026-10-12 真机：依赖卸载后降级徽标只在重启后出现（三态只在启动/重扫时算过）。
        成本＝每插件读 installed-deps/components 两份清单 + 规则判定，纯文件级、无 import；
        再加 2 秒 TTL 兜住连续请求（force=True 用于依赖装卸后即时翻转）。
        保号：disabled（停用）/ error（挂载失败）原样保留，其余只更新三态字段。
        """
        now = time.time()
        if not force and now - _verdicts_at["ts"] < ttl:
            return
        _verdicts_at["ts"] = now
        for m in local_manifests:
            pid = m.get("id")
            cur = local_states.get(pid) or {}
            if cur.get("status") in ("disabled", "error"):
                continue
            pdir = os.path.join(local_plugins_dir, m.get("_dir", ""))
            try:
                v = _eval_local_verdict(m, pdir)
            except Exception as exc:
                _log("本地插件 %s 依赖复评异常（保留原状态）：%s" % (pid, exc))
                continue
            merged = {"status": v["status"], "missing": v.get("missing", []),
                      "degraded": v.get("degraded", []), "reason": v.get("reason", "")}
            if cur.get("mode"):
                merged["mode"] = cur["mode"]
            local_states[pid] = merged

    def _refresh_local_states():
        from client_config import load_client_config
        # 停用状态读磁盘最新配置（lp_state 免重启写盘后 rescan 立即生效，不用启动快照）
        states_cfg = (load_client_config(build_cfg).get("local_state") or {})
        for m in local_manifests:
            pid = m.get("id")
            pdir = os.path.join(local_plugins_dir, m.get("_dir", ""))
            enabled = states_cfg.get(pid, True) and m.get("enabled", True)
            if not enabled:
                local_states[pid] = {"status": "disabled"}
                hybrid_apps.pop(pid, None)
                continue
            if m.get("kind") == "hybrid":
                verdict = _eval_local_verdict(m, pdir)
                local_states[pid] = {"status": verdict["status"],
                                     "missing": verdict.get("missing", []),
                                     "degraded": verdict.get("degraded", []),
                                     "reason": verdict.get("reason", "")}
                if verdict["status"] == "blocked":
                    hybrid_apps.pop(pid, None)
                    _log("本地插件 %s 不可运行（%s），跳过挂载" % (pid, verdict.get("reason")))
                    continue
                try:
                    routes = jz_plugin_host.load_backend_module(local_plugins_dir, pid)
                    sub = Flask("local_" + pid.replace("-", "_"))
                    bp = getattr(routes, "bp", None)
                    if bp is not None:
                        sub.register_blueprint(bp)     # 挂载前缀由能力层剥除后派发
                    else:
                        reg = getattr(routes, "register", None)
                        if not callable(reg):
                            raise RuntimeError("未导出 register(app) 或 bp")
                        reg(sub)
                    hybrid_apps[pid] = sub            # 子应用派发：热插拔不受 Flask 首请求后注册限制
                    _log("本地插件子应用已挂载：%s" % pid)
                except Exception as exc:
                    local_states[pid] = {"status": "error", "reason": str(exc)}
                    _log("本地插件 %s 挂载失败，已隔离：%s" % (pid, exc))
            elif (m.get("entry") or {}).get("backend"):
                # worker 型同样三态评估（2026-10-12）：缺依赖要能在主界面标「功能降级」；
                # 运行期门由 worker 自己把（此处不改挂载/派发行为）
                try:
                    verdict = _eval_local_verdict(m, pdir)
                    local_states[pid] = {"status": verdict["status"],
                                         "missing": verdict.get("missing", []),
                                         "degraded": verdict.get("degraded", []),
                                         "reason": verdict.get("reason", ""),
                                         "mode": "worker"}
                except Exception as exc:
                    local_states[pid] = {"status": "ok", "mode": "worker"}
                    _log("本地插件 %s 依赖评估异常（按 ok 展示）：%s" % (pid, exc))
            else:
                # 纯前端本地插件同样三态评估（2026-10-12）：声明了依赖/版本门就要能在
                # 主界面标「功能降级」（此前该分支不落 state → 角标永远不出现）
                try:
                    verdict = _eval_local_verdict(m, pdir)
                    local_states[pid] = {"status": verdict["status"],
                                         "missing": verdict.get("missing", []),
                                         "degraded": verdict.get("degraded", []),
                                         "reason": verdict.get("reason", "")}
                except Exception as exc:
                    local_states[pid] = {"status": "ok"}
                    _log("本地插件 %s 依赖评估异常（按 ok 展示）：%s" % (pid, exc))

    _refresh_local_states()
    _probe = {"ts": 0.0, "ok": False}
    _probe_lock = threading.Lock()

    def _remote_alive():
        """5 秒缓存的轻量探针（GET /login，T09 口径）。

        主界面/工具页是本地直出，远程挂死时页面本身仍会加载、前端 API 会卡
        「加载中」——导航门控在这里：探针失败就把页面导航换成本地兜底页，
        远程恢复后自动放行。缓存 5 秒避免每次导航都打远程。
        """
        now = time.time()
        with _probe_lock:
            if now - _probe["ts"] < 5:
                return _probe["ok"]
        ok = False
        if state["address"]:
            try:
                upstream.get(state["address"] + "/login", timeout=2)
                ok = True
            except requests.RequestException:
                ok = False
        with _probe_lock:
            _probe["ts"] = now
            _probe["ok"] = ok
        if not ok:
            health.mark(False, "探针失败：远程不可达")
        return ok

    def _fallback_page():
        resp = send_from_directory(fallback_dir_or(app), "fallback.html")
        resp.status_code = 503
        return resp

    # ---------------- 本地端口壳自证（G2-6，永不中继） ----------------

    @app.before_request
    def _shell_self_guard():
        """本端口的业务路由一律要求壳自证 Cookie；/static、favicon、boot 本身放行。"""
        path = request.path
        if path in _LOCAL_EXEMPT_EXACT or path.startswith("/static/"):
            return None
        if request.cookies.get(SHELL_COOKIE) == shell_run_token:
            return None
        _log("本地端口壳自证拦截：%s %s（无壳 Cookie，疑似浏览器直开）" % (request.method, path))
        return Response(_GATE_BLOCK_HTML, status=403, content_type="text/html; charset=utf-8")

    @app.get("/shell-boot")
    def shell_boot():
        """壳窗口的启动引导：校验 run token → 种持久 Cookie → 302 到干净 URL。

        Cookie 带一年 Max-Age（WebView2 持久 profile 存得下）；每次启动壳都会重新
        走 boot，即使 Cookie 被 profile 清理也能自愈。next 仅接受本站相对路径。
        """
        if request.args.get("_shell", "") != shell_run_token:
            _log("shell-boot 校验失败（run token 不符）")
            return Response(_GATE_BLOCK_HTML, status=403, content_type="text/html; charset=utf-8")
        nxt = request.args.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        resp = Response(status=302)
        resp.headers["Location"] = nxt
        resp.set_cookie(SHELL_COOKIE, shell_run_token, max_age=365 * 24 * 3600,
                        httponly=True, samesite="Lax", path="/")
        return resp

    @app.get("/connection")
    def connection_page():
        """连接设置页（fallback.html 复用）：地址 + 接入密钥；壳内导航/提示页跳转用。"""
        return send_from_directory(fallback_dir_or(app), "fallback.html")

    # ---------------- 本地接口（永不中继） ----------------

    @app.get("/local-api/config")
    def local_config():
        from client_config import resolve_token, load_client_config
        return jsonify({
            "address": state["address"] or "",
            "token_set": bool(resolve_token(build_cfg, load_client_config(build_cfg))),
            "gate_blocked": gate["blocked"],
            "gate_message": gate["message"],
            "product": build_cfg.get("product"),
            "webview2": build_cfg.get("webview2"),
            "health": health.snapshot(),
        })

    @app.post("/local-api/config/address")
    def local_set_address():
        data = request.get_json(force=True, silent=True) or {}
        from client_config import normalize_address, save_client_config
        addr = normalize_address(data.get("address"))
        cfg = dict(client_cfg)
        cfg["remote_address"] = addr or ""
        # 接入密钥（G2-3）：随地址同一表单保存；显式传 token 才改（旧前端只传 address 不受影响）
        if "token" in data:
            cfg["remote_token"] = str(data.get("token") or "").strip()
        save_client_config(build_cfg, cfg)
        state["address"] = addr
        gate["blocked"] = False          # 配置已变，门状态重判（下一次请求见分晓）
        gate["message"] = ""
        if on_address_change:
            try:
                on_address_change(addr)
            except Exception:
                pass
        from client_config import resolve_token, load_client_config
        return jsonify({"ok": True, "address": addr or "",
                        "token_set": bool(resolve_token(build_cfg, load_client_config(build_cfg)))})

    @app.get("/local-api/health")
    def local_health():
        return jsonify(dict(state["address"] and {"address": state["address"]} or {},
                            **health.snapshot()))

    # ---------------- 本地直出（主界面为客户端自带副本，页面只有一份） ----------------

    @app.get("/")
    def index():
        if not state["address"] or not _remote_alive():
            return _fallback_page()
        return send_from_directory(static_dir, "index.html")

    @app.get("/tool/<tool_id>")
    def tool_page(tool_id):
        # 会话可用性门（D7）：账号模式下未登记/账号停用的插件 → 可读提示页，不出裸 404
        gated, allowed, reason = _gate_resource(tool_id)
        if gated and not allowed:
            return _not_installed_page(tool_id, reason)
        # 工具外壳页是通用件：iframe 的 src 决定内容来自远程（类 A）还是本地（类 B/C）
        if tool_id.startswith("local-"):
            return send_from_directory(static_dir, "tool.html")   # 本地插件离线可用
        if not state["address"] or not _remote_alive():
            return _fallback_page()
        return send_from_directory(static_dir, "tool.html")

    @app.get("/favicon.ico")
    def favicon():
        try:
            return send_from_directory(static_dir, "favicon.ico")
        except Exception:
            return Response(b"", status=204)

    @app.get("/plugin/local-<plugin_id>/<path:filename>")
    def local_plugin_assets(plugin_id, filename):
        gated, allowed, _r = _gate_resource("local-" + plugin_id)   # D7：未登记 → 404
        if gated and not allowed:
            return jsonify({"error": "未安装", "plugin": "local-" + plugin_id}), 404
        return send_from_directory(local_plugins_dir, "local-%s/frontend/%s" % (plugin_id, filename))

    @app.get("/plugin/<plugin_id>/<path:filename>")
    def server_frontend_assets(plugin_id, filename):
        """server 前端包本地直出（需求⑧）：仅 GET 且文件命中已装前端包清单才本地伺服；
        其余（未安装/已停用/清单外路径/非 GET）照旧中继。

        账号级安装（D7/§2.2 收窄注）：未登记插件的页面资产一律 404——中继直出仅保留为
        已装副本缺失时的技术兜底（登记制取代其作为网格入口）。"""
        gated, allowed, reason = _gate_resource(plugin_id)
        if gated and not allowed:
            return jsonify({"error": "未安装", "plugin": plugin_id, "reason": reason}), 404
        fe = frontends.get(plugin_id)
        if request.method == "GET" and fe and _frontend_enabled(plugin_id) \
                and filename in (fe.get("files") or []):
            return send_from_directory(os.path.join(frontends_dir, plugin_id), filename)
        return forward("plugin/%s/%s" % (plugin_id, filename))

    @app.get("/local-admin")
    def local_admin():
        return send_from_directory(fallback_dir_or(app), "local_admin.html")

    # ---------------- 本地插件 API（批次 4 T20 执行器：类 B 子进程派发） ----------------

    def _find_local(plugin_id):
        return local_registry.find(local_manifests, plugin_id) or \
            local_registry.find(local_manifests, "local-" + plugin_id)

    @app.route("/api/local/<plugin_id>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    @app.route("/api/local/<plugin_id>/<path:rest>",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def local_plugin_api(plugin_id, rest=""):
        refresh_account()                     # 登记即时性（60s TTL；登录/切号在此收敛）
        m = _find_local(plugin_id)
        if m is not None:
            gated, allowed, reason = _gate_resource(m.get("id", plugin_id))   # D7 门
        else:
            g1 = _gate_resource(plugin_id)
            g2 = _gate_resource("local-" + plugin_id)
            gated = g1[0] or g2[0]
            allowed = (not g1[0] or g1[1]) and (not g2[0] or g2[1])
            reason = "未安装"
        if gated and not allowed:
            return jsonify({"error": "未安装", "plugin": plugin_id, "reason": reason}), 404
        if m is None:
            return jsonify({"error": "本地插件不存在"}), 404
        pid = m.get("id", plugin_id)
        st = local_states.get(pid, {})
        if st.get("status") == "blocked":
            return jsonify({"error": "本地插件不可运行（依赖缺失）",
                            "missing": st.get("missing", []), "hint": st.get("reason", "")}), 503
        if st.get("status") == "disabled":
            return jsonify({"error": "本地插件已停用"}), 404
        # hybrid → 进程内子应用；worker → 子进程执行器。两者都剥挂载前缀：
        # 显式 mount 优先；未声明（默认 worker）剥规范前缀 /api/local/<id>。
        mount = ((m.get("entry") or {}).get("mount") or "").rstrip("/")
        if not mount:
            mount = "/api/local/" + pid
        worker_path = request.path[len(mount):] or "/"
        qs = request.query_string.decode("utf-8", "replace")
        if qs:                                # 查询串随派发透传（file-filter mappings?limit 等在用）
            worker_path += "?" + qs
        worker_headers = {k: v for k, v in request.headers.items()
                          if k.lower() not in ("host", "content-length")}
        user = refresh_identity()             # F1：worker 链身份注入（仅本地回环派发链写入）
        if user:
            worker_headers["X-JZ-User"] = json.dumps(user, ensure_ascii=False)
        if m.get("kind") == "hybrid":
            sub = hybrid_apps.get(pid)
            if sub is None:
                return jsonify({"error": "本地插件子应用未挂载"}), 503
            with sub.test_client() as tc:
                resp = tc.open(worker_path, method=request.method,
                               headers=list(worker_headers.items()),
                               data=request.get_data())
            hop = {"connection", "keep-alive", "transfer-encoding", "content-length"}
            return Response(resp.data, status=resp.status_code,
                            headers=[(k, v) for k, v in resp.headers
                                     if k.lower() not in hop])
        if executor is None:
            return jsonify({"error": "子进程执行器未配置"}), 501
        status, hdrs, body = executor.call(
            m, request.method, worker_path, worker_headers,
            request.get_data(), m.get("timeout_ms") or 60000,
            user=(user or {}).get("username") or "-")      # V9：审计落真实身份
        hop = {"connection", "keep-alive", "transfer-encoding", "content-length"}
        return Response(body, status=status,
                        headers=[(k, v) for k, v in (hdrs or {}).items()
                                 if k.lower() not in hop])

    # ---------------- 本地插件管理（批次 4 T21，GD7 A+C） ----------------

    @app.get("/local-api/plugins")
    def lp_list():
        _refresh_local_verdicts()      # 商店页每次加载同批复评（与主界面同源）
        out = []
        for m in local_manifests:
            st = dict(local_states.get(m.get("id"), {}))
            out.append({"id": m.get("id"), "name": m.get("name"),
                        "version": m.get("version"), "kind": m.get("kind"),
                        "mount": (m.get("entry") or {}).get("mount"),
                        "enabled": st.get("status") != "disabled", "state": st,
                        "server_data": bool(m.get("server_data")),   # V5：数据出机明示
                        "requires": m.get("requires") or {}})
        # server 前端包（需求⑧）与已装依赖组件（dep-slot）：商店页状态感知的依据
        from client_config import load_client_config
        cfg_states = (load_client_config(build_cfg).get("local_state") or {})
        fronts = []
        for fid, f in frontends.items():
            fronts.append({"id": fid, "version": f.get("version"),
                           "files": len(f.get("files") or []),
                           "enabled": bool(cfg_states.get(fid, True))})
        return jsonify({"plugins": out, "store": build_cfg.get("plugin_store") or {},
                        "server_frontends": fronts,
                        "components": pkg_install.installed_components(_program_dir)})

    @app.post("/local-api/plugins/install")
    def lp_install():
        f = request.files.get("file")
        if f is None:
            return jsonify({"error": "未选择插件包"}), 400
        # 校验+落盘收口到 pkg_install 单入口（批次 8 接缝③；外部行为不变）。
        # 临时名唯一（P2）：并发上传不互踩（原固定 .upload.tmp 有对撞风险）。
        os.makedirs(local_plugins_dir, exist_ok=True)
        fd, spool = tempfile.mkstemp(prefix="upload-", suffix=".zip", dir=local_plugins_dir)
        os.close(fd)
        try:
            f.save(spool)
            result = pkg_install.install_local_zip(spool, local_plugins_dir,
                                                   local_manifests, _log)
        finally:
            try:
                os.remove(spool)
            except OSError:
                pass
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "安装失败")}), 400
        _rescan()
        return jsonify({"ok": True, "id": result["id"],
                        "restart_required": result.get("restart_required"),
                        "hint": result.get("hint", "")})

    @app.post("/local-api/plugins/uninstall")
    def lp_uninstall():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id", "")
        refresh_account()
        if _account_reg() is not None:
            # 账号级卸载（2026-10-11 需求①④/§2.4/D3/D18）：只移**账号登记**；
            # 本机副本保留为缓存（多账号共用，A 卸不删 B 的可用性，换回秒装）。
            if pid in _builtin_ids:
                return jsonify({"error": "内置插件不可卸载"}), 400
            if not _installs_write("uninstall", [pid]):
                return jsonify({"error": "卸载登记失败（服务器不可达），请稍后重试"}), 502
            purged = False
            if data.get("purge_data"):
                # 清除数据（可选，D9）：作用域 = 当前账号数据目录 users\<账号>\plugins\<id>
                import re as _re
                if not _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(pid)):
                    return jsonify({"error": "非法 id，拒绝清除数据"}), 400
                root = _account["root"] or _data_dir
                droot = os.path.join(root, "plugins", pid)
                base = os.path.abspath(os.path.join(root, "plugins"))
                if os.path.abspath(droot).startswith(base + os.sep) and os.path.isdir(droot):
                    shutil.rmtree(droot, ignore_errors=True)
                    purged = True
            return jsonify({"ok": True, "restart_required": False,
                            "data_purged": purged, "copy_kept": True})
        if not pid.startswith("local-"):
            # server 前端本地副本（需求⑧生命周期）：删副本 → 自动回退中继直出
            r = pkg_install.remove_frontend(frontends_dir, pid)
            if not r.get("ok"):
                return jsonify({"error": r.get("error", "卸载失败")}), 404
            _rescan_frontends()
            return jsonify({"ok": True, "restart_required": False})
        target = os.path.join(local_plugins_dir, pid)
        if not os.path.isdir(target):
            return jsonify({"error": "本地插件不存在"}), 404
        kind = next((m.get("kind") for m in local_manifests if m.get("id") == pid), "local")
        shutil.rmtree(target, ignore_errors=True)
        _rescan()
        return jsonify({"ok": True, "restart_required": kind == "hybrid", "data_purged": False})

    @app.post("/local-api/plugins/state")
    def lp_state():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id", "")
        if not pid.startswith("local-") and pid not in frontends:
            return jsonify({"error": "非法 id"}), 400
        from client_config import load_client_config, save_client_config
        cfg = dict(load_client_config(build_cfg))
        states = dict(cfg.get("local_state") or {})
        states[pid] = bool(data.get("enabled"))
        cfg["local_state"] = states
        save_client_config(build_cfg, cfg)
        if pid.startswith("local-"):
            _rescan()
            kind = next((m.get("kind") for m in local_manifests if m.get("id") == pid), "local")
            return jsonify({"ok": True,
                            "restart_required": kind == "hybrid" and data.get("enabled")})
        # server 前端：停用 = 本地伺服关闭自动回退中继直出（副本保留），即时生效
        return jsonify({"ok": True, "restart_required": False})

    @app.post("/local-api/plugins/check-update")
    def lp_check_update():
        """检查更新：经 Source 抽象读目录（server=商店 / share=共享盘，接缝①②收口）。"""
        from client_config import load_client_config
        src = plugin_source.make_source(build_cfg, load_client_config(build_cfg),
                                        upstream, lambda: state["address"], mirror, _log)
        catalog, err = src.get_catalog()
        if err:
            return jsonify({"error": err, "source": src.type, "updates": []}), 200
        updates = []
        installed = {m.get("id"): m.get("version") for m in local_manifests}
        for fid, f in frontends.items():                 # server 前端本地副本也参与比对
            installed.setdefault(fid, f.get("version"))
        for entry in catalog.get("packages") or []:
            pid = str(entry.get("id") or "")
            cur = installed.get(pid)
            if cur is None or _ver_lt(cur, entry.get("version", "")):
                updates.append({"id": pid, "version": entry.get("version"),
                                "file": entry.get("file", ""),
                                "changelog": entry.get("changelog", ""),
                                "runtime": entry.get("runtime", "local")})
        return jsonify({"updates": updates, "source": src.type})

    @app.post("/local-api/restart")
    def lp_restart():
        """类 C 升级 / DLL 依赖安装后的能力层重启（§6.7；会话镜像已持久化，免重登）。

        接棒方式（2026-10-08 改版）：新实例带 --await-pid <本进程 pid> 拉起，由**新实例**
        用 WaitForSingleObject 等旧进程真实退出后再抢单实例互斥——不看时间差，不用 cmd。
        原实现两处致命伤：①裸名 sys（本文件是 import sys as _sys）→ NameError → 端点
        从未成功执行过（前端只见 500 HTML，报「自动重启失败」）；②cmd+timeout 的 1 秒
        延迟在无控制台进程里不可靠。现打包态与源码态同法支持。"""
        import subprocess as _sp
        my_pid = os.getpid()
        if getattr(_sys, "frozen", False):
            argv = [_sys.executable, "--await-pid", str(my_pid)]
            cwd = os.path.dirname(_sys.executable)
        else:
            script = os.path.abspath(_sys.argv[0])
            argv = [_sys.executable, script, "--await-pid", str(my_pid)]
            cwd = os.path.dirname(script)
        flags = getattr(_sp, "DETACHED_PROCESS", 0) | getattr(_sp, "CREATE_NEW_PROCESS_GROUP", 0)
        # 壳自证 run token 随环境传给新实例（G2-6）：否则重启后新 token 与壳里已种的
        # Cookie 不匹配，自动重启后整个 UI 会被本地壳门挡在门外。
        child_env = dict(os.environ, JZCLIENT_SHELL_TOKEN=shell_run_token)
        try:
            proc = _sp.Popen(argv, cwd=cwd, creationflags=flags, close_fds=True, env=child_env)
        except Exception as exc:
            _log("restart：拉起新实例失败：%r（argv=%r）" % (exc, argv))
            return jsonify({"ok": False, "error": "拉起新实例失败：%s" % exc}), 500
        _log("restart：新实例已拉起 pid=%s（%s）；本进程 pid=%s 将于 0.5s 后退出接棒"
             % (proc.pid, " ".join(argv), my_pid))

        def _reexec():
            time.sleep(0.5)
            _log("restart：旧进程退出（os._exit 0）")
            os._exit(0)

        threading.Thread(target=_reexec, daemon=True, name="restart-exit").start()
        return jsonify({"ok": True, "note": "客户端即将重启（新实例待本进程退出后接棒）"})

    def _ver_lt(a, b):
        def nums(v):
            return [int(x) for x in re.findall(r"\d+", str(v))]
        return nums(a) < nums(b) if nums(a) and nums(b) else False

    # ---------------- 插件商店安装闭环（批次 8 T-O2-4，需求⑤⑧；§3.5） ----------------
    # 能力层独立取 catalog 与拉包（以用户会话；不信任页面传参的 URL/哈希，只收 id+version），
    # sha256 必校验，安装按 runtime 分派：local=完整插件 / server=前端包 / dep-slot=依赖包。

    def _store_source():
        from client_config import load_client_config
        return plugin_source.make_source(build_cfg, load_client_config(build_cfg),
                                         upstream, lambda: state["address"], mirror, _log)

    def _find_catalog_entry(catalog, pid, version):
        entry = next((p for p in catalog.get("packages") or []
                      if str(p.get("id")) == pid), None)
        if entry is None:
            return None, "商店目录中没有条目：%s" % pid
        if version:
            ver = entry if str(entry.get("version")) == str(version) else None
            if ver is None:
                return None, "条目 %s 没有版本 %s（可能已下架）" % (pid, version)
        else:
            ver = entry
        return ver, None

    def _pull_and_verify(src, pid, version, expect_sha, filename, artifact=None, progress=None):
        """拉包 → spool 落盘 → sha256 对 catalog 声明值（防传输损坏，C7）。返回本地路径或 (None, err)。"""
        spool_dir = os.path.join(_data_dir, "downloads")
        dest = os.path.join(spool_dir, os.path.basename(filename or ("%s-%s.zip" % (pid, version))))
        path, err = src.download(pid, version, dest, artifact=artifact, progress=progress)
        if err:
            return None, err
        if expect_sha:
            actual = pkg_install.sha256_file(path)
            if actual.lower() != str(expect_sha).lower():
                try:
                    os.remove(path)
                except OSError:
                    pass
                return None, "包 sha256 与目录声明不符（介质损坏或被篡改）"
        return path, None

    @app.post("/local-api/store/preflight")
    def store_preflight():
        """依赖三态预检（§3.5 ①）：requires 逐项评估 + 缺依赖映射到可装依赖包（O1 会合点）。"""
        data = request.get_json(force=True, silent=True) or {}
        pid = str(data.get("id") or "")
        version = str(data.get("version") or "")
        catalog, err = _store_source().get_catalog()
        if err:
            return jsonify({"error": err, "installable": False, "deps": []}), 200
        ver, err = _find_catalog_entry(catalog, pid, version)
        if err:
            return jsonify({"error": err, "installable": False, "deps": []}), 200
        # D22 安装门：server 条目必须仍在服务器可见清单（停用/无权限 → 拒绝并给可读原因；
        # 停用≠下架，商店目录不动，故必须由安装门补位）。清单取不到（离线）不硬拦。
        if (ver.get("runtime") or "") == "server":
            vis = _server_visible_ids()
            if vis is not None and pid not in vis:
                return jsonify({"error": "该插件在服务器端已停用或无权使用",
                                "installable": False, "deps": [],
                                "server_disabled": True}), 200
        manifest = {"requires": ver.get("requires") or {}}
        try:
            verdict = jz_deps.evaluate("store:" + pid, "", manifest,
                                       app_dir=_program_dir,
                                       app_version=(build_cfg.get("product") or {}).get("version"),
                                       plugin_api=build_cfg.get("plugin_api", "1.0"))
        except Exception as exc:      # 预检是建议性面（D-8：提示不阻断），评估炸了不拦安装
            _log("preflight 依赖评估异常（按可安装继续）：%s" % exc)
            return jsonify({"installable": True, "reason": "", "deps": [],
                            "preflight_error": str(exc)})
        # 缺依赖 → 商店依赖类别有对应包则标 resolve=dep-package（页面给「同时安装」勾选）；
        # external/vendored 行作提示性返回（P2-5）：不触发确认联动——vendored 随包自带此刻
        # 必然「缺失」（尚未安装），external 由插件挂载时的三态判定兜底展示。
        dep_entries = [p for p in catalog.get("packages") or []
                       if p.get("runtime") == "dep-slot"]
        # 服务端（A 类）条目的依赖属服务器侧：客户端装的是前端包，本地有没有 docx/flask
        # 不决定安装结果——行标 informational（前端不联动本地「同时安装」，2026-10-08）。
        server_side = (ver.get("runtime") == "server")
        deps = []
        for row in verdict.get("detail") or []:
            if row.get("state") == "ok":
                continue
            kind = row.get("kind")
            if kind == "python_package":
                name = str(row.get("name") or "")
                resolve = "missing"
                for dep in dep_entries:
                    provides = [str(x) for x in (dep.get("provides") or [])]
                    if dep.get("id") == name or name in provides:
                        deps.append({"name": name, "require": row.get("require"),
                                     "state": row.get("state"),
                                     "required": row.get("required", True),
                                     "informational": server_side,
                                     "resolve": "dep-package", "dep_id": dep.get("id"),
                                     "dep_version": dep.get("version")})
                        break
                else:
                    deps.append({"name": name, "require": row.get("require"),
                                 "state": row.get("state"),
                                 "required": row.get("required", True),
                                 "informational": server_side,
                                 "resolve": "missing"})
            elif kind in ("external", "vendored"):
                deps.append({"name": str(row.get("name") or ""),
                             "require": row.get("require"), "state": row.get("state"),
                             "required": row.get("required", True),
                             "resolve": "manual", "informational": True})
        blocked_framework = bool(verdict.get("reason"))
        return jsonify({"installable": not blocked_framework,
                        "reason": verdict.get("reason", ""),
                        "deps": deps})

    # 安装进度台账（2026-10-08：安装按钮内的进度条）。token 由前端生成随 install 请求带上，
    # 只读展示面：拉包占每包权重 35%、解压占 65%，按目录声明 size 加权折算单调百分比。
    _install_jobs = {}   # token → {status, phase, pct, error}

    @app.get("/local-api/store/install/progress")
    def store_install_progress():
        st = _install_jobs.get(request.args.get("id") or "")
        return jsonify(st or {"status": "unknown"})

    class _InstallProgress(object):
        """多包安装 → 单调百分比（权重=目录 size，缺省等权）。写台账失败静默，绝不影响安装。"""

        def __init__(self, token):
            self.token = token
            self.weights = [1]
            self.total = 1.0
            self.idx = 0
            self.done_w = 0.0
            self.pct = 0
            self.label = ""

        def set_plan(self, weights):
            self.weights = [max(int(w or 0), 1) for w in weights] or [1]
            self.total = float(sum(self.weights))
            self.idx, self.done_w = 0, 0.0
            self._push("准备", 0)

        def _push(self, phase, pct):
            self.pct = max(self.pct, min(99, int(pct)))
            if not self.token:
                return
            try:
                _install_jobs[self.token] = {"status": "running", "phase": phase,
                                             "pct": self.pct, "error": ""}
                while len(_install_jobs) > 200:          # 台账上界（淘汰最旧）
                    _install_jobs.pop(next(iter(_install_jobs)))
            except Exception:
                pass

        def _w(self):
            return self.weights[min(self.idx, len(self.weights) - 1)]

        def begin(self, idx, label):
            self.idx, self.label = idx, label
            self._push("准备安装 %s" % label, self.done_w / self.total * 100)

        def pull(self, received, total):
            frac = (float(received) / float(total)) if total else 0.0
            self._push("下载 %s" % self.label, (self.done_w + self._w() * 0.35 * frac)
                       / self.total * 100)

        def extract(self, done, total):
            frac = (float(done) / float(total)) if total else 0.0
            self._push("解压 %s" % self.label, (self.done_w + self._w() * (0.35 + 0.65 * frac))
                       / self.total * 100)

        def pkg_done(self):
            self.done_w += self._w()
            self._push("%s 完成" % self.label, self.done_w / self.total * 100)

    def _prog_cb(prog, kind):
        """进度回调包装（prog 为空返回 None；回调内异常吞掉，绝不打断安装）。"""
        if prog is None:
            return None

        def _cb(a, b):
            try:
                getattr(prog, kind)(a, b)
            except Exception:
                pass

        return _cb

    @app.post("/local-api/store/install")
    def store_install():
        """商店安装（§3.5 ③）：拉包 → sha256 → pkg_install 单入口 → 落位 → rescan。"""
        data = request.get_json(force=True, silent=True) or {}
        pid = str(data.get("id") or "")
        version = str(data.get("version") or "")
        include_deps = bool(data.get("include_deps"))
        src = _store_source()
        catalog, err = src.get_catalog()
        if err:
            return jsonify({"ok": False, "error": err}), 200
        ver, err = _find_catalog_entry(catalog, pid, version)
        if err:
            return jsonify({"ok": False, "error": err}), 200
        runtime = ver.get("runtime") or "local"
        # D22 安装门（§2.6）：server 条目不在服务器可见清单 → 拒绝（停用/无权限）
        if runtime == "server":
            vis = _server_visible_ids()
            if vis is not None and pid not in vis:
                return jsonify({"ok": False,
                                "error": "该插件在服务器端已停用或无权使用"}), 200
        # D18（§5）：先写账号登记（服务端确认）→ 再落本机副本。登记失败且登记面可用
        # → 拒绝（登记是网格显示全集，落了盘也不显示；报错可重试自愈）。
        # 旧服务器（installs 404 降级，G2-7）与 dep-slot（D6 依赖不进登记）照旧。
        refresh_account()
        if _account["installs_ok"] and runtime != "dep-slot":
            if not _installs_write("install", [pid],
                                   version=str(ver.get("version") or ""), source="store"):
                return jsonify({"ok": False,
                                "error": "安装登记失败（服务器不可达），请稍后重试"}), 200

        # 进度台账（可选）：权重 = 依赖包 size（按序）+ 主件 size；顺序与下方安装顺序一致
        prog = None
        main_idx = 0
        if data.get("progress_id"):
            prog = _InstallProgress(str(data.get("progress_id"))[:64])
            dep_w = []
            for dep in (data.get("deps") or []):
                dep_id = str(dep.get("dep_id") or "")
                if not dep_id:
                    continue
                de = next((p for p in catalog.get("packages") or []
                           if p.get("id") == dep_id and p.get("runtime") == "dep-slot"), None)
                dep_w.append((de or {}).get("size"))
            prog.set_plan(dep_w + [ver.get("size")])
            main_idx = len(dep_w)

        # ① 联合安装依赖（include_deps 勾选；dep-slot 条目逐个装，失败不阻断主件）
        dep_results = []
        if include_deps:
            dep_list = [d for d in (data.get("deps") or []) if str(d.get("dep_id") or "")]
            for i, dep in enumerate(dep_list):
                dep_id = str(dep.get("dep_id") or "")
                dep_ver = str(dep.get("dep_version") or "")
                d_entry = next((p for p in catalog.get("packages") or []
                                if p.get("id") == dep_id and p.get("runtime") == "dep-slot"), None)
                if d_entry is None or (dep_ver and str(d_entry.get("version")) != dep_ver):
                    dep_results.append({"id": dep_id, "ok": False, "error": "目录中找不到该依赖包"})
                    continue
                if prog:
                    prog.begin(i, "依赖 %s" % dep_id)
                d_path, d_err = _pull_and_verify(src, dep_id, d_entry.get("version"),
                                                 d_entry.get("sha256"), d_entry.get("file"),
                                                 progress=_prog_cb(prog, "pull"))
                if d_err:
                    dep_results.append({"id": dep_id, "ok": False, "error": d_err})
                    continue
                d_res = pkg_install.install_component_zip(d_path, _program_dir,
                                                          progress=_prog_cb(prog, "extract"))
                try:
                    os.remove(d_path)
                except OSError:
                    pass
                dep_results.append(dict(d_res, id=dep_id))
                if prog:
                    prog.pkg_done()
        if include_deps and dep_results:
            _refresh_local_verdicts(force=True)   # 依赖装完即复评：回主界面徽标即时消失

        # ② 主件按 runtime 分派
        if runtime == "server":
            # 需求⑧：桌面端「安装」server 条目 = 仅装前端包（页面本地直出，API 照旧中继）
            if not ver.get("frontend_file"):
                return jsonify({"ok": False,
                                "error": "该条目没有前端包工件（服务器侧尚未发布前端包）"}), 200
            if prog:
                prog.begin(main_idx, "前端包 %s" % pid)
            path, err = _pull_and_verify(src, pid, ver.get("version"),
                                         ver.get("frontend_sha256"), ver.get("frontend_file"),
                                         artifact="frontend",
                                         progress=_prog_cb(prog, "pull"))
            if err:
                return jsonify({"ok": False, "error": err, "dep_results": dep_results}), 200
            res = pkg_install.install_frontend_zip(path, frontends_dir,
                                                   progress=_prog_cb(prog, "extract"))
            try:
                os.remove(path)
            except OSError:
                pass
            if not res.get("ok"):
                return jsonify({"ok": False, "error": res.get("error"),
                                "dep_results": dep_results}), 200
            _rescan_frontends()
            return jsonify({"ok": True, "id": res["id"], "frontend": True,
                            "version": res.get("version"), "restart_required": False,
                            "dep_results": dep_results})

        if runtime == "dep-slot":
            if prog:
                prog.begin(main_idx, "依赖包 %s" % pid)
            path, err = _pull_and_verify(src, pid, ver.get("version"),
                                         ver.get("sha256"), ver.get("file"),
                                         progress=_prog_cb(prog, "pull"))
            if err:
                return jsonify({"ok": False, "error": err, "dep_results": dep_results}), 200
            res = pkg_install.install_component_zip(path, _program_dir,
                                                    progress=_prog_cb(prog, "extract"))
            try:
                os.remove(path)
            except OSError:
                pass
            if not res.get("ok"):
                return jsonify({"ok": False, "error": res.get("error"),
                                "dep_results": dep_results}), 200
            return jsonify({"ok": True, "id": res["id"], "kind": "component",
                            "restart_required": res.get("pending", False),
                            "hint": "组件含 DLL，下次启动能力层时生效"
                            if res.get("pending") else "",
                            "dep_results": dep_results})

        # runtime=local：完整本地插件包
        if not str(pid).startswith("local-"):
            return jsonify({"ok": False,
                            "error": "目录条目类别与包形态不符（local 条目须 local- 前缀）"}), 200
        if prog:
            prog.begin(main_idx, "插件包 %s" % pid)
        path, err = _pull_and_verify(src, pid, ver.get("version"), ver.get("sha256"),
                                     ver.get("file"), progress=_prog_cb(prog, "pull"))
        if err:
            return jsonify({"ok": False, "error": err, "dep_results": dep_results}), 200
        res = pkg_install.install_local_zip(path, local_plugins_dir, local_manifests, _log,
                                            progress=_prog_cb(prog, "extract"))
        try:
            os.remove(path)
        except OSError:
            pass
        if not res.get("ok"):
            return jsonify({"ok": False, "error": res.get("error"),
                            "dep_results": dep_results}), 200
        _rescan()
        return jsonify({"ok": True, "id": res["id"],
                        "restart_required": res.get("restart_required"),
                        "hint": res.get("hint", ""), "dep_results": dep_results})

    _dl_jobs = {}   # dl_id → {received,total,status,path,error,filename}（下载进度台账）

    @app.post("/local-api/store/download")
    def store_download():
        """桌面端「下载包」：启动后台拉包任务，返回任务 id（前端轮询 /download/progress）。"""
        data = request.get_json(force=True, silent=True) or {}
        pid = str(data.get("id") or "")
        version = str(data.get("version") or "")
        src = _store_source()
        catalog, err = src.get_catalog()
        if err:
            return jsonify({"ok": False, "error": err}), 200
        ver, err = _find_catalog_entry(catalog, pid, version)
        if err:
            return jsonify({"ok": False, "error": err}), 200
        # 下载语义（2026-10-08 需求）：全部插件都给「下载」（仅下载不安装）——
        # server 条目取**客户端工件（前端包）**（后端包不对用户暴露，留档走构建侧）；其余 = 自身包。
        artifact = None
        if (ver.get("runtime") or "local") == "server":
            if not ver.get("frontend_file"):
                return jsonify({"ok": False,
                                "error": "该条目没有前端包工件（服务器侧尚未发布前端包）"}), 200
            artifact = "frontend"
            filename = ver.get("frontend_file")
            expect_sha = ver.get("frontend_sha256")
        else:
            filename = ver.get("file") or ("%s-%s.zip" % (pid, ver.get("version")))
            expect_sha = ver.get("sha256")
        dl_id = uuid.uuid4().hex
        _dl_jobs[dl_id] = {"received": 0, "total": None, "status": "running",
                           "path": "", "error": "", "filename": filename}

        def _worker():
            st = _dl_jobs.get(dl_id)
            if st is None:
                return
            try:
                def _prog(received, total):
                    st["received"], st["total"] = received, total
                path, err = _pull_and_verify(src, pid, ver.get("version"),
                                             expect_sha, filename, artifact=artifact,
                                             progress=_prog)
                if err:
                    st["status"], st["error"] = "error", err
                else:
                    st.update(status="done", path=path)
            except Exception as exc:
                st["status"], st["error"] = "error", str(exc)

        threading.Thread(target=_worker, daemon=True, name="store-dl-" + dl_id[:8]).start()
        return jsonify({"ok": True, "id": dl_id, "filename": filename})

    @app.get("/local-api/store/download/progress")
    def store_download_progress():
        st = _dl_jobs.get(request.args.get("id") or "")
        if st is None:
            return jsonify({"ok": False, "status": "error", "error": "任务不存在"}), 200
        return jsonify({"ok": True, "received": st["received"], "total": st["total"],
                        "status": st["status"], "path": st["path"], "error": st["error"],
                        "filename": st["filename"]})

    @app.get("/local-api/store/downloaded")
    def store_downloaded():
        """校验下载状态（点击下载前先查）：catalog 文件名在 downloads 下存在且大小一致 → 已下载。"""
        pid = request.args.get("id") or ""
        version = request.args.get("version") or ""
        src = _store_source()
        catalog, err = src.get_catalog()
        if err:
            return jsonify({"ok": False, "error": err}), 200
        ver, err = _find_catalog_entry(catalog, pid, version)
        if err:
            return jsonify({"ok": False, "error": err}), 200
        filename = os.path.basename(str(ver.get("file") or ("%s-%s.zip" % (pid, version))))
        path = os.path.join(_data_dir, "downloads", filename)
        exists = os.path.isfile(path)
        try:
            size_ok = exists and (not ver.get("size")
                                  or os.path.getsize(path) == int(ver.get("size")))
        except (OSError, TypeError, ValueError):
            size_ok = exists
        return jsonify({"ok": True, "downloaded": bool(size_ok), "filename": filename})

    def _component_dirs(cid):
        pylibs = os.path.join(_program_dir, "runtime", "pylibs")
        return (os.path.join(pylibs, cid),
                os.path.join(pylibs, ".pending", cid),
                os.path.join(pylibs, ".pending-remove", cid))

    def _drop_dep_payload_files(cid):
        """按登记 entry.files 删组件载荷文件（排除其它组件仍声明的共享文件）+ 空目录剪枝。

        卸载端点与启动清扫共用（2026-10-12：清扫原来只删 .pending-remove/<cid> 这一层，
        组件铺在 pylibs 根的其余载荷会变孤儿——真机 numpy.libs 即实例）。→ (removed, failed)
        """
        try:
            entry = jz_deps.component_entry(_program_dir, cid)
        except Exception:
            entry = None
        if not entry:
            return 0, []
        pylibs = os.path.join(_program_dir, "runtime", "pylibs")
        others = set()
        try:
            for rel, ids in (jz_deps.component_file_owners(_program_dir) or {}).items():
                if any(str(x) != cid for x in ids):
                    others.add(rel)
        except Exception:
            others = set()
        removed, failed = 0, []
        for rel in [str(f).lstrip("\\/").replace("/", "\\") for f in (entry.get("files") or [])]:
            if not rel or rel.lower() == "manifest.json" or rel.lower() in others:
                continue
            pth = os.path.join(pylibs, rel)
            try:
                if os.path.isdir(pth):
                    shutil.rmtree(pth)
                    removed += 1
                elif os.path.exists(pth):
                    os.remove(pth)
                    removed += 1
            except OSError:
                failed.append(rel)
        for cur, subs, _fs in os.walk(pylibs, topdown=False):
            for d in subs:
                pd = os.path.join(cur, d)
                try:
                    if os.path.isdir(pd) and not os.listdir(pd):
                        os.rmdir(pd)
                except OSError:
                    pass
        return removed, failed

    def _sweep_dep_files_journal(path):
        """处理卸载台账 <cid>.json：逐项删 payload 文件；返回仍删不掉的项（写回台账）。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                rec = json.load(f) or {}
        except Exception:
            return []
        pylibs = os.path.join(_program_dir, "runtime", "pylibs")
        left = []
        for rel in (rec.get("files") or []):
            rel = str(rel).lstrip("\\/")
            q = os.path.join(pylibs, rel)
            try:
                if os.path.isdir(q):
                    shutil.rmtree(q)
                elif os.path.exists(q):
                    os.remove(q)
            except OSError:
                left.append(rel)
        if left:
            rec["files"] = left
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=1)
            except OSError:
                pass
        return left

    def _sweep_pending_component_removals():
        """启动清扫：.pending-remove 下的组件目录（上次卸载时被占用的）此刻未映射，直接删除。"""
        rm_root = os.path.join(_program_dir, "runtime", "pylibs", ".pending-remove")
        if not os.path.isdir(rm_root):
            return
        for name in os.listdir(rm_root):
            pth = os.path.join(rm_root, name)
            if os.path.isdir(pth):
                shutil.rmtree(pth, ignore_errors=True)
                _removed, failed = _drop_dep_payload_files(name)
                if failed:      # 仍被占用的落台账，下次启动再收
                    try:
                        with open(os.path.join(rm_root, name + ".json"), "w",
                                  encoding="utf-8") as f:
                            json.dump({"schema": 1, "id": name, "files": failed}, f,
                                      ensure_ascii=False, indent=1)
                    except OSError:
                        pass
                try:   # 清单同步（2026-10-12）：只删目录不更新清单 → 商店仍显示"已安装"
                    jz_deps.drop_component_entry(_program_dir, name)
                except Exception:
                    pass
                continue
            if name.endswith(".json"):      # 文件台账（卸载时被占用的载荷文件）
                cid = name[:-5]
                left = _sweep_dep_files_journal(pth)
                try:
                    jz_deps.drop_component_entry(_program_dir, cid)
                except Exception:
                    pass
                if not left:
                    try:
                        os.remove(pth)
                    except OSError:
                        pass
        try:
            os.rmdir(rm_root)
            _log("已清扫待卸载依赖组件（.pending-remove）")
        except OSError:
            pass

    _sweep_pending_component_removals()

    @app.post("/local-api/store/uninstall-dep")
    def store_uninstall_dep():
        r"""依赖组件卸载（2026-10-12 重写：以**登记为准、按清单 files 逐项删**）。

        原实现只看 pylibs/<cid> 目录：组件生效后载荷是铺在 pylibs 根（docx/、lxml/、
        公共 VC DLL 等顶层项），<cid> 目录通常不存在 → 误报"未安装"；且第一次卸载把
        pylibs/<cid> 删掉后登记未清 → 再次卸载 404（真机 docx 症状）。
        现口径：
          ① 有登记（manifest 条目）或存在目录 = 可卸载；
          ② 按 entry.files 逐项删（排除仍被其它已装组件声明的共享文件，如 msvcp140.dll）；
          ③ 空目录剪枝（删文件留空壳，而"已装组件"另有按目录列举的路径）；
          ④ 清登记（jz_deps.drop_component_entry）；
          ⑤ 被占用删不掉的写 .pending-remove/<cid>.json 台账，下次启动清扫收尾。
        """
        data = request.get_json(force=True, silent=True) or {}
        cid = str(data.get("id") or "")
        import re as _re
        if not _re.fullmatch(r"[A-Za-z0-9_+.-]{1,64}", cid):
            return jsonify({"ok": False, "error": "非法组件 id"}), 400
        try:
            entry = jz_deps.component_entry(_program_dir, cid)
        except Exception:
            entry = None
        active, staged, rm = _component_dirs(cid)
        if entry is None and not os.path.isdir(active) and not os.path.isdir(staged):
            return jsonify({"ok": False, "error": "该依赖组件未安装"}), 404
        removed = 0
        failed = []
        pending_remove = False
        # ① 暂存目录/同名目录（保留原语义）
        if os.path.isdir(staged):
            shutil.rmtree(staged, ignore_errors=True)   # 待应用暂存一并取消
        if os.path.isdir(active):
            try:
                shutil.rmtree(active)
                removed += 1
            except OSError:
                os.makedirs(os.path.dirname(rm), exist_ok=True)
                try:
                    if os.path.isdir(rm):
                        shutil.rmtree(rm, ignore_errors=True)
                    os.rename(active, rm)      # 同卷改名，句柄不再阻塞
                    pending_remove = True
                except OSError as exc:
                    return jsonify({"ok": False, "error": "删除失败：%s" % exc}), 500
        # ②③ 载荷文件（清单 files 为准）+ 空目录剪枝（与启动清扫共用助手）
        n_files, failed = _drop_dep_payload_files(cid)
        removed += n_files
        # ④ 登记清除
        try:
            jz_deps.drop_component_entry(_program_dir, cid)
        except Exception:
            pass
        # ⑤ 台账：删不掉的留给下次启动
        if failed:
            os.makedirs(os.path.dirname(rm), exist_ok=True)
            try:
                with open(os.path.join(os.path.dirname(rm), cid + ".json"), "w",
                          encoding="utf-8") as f:
                    json.dump({"schema": 1, "id": cid, "files": failed}, f,
                              ensure_ascii=False, indent=1)
                pending_remove = True
            except OSError:
                pass
        try:
            jz_plugin_host.setup_dep_components(_program_dir, _log)   # 重注入（已删路径无副作用）；
            # 依赖旗标由各插件 install_refresher 在请求期刷新（既有机制）
        except Exception:
            pass
        _refresh_local_verdicts(force=True)    # 回主界面即刻见降级徽标（免重启，2026-10-12 需求）
        note = ""
        if pending_remove:
            note = "部分文件被占用，将在下次启动客户端后清除"
        elif entry is not None and removed == 0:
            note = "组件文件此前已不在本机，已清除安装登记"
        return jsonify({"ok": True, "pending_remove": pending_remove,
                        "files_removed": removed, "files_failed": len(failed),
                        "note": note})

    @app.post("/local-api/store/deps-check")
    def store_deps_check():
        """本机依赖检测（2026-10-12 需求②：详情卡「检测本机依赖」）。

        与 preflight 同源口径（jz_deps.evaluate；unknown=不可核验，不按缺失处理），
        区别=返回**全部**明细行（含 ok）与汇总，且不改任何状态（纯只读展示面）。
        """
        data = request.get_json(force=True, silent=True) or {}
        pid = str(data.get("id") or "")
        version = str(data.get("version") or "")
        catalog, err = _store_source().get_catalog()
        if err:
            return jsonify({"ok": False, "error": err, "rows": []}), 200
        ver, err = _find_catalog_entry(catalog, pid, version)
        if err:
            return jsonify({"ok": False, "error": err, "rows": []}), 200
        manifest = {"requires": ver.get("requires") or {}}
        # 服务端（A 类）条目的依赖属服务器侧（2026-10-12 真机）：本机检测不决定其可用性——
        # 与 preflight 同口径标 informational；可用性以服务器 health 为准（主界面角标）。
        server_side = (ver.get("runtime") == "server")
        try:
            verdict = jz_deps.evaluate("deps-check:" + pid, "", manifest,
                                       app_dir=_program_dir,
                                       app_version=(build_cfg.get("product") or {}).get("version"),
                                       plugin_api=build_cfg.get("plugin_api", "1.0"))
        except Exception as exc:
            return jsonify({"ok": False, "error": "依赖评估异常：%s" % exc, "rows": []}), 200
        dep_entries = [p for p in catalog.get("packages") or []
                       if p.get("runtime") == "dep-slot"]
        rows = []
        for row in verdict.get("detail") or []:
            name = str(row.get("name") or "")
            item = {"name": name, "kind": row.get("kind"), "require": row.get("require"),
                    "actual": row.get("actual"), "state": row.get("state"),
                    "required": row.get("required", True), "note": row.get("note") or "",
                    "informational": server_side,
                    "resolve": "missing", "dep_id": "", "dep_version": ""}
            if row.get("kind") == "python_package":
                for dep in dep_entries:
                    provides = [str(x) for x in (dep.get("provides") or [])]
                    if dep.get("id") == name or name in provides:
                        item["resolve"] = "dep-package"
                        item["dep_id"] = dep.get("id")
                        item["dep_version"] = dep.get("version")
                        break
            elif row.get("kind") in ("external", "vendored"):
                item["resolve"] = "manual"
            rows.append(item)
        ok_n = sum(1 for r in rows if r.get("state") == "ok")
        return jsonify({"ok": True, "id": pid, "version": ver.get("version"),
                        "rows": rows, "ok_count": ok_n, "total": len(rows),
                        "status": verdict.get("status"), "server_side": server_side,
                        "note": "unknown=清单不可核验，不按缺失处理"})

    @app.post("/local-api/store/reveal")
    def store_reveal():
        """下载完成后打开 downloads 目录并定位文件（留档定位；失败原因显式返回）。

        用 explorer /select 而非 os.startfile：前者把窗口置前并选中文件，
        后者常被前台客户端窗口遮挡（真机反馈"提示已打开但看不到文件夹"）。"""
        import subprocess as _sp
        d = os.path.join(data_dir, "downloads")
        os.makedirs(d, exist_ok=True)
        data = request.get_json(silent=True) or {}
        p = str(data.get("path") or "")
        try:
            if p and os.path.isfile(p) and os.path.abspath(p).lower().startswith(os.path.abspath(d).lower()):
                _sp.Popen('explorer /select,"%s"' % os.path.abspath(p))
            else:
                os.startfile(d)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.get("/local-api/config/plugin-source")
    def plugin_source_get():
        from client_config import load_client_config
        cfg = plugin_source.load_source_config(build_cfg, load_client_config(build_cfg))
        return jsonify({"ok": True, "plugin_source": cfg})

    @app.post("/local-api/config/plugin-source")
    def plugin_source_set():
        data = request.get_json(force=True, silent=True) or {}
        ptype = data.get("type")
        if ptype not in ("server", "share"):
            return jsonify({"error": "type 仅支持 server / share"}), 400
        if ptype == "share" and not str(data.get("index_path") or "").strip():
            return jsonify({"error": "share 源需要 index_path"}), 400
        from client_config import load_client_config, save_client_config
        cfg = dict(load_client_config(build_cfg))
        cfg["plugin_source"] = {"type": ptype,
                                "index_path": str(data.get("index_path") or "")}
        save_client_config(build_cfg, cfg)
        return jsonify({"ok": True, "plugin_source": cfg["plugin_source"],
                        "note": "已写入客户端配置，立即生效（免重启）"})

    def _rescan():
        nonlocal local_manifests
        local_manifests[:] = local_registry.scan_local_plugins(local_plugins_dir, _log)
        app.config["LOCAL_MANIFESTS"] = local_manifests
        local_states.clear()
        _refresh_local_states()
        alive = {m.get("id") for m in local_manifests}
        for pid in list(hybrid_apps):
            if pid not in alive:
                hybrid_apps.pop(pid, None)    # 卸载即弹（热插拔 L2/L3）

    _on_root_change["fn"] = _rescan       # 账号切换 → 数据根变更后重挂 hybrid 子应用

    def _rescan_frontends():
        """重扫 server 前端包注册表（装卸后调用；停用只读配置，无需重扫）。"""
        frontends.clear()
        frontends.update({f["id"]: f for f in pkg_install.scan_frontends(frontends_dir)})

    def _frontend_enabled(pid):
        """server 前端本地直出开关：客户端配置 local_state（停用 = 回退中继直出）。"""
        from client_config import load_client_config
        return bool((load_client_config(build_cfg).get("local_state") or {}).get(pid, True))

    # ---------------- 中继（其余全部转发远程） ----------------

    def client_headers():
        h = {}
        for k, v in request.headers.items():
            if k.lower() in _HOP_HEADERS or k.lower() == "content-length":
                continue
            h[k] = v
        # 会话镜像补位（T14）：浏览器 Cookie 优先，同名镜像让位
        mc = mirror.header()
        if mc:
            existing = h.pop("Cookie", None)
            parts = [p.strip() for p in existing.split(";")] if existing else []
            name = mc.split("=", 1)[0]
            if not any(p.startswith(name + "=") for p in parts):
                parts.append(mc)
            if parts:
                h["Cookie"] = "; ".join(parts)
        # 壳标识（需求① G2-2）：服务器壳门凭此放行业务面；未配置密钥=不带（服务端 403 给提示）
        from client_config import shell_headers, load_client_config
        h.update(shell_headers(build_cfg, load_client_config(build_cfg)))
        return h

    def forward(path):
        """T03 spike 口径的转发 + 会话镜像维护（T14）。"""
        if not state["address"]:
            return _fallback_response()
        qs = request.query_string.decode("utf-8", "replace")
        url = state["address"] + "/" + path + (("?" + qs) if qs else "")
        headers = client_headers()
        data = None
        if request.method in ("POST", "PUT", "PATCH"):
            body = tempfile.SpooledTemporaryFile(max_size=512 * 1024)
            shutil.copyfileobj(request.stream, body, 1024 * 1024)
            body.seek(0)
            data = body
            if request.content_length:
                headers["Content-Length"] = str(request.content_length)
        try:
            r = upstream.request(request.method, url, headers=headers, data=data,
                                 stream=True, allow_redirects=False, timeout=(5, 300))
        except requests.RequestException as exc:
            health.mark(False, "%s: %s" % (type(exc).__name__, exc))
            return _fallback_response(upstream_error=str(exc))
        health.mark(True)
        # 服务器壳门拒绝（G2-4）：接入密钥缺失/不正确——专用提示面，**不进**会话失效
        # 分支（不清会话镜像，登录态与门状态无关）；页面给本地提示页，API 透传 JSON 契约。
        if r.status_code == 403 and r.headers.get("X-JZ-Gate") == "shell_required":
            gate["blocked"] = True
            gate["ts"] = time.time()
            gate["message"] = "接入密钥未配置或不正确，请向管理员索取并在连接设置中填写"
            _log("服务器壳门拒绝（%s %s）：接入密钥缺失或不正确" % (request.method, path))
            if _wants_page():
                return Response(_GATE_BLOCK_HTML, status=403,
                                content_type="text/html; charset=utf-8")
            return jsonify({"error": "shell_required", "hint": gate["message"],
                            "gate_blocked": True}), 403
        sc = r.headers.get("Set-Cookie")          # 滑动续期/登录都会重发
        if sc:
            mirror.update_from_set_cookie(sc)
        if r.status_code == 401:                  # 会话失效统一信号
            mirror.clear()
        if r.status_code != 403:                  # 门已放行一次即视为密钥正常（状态复位）
            if gate["blocked"]:
                gate["blocked"] = False
                gate["message"] = ""
        out = [(k, v) for k, v in r.headers.items() if k.lower() not in _DROP_RESP]
        return Response(r.iter_content(chunk_size=64 * 1024),
                        status=r.status_code, headers=out)

    def _fallback_response(upstream_error=""):
        # 页面请求 → 本地兜底页；API 请求 → 502 JSON（都不依赖远程渲染）
        if _wants_page():
            resp = send_from_directory(fallback_dir_or(app), "fallback.html")
            resp.status_code = 503
            return resp
        return jsonify({"error": "远程服务器不可达",
                        "detail": upstream_error,
                        "address": state["address"] or ""}), 502

    def _wants_page():
        if request.path.startswith("/api/"):
            return False
        accept = request.headers.get("Accept", "")
        return "text/html" in accept or "*/*" in accept or accept == ""

    # ---------------- 清单合并与本地工具元数据（T16） ----------------

    def _local_only_payload(gate_blocked=False):
        # F5（批次 9 T-O9-5，Q3 配套）：远程不可达/未配置时降级为仅本地条目——
        # 本地条目（local-plugins）是离线网格的唯一载体，A 类离线显兜底页（既有口径）
        enabled = [m for m in local_manifests
                   if local_states.get(m.get("id"), {}).get("status") != "disabled"]
        reg = _account_reg()
        if reg is not None:
            # 账号模式离线（G3-5）：网格按快照登记显示——登记 ∧ 本机副本在 → 正常卡；
            # B/C 登记 ∧ 副本缺 → 云卡但下载禁用（D15：离线无包可取，给明确原因不静默）；
            # A 类云卡离线不合成（其运行面本就需要服务器）。
            tools, order = [], 1000
            for pid in sorted(reg.keys()):
                rec = reg.get(pid) or {}
                if rec.get("enabled") is False:
                    continue
                m = next((x for x in enabled if x.get("id") == pid), None)
                if m is not None:
                    tools.append(local_registry.tool_item(m, order))
                    order += 1
                elif str(pid).startswith("local-"):
                    tools.append({"id": pid, "name": pid,
                                  "description": "该插件未装到本机，联网后可从云端下载",
                                  "icon": "🧩", "accent": "#9AA0A6",
                                  "entry": "index.html", "features": [],
                                  "category": local_registry.LOCAL_CATEGORY["name"],
                                  "category_id": "local", "order": order,
                                  "health": "ok", "health_degraded": [],
                                  "version": rec.get("version") or "",
                                  "cloud": True, "machine_present": False,
                                  "account_installed": True, "offline": True})
                    order += 1
            payload = {"tools": tools,
                       "categories": [dict(local_registry.LOCAL_CATEGORY)],
                       "degraded": True, "account_mode": True}
        else:
            payload = {"tools": [_local_item_with_health(m, 1000 + i)
                                 for i, m in enumerate(enabled)],
                       "categories": [dict(local_registry.LOCAL_CATEGORY)],
                       "degraded": True}
            if _account.get("signed_out"):
                # D19：登出/401 后网格空 + 登录引导（离线亦然，快照保留不展示）
                payload = {"tools": [], "categories": [], "degraded": True,
                           "signed_out": True, "account_mode": True}
        if gate_blocked:
            # 服务器壳门拒绝（G2-4）：带标记让主界面给可执行指引（main.js 渲染横幅）
            payload["gate_blocked"] = True
            payload["gate_message"] = gate["message"]
        return payload

    def _local_only_tools_response():
        return jsonify(_local_only_payload())

    @app.get("/api/tools")
    def api_tools_merged():
        # 注意：本装饰器必须紧跟本函数——F5 曾把辅助函数插在两者之间，致 /api/tools
        # 全局误走降级分支（2026-10-08 台架抓出；插入带装饰器函数前一律锚定装饰器行）
        _refresh_local_verdicts()      # 每次加载主界面复评三态（依赖装卸后徽标即时翻转）
        if not state["address"]:
            return _local_only_tools_response()
        headers = client_headers()
        try:
            r = upstream.get(state["address"] + "/api/tools",
                             headers=headers, timeout=(5, 60))
        except requests.RequestException as exc:
            health.mark(False, "%s: %s" % (type(exc).__name__, exc))
            return _local_only_tools_response()
        health.mark(True)
        # 服务器壳门拒绝（G2-4）：降级仅本地清单 + gate_blocked 标记（主界面渲染指引横幅）
        if r.status_code == 403 and r.headers.get("X-JZ-Gate") == "shell_required":
            gate["blocked"] = True
            gate["ts"] = time.time()
            gate["message"] = "接入密钥未配置或不正确，请向管理员索取并在连接设置中填写"
            _log("服务器壳门拒绝 /api/tools：接入密钥缺失或不正确（降级仅本地清单）")
            return jsonify(_local_only_payload(gate_blocked=True))
        sc = r.headers.get("Set-Cookie")
        if sc:
            mirror.update_from_set_cookie(sc)
        if r.status_code == 401:
            mirror.clear()
        if r.status_code != 200:
            return Response(r.content, status=r.status_code,
                            content_type=r.headers.get("Content-Type", "application/json"))
        try:
            data = r.json()
        except Exception:
            return Response(r.content, status=200,
                            content_type=r.headers.get("Content-Type", "application/json"))
        base = data.get("tools") if isinstance(data, dict) else data
        base = base if isinstance(base, list) else []
        refresh_account()                     # 账号上下文同步（登录/切号/登记刷新，60s TTL）
        reg = _account_reg()
        if reg is not None:
            # 账号级安装（2026-10-11 需求①②）：登记为显示全集 + 双源匹配 + 云卡（§2.2/D22）。
            # 本机 hidden 过滤不再参与（登记制取代本机隐藏；升级补种后 hidden 键已清）。
            return jsonify(_account_grid_payload(data if isinstance(data, dict) else {},
                                                 base, reg))
        if _account.get("signed_out"):
            # D19：登出/401 后网格空 + 登录引导（快照保留内容但不展示）
            return jsonify({"tools": [], "categories": [], "signed_out": True,
                            "account_mode": True})
        # 本机移除（legacy：旧服务器无 installs API 时的机器级行为，G2-7 保留）：
        # 服务器侧条目在本机不再下发。只影响本机（客户端数据目录 config.json），
        # 服务器端运行与全局清单不受影响。
        hidden = set(hidden_tools(build_cfg))
        if hidden:
            dropped = [str(t.get("id") or "") for t in base if str(t.get("id") or "") in hidden]
            base = [t for t in base if str(t.get("id") or "") not in hidden]
            if dropped:
                _log("本机已移除的工具不再下发：%s" % "、".join(dropped))
        start = max([t.get("order", 0) for t in base] or [0]) + 1000
        # 停用即从工具网格隐藏（需求⑦：停用为一等操作；本地 API 404 语义保留作兜底）
        enabled_manifests = [m for m in local_manifests
                             if local_states.get(m.get("id"), {}).get("status") != "disabled"]
        merged = local_registry.tool_items(enabled_manifests, start)
        categories = data.get("categories") if isinstance(data, dict) else None
        if isinstance(categories, list) and all(c.get("id") != "local" for c in categories):
            categories.append(dict(local_registry.LOCAL_CATEGORY))
        if isinstance(data, dict):
            data["tools"] = base + merged
        else:
            data = base + merged
        return jsonify(data)

    @app.get("/api/tools/<tool_id>")
    def api_tool_meta(tool_id):
        if tool_id.startswith("local-"):
            m = local_registry.find(local_manifests, tool_id)
            if m is None:
                return jsonify({"error": "本地插件不存在"}), 404
            return jsonify(local_registry.tool_item(m, 0))
        return forward("api/tools/" + tool_id)

    @app.route("/local-api/<path:_rest>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    def local_api_fallback(_rest):
        """本地接口兜底：/local-api/* 未命中任何端点 → 404（不落中继兜底）。

        hidden_tools 写链退役（2026-10-11 账号级批次）后 /local-api/plugins/hidden 等退役
        端点由此给出明确的 404，而不是撞 <path:path> 兜底被转发到远程（smoke 抓出 502）。"""
        return jsonify({"error": "接口不存在或已退役", "path": "/local-api/" + _rest}), 404

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    def relay(path):
        # ② 清单合并（批次 3 T16）——本批次原样中继
        return forward(path)

    # 兜底页目录：由 fallback_file 推导（同一目录）
    def fallback_dir_or(_app):
        import os
        return os.path.dirname(fallback_file)

    app.config["CAPABILITY_HEALTH"] = health
    app.config["CAPABILITY_STATE"] = state
    return app
