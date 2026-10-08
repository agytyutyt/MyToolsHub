# -*- coding: utf-8 -*-
r"""client-plugins/local-trajectory-sketch/backend 适配补丁（批次 11，C 类收尾）。

流程（幂等：每次先整体重拷服务器后端，再打三处手术）：
  1) 复制 plugins/trajectory-sketch/backend → client-plugins/local-trajectory-sketch/backend
     （排除 __pycache__ 与 test_*）；
  2) llm_client.py → **客户端网关**（可用性探服务器 /status，语义匹配经 own() 调服务器
     /llm/match-columns，Key 不落本机；对齐 file-filter 样板的 llm_gateway 口径）；
  3) config_store.py → 配置权威在服务器（own() GET 取回 + 本地 config.json 作离线缓存），
     保存经 own() 写服务器（管理员权限由服务器以会话判定）；
  4) routes.py → 配置保存端点适配 save_config 新契约（返回 (err, status)）。
"""
import io
import os
import py_compile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "plugins", "trajectory-sketch", "backend")
DST = os.path.join(ROOT, "client-plugins", "local-trajectory-sketch", "backend")

# ---------------- 1) 复制 ----------------
if os.path.isdir(DST):
    shutil.rmtree(DST)
shutil.copytree(SRC, DST,
                ignore=shutil.ignore_patterns("__pycache__", "test_*.py"))
print("复制后端 →", os.path.relpath(DST, ROOT))

# ---------------- 2) llm_client.py → 客户端网关 ----------------
CLIENT_LLM = '''# -*- coding: utf-8 -*-
"""llm_client —— 统一大模型的客户端网关（存量适配 2026-10-08，C 类客户端工件专用）。

服务器侧统一大模型（jz_llm）的能力经 jz_relay.own() 触达：本模块只回答「能不能用」
（探服务器 /status 的 llm_configured），语义匹配全部发生在服务器工件端点
（POST /api/trajectory-sketch/llm/match-columns），Key 不落本机（红线 5 / L-10）。
缓存 60s；离线时按「不可用」降级（分析仍可走硬过滤档，界面明示）。
"""
import time

PLUGIN_ID = "trajectory-sketch"
LLM_TIMEOUT = 120


class LLMError(Exception):
    """大模型不可用 / 调用失败（与服务器侧 jz_llm.LLMError 同名同义，调用点无需改）。"""


_CACHE = {"ts": 0.0, "ok": False, "reason": "尚未探测"}
REQUESTS_AVAILABLE = True     # 由 _probe() 按服务器 /status 如实刷新（本机不再直连大模型）


def _own():
    import jz_relay
    return jz_relay.own("local-trajectory-sketch")


class GatewaySession(object):
    """jz_llm.resolve() 返回形态的最小兼容面（configured/provider/reason/source）。"""

    def __init__(self, ok, reason):
        self.provider = {}
        self.reason = reason
        self.source = "server"
        self._ok = ok

    def configured(self):
        return self._ok


def _probe():
    if time.time() - _CACHE["ts"] < 60:
        return
    try:
        r = _own().get("/api/trajectory-sketch/status", timeout=15)
        d = r.json() or {}
        ok = bool(d.get("llm_configured"))
        deps = d.get("dependencies") or {}
        globals()["REQUESTS_AVAILABLE"] = bool(deps.get("requests", True))
        _CACHE.update(ts=time.time(), ok=ok,
                      reason="" if ok else "服务器端大模型未配置（管理后台 → 大模型设置）")
    except Exception as exc:
        _CACHE.update(ts=time.time(), ok=False,
                      reason="服务器暂不可用，本地部分可继续（%s）" % type(exc).__name__)


def invalidate():
    """服务器语义段连接失败时调用：立即使可用性缓存失效（后续请求在入口即降级）。"""
    _CACHE.update(ts=0.0, ok=False, reason="服务器暂不可用")


def available():
    _probe()
    return bool(_CACHE["ok"])


def session():
    """取当前生效的接入配置快照（网关视图；只能在请求线程内调用，与服务器侧同纪律）。"""
    _probe()
    return GatewaySession(_CACHE["ok"], _CACHE["reason"])


def resolve(plugin_id=""):
    return session()


def default_prompt():
    """提示词归服务器（Key 与提示词同侧）；本地不需要，保留空实现兼容调用点。"""
    return {}


def match_columns(headers, keep_columns, session=None, timeout=LLM_TIMEOUT):
    """经 own() 调服务器语义匹配端点：{表头: 保留字段原文 or ""}。

    防幻觉名单过滤在服务器完成（名单外值→空）；离线/未配置抛 LLMError，由调用方
    转成可展示提示（分析任务内自动回退硬过滤档）。
    """
    if not headers:
        raise LLMError("表格字段为空，无法匹配")
    keep = [str(k).strip() for k in (keep_columns or []) if str(k).strip()]
    if not keep:
        raise LLMError("保留字段名单为空，无法匹配（请先在「⚙️ 配置」中设定保留字段）")
    try:
        r = _own().post("/api/trajectory-sketch/llm/match-columns",
                        json={"headers": [str(h) for h in headers], "keep_columns": keep},
                        timeout=timeout)
    except Exception as exc:
        invalidate()
        raise LLMError("服务器不可达（大模型辅助需联网）：%s" % type(exc).__name__)
    if r.status_code == 409:
        d = r.json() or {}
        raise LLMError(str(d.get("error") or "服务器端大模型未配置"))
    if r.status_code != 200:
        raise LLMError("大模型匹配失败（HTTP %s）" % r.status_code)
    d = r.json() or {}
    m = d.get("matches")
    if not isinstance(m, dict):
        raise LLMError("服务器返回缺少 matches 对象")
    return {str(h): str(m.get(str(h)) or "") for h in headers}
'''
io.open(os.path.join(DST, "llm_client.py"), "w", encoding="utf-8", newline="").write(CLIENT_LLM)
print("llm_client.py → 客户端网关（own() 语义匹配）")

# ---------------- 3) config_store.py：服务器权威 + 本地缓存 ----------------
P = os.path.join(DST, "config_store.py")
s = io.open(P, encoding="utf-8", newline="").read()

old_load = '''def load_config() -> Dict[str, Any]:
    """读取配置并与默认值合并（缺键补默认值；文件损坏退回全默认）。"""
    raw: Dict[str, Any] = {}
    path = config_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                raw = data
        except Exception:
            raw = {}
    cfg = _deep_merge(DEFAULT_CONFIG, raw)
    cfg["filter"] = _sanitize_filter(cfg.get("filter"))
    return cfg'''
new_load = '''def _read_cache() -> Dict[str, Any]:
    """本地配置缓存（离线兜底；服务器可达时会被刷新）。"""
    path = config_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _write_cache(raw: Dict[str, Any]) -> None:
    """原子写本地缓存（tmp + os.replace）。"""
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def load_config() -> Dict[str, Any]:
    """配置：**权威在服务器**（管理员统一维护）；本地 config.json 作离线缓存兜底。

    服务器可达 → 取回（按本地口径规整前先刷缓存）；不可达/未登录 → 用最近缓存；
    首装无缓存 → 代码默认值（DEFAULT_CONFIG；规范：代码默认值=配置同源）。
    C 类拆分口径见《存量插件三类适配-设计文档》§3.16（2026-10-08）。
    """
    got = None
    try:
        import jz_relay
        r = jz_relay.own("local-trajectory-sketch").get(
            "/api/trajectory-sketch/config", timeout=30)
        if r.status_code == 200:
            d = r.json() or {}
            cfg = d.get("config") if isinstance(d.get("config"), dict) else d
            if isinstance(cfg, dict) and cfg:
                got = cfg
                _write_cache(cfg)
    except Exception:
        got = None
    raw = got if isinstance(got, dict) and got else _read_cache()
    cfg = _deep_merge(DEFAULT_CONFIG, raw)
    cfg["filter"] = _sanitize_filter(cfg.get("filter"))
    return cfg'''
assert s.count(old_load) == 1, "load_config 锚点未命中"
s = s.replace(old_load, new_load, 1)

old_save = '''def save_config(cfg: Dict[str, Any]) -> None:
    """原子写盘（tmp + os.replace），避免半截文件。"""
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    clean = _deep_merge(DEFAULT_CONFIG, cfg)
    clean["filter"] = _sanitize_filter(clean.get("filter"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)'''
new_save = '''def save_config(cfg: Dict[str, Any]):
    """配置写**透传到服务器**（管理员权限由服务器以会话判定）；本地留一份缓存。

    返回 ``(error_message, http_status)``：成功 = ``(None, 0)``；失败给可展示错误
    （服务器 403 权限不足原样透传状态码；不可达按 502）。C 类拆分口径 §3.16。
    """
    clean = _deep_merge(DEFAULT_CONFIG, cfg)
    clean["filter"] = _sanitize_filter(clean.get("filter"))
    _write_cache(clean)
    try:
        import jz_relay
        r = jz_relay.own("local-trajectory-sketch").post(
            "/api/trajectory-sketch/config", json=clean, timeout=30)
    except Exception as exc:
        return ("服务器不可达，配置未保存（%s）" % type(exc).__name__, 502)
    if r.status_code != 200:
        msg = ""
        try:
            msg = str((r.json() or {}).get("error") or "")
        except Exception:
            msg = ""
        return (msg or "保存失败（HTTP %s）" % r.status_code, r.status_code)
    return (None, 0)'''
assert s.count(old_save) == 1, "save_config 锚点未命中"
s = s.replace(old_save, new_save, 1)
io.open(P, "w", encoding="utf-8", newline="").write(s)
print("config_store.py → 服务器权威 + 本地缓存 / 保存透传")

# ---------------- 4) routes.py：保存端点适配新契约 ----------------
P = os.path.join(DST, "routes.py")
s = io.open(P, encoding="utf-8", newline="").read()
old_post = '''        config_store.save_config(cfg)
        _set_operation("保存轨迹速写配置")
        return jsonify({"ok": True})'''
new_post = '''        err, status = config_store.save_config(cfg)     # C 类拆分：写透传服务器（本机留缓存）
        if err:
            return jsonify({"error": err}), status or 502
        _set_operation("保存轨迹速写配置")
        return jsonify({"ok": True})'''
assert s.count(old_post) == 1, "保存端点锚点未命中"
s = s.replace(old_post, new_post, 1)
io.open(P, "w", encoding="utf-8", newline="").write(s)
print("routes.py → 保存端点适配（err, status）契约")

for f in ("llm_client.py", "config_store.py", "routes.py"):
    py_compile.compile(os.path.join(DST, f), doraise=True)
print("adapt-ts-client：完成 + 编译通过")
